"""Durable file parsing queue shared by the console and the parser worker."""
import argparse
import csv
from hashlib import sha256
from io import StringIO
import logging
import os
from pathlib import Path
import sys
from time import sleep
from urllib.error import HTTPError
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent))

from access import connect
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from admin import (ManagementError, activate, build_indexes, index_errors, markdown_chunks,
                   migrate, record_ingest, save_version)
import search
from docling_parser import FORMATS, MAX_FILE, ParseError, evidence, result, submit, validate_file


LOG = logging.getLogger(__name__)
QUEUED_SUFFIXES = FORMATS.keys() | {".csv"}
TEXT_SUFFIXES = {".md": "text/markdown", ".txt": "text/plain"}
SUPPORTED_SUFFIXES = QUEUED_SUFFIXES | TEXT_SUFFIXES.keys()
FIELDS = """id, document_id, title, filename, status, attempts, last_error, created_at,
            publication_status, publication_error, auto_publish"""


def queue(db, library, document, title, filename, content, *, requested_by=None, auto_publish=False,
          requires_mcp_grant=False):
    suffix = Path(filename).suffix.lower()
    if suffix == ".csv" or suffix in TEXT_SUFFIXES:
        if not 0 < len(content) <= MAX_FILE:
            raise ParseError("请选择非空且不超过 20 MiB 的文件。")
        mime = TEXT_SUFFIXES.get(suffix, "text/csv")
    else:
        _, mime = validate_file(filename, content)
    doc_id = str(document or uuid4())
    digest = sha256(content).hexdigest()
    with db.transaction():
        if not db.execute("SELECT 1 FROM knowledge_base WHERE id=%s", (library,)).fetchone():
            raise ManagementError("找不到目标知识库。")
        if document is None:
            db.execute("""
                INSERT INTO source_document (platform, document_id, kb_id, current_version, published)
                VALUES ('upload', %s, %s, '', false)
            """, (doc_id, library))
        row = db.execute("""
            SELECT kb_id FROM source_document WHERE platform='upload' AND document_id=%s FOR UPDATE
        """, (doc_id,)).fetchone()
        if not row or row[0] != library:
            raise ManagementError("文档不属于目标知识库。")
        if db.execute("SELECT 1 FROM managed_source WHERE document_id=%s", (doc_id,)).fetchone():
            raise ManagementError("在线来源请使用同步功能更新。")
        with db.cursor(row_factory=dict_row) as cur:
            row = cur.execute("""
                INSERT INTO managed_file (id, library, document_id, title, filename, content_type, content, digest,
                                          requested_by, auto_publish, requires_mcp_grant)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (library, document_id, digest, title) DO NOTHING
                RETURNING """ + FIELDS,
                (uuid4(), library, doc_id, title, filename, mime, content, digest, requested_by, auto_publish,
                 requires_mcp_grant)).fetchone()
            if row:
                if requested_by:
                    record_ingest(db, "file", row["id"], library, requested_by, title)
                return row
            return cur.execute("SELECT " + FIELDS + """
                FROM managed_file WHERE library=%s AND document_id=%s AND digest=%s AND title=%s
            """, (library, doc_id, digest, title)).fetchone()


def jobs(db, library):
    with db.cursor(row_factory=dict_row) as cur:
        return cur.execute("SELECT " + FIELDS + """
            FROM managed_file WHERE library=%s ORDER BY created_at DESC LIMIT 100
        """, (library,)).fetchall()


def file_status(db, job_id, library):
    with db.cursor(row_factory=dict_row) as cur:
        return cur.execute("SELECT " + FIELDS + " FROM managed_file WHERE id=%s AND library=%s",
                           (job_id, library)).fetchone()


def retry(db, job_id):
    with db.cursor(row_factory=dict_row) as cur:
        job = cur.execute("""
            UPDATE managed_file SET status='queued', provider_task=NULL, attempts=0, last_error=NULL,
                lease=NULL, lease_until=NULL, next_check=now(), deadline=now()+interval '1 hour'
            WHERE id=%s AND status='failed' RETURNING """ + FIELDS, (job_id,)).fetchone()
    if not job:
        raise ManagementError("只有失败的解析任务可以重试。")
    return job


def queue_publication(db, document, version, expected, valid_from=None, valid_until=None, *, actor_id=None):
    if valid_from and valid_until and valid_until <= valid_from:
        raise ManagementError("到期时间必须晚于生效时间。")
    with db.transaction(), db.cursor(row_factory=dict_row) as cur:
        row = cur.execute("""
            SELECT current_version, published, valid_until FROM source_document
            WHERE platform='upload' AND document_id=%s FOR UPDATE
        """, (document,)).fetchone()
        revision = cur.execute("""
            SELECT publication_status FROM managed_file WHERE id=%s AND document_id=%s AND status='ready'
        """, (version, document)).fetchone()
        if revision is None:
            return None
        if row is None or row["current_version"] != expected:
            raise ManagementError("生效版本已变化，请刷新后再操作。")
        if revision["publication_status"] in ("queued", "indexing"):
            return {"publication_status": revision["publication_status"]}
        if cur.execute("""
            SELECT 1 FROM managed_file WHERE document_id=%s AND publication_status IN ('queued','indexing')
        """, (document,)).fetchone():
            raise ManagementError("此文档已有版本正在发布。")
        cur.execute("""
            UPDATE managed_file SET publication_status='queued', publication_expected=%s,
                publication_previous_live=%s, publication_previous_until=%s, publication_requested_at=now(),
                publication_error=NULL, publication_valid_from=%s, publication_valid_until=%s,
                publication_actor_id=COALESCE(%s, requested_by)
            WHERE id=%s
        """, (expected, row["published"], row["valid_until"], valid_from, valid_until, actor_id, version))
    return {"publication_status": "queued"}


def clean_deleted_index(db):
    with db.transaction():
        row = db.execute("""
            SELECT index_name FROM managed_index_cleanup WHERE retry_after <= now()
            ORDER BY retry_after FOR UPDATE SKIP LOCKED LIMIT 1
        """).fetchone()
        if not row:
            return False
        name, = row
        try:
            search.request("DELETE", "/" + name)
        except OSError as error:
            if not isinstance(error, HTTPError) or error.code != 404:
                db.execute("UPDATE managed_index_cleanup SET retry_after=now()+interval '1 minute' WHERE index_name=%s",
                           (name,))
                return False
        db.execute("DELETE FROM managed_index_cleanup WHERE index_name=%s", (name,))
    return True


def queue_next_automatic_publication(db):
    row = db.execute("""
        SELECT f.document_id, f.id, d.current_version
        FROM managed_file f JOIN source_document d ON d.platform='upload' AND d.document_id=f.document_id
        JOIN knowledge_base k ON k.id=f.library
        WHERE f.status='ready' AND f.publication_status IS NULL AND (f.auto_publish OR k.owner_member_id IS NOT NULL)
          AND NOT EXISTS (
            SELECT 1 FROM managed_file busy WHERE busy.document_id=f.document_id
              AND busy.publication_status IN ('queued','indexing')
          )
          AND NOT EXISTS (
            SELECT 1 FROM managed_file newer WHERE newer.document_id=f.document_id
              AND (newer.created_at, newer.id) > (f.created_at, f.id)
              AND newer.status <> 'failed' AND (newer.auto_publish OR k.owner_member_id IS NOT NULL)
          )
        ORDER BY f.created_at, f.id LIMIT 1
    """).fetchone()
    if row:
        try:
            queue_publication(db, *row)
        except ManagementError:
            return


def publish_next(db, hybrid):
    lock_id = 5620191339462067150
    if not db.execute("SELECT pg_try_advisory_lock(%s)", (lock_id,)).fetchone()[0]:
        return False
    try:
        cleaned = clean_deleted_index(db)
        queue_next_automatic_publication(db)
        with db.cursor(row_factory=dict_row) as cur:
            job = cur.execute("""
                UPDATE managed_file SET publication_status='indexing'
                WHERE id=(SELECT id FROM managed_file WHERE publication_status IN ('queued','indexing')
                          ORDER BY publication_requested_at LIMIT 1)
                RETURNING id, library, document_id, requested_by, requires_mcp_grant, publication_actor_id,
                          publication_expected, publication_previous_live,
                          publication_previous_until, publication_valid_from, publication_valid_until
            """).fetchone()
        if not job:
            return cleaned
        version = job["id"]
        try:
            chunks, keyword, vector = db.execute("""
                SELECT chunks, keyword_index, vector_index FROM managed_version WHERE id=%s
            """, (version,)).fetchone()
            with index_errors(db, version):
                prepared = (keyword, vector) if keyword and (vector or not hybrid) else build_indexes(chunks, hybrid)
            schedule = None
            if job["publication_valid_from"] is not None or job["publication_valid_until"] is not None:
                schedule = (job["publication_valid_from"], job["publication_valid_until"])
            with db.transaction():
                owner, = db.execute("SELECT owner_member_id FROM knowledge_base WHERE id=%s", (job["library"],)).fetchone()
                if owner and not db.execute("""
                    SELECT 1 FROM member_account WHERE id=%s AND enabled AND personal_upload FOR UPDATE
                """, (owner,)).fetchone():
                    raise ManagementError("个人知识库上传权限已撤销。")
                if job["requires_mcp_grant"]:
                    active_admin = db.execute("""
                        SELECT 1 FROM admin_account WHERE id=%s AND enabled FOR UPDATE
                    """, (job["requested_by"],)).fetchone()
                    if not active_admin or not db.execute("""
                        SELECT 1 FROM admin_upload_grant WHERE admin_id=%s AND library=%s
                    """, (job["requested_by"], job["library"])).fetchone():
                        raise ManagementError("此企业库的 MCP 上传授权已撤销。")
                activate(db, job["document_id"], version, job["publication_expected"], hybrid, prepared=prepared,
                         availability=(job["publication_previous_live"], job["publication_previous_until"]),
                         schedule=schedule, actor_id=job["publication_actor_id"] or owner)
                db.execute("UPDATE managed_file SET publication_status='published' WHERE id=%s", (version,))
        except ManagementError as error:
            db.execute("UPDATE managed_file SET publication_status='failed', publication_error=%s WHERE id=%s",
                       (str(error), version))
        return True
    finally:
        db.execute("SELECT pg_advisory_unlock(%s)", (lock_id,))


def csv_evidence(data, job):
    try:
        original = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            original = data.decode("gb18030")
        except UnicodeDecodeError:
            raise ParseError("无法识别 CSV 编码，请另存为 UTF-8 CSV。") from None
    if "\x00" in original:
        raise ParseError("CSV 文件包含无效字符，请重新导出。")

    reader = csv.reader(StringIO(original, newline=""), strict=True)
    version = str(job["id"])
    source = f"/v1/knowledge/files/{version}"
    columns, chunks = [], []
    header_line, previous_end = 0, 0
    try:
        for row in reader:
            start, previous_end = previous_end + 1, reader.line_num
            if not any(value.strip() for value in row):
                continue
            if not columns:
                columns = [value.strip() or f"第 {index} 列" for index, value in enumerate(row, 1)]
                header_line = start
                continue
            if len(row) != len(columns):
                raise ParseError(f"CSV 第 {start} 行的列数与表头不一致。")
            if len(chunks) == 2500:
                raise ParseError("CSV 超过 2500 条数据，请拆成多个文件上传。")
            text = "\n".join(f"{column}：{value}" for column, value in zip(columns, row))
            if len(text) > 20000:
                raise ParseError(f"CSV 第 {start} 行内容过长，请拆分此行后再上传。")
            chunks.append({
                "chunk_id": sha256(f"{version}:csv:{start}".encode()).hexdigest(),
                "platform": "upload", "document_id": job["document_id"], "document_version": version,
                "title": job["title"], "heading_path": [], "text": text,
                "source_url": source, "reference_url": source,
                "locator": {"position_unit": "csv_line", "line_start": start, "line_end": previous_end,
                            "table_header_line": header_line},
                "metadata": {},
            })
    except csv.Error:
        raise ParseError(f"CSV 第 {previous_end + 1} 行格式有误，请检查分隔符或引号。") from None
    if not chunks:
        raise ParseError("CSV 需要表头和至少一条数据。")
    return original, {"format": "csv", "columns": columns, "header_line": header_line}, chunks


def text_evidence(data, job):
    try:
        original = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ParseError("请将文本另存为 UTF-8。") from None
    if "\x00" in original:
        raise ParseError("文本文件包含无效字符。")
    source = f"/v1/knowledge/files/{job['id']}"
    chunks = markdown_chunks(job["document_id"], job["id"], job["title"], original, source)
    return original, {"format": Path(job["filename"]).suffix.lstrip(".").lower()}, chunks


def claim(db):
    lease = uuid4()
    with db.transaction(), db.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT pg_advisory_xact_lock(5620191339462067149)")
        return cur.execute("""
            WITH next_job AS (
                SELECT id FROM managed_file
                WHERE status IN ('queued','parsing') AND next_check <= clock_timestamp()
                  AND (lease_until IS NULL OR lease_until <= clock_timestamp())
                  AND (status='parsing' OR deadline <= clock_timestamp() OR lower(right(filename,4)) IN ('.csv','.txt') OR lower(right(filename,3))='.md' OR (
                      SELECT count(*) FROM managed_file WHERE status='parsing' AND deadline > clock_timestamp()
                  ) < 4)
                ORDER BY next_check, created_at
                FOR UPDATE SKIP LOCKED LIMIT 1
            )
            UPDATE managed_file AS f SET lease=%s, lease_until=clock_timestamp()+interval '2 minutes',
                status='parsing'
            FROM next_job WHERE f.id=next_job.id
            RETURNING f.id, f.library, f.document_id, f.filename, f.title, f.provider_task, f.attempts,
                f.lease, f.deadline <= clock_timestamp() AS expired
        """, (lease,)).fetchone()


def continue_job(db, job, remote, *, polled=False):
    db.execute("""
        UPDATE managed_file SET provider_task=%s, lease=NULL, lease_until=NULL,
            attempts=CASE WHEN %s THEN 0 ELSE attempts END,
            last_error=NULL, next_check=clock_timestamp()+interval '3 seconds'
        WHERE id=%s AND lease=%s AND status='parsing'
    """, (remote, polled, job["id"], job["lease"]))


def fail(db, job, error):
    attempt = job["attempts"] + 1
    retryable = isinstance(error, ParseError) and error.retryable and attempt < 4
    db.execute("""
        UPDATE managed_file SET status=%s, attempts=%s, last_error=%s, lease=NULL, lease_until=NULL,
            provider_task=CASE WHEN %s THEN NULL ELSE provider_task END,
            next_check=clock_timestamp() + %s * interval '1 second'
        WHERE id=%s AND lease=%s AND status='parsing'
    """, ("parsing" if retryable else "failed", attempt, str(error),
           isinstance(error, ParseError) and error.task_missing, 3 * 2 ** attempt, job["id"], job["lease"]))


def finish(db, job, parsed):
    original, structured, chunks = parsed
    with db.transaction():
        if not db.execute("""
            SELECT 1 FROM source_document WHERE platform='upload' AND document_id=%s FOR UPDATE
        """, (job["document_id"],)).fetchone():
            return
        if not db.execute("""
            SELECT 1 FROM managed_file WHERE id=%s AND lease=%s AND status='parsing' FOR UPDATE
        """, (job["id"], job["lease"])).fetchone():
            return
        save_version(db, job["library"], job["document_id"], job["id"], job["title"],
                     job["filename"], original, chunks)
        db.execute("""
            UPDATE managed_file SET status='ready', parser_document=%s, last_error=NULL, lease=NULL, lease_until=NULL
            WHERE id=%s
        """, (Jsonb(structured), job["id"]))


def tick(db):
    job = claim(db)
    if not job:
        return False
    try:
        if job["expired"]:
            raise ParseError("解析超时，请检查处理服务后重试。")
        remote = job["provider_task"]
        if remote is None:
            row = db.execute("SELECT content FROM managed_file WHERE id=%s AND lease=%s",
                             (job["id"], job["lease"])).fetchone()
            if row is None:
                return True
            suffix = Path(job["filename"]).suffix.lower()
            if suffix == ".csv":
                finish(db, job, csv_evidence(row[0], job))
            elif suffix in TEXT_SUFFIXES:
                finish(db, job, text_evidence(row[0], job))
            else:
                remote = submit(job["filename"], row[0], FORMATS[suffix][0])
                continue_job(db, job, remote)
        elif (payload := result(remote)) is None:
            continue_job(db, job, remote, polled=True)
        else:
            finish(db, job, evidence(payload, job))
    except (ParseError, ManagementError) as error:
        fail(db, job, error)
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        LOG.warning("Invalid parser result for job %s (%s)", job["id"], type(error).__name__)
        fail(db, job, ParseError("解析结果格式不完整。"))
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Process at most one queued or polling job.")
    parser.add_argument("--publish", action="store_true", help="Build indexes and activate approved drafts.")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    with connect() as db:
        migrate(db)
        while True:
            handled = publish_next(db, os.environ.get("KB_ENABLE_HYBRID") == "1") if args.publish else tick(db)
            if args.once:
                return
            if not handled:
                sleep(2)


if __name__ == "__main__":
    main()
