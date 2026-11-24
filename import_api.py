"""MCP import and owner-only draft management endpoints."""

from base64 import b64decode
from binascii import Error as Base64Error
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
import logging
from pathlib import Path
from secrets import compare_digest, token_urlsafe
from threading import Lock
from time import monotonic
from typing import Annotated
from urllib.parse import urlencode
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, StringConstraints

from access import connect, token_hash
from admin import ManagementError, activate, delete_document, documents, preview, set_availability, versions
from admin_api import SourceSyncRequest, Title, admin_identity, reader_identity
from agent_import import (CALLBACK, OAuthFailure, admin_import_access, authorization_status, begin_oauth, complete_oauth,
                          console_path, import_identity, issue_admin_import_token, oauth_requests,
                          personal_library, set_admin_import_access, stage_feishu_import, target_library,
                          upload_enabled, uploadable_libraries, web_importer)
from docling_parser import MAX_FILE, ParseError
from feishu_source import parse_link
from feishu_sync import request_check
from file_ingest import SUPPORTED_SUFFIXES, file_status, jobs as file_jobs, queue as queue_file
from notion_source import SourceError


Session = Annotated[str, Depends(reader_identity)]
Admin = Annotated[dict, Depends(admin_identity)]
FLOW_COOKIE = "kb_feishu_flow"
LINK = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2048)]


class FeishuImport(BaseModel):
    url: LINK
    library: Title | None = None
    section_heading: Title | None = None


@dataclass
class ImportJob:
    key: tuple
    created_at: float
    future: Future


import_workers = ThreadPoolExecutor(max_workers=1, thread_name_prefix="feishu-import")
import_jobs: dict[UUID, ImportJob] = {}
import_jobs_lock = Lock()


def run_import(token, url, library, heading, hybrid):
    with connect() as db:
        return stage_feishu_import(db, token, url, library, heading, hybrid=hybrid)


def submit_import(token, url, library, heading, hybrid):
    key = (token_hash(token), url, library, heading)
    now = monotonic()
    with import_jobs_lock:
        for job_id, job in list(import_jobs.items()):
            if job.future.done() and (now - job.created_at > 3600 or len(import_jobs) >= 128):
                del import_jobs[job_id]
        active = [item for item in import_jobs.items() if not item[1].future.done()]
        for job_id, job in active:
            if job.key == key:
                return job_id, job
        if len(active) >= 6:
            raise SourceError("飞书导入队列已满，请稍后再试。", 429)
        job_id = uuid4()
        job = ImportJob(key, now, import_workers.submit(run_import, token, url, library, heading, hybrid))
        import_jobs[job_id] = job
        return job_id, job


def import_outcome(job_id, job):
    try:
        return job.future.result(timeout=6)
    except TimeoutError:
        if job.future.done():
            return job.future.result()
        return {"status": "processing", "job_id": str(job_id), "message": "正在后台导入，可查询任务进度。"}


MAX_BASE64 = 4 * ((MAX_FILE + 2) // 3)


class FileImport(BaseModel):
    filename: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    content_base64: Annotated[str, StringConstraints(min_length=1, max_length=MAX_BASE64)]
    title: Title | None = None
    library: Title | None = None


class PersonalVisibility(BaseModel):
    expected_version: UUID
    published: bool


class AdminImportAccess(BaseModel):
    email: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)]
    libraries: list[Title]


class PublishDraft(BaseModel):
    expected: str


def translate(error, status=422):
    return HTTPException(getattr(error, "status", status), str(error), headers={"Cache-Control": "no-store"})


def named_admin(identity):
    if identity["role"] != "admin":
        raise HTTPException(403, "请使用管理员账号登录。")


def member_actor(db, token):
    actor = web_importer(db, token)
    if actor.role != "member":
        raise SourceError("请使用成员账号。", 403)
    return actor


def personal_writer(db, token):
    actor = member_actor(db, token)
    permitted = db.execute("""
        SELECT 1 FROM member_account WHERE id=%s AND enabled AND personal_upload FOR UPDATE
    """, (actor.id,)).fetchone()
    if not permitted or member_actor(db, token) != actor:
        raise SourceError("你的账号尚未开通上传权限。", 403)
    return actor


def queue_new_file(db, library, filename, content, actor, title=None, *, via_mcp=False, upload_id=None):
    filename = filename.replace("\\", "/").rsplit("/", 1)[-1]
    if len(filename) > 200 or Path(filename).suffix.lower() not in SUPPORTED_SUFFIXES:
        raise SourceError("请选择 PDF、DOCX、XLSX、CSV、PPTX、扫描图片、Markdown 或 TXT。", 422)
    try:
        return queue_file(db, library, None, title or Path(filename).stem[:160], filename, content,
                          requested_by=actor.id, auto_publish=True, requires_mcp_grant=via_mcp and actor.role == "admin",
                          upload_id=upload_id)
    except ParseError as error:
        raise SourceError(str(error), 422) from None


def owned_document(db, actor, document):
    current = db.execute("""
        SELECT d.current_version FROM source_document d JOIN knowledge_base k ON k.id=d.kb_id
        WHERE d.platform='upload' AND d.document_id=%s AND k.owner_member_id=%s
    """, (str(document), actor.id)).fetchone()
    if current is None:
        raise SourceError("找不到个人知识库中的此文档。", 404)
    return current[0]


def router(hybrid):
    routes = APIRouter()

    @routes.post("/v1/knowledge/imports/feishu")
    def import_feishu(payload: FeishuImport, token: Session, response: Response):
        response.headers["Cache-Control"] = "no-store"
        try:
            with connect() as db:
                target_library(db, import_identity(db, token), payload.library)
            _, _, url = parse_link(payload.url)
            job_id, job = submit_import(token, url, payload.library, payload.section_heading, hybrid)
            return import_outcome(job_id, job)
        except SourceError as error:
            raise translate(error) from None

    @routes.get("/v1/knowledge/imports/feishu/{job_id}")
    def feishu_import_status(job_id: UUID, token: Session, response: Response):
        response.headers["Cache-Control"] = "no-store"
        with import_jobs_lock:
            job = import_jobs.get(job_id)
        if job is None or not compare_digest(job.key[0], token_hash(token)):
            raise HTTPException(404, "找不到此导入任务。请重新提交飞书链接。")
        try:
            with connect() as db:
                target_library(db, import_identity(db, token), job.key[2])
            return import_outcome(job_id, job)
        except SourceError as error:
            raise translate(error) from None

    @routes.get("/v1/knowledge/imports/libraries")
    def list_import_libraries(token: Session, response: Response):
        response.headers["Cache-Control"] = "no-store"
        try:
            with connect() as db:
                actor = import_identity(db, token)
                return {"role": actor.role, "libraries": uploadable_libraries(db, actor)}
        except SourceError as error:
            raise translate(error) from None

    @routes.post("/v1/knowledge/imports/files", status_code=202)
    def import_mcp_file(payload: FileImport, token: Session, response: Response):
        response.headers["Cache-Control"] = "no-store"
        try:
            with connect() as db, db.transaction():
                actor = import_identity(db, token, lock=True)
                library, _ = target_library(db, actor, payload.library)
                try:
                    data = b64decode(payload.content_base64, validate=True)
                except (Base64Error, ValueError):
                    raise SourceError("附件内容不是有效的 Base64。", 422) from None
                if len(data) > MAX_FILE:
                    raise SourceError("上传文件超过 20 MiB。", 413)
                if actor.role == "member":
                    library = personal_library(db, actor.id, create=True)
                return queue_new_file(db, library, payload.filename, data, actor, payload.title, via_mcp=True)
        except ManagementError as error:
            raise translate(error) from None

    @routes.get("/v1/knowledge/imports/files/{job_id}")
    def mcp_file_status(job_id: UUID, token: Session, response: Response):
        response.headers["Cache-Control"] = "no-store"
        try:
            with connect() as db:
                actor = import_identity(db, token)
                if actor.role == "member":
                    library = personal_library(db, actor.id)
                else:
                    allowed = db.execute("""
                        SELECT f.library FROM managed_file f JOIN admin_upload_grant g ON g.library=f.library
                        JOIN knowledge_base k ON k.id=f.library AND k.owner_member_id IS NULL
                        WHERE f.id=%s AND g.admin_id=%s
                    """, (job_id, actor.id)).fetchone()
                    library = allowed[0] if allowed else None
                if not library or (job := file_status(db, job_id, library)) is None:
                    raise SourceError("找不到可访问的附件任务。", 404)
                return job
        except SourceError as error:
            raise translate(error) from None

    @routes.get("/v1/knowledge/feishu/status")
    def feishu_status(token: Session, response: Response):
        response.headers["Cache-Control"] = "no-store"
        try:
            with connect() as db:
                actor = web_importer(db, token)
                return {**authorization_status(db, actor), "upload_enabled": upload_enabled(db, actor),
                        "email": actor.email}
        except SourceError as error:
            raise translate(error) from None

    @routes.post("/v1/knowledge/feishu/authorize")
    def authorize_feishu(token: Session, response: Response, offline: bool = False):
        try:
            state = token_urlsafe(32)
            with connect() as db:
                url = begin_oauth(db, token, state, offline=offline)
        except SourceError as error:
            raise translate(error) from None
        response.headers["Cache-Control"] = "no-store"
        response.set_cookie(FLOW_COOKIE, state, secure=True, httponly=True, samesite="lax",
                            path=CALLBACK, max_age=300)
        return {"url": url}

    @routes.get(CALLBACK, include_in_schema=False)
    def feishu_callback(request: Request, code: str = "", state: str = ""):
        nonce = request.cookies.get(FLOW_COOKIE, "")
        if not (nonce and len(state) == 43 and state.isascii() and compare_digest(nonce.encode(), state.encode())):
            raise HTTPException(400, "飞书授权状态无效，请从知识库网页重新授权。",
                                headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
        pending = oauth_requests.get(state, consume=True)
        if not pending:
            raise HTTPException(400, "飞书授权请求已过期，请从知识库网页重试。",
                                headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
        result = "cancelled"
        query = {}
        if code and len(code) <= 2048:
            try:
                with connect() as db:
                    complete_oauth(db, pending, code)
                result = "ok"
            except OAuthFailure as error:
                result = error.reason
                if error.provider_code is not None:
                    query["feishu_code"] = error.provider_code
            except SourceError as error:
                result = "session" if error.status == 401 else "failed"
                logging.getLogger(__name__).warning("Feishu OAuth callback failed (status=%s): %s", error.status, error)
        query["feishu"] = result
        response = RedirectResponse(console_path(pending.actor) + "?" + urlencode(query), status_code=303,
                                    headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
        response.delete_cookie(FLOW_COOKIE, path=CALLBACK, secure=True, httponly=True, samesite="lax")
        return response

    @routes.get("/v1/knowledge/personal")
    def personal(token: Session, response: Response):
        response.headers["Cache-Control"] = "no-store"
        try:
            with connect() as db:
                actor = member_actor(db, token)
                library = personal_library(db, actor.id)
                return {"upload_enabled": upload_enabled(db, actor),
                        "documents": documents(db, library) if library else [],
                        "jobs": file_jobs(db, library) if library else []}
        except SourceError as error:
            raise translate(error) from None

    @routes.post("/v1/knowledge/personal/files", status_code=202)
    def upload_personal(token: Session, response: Response, file: Annotated[UploadFile, File()],
                        title: Annotated[Title | None, Form()] = None,
                        upload_id: Annotated[UUID | None, Form()] = None):
        response.headers["Cache-Control"] = "no-store"
        try:
            data = file.file.read(MAX_FILE + 1)
            if len(data) > MAX_FILE:
                raise SourceError("上传文件超过 20 MiB。", 413)
            with connect() as db, db.transaction():
                actor = personal_writer(db, token)
                library = personal_library(db, actor.id, create=True)
                return queue_new_file(db, library, file.filename or "", data, actor, title, upload_id=upload_id)
        except ManagementError as error:
            raise translate(error) from None

    def queue_personal_sync(db, token, document, enabled=None):
        actor = personal_writer(db, token)
        owned_document(db, actor, document)
        source = db.execute("""
            SELECT id FROM managed_source WHERE document_platform='upload' AND document_id=%s AND provider='feishu'
        """, (str(document),)).fetchone()
        if not source:
            raise SourceError("此文档没有飞书同步来源。", 404)
        return request_check(db, source[0], enabled=enabled)

    @routes.post("/v1/knowledge/personal/{document}/sync", status_code=202)
    def check_personal_source(document: UUID, token: Session):
        try:
            with connect() as db:
                return queue_personal_sync(db, token, document)
        except SourceError as error:
            raise translate(error) from None

    @routes.put("/v1/knowledge/personal/{document}/auto-sync")
    def configure_personal_sync(document: UUID, payload: SourceSyncRequest, token: Session):
        try:
            with connect() as db:
                return queue_personal_sync(db, token, document, payload.enabled)
        except SourceError as error:
            raise translate(error) from None

    @routes.delete("/v1/knowledge/personal/{document}")
    def delete_personal(document: UUID, token: Session):
        try:
            with connect() as db:
                actor = member_actor(db, token)
                owned_document(db, actor, document)
                if not delete_document(db, str(document), actor_id=actor.id):
                    raise SourceError("找不到个人知识库中的此文档。", 404)
        except ManagementError as error:
            raise translate(error, 409) from None
        return {"deleted": True}

    @routes.put("/v1/knowledge/personal/{document}/availability")
    def personal_visibility(document: UUID, payload: PersonalVisibility, token: Session):
        try:
            with connect() as db, db.transaction():
                actor = member_actor(db, token)
                owned_document(db, actor, document)
                state = db.execute("""
                    SELECT published, valid_until, valid_from FROM source_document
                    WHERE platform='upload' AND document_id=%s FOR UPDATE
                """, (str(document),)).fetchone()
                if not state:
                    raise SourceError("找不到个人知识库中的此文档。", 404)
                was_published, until, start = state
                return set_availability(db, str(document), str(payload.expected_version), was_published, until,
                                        payload.published, until, start, actor_id=actor.id)
        except ManagementError as error:
            raise translate(error, 409) from None

    @routes.get("/v1/knowledge/personal/{document}")
    def personal_document(document: UUID, token: Session, response: Response, version: UUID | None = None):
        response.headers["Cache-Control"] = "no-store"
        try:
            with connect() as db:
                actor = member_actor(db, token)
                owned_document(db, actor, document)
                entries = versions(db, str(document), hybrid)
                if not entries:
                    raise SourceError("找不到文档版本。", 404)
                if version is None:
                    version = entries[0]["id"]
                if not any(row["id"] == version for row in entries):
                    raise SourceError("找不到此文档版本。", 404)
                detail = preview(db, str(document), version)
                return {"version": version, "versions": entries, "title": detail["title"],
                        "original": detail["original"]}
        except ManagementError as error:
            raise translate(error, 404) from None

    @routes.post("/v1/knowledge/personal/{document}/versions/{version}/publish")
    def publish_personal(document: UUID, version: UUID, payload: PublishDraft, token: Session, response: Response):
        response.headers["Cache-Control"] = "no-store"
        try:
            with connect() as db, db.transaction():
                actor = personal_writer(db, token)
                owned_document(db, actor, document)
                return activate(db, str(document), version, payload.expected, hybrid, actor_id=actor.id)
        except ManagementError as error:
            raise translate(error, 409) from None

    @routes.get("/v1/admin/administrators/{identifier}/import-access")
    def get_admin_import(identifier: str, identity: Admin, response: Response):
        named_admin(identity)
        response.headers["Cache-Control"] = "no-store"
        try:
            with connect() as db:
                return admin_import_access(db, identifier)
        except SourceError as error:
            raise translate(error) from None

    @routes.put("/v1/admin/administrators/{identifier}/import-access")
    def set_admin_import(identifier: str, payload: AdminImportAccess, identity: Admin):
        named_admin(identity)
        try:
            with connect() as db:
                return set_admin_import_access(db, identifier, payload.email, payload.libraries)
        except ManagementError as error:
            raise translate(error) from None

    @routes.post("/v1/admin/import-token")
    def admin_mcp_token(identity: Admin, response: Response):
        named_admin(identity)
        response.headers["Cache-Control"] = "no-store"
        try:
            with connect() as db:
                return {"token": issue_admin_import_token(db, identity["id"])}
        except SourceError as error:
            raise translate(error) from None

    return routes
