"""Read whole Feishu Docx documents or selected sections."""
import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request

from notion_source import MAX_RESPONSE, OPENER, SourceError, bind_source


API = "https://open.feishu.cn/open-apis"
TOKEN = re.compile(r"[A-Za-z0-9]{8,80}")
HEADER = re.compile(r"heading([1-9])")
TEXT_KEYS = ("text", "bullet", "ordered", "code", "quote", "todo")


def parse_link(link):
    try:
        url = urlsplit(link)
        host = url.hostname or ""
        if url.scheme != "https" or not host.endswith(".feishu.cn") or url.username or url.password or url.port:
            raise ValueError
        _, kind, token = url.path.rstrip("/").split("/")
        if kind not in ("wiki", "docx") or not TOKEN.fullmatch(token):
            raise ValueError
    except ValueError:
        raise SourceError("请输入飞书 Wiki 或新版云文档的 HTTPS 链接。", 422) from None
    return kind, token, f"https://{host}/{kind}/{token}"


def bind_feishu(db, library, link, title, heading):
    kind, token, canonical = parse_link(link)
    return bind_source(db, library, "feishu", kind + ":" + token, canonical, title, heading)


def call(path, token=None, body=None, *, origin=API):
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = Request(origin + path, json.dumps(body).encode() if body is not None else None, headers)
    try:
        with OPENER.open(request, timeout=18) as response:
            raw = response.read(MAX_RESPONSE + 1)
        if len(raw) > MAX_RESPONSE:
            raise SourceError("飞书返回的数据过大，请缩小来源范围。", 422)
        payload = json.loads(raw)
    except HTTPError as error:
        if error.code in (401, 403, 404):
            raise SourceError("飞书拒绝访问，请检查当前飞书账号的文档权限或应用授权。", 409) from None
        raise SourceError(f"飞书请求失败（HTTP {error.code}），本次没有同步。") from None
    except (URLError, TimeoutError, UnicodeError, json.JSONDecodeError):
        raise SourceError("暂时无法读取飞书，请稍后重试。", 503) from None
    if not isinstance(payload, dict):
        raise SourceError("飞书返回了无法识别的响应。")
    code = payload.get("code")
    if type(code) is not int:
        raise SourceError("飞书返回了无法识别的状态。")
    if code:
        raise SourceError(f"飞书拒绝了请求（错误码 {code}）。请检查应用是否发布、权限是否生效及文档是否已授权。", 409)
    return payload


def credential():
    app_id = os.environ.get("KB_FEISHU_APP_ID", "").strip()
    secret = os.environ.get("KB_FEISHU_APP_SECRET", "").strip()
    if not app_id or not secret:
        raise SourceError("ECS 尚未配置飞书 App ID 和 App Secret。", 503)
    token = call("/auth/v3/tenant_access_token/internal", body={"app_id": app_id, "app_secret": secret})
    value = token.get("tenant_access_token")
    if not isinstance(value, str) or not value:
        raise SourceError("飞书没有返回有效的应用身份凭证。")
    return value


def get_document_id(kind, token, access_token):
    if kind == "docx":
        return token
    payload = call("/wiki/v2/spaces/get_node?" + urlencode({"token": token}), access_token)
    node = payload.get("data", {}).get("node", {})
    if node.get("obj_type") != "docx":
        raise SourceError("这个 Wiki 节点没有指向新版飞书云文档。", 422)
    candidate = node.get("obj_token", "")
    if not isinstance(candidate, str) or not TOKEN.fullmatch(candidate):
        raise SourceError("飞书返回的云文档 ID 无效。")
    return candidate


def list_blocks(document_id, revision, access_token):
    blocks, seen = {}, set()
    params = {"page_size": 200, "document_revision_id": revision}
    for _ in range(12):
        result = call(f"/docx/v1/documents/{document_id}/blocks?" + urlencode(params), access_token).get("data", {})
        items = result.get("items")
        if not isinstance(items, list):
            raise SourceError("飞书云文档块返回格式不完整。")
        for item in items:
            block_id = item.get("block_id", "")
            if not TOKEN.fullmatch(block_id) or block_id in blocks:
                raise SourceError("飞书返回了无效或重复的文档块。")
            blocks[block_id] = item
        if not result.get("has_more"):
            return blocks
        marker = result.get("page_token")
        if not isinstance(marker, str) or not marker or marker in seen:
            raise SourceError("飞书云文档分页结果不完整。")
        seen.add(marker)
        params["page_token"] = marker
    raise SourceError("飞书文档块数超过当前同步上限，请使用较小的同步文档。", 422)


def heading(block):
    for field in block:
        if match := HEADER.fullmatch(field):
            return int(match[1]), text(block[field])
    return None


def text(value):
    return "".join(element.get("text_run", {}).get("content", "")
                   for element in value.get("elements", []) if isinstance(element, dict)).strip()


def child_ids(block):
    result = block.get("children", [])
    if not isinstance(result, list) or any(not isinstance(item, str) for item in result):
        raise SourceError("飞书返回了不完整的文档结构。")
    return result


def render_section(blocks, root, selected):
    if root not in blocks:
        raise SourceError("飞书结果缺少文档顶层结构。")
    try:
        top = [blocks[key] for key in child_ids(blocks[root])]
    except KeyError:
        raise SourceError("飞书结果缺少部分文档块。") from None
    start, end = 0, len(top)
    if selected is not None:
        matches = [(index, name[0]) for index, block in enumerate(top)
                   if (name := heading(block)) and name[1] == selected]
        if len(matches) != 1:
            raise SourceError("找不到唯一匹配的顶层标题，请复制完整章节标题。", 422)
        start, level = matches[0]
        end = next((index for index in range(start + 1, len(top))
                    if (name := heading(top[index])) and name[0] <= level), len(top))

    lines, line_blocks = [], {}
    visiting = set()

    def emit(value, block_id):
        for piece in value.splitlines():
            if piece:
                line_blocks[len(lines) + 1] = block_id
            lines.append(piece)

    def table_cell(cell_id):
        if cell_id not in blocks:
            raise SourceError("飞书表格缺少单元格，无法确认行列对应关系。")
        try:
            values = [text(blocks[key].get("text", {})) for key in child_ids(blocks[cell_id])]
        except KeyError:
            raise SourceError("飞书表格单元格不完整。") from None
        return " ".join(" ".join(values).split()).replace("|", r"\|")

    def render(block):
        block_id = block["block_id"]
        if block_id in visiting:
            raise SourceError("飞书文档块包含循环引用。")
        visiting.add(block_id)
        name = heading(block)
        if name:
            emit("#" * min(name[0], 6) + " " + name[1], block_id)
        elif "table" in block:
            table = block["table"]
            sizes = table.get("property", {})
            width, height = sizes.get("column_size"), sizes.get("row_size")
            cells = table.get("cells") or child_ids(block)
            if (type(width) is not int or type(height) is not int or min(width, height) <= 0
                    or not isinstance(cells, list) or height * width != len(cells)):
                raise SourceError("飞书表格行列信息不完整。")
            if sizes.get("header_row") is False:
                emit("| " + " | ".join(f"第 {column} 列" for column in range(1, width + 1)) + " |", block_id)
                emit("| " + " | ".join(["---"] * width) + " |", block_id)
            for row in range(height):
                values = [table_cell(key) for key in cells[row * width:(row + 1) * width]]
                emit("| " + " | ".join(values) + " |", block_id)
                if row == 0 and sizes.get("header_row") is not False:
                    emit("| " + " | ".join(["---"] * width) + " |", block_id)
            lines.append("")
            visiting.remove(block_id)
            return
        else:
            for field in TEXT_KEYS:
                if field in block and (value := text(block[field])):
                    emit(value, block_id)
                    break
        lines.append("")
        for key in child_ids(block):
            if key not in blocks:
                raise SourceError("飞书结果缺少子文档块。")
            render(blocks[key])
        visiting.remove(block_id)

    for block in top[start:end]:
        render(block)
    return "\n".join(lines), line_blocks


def fetch_feishu_document(link, selected, access_token):
    kind, token, _ = parse_link(link)
    document_id = get_document_id(kind, token, access_token)
    document = call(f"/docx/v1/documents/{document_id}", access_token).get("data", {}).get("document", {})
    revision = document.get("revision_id")
    if type(revision) is not int or revision < 1:
        raise SourceError("飞书没有返回可校验的文档版本。")
    markdown, line_blocks = render_section(list_blocks(document_id, revision, access_token), document_id, selected)
    title = document.get("title")
    title = title.strip()[:160] if isinstance(title, str) else ""
    return document_id, title or "飞书云文档", markdown, line_blocks


def fetch_feishu_section(link, selected):
    _, _, markdown, line_blocks = fetch_feishu_document(link, selected, credential())
    return markdown, line_blocks
