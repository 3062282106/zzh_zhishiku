"""Read-only local MCP adapter for the enterprise knowledge HTTP service."""

import argparse
import json
from pathlib import Path
import site
import sys
from typing import Annotated
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


ROOT = Path(__file__).resolve().parent
packages = ROOT / ".local" / "python"
if packages.is_dir():
    site.addsitedir(str(packages))

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field, StringConstraints


BASE_URL = "http://127.0.0.1:18081"
TOKEN_FILE = ROOT / ".local" / "test-accounts.json"


class RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = build_opener(ProxyHandler({}), RejectRedirects())


def request(path, token, body=None):
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    try:
        with OPENER.open(Request(BASE_URL + path, data=data, headers=headers), timeout=50) as response:
            return json.load(response)
    except HTTPError as error:
        if error.code == 401:
            raise ToolError("本地知识库身份无效。") from None
        if error.code == 404:
            raise ToolError("证据不存在或当前身份无权读取。") from None
        raise ToolError(f"知识库接口调用失败（HTTP {error.code}）。") from None
    except (URLError, OSError, ValueError):
        raise ToolError("知识库服务不可用。请检查本机 18081 服务。") from None


def create_server(get_token):
    server = MCPServer("enterprise-knowledge-local")
    read_only = ToolAnnotations(read_only_hint=True)

    def tool_result(data):
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(data, ensure_ascii=False))],
            structured_content=data,
        )

    @server.tool(annotations=read_only)
    def search_knowledge(
        query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1024)],
        limit: Annotated[int, Field(ge=1, le=20)] = 5,
        filters: Annotated[dict[str, str | list[str]] | None, Field(
            description="可选的已配置元数据字段及精确值。不同字段同时满足，同字段列表匹配任一值。"
        )] = None,
    ) -> CallToolResult:
        """搜索授权资料。insufficient_evidence 表示没有符合条件的证据。逐项核对原文能否支持答案。"""
        payload = {"query": query, "limit": limit, "filters": filters or {}}
        return tool_result(request("/v1/knowledge/search", get_token(), payload))

    @server.tool(annotations=read_only)
    def get_knowledge_reference(
        chunk_id: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")],
    ) -> CallToolResult:
        """通过搜索结果里的 chunk_id 重新读取证据。每次调用都检查当前权限。"""
        return tool_result(request(f"/v1/knowledge/references/{chunk_id}", get_token()))

    return server


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", required=True, choices=["operations-test", "support-test"])
    args = parser.parse_args()
    try:
        token = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))[args.account]
        if not isinstance(token, str) or not token:
            raise ValueError
    except (OSError, KeyError, ValueError):
        sys.exit("Unable to load the selected local knowledge test identity.")
    create_server(lambda: token).run(transport="stdio")
