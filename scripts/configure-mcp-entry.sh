#!/usr/bin/env bash
# Give colleagues a reachable MCP entry point.
#
# The MCP service deliberately listens on 127.0.0.1 only. This script puts nginx in
# front of it on a non-standard port, so nothing has to touch 80/443 — which is what
# keeps the deployment free of ICP filing.
#
# Source restriction is optional. A single office already showed two different egress
# IPs (one for SSH, one for the browser), so an allowlist turns into whack-a-mole;
# pass `any` to drop it and let the bearer token be the only credential.
set -euo pipefail

port="${KB_MCP_ENTRY_PORT:-18443}"
app=/opt/knowledge-kb
envfile=/etc/knowledge-kb/api.env

usage() {
  cat >&2 <<'USAGE'
用法：sudo bash scripts/configure-mcp-entry.sh <对外地址> <来源> [...]

  对外地址   同事填进客户端的主机。写 IP 走 http://IP:端口；写域名则视为已经由
             Cloudflare 隧道或反向代理接管了 443，对外地址是 https://域名/mcp
  来源       any 表示不限制来源（只靠令牌），或者写 IP / CIDR，可以写多个

例：
  sudo bash scripts/configure-mcp-entry.sh 47.120.35.20 any
  sudo bash scripts/configure-mcp-entry.sh 47.120.35.20 58.248.185.4 14.23.147.186
  sudo bash scripts/configure-mcp-entry.sh kb.example.com any
USAGE
  exit 1
}

[[ $# -ge 2 ]] || usage
[[ $(id -u) == 0 ]] || { echo '需要 root。' >&2; exit 1; }
[[ -x "$app/.venv/bin/python" && -s "$envfile" ]] || { echo '先部署后台，再配入口。' >&2; exit 1; }
systemctl is-active --quiet knowledge-kb-mcp.service || { echo 'MCP 服务没有在运行。' >&2; exit 1; }

host="$1"
shift
# A domain means something already terminates TLS on 443 for us (Cloudflare tunnel,
# or a reverse proxy on the box). An IP means colleagues dial the bare port.
if [[ $host =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]]; then
  public="http://$host:$port/mcp"
else
  public="https://$host/mcp"
fi

open=no
access=
if [[ ${1-} == any ]]; then
  [[ $# -eq 1 ]] || { echo 'any 不能和其他来源混用。' >&2; exit 1; }
  open=yes
  access=$'    # 不限制来源：令牌是唯一凭据，限流见 /etc/nginx/conf.d/kb-mcp-limit.conf。\n'
else
  access=$'    allow 127.0.0.1;\n'
  for source in "$@"; do
    [[ $source =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}(/[0-9]{1,2})?$ ]] ||
      { echo "来源地址不合法：$source" >&2; exit 1; }
    access+="    allow $source;"$'\n'
  done
  access+=$'    deny all;\n'
fi

if ! command -v nginx >/dev/null; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq nginx-light
fi

# 限流区域只能声明在 http 块里，所以单独放一个 conf.d 文件；nginx.conf 先 include
# conf.d 再 include sites-enabled，顺序正好。
cat > /etc/nginx/conf.d/kb-mcp-limit.conf <<'CONF'
# 入口不做来源限制时，这里是唯一挡住无差别刷请求的东西。令牌猜不出来，但没人拦着
# 扫描器每秒发几千个请求。按 IP 限速，正常客户端一分钟几十次完全够用。
limit_req_zone $binary_remote_addr zone=kbmcp:10m rate=120r/m;
CONF

cat > /etc/nginx/sites-available/kb-mcp <<CONF
# 同事的 MCP 入口：只把 /mcp 转给回环上的 MCP 服务，其余路径一律 404。
# default_server 是必须的：控制台入口也监听同一个端口，没有它，Host 不匹配的请求
# 会落到先加载的那个 server 上，MCP 就会被 Basic Auth 拦掉。
server {
    listen $port default_server;
    server_name _;
    server_tokens off;

$access
    limit_req zone=kbmcp burst=30 nodelay;
    limit_req_status 429;

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

echo
echo "入口已就绪：$public"
echo "控制台的「复制接入说明」会自动带上这个地址。"
echo
if [[ $public == https://* ]]; then
  cat <<DONE
域名入口由隧道/反代接管 443，本机 nginx 仍然只监听 $port，不需要开任何入站端口，
安全组里那条 $port/$port 的规则可以删掉。
还差一步：把 $host 的 DNS/隧道指向这台机器的 http://127.0.0.1:$port。
DONE
elif [[ $open == yes ]]; then
  cat <<DONE
来源不限制。还差一步，只能在阿里云控制台做：
  ECS → 安全组 → 入方向 → 把 $port/$port 那条规则的「授权对象」改成 0.0.0.0/0

改完任何人任何网络都能连，凭令牌区分身份。想改回白名单就重跑本脚本并列出 IP。
DONE
else
  cat <<DONE
来源限制在：$(printf '%s ' "$@")
还差一步，只能在阿里云控制台做：
  ECS → 安全组 → 入方向 → 手动添加
    协议类型  自定义 TCP
    端口范围  $port/$port
    授权对象  上面那些 IP（多个就各加一条）
    描述      MCP 入口

来源 IP 变了就重跑本脚本，并把安全组里旧的规则改掉。
DONE
fi
