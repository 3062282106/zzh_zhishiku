"""Verify deployment through the running admin API and MCP over loopback."""
import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

API = "http://127.0.0.1:18081"
MCP = "http://127.0.0.1:18082/mcp"
CLOUD = Path("/opt/knowledge-kb")
ENV = Path("/etc/knowledge-kb/api.env")


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def request(url, token, method="GET", payload=None, content_type="application/json", expected=200):
    if payload is not None and not isinstance(payload, bytes):
        payload = json.dumps(payload).encode()
    headers = {"Authorization": "Bearer " + token, "Content-Type": content_type,
               "Accept": "application/json, text/event-stream"}
    try:
        with urlopen(Request(url, payload, headers, method=method), timeout=120) as response:
            require(response.status == expected, f"Expected status {expected}, received {response.status}")
            return response.read()
    except HTTPError as error:
        if error.code != expected:
            raise RuntimeError(f"{method} {url} returned HTTP {error.code}: {error.read(500).decode(errors='replace')}") from None


def upload(library, admin, title, text, document=None):
    boundary = uuid4().hex
    fields = [("title", title)]
    if document:
        fields.append(("document_id", document))
    body = "".join(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
                   for name, value in fields)
    body += (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="qa.md"\r\n'
             f'Content-Type: text/markdown\r\n\r\n{text}\r\n--{boundary}--\r\n')
    return json.loads(request(library + "/documents", admin, "POST", body.encode(),
                              f"multipart/form-data; boundary={boundary}", 201))


def mcp_call(token, name, arguments):
    reply = json.loads(request(MCP, token, "POST", {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }))
    result = reply.get("result", {})
    require(not result.get("isError") and "structuredContent" in result, f"MCP tool failed: {name}")
    return result["structuredContent"]


def verify(admin, tokens):
    run_id = uuid4().hex[:10]
    title = "部署测试手册 " + run_id
    libraries = API + "/v1/admin/libraries"
    library = json.loads(request(libraries, admin, "POST", {"name": "云端发布验收 " + run_id}, expected=201))
    library_url = libraries + "/" + library["id"]
    operator = tokens["operations-test"]
    support = tokens["support-test"]

    def grant(enabled):
        request(library_url + "/grant", admin, "PUT", {"member": "operations-test", "enabled": enabled})

    def query(token, document):
        result = mcp_call(token, "search_knowledge", {"query": title + " 设备归还到哪里？", "limit": 20})
        prefix = "/v1/knowledge/originals/" + document + "/"
        return [hit for hit in result["evidence"] if hit["source_url"].startswith(prefix)]

    def expect(reference, label):
        hits = query(operator, document)
        require(len(hits) == 1 and label in hits[0]["snippet"], "查询返回的版本不正确。")
        require(hits[0]["document_version"] == reference["version_id"], "返回版本号不正确。")
        return hits[0]

    def activate(revision, action, current):
        url = versions + revision["version_id"] + "/" + action
        request(url, admin, "POST", {"expected": current})

    grant(True)
    try:
        v1 = upload(library_url, admin, title, "## 设备归还\n\n测试设备归还到一楼服务台。")
        document = v1["document_id"]
        versions = API + "/v1/admin/documents/" + document + "/versions/"
        preview = json.loads(request(versions + v1["version_id"], admin))
        require("一楼服务台" in preview["chunks"][0]["text"], "分块预览不符。")
        require(not query(operator, document), "草稿出现在员工检索中。")
        activate(v1, "publish", "")
        first = expect(v1, "一楼服务台")
        request(API + first["source_url"], operator)
        request(API + first["source_url"], support, expected=404)

        v2 = upload(library_url, admin, title, "## 设备归还\n\n测试设备归还到二楼服务台。", document)
        expect(v1, "一楼服务台")
        activate(v2, "publish", v1["version_id"])
        expect(v2, "二楼服务台")
        request(API + first["reference_path"], operator, expected=404)
        request(API + first["source_url"], operator, expected=404)
        require(not query(support, document), "未授权身份查到了测试库。")

        activate(v1, "rollback", v2["version_id"])
        restored = expect(v1, "一楼服务台")
        require(restored["chunk_id"] == first["chunk_id"], "回滚后的证据编号不符。")
        reference = mcp_call(operator, "get_knowledge_reference", {"chunk_id": first["chunk_id"]})
        require("一楼服务台" in reference["snippet"], "MCP 回读内容不符。")
    finally:
        grant(False)
    print("PASS：云端草稿隔离、分块预览、一楼 → 二楼 → 一楼、引用回读及跨库拒绝。")
    print("测试库的运营测试访问权限已撤销。编号：" + run_id)


if __name__ == "__main__":
    env = dict(line.split("=", 1) for line in ENV.read_text().splitlines()
               if line and not line.startswith("#") and "=" in line)
    tokens = json.loads((CLOUD / ".local" / "test-accounts.json").read_text())
    verify(env["KB_ADMIN_TOKEN"], tokens)
