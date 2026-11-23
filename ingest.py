"""Turn private Notion and Feishu snapshots into traceable JSONL chunks."""

import argparse
from dataclasses import dataclass
import hashlib
import html
import json
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import quote
from xml.etree import ElementTree as ET
sys.path.insert(0, str(Path(__file__).resolve().parent))
from metadata import normalize


DEFAULT_ROOT = Path(__file__).resolve().parent / ".local"
CHUNK_CHAR_LIMIT = 1000
NOTION_HEADING = re.compile(r"^\s*(#{1,6})\s+(.+)$")
TABLE_SEPARATOR = re.compile(r":?-{3,}:?")
FEISHU_HEADING = re.compile(r"h([1-6])")
NOTION_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")


@dataclass(frozen=True)
class Unit:
    heading: tuple[str, ...]
    text: str
    locator: dict
    cells: tuple[tuple[str, str], ...] = ()


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def update_heading(stack, level, title):
    while stack and stack[-1][0] >= level:
        stack.pop()
    stack.append((level, title))
    return tuple(name for _, name in stack)


def markdown_text(value):
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
    value = re.sub(r"</?(?:b|i|s|u|span|code|callout|details|summary)\b[^>]*>", "", value)
    value = NOTION_LINK.sub(r"\1 (\2)", value)
    return html.unescape(value).strip()


def markdown_table_cells(line):
    line = line.strip()
    if not line.startswith("|") or len(line) < 2:
        return []
    cells = re.split(r"(?<!\\)\|", line[1:])
    if cells[-1] == "" and line.endswith("|") and not line.endswith(r"\|"):
        cells.pop()
    return [markdown_text(cell.replace(r"\|", "|")) for cell in cells]


def table_row_text(headers, row):
    if len(row) == len(headers) and all(headers):
        return " | ".join(f"{name}: {cell}" for name, cell in zip(headers, row))
    return "表头: " + " | ".join(headers) + "\n记录: " + " | ".join(row)


def table_cells(headers, row):
    if len(headers) != len(row) or len(set(headers)) != len(headers) or not all(headers):
        return ()
    return tuple(zip(headers, row))


def notion_units(markdown):
    stack, lines = [], []
    start = 0
    raw_lines = [*markdown.splitlines(), ""]
    next_line = 1
    for number, raw in enumerate(raw_lines, 1):
        if number < next_line:
            continue
        found = NOTION_HEADING.match(raw)
        headers = markdown_table_cells(raw)
        separator = markdown_table_cells(raw_lines[number]) if headers and number < len(raw_lines) else []
        table = (bool(headers) and all(headers) and len(headers) == len(separator)
                 and all(TABLE_SEPARATOR.fullmatch(cell) for cell in separator))
        barrier = table or not raw.strip() or found or raw.lstrip().startswith(("<image ", "<file ", "<icon"))
        if barrier and lines:
            yield Unit(tuple(name for _, name in stack), "\n".join(lines), {
                "line_start": start, "line_end": number - 1,
            })
            lines = []
        if table:
            heading = tuple(name for _, name in stack)
            next_line = number + 2
            while next_line <= len(raw_lines):
                row = markdown_table_cells(raw_lines[next_line - 1])
                if not row:
                    break
                yield Unit(heading, table_row_text(headers, row), {
                    "line_start": next_line, "line_end": next_line, "table_header_line": number,
                }, table_cells(headers, row))
                next_line += 1
            if next_line == number + 2:
                yield Unit(heading, " | ".join(headers), {"line_start": number, "line_end": number})
        elif found:
            update_heading(stack, len(found[1]), markdown_text(found[2]))
        elif not barrier:
            cleaned = markdown_text(raw)
            if cleaned:
                if not lines:
                    start = number
                lines.append(cleaned)


def node_text(node):
    def collect(element):
        if element.tag in {"br", "hr"}:
            return "\n"
        parts = [element.text or ""]
        for child in element:
            if child.tag in {"p", "li", "tr", "ol", "ul"}:
                parts.append("\n")
            parts.append(collect(child))
            parts.append(child.tail or "")
        if element.tag == "a" and element.attrib.get("href", "").startswith(("https://", "http://")):
            parts.append(f' ({element.attrib["href"]})')
        return "".join(parts)

    return "\n".join(line.strip() for line in collect(node).splitlines() if line.strip())


def feishu_units(snapshot):
    for section in snapshot["sections"]:
        xml = section["xml"]
        if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", xml, re.I):
            raise ValueError("Feishu XML contains an unsupported declaration")
        root = ET.fromstring(xml)
        anchor = section["anchor"]
        if root.tag != "fragment" or root.attrib.get("requested-start") != anchor:
            raise ValueError(f"Feishu section anchor does not match {anchor}")
        stack = []
        heading = ()
        for ordinal, block in enumerate(root, 1):
            tag = block.tag
            block_id = block.attrib.get("id")
            if tag in {"hr", "img", "image", "whiteboard", "file"} or tag.startswith("image."):
                continue
            if match := FEISHU_HEADING.fullmatch(tag):
                title = node_text(block)
                if title:
                    heading = update_heading(stack, int(match[1]), title)
                continue
            locator = {"section_block_id": anchor, "block_id": block_id or anchor, "xml_block_number": ordinal}
            if tag == "table":
                rows = [[node_text(cell) for cell in row] for row in block.iter("tr")]
                if rows:
                    headers = rows[0]
                    for number, row in enumerate(rows[1:], 2):
                        yield Unit(heading, table_row_text(headers, row), {**locator, "table_xml_row": number},
                                   table_cells(headers, row))
                    if len(rows) == 1:
                        yield Unit(heading, " | ".join(headers), {**locator, "table_xml_row": 1})
                    continue
            value = node_text(block)
            if value:
                yield Unit(heading, value, locator)


def split_text(value):
    while len(value) > CHUNK_CHAR_LIMIT:
        window = value[:CHUNK_CHAR_LIMIT]
        cut = max(window.rfind(separator) for separator in "\n。！？； ")
        cut = cut + 1 if cut >= CHUNK_CHAR_LIMIT // 2 else CHUNK_CHAR_LIMIT
        yield value[:cut].strip()
        value = value[cut:].lstrip()
    if value:
        yield value


def source_chunks(source, root, fields=None):
    snapshot = read_json(root / source["file"])
    if snapshot["id"] != source["id"]:
        raise ValueError(f'Snapshot ID does not match source {source["title"]}')
    if source["platform"] == "notion":
        version = snapshot["page_last_edited_at"]
        units = notion_units(snapshot["markdown"])
    elif source["platform"] == "feishu":
        version = snapshot["revision_id"]
        if [s["anchor"] for s in snapshot["sections"]] != source["sections"]:
            raise ValueError(f'Snapshot sections do not match source {source["title"]}')
        units = feishu_units(snapshot)
    else:
        raise ValueError(f'Unknown source platform {source["platform"]}')
    if version is None or version == "":
        raise ValueError(f'Missing version for source {source["title"]}')

    columns = normalize((fields or {}).get("columns"))
    source_metadata = normalize(source.get("metadata"))
    sections = [(tuple(rule["heading_path"]), normalize(rule["metadata"]))
                for rule in source.get("section_metadata", ())]
    count = 0
    for unit in units:
        values = dict(source_metadata)
        for heading, section_values in sections:
            if unit.heading[:len(heading)] == heading:
                values.update(section_values)
        cells = dict(unit.cells)
        for key, names in columns.items():
            matches = [cells[name] for name in names if name in cells and cells[name].strip()]
            if matches:
                values.update(normalize({key: matches}))
        for part_number, part in enumerate(split_text(unit.text), 1):
            key = [source["platform"], source["id"], version, unit.locator, part_number]
            chunk_id = hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()
            url = source["url"]
            block_id = unit.locator.get("block_id")
            if block_id:
                url = f'{url.split("#", 1)[0]}#{quote(block_id, safe="")}'
            count += 1
            yield {
                "chunk_id": chunk_id,
                "platform": source["platform"],
                "document_id": source["id"],
                "document_version": version,
                "title": source["title"],
                "heading_path": list(unit.heading),
                "text": part,
                "source_url": source["url"],
                "reference_url": url,
                "locator": {**unit.locator, "part": part_number},
                **({"metadata": values} if values else {}),
            }
    if count == 0:
        raise ValueError(f'No text chunks for source {source["title"]}')


def build(root):
    manifest = read_json(root / "sources.json")
    output = root / "chunks.jsonl"
    counts = {}
    seen = set()
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=root, delete=False) as stream:
            tmp = Path(stream.name)
            for source in manifest["sources"]:
                profiles = manifest.get("metadata_fields", {})
                profile = source.get("metadata_profile")
                fields = profiles[profile] if profile is not None else profiles.get(source["platform"])
                for chunk in source_chunks(source, root, fields):
                    if chunk["chunk_id"] in seen:
                        raise ValueError(f'Duplicate chunk in source {source["title"]}')
                    seen.add(chunk["chunk_id"])
                    stream.write(json.dumps(chunk, ensure_ascii=False) + "\n")
                    platform = chunk["platform"]
                    counts[platform] = counts.get(platform, 0) + 1
            if not seen:
                raise ValueError("No sources configured")
        tmp.replace(output)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
    print(json.dumps({"chunks": counts, "total": len(seen)}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="Private source directory")
    try:
        build(parser.parse_args().root)
    except (OSError, ValueError, KeyError, ET.ParseError) as exc:
        print(f"Import failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
