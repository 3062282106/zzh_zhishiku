"""Stage explicitly requested Feishu imports under the requesting user's permissions."""

from base64 import urlsafe_b64encode
from dataclasses import dataclass
from hashlib import sha256
import logging
import os
from secrets import token_urlsafe
from threading import Lock
from time import monotonic
from urllib.parse import urlencode, urlsplit
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken
from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row

from access import ADMIN_MCP_PREFIX, is_admin_mcp_token, member_web_identity, token_hash
from admin import ManagementError, activate, normalize_member_email, record_ingest, resolve_session
from feishu_source import call, fetch_feishu_document, parse_link
from notion_source import SourceError, bind_source, stage_source_snapshot


CALLBACK = "/v1/knowledge/feishu/callback"
ACCOUNTS = "https://accounts.feishu.cn"
SCOPES = "docx:document:readonly wiki:wiki:readonly contact:user.email:readonly contact:user.employee:readonly"


@dataclass(frozen=True)
class Importer:
    role: str
    id: str
    email: str

    @property
    def key(self):
        return self.role, self.id


@dataclass(frozen=True)
class OAuthRequest:
    actor: Importer
    web_token: str
    verifier: str
    redirect: str
    offline: bool = False


class ExpiringCache:
    """One API worker serves the console and the MCP adapter through loopback."""

    def __init__(self):
        self.items = {}
        self.lock = Lock()

    def put(self, key, value, lifetime):
        now = monotonic()
        with self.lock:
            self.items = {k: item for k, item in self.items.items() if item[0] > now}
            self.items.pop(key, None)
            if len(self.items) >= 512:
                self.items.pop(next(iter(self.items)))
            self.items[key] = (now + lifetime, value)

    def get(self, key, *, consume=False):
        with self.lock:
            item = self.items.pop(key, None) if consume else self.items.get(key)
            if item and item[0] > monotonic():
                return item[1]
            self.items.pop(key, None)


oauth_requests = ExpiringCache()


def web_importer(db, token):
    if member := member_web_identity(db, token):
        email, = db.execute("SELECT email FROM member_account WHERE id=%s", (member[0],)).fetchone()
        return Importer("member", member[0], (email or "").strip().lower())
    if admin := resolve_session(db, token):
        email, = db.execute("""
            SELECT COALESCE(email, CASE WHEN position('@' in username) > 0 THEN username END, '')
            FROM admin_account WHERE id=%s
        """, (admin["id"],)).fetchone()
        return Importer("admin", admin["id"], email.strip().lower())
    raise SourceError("请登录知识库网页。", 401)


def import_identity(db, token, *, lock=False):
    suffix = " FOR UPDATE OF a" if lock else ""
    digest = token_hash(token)
    if is_admin_mcp_token(token):
        row = db.execute("""
            SELECT a.id, COALESCE(a.email, CASE WHEN position('@' in a.username) > 0 THEN a.username END, '')
            FROM admin_import_token t JOIN admin_account a ON a.id=t.admin_id
            WHERE t.token_hash=%s AND a.enabled
        """ + suffix, (digest,)).fetchone()
        if not row:
            raise SourceError("管理员 MCP 令牌无效或已停用。", 401)
        return Importer("admin", row[0], row[1].strip().lower())
    row = db.execute("""
        SELECT a.id, a.email, a.personal_upload FROM member_account a
        WHERE a.token_hash=%s AND a.enabled
    """ + suffix, (digest,)).fetchone()
    if not row:
        raise SourceError("MCP 上传凭证无效或已停用。", 401)
    if not row[2]:
        raise SourceError("你的账号尚未开通上传权限，请联系管理员。", 403)
    return Importer("member", row[0], (row[1] or "").strip().lower())


def upload_enabled(db, actor):
    if actor.role == "member":
        return db.execute("SELECT 1 FROM member_account WHERE id=%s AND enabled AND personal_upload",
                          (actor.id,)).fetchone() is not None
    return db.execute("SELECT 1 FROM admin_upload_grant WHERE admin_id=%s LIMIT 1",
                      (actor.id,)).fetchone() is not None


def admin_import_access(db, identifier):
    row = db.execute("""
        SELECT COALESCE(email, CASE WHEN position('@' in username) > 0 THEN username END, ''),
               EXISTS (SELECT 1 FROM admin_import_token WHERE admin_id=a.id)
        FROM admin_account a WHERE id=%s
    """, (identifier,)).fetchone()
    if not row:
        raise SourceError("找不到管理员。", 404)
    grants = db.execute("SELECT library FROM admin_upload_grant WHERE admin_id=%s ORDER BY library",
                        (identifier,)).fetchall()
    return {"email": row[0], "has_token": row[1], "libraries": [item[0] for item in grants]}


def set_admin_import_access(db, identifier, email, libraries):
    email = normalize_member_email(email) if email.strip() else None
    try:
        with db.transaction():
            if email and db.execute("SELECT 1 FROM admin_account WHERE lower(username)=%s AND id<>%s",
                                    (email, identifier)).fetchone():
                raise SourceError("该企业邮箱已绑定其他管理员。", 409)
            if not db.execute("UPDATE admin_account SET email=%s WHERE id=%s", (email, identifier)).rowcount:
                raise SourceError("找不到管理员。", 404)
            allowed = db.execute("""
                SELECT id FROM knowledge_base WHERE owner_member_id IS NULL AND id=ANY(%s)
            """, (libraries,)).fetchall()
            if len(allowed) != len(set(libraries)):
                raise SourceError("只能授权存在的企业知识库。", 422)
            if allowed and not admin_import_access(db, identifier)["email"]:
                raise SourceError("开通上传前请填写企业邮箱。", 422)
            db.execute("DELETE FROM admin_upload_grant WHERE admin_id=%s", (identifier,))
            with db.cursor() as cur:
                cur.executemany("INSERT INTO admin_upload_grant (admin_id, library) VALUES (%s,%s)",
                                [(identifier, library) for library, in allowed])
    except UniqueViolation:
        raise SourceError("该企业邮箱已绑定其他管理员。", 409) from None
    return admin_import_access(db, identifier)


def issue_admin_import_token(db, identifier):
    token = ADMIN_MCP_PREFIX + token_urlsafe(32)
    with db.transaction():
        if not db.execute("SELECT 1 FROM admin_account WHERE id=%s AND enabled FOR UPDATE", (identifier,)).fetchone():
            raise SourceError("找不到已启用的管理员。", 404)
        actor = Importer("admin", identifier, "")
        if not upload_enabled(db, actor):
            raise SourceError("请先授权一个企业知识库。", 403)
        db.execute("""
            INSERT INTO admin_import_token (admin_id, token_hash) VALUES (%s,%s)
            ON CONFLICT (admin_id) DO UPDATE SET token_hash=EXCLUDED.token_hash
        """, (identifier, token_hash(token)))
    return token


def oauth_settings():
    app = os.environ.get("KB_FEISHU_APP_ID", "").strip()
    secret = os.environ.get("KB_FEISHU_APP_SECRET", "").strip()
    redirect = os.environ.get("KB_FEISHU_OAUTH_REDIRECT_URI", "").strip()
    try:
        parts = urlsplit(redirect)
        valid = (parts.scheme == "https" and parts.hostname and not parts.username and not parts.password
                 and parts.path == CALLBACK and not parts.query and not parts.fragment)
    except ValueError:
        valid = False
    if not (app and secret and valid):
        raise SourceError("服务端尚未启用飞书用户授权，请联系管理员。", 503)
    return app, secret, redirect


def console_path(actor):
    return "/admin/query" if actor.role == "member" else "/admin/operations/members"


def console_origin(redirect):
    parts = urlsplit(redirect)
    return f"{parts.scheme}://{parts.netloc}"


def begin_oauth(db, token, state, *, offline=False):
    actor = web_importer(db, token)
    if not upload_enabled(db, actor):
        raise SourceError("你的账号尚未开通上传权限。", 403)
    if not actor.email:
        raise SourceError("请先在知识库后台绑定你的企业邮箱。", 409)
    app, _, redirect = oauth_settings()
    token_cipher()
    verifier = token_urlsafe(48)
    challenge = urlsafe_b64encode(sha256(verifier.encode()).digest()).decode().rstrip("=")
    oauth_requests.put(state, OAuthRequest(actor, token, verifier, redirect, offline), 300)
    scope = SCOPES + " offline_access" if offline else SCOPES
    query = urlencode({"client_id": app, "response_type": "code", "redirect_uri": redirect, "scope": scope, "state": state,
                       "code_challenge": challenge, "code_challenge_method": "S256", "prompt": "consent"})
    return ACCOUNTS + "/open-apis/authen/v1/authorize?" + query


class OAuthFailure(SourceError):
    def __init__(self, reason, provider_code=None):
        super().__init__(reason)
        self.reason = reason
        self.provider_code = provider_code


def oauth_call(step, path, **options):
    try:
        return call(path, **options)
    except SourceError as error:
        provider_code = getattr(error, "provider_code", None)
        logging.getLogger(__name__).warning(
            "Feishu OAuth failed at %s (status=%s, provider_code=%s): %s",
            step, getattr(error, "http_status", error.status), provider_code, error,
        )
        raise OAuthFailure(step, provider_code) from None


def complete_oauth(db, pending, code):
    if web_importer(db, pending.web_token) != pending.actor or not upload_enabled(db, pending.actor):
        raise OAuthFailure("session")
    app, secret, redirect = oauth_settings()
    if redirect != pending.redirect:
        raise SourceError("飞书授权配置已变更，请重试。", 409)
    # Feishu v3 returned 20049 for the documented S256 flow in production.
    # v2 supports this authorization endpoint's PKCE exchange.
    data = oauth_call("token", "/authen/v2/oauth/token", body={
        "grant_type": "authorization_code", "client_id": app, "client_secret": secret,
        "code": code, "redirect_uri": redirect, "code_verifier": pending.verifier,
    })
    access_token, expires_in = data.get("access_token"), data.get("expires_in")
    if not isinstance(access_token, str) or not access_token or type(expires_in) is not int or expires_in <= 30:
        logging.getLogger(__name__).warning("Feishu OAuth token response lacks a usable token or expiry")
        raise OAuthFailure("token")
    info = oauth_call("identity", "/authen/v1/user_info", token=access_token).get("data", {})
    if not isinstance(info, dict):
        raise OAuthFailure("identity")
    email = info.get("enterprise_email") or info.get("email")
    email = email.strip().lower() if isinstance(email, str) else ""
    if not email:
        raise OAuthFailure("email_missing")
    if email != pending.actor.email:
        raise OAuthFailure("mismatch")
    if web_importer(db, pending.web_token) != pending.actor or not upload_enabled(db, pending.actor):
        raise OAuthFailure("session")
    refresh, refresh_lifetime = refresh_grant(data)
    save_user_token(db, pending.actor, app, access_token, expires_in - 30,
                    refresh_token=refresh, refresh_lifetime=refresh_lifetime)
    if pending.offline and not refresh:
        raise OAuthFailure("offline_missing")


def token_cipher():
    key = os.environ.get("KB_FEISHU_TOKEN_KEY", "").strip()
    try:
        return Fernet(key.encode("ascii"))
    except ValueError:
        raise SourceError("飞书授权的加密存储尚未配置，请联系管理员。", 503) from None


def refresh_grant(data):
    refresh, lifetime = data.get("refresh_token"), data.get("refresh_token_expires_in")
    return (refresh, lifetime - 30) if isinstance(refresh, str) and refresh and type(lifetime) is int and lifetime > 30 else (None, None)


def save_user_token(db, actor, app_id, access_token, lifetime, *, refresh_token=None, refresh_lifetime=None):
    cipher = token_cipher()
    encrypted = cipher.encrypt(access_token.encode())
    refresh = cipher.encrypt(refresh_token.encode()) if refresh_token else None
    with db.transaction():
        db.execute("""
            INSERT INTO feishu_user_authorization AS stored
                (role, account_id, email, app_id, ciphertext, expires_at, refresh_ciphertext, refresh_expires_at)
            VALUES (%s,%s,%s,%s,%s,statement_timestamp() + make_interval(secs => %s),
                    %s,statement_timestamp() + make_interval(secs => %s))
            ON CONFLICT (role, account_id) DO UPDATE SET email=EXCLUDED.email, app_id=EXCLUDED.app_id,
                ciphertext=EXCLUDED.ciphertext, expires_at=EXCLUDED.expires_at,
                refresh_ciphertext=COALESCE(EXCLUDED.refresh_ciphertext,
                    CASE WHEN stored.email=EXCLUDED.email AND stored.app_id=EXCLUDED.app_id THEN stored.refresh_ciphertext END),
                refresh_expires_at=COALESCE(EXCLUDED.refresh_expires_at,
                    CASE WHEN stored.email=EXCLUDED.email AND stored.app_id=EXCLUDED.app_id THEN stored.refresh_expires_at END)
        """, (*actor.key, actor.email, app_id, encrypted, lifetime, refresh, refresh_lifetime))


AUTHORIZATION = """
    FROM feishu_user_authorization WHERE role=%s AND account_id=%s AND email=%s AND app_id=%s
"""


def authorization_status(db, actor):
    row = db.execute("""
        SELECT expires_at > statement_timestamp(),
               COALESCE(refresh_expires_at > statement_timestamp(), false) AND refresh_ciphertext IS NOT NULL
    """ + AUTHORIZATION, (*actor.key, actor.email, os.environ.get("KB_FEISHU_APP_ID", "").strip())).fetchone()
    current, renewable = row or (False, False)
    return {"authorized": current or renewable, "auto_sync_authorized": renewable}


def decrypt_token(ciphertext):
    try:
        return token_cipher().decrypt(bytes(ciphertext)).decode()
    except (InvalidToken, UnicodeError):
        logging.getLogger(__name__).warning("Stored Feishu authorization could not be decoded")
        return None


def user_token(db, actor):
    app = os.environ.get("KB_FEISHU_APP_ID", "").strip()
    with db.transaction():
        # A refresh token rotates on use, so API requests and background sync must share this lock.
        row = db.execute("""
            SELECT CASE WHEN expires_at > statement_timestamp() THEN ciphertext END,
                   CASE WHEN refresh_expires_at > statement_timestamp() THEN refresh_ciphertext END
        """ + AUTHORIZATION + " FOR UPDATE", (*actor.key, actor.email, app)).fetchone()
        if not row:
            return None
        if row[0] is not None:
            return decrypt_token(row[0])
        refresh = decrypt_token(row[1]) if row[1] is not None else None
        if not refresh:
            return None
        _, secret, _ = oauth_settings()
        try:
            data = call("/oauth/v3/token", origin=ACCOUNTS, body={
                "grant_type": "refresh_token", "client_id": app, "client_secret": secret, "refresh_token": refresh,
            })
        except SourceError as error:
            if getattr(error, "provider_code", None) not in {20010, 20026, 20037}:
                raise
            db.execute("UPDATE feishu_user_authorization SET refresh_ciphertext=NULL, refresh_expires_at=NULL "
                       "WHERE role=%s AND account_id=%s", actor.key)
            return None
        access, lifetime = data.get("access_token"), data.get("expires_in")
        rotated, refresh_lifetime = refresh_grant(data)
        if not isinstance(access, str) or not access or type(lifetime) is not int or lifetime <= 30 or not rotated:
            raise SourceError("飞书未返回可续期授权，请在账号页重新授权。", 409)
        save_user_token(db, actor, app, access, lifetime - 30,
                        refresh_token=rotated, refresh_lifetime=refresh_lifetime)
        return access


def personal_library(db, member_id, *, create=False):
    if create:
        db.execute("""
            INSERT INTO knowledge_base (id, name, owner_member_id) VALUES (%s, '个人知识库', %s)
            ON CONFLICT (owner_member_id) WHERE owner_member_id IS NOT NULL DO NOTHING
        """, ("personal_" + uuid4().hex, member_id))
    row = db.execute("SELECT id FROM knowledge_base WHERE owner_member_id=%s", (member_id,)).fetchone()
    return row[0] if row else None


def uploadable_libraries(db, actor):
    if actor.role == "member":
        return [{"name": "个人知识库", "id": "个人知识库"}]
    return [{"id": key, "name": name} for key, name in db.execute("""
        SELECT k.id, k.name FROM admin_upload_grant g JOIN knowledge_base k ON k.id=g.library
        WHERE g.admin_id=%s AND k.owner_member_id IS NULL ORDER BY k.name, k.id
    """, (actor.id,))]


def target_library(db, actor, target):
    if actor.role == "member":
        if target not in (None, "个人知识库"):
            raise SourceError("普通成员只能上传到自己的个人知识库。", 403)
        return None, "个人知识库"
    if target is None:
        raise SourceError("请先调用 list_upload_libraries，列出可上传的企业知识库，让用户选择目标库。", 422)
    choices = db.execute("""
        SELECT k.id, k.name FROM admin_upload_grant g JOIN knowledge_base k ON k.id=g.library
        WHERE g.admin_id=%s AND k.owner_member_id IS NULL AND (k.id=%s OR k.name=%s)
        ORDER BY (k.id=%s) DESC, k.id LIMIT 2
    """, (actor.id, target, target, target)).fetchall()
    if choices and (len(choices) == 1 or choices[0][0] == target):
        return choices[0]
    if len(choices) > 1:
        raise SourceError("存在同名的企业知识库，请指定知识库 ID。", 409)
    raise SourceError("请明确指定一个已授予你上传权限的企业知识库。", 403)


def publish_staged_import(db, token, actor, source_id, document, version, target, hybrid):
    with db.transaction():
        if import_identity(db, token, lock=True) != actor:
            raise SourceError("上传权限已变更，请重试。", 403)
        target_library(db, actor, target)
        row = db.execute("""
            SELECT d.current_version, d.published, s.last_version_id
            FROM managed_source s JOIN source_document d
              ON d.platform=s.document_platform AND d.document_id=s.document_id
            WHERE s.id=%s FOR UPDATE OF d
        """, (source_id,)).fetchone()
        if row is None:
            raise SourceError("这份文档已删除。", 404)
        current, published, latest = row
        if current == str(version) and published:
            return "published", current, None
        if latest != version:
            return "superseded", current, "有更新的导入版本，请查看目标知识库。"
        try:
            activate(db, document, version, current, hybrid, actor_id=actor.id)
        except ManagementError as error:
            return "publication_failed", current, str(error)
    return "published", str(version), None


def stage_feishu_import(db, token, url, target=None, heading=None, *, hybrid=False):
    actor = import_identity(db, token)
    library, name = target_library(db, actor, target)
    kind, external, url = parse_link(url)
    _, _, redirect = oauth_settings()
    origin = console_origin(redirect)
    access = user_token(db, actor)
    if not access:
        destination = console_path(actor) + ("?personal=1" if actor.role == "member" else "?authorize=feishu")
        return {"status": "authorization_required", "authorization_url": origin + destination,
                "message": "请登录知识库网页并点击「授权飞书」，使用与知识库账号相同的企业邮箱，然后重试。"}
    doc_id, title, markdown, line_blocks = fetch_feishu_document(url, heading, access)
    with db.transaction(), db.cursor(row_factory=dict_row) as cur:
        if import_identity(db, token, lock=True) != actor or user_token(db, actor) != access:
            raise SourceError("上传权限或飞书授权已变更，请重试。", 403)
        library, name = target_library(db, actor, target)
        if actor.role == "member":
            library = personal_library(db, actor.id, create=True)
        key = "docx:" + doc_id
        db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (library + ":" + key,))
        source = cur.execute("""
            SELECT * FROM managed_source WHERE library=%s AND provider='feishu' AND external_id=ANY(%s)
            ORDER BY user_imported DESC, id LIMIT 1 FOR UPDATE
        """, (library, [key, kind + ":" + external])).fetchone()
        if source is None:
            bound = bind_source(db, library, "feishu", key, url, title, heading, user_imported=True)
            source = cur.execute("SELECT * FROM managed_source WHERE id=%s", (bound["id"],)).fetchone()
        elif not source["user_imported"]:
            raise SourceError("该文档已在目标库通过「同步来源」绑定，请在那里更新。", 409)
        elif source["section_heading"] != heading:
            raise SourceError("该文档已在目标库导入，请沿用首次导入时选择的章节范围。", 409)
        if source["title"] != title:
            cur.execute("UPDATE managed_source SET title=%s WHERE id=%s", (title, source["id"]))
            source["title"] = title
            source["last_digest"] = None
        try:
            result = stage_source_snapshot(db, source, markdown, line_blocks)
            cur.execute("""
                UPDATE managed_source SET requested_by=%s, feishu_doc_token=%s, feishu_revision=NULL,
                    sync_pending_version=NULL, sync_next_at=statement_timestamp() + interval '15 minutes'
                WHERE id=%s
            """, (actor.id, doc_id, source["id"]))
            if result["changed"]:
                record_ingest(db, "feishu", result["version_id"], library, actor.id, title)
        except ManagementError as error:
            raise SourceError(str(error), 422) from None
    document, version = result["document_id"], result["version_id"]
    review = (f"/admin/query?document={document}&version={version}" if actor.role == "member"
              else f"/admin/kb/{library}/doc/{document}?v={version}")
    status, live, publication_error = publish_staged_import(
        db, token, actor, source["id"], document, version, target, hybrid,
    )
    if status == "publication_failed":
        db.execute("UPDATE managed_source SET last_error=%s WHERE id=%s AND last_version_id=%s",
                   (publication_error, source["id"], version))
    outcome = {**result, "status": status, "published_version": live or None, "title": title, "library": name,
               "review_url": origin + review, "source_url": url}
    if publication_error:
        outcome["message"] = publication_error
    return outcome
