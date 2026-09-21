# Step Tool Proxy

自托管的 **OpenAI 兼容**代理，专为 [StepFun Plan API](https://api.stepfun.ai) 设计：修复 Grok Build 等客户端在流式 `tool_calls` 上的 400 错误、透传模型思考过程，并提供简易 WebUI 管理客户端密钥。

默认端口：**8722** · 纯标准库 · 单进程 `ThreadingHTTPServer`

---

## 这是什么

`step-tool-proxy` 是一个轻量本地/自托管代理：

- 对客户端暴露 OpenAI 风格接口：`/v1/chat/completions`、`/v1/models`
- 上游转发到 StepFun Plan（`STEP_UPSTREAM`），用 WebUI 里配置的**上游密钥**鉴权（可多把；`STEP_API_KEY` 仅在首次启动时作为种子）
- WebUI 用**管理密钥**登录，管理上游密钥、签发/撤销**客户端密钥**（明文可随时复制；每把上游密钥最多 3 个有效下游密钥）
- 默认开启 **FORCE_BUFFER**：在 `stream + tools` 场景下强制上游非流式，再回放完整 SSE `tool_calls`，避免客户端把空字段覆盖成 400
- 自动补齐思考过程：请求加 `reasoning_format=deepseek-style`，响应里 `reasoning` / `reasoning_content` 两个字段都吐，客户端不会再丢思考块

---

## 解决什么问题

### 1. 流式 tool_calls 400

Grok Build（及类似客户端）合并流式 `tool_calls` delta 时有问题：Step 续传 chunk 会带上空的 `id` / `type` / `name`，客户端用空值覆盖先前正确值，最终请求 Step 时报：

```text
400  tool_calls.id and tool_calls.type are required
```

**修复策略：**

| 模式 | 行为 |
|------|------|
| `FORCE_BUFFER=1`（默认） | 客户端要 stream+tools 时，上游改为非流式；再合成完整 SSE（每条 tool_call 带齐 id/type/name/arguments） |
| `FORCE_BUFFER=0` | 透传上游 SSE，但用 `sanitize_tool_delta` 去掉续传里的空 id/type/name，避免覆盖 |

### 2. 思考过程被吞

两个坑叠在一起：

1. StepFun 默认（`reasoning_format=general`）只返回 `reasoning` 字段；
2. Grok Build 用的 `@ai-sdk/xai` 只读 `reasoning_content`。

结果就是上游明明有思考，客户端一个字都收不到。代理的处理：

- 请求一律带上 `reasoning_format=deepseek-style`，让 StepFun 同时输出两个字段；
- 响应侧再把 `reasoning` / `reasoning_content` 互相补齐（`with_reasoning_aliases`），流式 SSE、非流式 JSON、FORCE_BUFFER 合成 SSE 三条路径都覆盖。

---

## 快速开始

需要 **Python 3.10+**（推荐 3.11/3.12），无第三方依赖。

```bash
cd step-tool-proxy

# 可选：首次启动用环境变量种下一把上游密钥（之后在 WebUI 里增删）
export STEP_API_KEY='sk-你的Step密钥'

# 可选：指定管理密钥；不设则首次启动自动生成并写入 data/master.token
export PROXY_MASTER_TOKEN='mtp_your_master'

# 启动
python3 -m step_tool_proxy
# 或
./scripts/run.sh
```

浏览器打开：http://127.0.0.1:8722/

健康检查：

```bash
curl -s http://127.0.0.1:8722/health
# {"ok":true}
```

用管理密钥签发客户端密钥（也可在 WebUI 操作）：

```bash
# 登录拿 Cookie，或直接 Bearer 管理密钥调管理 API
curl -s -X POST http://127.0.0.1:8722/api/tokens \
  -H "Authorization: Bearer $PROXY_MASTER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name":"grok-build","upstream_id":"上游密钥id"}'
# token 明文会一直保存在 WebUI，可随时复制；每把上游密钥最多 3 个有效下游密钥
```

客户端调用：

```bash
curl -s http://127.0.0.1:8722/v1/models \
  -H "Authorization: Bearer stp_你的客户端密钥"
```

---

## 环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `PROXY_HOST` | `0.0.0.0` | 监听地址 |
| `PROXY_PORT` | `8722` | 监听端口 |
| `STEP_UPSTREAM` | `https://api.stepfun.ai/step_plan/v1` | 上游 Base URL（无末尾 `/`） |
| `STEP_API_KEY` | （空） | 仅首次启动时种下一把上游密钥；之后以 WebUI / `data/upstreams.json` 为准 |
| `PROXY_MASTER_TOKEN` | （首次自动生成） | 管理密钥；未设置时生成并保存到 `data/master.token` |
| `FORCE_BUFFER` | `1` | 流式+tools 时缓冲重放 |
| `FORWARD_REASONING_HISTORY` | `0` | 是否把 `reasoning_content` 回传给上游，见下文 |
| `DATA_DIR` | `./data` | 密钥存储目录 |
| `DEBUG_DUMP` | `0` | 为 `1` 时把含 tools 的请求体落到 `data/`（默认关闭） |

示例文件：`config.example.env`

---

## WebUI 与客户端密钥

### 鉴权模型

1. **管理密钥（Master）**
   - 仅用于 WebUI 会话与 `/api/*` 管理接口
   - 登录后种 `stp_session` Cookie；也可用 `Authorization: Bearer <管理密钥>`
   - 未设置 `PROXY_MASTER_TOKEN` 时，首次启动自动生成并存到 `data/master.token`

2. **上游密钥（StepFun API Key）**
   - 代理访问 StepFun 时真正带上的 `sk-...`
   - 可添加多把；每把独立限额 3 个有效下游密钥
   - 撤销上游密钥会**级联撤销**它名下全部下游密钥
   - 存在 `data/upstreams.json`（明文，文件权限 600）

3. **客户端密钥（Client Token）**
   - 形态：`stp_` + 随机串，发给 Grok Build 等客户端
   - 每条绑定一把上游密钥，请求按绑定关系选上游
   - 明文保存在 `data/tokens.json`，WebUI 可随时复制
   - 升级前只存 sha256 的旧密钥仍能认证，但明文已丢失，无法再复制

管理密钥也可直接调 API 路由（方便调试，走第一把可用上游密钥），生产环境建议只给客户端密钥。

### WebUI 功能

- 粘贴管理密钥登录
- 查看状态：端口、上游主机、FORCE_BUFFER、思考回传、上游/下游密钥数
- 修改上游地址、思考回传开关（立即生效，写入 `data/settings.json`）
- 添加 / 撤销上游密钥
- 选择归属上游后签发客户端密钥；列表里随时复制或撤销

### 管理 API 摘要

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/login` | `{"master_token":"..."}` → Set-Cookie |
| `POST` | `/api/logout` | 清除会话 |
| `GET` | `/api/status` | 状态面板 JSON |
| `GET` | `/api/settings` | 当前上游地址 / 思考回传 |
| `POST` | `/api/settings` | `{"step_upstream","forward_reasoning_history"}` 保存 |
| `GET` | `/api/upstreams` | 上游密钥列表（掩码） |
| `POST` | `/api/upstreams` | `{"name","key"}` 添加 |
| `POST` | `/api/upstreams/{id}/revoke` | 撤销（级联下游） |
| `GET` | `/api/tokens` | 列表（含明文，供复制） |
| `POST` | `/api/tokens` | `{"name","upstream_id"}` 签发 |
| `POST` | `/api/tokens/{id}/revoke` | 撤销 |

除 login/logout 外，均需管理密钥会话或 Bearer。

---

## Grok Build 配置示例

在 Grok Build 的模型配置中指向本代理，并用 WebUI 签发的客户端密钥：

```toml
[model.step5]
model = "step-5-preview"
base_url = "http://127.0.0.1:8722/v1"
# api_key = 从 WebUI 签发的客户端密钥（stp_...）
```

确保本机代理已启动且 `FORCE_BUFFER=1`（默认）。若仍异常，可临时设 `FORCE_BUFFER=0` 走 sanitize 路径对比。

### 思考过程什么时候会出现在界面

Grok Build 侧需要 `ui.show_thinking_blocks` 不是 `false`（默认开）。开启后，`reasoning_content` 会以思考块形式流式显示，`content` 照常输出。

### 关于 FORWARD_REASONING_HISTORY

默认 `0`（不回传）。原因是：

- Grok Build 多轮请求时**不会**把 `reasoning_content` 塞回 assistant 消息，开了也用不上；
- StepFun 对"带 `tool_calls` 的 assistant 消息又带 `reasoning_content`"会报错，开了反而可能 400。

只有当你的客户端确实会在多轮里回传 `reasoning_content` 时才打开它。

---

## Docker

```bash
export STEP_API_KEY='sk-...'
export PROXY_MASTER_TOKEN='mtp_...'   # 可选

docker compose up -d --build
# 或
docker build -t step-tool-proxy .
docker run --rm -p 8722:8722 \
  -e STEP_API_KEY="$STEP_API_KEY" \
  -e PROXY_MASTER_TOKEN="$PROXY_MASTER_TOKEN" \
  -v "$(pwd)/data:/app/data" \
  step-tool-proxy
```

数据卷挂载 `./data`，重启不丢密钥。

---

## 安全注意

- **不要**把 `STEP_API_KEY`、`PROXY_MASTER_TOKEN`、`data/master.token`、`data/tokens.json`、`data/upstreams.json`、`data/settings.json` 提交进 Git（已在 `.gitignore`）
- 客户端密钥明文可随时在 WebUI 复制；泄露请立即撤销
- 默认监听 `0.0.0.0`：若暴露公网，务必配合防火墙 / 反向代理 TLS，并使用强管理密钥
- 本代理不记录完整请求体（除非 `DEBUG_DUMP=1`）
- 会话 Cookie 为进程内存态，重启后需重新登录 WebUI

---

## 故障排查

| 现象 | 排查 |
|------|------|
| `/health` 不通 | 确认进程已起、端口 `8722` 未被占用 |
| WebUI 登录失败 | 检查 `PROXY_MASTER_TOKEN` 或 `data/master.token` 内容是否一致 |
| `/v1/*` 返回 401 | 是否用了客户端密钥 / 管理密钥；注意 `Bearer ` 前缀 |
| `/v1/*` 返回 503 | 该客户端密钥绑定的上游密钥未配置或已撤销 |
| 上游 401/403 | Step 密钥无效或过期；检查 `STEP_UPSTREAM` |
| 仍出现 tool_calls 400 | 确认 `FORCE_BUFFER=1`；看启动日志是否走 FORCE_BUFFER 路径 |
| 界面上看不到思考块 | 客户端是否开思考显示；上游返回里有没有 `reasoning` 字段 |
| Docker 丢密钥 | 是否挂载了 `data` 卷 |

日志打到 stdout，格式类似：

```text
21:35:01 step-tool-proxy v1.2.0 listening http://0.0.0.0:8722 -> https://api.stepfun.ai/step_plan/v1 FORCE_BUFFER=1
```

---

## 目录结构

```text
step-tool-proxy/
  README.md
  LICENSE
  .gitignore
  GIT_GUIDE.md
  requirements.txt
  config.example.env
  Dockerfile
  docker-compose.yml
  scripts/run.sh
  data/.gitkeep
  static/index.html
  step_tool_proxy/
    __init__.py
    __main__.py
    config.py
    settings.py
    auth.py
    store.py
    proxy.py
    web.py
    server.py
```

---

## License

MIT © 2026 珂夜 / ElaraKaya
