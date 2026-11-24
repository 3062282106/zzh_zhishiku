#!/usr/bin/env bash
# Publish the console on the same nginx port as the MCP entry, distinguished by Host.
#
# There is exactly one password: the console's own account login. An earlier version
# also put an HTTP Basic Auth prompt in front, which does not work here — the browser
# caches Basic credentials in the `Authorization` header, but the console sends its
# session token in that same header, so every API call arrived without credentials and
# nginx answered 401. Two prompts would also be two passwords for colleagues to keep,
# which is not what "one admin account" was supposed to mean.
#
# What still guards the door: scrypt-hashed passwords, 8 failed logins per 5 minutes
# per IP, 24-hour session tokens stored only as SHA-256, and the nginx rate limit below.
set -euo pipefail

port="${KB_ADMIN_ENTRY_PORT:-18443}"
app=/opt/knowledge-kb

usage() {
  cat >&2 <<'USAGE'
用法：sudo bash scripts/configure-admin-entry.sh <后台域名>

  后台域名   隧道里映射到 nginx 的那个主机名，例如 admin.example.com

例：sudo bash scripts/configure-admin-entry.sh admin.example.com
USAGE
  exit 1
}

[[ $# -ge 1 ]] || usage
[[ $(id -u) == 0 ]] || { echo '需要 root。' >&2; exit 1; }
[[ -x "$app/.venv/bin/python" && -s /etc/knowledge-kb/api.env ]] || { echo '先部署后台。' >&2; exit 1; }
systemctl is-active --quiet knowledge-kb-api.service || { echo 'API 没有在运行。' >&2; exit 1; }

host="$1"
[[ $host =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]] || { echo "域名不合法：$host" >&2; exit 1; }

command -v nginx >/dev/null || { echo '先跑 scripts/configure-mcp-entry.sh 把 nginx 装上。' >&2; exit 1; }

cat > /etc/nginx/sites-available/kb-admin <<CONF
# 管理后台。和 MCP 入口共用 $port，靠 Host 区分。
server {
    listen $port;
    server_name $host;
    server_tokens off;
    # 否则 return 302 会拼出带内部端口的绝对地址（https://host:18443/admin/）。
    absolute_redirect off;

    limit_req zone=kbmcp burst=30 nodelay;
    limit_req_status 429;

    access_log /var/log/nginx/kb-admin.log;
    # OAuth codes and state appear in the callback query string.
    location = /v1/knowledge/feishu/callback {
        access_log off;
        proxy_pass http://127.0.0.1:18081;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-Proto https;
    }


    # 直接敲域名的人应该落在控制台，而不是 API 的根路径。
    location = / { return 302 /admin/; }

    location / {
        proxy_pass http://127.0.0.1:18081;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        # 上传走这条路，MAX_FILE 是 20 MiB，留一倍余量。
        client_max_body_size 64m;
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
    }
}
CONF

ln -sf /etc/nginx/sites-available/kb-admin /etc/nginx/sites-enabled/kb-admin
nginx -t
systemctl reload nginx

# `reload` returns before the new workers take over, so poll instead of trusting the
# first request — otherwise the script reports a false failure on the very first run.
page=login=
for _ in $(seq 1 20); do
  page=$(curl -s -o /dev/null -w '%{http_code}' -H "Host: $host" "http://127.0.0.1:$port/admin/")
  login=$(curl -s -o /dev/null -w '%{http_code}' -X POST -H "Host: $host" \
    -H 'Content-Type: application/json' -d '{"username":"-","password":"-"}' \
    "http://127.0.0.1:$port/v1/admin/login")
  [[ $page == 200 && $login == 401 ]] && break
  sleep 0.5
done
[[ $page == 200 ]] || { echo "控制台首页返回 $page，预期 200。" >&2; exit 1; }
# 401 说明登录接口通了、凭据被拒；200 就说明空账号能进，那是真问题。
[[ $login == 401 ]] || { echo "登录接口返回 $login，预期 401。" >&2; exit 1; }

cat <<DONE

后台入口已就绪：https://$host/admin/
登录用的是控制台里的管理员账号（首次部署时生成，存在 /etc/knowledge-kb/api.env
的 KB_ADMIN_PASSWORD）。同事要进来，就在控制台的「管理员」页给他建一个账号。

还差隧道那一步，在 /etc/cloudflared/config.yml 的 ingress 里加：
  - hostname: $host
    service: http://127.0.0.1:$port
然后 systemctl restart cloudflared。
DONE
