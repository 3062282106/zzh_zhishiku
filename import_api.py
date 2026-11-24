"""MCP import and owner-only draft management endpoints."""

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
import logging
from secrets import compare_digest, token_urlsafe
from threading import Lock
from time import monotonic
from typing import Annotated
from urllib.parse import urlencode
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, StringConstraints

from access import connect, token_hash
from admin import ManagementError, activate, documents, preview, versions
from admin_api import Title, admin_identity, reader_identity
from agent_import import (CALLBACK, OAuthFailure, admin_import_access, begin_oauth, complete_oauth,
                          console_path, import_identity, issue_admin_import_token, oauth_requests,
                          personal_library, set_admin_import_access, stage_feishu_import, target_library,
                          upload_enabled, user_token, web_importer)
from feishu_source import parse_link
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

    @routes.get("/v1/knowledge/feishu/status")
    def feishu_status(token: Session, response: Response):
        response.headers["Cache-Control"] = "no-store"
        try:
            with connect() as db:
                actor = web_importer(db, token)
                return {"authorized": bool(user_token(actor)), "upload_enabled": upload_enabled(db, actor),
                        "email": actor.email}
        except SourceError as error:
            raise translate(error) from None

    @routes.post("/v1/knowledge/feishu/authorize")
    def authorize_feishu(token: Session, response: Response):
        try:
            state = token_urlsafe(32)
            with connect() as db:
                url = begin_oauth(db, token, state)
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
                        "documents": documents(db, library) if library else []}
        except SourceError as error:
            raise translate(error) from None

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
                actor = member_actor(db, token)
                permitted = db.execute("""
                    SELECT 1 FROM member_account WHERE id=%s AND enabled AND personal_upload FOR UPDATE
                """, (actor.id,)).fetchone()
                if not permitted or member_actor(db, token) != actor:
                    raise SourceError("你的账号尚未开通上传权限。", 403)
                owned_document(db, actor, document)
                return activate(db, str(document), version, payload.expected, hybrid)
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
