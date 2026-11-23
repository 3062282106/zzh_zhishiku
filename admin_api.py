"""Authenticated HTTP endpoints for the knowledge console."""
from datetime import datetime
import os
from pathlib import Path
from secrets import compare_digest
from typing import Annotated
from urllib.parse import quote
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, StringConstraints

from access import VISIBLE, connect, token_hash
from admin import (ManagementError, activate, audit_feed, documents, libraries, member_directory, migrate, preview,
                   probe, set_availability, set_member_grants, set_owner, task_feed, upload, versions)
from feishu_source import bind_feishu, fetch_feishu_section
from docling_parser import FORMATS, MAX_FILE, ParseError
from file_ingest import jobs as file_jobs, queue as queue_file, queue_publication, retry as retry_file
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
    secret = os.environ.get("KB_ADMIN_TOKEN", "")
    if len(secret) < 32:
        raise HTTPException(503, "管理后台尚未配置。")
    if not credentials or not compare_digest(secret.encode(), credentials.credentials.encode()):
        raise HTTPException(401, "请输入正确的管理令牌。")


def reader_identity(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(auth)]):
    if not credentials:
        raise HTTPException(401, "需要知识库访问令牌。")
    return credentials.credentials


class LibraryRequest(BaseModel):
    name: Title


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
    control = APIRouter(prefix="/v1/admin", dependencies=[Depends(admin_identity)])

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
            db.execute("INSERT INTO knowledge_base VALUES (%s, %s)", (key, payload.name))
        return {"id": key, "name": payload.name}

    @control.get("/members")
    def members():
        with connect() as db:
            migrate(db)
            return member_directory(db)

    @control.post("/members/{member}/grants")
    def replace_grants(member: str, payload: MemberGrantRequest):
        try:
            with connect() as db:
                migrate(db)
                return set_member_grants(db, member, payload.libraries, payload.expires_at)
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
        if len(filename) <= 200 and suffix in FORMATS:
            data = file.file.read(MAX_FILE + 1)
            if len(data) > MAX_FILE:
                raise HTTPException(413, "上传文件超过 20 MiB。")
            try:
                with connect() as db:
                    return {"job": queue_file(db, library, document_id, title, filename, data)}
            except (ManagementError, ParseError) as error:
                raise HTTPException(422, str(error)) from None
        if len(filename) > 200 or Path(filename).suffix.lower() not in (".md", ".txt"):
            raise HTTPException(422, "请选择 PDF、DOCX、XLSX、PPTX、扫描图片、Markdown 或 TXT。")
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
        digest = token_hash(token)
        with connect() as db:
            if not db.execute("SELECT 1 FROM member_account WHERE token_hash=%s AND enabled", (digest,)).fetchone():
                raise HTTPException(401, "无效的知识库令牌。")
            row = db.execute("""
                SELECT v.original FROM managed_version v
                JOIN source_chunk c ON c.platform=v.platform AND c.document_id=v.document_id AND c.version=v.id::text
                WHERE v.platform='upload' AND v.document_id=%s AND v.id=%s
                  AND c.chunk_id IN (
            """ + VISIBLE + ") LIMIT 1", (str(document), version, digest)).fetchone()
            if not row:
                raise HTTPException(404, "找不到可访问的原文。")
        return PlainTextResponse(row[0], headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    @routes.get("/v1/knowledge/files/{version}")
    def original_file(version: UUID, token: Annotated[str, Depends(reader_identity)]):
        digest = token_hash(token)
        with connect() as db:
            if not db.execute("SELECT 1 FROM member_account WHERE token_hash=%s AND enabled", (digest,)).fetchone():
                raise HTTPException(401, "无效的知识库令牌。")
            row = db.execute("""
                SELECT f.content, f.filename FROM managed_file f
                WHERE f.id=%s AND f.status='ready' AND EXISTS (
                    SELECT 1 FROM source_chunk x
                    WHERE x.platform='upload' AND x.document_id=f.document_id AND x.version=f.id::text
                      AND x.chunk_id IN (
            """ + VISIBLE + "))", (version, digest)).fetchone()
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
