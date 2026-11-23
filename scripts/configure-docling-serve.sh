#!/usr/bin/env bash
# Install Docling Serve on the deployment host so PDF/Office/scan uploads can be parsed.
#
# The console and the parser worker both live in /opt/knowledge-kb/.venv, but Docling
# needs torch, transformers and OCR wheels that are far larger than the API's own
# dependencies. Keeping them in a second virtualenv means a console redeploy never has
# to re-download 2 GB of wheels, and the parser stays a plain HTTP client.
set -euo pipefail

app=/opt/docling
port=${KB_DOCLING_PORT:-18112}
python=${KB_DOCLING_PYTHON:-python3}

[[ $(id -u) == 0 ]] || { echo '请以 root 运行。' >&2; exit 1; }
id kb-service >/dev/null 2>&1 || { echo '先部署后台，kb-service 用户由部署脚本创建。' >&2; exit 1; }
[[ -x "$app/.venv/bin/python" ]] || "$python" -m venv "$app/.venv"

# An SSH tunnel or a forgotten manual run can hold the port while the unit keeps
# restarting with "address already in use". Stop our own unit first, then make sure
# nothing else is left listening, so the failure is reported here instead of in a loop.
systemctl stop docling-serve.service 2>/dev/null || true
sleep 1
if ss -ltn "sport = :$port" | grep -q LISTEN; then
  echo "端口 $port 已被其他进程占用，请换一个：KB_DOCLING_PORT=18113 bash $0" >&2
  ss -ltnp "sport = :$port" >&2
  exit 1
fi

# Docling peaks above 1 GiB while it loads the layout and OCR models. A 2 GiB swap file
# keeps the kernel from killing it on the small ECS instances the console ships to.
if [[ ! -f /swapfile ]]; then
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap -q /swapfile >/dev/null
  swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# torch and torchvision must come from the same CPU wheel index. Installing torchvision
# from PyPI next to a CPU torch yields "operator torchvision::nms does not exist" at
# import time, which surfaces only as a transformers import failure inside Docling.
"$app/.venv/bin/pip" install --no-cache-dir --upgrade pip >/dev/null
"$app/.venv/bin/pip" install --no-cache-dir torch torchvision --index-url https://download.pytorch.org/whl/cpu
"$app/.venv/bin/pip" install --no-cache-dir "docling-serve==1.38.2" onnxruntime "transformers==5.23.0"

chown -R kb-service:kb-service "$app"
install -d -o kb-service -g kb-service -m 0750 /var/lib/docling /var/lib/docling/scratch

cat > /etc/systemd/system/docling-serve.service <<UNIT
[Unit]
Description=Docling Serve document parsing API
After=network-online.target
Wants=network-online.target

[Service]
User=kb-service
Group=kb-service
WorkingDirectory=$app
Environment=HOME=$app
Environment=HF_ENDPOINT=https://hf-mirror.com
Environment=HF_HUB_DISABLE_TELEMETRY=1
Environment=DOCLING_DEVICE=cpu
Environment=DOCLING_NUM_THREADS=2
Environment=DOCLING_SERVE_ENABLE_UI=false
Environment=DOCLING_SERVE_LOAD_MODELS_AT_BOOT=false
Environment=DOCLING_SERVE_ENG_LOC_NUM_WORKERS=1
Environment=DOCLING_SERVE_OPTIONS_CACHE_SIZE=1
Environment=DOCLING_SERVE_ENABLE_REMOTE_SERVICES=false
Environment=DOCLING_SERVE_MAX_NUM_PAGES=200
Environment=DOCLING_SERVE_MAX_FILE_SIZE=20971520
Environment=DOCLING_SERVE_MAX_DOCUMENT_TIMEOUT=900
Environment=DOCLING_SERVE_SCRATCH_PATH=/var/lib/docling/scratch
ExecStart=$app/.venv/bin/python -m docling_serve run --host 127.0.0.1 --port $port --workers 1
Restart=on-failure
RestartSec=5
TimeoutStopSec=30
# Docling holds about 1.2 GiB once the layout and OCR models are warm, and the same box
# runs PostgreSQL and OpenSearch. Cap it so a heavy document kills the parser (which
# systemd restarts) instead of the kernel killing the database.
MemoryHigh=1600M
MemoryMax=2200M
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=full
ReadWritePaths=$app /var/lib/docling

[Install]
WantedBy=multi-user.target
UNIT

# The parser worker reaches Docling over loopback, so no API key is involved; the port
# must match KB_DOCLING_URL in /etc/knowledge-kb/parser.env.
sed -i "s#^KB_DOCLING_URL=.*#KB_DOCLING_URL=\"http://127.0.0.1:$port\"#" /etc/knowledge-kb/parser.env

systemctl daemon-reload
systemctl enable --now docling-serve.service
systemctl restart knowledge-kb-parser.service
sleep 8
systemctl is-active --quiet docling-serve.service
curl -fsS -m 5 "http://127.0.0.1:$port/health" >/dev/null
printf 'Docling Serve is answering on 127.0.0.1:%s; the parser worker points at it.\n' "$port"
