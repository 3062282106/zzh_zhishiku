"""Stage explicitly requested Feishu imports under the requesting user's permissions."""

from base64 import urlsafe_b64encode
from dataclasses import dataclass
from hashlib import sha256
import os
from secrets import token_urlsafe
from threading import Lock
from time import monotonic
from urllib.parse import urlencode, urlsplit
from uuid import uuid4

from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row

from access import ADMIN_MCP_PREFIX, is_admin_mcp_token, member_web_identity, token_hash
from admin import ManagementError, normalize_member_email, resolve_session
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
feishu_tokens = ExpiringCache()


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


def begin_oauth(db, token, state):
    actor = web_importer(db, token)
    if not upload_enabled(db, actor):
        raise SourceError("你的账号尚未开通上传权限。", 403)
    if not actor.email:
        raise SourceError("请先在知识库后台绑定你的企业邮箱。", 409)
    app, _, redirect = oauth_settings()
    verifier = token_urlsafe(48)
    challenge = urlsafe_b64encode(sha256(verifier.encode()).digest()).decode().rstrip("=")
    oauth_requests.put(state, OAuthRequest(actor, token, verifier, redirect), 300)
    query = urlencode({"client_id": app, "redirect_uri": redirect, "scope": SCOPES, "state": state,
                       "code_challenge": challenge, "code_challenge_method": "S256", "prompt": "consent"})
    return ACCOUNTS + "/open-apis/authen/v1/authorize?" + query


def complete_oauth(db, pending, code):
    if web_importer(db, pending.web_token) != pending.actor or not upload_enabled(db, pending.actor):
        raise SourceError("知识库登录或上传权限已失效，请重新授权。", 403)
    app, secret, redirect = oauth_settings()
    if redirect != pending.redirect:
        raise SourceError("飞书授权配置已变更，请重试。", 409)
    data = call("/oauth/v3/token", body={
        "grant_type": "authorization_code", "client_id": app, "client_secret": secret,
        "code": code, "redirect_uri": redirect, "code_verifier": pending.verifier,
    }, origin=ACCOUNTS)
    access_token, expires_in = data.get("access_token"), data.get("expires_in")
    if not isinstance(access_token, str) or not access_token or type(expires_in) is not int or expires_in <= 30:
        raise SourceError("飞书未返回有效的用户授权，请重新授权。", 409)
    info = call("/authen/v1/user_info", access_token).get("data", {})
    email = info.get("enterprise_email") or info.get("email")
    email = email.strip().lower() if isinstance(email, str) else ""
    if email != pending.actor.email:
        raise SourceError("飞书邮箱与知识库账号的企业邮箱不一致。", 403)
    if web_importer(db, pending.web_token) != pending.actor or not upload_enabled(db, pending.actor):
        raise SourceError("知识库登录或上传权限已失效，请重新授权。", 403)
    feishu_tokens.put(pending.actor.key, (email, access_token), min(7200, expires_in - 30))


def user_token(actor):
    authorized = feishu_tokens.get(actor.key)
    return authorized[1] if authorized and authorized[0] == actor.email else None


def personal_library(db, member_id, *, create=False):
    if create:
        db.execute("""
            INSERT INTO knowledge_base (id, name, owner_member_id) VALUES (%s, '个人知识库', %s)
            ON CONFLICT (owner_member_id) WHERE owner_member_id IS NOT NULL DO NOTHING
        """, ("personal_" + uuid4().hex, member_id))
    row = db.execute("SELECT id FROM knowledge_base WHERE owner_member_id=%s", (member_id,)).fetchone()
    return row[0] if row else None


def target_library(db, actor, target):
    if actor.role == "member":
        if target not in (None, "个人知识库"):
            raise SourceError("普通成员只能上传到自己的个人知识库。", 403)
        return None, "个人知识库"
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


def stage_feishu_import(db, token, url, target=None, heading=None):
    actor = import_identity(db, token)
    library, name = target_library(db, actor, target)
    kind, external, url = parse_link(url)
    _, _, redirect = oauth_settings()
    origin = console_origin(redirect)
    access = user_token(actor)
    if not access:
        destination = console_path(actor) + ("?personal=1" if actor.role == "member" else "?authorize=feishu")
        return {"status": "authorization_required", "authorization_url": origin + destination,
                "message": "请登录知识库网页并点击「授权飞书」，使用与知识库账号相同的企业邮箱，然后重试。"}
    doc_id, title, markdown, line_blocks = fetch_feishu_document(url, heading, access)
    with db.transaction(), db.cursor(row_factory=dict_row) as cur:
        if import_identity(db, token, lock=True) != actor or user_token(actor) != access:
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
        except ManagementError as error:
            raise SourceError(str(error), 422) from None
    document, version = result["document_id"], result["version_id"]
    review = (f"/admin/query?document={document}&version={version}" if actor.role == "member"
              else f"/admin/kb/{library}/doc/{document}?v={version}")
    live, = db.execute("SELECT current_version FROM source_document WHERE platform='upload' AND document_id=%s",
                      (document,)).fetchone()
    status = "draft_created" if result["changed"] else "unchanged"
    return {**result, "status": status, "published_version": live or None, "title": title, "library": name,
            "review_url": origin + review, "source_url": url}
