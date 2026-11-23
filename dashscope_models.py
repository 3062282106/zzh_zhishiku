"""DashScope HTTP adapters for the two selected retrieval models."""

import json
import math
import os
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


BASE_URL = "https://ws-kfqieybqhjlwk9wr.cn-beijing.maas.aliyuncs.com/api/v1"
EMBEDDING_MODEL = "qwen3.7-text-embedding-flash"
RERANK_MODEL = "qwen3.7-text-rerank"
DIMENSION = 1024
BATCH_SIZE = 20


class ModelError(ValueError):
    pass


class RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = build_opener(RejectRedirects())


def post(path, body):
    key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    if not key:
        raise ModelError("DASHSCOPE_API_KEY is not configured.")
    request = Request(
        BASE_URL + path,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with OPENER.open(request, timeout=20) as response:
            return json.load(response)["output"]
    except HTTPError as exc:
        raise ModelError(f"DashScope returned HTTP {exc.code}.") from None
    except (URLError, TimeoutError):
        raise ModelError("DashScope API did not respond.") from None
    except (ValueError, KeyError, TypeError):
        raise ModelError("DashScope returned an unexpected response.") from None


def embed(texts, text_type):
    if not 1 <= len(texts) <= BATCH_SIZE:
        raise ModelError("Embedding batch must contain 1 to 20 texts.")
    output = post("/services/embeddings/text-embedding/text-embedding", {
        "model": EMBEDDING_MODEL,
        "input": {"texts": texts},
        "parameters": {"text_type": text_type},
    })
    try:
        indexed = [(row.get("index", row.get("text_index")), row) for row in output["embeddings"]]
        if any(type(index) is not int for index, _ in indexed):
            raise ValueError
        indexed.sort(key=lambda item: item[0])
        if [index for index, _ in indexed] != list(range(len(texts))):
            raise ValueError
        vectors = [row["embedding"] for _, row in indexed]
        if any(len(vector) != DIMENSION or not all(map(math.isfinite, vector))
               or not any(vector) for vector in vectors):
            raise ValueError
        return vectors
    except (KeyError, TypeError, ValueError):
        raise ModelError("DashScope returned invalid embedding vectors.") from None


def rerank(query, documents, limit):
    if not documents:
        return []
    output = post("/services/rerank/text-rerank/text-rerank", {
        "model": RERANK_MODEL,
        "input": {"query": query, "documents": documents},
        "parameters": {"top_n": min(limit, len(documents)), "return_documents": False},
    })
    try:
        results = [(row["index"], row["relevance_score"]) for row in output["results"]]
        ids = [index for index, _ in results]
        if (len(results) != min(limit, len(documents)) or len(ids) != len(set(ids))
                or any(type(i) is not int or not 0 <= i < len(documents) for i in ids)
                or any(not math.isfinite(score) for _, score in results)):
            raise ValueError
        return sorted(results, key=lambda result: result[1], reverse=True)
    except (KeyError, TypeError, ValueError):
        raise ModelError("DashScope returned invalid rerank results.") from None
