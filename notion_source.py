"""Read approved Notion pages and stage changed content for publication."""
from hashlib import sha256
import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID, uuid4

from psycopg.rows import dict_row

from admin import ManagementError, upload


NOTION_VERSION = "2026-03-11"
MAX_RESPONSE = 1024 * 1024
PAGE_ID = re.compile(r"(?:^|-)([0-9a-f]{32}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})$", re.I)


class SourceError(ManagementError):
    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


class RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = build_opener(RejectRedirects())


def notion_page_id(link):
    try:
        url = urlsplit(link)
        host = url.hostname or ""
        allowed = host in ("notion.so", "app.notion.com") or host.endswith((".notion.so", ".notion.site"))
        if url.scheme != "https" or not allowed or url.username or url.password or url.port:
            raise ValueError
        match = PAGE_ID.search(unquote(url.path.rstrip("/").rsplit("/", 1)[-1]))
        if not match:
            raise ValueError
        return str(UUID(match[1]))
    except ValueError:
        raise SourceError("请输入包含页面 ID 的 Notion HTTPS 链接。", 422) from None


def fetch_notion_markdown(page_id):
    token = os.environ.get("KB_NOTION_TOKEN", "").strip()
    if not token:
        raise SourceError("ECS 尚未配置 Notion 只读集成密钥。", 503)
    canonical_id = str(UUID(page_id))
    request = Request(f"https://api.notion.com/v1/pages/{canonical_id}/markdown?markdown_version=v2",
                      headers={"Authorization": f"Bearer {token}", "Notion-Version": NOTION_VERSION,
                               "Accept": "application/json"})
    try:
        with OPENER.open(request, timeout=20) as reply:
            raw = reply.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise SourceError("Notion 页面响应过大，请缩小同步范围。", 422)
            data = json.loads(raw)
    except HTTPError as error:
        if error.code == 401:
            raise SourceError("Notion 集成密钥无效，请在 ECS 检查配置。", 503) from None
        if error.code in (403, 404):
            raise SourceError("Notion 集成读不到此页。请检查页面连接与只读权限。", 409) from None
        if error.code == 429:
            raise SourceError("Notion 暂时限流，请稍后重试。", 503) from None
        raise SourceError(f"Notion 返回 HTTP {error.code}，本次未生成草稿。") from None
    except (URLError, TimeoutError):
        raise SourceError("ECS 暂时无法连接 Notion，请稍后重试。", 503) from None
    except (json.JSONDecodeError, UnicodeError):
        raise SourceError("Notion 返回无法解析的响应。") from None

    if not isinstance(data, dict) or data.get("object") != "page_markdown" or data.get("id") != canonical_id:
        raise SourceError("Notion 返回的页面标识与绑定页面不符。")
    if data.get("truncated") or data.get("unknown_block_ids"):
        raise SourceError("Notion 返回的页面不完整，请检查页面权限或缩小同步范围。", 409)
    markdown = data.get("markdown")
    if not isinstance(markdown, str) or not markdown.strip() or "\x00" in markdown:
        raise SourceError("Notion 页面没有可同步的正文。", 422)
    return markdown


def list_sources(db, library):
    with db.cursor(row_factory=dict_row) as cur:
        return cur.execute("""
            SELECT id, provider, source_url, title, document_id, last_version_id, checked_at, last_error, section_heading
            FROM managed_source WHERE library=%s ORDER BY title, id
        """, (library,)).fetchall()


def bind_notion(db, library, link, title):
    page = notion_page_id(link)
    url = "https://app.notion.com/p/" + UUID(page).hex
    return bind_source(db, library, "notion", page, url, title)


def bind_source(db, library, provider, external_id, url, title, heading=None):
    identifier, document = uuid4(), str(uuid4())
    with db.transaction():
        if not db.execute("SELECT 1 FROM knowledge_base WHERE id=%s", (library,)).fetchone():
            raise SourceError("找不到此知识库。", 404)
        db.execute("""
            INSERT INTO source_document (platform, document_id, kb_id, current_version, published)
            VALUES ('upload', %s, %s, '', false)
        """, (document, library))
        bound = db.execute("""
            INSERT INTO managed_source
                (id, library, document_id, provider, external_id, source_url, title, section_heading)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (library, provider, external_id) DO NOTHING RETURNING id
        """, (identifier, library, document, provider, external_id, url, title, heading)).fetchone()
        if not bound:
            raise SourceError("此来源已经绑定到当前知识库。", 409)
    return {"id": identifier, "document_id": document, "source_url": url}


def sync_source(db, identifier, fetch_feishu):
    with db.transaction(), db.cursor(row_factory=dict_row) as cur:
        source = cur.execute("""
            SELECT * FROM managed_source WHERE id=%s FOR UPDATE
        """, (identifier,)).fetchone()
        if not source:
            raise SourceError("找不到此在线来源。", 404)
        try:
            provider = source["provider"]
            if provider == "notion":
                markdown, line_blocks = fetch_notion_markdown(source["external_id"]), {}
            elif provider == "feishu":
                markdown, line_blocks = fetch_feishu(source["source_url"], source["section_heading"])
            else:
                raise SourceError("暂不支持此来源类型。", 422)
            markdown = markdown.replace("\r\n", "\n")
            snapshot = json.dumps([markdown, line_blocks], ensure_ascii=False, sort_keys=True) if line_blocks else markdown
            digest = sha256(snapshot.encode("utf-8")).hexdigest()
            changed = digest != source["last_digest"]
            version = source["last_version_id"]
            if changed:
                revision = upload(db, source["library"], source["document_id"], source["title"], provider + ".md",
                                  markdown, source_url=source["source_url"], provider=provider,
                                  line_blocks=line_blocks)
                version = revision["version_id"]
        except ManagementError as error:
            cur.execute("UPDATE managed_source SET checked_at=statement_timestamp(), last_error=%s WHERE id=%s",
                        (str(error), identifier))
            failure = error if isinstance(error, SourceError) else SourceError(str(error), 422)
        else:
            cur.execute("""
                UPDATE managed_source SET last_digest=%s, last_version_id=%s, checked_at=statement_timestamp(),
                    last_error=NULL WHERE id=%s
            """, (digest, version, identifier))
            failure = None
    if failure:
        raise failure
    return {"changed": changed, "version_id": version, "document_id": source["document_id"]}
