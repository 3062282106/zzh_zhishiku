"""Verify the local fixture through live PostgreSQL and OpenSearch."""

from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parent))
import access


def verify():
    tokens = access.fixture_tokens()
    question = "聚客宝放假期间可以在哪个端回复？"
    with access.connect() as connection:
        support = tokens["support-test"]
        operations = tokens["operations-test"]
        hits = access.authorized_query(connection, support, question)
        assert hits and "终端类型" in hits[0]["heading_path"]
        assert all(hit["platform"] == "feishu" for hit in hits)
        reference = hits[0]["chunk_id"]
        hits = access.authorized_query(connection, operations, question)
        assert all(hit["platform"] == "notion" for hit in hits)
        assert access.authorized_reference(connection, operations, reference) is None
        assert access.authorized_reference(connection, support, reference)

        operations_hits = access.authorized_query(connection, operations, "微商城的实物订单如何履约？")
        assert operations_hits and "四个环节" in operations_hits[0]["heading_path"]
        assert access.authorized_reference(connection, support, operations_hits[0]["chunk_id"]) is None

        # Separate database connections confirm that a committed change applies to the next call.
        with access.connect() as admin:
            row = admin.execute("""
                UPDATE knowledge_grant SET revoked_at = statement_timestamp()
                WHERE member_id = %s AND kb_id = %s AND revoked_at IS NULL RETURNING revoked_at
                """, ("support-test", "feishu")).fetchone()
            assert row, "Expected an active local fixture grant"
            try:
                assert access.authorized_query(connection, support, question) == []
                assert access.authorized_reference(connection, support, reference) is None
                access.seed(connection)
                assert access.authorized_query(connection, support, question) == []
            finally:
                admin.execute("""
                    UPDATE knowledge_grant SET revoked_at = NULL
                    WHERE member_id = %s AND kb_id = %s AND revoked_at = %s
                    """, ("support-test", "feishu", row[0]))
        assert access.authorized_reference(connection, support, reference)
        try:
            access.authorized_query(connection, "invalid-test-token", question)
        except PermissionError:
            pass
        else:
            raise AssertionError("Invalid token was accepted")
    print("PASS: own-library search, cross-library reference denial, immediate revocation, and invalid token rejection.")


if __name__ == "__main__":
    verify()
