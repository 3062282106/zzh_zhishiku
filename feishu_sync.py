"""Check bound Feishu cloud documents and replace their published snapshot safely."""

from functools import cache
import logging
import os

from psycopg.rows import dict_row

from access import connect
from admin import ManagementError, activate, migrate
from agent_import import Importer, authorization_status, user_token
from feishu_source import credential, document_revision, list_blocks, render_section
from notion_source import SourceError, stage_source_snapshot


log = logging.getLogger(__name__)
CHECK_INTERVAL = "15 minutes"
RETRY_INTERVAL = "5 minutes"

SOURCE = """
    SELECT s.*, d.current_version, d.published, d.valid_until, k.owner_member_id
    FROM managed_source s
    JOIN source_document d ON d.platform=s.document_platform AND d.document_id=s.document_id
    JOIN knowledge_base k ON k.id=s.library
    WHERE s.id=%s AND s.provider='feishu'
"""


def request_check(db, source_id, *, enabled=None):
    row = db.execute("""
        UPDATE managed_source SET auto_sync=COALESCE(%s, auto_sync),
            sync_requested=COALESCE(%s, true),
            sync_next_at=CASE WHEN %s IS FALSE THEN NULL ELSE statement_timestamp() END
        WHERE id=%s AND provider='feishu' RETURNING id
    """, (enabled, enabled, enabled, source_id)).fetchone()
    if row is None:
        raise SourceError("找不到可同步的飞书来源。", 404)
    return {"status": "paused" if enabled is False else "queued"}


def source_row(db, source_id, *, lock=False):
    with db.cursor(row_factory=dict_row) as cur:
        return cur.execute(SOURCE + (" FOR UPDATE OF s" if lock else ""), (source_id,)).fetchone()


def eligible(source):
    if not source:
        return False
    if source["sync_requested"]:
        return True
    enabled = source["auto_sync"] if source["auto_sync"] is not None else bool(source["current_version"])
    return enabled and (not source["current_version"] or source["published"])


def source_actor(db, source, *, lock=False):
    if not source["user_imported"]:
        return None
    owner = source["owner_member_id"]
    if owner:
        role = "member"
        row = db.execute("""
            SELECT id, email FROM member_account
            WHERE id=%s AND enabled AND personal_upload
        """ + (" FOR UPDATE" if lock else ""), (owner,)).fetchone()
    else:
        role = "admin"
        row = db.execute("""
            SELECT a.id, COALESCE(a.email, CASE WHEN position('@' in a.username)>0 THEN a.username END)
            FROM admin_account a
            WHERE a.id=%s AND a.enabled AND EXISTS (
                SELECT 1 FROM admin_upload_grant g WHERE g.admin_id=a.id AND g.library=%s
            )
        """ + (" FOR UPDATE OF a" if lock else ""), (source["requested_by"], source["library"])).fetchone()
    if not row or not row[1]:
        raise SourceError("导入人的知识库权限已变更，请由有权账号重新导入。", 403)
    return Importer(role, row[0], row[1].strip().lower())


def ensure_actor(db, source, expected):
    if source_actor(db, source, lock=True) != expected:
        raise SourceError("来源身份已变更，请重新检查。", 409)
    if expected and not authorization_status(db, expected)["authorized"]:
        raise SourceError("飞书授权已失效，请重新授权自动同步。", 409)


def same_source(current, previous):
    return eligible(current) and all(current[key] == previous[key] for key in (
        "last_version_id", "checked_at", "requested_by", "source_url", "section_heading", "auto_sync",
    ))


def sync_source(db, source_id, hybrid, application_token=credential):
    """Keep the current version until the newly staged snapshot has valid indexes."""
    checkpoint = source_row(db, source_id)
    if not eligible(checkpoint):
        return
    try:
        actor = source_actor(db, checkpoint)
        access = user_token(db, actor) if actor else application_token()
        if not access:
            raise SourceError("请在知识库网页重新授权飞书自动同步。", 409)
        token, revision, title = document_revision(checkpoint["source_url"], access)
        expected_token = checkpoint["feishu_doc_token"]
        if not expected_token and checkpoint["user_imported"] and checkpoint["external_id"].startswith("docx:"):
            expected_token = checkpoint["external_id"].split(":", 1)[1]
        if expected_token and expected_token != token:
            raise SourceError("此链接指向的飞书文档已变化，请重新绑定来源。", 409)
        changed_revision = (token, revision) != (checkpoint["feishu_doc_token"], checkpoint["feishu_revision"])
        changed_title = checkpoint["user_imported"] and title != checkpoint["title"]
        snapshot = None
        if changed_revision or changed_title:
            blocks = list_blocks(token, revision, access)
            snapshot = render_section(blocks, token, checkpoint["section_heading"])

        with db.transaction():
            ensure_actor(db, checkpoint, actor)
            source = source_row(db, source_id, lock=True)
            if not same_source(source, checkpoint):
                return
            pending = source["sync_pending_version"]
            if snapshot is not None:
                if source["user_imported"] and source["title"] != title:
                    source.update(title=title, last_digest=None)
                result = stage_source_snapshot(db, source, *snapshot)
                if result["changed"]:
                    pending = result["version_id"]
                elif source["sync_requested"] and not source["current_version"]:
                    pending = result["version_id"]
            progress = db.execute("""
                UPDATE managed_source SET title=%s, feishu_doc_token=%s, feishu_revision=%s,
                    sync_pending_version=%s, checked_at=statement_timestamp(), last_error=NULL
                WHERE id=%s RETURNING checked_at,last_version_id
            """, (source["title"], token, revision, pending, source_id)).fetchone()
        checkpoint.update(checked_at=progress[0], last_version_id=progress[1])

        with db.transaction():
            ensure_actor(db, checkpoint, actor)
            source = source_row(db, source_id, lock=True)
            if not same_source(source, checkpoint):
                return
            active = db.execute("""
                SELECT current_version, published, valid_until FROM source_document
                WHERE platform=%s AND document_id=%s FOR UPDATE
            """, (source["document_platform"], source["document_id"])).fetchone()
            current, published, until = active
            if pending == source["last_version_id"] and pending is not None:
                if current != str(pending) and (published or not current):
                    activate(db, source["document_id"], pending, current, hybrid,
                             availability=(published, until), actor_id="飞书同步")
                if published or not current:
                    pending = None
            if source["sync_pending_version"] != source["last_version_id"]:
                pending = None
            db.execute("""
                UPDATE managed_source SET sync_requested=false, sync_pending_version=%s,
                    sync_next_at=statement_timestamp() + %s::interval, checked_at=statement_timestamp()
                WHERE id=%s
            """, (pending, CHECK_INTERVAL, source_id))
    except ManagementError as error:
        db.execute("""
            UPDATE managed_source SET sync_requested=false,
                sync_next_at=statement_timestamp() + %s::interval, last_error=%s, checked_at=statement_timestamp()
            WHERE id=%s AND checked_at IS NOT DISTINCT FROM %s AND auto_sync IS NOT DISTINCT FROM %s
        """, (RETRY_INTERVAL, str(error), source_id, checkpoint["checked_at"], checkpoint["auto_sync"]))


def check_due(db, hybrid):
    if not db.execute("SELECT pg_try_advisory_lock(5620191339462067151)").fetchone()[0]:
        return 0
    try:
        due = db.execute("""
            SELECT s.id FROM managed_source s
            JOIN source_document d ON d.platform=s.document_platform AND d.document_id=s.document_id
            WHERE s.provider='feishu' AND (
                s.sync_requested OR (COALESCE(s.auto_sync, d.current_version <> '')
                AND (d.current_version='' OR d.published))
            ) AND (s.sync_next_at IS NULL OR s.sync_next_at <= statement_timestamp())
            ORDER BY s.sync_requested DESC, s.sync_next_at NULLS FIRST, s.id LIMIT 6
        """).fetchall()
        application_token = cache(credential)
        for identifier, in due:
            sync_source(db, identifier, hybrid, application_token)
        return len(due)
    finally:
        db.execute("SELECT pg_advisory_unlock(5620191339462067151)")


def main():
    logging.basicConfig(level=logging.INFO)
    hybrid = os.environ.get("KB_ENABLE_HYBRID", "0") == "1"
    with connect() as db:
        migrate(db)
        count = check_due(db, hybrid)
    if count:
        log.info("Checked %d Feishu source(s)", count)


if __name__ == "__main__":
    main()
