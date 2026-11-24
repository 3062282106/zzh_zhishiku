"""Local MCP adapter for the enterprise knowledge HTTP service."""

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
MAX_ATTACHMENT_BASE64 = 4 * ((5 * 1024 * 1024 + 2) // 3)


class RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = build_opener(ProxyHandler({}), RejectRedirects())


def request(path, token, body=None):
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json", "X-KB-Channel": "mcp"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    try:
        with OPENER.open(Request(BASE_URL + path, data=data, headers=headers), timeout=50) as response:
            return json.load(response)
    except HTTPError as error:
        if path.startswith("/v1/knowledge/imports/"):
            try:
                detail = json.load(error).get("detail")
            except (OSError, ValueError):
                detail = None
            if isinstance(detail, str):
                raise ToolError(detail) from None
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

    @server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False,
                                             idempotent_hint=True, open_world_hint=True))
    def import_feishu_document(
        url: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2048)],
        library: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)] | None = None,
        section_heading: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)] | None = None,
    ) -> CallToolResult:
        """仅在用户明确要求入库时使用。个人知识库导入后自动发布，企业知识库导入为草稿。
        普通成员留空 library 或填写「个人知识库」。有授权的管理员必须明确指定企业库名称或 ID。
        未提供章节时读取整篇。authorization_required 时请把网页授权链接交给用户，授权后重试。
        processing 时用 get_feishu_import_status 和返回的 job_id 查询进度。
        published 表示已可检索。publication_failed 表示个人文档已导入但索引未完成，可重新导入重试。"""
        return tool_result(request("/v1/knowledge/imports/feishu", get_token(), {
            "url": url, "library": library, "section_heading": section_heading,
        }))

    @server.tool(annotations=read_only)
    def get_feishu_import_status(
        job_id: Annotated[str, StringConstraints(pattern=r"^[a-f0-9-]{36}$")],
    ) -> CallToolResult:
        """查询自己提交的飞书导入任务。processing 表示后台正在处理。发布完成后可检索个人内容。"""
        return tool_result(request(f"/v1/knowledge/imports/feishu/{job_id}", get_token()))

    @server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False,
                                             idempotent_hint=False, open_world_hint=True))
    def upload_knowledge_file(
        filename: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)],
        content_base64: Annotated[str, StringConstraints(min_length=1, max_length=MAX_ATTACHMENT_BASE64)],
        library: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)] | None = None,
        title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)] | None = None,
    ) -> CallToolResult:
        """根据用户明确请求上传实际附件。支持 PDF、DOCX、XLSX、CSV、PPTX、图片、Markdown 和 TXT，最大 5 MiB。
        content_base64 必须由客户端读取附件原始字节并编码。个人库默认，企业库须指定已获授权的名称或 ID。
        返回任务字段 id，用 get_knowledge_file_status 查询。个人库解析后自动发布，企业库生成草稿。"""
        return tool_result(request("/v1/knowledge/imports/files", get_token(), {
            "filename": filename, "content_base64": content_base64, "library": library, "title": title,
        }))

    @server.tool(annotations=read_only)
    def get_knowledge_file_status(
        job_id: Annotated[str, StringConstraints(pattern=r"^[a-f0-9-]{36}$")],
    ) -> CallToolResult:
        """检查本次附件上传的解析与发布状态。个人库 publication_status 为 published 后可以检索。
        企业库 status 为 ready 时草稿已生成。"""
        return tool_result(request(f"/v1/knowledge/imports/files/{job_id}", get_token()))

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
