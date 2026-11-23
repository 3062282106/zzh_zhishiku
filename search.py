"""Load the current private snapshot into local OpenSearch and run keyword searches."""

import argparse
import hashlib
import json
from pathlib import Path
import sys
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parent))
from metadata import normalize, term


BASE_URL = "http://127.0.0.1:19200"
DEFAULT_CHUNKS = Path(__file__).resolve().parent / ".local" / "chunks.jsonl"
ALIAS = "kb-local-chunks"
HYBRID_ALIAS = "kb-local-hybrid"
OPENER = build_opener(ProxyHandler({}))
TEXT = {"type": "text", "analyzer": "cjk"}


def request(method, path, body=None):
    is_ndjson = isinstance(body, bytes)
    if body is not None and not is_ndjson:
        body = json.dumps(body, ensure_ascii=False).encode("utf-8")
    content_type = "application/x-ndjson" if is_ndjson else "application/json"
    req = Request(BASE_URL + path, data=body, headers={"Content-Type": content_type}, method=method)
    with OPENER.open(req, timeout=30) as response:
        return json.load(response)


def index_snapshot(path, models=None, *, managed=False):
    raw = path.read_bytes()
    chunks = [json.loads(line) for line in raw.splitlines() if line.strip()]
    if not chunks:
        raise ValueError("Run ingest.py first. No chunks to index.")
    ids = [chunk["chunk_id"] for chunk in chunks]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate chunk IDs in snapshot")
    if request("GET", "/")["cluster_name"] != "kb-local":
        raise ValueError("Unexpected cluster at the local development endpoint")

    alias = HYBRID_ALIAS if models else ALIAS
    if managed:
        alias += "-managed"
    settings = {"number_of_shards": 1, "number_of_replicas": 0}
    fields = {"title": TEXT, "heading_path": TEXT, "text": TEXT, "locator": {"enabled": False},
              "metadata": {"enabled": False}, "metadata_terms": {"type": "keyword"}}
    identity = raw
    if any(chunk.get("retrieval_text") for chunk in chunks):
        fields["retrieval_text"] = TEXT
        identity += b"\0retrieval-text-cjk"
    if models:
        settings["index.knn"] = True
        fields["embedding"] = {
            "type": "knn_vector", "dimension": models.DIMENSION,
            "method": {"name": "hnsw", "engine": "lucene", "space_type": "cosinesimil"},
        }
        identity += f"\0{models.BASE_URL}/{models.EMBEDDING_MODEL}/{models.DIMENSION}".encode()
    index = f"{alias}-{hashlib.sha256(identity).hexdigest()[:16]}"
    try:
        request("PUT", f"/{index}", {
            "settings": settings, "mappings": {"properties": fields},
        })
    except HTTPError as exc:
        if exc.code != 400 or json.load(exc).get("error", {}).get("type") != "resource_already_exists_exception":
            raise
    complete = request("GET", f"/{index}/_count")["count"] == len(chunks)
    batch_size = models.BATCH_SIZE if models else 200
    offsets = range(0) if complete else range(0, len(chunks), batch_size)
    for start in offsets:
        batch = chunks[start:start + batch_size]
        if models:
            vectors = models.embed([passage(chunk) for chunk in batch], "document")
            for chunk, vector in zip(batch, vectors):
                chunk["embedding"] = vector
        lines = []
        for chunk in batch:
            chunk["metadata_terms"] = [term(key, value) for key, values in normalize(chunk.get("metadata")).items()
                                       for value in values]
            lines.extend(({"index": {"_id": chunk["chunk_id"]}}, chunk))
        payload = ("\n".join(json.dumps(line, ensure_ascii=False) for line in lines) + "\n").encode("utf-8")
        result = request("POST", f"/{index}/_bulk?refresh=wait_for", payload)
        if result["errors"]:
            failed = sum("error" in item["index"] for item in result["items"])
            raise ValueError(f"OpenSearch rejected {failed} chunks")
    if request("GET", f"/{index}/_count")["count"] != len(chunks):
        raise ValueError("Indexed document count does not match the snapshot")
    if managed:
        return index

    # Only switch visible data after the new snapshot has been fully indexed.
    try:
        current = request("GET", f"/_alias/{alias}")
    except HTTPError as exc:
        if exc.code != 404:
            raise
        current = {}
    if list(current) != [index]:
        actions = [{"remove": {"index": name, "alias": alias}} for name in current]
        actions.append({"add": {"index": index, "alias": alias}})
        request("POST", "/_aliases", {"actions": actions})
    print(f"Indexed {len(chunks)} chunks in {index}.")


def passage(chunk):
    if chunk.get("retrieval_text"):
        return "\n".join([chunk["title"], chunk["retrieval_text"]])
    return "\n".join([chunk["title"], " / ".join(chunk["heading_path"]), chunk["text"]])


def search_path(alias):
    return f"/{alias},{alias}-managed-*/_search?ignore_unavailable=true"


def exact_filters(allowed_ids, filters):
    return [{"ids": {"values": sorted(allowed_ids)}}, *[
        {"terms": {"metadata_terms": [term(key, value) for value in values]}}
        for key, values in normalize(filters).items()
    ]]


def vector_search(vector, limit, allowed_ids, filters=None):
    if not allowed_ids:
        return []
    result = request("POST", search_path(HYBRID_ALIAS), {
        "size": limit,
        "_source": {"excludes": ["embedding", "metadata_terms"]},
        "query": {"knn": {"embedding": {
            "vector": vector, "k": limit, "filter": {"bool": {"filter": exact_filters(allowed_ids, filters)}},
        }}},
    })
    return [{"score": hit["_score"], **hit["_source"]} for hit in result["hits"]["hits"]]


def search(query, limit, allowed_ids, filters=None):
    if not allowed_ids:
        return []
    result = request("POST", search_path(ALIAS), {
        "size": limit,
        "_source": {"excludes": ["metadata_terms"]},
        "query": {"bool": {
            "must": [{"multi_match": {
                "query": query, "fields": ["heading_path^2", "title", "text", "retrieval_text^0.75"],
            }}],
            "filter": exact_filters(allowed_ids, filters),
        }},
    })
    return [{"score": hit["_score"], **hit["_source"]} for hit in result["hits"]["hits"]]


def lookup(chunk_id, allowed_ids):
    if chunk_id not in allowed_ids:
        return None
    result = request("POST", search_path(ALIAS), {
        "size": 1,
        "_source": {"excludes": ["metadata_terms"]},
        "query": {"ids": {"values": [chunk_id]}},
    })
    hits = result["hits"]["hits"]
    return hits[0]["_source"] if hits else None


def main():
    parser = argparse.ArgumentParser(description="Index the approved local source snapshot.")
    commands = parser.add_subparsers(dest="command", required=True)
    loader = commands.add_parser("index")
    loader.add_argument("--chunks", type=Path, default=DEFAULT_CHUNKS)
    loader.add_argument("--hybrid", action="store_true", help="Send approved slice text to DashScope and build vector index")
    args = parser.parse_args()
    try:
        if args.hybrid:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            import dashscope_models
            index_snapshot(args.chunks, dashscope_models)
        else:
            index_snapshot(args.chunks)
    except (OSError, ValueError, KeyError, URLError) as exc:
        print(f"Local OpenSearch request failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
