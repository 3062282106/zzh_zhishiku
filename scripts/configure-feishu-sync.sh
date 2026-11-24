#!/usr/bin/env bash
set -euo pipefail

app=/opt/knowledge-kb
[[ "$(id -u)" == 0 && -x "$app/.venv/bin/python" && -f "$app/feishu_sync.py" ]] ||
  { echo '先以 root 部署飞书同步服务。' >&2; exit 1; }

cat > /etc/systemd/system/knowledge-kb-feishu-sync.service <<'UNIT'
[Unit]
Description=Knowledge Base Feishu Document Sync
Wants=network-online.target
After=network-online.target postgresql.service opensearch.service

[Service]
Type=oneshot
User=kb-service
WorkingDirectory=/opt/knowledge-kb
EnvironmentFile=/etc/knowledge-kb/api.env
ExecStart=/opt/knowledge-kb/.venv/bin/python /opt/knowledge-kb/feishu_sync.py
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
TimeoutStartSec=15min
UNIT

cat > /etc/systemd/system/knowledge-kb-feishu-sync.timer <<'UNIT'
[Unit]
Description=Run Knowledge Base Feishu Document Sync

[Timer]
OnBootSec=30s
OnUnitInactiveSec=60s
AccuracySec=5s

[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
systemctl enable --now knowledge-kb-feishu-sync.timer
systemctl is-active --quiet knowledge-kb-feishu-sync.timer
echo 'Feishu sync timer is active.'
