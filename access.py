"""Manage the local access fixture and query OpenSearch through PostgreSQL permissions."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys

ROOT = Path(__file__).resolve().parent
PRIVATE = ROOT / ".local"
# The local Windows Python can be isolated and ignore PYTHONPATH.
sys.path.insert(0, str(ROOT))
local_packages = PRIVATE / "python"
if local_packages.is_dir():
    sys.path.insert(0, str(local_packages))

import psycopg

import dashscope_models
from search import DEFAULT_CHUNKS, lookup, passage, search, vector_search

ACCOUNTS = PRIVATE / "test-accounts.json"
ACCOUNTS_TO_LIBRARIES = {"operations-test": "notion", "support-test": "feishu"}
VISIBLE = """
    SELECT c.chunk_id
    FROM member_account m
    JOIN knowledge_grant g ON g.member_id = m.id
    JOIN source_document d ON d.kb_id = g.kb_id
    JOIN source_chunk c ON c.platform = d.platform
         AND c.document_id = d.document_id AND c.version = d.current_version
    WHERE m.token_hash = %s AND m.enabled
      AND g.revoked_at IS NULL AND (g.expires_at IS NULL OR g.expires_at > statement_timestamp())
      AND d.published
      AND (d.valid_from IS NULL OR d.valid_from <= statement_timestamp())
      AND (d.valid_until IS NULL OR d.valid_until > statement_timestamp())
"""


def connect():
    password = (PRIVATE / "postgres-password.txt").read_text(encoding="utf-8").strip()
    return psycopg.connect(host="127.0.0.1", port=int(os.environ.get("KB_DB_PORT", "15432")),
                           dbname=os.environ.get("KB_DB_NAME", "postgres"), user="kb_local",
                           password=password, connect_timeout=4, autocommit=True)


def token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def fixture_tokens():
    return json.loads(ACCOUNTS.read_text(encoding="utf-8"))


def seed(connection):
    chunks = [json.loads(line) for line in DEFAULT_CHUNKS.read_text(encoding="utf-8").splitlines() if line]
    documents = {}
    for chunk in chunks:
        key = (chunk["platform"], chunk["document_id"])
        if key[0] not in ACCOUNTS_TO_LIBRARIES.values():
            raise ValueError("Unrecognized fixture knowledge base")
        version = str(chunk["document_version"])
        if documents.setdefault(key, version) != version:
            raise ValueError("The fixture contains more than one version of a document")
    if not chunks:
        raise ValueError("No chunks in local snapshot")
    if not ACCOUNTS.exists():
        tokens = {name: secrets.token_urlsafe(32) for name in ACCOUNTS_TO_LIBRARIES}
        with ACCOUNTS.open("x", encoding="utf-8") as stream:
            json.dump(tokens, stream)
    tokens = fixture_tokens()

    with connection.transaction(), connection.cursor() as cursor:
        cursor.execute((ROOT / "schema.sql").read_text(encoding="utf-8"))
        cursor.executemany("INSERT INTO knowledge_base (id, name) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                           [("notion", "Notion 运营样本"), ("feishu", "飞书客服样本")])
        for account, library in ACCOUNTS_TO_LIBRARIES.items():
            cursor.execute("""
                INSERT INTO member_account (id, token_hash) VALUES (%s, %s)
                ON CONFLICT (id) DO UPDATE SET token_hash = EXCLUDED.token_hash
                """, (account, token_hash(tokens[account])))
            cursor.execute("""
                INSERT INTO knowledge_grant (member_id, kb_id) VALUES (%s, %s) ON CONFLICT DO NOTHING
                """, (account, library))
        for (platform, document_id), version in documents.items():
            cursor.execute("""
                INSERT INTO source_document (platform, document_id, kb_id, current_version)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (platform, document_id) DO UPDATE SET current_version = EXCLUDED.current_version
                """, (platform, document_id, platform, version))
        cursor.executemany("""
            INSERT INTO source_chunk (chunk_id, platform, document_id, version)
            VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING
            """, [(chunk["chunk_id"], chunk["platform"], chunk["document_id"],
                   str(chunk["document_version"])) for chunk in chunks])
    print(f"Registered {len(chunks)} chunks, {len(documents)} documents and 2 local test accounts.")


def authorized_ids(connection, token, subset=None):
    digest = token_hash(token)
    if subset is None:
        if connection.execute("SELECT 1 FROM member_account WHERE token_hash = %s AND enabled",
                              (digest,)).fetchone() is None:
            raise PermissionError("Invalid or disabled local test account")
        statement, parameters = VISIBLE, (digest,)
    else:
        statement, parameters = VISIBLE + " AND c.chunk_id = ANY(%s)", (digest, list(subset))
    return {row[0] for row in connection.execute(statement, parameters).fetchall()}


def authorized_query(connection, token, question, limit=5, *, filters=None):
    allowed = authorized_ids(connection, token)
    hits = search(question, limit, allowed, filters)
    if not hits:
        return []
    still_allowed = authorized_ids(connection, token, [hit["chunk_id"] for hit in hits])
    return [hit for hit in hits if hit["chunk_id"] in still_allowed]


def authorized_fusion_candidates(connection, token, question, *, allow_fallback=True, filters=None):
    allowed = authorized_ids(connection, token)
    if not allowed:
        return []
    lexical = search(question, 50, allowed, filters)
    try:
        vector = dashscope_models.embed([question], "query")[0]
        semantic = vector_search(vector, 50, allowed, filters)
    except (dashscope_models.ModelError, OSError):
        if not allow_fallback:
            raise
        print("Vector retrieval unavailable; using authorized keyword results.", file=sys.stderr)
        semantic = []

    scores, candidates = {}, {}
    for ranking in (lexical, semantic):
        for rank, row in enumerate(ranking, 1):
            chunk_id = row["chunk_id"]
            candidates.setdefault(chunk_id, row)
            scores[chunk_id] = scores.get(chunk_id, 0) + 1 / (60 + rank)
    if not candidates:
        return []
    permitted = authorized_ids(connection, token, candidates)
    combined = sorted(permitted, key=lambda chunk_id: (-scores[chunk_id], chunk_id))[:30]
    return [{**candidates[chunk_id], "rrf_score": scores[chunk_id]} for chunk_id in combined]


def authorized_rerank(connection, token, question, hits, limit, *, allow_fallback=True):
    if not hits:
        return []
    try:
        ranked = dashscope_models.rerank(question, [passage(hit) for hit in hits], limit)
        hits = [{**hits[index], "rerank_score": score} for index, score in ranked]
    except dashscope_models.ModelError:
        if not allow_fallback:
            raise
        print("Rerank unavailable; using authorized fusion results.", file=sys.stderr)
        hits = hits[:limit]
    permitted = authorized_ids(connection, token, [hit["chunk_id"] for hit in hits])
    return [hit for hit in hits if hit["chunk_id"] in permitted]


def authorized_hybrid_query(connection, token, question, limit=5, *, filters=None):
    hits = authorized_fusion_candidates(connection, token, question, filters=filters)
    return authorized_rerank(connection, token, question, hits, limit)


def authorized_reference(connection, token, chunk_id):
    allowed = authorized_ids(connection, token)
    if chunk_id not in allowed:
        return None
    hit = lookup(chunk_id, allowed)
    if hit is None or not authorized_ids(connection, token, [chunk_id]):
        return None
    return hit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("seed")
    query = commands.add_parser("query")
    query.add_argument("question")
    query.add_argument("--limit", type=int, default=5)
    query.add_argument("--hybrid", action="store_true", help="Use DashScope embedding and rerank")
    reference = commands.add_parser("reference")
    reference.add_argument("chunk_id")
    for command in (query, reference):
        command.add_argument("--account", required=True, choices=ACCOUNTS_TO_LIBRARIES)
    args = parser.parse_args()

    try:
        with connect() as connection:
            if args.command == "seed":
                seed(connection)
                return 0
            token = fixture_tokens()[args.account]
            if args.command == "query":
                if not args.question.strip() or not 1 <= args.limit <= 20:
                    parser.error("Provide a question and a limit from 1 to 20.")
                run_query = authorized_hybrid_query if args.hybrid else authorized_query
                result = run_query(connection, token, args.question, args.limit)
            else:
                result = authorized_reference(connection, token, args.chunk_id)
            print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, ValueError, KeyError, psycopg.Error) as exc:
        print(f"Local access request failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
