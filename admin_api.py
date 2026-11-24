"""Authenticated HTTP endpoints for the knowledge console."""
from datetime import datetime
import os
from pathlib import Path
from secrets import compare_digest
from time import monotonic
from typing import Annotated, Literal
from urllib.parse import quote
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, StringConstraints

from access import (connect, is_member_web_token, member_web_identity, reader_digest, reader_id,
                    reader_visibility, token_hash)
from admin import (ManagementError, WebLoginError, access_contact_url, activate, admin_directory, audit_feed,
                   close_session, create_admin, create_member, documents, issue_member_web_setup, libraries,
                   member_directory, migrate, open_member_web_session, open_session, preview,
                   probe, reset_member_token, resolve_session, retrieval_feed, set_admin_password, set_admin_state,
                   set_access_contact_url, set_availability, set_member_grants, set_member_state, set_owner, task_feed, upload,
                   versions)
from feishu_source import bind_feishu, fetch_feishu_section
from docling_parser import MAX_FILE, ParseError
from file_ingest import QUEUED_SUFFIXES, jobs as file_jobs, queue as queue_file, queue_publication, retry as retry_file
from notion_source import SourceError, bind_notion, list_sources, sync_source


auth = HTTPBearer(auto_error=False)
# Vue build output (web/dist). The console is a history-mode SPA, so every path
# below /admin/ that is not a built asset falls back to index.html.
DIST = Path(__file__).resolve().with_name("web") / "dist"
SHELL = DIST / "index.html"
PAGE_HEADERS = {
    "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                               "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
                               "base-uri 'none'; frame-ancestors 'none'",
}
ASSET_HEADERS = {"Cache-Control": "public, max-age=31536000, immutable", "X-Content-Type-Options": "nosniff"}
Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]


def admin_identity(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(auth)]):
    """Accept a logged-in console session, or the deployment token.

    The token stays valid because scripts/deploy-admin.sh and the cloud verification
    use it headlessly; people are expected to log in with their own account so the
    audit feed can tell them apart.
    """
    secret = os.environ.get("KB_ADMIN_TOKEN", "")
    if not credentials:
        raise HTTPException(401, "请先登录。")
    token = credentials.credentials
    if len(secret) >= 32 and compare_digest(secret.encode(), token.encode()):
        return {"id": "deploy", "username": "deploy", "name": "部署令牌", "role": "service"}
    with connect() as db:
        migrate(db)
        row = resolve_session(db, token)
    if not row:
        raise HTTPException(401, "登录已过期，请重新登录。")
    return admin_profile(row)


def admin_profile(row):
    return {"id": row["id"], "username": row["username"], "name": row["display_name"] or row["username"],
            "role": "admin"}


# Login throttling stays in-process: there is one uvicorn worker, and a shared table
# would mean a write on every failed attempt. nginx already caps the overall rate.
_attempts: dict[str, list[float]] = {}
ATTEMPT_WINDOW = 300
ATTEMPT_LIMIT = 8


def _throttled(key):
    now = monotonic()
    hits = [stamp for stamp in _attempts.get(key, []) if now - stamp < ATTEMPT_WINDOW]
    _attempts[key] = hits
    return len(hits) >= ATTEMPT_LIMIT


def _record_failure(key):
    _attempts.setdefault(key, []).append(monotonic())


def reader_identity(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(auth)]):
    if not credentials:
        raise HTTPException(401, "需要知识库访问令牌。")
    return credentials.credentials


def knowledge_file_digest(db, token):
    try:
        digest = reader_digest(db, token)
    except PermissionError:
        raise HTTPException(401, "登录已过期，请重新登录。") from None
    if not reader_id(db, token, digest):
        raise HTTPException(401, "无效的知识库令牌。")
    return digest


def enterprise_scope(request: Request):
    """The operator console manages shared content only."""
    params = request.path_params
    if "library" in params:
        query, key = "SELECT id FROM knowledge_base WHERE id=%s AND owner_member_id IS NOT NULL", params["library"]
    elif "document" in params:
        query, key = """
            SELECT 1 FROM source_document d JOIN knowledge_base k ON k.id=d.kb_id
            WHERE d.platform='upload' AND d.document_id=%s AND k.owner_member_id IS NOT NULL
        """, params["document"]
    elif "identifier" in params:
        table = "managed_source" if request.url.path.startswith("/v1/admin/sources/") else "managed_file"
        try:
            key = UUID(params["identifier"])
        except ValueError:
            return
        query = f"SELECT 1 FROM {table} d JOIN knowledge_base k ON k.id=d.library WHERE d.id=%s AND k.owner_member_id IS NOT NULL"
    else:
        return
    with connect() as db:
        if db.execute(query, (key,)).fetchone():
            raise HTTPException(404, "找不到企业知识库中的对应内容。")


class LibraryRequest(BaseModel):
    name: Title


class LoginRequest(BaseModel):
    username: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
    password: Annotated[str, StringConstraints(min_length=1, max_length=200)]


class ConsoleLoginRequest(LoginRequest):
    username: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    setup_code: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)] | None = None


class MemberWebSetup(BaseModel):
    email: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = None


class AccessContact(BaseModel):
    url: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2048)]


class AdminCreate(BaseModel):
    username: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=32)]
    password: Annotated[str, StringConstraints(min_length=10, max_length=200)]
    name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=60)] = ""


class AdminPassword(BaseModel):
    password: Annotated[str, StringConstraints(min_length=10, max_length=200)]


class ChangeVersion(BaseModel):
    expected: str
    valid_from: datetime | None = None
    valid_until: datetime | None = None


class AvailabilityRequest(BaseModel):
    expected: str
    expected_published: bool
    expected_valid_until: datetime | None = None
    published: bool
    valid_until: datetime | None = None
    valid_from: datetime | None = None


class GrantRequest(BaseModel):
    member: Title
    enabled: bool
    expires_at: datetime | None = None


class MemberGrantRequest(BaseModel):
    libraries: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]]
    expires_at: datetime | None = None
    personal_upload: bool | None = None


class MemberCreate(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
    email: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] = ""
    libraries: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]] = []
    expires_at: datetime | None = None
    kind: Annotated[str, StringConstraints(strip_whitespace=True, max_length=20)] = "employee"
    personal_upload: bool = False


class MemberState(BaseModel):
    enabled: bool


class OwnerRequest(BaseModel):
    owner: Annotated[str, StringConstraints(strip_whitespace=True, max_length=120)]


class ProbeRequest(BaseModel):
    question: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
    member: Title


class NotionSourceRequest(BaseModel):
    title: Title
    url: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2048)]


class FeishuSourceRequest(NotionSourceRequest):
    section_heading: Title


def router(hybrid):
    routes = APIRouter()
    control = APIRouter(prefix="/v1/admin", dependencies=[Depends(admin_identity), Depends(enterprise_scope)])

    @routes.get("/admin", include_in_schema=False)
    def console_redirect():
        # The SPA is mounted at /admin/; keep one canonical URL so deep links work.
        return RedirectResponse("/admin/", status_code=307)

    @routes.get("/admin/{asset:path}", include_in_schema=False)
    def console(asset: str):
        if not SHELL.is_file():
            raise HTTPException(503, "管理后台前端尚未构建，请先运行 npm --prefix web run build。")
        if asset:
            target = (DIST / asset).resolve()
            if target.is_file() and target.is_relative_to(DIST.resolve()):
                return FileResponse(target, headers=ASSET_HEADERS)
        return FileResponse(SHELL, media_type="text/html", headers=PAGE_HEADERS)

    @routes.post("/v1/admin/login")
    def login(payload: LoginRequest):
        key = payload.username.lower()
        if _throttled(key):
            raise HTTPException(429, "尝试次数太多，请 5 分钟后再试。")
        with connect() as db:
            migrate(db)
            try:
                return open_session(db, payload.username, payload.password)
            except ManagementError as error:
                _record_failure(key)
                raise HTTPException(401, str(error)) from None

    @routes.post("/v1/session/login")
    def console_login(payload: ConsoleLoginRequest, response: Response):
        key = payload.username.lower()
        if _throttled(key):
            raise HTTPException(429, "尝试次数太多，请 5 分钟后再试。")
        response.headers["Cache-Control"] = "no-store"
        with connect() as db:
            migrate(db)
            try:
                if "@" in payload.username:
                    if not payload.setup_code:
                        try:
                            return {**open_session(db, payload.username, payload.password), "role": "admin"}
                        except ManagementError:
                            pass
                    return open_member_web_session(db, payload.username, payload.password, payload.setup_code)
                return {**open_session(db, payload.username, payload.password), "role": "admin"}
            except WebLoginError as error:
                if error.status in (401, 403):
                    _record_failure(key)
                detail = {"code": error.code, "message": str(error)} if error.code else str(error)
                raise HTTPException(error.status, detail, headers={"Cache-Control": "no-store"}) from None
            except ManagementError as error:
                if "@" in payload.username:
                    raise HTTPException(422, str(error)) from None
                _record_failure(key)
                raise HTTPException(401, "账号或密码不正确。") from None

    @routes.get("/v1/session/access-contact")
    def sign_in_contact(response: Response):
        response.headers["Cache-Control"] = "no-store"
        with connect() as db:
            migrate(db)
            return {"url": access_contact_url(db)}

    @routes.get("/v1/session/me")
    def console_me(response: Response, token: Annotated[str, Depends(reader_identity)]):
        response.headers["Cache-Control"] = "no-store"
        with connect() as db:
            migrate(db)
            if member := member_web_identity(db, token):
                return {"id": member[0], "name": member[1], "role": "member"}
            if row := resolve_session(db, token):
                return admin_profile(row)
        raise HTTPException(401, "登录已过期，请重新登录。")

    @routes.post("/v1/session/logout")
    def console_logout(token: Annotated[str, Depends(reader_identity)]):
        with connect() as db:
            migrate(db)
            if is_member_web_token(token):
                db.execute("DELETE FROM member_web_session WHERE token_hash=%s", (token_hash(token),))
            else:
                close_session(db, token)
        return {"ok": True}

    @routes.post("/v1/admin/logout")
    def logout(identity: Annotated[dict, Depends(admin_identity)],
               credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(auth)]):
        if credentials:
            with connect() as db:
                migrate(db)
                close_session(db, credentials.credentials)
        return {"ok": True}

    @routes.get("/v1/admin/me")
    def me(identity: Annotated[dict, Depends(admin_identity)]):
        return identity

    @control.get("/administrators")
    def list_administrators():
        with connect() as db:
            migrate(db)
            return admin_directory(db)

    @control.post("/administrators", status_code=201)
    def add_administrator(payload: AdminCreate):
        with connect() as db:
            migrate(db)
            try:
                return create_admin(db, payload.username, payload.password, payload.name)
            except ManagementError as error:
                raise HTTPException(422, str(error)) from None

    @control.put("/administrators/{admin}/password")
    def rotate_admin_password(admin: str, payload: AdminPassword):
        with connect() as db:
            migrate(db)
            try:
                set_admin_password(db, admin, payload.password)
            except ManagementError as error:
                raise HTTPException(404, str(error)) from None
        return {"ok": True}

    @control.put("/administrators/{admin}/state")
    def switch_administrator(admin: str, payload: MemberState,
                             identity: Annotated[dict, Depends(admin_identity)]):
        # Locking yourself out of the only account is unrecoverable from the console.
        if not payload.enabled and identity.get("id") == admin:
            raise HTTPException(422, "不能停用当前登录的账号。")
        with connect() as db:
            migrate(db)
            try:
                set_admin_state(db, admin, payload.enabled)
            except ManagementError as error:
                raise HTTPException(404, str(error)) from None
        return {"ok": True}

    @control.get("/libraries")
    def list_libraries():
        with connect() as db:
            migrate(db)
            return [{"id": key, "name": name} for key, name in libraries(db)]

    @control.post("/libraries", status_code=201)
    def add_library(payload: LibraryRequest):
        with connect() as db:
            migrate(db)
            key = uuid4().hex
            db.execute("INSERT INTO knowledge_base (id, name) VALUES (%s, %s)", (key, payload.name))
        return {"id": key, "name": payload.name}

    @control.get("/members")
    def members():
        with connect() as db:
            migrate(db)
            return member_directory(db)

    @control.post("/members", status_code=201)
    def add_member(payload: MemberCreate, response: Response):
        if payload.kind not in ("employee", "guest"):
            raise HTTPException(422, "身份类型只能是 employee 或 guest。")
        try:
            response.headers["Cache-Control"] = "no-store"
            with connect() as db:
                migrate(db)
                return create_member(db, payload.name, payload.email, payload.libraries,
                                     payload.expires_at, payload.kind, enable_web=True, personal_upload=payload.personal_upload)
        except ManagementError as error:
            raise HTTPException(422, str(error)) from None

    @control.post("/members/{member}/token")
    def rotate_token(member: str):
        try:
            with connect() as db:
                migrate(db)
                return reset_member_token(db, member)
        except ManagementError as error:
            raise HTTPException(404, str(error)) from None

    @control.post("/members/{member}/web-setup")
    def web_setup(member: str, payload: MemberWebSetup, response: Response,
                  identity: Annotated[dict, Depends(admin_identity)]):
        if identity["role"] != "admin":
            raise HTTPException(403, "请使用管理员账号管理网页登录。")
        response.headers["Cache-Control"] = "no-store"
        try:
            with connect() as db:
                return issue_member_web_setup(db, member, payload.email)
        except ManagementError as error:
            raise HTTPException(422, str(error)) from None

    @control.put("/members/{member}/state")
    def switch_member(member: str, payload: MemberState):
        try:
            with connect() as db:
                migrate(db)
                return set_member_state(db, member, payload.enabled)
        except ManagementError as error:
            raise HTTPException(404, str(error)) from None

    @control.get("/settings")
    def settings():
        # The address colleagues put in their MCP client. It is deployment-specific
        # (tunnel, LAN entry, or a proxied hostname), so it lives in api.env rather
        # than being baked into the console bundle.
        with connect() as db:
            migrate(db)
            return {"mcp_url": os.environ.get("KB_MCP_PUBLIC_URL", "").strip(), "access_contact_url": access_contact_url(db)}

    @control.put("/settings/access-contact")
    def configure_contact(payload: AccessContact, identity: Annotated[dict, Depends(admin_identity)]):
        if identity["role"] != "admin":
            raise HTTPException(403, "请使用管理员账号修改申请入口。")
        try:
            with connect() as db:
                return {"url": set_access_contact_url(db, payload.url)}
        except ManagementError as error:
            raise HTTPException(422, str(error)) from None

    @control.post("/members/{member}/grants")
    def replace_grants(member: str, payload: MemberGrantRequest):
        try:
            with connect() as db:
                migrate(db)
                return set_member_grants(db, member, payload.libraries, payload.expires_at, payload.personal_upload)
        except ManagementError as error:
            raise HTTPException(422, str(error)) from None

    @control.get("/tasks")
    def tasks(library: str | None = None, days: int = 30):
        with connect() as db:
            migrate(db)
            return task_feed(db, library, max(1, min(days, 365)))

    @control.get("/audit")
    def audit(library: str | None = None, kind: str | None = None, limit: int = 200):
        with connect() as db:
            migrate(db)
            return audit_feed(db, library, kind, max(1, min(limit, 500)))

    @control.get("/retrieval-records")
    def retrieval_records(
        identity: Annotated[dict, Depends(admin_identity)], response: Response,
        days: Annotated[int, Query(ge=1, le=30)] = 7,
        member: Annotated[str | None, Query(min_length=1, max_length=160)] = None,
        library: Annotated[str | None, Query(min_length=1, max_length=160)] = None,
        status: Literal["evidence_found", "insufficient_evidence", "unavailable"] | None = None,
        before: Annotated[int | None, Query(ge=1, le=2**63 - 1)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 30,
    ):
        if identity["role"] != "admin":
            raise HTTPException(403, "请使用管理员账号登录查看检索记录。")
        response.headers["Cache-Control"] = "no-store"
        with connect() as db:
            return retrieval_feed(db, days=days, member=member, library=library, status=status,
                                  before=before, limit=limit)

    @control.put("/documents/{document}/owner")
    def owner(document: UUID, payload: OwnerRequest):
        try:
            with connect() as db:
                return set_owner(db, str(document), payload.owner or None)
        except ManagementError as error:
            raise HTTPException(404, str(error)) from None

    @control.post("/documents/{document}/versions/{version}/probe")
    def probe_version(document: UUID, version: UUID, payload: ProbeRequest):
        try:
            with connect() as db:
                return probe(db, str(document), str(version), payload.question, payload.member)
        except ManagementError as error:
            raise HTTPException(404, str(error)) from None

    @control.put("/libraries/{library}/grant")
    def set_grant(library: str, payload: GrantRequest):
        if payload.expires_at and payload.expires_at.utcoffset() is None:
            raise HTTPException(422, "到期时间需要包含时区。")
        with connect() as db, db.transaction():
            if not db.execute("SELECT 1 FROM knowledge_base WHERE id=%s", (library,)).fetchone():
                raise HTTPException(404, "找不到知识库。")
            if not db.execute("SELECT 1 FROM member_account WHERE id=%s AND enabled", (payload.member,)).fetchone():
                raise HTTPException(404, "找不到已启用的成员。")
            db.execute("""
                INSERT INTO knowledge_grant (member_id, kb_id, expires_at, revoked_at, updated_at)
                VALUES (%s,%s,%s,CASE WHEN %s THEN NULL ELSE now() END, now())
                ON CONFLICT (member_id,kb_id) DO UPDATE
                  SET revoked_at=excluded.revoked_at, expires_at=excluded.expires_at, updated_at=now()
            """, (payload.member, library, payload.expires_at, payload.enabled))
        return {"saved": True}

    @control.get("/libraries/{library}/documents")
    def list_documents(library: str):
        with connect() as db:
            return documents(db, library)

    @control.get("/libraries/{library}/sources")
    def sources(library: str):
        with connect() as db:
            return list_sources(db, library)

    @control.post("/libraries/{library}/sources/notion", status_code=201)
    def add_notion_source(library: str, payload: NotionSourceRequest):
        try:
            with connect() as db:
                return bind_notion(db, library, payload.url, payload.title)
        except SourceError as error:
            raise HTTPException(error.status, str(error)) from None

    @control.post("/libraries/{library}/sources/feishu", status_code=201)
    def add_feishu_source(library: str, payload: FeishuSourceRequest):
        try:
            with connect() as db:
                return bind_feishu(db, library, payload.url, payload.title, payload.section_heading)
        except SourceError as error:
            raise HTTPException(error.status, str(error)) from None

    @control.post("/sources/{identifier}/sync")
    def sync_online_source(identifier: UUID):
        try:
            with connect() as db:
                return sync_source(db, identifier, fetch_feishu_section)
        except SourceError as error:
            raise HTTPException(error.status, str(error)) from None

    @control.post("/libraries/{library}/documents", status_code=201)
    def add_version(library: str, title: Annotated[Title, Form()], file: Annotated[UploadFile, File()],
                    document_id: Annotated[UUID | None, Form()] = None):
        filename = (file.filename or "").replace("\\", "/").rsplit("/", 1)[-1]
        suffix = Path(filename).suffix.lower()
        if len(filename) <= 200 and suffix in QUEUED_SUFFIXES:
            data = file.file.read(MAX_FILE + 1)
            if len(data) > MAX_FILE:
                raise HTTPException(413, "上传文件超过 20 MiB。")
            try:
                with connect() as db:
                    return {"job": queue_file(db, library, document_id, title, filename, data)}
            except (ManagementError, ParseError) as error:
                raise HTTPException(422, str(error)) from None
        if len(filename) > 200 or suffix not in (".md", ".txt"):
            raise HTTPException(422, "请选择 PDF、DOCX、XLSX、CSV、PPTX、扫描图片、Markdown 或 TXT。")
        limit = 256 * 1024
        try:
            data = file.file.read(limit + 1)
            if len(data) > limit:
                raise HTTPException(413, "文件超过 256 KiB。")
            original = data.decode("utf-8-sig")
            if "\x00" in original:
                raise ValueError("Invalid text")
            with connect() as db:
                return upload(db, library, str(document_id) if document_id else None, title, filename, original)
        except UnicodeDecodeError:
            raise HTTPException(422, "请将文件另存为 UTF-8。") from None
        except ValueError as error:
            raise HTTPException(422, str(error)) from None

    @control.get("/libraries/{library}/file-jobs")
    def list_file_jobs(library: str):
        with connect() as db:
            return file_jobs(db, library)

    @control.post("/file-jobs/{identifier}/retry")
    def retry_file_job(identifier: UUID):
        try:
            with connect() as db:
                return retry_file(db, identifier)
        except ManagementError as error:
            raise HTTPException(409, str(error)) from None

    @control.get("/file-jobs/{identifier}/original")
    def admin_file(identifier: UUID):
        with connect() as db:
            row = db.execute("SELECT content, filename FROM managed_file WHERE id=%s", (identifier,)).fetchone()
        if not row:
            raise HTTPException(404, "找不到原文件。")
        return file_response(row)

    @control.get("/documents/{document}/versions")
    def list_versions(document: UUID):
        with connect() as db:
            return versions(db, str(document), hybrid)

    @control.put("/documents/{document}/availability")
    def visibility(document: UUID, payload: AvailabilityRequest):
        times = (payload.expected_valid_until, payload.valid_until, payload.valid_from)
        if any(value is not None and value.utcoffset() is None for value in times):
            raise HTTPException(422, "生效和到期时间需要包含时区。")
        try:
            with connect() as db:
                return set_availability(db, str(document), **payload.model_dump())
        except ManagementError as error:
            raise HTTPException(409, str(error)) from None

    @control.get("/documents/{document}/versions/{version}")
    def inspect(document: UUID, version: UUID):
        try:
            with connect() as db:
                return preview(db, str(document), version)
        except ManagementError as error:
            raise HTTPException(404, str(error)) from None

    @control.post("/documents/{document}/versions/{version}/{action}")
    def switch(document: UUID, version: UUID, action: str, payload: ChangeVersion, response: Response):
        if action not in ("publish", "rollback"):
            raise HTTPException(404)
        times = (payload.valid_from, payload.valid_until)
        if any(value is not None and value.utcoffset() is None for value in times):
            raise HTTPException(422, "生效和到期时间需要包含时区。")
        if payload.valid_from and payload.valid_until and payload.valid_until <= payload.valid_from:
            raise HTTPException(422, "到期时间必须晚于生效时间。")
        schedule = (payload.valid_from, payload.valid_until) if any(times) else None
        try:
            with connect() as db:
                if action == "publish" and (job := queue_publication(
                        db, str(document), version, payload.expected, *(schedule or (None, None)))):
                    response.status_code = 202
                    return job
                return activate(db, str(document), version, payload.expected, hybrid,
                                rollback=action == "rollback", schedule=schedule)
        except ManagementError as error:
            raise HTTPException(409, str(error)) from None

    @routes.get("/v1/knowledge/originals/{document}/{version}")
    def original(document: UUID, version: UUID, token: Annotated[str, Depends(reader_identity)]):
        with connect() as db:
            digest = knowledge_file_digest(db, token)
            row = db.execute("""
                SELECT v.original FROM managed_version v
                JOIN source_chunk c ON c.platform=v.platform AND c.document_id=v.document_id AND c.version=v.id::text
                WHERE v.platform='upload' AND v.document_id=%s AND v.id=%s
                  AND c.chunk_id IN (
            """ + reader_visibility(token) + ") LIMIT 1", (str(document), version, digest)).fetchone()
            if not row:
                raise HTTPException(404, "找不到可访问的原文。")
        return PlainTextResponse(row[0], headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    @routes.get("/v1/knowledge/files/{version}")
    def original_file(version: UUID, token: Annotated[str, Depends(reader_identity)]):
        with connect() as db:
            digest = knowledge_file_digest(db, token)
            row = db.execute("""
                SELECT f.content, f.filename FROM managed_file f
                WHERE f.id=%s AND f.status='ready' AND EXISTS (
                    SELECT 1 FROM source_chunk x
                    WHERE x.platform='upload' AND x.document_id=f.document_id AND x.version=f.id::text
                      AND x.chunk_id IN (
            """ + reader_visibility(token) + "))", (version, digest)).fetchone()
        if not row:
            raise HTTPException(404, "找不到可访问的原文件。")
        return file_response(row)

    routes.include_router(control)
    return routes


def file_response(row):
    content, filename = row
    return Response(content, media_type="application/octet-stream", headers={
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
        "Content-Disposition": "attachment; filename*=UTF-8''" + quote(filename, safe=""),
    })
