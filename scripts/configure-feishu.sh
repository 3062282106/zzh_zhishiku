#!/usr/bin/env bash
set -euo pipefail
if [[ "$(id -u)" != 0 || $# != 2 ]]; then
  echo '用法：以 root 运行 bash configure-feishu.sh <飞书文档链接> <完整章节标题>' >&2
  exit 1
fi
cd /opt/knowledge-kb
.venv/bin/python - "$@" <<'PY'
import getpass
import os
from pathlib import Path
import re
import sys
import tempfile

from feishu_source import SourceError, fetch_feishu_section

values = {}
for name, label in (("KB_FEISHU_APP_ID", "飞书 App ID"), ("KB_FEISHU_APP_SECRET", "飞书 App Secret")):
    value = getpass.getpass(label + "（输入不会显示）：").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,512}", value):
        sys.exit(label + " 为空或格式有误。")
    os.environ[name] = values[name] = value

try:
    markdown, _ = fetch_feishu_section(sys.argv[1], sys.argv[2])
    if not markdown.strip():
        raise SourceError("指定章节没有可同步内容。")
except SourceError as error:
    sys.exit(str(error))

config = Path("/etc/knowledge-kb/api.env")
before = config.read_text(encoding="utf-8").splitlines()
after = [line for line in before if line.split("=", 1)[0] not in values]
with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=config.parent, delete=False) as file:
    temp = Path(file.name)
    file.write("\n".join([*after, *(f"{key}={value}" for key, value in values.items())]) + "\n")
try:
    os.replace(temp, config)
finally:
    temp.unlink(missing_ok=True)
print("PASS：飞书应用能读取指定章节。")
PY
systemctl restart knowledge-kb-api.service knowledge-kb-mcp.service
curl -fsS --retry 10 --retry-connrefused --retry-delay 1 http://127.0.0.1:18081/healthz
printf '\n'
