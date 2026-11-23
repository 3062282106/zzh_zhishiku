"""Compare three retrieval modes with authorized, labeled local questions."""

import argparse
import getpass
import json
import os
from pathlib import Path
from statistics import median
import sys
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parent))

from access import (
    authorized_fusion_candidates, authorized_query, authorized_reference,
    authorized_rerank, connect, fixture_tokens,
)
from dashscope_models import ModelError


LIMIT = 5
MODES = ("keyword", "hybrid_rrf", "hybrid_rerank")


def measure(hits, case, started):
    elapsed_ms = round((perf_counter() - started) * 1000, 1)
    ids = [hit["chunk_id"] for hit in hits]
    relevant = set(case["relevant_ids"])
    rank = next((index for index, chunk_id in enumerate(ids, 1) if chunk_id in relevant), None)
    distractors = set(case.get("distractor_ids", []))
    distractor_rank = next((index for index, chunk_id in enumerate(ids, 1) if chunk_id in distractors), None)
    return {"rank": rank, "distractor_rank": distractor_rank, "ms": elapsed_ms, "top_ids": ids}


def compare(connection, token, case):
    for chunk_id in case["relevant_ids"]:
        if authorized_reference(connection, token, chunk_id) is None:
            raise ValueError(f"{case['id']}: labeled reference is missing or not authorized")
    question = case["query"]

    started = perf_counter()
    keyword = measure(authorized_query(connection, token, question, LIMIT), case, started)

    started = perf_counter()
    fused = authorized_fusion_candidates(connection, token, question, allow_fallback=False)
    hybrid = measure(fused[:LIMIT], case, started)
    ranked = authorized_rerank(connection, token, question, fused, LIMIT, allow_fallback=False)
    return {**case, "keyword": keyword, "hybrid_rrf": hybrid,
            "hybrid_rerank": measure(ranked, case, started)}


def summarize(rows):
    ranks = [row["rank"] for row in rows]
    return {"hit_at_1": ranks.count(1), "hit_at_5": sum(rank is not None for rank in ranks),
            "mrr_at_5": round(sum(1 / rank for rank in ranks if rank) / len(rows), 3),
            "median_ms": median(row["ms"] for row in rows)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cases", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    if not cases:
        parser.error("Provide at least one labeled question.")

    if not os.environ.get("DASHSCOPE_API_KEY", "").strip():
        os.environ["DASHSCOPE_API_KEY"] = getpass.getpass("DashScope API key: ").strip()
    tokens = fixture_tokens()
    with connect() as connection:
        results = [compare(connection, tokens[case["account"]], case) for case in cases]
    summary = {mode: summarize([case[mode] for case in results]) for mode in MODES}
    args.output.write_text(json.dumps({"cases": results, "summary": summary}, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    try:
        main()
    except ModelError as exc:
        sys.exit(f"Evaluation stopped: {exc}")
