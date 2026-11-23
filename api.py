"""Local HTTP entry point for the enterprise knowledge base."""

import argparse
import logging
import os
from pathlib import Path
import sys
from typing import Annotated, Literal
from urllib.error import URLError


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
local_packages = ROOT / ".local" / "python"
if local_packages.is_dir():
    sys.path.insert(0, str(local_packages))

from fastapi import Depends, FastAPI, HTTPException, Path as PathParameter, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
import psycopg
from pydantic import BaseModel, Field, StringConstraints, field_validator

from access import authorized_hybrid_query, authorized_query, authorized_reference, connect
from metadata import normalize


app = FastAPI(title="企业知识库", version="0.1.0")
bearer = HTTPBearer(auto_error=False)
log = logging.getLogger(__name__)
HYBRID_ENABLED = os.environ.get("KB_ENABLE_HYBRID") == "1"

from admin_api import router as admin_router
app.include_router(admin_router(HYBRID_ENABLED))


class SearchRequest(BaseModel):
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1024)]
    limit: int = Field(default=5, ge=1, le=20)
    filters: dict[str, str | list[str]] = Field(default_factory=dict)

    @field_validator("filters")
    @classmethod
    def validate_filters(cls, value):
        return normalize(value)


class Evidence(BaseModel):
    chunk_id: str
    title: str
    platform: str
    heading_path: list[str]
    snippet: str
    document_version: str
    reference_path: str
    source_url: str
    locator: dict
    metadata: dict[str, list[str]] = Field(default_factory=dict)


class SearchResponse(BaseModel):
    status: Literal["evidence_found", "insufficient_evidence"]
    answer_guidance: str
    evidence: list[Evidence]


def unauthorized():
    return HTTPException(status_code=401, detail="Valid credentials required",
                         headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"})


def token_from_request(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
    if credentials is None or not credentials.credentials or len(credentials.credentials) > 256:
        raise unauthorized()
    return credentials.credentials


def evidence_from_hit(hit):
    chunk_id = hit["chunk_id"]
    return Evidence(
        chunk_id=chunk_id, title=hit["title"], platform=hit["platform"],
        heading_path=hit["heading_path"], snippet=hit["text"],
        document_version=str(hit["document_version"]),
        reference_path=f"/v1/knowledge/references/{chunk_id}",
        source_url=hit["reference_url"], locator=hit["locator"], metadata=hit.get("metadata", {}),
    )


def unavailable(error):
    # Log only the exception class. Request URLs or exception bodies can contain data.
    log.warning("Knowledge backend unavailable: %s", type(error).__name__)
    return HTTPException(status_code=503, detail="Knowledge service unavailable",
                         headers={"Cache-Control": "no-store"})


@app.get("/healthz")
def health():
    return {"status": "ok"}


@app.post("/v1/knowledge/search", response_model=SearchResponse)
def query(payload: SearchRequest, response: Response, token: Annotated[str, Depends(token_from_request)]):
    response.headers["Cache-Control"] = "no-store"
    try:
        with connect() as connection:
            retrieve = authorized_hybrid_query if HYBRID_ENABLED else authorized_query
            hits = retrieve(connection, token, payload.query, payload.limit, filters=payload.filters)
            if not hits:
                return SearchResponse(status="insufficient_evidence", evidence=[],
                                      answer_guidance="没有检索到符合条件的授权证据。请说明资料不足和仍需确认的信息。")
            return SearchResponse(status="evidence_found", evidence=[evidence_from_hit(hit) for hit in hits],
                                  answer_guidance="逐项核对证据是否支持所问条件。说明有依据的部分和证据缺口，并引用原文。")
    except PermissionError:
        raise unauthorized() from None
    except (psycopg.Error, OSError, URLError, ValueError, KeyError) as error:
        raise unavailable(error) from None


@app.get("/v1/knowledge/references/{chunk_id}", response_model=Evidence)
def reference(
    chunk_id: Annotated[str, PathParameter(pattern=r"^[a-f0-9]{64}$")],
    response: Response,
    token: Annotated[str, Depends(token_from_request)],
):
    response.headers["Cache-Control"] = "no-store"
    try:
        with connect() as connection:
            hit = authorized_reference(connection, token, chunk_id)
    except PermissionError:
        raise unauthorized() from None
    except (psycopg.Error, OSError, URLError, ValueError, KeyError) as error:
        raise unavailable(error) from None
    if hit is None:
        # Use an indistinguishable response for missing and inaccessible evidence.
        raise HTTPException(status_code=404, detail="Evidence not found",
                            headers={"Cache-Control": "no-store"})
    return evidence_from_hit(hit)


if __name__ == "__main__":
    import uvicorn

    if HYBRID_ENABLED and not os.environ.get("DASHSCOPE_API_KEY", "").strip():
        sys.exit("Set DASHSCOPE_API_KEY before enabling hybrid retrieval.")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18080)
    uvicorn.run(app, host="127.0.0.1", port=parser.parse_args().port, access_log=False, proxy_headers=False)

