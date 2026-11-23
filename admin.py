"""Knowledge management operations and revision publication."""
from contextlib import contextmanager
from hashlib import sha256
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from uuid import uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

import dashscope_models
from ingest import notion_units, split_text
from metadata import normalize
import search


ROOT = Path(__file__).resolve().parent
VISIBILITY_STATUS = """
    CASE WHEN d.current_version = '' THEN 'draft'
         WHEN NOT d.published THEN 'off'
         WHEN d.valid_until <= statement_timestamp() THEN 'expired'
         WHEN d.valid_from > statement_timestamp() THEN 'pending'
         ELSE 'live' END
"""


class ManagementError(ValueError):
    pass


def migrate(db):
    with db.transaction():
        db.execute((ROOT / "schema.sql").read_text(encoding="utf-8"))
        db.execute((ROOT / "admin_schema.sql").read_text(encoding="utf-8"))


def libraries(db):
    return db.execute("SELECT id, name FROM knowledge_base ORDER BY name").fetchall()


def documents(db, library):
    with db.cursor(row_factory=dict_row) as cur:
        return cur.execute("""
            SELECT d.document_id AS id, d.current_version, d.published, d.owner,
                   v.title, v.created_at AS updated_at, v.chunk_count, v.version_count,
                   s.id AS source_id, COALESCE(s.provider, 'upload') AS source,
                   """ + VISIBILITY_STATUS + """ AS visibility_status
            FROM source_document d
            JOIN LATERAL (
                SELECT title, created_at, jsonb_array_length(chunks) AS chunk_count,
                       (SELECT count(*) FROM managed_version m2
                        WHERE m2.platform = d.platform AND m2.document_id = d.document_id) AS version_count
                FROM managed_version
                WHERE platform = d.platform AND document_id = d.document_id
                ORDER BY (id::text = d.current_version) DESC, created_at DESC LIMIT 1
            ) v ON true
            LEFT JOIN managed_source s ON s.document_id = d.document_id
            WHERE d.kb_id = %s AND d.platform = 'upload'
            ORDER BY v.title, d.document_id
        """, (library,)).fetchall()


def versions(db, document, hybrid):
    with db.cursor(row_factory=dict_row) as cur:
        return cur.execute("""
            SELECT v.id, v.title, v.filename, v.created_at,
                   (v.keyword_index IS NOT NULL AND (%s = false OR v.vector_index IS NOT NULL)) AS ready,
                   v.last_error, jsonb_array_length(v.chunks) AS chunk_count,
                   d.current_version, d.published, d.valid_from, d.valid_until, """ + VISIBILITY_STATUS + """ AS visibility_status
            FROM managed_version v JOIN source_document d USING (platform, document_id)
            WHERE v.platform = 'upload' AND v.document_id = %s
            ORDER BY v.created_at DESC, v.id DESC
        """, (hybrid, document)).fetchall()


def preview(db, document, version):
    with db.cursor(row_factory=dict_row) as cur:
        row = cur.execute("""
            SELECT title, filename, original, chunks FROM managed_version
            WHERE platform = 'upload' AND document_id = %s AND id = %s
        """, (document, version)).fetchone()
    if not row:
        raise ManagementError("找不到此版本。")
    return row


def set_availability(db, document, expected, expected_published, expected_valid_until, published, valid_until,
                     valid_from=None):
    with db.transaction(), db.cursor(row_factory=dict_row) as cur:
        row = cur.execute("""
            SELECT current_version, published, valid_from, valid_until
            FROM source_document WHERE platform='upload' AND document_id=%s FOR UPDATE
        """, (document,)).fetchone()
        if not row or not row["current_version"]:
            raise ManagementError("请先发布一个版本，再设置可见性。")
        if (row["current_version"], row["published"], row["valid_until"]) != (
            expected, expected_published, expected_valid_until
        ):
            raise ManagementError("文档状态已变化，请刷新后再操作。")
        if valid_until and valid_from and valid_until <= valid_from:
            raise ManagementError("到期时间必须晚于生效时间。")
        if valid_until and row["valid_from"] and valid_until <= row["valid_from"]:
            raise ManagementError("到期时间必须晚于生效时间。")
        if (row["published"], row["valid_until"], row["valid_from"]) != (published, valid_until, valid_from):
            cur.execute("""
                UPDATE source_document SET published=%s, valid_until=%s, valid_from=%s
                WHERE platform='upload' AND document_id=%s
            """, (published, valid_until, valid_from, document))
            cur.execute("""
                INSERT INTO managed_availability_event
                    (document_id, version_id, was_published, published, previous_valid_until, valid_until,
                     previous_valid_from, valid_from)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (document, expected, row["published"], published, row["valid_until"], valid_until,
                  row["valid_from"], valid_from))
    return {"published": published, "valid_until": valid_until, "valid_from": valid_from}


def upload(db, library, document, title, filename, original, metadata=None, *,
           source_url=None, provider="upload", line_blocks=None):
    document = document or str(uuid4())
    version = uuid4()
    source = source_url or f"/v1/knowledge/originals/{document}/{version}"
    metadata = normalize(metadata)
    chunks = []
    for unit in notion_units(original):
        for part, text in enumerate(split_text(unit.text), 1):
            locator = {**unit.locator, "part": part}
            block_id = (line_blocks or {}).get(locator["line_start"])
            if block_id:
                locator["block_id"] = block_id
            chunk_id = sha256(f"{version}:{json.dumps(locator, sort_keys=True)}".encode()).hexdigest()
            chunks.append({
                "chunk_id": chunk_id, "platform": provider, "document_id": document,
                "document_version": str(version), "title": title, "heading_path": list(unit.heading),
                "text": text, "source_url": source, "reference_url": f"{source}#{block_id}" if block_id else source,
                "locator": locator,
                "metadata": metadata,
            })
    if not chunks:
        raise ManagementError("文档没有可以检索的正文。")
    if len(chunks) > 200:
        raise ManagementError("此文档超过 200 个分块，请拆成多份上传。")
    return save_version(db, library, document, version, title, filename, original, chunks,
                        online=source_url is not None)


def save_version(db, library, document, version, title, filename, original, chunks, *, online=False):
    with db.transaction():
        if not db.execute("SELECT 1 FROM knowledge_base WHERE id = %s", (library,)).fetchone():
            raise ManagementError("找不到此知识库。")
        db.execute("""
            INSERT INTO source_document (platform, document_id, kb_id, current_version, published)
            VALUES ('upload', %s, %s, '', false) ON CONFLICT DO NOTHING
        """, (document, library))
        actual_library, = db.execute("""
            SELECT kb_id FROM source_document WHERE platform='upload' AND document_id=%s FOR UPDATE
        """, (document,)).fetchone()
        if actual_library != library:
            raise ManagementError("文档不属于当前知识库。")
        if not online and db.execute("SELECT 1 FROM managed_source WHERE document_id=%s", (document,)).fetchone():
            raise ManagementError("此文档已绑定在线来源，请从来源同步新版。")
        db.execute("""
            INSERT INTO managed_version (id, platform, document_id, title, filename, original, chunks)
            VALUES (%s, 'upload', %s, %s, %s, %s, %s)
        """, (version, document, title, filename, original, Jsonb(chunks)))
    return {"document_id": document, "version_id": version, "chunk_count": len(chunks)}


def build_indexes(chunks, hybrid):
    with TemporaryDirectory() as folder:
        path = Path(folder) / "chunks.jsonl"
        path.write_text("".join(json.dumps(chunk, ensure_ascii=False) + "\n" for chunk in chunks), encoding="utf-8")
        keyword = search.index_snapshot(path, managed=True)
        vector = search.index_snapshot(path, dashscope_models, managed=True) if hybrid else None
    return keyword, vector


@contextmanager
def index_errors(db, version):
    try:
        yield
    except (OSError, ValueError, KeyError) as error:
        if isinstance(error, ManagementError):
            raise
        if isinstance(error, dashscope_models.ModelError):
            message = f"百炼向量构建失败。{error}"
        else:
            message = f"索引构建或校验失败（{type(error).__name__}）。原版本仍生效。"
        db.execute("""
            UPDATE managed_version SET last_error=%s
            WHERE id=%s AND keyword_index IS NULL
        """, (message, version))
        raise ManagementError(message) from None


def activate(db, document, version, expected, hybrid, rollback=False, *, prepared=None, availability=None,
             schedule=None):
    with index_errors(db, version), db.transaction(), db.cursor(row_factory=dict_row) as cur:
        row = cur.execute("""
            SELECT current_version, published, valid_until FROM source_document
            WHERE platform='upload' AND document_id=%s FOR UPDATE
        """, (document,)).fetchone()
        if not row:
            raise ManagementError("找不到此文档。")
        current = row["current_version"]
        if current != expected:
            raise ManagementError("生效版本已变化，请刷新后再操作。")
        if availability is not None and (row["published"], row["valid_until"]) != availability:
            raise ManagementError("文档的上下架状态或有效期已变化，请核对后重新发布。")
        revision = cur.execute("""
            SELECT chunks, keyword_index, vector_index FROM managed_version
            WHERE platform='upload' AND document_id=%s AND id=%s
        """, (document, version)).fetchone()
        if not revision:
            raise ManagementError("找不到此版本。")
        keyword, vector = prepared or (revision["keyword_index"], revision["vector_index"])
        if not keyword or (hybrid and not vector):
            if rollback:
                raise ManagementError("此版本尚未构建当前检索模式所需的索引。")
            keyword, vector = build_indexes(revision["chunks"], hybrid)
        for index in (keyword, vector):
            if index and search.request("GET", f"/{index}/_count")["count"] != len(revision["chunks"]):
                raise ManagementError("目标版本索引不完整。")
        cur.executemany("""
            INSERT INTO source_chunk (chunk_id, platform, document_id, version)
            VALUES (%s, 'upload', %s, %s) ON CONFLICT DO NOTHING
        """, [(chunk["chunk_id"], document, str(version)) for chunk in revision["chunks"]])
        cur.execute("""
            UPDATE managed_version SET keyword_index=%s, vector_index=%s, last_error=NULL WHERE id=%s
        """, (keyword, vector, version))
        cur.execute("""
            UPDATE source_document SET current_version=%s, published=true
            WHERE platform='upload' AND document_id=%s
        """, (str(version), document))
        if schedule is not None:
            valid_from, valid_until = schedule
            cur.execute("""
                UPDATE source_document SET valid_from=%s, valid_until=%s
                WHERE platform='upload' AND document_id=%s
            """, (valid_from, valid_until, document))
            cur.execute("""
                INSERT INTO managed_availability_event
                    (document_id, version_id, was_published, published, previous_valid_until, valid_until,
                     previous_valid_from, valid_from)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (document, version, row["published"], True, row["valid_until"], valid_until, None, valid_from))
        if current != str(version):
            cur.execute("""
                INSERT INTO managed_publish_event (document_id, previous_version, version_id, action)
                VALUES (%s, %s, %s, %s)
            """, (document, current, version, "rollback" if rollback else "publish"))
    return {"current_version": version}


def set_owner(db, document, owner):
    with db.transaction():
        if not db.execute("SELECT 1 FROM source_document WHERE platform='upload' AND document_id=%s",
                          (document,)).fetchone():
            raise ManagementError("找不到此文档。")
        db.execute("UPDATE source_document SET owner=%s WHERE platform='upload' AND document_id=%s",
                   (owner, document))
    return {"owner": owner}


def member_directory(db):
    with db.cursor(row_factory=dict_row) as cur:
        return cur.execute("""
            SELECT m.id, COALESCE(m.display_name, m.id) AS name, m.email,
                   COALESCE(m.kind, 'employee') AS kind, m.enabled,
                   COALESCE(json_agg(json_build_object(
                       'library', g.kb_id, 'name', kb.name,
                       'expires_at', g.expires_at, 'revoked_at', g.revoked_at
                   ) ORDER BY kb.name) FILTER (WHERE g.kb_id IS NOT NULL), '[]'::json) AS grants
            FROM member_account m
            LEFT JOIN knowledge_grant g ON g.member_id = m.id
            LEFT JOIN knowledge_base kb ON kb.id = g.kb_id
            GROUP BY m.id
            ORDER BY name
        """).fetchall()


def set_member_grants(db, member, libraries, expires_at=None):
    if expires_at is not None and expires_at.utcoffset() is None:
        raise ManagementError("到期时间需要包含时区。")
    with db.transaction():
        if not db.execute("SELECT 1 FROM member_account WHERE id=%s AND enabled", (member,)).fetchone():
            raise ManagementError("找不到已启用的成员。")
        known = {row[0] for row in db.execute("SELECT id FROM knowledge_base").fetchall()}
        unknown = [name for name in libraries if name not in known]
        if unknown:
            raise ManagementError("找不到知识库：" + "、".join(unknown))
        db.execute("""
            UPDATE knowledge_grant SET revoked_at=now(), updated_at=now()
            WHERE member_id=%s AND revoked_at IS NULL AND NOT (kb_id = ANY(%s))
        """, (member, libraries))
        for library in libraries:
            db.execute("""
                INSERT INTO knowledge_grant (member_id, kb_id, expires_at, revoked_at, updated_at)
                VALUES (%s, %s, %s, NULL, now())
                ON CONFLICT (member_id, kb_id) DO UPDATE
                  SET revoked_at=NULL, expires_at=excluded.expires_at, updated_at=now()
            """, (member, library, expires_at))
    return {"member": member, "libraries": libraries}


PARSE_STATES = {"queued": "queued", "parsing": "running", "ready": "done", "failed": "failed"}
PUBLISH_STATES = {"queued": "queued", "indexing": "running", "published": "done", "failed": "failed"}


def task_feed(db, library=None, days=30):
    scope, parameters = "", []
    if library:
        scope, parameters = " AND f.library=%s", [library]
    with db.cursor(row_factory=dict_row) as cur:
        files = cur.execute("""
            SELECT f.id, f.document_id, f.library, kb.name AS library_name, f.title, f.filename,
                   f.status, f.attempts, f.next_check, f.last_error, f.created_at,
                   f.publication_status, f.publication_error, f.publication_requested_at,
                   -- next_check is the "look at me again" clock, and a live worker claims a
                   -- free queued job immediately, so a queue that nobody touched for five
                   -- minutes means the parser is down. The NOT EXISTS guard keeps a busy
                   -- worker from looking dead: while it runs anything, the queue is just deep.
                   ((f.status = 'queued' AND f.attempts = 0
                     AND f.next_check < statement_timestamp() - interval '5 minutes'
                     AND NOT EXISTS (SELECT 1 FROM managed_file busy WHERE busy.status = 'parsing'))
                    OR (f.status = 'parsing' AND f.lease_until IS NOT NULL
                        AND f.lease_until < statement_timestamp() - interval '5 minutes')) AS stalled,
                   GREATEST(0, EXTRACT(EPOCH FROM (statement_timestamp() - f.created_at))::int)
                     AS waiting_seconds
            FROM managed_file f JOIN knowledge_base kb ON kb.id = f.library
            WHERE f.created_at > statement_timestamp() - make_interval(days => %s)""" + scope + """
            ORDER BY COALESCE(f.publication_requested_at, f.created_at) DESC LIMIT 200
        """, [days, *parameters]).fetchall()
        source_scope, source_parameters = "", []
        if library:
            source_scope, source_parameters = " AND s.library=%s", [library]
        sources = cur.execute("""
            SELECT s.id, s.document_id, s.library, kb.name AS library_name, s.title, s.provider,
                   s.checked_at, s.last_error
            FROM managed_source s JOIN knowledge_base kb ON kb.id = s.library
            WHERE s.checked_at > statement_timestamp() - make_interval(days => %s)""" + source_scope + """
            ORDER BY s.checked_at DESC LIMIT 100
        """, [days, *source_parameters]).fetchall()

    items = []
    for row in files:
        if row["publication_status"]:
            items.append({
                "id": "publish:" + str(row["id"]), "kind": "publish", "job_id": str(row["id"]),
                "version_id": str(row["id"]), "document_id": row["document_id"],
                "library": row["library"], "library_name": row["library_name"],
                "title": row["title"], "filename": row["filename"],
                "status": PUBLISH_STATES.get(row["publication_status"], "queued"),
                "error": row["publication_error"], "at": row["publication_requested_at"],
            })
        items.append({
            "id": "parse:" + str(row["id"]), "kind": "parse", "job_id": str(row["id"]),
            "version_id": str(row["id"]), "document_id": row["document_id"],
            "library": row["library"], "library_name": row["library_name"],
            "title": row["title"], "filename": row["filename"],
            "status": PARSE_STATES.get(row["status"], "queued"),
            "error": row["last_error"], "at": row["created_at"],
            "stalled": bool(row["stalled"]), "waiting_seconds": row["waiting_seconds"],
        })
    for row in sources:
        items.append({
            "id": "sync:" + str(row["id"]), "kind": "sync", "source_id": str(row["id"]),
            "document_id": row["document_id"], "library": row["library"],
            "library_name": row["library_name"], "title": row["title"],
            "filename": row["provider"], "status": "failed" if row["last_error"] else "done",
            "error": row["last_error"], "at": row["checked_at"],
        })
    items.sort(key=lambda item: item["at"] or "", reverse=True)
    return items


AUDIT_KINDS = {"publish": "发布", "rollback": "回滚", "unpublish": "下架", "restore": "恢复上线",
               "schedule": "调整有效期", "grant": "授权", "revoke": "撤销访问"}


def audit_feed(db, library=None, kind=None, limit=200):
    scope, parameters = "", []
    if library:
        scope, parameters = " WHERE library = %s", [library]
    with db.cursor(row_factory=dict_row) as cur:
        rows = cur.execute("""
            SELECT * FROM (
                SELECT 'publish' AS source, e.created_at AS at, e.action AS action,
                       d.kb_id AS library, kb.name AS library_name, mv.title AS object_title,
                       e.version_id::text AS version_id, e.document_id, NULL::text AS member
                FROM managed_publish_event e
                JOIN managed_version mv ON mv.id = e.version_id
                JOIN source_document d ON d.platform='upload' AND d.document_id = e.document_id
                JOIN knowledge_base kb ON kb.id = d.kb_id
                UNION ALL
                SELECT 'availability',
                       a.created_at,
                       CASE WHEN a.was_published IS DISTINCT FROM a.published
                            THEN CASE WHEN a.published THEN 'restore' ELSE 'unpublish' END
                            ELSE 'schedule' END,
                       d.kb_id, kb.name, mv.title, a.version_id::text, a.document_id, NULL
                FROM managed_availability_event a
                JOIN managed_version mv ON mv.id = a.version_id
                JOIN source_document d ON d.platform='upload' AND d.document_id = a.document_id
                JOIN knowledge_base kb ON kb.id = d.kb_id
                UNION ALL
                SELECT 'grant', g.updated_at, CASE WHEN g.revoked_at IS NULL THEN 'grant' ELSE 'revoke' END,
                       g.kb_id, kb.name, COALESCE(m.display_name, m.id), NULL, NULL, m.id
                FROM knowledge_grant g
                JOIN knowledge_base kb ON kb.id = g.kb_id
                JOIN member_account m ON m.id = g.member_id
            ) feed""" + scope + """
            ORDER BY at DESC LIMIT %s
        """, [*parameters, limit]).fetchall()
    for row in rows:
        row["action_label"] = AUDIT_KINDS.get(row["action"], row["action"])
    if kind:
        rows = [row for row in rows if row["action"] == kind]
    return rows


def _terms(text):
    lowered = text.lower()
    terms = set(re.findall(r"[a-z0-9]{2,}", lowered))
    for run in re.findall(r"[\u4e00-\u9fff]+", lowered):
        terms.add(run)
        terms.update(run[index:index + 2] for index in range(len(run) - 1))
    return terms


def probe(db, document, version, question, member):
    """Score one draft version against a question for a specific member's permissions."""
    with db.cursor(row_factory=dict_row) as cur:
        row = cur.execute("""
            SELECT d.kb_id, d.current_version, d.published, d.valid_from, d.valid_until,
                   kb.name AS library_name, v.title, v.chunks,
                   COALESCE(m.display_name, m.id) AS member_name, m.kind
            FROM source_document d
            JOIN knowledge_base kb ON kb.id = d.kb_id
            JOIN managed_version v ON v.platform = d.platform AND v.document_id = d.document_id AND v.id = %s
            LEFT JOIN member_account m ON m.id = %s
            WHERE d.platform='upload' AND d.document_id = %s
        """, (version, member, document)).fetchone()
        if not row:
            raise ManagementError("找不到此版本或此成员。")
        grant = cur.execute("""
            SELECT expires_at FROM knowledge_grant
            WHERE member_id=%s AND kb_id=%s AND revoked_at IS NULL
              AND (expires_at IS NULL OR expires_at > statement_timestamp())
        """, (member, row["kb_id"])).fetchone()
    if row["member_name"] is None:
        raise ManagementError("找不到此成员。")
    if not grant:
        return {"allowed": False, "member": row["member_name"], "library": row["library_name"],
                "reason": f"{row['member_name']} 没有「{row['library_name']}」的访问权限。",
                "hits": [], "live": False}
    terms = _terms(question)
    scored = []
    for chunk in row["chunks"]:
        heading = " / ".join(chunk.get("heading_path") or [])
        body = chunk.get("text") or ""
        haystack = (chunk.get("title") or "") + "\n" + heading + "\n" + body
        matched = terms & _terms(haystack)
        if not matched:
            continue
        heading_hits = len(terms & _terms(heading))
        score = len(matched) + heading_hits * 2
        scored.append((score, len(matched), chunk, heading))
    scored.sort(key=lambda item: (-item[0], -item[1]))
    hits = [{
        "chunk_id": chunk["chunk_id"], "text": chunk["text"], "heading_path": chunk.get("heading_path") or [],
        "locator": chunk.get("locator") or {}, "score": score, "matched": matched,
    } for score, matched, chunk, _ in scored[:5]]
    return {"allowed": True, "member": row["member_name"], "library": row["library_name"],
            "live": str(version) == row["current_version"] and row["published"], "hits": hits}
