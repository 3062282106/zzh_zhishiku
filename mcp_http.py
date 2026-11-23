"""Local Streamable HTTP entry point for desktop knowledge connectors."""

import argparse
from contextvars import ContextVar
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp_server import create_server
import uvicorn
from starlette.requests import Request
from starlette.responses import Response


request_token = ContextVar("request_token")
mcp_app = create_server(request_token.get).streamable_http_app(
    stateless_http=True, json_response=True,
)


async def app(scope, receive, send):
    if scope["type"] != "http" or scope["path"] != "/mcp":
        await mcp_app(scope, receive, send)
        return

    headers = Request(scope).headers.getlist("authorization")
    credentials = headers[0].split() if len(headers) == 1 else []
    if len(credentials) != 2 or credentials[0].lower() != "bearer":
        await Response(status_code=401, headers={"WWW-Authenticate": "Bearer"})(scope, receive, send)
        return

    context = request_token.set(credentials[1])
    try:
        await mcp_app(scope, receive, send)
    finally:
        request_token.reset(context)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18082)
    uvicorn.run(app, host="127.0.0.1", port=parser.parse_args().port, proxy_headers=False, access_log=False)
