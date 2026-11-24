#!/usr/bin/env bash
set -euo pipefail
# The uploader extracts the release under umask 077; anything created here must still
# be readable by the service account that runs the API.
umask 022

if [[ "$(id -u)" != 0 ]]; then
  echo 'Run this script as root on the existing knowledge-base ECS.'
  exit 1
fi

release="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
app=/opt/knowledge-kb
envfile=/etc/knowledge-kb/api.env

test -x "$app/.venv/bin/python"
test -s "$envfile"
systemctl is-active --quiet knowledge-kb-api.service
systemctl is-active --quiet knowledge-kb-mcp.service

files=(access.py admin.py admin_api.py admin_schema.sql agent_import.py api.py docling_parser.py feishu_source.py file_ingest.py
       import_api.py mcp_server.py notion_source.py search.py schema.sql)
for file in "${files[@]}"; do
  test -s "$release/$file"
done
"$app/.venv/bin/python" -m py_compile \
  "$release"/{access.py,admin.py,admin_api.py,agent_import.py,api.py,docling_parser.py,feishu_source.py,file_ingest.py,import_api.py,mcp_server.py,notion_source.py,search.py}

# The console is a Vite build served from web/dist. Build it before packaging:
#   npm --prefix web ci && npm --prefix web run build
web="$release/web/dist"
test -s "$web/index.html"
if ! grep -q '<div id="app">' "$web/index.html"; then
  echo 'web/dist/index.html is not the built console. Run npm --prefix web run build first.' >&2
  exit 1
fi
# Asset names are hashed ASCII, so word splitting is safe here.
for asset in $(grep -o 'assets/[A-Za-z0-9._-]*' "$web/index.html" | sort -u); do
  test -s "$web/$asset"
done

backup="$(mktemp -d)"
for file in "${files[@]}"; do
  if [[ -f "$app/$file" ]]; then
    cp -p -- "$app/$file" "$backup/$file"
  fi
done
cp -p -- "$envfile" "$backup/api.env"
if [[ -d "$app/web/dist" ]]; then
  cp -a -- "$app/web/dist" "$backup/web-dist"
fi

deployed=false
restore_on_failure() {
  local status=$?
  set +e
  if [[ $deployed == false ]]; then
    echo '部署未通过，正在恢复原服务文件。' >&2
    for file in "${files[@]}"; do
      if [[ -f "$backup/$file" ]]; then
        cp -p -- "$backup/$file" "$app/$file"
      else
        rm -f -- "$app/$file"
      fi
    done
    cp -p -- "$backup/api.env" "$envfile"
    if [[ -d "$backup/web-dist" ]]; then
      rm -rf -- "$app/web/dist"
      cp -a -- "$backup/web-dist" "$app/web/dist"
    fi
    systemctl restart knowledge-kb-api.service knowledge-kb-mcp.service ||
      echo '原文件已恢复，请检查 API 和 MCP 的服务日志。' >&2
    for service in knowledge-kb-parser.service knowledge-kb-publisher.service; do
      if systemctl cat "$service" >/dev/null 2>&1; then
        systemctl restart "$service" || echo "请检查 $service 的日志。" >&2
      fi
    done
  fi
  for file in "${files[@]}" api.env; do
    rm -f -- "$backup/$file"
  done
  rm -rf -- "$backup/web-dist"
  rmdir -- "$backup"
  return "$status"
}
trap restore_on_failure EXIT

for file in "${files[@]}"; do
  install -m 0644 -- "$release/$file" "$app/$file"
done
rm -rf -- "$app/web/dist"
# The release tarball is extracted under umask 077 and carries the build machine's
# numeric owners, so neither the modes nor the ownership may be preserved: the API
# runs as kb-service and a console it cannot read surfaces as a 500 on /admin.
install -d -m 0755 -- "$app/web" "$app/web/dist"
cp -R -- "$web/." "$app/web/dist/"
chown -R root:root -- "$app/web"
if ! grep -q '^KB_ADMIN_TOKEN=' "$envfile"; then
  chmod 0600 "$envfile"
  printf '\nKB_ADMIN_TOKEN=%s\n' "$("$app/.venv/bin/python" -c 'import secrets; print(secrets.token_urlsafe(32))')" >> "$envfile"
fi
# The console logs in with a username and password now. Seed one on the first deploy
# only: re-seeding on later deploys would silently reset a password someone changed.
if ! grep -q '^KB_ADMIN_PASSWORD=' "$envfile"; then
  chmod 0600 "$envfile"
  printf 'KB_ADMIN_USER=admin\nKB_ADMIN_PASSWORD=%s\n' \
    "$("$app/.venv/bin/python" -c 'import secrets; print(secrets.token_urlsafe(12))')" >> "$envfile"
  echo '已生成初始管理员 admin，密码在 /etc/knowledge-kb/api.env 的 KB_ADMIN_PASSWORD。'
fi
if ! grep -q '^KB_FEISHU_TOKEN_KEY=' "$envfile"; then
  chmod 0600 "$envfile"
  feishu_token_key=$("$app/.venv/bin/python" -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')
  printf '\nKB_FEISHU_TOKEN_KEY=%s\n' "$feishu_token_key" >> "$envfile"
fi
systemctl restart knowledge-kb-api.service knowledge-kb-mcp.service
curl -fsS --retry 10 --retry-connrefused --retry-delay 1 http://127.0.0.1:18081/healthz
printf '\n'
"$app/.venv/bin/python" - <<'PY'
from pathlib import Path
from urllib.request import Request, urlopen

values = dict(line.split("=", 1) for line in Path("/etc/knowledge-kb/api.env").read_text().splitlines()
              if line and not line.startswith("#") and "=" in line)
req = Request("http://127.0.0.1:18081/v1/admin/libraries",
              headers={"Authorization": "Bearer " + values["KB_ADMIN_TOKEN"]})
with urlopen(req, timeout=5) as response:
    assert response.status == 200

page = urlopen("http://127.0.0.1:18081/admin/", timeout=5)
body = page.read().decode("utf-8")
assert page.status == 200 and '<div id="app">' in body, "console build is not being served"
policy = page.headers["Content-Security-Policy"]
assert "script-src 'self'" in policy, policy
assert "style-src 'self' 'unsafe-inline'" in policy, policy
assert "frame-ancestors 'none'" in policy, policy
print("PASS: admin authentication, database schema and console build")
PY
systemctl is-active knowledge-kb-api.service knowledge-kb-mcp.service
for service in knowledge-kb-parser.service knowledge-kb-publisher.service; do
  if systemctl is-active --quiet "$service"; then
    systemctl restart "$service"
  fi
done
for attempt in {1..10}; do
  mcp_status=$(curl -s --max-time 3 -o /dev/null -w '%{http_code}' \
    -X POST -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
    --data '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' http://127.0.0.1:18082/mcp || true)
  [[ "$mcp_status" == 401 ]] && break
  sleep 1
done
[[ "$mcp_status" == 401 ]] || { echo "MCP HTTP $mcp_status，预期 401。" >&2; exit 1; }
deployed=true
