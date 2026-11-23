#!/usr/bin/env bash
set -euo pipefail

app=/opt/knowledge-kb
settings=/etc/knowledge-kb/parser.env
[[ $(id -u) == 0 && -x "$app/.venv/bin/python" && -f "$app/file_ingest.py" && -s /etc/knowledge-kb/api.env ]] ||
  { echo '先以 root 部署新版后台。' >&2; exit 1; }

cd "$app"
.venv/bin/python - <<'PY'
import getpass
import os
from pathlib import Path
import re
import tempfile

from docling_parser import ParseError, parser_endpoint

print("Docling 服务根地址：", end="", flush=True)
with open("/dev/tty", encoding="utf-8") as terminal:
    url = terminal.readline().strip()
key = getpass.getpass("Docling API Key（本机服务可留空）：").strip()
if any(char in url for char in "\r\n\"") or (key and not re.fullmatch(r"[A-Za-z0-9_.~-]{1,512}", key)):
    raise SystemExit("服务地址或密钥格式不正确。")
os.environ.update(KB_DOCLING_URL=url, KB_DOCLING_API_KEY=key)
try:
    parser_endpoint()
except ParseError as error:
    raise SystemExit(str(error)) from None

root = Path("/etc/knowledge-kb")
root.mkdir(mode=0o700, parents=True, exist_ok=True)
config = root / "parser.env"
with tempfile.NamedTemporaryFile("w", dir=root, delete=False, encoding="utf-8") as handle:
    stage = Path(handle.name)
    handle.write(f'KB_DB_PORT=5432\nKB_DB_NAME=knowledge_base\nKB_DOCLING_URL="{url}"\nKB_DOCLING_API_KEY={key}\n')
try:
    os.replace(stage, config)
finally:
    stage.unlink(missing_ok=True)
PY

cat > /etc/systemd/system/knowledge-kb-parser.service <<'UNIT'
[Unit]
Description=Knowledge Base Document Parser Worker
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
User=kb-service
WorkingDirectory=/opt/knowledge-kb
EnvironmentFile=/etc/knowledge-kb/parser.env
ExecStart=/opt/knowledge-kb/.venv/bin/python /opt/knowledge-kb/file_ingest.py
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict

[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/systemd/system/knowledge-kb-publisher.service <<'UNIT'
[Unit]
Description=Knowledge Base Index Publishing Worker
After=network-online.target postgresql.service opensearch.service
Wants=network-online.target

[Service]
User=kb-service
WorkingDirectory=/opt/knowledge-kb
EnvironmentFile=/etc/knowledge-kb/api.env
ExecStart=/opt/knowledge-kb/.venv/bin/python /opt/knowledge-kb/file_ingest.py --publish
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict

[Install]
WantedBy=multi-user.target
UNIT

chmod 0600 "$settings"
systemctl daemon-reload
systemctl enable knowledge-kb-parser.service knowledge-kb-publisher.service
systemctl restart knowledge-kb-parser.service knowledge-kb-publisher.service
systemctl is-active --quiet knowledge-kb-parser.service
systemctl is-active --quiet knowledge-kb-publisher.service
printf 'Docling parser and index publisher workers are active.\n'
