#!/usr/bin/env bash
# Give colleagues a reachable MCP entry point.
#
# The MCP service deliberately listens on 127.0.0.1 only. This script puts nginx in
# front of it on a non-standard port, so nothing has to touch 80/443 — which is what
# keeps the deployment free of ICP filing. Two gates: the Alibaba Cloud security group
# (outside) and nginx `allow` (inside), so a security group opened too wide still does
# not expose the endpoint.
set -euo pipefail

port="${KB_MCP_ENTRY_PORT:-18443}"
app=/opt/knowledge-kb
envfile=/etc/knowledge-kb/api.env

if [[ $# -lt 2 ]]; then
  cat >&2 <<'USAGE'
用法：sudo bash scripts/configure-mcp-entry.sh <对外地址> <允许的来源 IP 或 CIDR> [...]

  对外地址     同事填进客户端的主机，例如 47.120.35.20
  允许的来源   办公室出口 IP，可写多个；127.0.0.1 始终放行

例：sudo bash scripts/configure-mcp-entry.sh 47.120.35.20 58.248.185.4
USAGE
  exit 1
fi

[[ $(id -u) == 0 ]] || { echo '需要 root。' >&2; exit 1; }
[[ -x "$app/.venv/bin/python" && -s "$envfile" ]] || { echo '先部署后台，再配入口。' >&2; exit 1; }
systemctl is-active --quiet knowledge-kb-mcp.service || { echo 'MCP 服务没有在运行。' >&2; exit 1; }

host="$1"
shift
public="http://$host:$port/mcp"

allow=$'    allow 127.0.0.1;\n'
for source in "$@"; do
  [[ $source =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}(/[0-9]{1,2})?$ ]] ||
    { echo "来源地址不合法：$source" >&2; exit 1; }
  allow+="    allow $source;"$'\n'
done

if ! command -v nginx >/dev/null; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq nginx-light
fi

cat > /etc/nginx/sites-available/kb-mcp <<CONF
# 同事的 MCP 入口：只把 /mcp 转给回环上的 MCP 服务，其余路径一律 404。
server {
    listen $port;
    server_name _;
    server_tokens off;

    # 第二道门。安全组是第一道；万一安全组被开成全网，这里也只认这些来源。
$allow    deny all;

    access_log /var/log/nginx/kb-mcp.log;

    location = /healthz {
        access_log off;
        add_header Content-Type text/plain;
        return 200 "ok\n";
    }

    location = /mcp {
        proxy_pass http://127.0.0.1:18082;
        proxy_http_version 1.1;
        # 上游按 localhost 做 DNS rebinding 校验，Host 要归一化；Origin 直接抹掉。
        proxy_set_header Host 127.0.0.1:18082;
        proxy_set_header Origin "";
        proxy_set_header Connection "";
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_buffering off;
        proxy_read_timeout 300s;
        client_max_body_size 8m;
    }

    location / { return 404; }
}
CONF

ln -sf /etc/nginx/sites-available/kb-mcp /etc/nginx/sites-enabled/kb-mcp
# 默认站点占着 80，先摘掉：这个部署不需要它，也不该悄悄对外开一个端口。
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl enable --now nginx >/dev/null 2>&1 || true
systemctl reload nginx

if grep -q '^KB_MCP_PUBLIC_URL=' "$envfile"; then
  sed -i "s|^KB_MCP_PUBLIC_URL=.*|KB_MCP_PUBLIC_URL=$public|" "$envfile"
else
  printf 'KB_MCP_PUBLIC_URL=%s\n' "$public" >> "$envfile"
fi
systemctl restart knowledge-kb-api.service
curl -fsS --retry 10 --retry-connrefused --retry-delay 1 "http://127.0.0.1:$port/healthz" >/dev/null

# 不带令牌必须是 401：401 说明转发通了、鉴权也还在；200 或 5xx 都说明链路上有问题。
# MCP 服务跟着 API 一起重启（PartOf），所以要等它真的起来，否则第一次探测必然 502。
status=
for _ in $(seq 1 40); do
  status=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
    -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
    -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' "http://127.0.0.1:$port/mcp")
  [[ $status == 401 ]] && break
  sleep 0.5
done
[[ $status == 401 ]] || { echo "MCP 入口返回 $status，预期 401。" >&2; exit 1; }

cat <<DONE

入口已就绪：$public
控制台的「复制接入说明」会自动带上这个地址。

还差一步，只能在阿里云控制台做：
  ECS → 安全组 → 入方向 → 手动添加
    协议类型  自定义 TCP
    端口范围  $port/$port
    授权对象  $(printf '%s' "$*" | cut -d' ' -f1)/32
    描述      MCP 入口（办公室出口 IP）

公司出口 IP 变了就改两处：安全组的授权对象，和 /etc/nginx/sites-available/kb-mcp 里的 allow。
DONE
