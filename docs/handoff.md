# 交接说明

给接手这个项目的 agent / 工程师看。读完这份应该能直接上手改代码、部署、排查。

---

## 1. 这是什么

一个团队内部知识库：把 Notion、飞书、上传的 PDF/Word/Excel 解析成片段，按「知识库 → 成员 → 授权」分发，成员通过 **MCP** 协议让 AI agent 检索自己有权看的内容。管理后台负责选库、上传、解析、预览、发布、开成员、看审计。

技术栈：

| 层 | 用什么 |
|---|---|
| 后端 | Python 3 + FastAPI，无 ORM，直接 psycopg |
| 前端 | Vue 3 + Vite + vue-router，构建产物 `web/dist` 提交在仓库里 |
| 数据库 | PostgreSQL 16 |
| 检索 | OpenSearch（混合检索时用），默认纯关键词 |
| 解析 | Docling（PDF/Office → 结构化片段） |
| 入口 | nginx + Cloudflare Tunnel |

---

## 2. 现状（2026-11-23）

**四条路径都通了**，且都已在云端跑过真实数据：

- 选库 → 上传 → 解析 → 预览 → 发布 ✅
- Notion / 飞书同步 ✅
- 成员开通、授权、撤销 ✅
- 失败任务定位与重试 ✅
- 管理员多账号登录 ✅

云端已有 5 个库、7 篇文档、3 个成员账号、1 个真实同事在用（吴东怡）。

---

## 3. 入口

| 用途 | 地址 |
|---|---|
| 管理后台 | `https://admin.zzh-resume.online/admin/` |
| 同事的 MCP | `https://kb.zzh-resume.online/mcp` |

两个域名走**同一条 Cloudflare 隧道**，都落到服务器 `127.0.0.1:18443` 的 nginx，靠 `Host` 头分流：

```
kb.*    → /mcp 转发到 127.0.0.1:18082（MCP 服务），其余路径 404
admin.* → 全部转发到 127.0.0.1:18081（API + 控制台静态文件）
```

**凭据不写在这个仓库里**（仓库是 public）。位置：

- 控制台管理员账号 → 服务器 `/etc/knowledge-kb/api.env` 的 `KB_ADMIN_USER` / `KB_ADMIN_PASSWORD`（首次部署生成，之后在控制台改）
- 数据库密码 → 服务器 `/opt/knowledge-kb/.local/postgres-password.txt`
- 部署令牌 `KB_ADMIN_TOKEN` → 同一个 `api.env`，脚本和 CI 用它，人不走这个
- SSH → 私钥 `~/.ssh/kb_repo`，`root@47.120.35.20`

---

## 4. 服务器

阿里云 ECS，`47.120.35.20`，Ubuntu，4 GiB 内存（**很紧**，PostgreSQL + OpenSearch 已占 2.2 GiB）。

```
cloudflared.service              隧道，主动往外连，不开任何入站端口
nginx.service                    18443，唯一对外的门（其实隧道也只连回环）
knowledge-kb-api.service         18081  API + 控制台
knowledge-kb-mcp.service         18082  MCP（PartOf=api，跟着 api 一起重启）
knowledge-kb-parser.service      解析 worker
knowledge-kb-publisher.service   索引发布 worker
postgresql@16-main.service       5432，db=knowledge_base，user=kb_local
opensearch.service               19200
```

全部只监听回环，除了 nginx 的 18443。

---

## 5. 代码结构

```
api.py              FastAPI 主入口，挂 /v1/knowledge 检索接口、控制台静态文件
mcp_server.py       MCP 工具定义（search_knowledge 等）
mcp_http.py         MCP 的 HTTP 传输层
access.py           鉴权：令牌 → sha256 → 查授权 → 过滤文档。connect() 在这里
search.py           检索，关键词 / 混合两种模式，index 子命令建索引
file_ingest.py      上传文件入库
docling_parser.py   Docling 调用
notion_source.py    Notion 同步
feishu_source.py    飞书同步
metadata.py         元数据映射与覆盖规则
admin.py            控制台领域逻辑（库、成员、授权、任务、审计、管理员账号）
admin_api.py        /v1/admin 路由
admin_schema.sql    控制台相关建表语句（migrate() 每次启动跑一遍，幂等）
schema.sql          核心检索表
web/src/            Vue 源码
web/src/views/      9 个页面
scripts/            部署与配置脚本（见下）
docs/onboarding.md  给同事的开通说明
```

`scripts/` 里值得知道的：

| 脚本 | 干什么 |
|---|---|
| `package-admin.sh` | 打部署包 `knowledge-admin.tar.gz`，只打需要的文件 |
| `deploy-admin-ssh.py` | 上传 + 校验 + 在服务器上执行 `deploy-admin.sh` |
| `deploy-admin.sh` | 服务器侧部署：备份、解包、编译检查、重启、验证 |
| `configure-mcp-entry.sh` | 配 MCP 的 nginx 入口 + 写 `KB_MCP_PUBLIC_URL` |
| `configure-admin-entry.sh` | 配后台的 nginx 入口 |
| `configure-notion.sh` / `configure-feishu.sh` | 配同步来源 |
| `verify-admin-cloud.py` | 部署后自检 |

---

## 6. 怎么改、怎么发

```bash
# 1. 改前端的话先构建（服务器上没有 Node）
npm --prefix web ci && npm --prefix web run build

# 2. 打包
bash scripts/package-admin.sh

# 3. 部署（走 SSH，会校验包内文件清单再上传）
python scripts/deploy-admin-ssh.py 47.120.35.20
```

`package-admin.sh` 的文件清单、`deploy-admin-ssh.py` 的 `REQUIRED`、`deploy-admin.sh` 的 `files` 三处**必须同步**。之前漏过一次 `configure-admin-entry.sh`，导致脚本根本没被带上服务器。

只改 nginx 配置不用打包，直接把脚本传上去跑：

```bash
cat scripts/configure-admin-entry.sh | ssh root@47.120.35.20 \
  'cat > /root/x.sh && bash /root/x.sh admin.zzh-resume.online'
```

---

### 文件上传和文档删除

- 个人库页面可上传本地文件，复用解析和发布 worker，自动发布。MCP 的 `upload_knowledge_file` 接受客户端读取的附件字节，`get_knowledge_file_status` 查询结果。网页和 MCP 附件均限 20 MiB。MCP 的 nginx 请求体上限为 32 MiB，容纳附件的 Base64 编码。
- 企业及个人库的「删除」移除知识库内的原件、版本、分块和来源绑定。发布 worker 会清理已登记的 OpenSearch 索引。「下架」可恢复。

三个运维记录页均可按成员筛选。个人库的操作和任务对管理员显示成员、类型和状态，个人文件标题、原文和引用仅本人可读。

### MCP 飞书链接导入

- 在飞书开放平台为现有企业自建应用开通用户身份权限 `docx:document:readonly`、`wiki:wiki:readonly`、`contact:user.email:readonly`、`contact:user.employee:readonly`，并发布应用版本。
- 在飞书「安全设置」登记 HTTPS 重定向 URL `https://admin.zzh-resume.online/v1/knowledge/feishu/callback`，并在 ECS 的 `/etc/knowledge-kb/api.env` 将 `KB_FEISHU_OAUTH_REDIRECT_URI` 设为同一个地址，重启 API。复用现有 `KB_FEISHU_APP_ID` 和 `KB_FEISHU_APP_SECRET`。更新管理域名的 nginx 配置，使授权回调路径关闭访问日志，避免记录授权码。
- 「运维管理 / 成员与权限」可开通成员个人库上传，管理员需保存企业邮箱并勾选可导入的企业库。同一个 MCP 连接器支持查询和导入。管理员可用已签发的管理员 MCP 令牌，在勾选的企业库检索并提交草稿。成员继续用自己的 MCP 令牌。用户在网页点击「授权飞书」，有效期内的用户凭证加密保存在 PostgreSQL，API 重启后可以继续使用。部署脚本首次生成 `KB_FEISHU_TOKEN_KEY` 并保存在受限的 `api.env`，密钥更换后需要重新授权。飞书授权到期后需重新授权。
- 在 Agent 中用 `import_feishu_document` 提交具体飞书 Docx 或指向 Docx 的 Wiki 链接。只需要导入某一章节时可提供完整标题。个人库和企业库的直接上传及 MCP 导入自动构建索引并发布。管理员必须明确指定企业库，Agent 可通过 `list_upload_libraries` 展示有权限的选项。旧草稿仍可手动发布。长文档在后台处理，Agent 用 `get_feishu_import_status` 查询结果。当前上限为 12,000 个飞书块、16 MiB 的分页数据和 2,000 个检索分块。导入任务在 API 进程中执行，服务重启中断时重新发同一链接即可。

---

## 7. 数据库要点

表分两组：

**核心检索**：`source_document`（平台+文档 id+版本+是否发布+有效期）、`source_chunk`（片段 id 索引）、`member_account`、`knowledge_grant`、`knowledge_base`

**控制台**：`managed_source`、`managed_file`、`managed_version`、`managed_availability_event`、`managed_publish_event`、`member_event`、`admin_account`、`admin_session`

容易搞错的地方：

- 库的表叫 `knowledge_base`，不叫 `library`；成员表叫 `member_account`，不叫 `member`
- `source_document` 没有 `id` 列，主键是 `(platform, document_id)`
- **授权 ≠ 发布**。授权决定"能看哪个库"，`published` 决定"库里的文档能不能被搜到"。两个都要满足
- `admin_session` 只存令牌的 sha256，24 小时过期

---

## 8. 已经踩过的坑（别重踩）

**nginx 的 `default_server` 不能删。** `sites-enabled` 按文件名字母序加载，`kb-admin` 排在 `kb-mcp` 前面。没有 `default_server` 时，`kb.*` 的请求会落到 `kb-admin` 那个 server 块上，被当成后台请求处理。

**`absolute_redirect off` 不能删。** 否则 `return 302 /admin/` 会拼出 `https://host:18443/admin/` 这种带内部端口的地址。

**Basic Auth 和后台登录不能共存。** 浏览器把 Basic 凭据放在 `Authorization` 头，控制台把会话令牌也放在同一个头，一个请求只能带一个。所以后台只有一道密码（控制台账号）。别再往里加 Basic Auth。

**MCP 上游要求 Host 归一化。** nginx 转发时要 `proxy_set_header Host 127.0.0.1:18082;` 并 `proxy_set_header Origin "";`，否则 FastMCP 的 DNS rebinding 校验会拒。

**本机 curl 默认走代理。** 这台开发机有 `http_proxy=127.0.0.1:9172`，测公网端口必须加 `--noproxy '*'`，否则拿到的是代理返回的 502，会误判服务挂了。

**国内直连 Cloudflare 不稳定。** 同一域名有时 1.4 秒返回、有时 TLS 握手卡到超时。从服务器自己测是稳定的 1.4～1.9 秒，所以问题在"国内 → Cloudflare 边缘"这一段。同事那边如果也这样，让他改回 `http://47.120.35.20:18443/mcp` 直连（这条还开着）。

**写 shell 脚本别产生 CRLF。** Windows 上写文件再传 Linux，`set: pipefail: invalid option name` 就是这个原因。

**服务器内存只有 4 GiB。** 装 Docling 全量模型会 OOM。现在的做法是解析 worker 跑在服务器上、Docling 留在开发机通过 SSH 反向隧道接过去。

---

## 9. 没做完的

按优先级：

1. **删库 / 删文档接口**。控制台现在只能建不能删，云端已经堆了两个「云端发布验收」垃圾库。表结构有外键，删要按依赖顺序来
2. **检索质量评测**。`evaluate_retrieval.py` 有骨架，但没有真实语料上的基线分数，调 chunk 大小/混合权重全靠手感
3. **Notion / 飞书定时同步**。现在要手动触发，没上定时
4. **网页对话界面**。用户提过想要，还没做
5. **备份**。PostgreSQL 没配定时备份，用户当时说"先没必要"
6. **nginx 收回到回环**。现在 `listen 0.0.0.0:18443`，隧道其实只需要 `127.0.0.1:18443`。收回之前要先确认不再需要 IP 直连兜底

---

## 10. 接手第一件事

```bash
# 确认链路还活着
curl -s -o /dev/null -w '%{http_code}\n' https://kb.zzh-resume.online/healthz        # 期望 200
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' \
  https://kb.zzh-resume.online/mcp                                                    # 期望 401（没带令牌）
```

然后 SSH 上去 `systemctl status knowledge-kb-api cloudflared nginx`，再看 `journalctl -u knowledge-kb-api -n 50`。

有问题先看日志和 `psql -d knowledge_base` 里的数据，不要先改配置。
