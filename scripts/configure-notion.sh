#!/usr/bin/env bash
set -euo pipefail
if [[ "$(id -u)" != 0 || ( $# != 1 && $# != 3 ) ]]; then
  echo '用法：以 root 运行 bash configure-notion.sh <Notion 页面链接> [<知识库 ID> <标题>]' >&2
  exit 1
fi

cd /opt/knowledge-kb
.venv/bin/python - "$1" <<'PY'
import getpass
import os
from pathlib import Path
import re
import sys
import tempfile

from notion_source import SourceError, fetch_notion_markdown, notion_page_id

try:
    identifier = notion_page_id(sys.argv[1])
    key = getpass.getpass("Notion 只读集成密钥（输入不会显示）：").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,512}", key):
        raise SourceError("密钥为空或格式不正确。")
    os.environ["KB_NOTION_TOKEN"] = key
    fetch_notion_markdown(identifier)
except SourceError as error:
    sys.exit(str(error))

config = Path("/etc/knowledge-kb/api.env")
before = config.read_text(encoding="utf-8").splitlines()
after = [line for line in before if not line.startswith("KB_NOTION_TOKEN=")]
with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=config.parent, delete=False) as file:
    temp = Path(file.name)
    file.write("\n".join([*after, "KB_NOTION_TOKEN=" + key]) + "\n")
try:
    os.replace(temp, config)
finally:
    temp.unlink(missing_ok=True)
print("PASS：Notion API 已读取获授权页面。")
PY
systemctl restart knowledge-kb-api.service knowledge-kb-mcp.service
curl -fsS --retry 10 --retry-connrefused --retry-delay 1 http://127.0.0.1:18081/healthz
printf '\n'

if [[ $# == 3 ]]; then
  .venv/bin/python - "$@" <<'PY'
import json
from pathlib import Path
import sys
from urllib.request import Request, urlopen
from uuid import UUID

from notion_source import notion_page_id

page, library, title = sys.argv[1:]
canonical = "https://app.notion.com/p/" + UUID(notion_page_id(page)).hex
config = Path("/etc/knowledge-kb/api.env").read_text(encoding="utf-8")
token = next(line.split("=", 1)[1] for line in config.splitlines() if line.startswith("KB_ADMIN_TOKEN="))

def call(path, method="GET", data=None):
    body = json.dumps(data).encode() if data is not None else None
    request = Request("http://127.0.0.1:18081/v1/admin" + path, body, {
        "Authorization": "Bearer " + token, "Content-Type": "application/json",
    }, method=method)
    with urlopen(request, timeout=40) as response:
        return json.load(response)

known = call("/libraries")
if not any(item["id"] == library for item in known):
    sys.exit("Notion 授权已验证，但找不到指定的目标知识库。请在后台中手动添加来源。")
path = "/libraries/" + library + "/sources"
source = next((row for row in call(path) if row["source_url"] == canonical), None)
if source is None:
    source = call(path + "/notion", "POST", {"title": title, "url": canonical})
outcome = call("/sources/" + source["id"] + "/sync", "POST")
print("PASS：已生成 Notion 草稿。" if outcome["changed"] else "PASS：Notion 内容未变化，已复用现有版本。")
PY
fi
