# 企业知识库

> 接手这个项目先读 [`docs/handoff.md`](docs/handoff.md)：现状、入口、部署方式、踩过的坑、没做完的事都在那一份里。凭据不在仓库（仓库是公开的）。

## 复杂文档入库

后台可上传 PDF、DOCX、XLSX、PPTX、PNG、JPEG、TIFF。Docling Serve 负责版面识别、RapidOCR（默认简体中文）、表格识别和按结构分块。Docling 识别的页眉页脚默认不参与正文分块。知识库保存原始文件、结构化文档、分块以及解析状态。正文证据与带标题和表格上下文的检索文本分开存储。可用的页码和坐标随证据返回。Notion、飞书和纯文本走现有入库入口。

后台也可上传带表头的逗号分隔 CSV。解析 worker 用 Python 读取 UTF-8（含 BOM）或 GB18030，每条数据生成一个带列名和原文件行号的分块。单文件最多 2500 条数据。CSV 原件随版本保留，可按权限下载。

上传产生解析任务，任务完成后在后台预览分块，再提交发布。后台展示解析和发布队列、构建索引、成功和失败状态。解析与发布由独立 worker 执行。索引准备好后再切换当前版本，等待期间团队继续检索现行版本。重新发布前需核对并确认发布期间发生的版本和有效期变更。查询端只读取已发布且在有效期内的授权版本，原件使用同一权限检查。单文件上限 20 MiB，单结果最多 2500 个分块，解析服务预设上限为 200 页。

### 在现有 ECS 上升级后台

前端构建产物一起发布，所以本地要有 Node（服务器不需要）。两步：

```powershell
npm --prefix web ci; npm --prefix web run build   # 产出 web/dist
bash scripts/package-admin.sh                     # 打出 scripts/knowledge-admin.tar.gz
python scripts/deploy-admin-ssh.py <ECS_IP> --tunnel
```

`package-admin.sh` 只打部署需要的文件（Python 源文件 + `web/dist`），`deploy-admin-ssh.py` 先校验包内文件名与内容再 scp 上传，然后执行 `scripts/deploy-admin.sh`。也可以手工解压发布包，在解压目录执行 `sudo bash scripts/deploy-admin.sh`。脚本依赖已运行的知识库 API、MCP、PostgreSQL 和原有 `/opt/knowledge-kb` 虚拟环境，并验证升级后的后台连通性（包括 `/admin/` 是否返回控制台、CSP 是否生效）。拆包后也可先运行 `tar -tzf` 核对内容。升级会重启知识库 API 和 MCP。

`web/dist` 缺失时 `deploy-admin.sh` 会直接失败并提示先构建；`/admin/` 在缺少构建产物时返回 503，不会回落到旧页面。

### 接入独立解析服务

在独立的解析主机运行 Docling Serve。已有可信的企业 Docling 服务时可直接使用。随代码提供的 `scripts/docling.compose.yml` 固定了官方 CPU 版镜像，限制为一个解析 worker。该配置预留至多 6 GiB 内存，现有 4 GiB ECS 用于运行知识库服务。服务只在解析主机回环地址监听，远端接入需由组织自己的 HTTPS 入口转发到本机 5001 端口，并限制访问范围。反向代理至少允许约 28 MiB 请求体以容纳 20 MiB 文件的 Base64 编码。初次启动后，解析主机需要从可信来源下载分块 tokenizer 和运行时缺少的模型，或由运维预置这些文件。

在解析主机受限的目录中设置 `DOCLING_SERVE_API_KEY`，然后运行 `docker compose -f scripts/docling.compose.yml up -d`。保留该独立密钥以接入知识库。远端地址使用已配置 HTTPS 的根地址，例如 `https://parser.example.com`。

回到知识库 ECS 后执行 `sudo bash scripts/configure-parser-worker.sh`，按终端提示输入解析服务根地址和密钥。密钥由脚本写入 `/etc/knowledge-kb/parser.env`。脚本启动 `knowledge-kb-parser` 和 `knowledge-kb-publisher`，发布 worker 读取 API 已有的检索和百炼配置。将获批的文档传入后台，草稿就绪后核对预览并提交发布。后台显示处理状态和失败原因，通过 `systemctl is-active knowledge-kb-parser knowledge-kb-publisher` 检查服务。原件与解析结果随 PostgreSQL 数据进入现有备份范围。

### 用本机 Docling 当解析主机（服务器内存不够时）

4 GiB 的 ECS 上 PostgreSQL 和 OpenSearch 已经占掉 2.2 GiB，而 Docling 模型常驻约 1.2 GiB，同机塞不下。这种情况让服务器只跑解析 worker，Docling 留在开发机上，用 SSH 反向隧道接过去：

```bash
# 开发机：把本机 Docling 的端口转发到服务器的回环地址
ssh -N -R 18111:127.0.0.1:18111 root@<ECS_IP>

# 开发机：本机起 Docling（见「本地解析 PDF」一节）
```

服务器上 `parser.env` 指向那个回环端口即可，`configure-parser-worker.sh` 输入 `http://127.0.0.1:18111`：

```bash
KB_DOCLING_URL="http://127.0.0.1:18111"
KB_DOCLING_API_KEY=
KB_DOCLING_TOKENIZER="D:/知识库/.local/qa-tokenizer"
```

注意三个容易踩的点：

- **`KB_DOCLING_TOKENIZER` 是解析主机上的路径**，不是服务器上的。worker 只把这个字符串原样放进请求，由 Docling 所在的那台机器去解析，所以填开发机的目录是对的；Linux 解析主机上则要留空（默认 `bert-base-multilingual-cased`）。
- **隧道断了任务会排队，不会报错**。后台任务页会显示「解析服务没有在运行」的红色横幅和真实等待时长，重启隧道后队列自动继续，不需要重新上传。
- **端口上没人监听不一定是故障**。`ss -ltnp` 显示 `sshd` 持有该端口时，说明是 `ssh -R` 转发，别急着在服务器上装一套 Docling。

本地入库与分块工程。使用 Python 3.11+ 和标准库。首批源文件、文档 ID、正文与切片保存在 Git 忽略的 `.local/` 中。

## 生成并核对切片

从项目根目录用 PowerShell 执行：

```powershell
python ingest.py
```

程序读取 `.local/sources.json` 及源快照，输出 `.local/chunks.jsonl`（UTF-8，每行一条 JSON）。每条记录包含稳定的切片 ID、来源文档 ID、版本、标题路径、正文、源 URL 和定位信息。飞书保留块 ID 与可打开的块引用，Notion 快照保留页面链接以及快照行号和标题路径。空标题、分隔线和媒体占位不进入文本切片。源快照保留排查和定位所需的信息。

## 刷新来源

- **飞书**：在已登录 `lark-cli` 的环境运行 `./scripts/refresh-feishu.ps1`，按私有清单里的章节块 ID 拉取 XML。脚本验证响应、文档 ID、章节和同一文档的版本后替换采集文件。
- **Notion**：用已授权连接器获取页面。私有 JSON 快照使用返回的 `id`、`title`、`url`、`page_last_edited_at`，另设 `markdown` 保存页面正文。刷新快照后重新运行 `python ingest.py`。本地工具读取已保存快照，实时采集由连接器负责。

私有清单示例见 `sources.example.json`。文档 JSON 路径相对 `.local/`。对飞书，`sections` 是待采集的章节块 ID，按原文顺序排列；使用带块 ID 的 XML 采集。多个飞书章节使用合成快照，形状为 `{"id":"...","revision_id":123,"sections":[{"anchor":"...","xml":"<fragment>...</fragment>"}]}`。

## 在 Windows 上运行检索和本地分库权限

使用 OpenSearch 官方 Windows ZIP 和 PostgreSQL 官方下载页链接的 EDB Windows 二进制包。默认安装在 `D:\kb-runtime\opensearch-3.10.0` 和 `D:\kb-runtime\postgresql-18.7\pgsql`。两项服务仅监听本机。PostgreSQL 使用 SCRAM 密码，首次启动生成的凭据和测试账号凭据保存在 Git 忽略的 `.local/`。OpenSearch 本机实例供项目开发使用，员工或 Agent 的请求须经过后端授权。

```powershell
./scripts/start-opensearch.ps1
./scripts/start-postgres.ps1
python -m pip install --target .local/python -r requirements.txt
python search.py index
python -X utf8 access.py seed
python -X utf8 access.py query '聚客宝放假期间可以在哪个端回复？' --account support-test
python -X utf8 access.py query '微商城的实物订单如何履约？' --account operations-test
python -X utf8 verify_access.py
```

两个账号是仅供本机验证的测试身份。客服测试账号只能访问飞书样本库，运营测试账号只能访问 Notion 样本库。查询从 PostgreSQL 获取当前授权及有效版本对应的切片 ID，再用这些 ID 限定 OpenSearch 候选，返回前复核。读取切片引用使用同一授权逻辑：

```powershell
python -X utf8 access.py reference '<检索返回的 chunk_id>' --account support-test
```

`seed` 登记本地样本版本和测试身份。重复执行时会保留已撤销的授权。`verify_access.py` 在测试账号上临时撤权，核对下次搜索及引用读取，再恢复原测试授权。数据库和运行日志位于 `.local/`。当前验证范围为本机命令行和服务，企业登录、管理界面和外部 Agent 对接另行实现。百炼调用方式见下节。

## 接入百炼模型 API

混合检索是显式启用的。默认查询仍使用本地关键词检索。已接入北京业务空间的 `qwen3.7-text-embedding-flash`（1024 维）和 `qwen3.7-text-rerank`。调用方提供进程环境变量 `DASHSCOPE_API_KEY`。密钥不写进项目文件、命令参数或日志。

确认样本切片可发送给百炼后，在一个 PowerShell 会话内运行：

```powershell
$secret = Read-Host '百炼 API Key' -AsSecureString
$env:DASHSCOPE_API_KEY = [System.Net.NetworkCredential]::new('', $secret).Password
try {
    python -X utf8 search.py index --hybrid
    if ($LASTEXITCODE -ne 0) { throw 'Hybrid indexing failed' }
    python -X utf8 access.py query '聚客宝放假期间可以在哪个端回复？' --account support-test --hybrid
} finally {
    Remove-Item Env:DASHSCOPE_API_KEY -ErrorAction SilentlyContinue
}
```

混合索引与关键词索引分开。用同一份快照和同一个模型重复导入时，完整索引会被复用。在线检索先按 PostgreSQL 授权限制关键词与向量候选，RRF 合并后最多把 30 条已复核的证据交给 Rerank。返回前再次检查权限。向量服务超时可回退到关键词结果，Rerank 失败可回退到融合结果。代理、供应商返回或本机日志均不用于存放密钥。

## 本地 HTTP 服务

先启动 PostgreSQL 与 OpenSearch，并完成样本索引及权限登记。运行 `python -X utf8 api.py`。服务监听 `http://127.0.0.1:18080`，`/docs` 提供交互式接口说明。

- `POST /v1/knowledge/search` 接收 `{"query":"问题","limit":5}`，返回 `evidence`。每条证据包含原文引用和需再次校验的 `reference_path`。
- `GET /v1/knowledge/references/{chunk_id}` 按当前权限读取切片。无权限与不存在都返回 404。
- 两个接口都需要 `Authorization: Bearer ...`。本机测试令牌保存在 Git 忽略的 `.local/test-accounts.json`，浏览器在本机接口文档中可用 Authorize 输入对应令牌。这些令牌仅用于本地联测。

查询可附加可选的元数据筛选，例如 `{"query":"放假在哪回复","filters":{"terminal":["mobile","tablet"]}}`。同一字段匹配任一值，多个字段需同时满足。筛选与权限条件一起进入关键词和向量召回。搜索返回 `evidence_found` 或 `insufficient_evidence`。有候选时，Agent 仍需核对原文是否支持所问条件。

导入配置见 `sources.example.json`。来源可选择元数据配置，配置把业务字段名映射到表格列名。来源也可设置文档级 `metadata` 或按完整标题路径设置 `section_metadata`。同名值按文档、章节、表格行的顺序覆盖。值使用字符串或字符串列表，精确匹配保留大小写及前导零。旧资料无需添加元数据即可继续搜索。

默认启动关键词模式。用百炼混合检索时，先完成 `search.py index --hybrid`。在启动服务的同一个 PowerShell 会话中输入密钥，设置 `$env:KB_ENABLE_HYBRID = '1'`，再运行 `api.py`。参考上一节的临时环境变量配置方式。该开关下，鉴权成功的搜索可能调用计费 API。

## 管理后台

后台用管理员账号登录，不再是粘 `KB_ADMIN_TOKEN`。首次部署由 `scripts/deploy-admin.sh` 生成 `admin` 的初始密码写进 `/etc/knowledge-kb/api.env` 的 `KB_ADMIN_PASSWORD`（只在表为空时播种，之后不会覆盖你改过的密码）。登录后到「管理员」页可以改密码、加账号、停用。

密码用 scrypt 加每账号独立盐，参数编在哈希串里；会话令牌只存 sha256，24 小时过期，改密码或停用会立刻清掉该账号的所有会话。连续 8 次密码错误按用户名锁 5 分钟。

`KB_ADMIN_TOKEN` 仍然有效，部署脚本和云端验证用它；人走登录，这样审计里才分得清谁是谁。

同事要进来：在「管理员」页给他建一个账号，把 `https://admin.<域名>/admin/` 和用户名密码给他。账号可以随时停用，停用会立刻踢掉他所有在线会话。

`GET /admin` 307 到 `/admin/`，由 FastAPI 直接提供 Vue 构建产物（`web/dist`），页面只调用 `/v1/admin` 下的接口，鉴权用登录换来的会话令牌。页面结构按 Figma 定稿：左侧导航（知识库、同步来源、处理任务、成员与权限、管理员、操作记录、设置），右侧列表页，详情与操作放在右侧抽屉。

### 前端工程

源码在 `web/`，Vue 3 + Vite + vue-router，构建产物提交在 `web/dist`，服务端不需要 Node。

```powershell
cd web
npm ci
npm run dev     # 本地调试，代理 /v1 到 127.0.0.1:18081
npm run build   # 产出 web/dist，部署脚本只认这个目录
```

改完前端必须重新 `npm run build`：`admin_api.py` 只读 `web/dist/index.html`，目录缺失时 `/admin/` 返回 503 并提示先构建。

路由用 history 模式，`/admin/` 下的非静态资源路径都回落到 `index.html`，因此这些地址可以直接分享、刷新：

- `/admin/kb/{library}`、`/admin/kb/{library}/doc/{document}`：库列表与文档详情。
- `/admin/kb/{library}/doc/{document}?v={version}&tab=probe&panel=history`：指定版本、试检索标签、直接打开版本历史抽屉。
- `/admin/sources/{library}`、`/admin/tasks`、`/admin/members`、`/admin/audit`、`/admin/settings`。

令牌只存在本标签页的 `sessionStorage`，关闭标签页即失效；不写 Cookie、不进 URL。CSP 由服务端下发：`script-src 'self'`（不再允许内联脚本），`style-src 'self' 'unsafe-inline'`（Vue 的 `:style` 绑定需要行内样式）。

回归检查（需要 API 在 18081 运行）：

```powershell
$env:KB_ADMIN_TOKEN='...'; npm run qa
```

### 本地解析 PDF（Windows，没有 Docker 时）

上传接口按扩展名分两条路：

- `.md` / `.txt` 在请求里直接解码成文本、切成块、生成草稿，不依赖任何外部服务。本地只跑 `python api.py` 就能走完「上传 → 预览 → 发布」。
- PDF、DOCX、XLSX、PPTX、图片走 `managed_file` 队列，由 `file_ingest.py` 领取后调用 Docling。**这个进程不在 `api.py` 里**，本地没起它时任务会一直停在「等待中」。

有 Docker 时最省事：

```bash
docker compose -f scripts/docling.compose.yml up -d   # 需要 DOCLING_SERVE_API_KEY
```

没有 Docker 时，可以复用 `.local/docling-qa` 里已经装好的 docling 包，只补一个 CPython 3.12 解释器（这些包是 `cp312-cp312-win_amd64` 编译的，3.13/3.14 加载不了）：

```powershell
# 1. 解压 https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip 到 .local/py312
#    把 .local/py312/python312._pth 改成：
#      python312.zip
#      .
#      D:\知识库\.local\docling-qa
#      import site
# 2. rapidocr 需要 onnxruntime，其余依赖已在 docling-qa 里：
#    curl -L -o ort.whl https://files.pythonhosted.org/packages/a9/3e/27039badc39a1b01628b6b40b1ff1bf4a886e6dfb2f311c483606b7c86f7/onnxruntime-1.32.0-cp312-cp312-win_amd64.whl
#    unzip -o ort.whl -d .local/docling-qa

# 3. 起 Docling（模型约 500 MB，只在第一次下载）
cd .local/py312
$env:HF_ENDPOINT='https://hf-mirror.com'          # huggingface.co 直连不通，走镜像
$env:DOCLING_SERVE_API_KEY='local-docling-key'
$env:DOCLING_SERVE_ALLOWED_SOURCE_TYPES='file'
$env:DOCLING_SERVE_ALLOWED_TARGET_TYPES='inbody'
$env:DOCLING_DEVICE='cpu'; $env:DOCLING_NUM_THREADS='2'
.\python.exe -m docling_serve run --host 127.0.0.1 --port 5001

# 4. 另开一个窗口，起解析 worker
cd D:\知识库
$env:KB_DOCLING_URL='http://127.0.0.1:5001'; $env:KB_DOCLING_API_KEY='local-docling-key'
python file_ingest.py
```

线上由 `knowledge-kb-parser.service` 承担，`scripts/configure-parser-worker.sh` 会写好 env 与 unit。

后台对此有兜底提示：队列超过 5 分钟没人处理、且当时没有任何任务在解析，处理任务页会显示「解析服务没有在运行」，任务恢复后会自动继续，不需要重新上传。判断带 `NOT EXISTS (parsing)` 条件，避免解析服务只是排队长时被误报。

`.local/` 不入库，所以上面这套解释器和包只在这台机器上有效，换机器要重做。

`npm run qa` 用 Chrome DevTools Protocol 走完登录、列表搜索、文档详情、试检索（有/无权限两种身份）、三个抽屉、深链刷新，并把截图写到 `.local/qa-console/`；任何页面报错或 CSP 拦截都会判失败。

列表与详情接口：

- `GET /v1/admin/libraries`、`POST /v1/admin/libraries`：知识库列表与新建。
- `GET /v1/admin/libraries/{library}/documents`：标题、来源（`managed_source.provider`，未绑定为 `upload`）、当前状态（`source_document` + `VISIBILITY_STATUS`）、负责人、更新时间与分块数。
- `GET /v1/admin/documents/{document}/versions`、`GET /v1/admin/documents/{document}/versions/{version}`：版本与分块预览，数据来自 `managed_version`。
- `POST /v1/admin/documents/{document}/versions/{version}/publish|rollback`：请求体可带 `valid_from`、`valid_until` 做定时发布；带 `managed_file` 的版本进入发布队列，否则同步构建索引并切换。

任务与审计接口：

- `GET /v1/admin/tasks`：把 `managed_file` 的解析/发布状态和 `managed_source` 的同步结果合并成统一任务流，失败项带原始错误。
- `GET /v1/admin/audit`：合并 `managed_publish_event`、`managed_availability_event` 与 `knowledge_grant.updated_at`，按时间倒序。

成员接口：

- `GET /v1/admin/members` 返回成员目录与分库授权明细（含到期与撤销时间）。
- `POST /v1/admin/members/{member}/grants` 用 `{"libraries":[...],"expires_at":...}` 一次性替换授权：列表外的库会被撤销，列表内的库恢复并更新到期时间。

试检索：

- `POST /v1/admin/documents/{document}/versions/{version}/probe` 接收 `{"question":"…","member":"…"}`，先按 `knowledge_grant` 判断该成员能否访问此库，再在该版本的分块内匹配并返回命中原文。它不写入线上索引，也不替代线上检索链路。

### 数据库变更

后台首次请求会执行 `schema.sql` 与 `admin_schema.sql`，本轮新增的列都用 `ADD COLUMN IF NOT EXISTS`，可直接在现有库上重跑：`source_document.owner`、`member_account.display_name/email/kind`、`knowledge_grant.updated_at`、`managed_file.publication_valid_from/publication_valid_until`、`managed_availability_event.previous_valid_from/valid_from`。已有记录保持原值，负责人为空时页面显示「未指定」。

### 尚未接入

- 邀请个人：需要企业身份服务签发账号与登录凭证，页面保留入口并提示依赖。
- 员工身份登录与跨端绑定：后台仍用管理令牌，按员工身份的检索验收走试检索的模拟身份。
- 原文版面：预览展示解析后的原文快照与分块，不还原 PDF 版式。
- 豆包工作、豆包工作伙伴：设置页如实标注「待连接」，接口与授权范围已按现有 MCP/HTTP 能力展示。

## 用 Codex 调用本地 MCP

保持 `18081` 的混合检索服务运行。在 Codex 中打开并信任 `D:\知识库` 这个项目，再开一个项目内聊天。项目配置 `.codex/config.toml` 注册 `knowledge_local`，绑定运营测试身份。也可以显式将同一个配置只应用到当前 CLI 会话：

```powershell
Set-Location -LiteralPath 'D:\知识库'
codex -c 'mcp_servers.knowledge_local={command="python",args=["-X","utf8","D:/知识库/mcp_server.py","--account","operations-test"],cwd="D:/知识库",tool_timeout_sec=55}'
```

在 Agent 对话中问「请用 knowledge_local 查：消费者在线下扫码消耗次卡，钱在哪里核对？请附原文」。MCP 提供只读工具 `search_knowledge` 和 `get_knowledge_reference`，调用本机 HTTP 服务。测试身份由启动参数固定，模型不能传账号或令牌。令牌仅从本地私有文件读取。要验收客服账号隔离，单独启动一个客户端，将启动参数 `operations-test` 换成 `support-test`。

### 桌面客户端的 HTTP 连接器

启动 18081 知识库 API 后，从项目目录运行 `python -X utf8 mcp_http.py`。在同一台电脑的桌面客户端选择 HTTP，服务器 URL 填 `http://127.0.0.1:18082/mcp`，自定义 Header 的名称填 `Authorization`，值填 `Bearer ` 加本地测试身份的令牌。测试身份从 `.local/test-accounts.json` 选取，百炼模型密钥由后端单独配置。

HTTP 每次调用都将请求携带的身份传给知识库 API，由 API 校验查询和原文读取权限。服务只监听本机回环地址。分别使用运营和客服令牌验收，确认运营能读取的专属引用对客服身份返回错误。

### 给同事的入口（公网访问，不需要域名）

MCP 服务只监听 `127.0.0.1:18082`，同事的客户端连不进来。用 nginx 在非标端口开一个口子，只转发 `/mcp`：

```bash
sudo bash scripts/configure-mcp-entry.sh 47.120.35.20 any
```

两个参数：同事填进客户端的主机，以及来源限制。来源写 `any` 表示不限制、只认令牌；也可以列 IP / CIDR。脚本做四件事——装 nginx、写 `/etc/nginx/sites-available/kb-mcp`、把默认站点从 80 摘掉、把 `KB_MCP_PUBLIC_URL` 写进 `api.env` 并重启 API。控制台的「复制接入说明」随即带上这个地址，不需要重新部署。

不开 80/443，所以不需要域名，也不需要备案。`http://<主机>:18443/healthz` 返回 `ok` 说明 nginx 在跑，`/mcp` 不带令牌返回 `401` 说明转发和鉴权都通。

来源限制建议直接写 `any`。同一个办公室实测就有两个出口 IP——SSH 走一个、浏览器走另一个——逐个收集只会变成打地鼠。真正拦住陌生人的是令牌（32 字节随机串，只在建成员时返回一次），nginx 那边按 IP 限速（每分钟 120 次，突发 30）挡住无差别扫描。确实要收紧就重跑脚本列出 IP，并在安全组里把授权对象改成同样的地址。

代价是明文 HTTP：令牌和检索结果在公网上不加密，端口对全网可见（没令牌一律 401）。日志里会有扫描噪音。以后买了域名，把这段换成 443 加证书即可，同事只改一个 URL，成员和授权都不用动。

### 用 Cloudflare 隧道换成 HTTPS（不用备案）

IP + 非标端口能用，但明文传输，而且同事换个网络就得改白名单。把域名托管到 Cloudflare、让服务器主动往外连，这两件事一起解决：不开任何入站端口，不需要备案，令牌走加密通道。

前提是域名已经在 Cloudflare 托管（`nslookup -type=NS <域名>` 能看到 `*.ns.cloudflare.com` 就对了；不在的话先改 nameserver）。

```bash
cloudflared tunnel login                 # 打印一个链接，浏览器授权一次
cloudflared tunnel create knowledge-kb   # 记下输出的 UUID
cloudflared tunnel route dns knowledge-kb kb.example.com
```

`/etc/cloudflared/config.yml`：

```yaml
tunnel: <UUID>
credentials-file: /root/.cloudflared/<UUID>.json
ingress:
  - hostname: kb.example.com
    service: http://127.0.0.1:18443
  - service: http_status:404
```

隧道落在 nginx 的 18443 上而不是直连 18082，这样限流、路径白名单、`/healthz` 在两条入口上行为一致。

```bash
cloudflared service install
systemctl enable --now cloudflared
```

最后把 `api.env` 的 `KB_MCP_PUBLIC_URL` 改成 `https://kb.example.com/mcp` 并重启 API，控制台的接入说明会自动跟着变。

**实测延迟**：免费版国内没有节点，隧道连到洛杉矶（日志里 `location=lax12`）。首次建连约 2.3 秒，其中 TLS 占 1.8 秒；同一连接上的后续请求约 0.5 秒。加上检索本身的一两秒，体感是「问一句等两秒」。要更快只有备案 + 国内直连。

延迟不能接受的话，18443 那条直连可以留着当快通道，隧道留给在家或外出的场景——两条入口共用同一套鉴权和权限，不用改任何配置。

**实测波动**：从国内家宽实测，同一域名有时 1.4 秒返回、有时 TLS 握手直接卡到超时（5 次里 3 次失败）。服务器自己访问是稳定的 1.4～1.9 秒，所以慢和断都发生在国内到 Cloudflare 边缘这一段。同事那边如果也这样，就让他改回 `http://<IP>:18443/mcp` 直连。

### 把后台也挂到域名上

后台和 MCP 共用 18443，靠 `Host` 区分：`kb.*` 走 MCP，`admin.*` 走控制台。

```bash
sudo bash scripts/configure-admin-entry.sh admin.example.com
```

脚本写 `/etc/nginx/sites-available/kb-admin`，然后在隧道 ingress 里加一条 `admin.example.com → http://127.0.0.1:18443` 并重启 `cloudflared`。

**为什么后台只有一道密码。** 最早在 nginx 上加了 HTTP Basic Auth，想挡扫描器。实测行不通：浏览器把 Basic 凭据放在 `Authorization` 头里，而控制台把自己 24 小时的会话令牌也放在同一个头里——一个请求只能带一个 `Authorization`，于是每个接口调用到 nginx 都被判 401。要么把令牌挪到自定义头，要么去掉 Basic Auth；去掉更合适，因为「一个统一的管理员账号」本来就不该是两个密码。

现在守着门口的是：scrypt 密码哈希、每个 IP 5 分钟内 8 次失败锁定、会话令牌只存 SHA-256 且 24 小时过期、nginx `120r/m` 限流。`scripts/configure-mcp-entry.sh` 里那条 `default_server` 不能删——`kb-admin` 按字母序先加载，没有它 `kb.*` 的请求会落到后台那个 server 块上。
