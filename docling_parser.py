"""Docling Serve client and conversion into the knowledge evidence contract."""
from base64 import b64encode
from hashlib import sha256
from io import BytesIO
import json
from math import isfinite
import os
from pathlib import Path
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request
from zipfile import BadZipFile, ZipFile

from notion_source import OPENER


MAX_FILE = 20 * 1024 * 1024
MAX_RESULT = 32 * 1024 * 1024
FORMATS = {
    ".pdf": ("pdf", "application/pdf"),
    ".docx": ("docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    ".xlsx": ("xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    ".pptx": ("pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
    ".png": ("image", "image/png"),
    ".jpg": ("image", "image/jpeg"),
    ".jpeg": ("image", "image/jpeg"),
    ".tiff": ("image", "image/tiff"),
    ".tif": ("image", "image/tiff"),
}
OFFICE_ROOTS = {".docx": "word/document.xml", ".xlsx": "xl/workbook.xml", ".pptx": "ppt/presentation.xml"}
TASK_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
ITEM_REF = re.compile(r"#/([a-z_]+)/([0-9]{1,7})")


class ParseError(ValueError):
    def __init__(self, message, *, retryable=False, task_missing=False):
        super().__init__(message)
        self.retryable = retryable
        self.task_missing = task_missing


def validate_file(filename, data):
    ext = Path(filename).suffix.lower()
    if ext not in FORMATS or not data or len(data) > MAX_FILE:
        raise ParseError("请选择不超过 20 MiB 的 PDF、Word、Excel、PowerPoint 或扫描图片。")
    magic = {
        ".pdf": b"%PDF-", ".png": b"\x89PNG\r\n\x1a\n", ".jpg": b"\xff\xd8\xff", ".jpeg": b"\xff\xd8\xff",
    }
    if (ext in magic and not data.startswith(magic[ext])) or (
        ext in (".tif", ".tiff") and not data.startswith((b"II*\0", b"MM\0*"))
    ):
        raise ParseError("文件内容与扩展名不匹配。")
    if ext in OFFICE_ROOTS:
        try:
            with ZipFile(BytesIO(data)) as archive:
                entries = archive.infolist()
                if len(entries) > 4000 or sum(item.file_size for item in entries) > 256 * 1024 * 1024:
                    raise ParseError("Office 文件解压后超过处理上限。")
                if OFFICE_ROOTS[ext] not in archive.namelist() or any(
                    item.flag_bits & 1 or item.filename.lower().endswith("vbaproject.bin") for item in entries
                ):
                    raise ParseError("请选择匹配格式且未加密、未包含宏的 Office 文件。")
        except BadZipFile:
            raise ParseError("Office 文件无法读取。") from None
    return FORMATS[ext]


def parser_endpoint():
    url = os.environ.get("KB_DOCLING_URL", "").strip().rstrip("/")
    try:
        parts = urlsplit(url)
        loopback = parts.hostname in ("127.0.0.1", "::1", "localhost")
        if parts.port == 0:
            raise ValueError
        if not parts.hostname or parts.username or parts.password or parts.query or parts.fragment or parts.path:
            raise ValueError
        if parts.scheme != "https" and not (parts.scheme == "http" and loopback):
            raise ValueError
        if not loopback and not os.environ.get("KB_DOCLING_API_KEY", "").strip():
            raise ValueError
    except ValueError:
        raise ParseError("请配置可信 Docling 服务地址；远端服务使用 HTTPS 和 API Key。") from None
    return url


def request(path, payload=None):
    headers = {"Accept": "application/json"}
    key = os.environ.get("KB_DOCLING_API_KEY", "").strip()
    if key:
        headers["X-Api-Key"] = key
    body = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    outgoing = Request(parser_endpoint() + path, data=body, headers=headers)
    try:
        with OPENER.open(outgoing, timeout=25) as response:
            raw = response.read(MAX_RESULT + 1)
        if len(raw) > MAX_RESULT:
            raise ParseError("解析结果超过 32 MiB，请拆成多个文件。")
        result = json.loads(raw)
    except HTTPError as error:
        if error.code == 404 and payload is None:
            raise ParseError("Docling 已丢失任务，将重新提交原文件。", retryable=True, task_missing=True) from None
        raise ParseError(f"Docling 请求失败（HTTP {error.code}）。",
                         retryable=error.code == 429 or 500 <= error.code <= 599) from None
    except (URLError, TimeoutError, OSError):
        raise ParseError("暂时无法访问 Docling 服务。", retryable=True) from None
    except (json.JSONDecodeError, UnicodeError):
        raise ParseError("Docling 返回了无法读取的结果。") from None
    if not isinstance(result, dict):
        raise ParseError("Docling 返回的结果缺少任务结构。")
    return result


def submit(filename, data, format_name):
    needs_layout = format_name in ("pdf", "image")
    opts = {
        "from_formats": [format_name], "to_formats": ["json", "md"], "do_ocr": needs_layout,
        "do_table_structure": needs_layout, "image_export_mode": "placeholder",
        "abort_on_error": True, "document_timeout": 900,
    }
    if needs_layout:
        opts.update(ocr_preset="rapidocr", table_mode="accurate")
        languages = os.environ.get("KB_DOCLING_OCR_LANG", "ch").strip()
        if languages:
            opts["ocr_lang"] = [language.strip() for language in languages.split(",") if language.strip()]
    reply = request("/v1/chunk/hybrid/source/async", {
        "sources": [{"kind": "file", "filename": filename, "base64_string": b64encode(data).decode("ascii")}],
        "target": {"kind": "inbody"}, "include_converted_doc": True, "convert_options": opts,
        "chunking_options": {
            "chunker": "hybrid", "max_tokens": 384, "merge_peers": True,
            "tokenizer": os.environ.get("KB_DOCLING_TOKENIZER", "bert-base-multilingual-cased"),
            "include_raw_text": True, "use_markdown_tables": True,
        },
    })
    task = reply.get("task_id")
    if not isinstance(task, str) or not TASK_ID.fullmatch(task):
        raise ParseError("Docling 没有返回有效的任务 ID。")
    return task


def result(task):
    if not TASK_ID.fullmatch(task):
        raise ParseError("解析任务 ID 无效。")
    state = request(f"/v1/status/poll/{task}").get("task_status")
    if state in ("pending", "started", "running"):
        return None
    if state != "success":
        raise ParseError("Docling 未完成解析。请检查文件、解析服务或 OCR 设置后重试。")
    return request(f"/v1/result/{task}")


def item_at(document, ref):
    if not (isinstance(ref, str) and (match := ITEM_REF.fullmatch(ref))):
        return {}
    category, offset = match.groups()
    items = document.get(category)
    if not isinstance(items, list) or int(offset) >= len(items):
        return {}
    return items[int(offset)]


def worksheet_names(document, refs):
    names = []
    for ref in refs:
        for _ in range(20):
            item = item_at(document, ref)
            if not item:
                break
            if item.get("label") == "sheet" and isinstance(name := item.get("name"), str):
                if name not in names:
                    names.append(name)
                break
            ref = (item.get("parent") or {}).get("$ref")
    return names


def positions(document, refs):
    output = []
    for ref in refs:
        for cell in item_at(document, ref).get("prov", []):
            box = cell.get("bbox")
            page = cell.get("page_no")
            if type(page) is not int or page < 1 or not isinstance(box, dict):
                continue
            clean = {name: box.get(name) for name in ("l", "t", "r", "b")}
            if all(type(value) in (int, float) and isfinite(value) for value in clean.values()):
                output.append({"page": page, "bbox": clean, "origin": box.get("coord_origin"), "item": ref})
    return output


def evidence(payload, job):
    if payload.get("status") not in ("success", None) or payload.get("errors"):
        raise ParseError("解析结果含失败页面，请检查原件后重新解析。")
    documents = payload.get("documents")
    chunks = payload.get("chunks")
    if not isinstance(documents, list) or len(documents) != 1 or not isinstance(chunks, list):
        raise ParseError("Docling 没有返回完整的结构化文档和分块。")
    parsed = documents[0]
    if parsed.get("status") not in ("success", None) or parsed.get("errors"):
        raise ParseError("部分文档内容解析失败，草稿未生成。")
    content = parsed.get("content", {})
    document = content.get("json_content")
    if isinstance(document, str):
        try:
            document = json.loads(document)
        except json.JSONDecodeError:
            raise ParseError("Docling 文档结构无法读取。") from None
    if not isinstance(document, dict) or not 1 <= len(chunks) <= 2500:
        raise ParseError("解析缺少文档结构、没有可检索内容或超出 2500 个分块。")
    version = str(job["id"])
    source = f"/v1/knowledge/files/{version}"
    extension = Path(job["filename"]).suffix.lower()
    output = []
    for i, row in enumerate(chunks):
        if not isinstance(row, dict) or not isinstance(row.get("raw_text"), str) or not isinstance(row.get("text"), str):
            raise ParseError("Docling 分块缺少原文或检索文本。")
        raw = row["raw_text"].strip()
        contextualized = row["text"].strip()
        if not raw:
            continue
        if max(len(raw), len(contextualized)) > 20000:
            raise ParseError("单个分块过长，请检查表格或拆分原文件。")
        headings = [entry for entry in (row.get("headings") or []) if isinstance(entry, str)]
        pages = sorted({page for page in (row.get("page_numbers") or []) if type(page) is int and page > 0})
        refs = [entry for entry in (row.get("doc_items") or []) if isinstance(entry, str) and ITEM_REF.fullmatch(entry)]
        geometry = positions(document, refs)
        pages = sorted(set(pages) | {item["page"] for item in geometry})
        locator = {"pages": pages, "items": refs, "positions": geometry, "chunk_index": i}
        if extension in (".xlsx", ".pptx"):
            locator["position_unit"] = "worksheet_cell" if extension == ".xlsx" else "slide"
        if extension == ".xlsx":
            names = worksheet_names(document, refs)
            locator["worksheets"] = names
            added = [name for name in names if name not in headings]
            if added:
                headings = added + headings
                contextualized = "\n".join([*added, contextualized])
        chunk_id = sha256(f"{version}:{i}".encode()).hexdigest()
        reference = source + (f"#page={pages[0]}" if pages and extension == ".pdf" else "")
        output.append({
            "chunk_id": chunk_id, "platform": "upload", "document_id": job["document_id"],
            "document_version": version, "title": job["title"], "heading_path": headings, "text": raw,
            "retrieval_text": contextualized, "source_url": source, "reference_url": reference,
            "locator": locator, "metadata": {},
        })
    if not output:
        raise ParseError("文档没有可检索正文，请检查文件或 OCR 语言。")
    original = content.get("md_content") or "\n\n".join(item["text"] for item in output)
    return original, document, output
