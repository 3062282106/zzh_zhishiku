"""Verify retrieval boundaries against isolated local campaign fixtures."""

import json
from contextlib import nullcontext
from pathlib import Path
import secrets
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import access
import api
import ingest
import search


TABLE = r"""## 测试活动

| 渠道 | SKU | 门槛 | 例外 |
| --- | --- | --- | --- |
| 自营旗舰店 | 00123 | 满 299 元 | 跨境店除外 |
| 门店 | 00123 | 满 399 元 | |
| 自营旗舰店 | 01230 | 满 599 元 | 仅会员 \| 单笔 |
"""
QUESTION = "自营旗舰店 SKU 00123 满减活动"


class FixtureModels:
    BASE_URL = "fixture"
    EMBEDDING_MODEL = "fixture"
    DIMENSION = 4
    BATCH_SIZE = 20

    @staticmethod
    def embed(texts, text_type):
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]


def verify_tables():
    rows = list(ingest.notion_units(TABLE))
    assert len(rows) == 3
    assert rows[0].text == "渠道: 自营旗舰店 | SKU: 00123 | 门槛: 满 299 元 | 例外: 跨境店除外"
    assert rows[1].text == "渠道: 门店 | SKU: 00123 | 门槛: 满 399 元 | 例外: "
    assert rows[2].text.endswith("例外: 仅会员 | 单笔")
    assert rows[0].locator == {"line_start": 5, "line_end": 5, "table_header_line": 3}
    xml = ('<fragment requested-start="anchor"><table id="tbl">'
           '<tr><th>渠道</th><th>SKU</th></tr><tr><td>自营旗舰店</td><td>00123</td></tr>'
           '</table></fragment>')
    row, = ingest.feishu_units({"sections": [{"anchor": "anchor", "xml": xml}]})
    assert row.text == "渠道: 自营旗舰店 | SKU: 00123"
    assert row.locator["block_id"] == "tbl" and row.locator["table_xml_row"] == 2
    return rows


def verify_indexed_rules(connection, token, expected, denied):
    def rerank(question, documents, limit):
        assert all("已过期旧规则" not in text for text in documents)
        return [(index, 1.0) for index in range(min(len(documents), limit))]

    with patch.object(access.dashscope_models, "rerank", rerank):
        keyword = access.authorized_query(connection, token, QUESTION)
        fused = access.authorized_fusion_candidates(connection, token, QUESTION, allow_fallback=False)
        ranked = access.authorized_rerank(connection, token, QUESTION, fused, 5, allow_fallback=False)
    for hits in (keyword, fused, ranked):
        ids = {hit["chunk_id"] for hit in hits}
        assert expected <= ids and not (denied & ids)
    for chunk_id in expected:
        assert access.authorized_reference(connection, token, chunk_id)
    for chunk_id in denied:
        assert access.authorized_reference(connection, token, chunk_id) is None


def verify_lifecycle(connection, fixture_chunks):
    token = secrets.token_urlsafe(32)
    schema = (access.ROOT / "schema.sql").read_text(encoding="utf-8")
    connection.execute("SET LOCAL search_path TO pg_temp, public")
    connection.execute(schema.replace("CREATE TABLE", "CREATE TEMP TABLE"))
    connection.execute("INSERT INTO knowledge_base VALUES ('qa', 'QA')")
    connection.execute("INSERT INTO member_account (id, token_hash) VALUES ('qa', %s)",
                       (access.token_hash(token),))
    connection.execute("INSERT INTO knowledge_grant (member_id, kb_id) VALUES ('qa', 'qa')")
    connection.execute("INSERT INTO source_document VALUES ('notion', 'campaign', 'qa', 'v1', true, NULL, NULL)")
    chunks = []
    for version in ("v1", "v2"):
        for row in fixture_chunks:
            chunk = {
                **row, "chunk_id": secrets.token_hex(32), "document_version": version,
            }
            if version == "v1":
                chunk["text"] = f"已过期旧规则 | {row['text'].replace('299', '199')}"
            chunks.append(chunk)
            connection.execute("INSERT INTO source_chunk VALUES (%s, 'notion', 'campaign', %s)",
                               (chunk["chunk_id"], version))

    first = {chunk["chunk_id"] for chunk in chunks if chunk["document_version"] == "v1"}
    second = {chunk["chunk_id"] for chunk in chunks if chunk["document_version"] == "v2"}
    sample = next(source for source in ingest.read_json(ingest.DEFAULT_ROOT / "sources.json")["sources"]
                  if source["platform"] == "feishu")
    sample["section_metadata"] = [
        {"heading_path": ["终端类型"], "metadata": {"terminal": ["desktop", "mobile"]}},
    ]
    terminal = next(chunk for chunk in ingest.source_chunks(sample, ingest.DEFAULT_ROOT) if chunk.get("metadata"))
    terminal_id = terminal["chunk_id"]
    assert terminal["metadata"] == {"terminal": ["desktop", "mobile"]}
    # Keep the manual in a separate library to verify filters cannot expand a caller's access.
    connection.execute("INSERT INTO knowledge_base VALUES ('manual', 'Manual')")
    connection.execute(
        "INSERT INTO source_document VALUES ('feishu', %s, 'manual', %s, true, NULL, NULL)",
        (terminal["document_id"], str(terminal["document_version"])),
    )
    connection.execute("INSERT INTO source_chunk VALUES (%s, 'feishu', %s, %s)",
                       (terminal_id, terminal["document_id"], str(terminal["document_version"])))
    prefix = "kb-test-edges-" + secrets.token_hex(6)
    with TemporaryDirectory() as folder, patch.object(search, "ALIAS", prefix + "-lex"), \
            patch.object(search, "HYBRID_ALIAS", prefix + "-vec"), \
            patch.object(access.dashscope_models, "embed", FixtureModels.embed):
        snapshot = Path(folder) / "chunks.jsonl"
        snapshot.write_text("\n".join(json.dumps(chunk, ensure_ascii=False) for chunk in [*chunks, terminal]),
                            encoding="utf-8")
        try:
            search.index_snapshot(snapshot)
            search.index_snapshot(snapshot, FixtureModels)
            baseline = access.authorized_query(connection, token, QUESTION)
            assert {hit["chunk_id"] for hit in baseline} == first
            assert all(access.authorized_reference(connection, token, item) is None for item in second)

            connection.execute("UPDATE source_document SET current_version = 'v2' WHERE document_id = 'campaign'")
            verify_indexed_rules(connection, token, second, first)

            for change in (
                "valid_until = statement_timestamp()",
                "valid_until = NULL, valid_from = statement_timestamp() + INTERVAL '1 day'",
                "valid_from = NULL, published = false",
            ):
                connection.execute(f"UPDATE source_document SET {change} WHERE document_id = 'campaign'")
                verify_indexed_rules(connection, token, set(), first | second)

            connection.execute("UPDATE source_document SET published = true WHERE document_id = 'campaign'")
            verify_indexed_rules(connection, token, second, first)
            verify_metadata(connection, token, chunks[3]["chunk_id"], terminal_id)
        finally:
            for alias in (search.ALIAS, search.HYBRID_ALIAS):
                indexes = search.request("GET", f"/_cat/indices/{alias}-*?format=json&h=index")
                for entry in indexes:
                    name = entry["index"]
                    if name.startswith(alias + "-"):
                        search.request("DELETE", f"/{name}")


def verify_metadata(connection, token, storefront_id, terminal_id):
    from starlette.responses import Response

    filters = {"sku": "00123", "channel": "自营旗舰店"}
    assert len(access.authorized_query(connection, token, QUESTION)) == 3
    for hits in (
        access.authorized_query(connection, token, QUESTION, filters=filters),
        access.authorized_fusion_candidates(connection, token, QUESTION, allow_fallback=False, filters=filters),
        search.vector_search(FixtureModels.embed([QUESTION], "query")[0], 5,
                             access.authorized_ids(connection, token), filters),
    ):
        assert [hit["chunk_id"] for hit in hits] == [storefront_id]

    question = "放假期间在哪个端回复"
    terminal_filter = {"terminal": ["mobile", "tablet"]}
    assert access.authorized_query(connection, token, question, filters=terminal_filter) == []
    connection.execute("INSERT INTO knowledge_grant (member_id, kb_id) VALUES ('qa', 'manual')")
    with patch.object(api, "connect", lambda: nullcontext(connection)), patch.object(api, "HYBRID_ENABLED", False):
        result = api.query(api.SearchRequest(query=question, filters=terminal_filter), Response(), token)
        assert result.status == "evidence_found" and [hit.chunk_id for hit in result.evidence] == [terminal_id]
        missing = api.query(api.SearchRequest(query=QUESTION, filters={"sku": "123"}), Response(), token)
        assert missing.status == "insufficient_evidence" and not missing.evidence and missing.answer_guidance


def main():
    verify_tables()
    with TemporaryDirectory() as folder:
        root = Path(folder)
        (root / "campaign.json").write_text(
            json.dumps({"id": "campaign", "page_last_edited_at": "v2", "markdown": TABLE}), encoding="utf-8",
        )
        source = {"platform": "notion", "id": "campaign", "title": "满减活动", "url": "https://example.test",
                  "file": "campaign.json"}
        chunks = list(ingest.source_chunks(source, root, {"columns": {"sku": "SKU", "channel": "渠道"}}))
        assert chunks[0]["metadata"] == {"sku": ["00123"], "channel": ["自营旗舰店"]}
    with access.connect() as connection, connection.transaction(force_rollback=True):
        verify_lifecycle(connection, chunks)
    print("PASS: Notion and Feishu row preservation; active version only; effective dates; takedown; reference checks.")
    print("PASS: generic exact filters, OR within one field, AND across fields, authorization, and API result status.")


if __name__ == "__main__":
    main()
