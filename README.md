# Step Tool Proxy

自托管的 **OpenAI 兼容**代理，支持两类上游节点： [StepFun Plan API](https://api.stepfun.ai) 和 **Claude Code / Anthropic SDK 节点**（Anthropic Messages API）：修复 Grok Build 等客户端在流式 `tool_calls` 上的 400 错误、透传模型思考过程，按节点类型把上游响应转换成下游的 `/v1/chat/completions` 或 `/v1/responses`，并提供简易 WebUI 管理客户端密钥。

默认端口：**8722** · 纯标准库 · 单进程 `ThreadingHTTPServer`

---

## 这是什么

`step-tool-proxy` 是一个轻量本地/自托管代理：

- 对客户端暴露 OpenAI 风格接口：`/v1/chat/completions`、`/v1/responses`、`/v1/models`
- 上游转发到 **StepFun Plan**（`STEP_UPSTREAM`）或 **Claude Code / Anthropic SDK 节点**（`ANTHROPIC_UPSTREAM`），用 WebUI 里配置的**上游密钥**鉴权（可多把；`STEP_API_KEY` 仅在首次启动时作为种子）
- WebUI 用**管理密钥**登录，管理上游密钥、签发/撤销**客户端密钥**（明文可随时复制；每把上游密钥最多 3 个有效下游密钥）
- 默认开启 **FORCE_BUFFER**：在 `stream + tools` 场景下强制上游非流式，再回放完整 SSE `tool_calls`，避免客户端把空字段覆盖成 400
- 自动补齐思考过程：请求加 `reasoning_format=deepseek-style`，响应里 `reasoning` / `reasoning_content` 两个字段都吐，客户端不会再丢思考块

---

## 上游节点类型

添加上游密钥时在 WebUI 里选择节点类型，客户端密钥按绑定关系走对应路径：

| 类型 | 上游协议 | 默认地址 | 说明 |
|------|----------|----------|------|
| `stepfun` | OpenAI 兼容 `/chat/completions` | `STEP_UPSTREAM` | 原有 StepFun Plan 节点；可按节点填地址覆盖 |
| `anthropic` | Anthropic Messages `/v1/messages` | `ANTHROPIC_UPSTREAM`（默认 `https://api.stepfun.ai/step_plan`） | Claude Code / Anthropic SDK 节点：StepFun「Step Plan Access Info」页面里 *Claude Code / Anthropic SDK Base URL* 的那个入口，也兼容第三方 Anthropic 中转；鉴权用 `x-api-key` + `anthropic-version`，可按节点填地址覆盖，留空用默认 |

下游接口对客户端不变，统一是 `/v1/chat/completions` 和 `/v1/responses`；上游是 Anthropic 节点时，代理自动做格式转换：

| 下游接口 | 上游 StepFun 节点 | 上游 Anthropic 节点 |
|----------|------------------|---------------------|
| `/v1/chat/completions` | 原有 FORCE_BUFFER / sanitize / 思考补齐逻辑，不变 | 请求转 `/v1/messages`（system、messages、tools、tool_choice、stop_sequences 等逐项映射），响应 JSON / SSE 转回 chat.completions |
| `/v1/responses` | 请求降级为 chat.completions 转发，响应转回 Responses 事件流 | 同上，中间再走 Anthropic Messages 转换 |

转换细节：

- **请求**：`system`/`developer` 消息并入 Anthropic `system`；`tool` 消息转 `tool_result` 块；assistant 的 `tool_calls` 转 `tool_use` 块；连续同角色消息自动合并（Anthropic 要求 user/assistant 交替）；`max_tokens` 缺失时按 `ANTHROPIC_MAX_TOKENS` 补齐（Anthropic 必填）。
- **响应**：`text` 块 → `content`，`thinking` 块 → `reasoning_content` / `reasoning` 双字段，`tool_use` 块 → `tool_calls`；`stop_reason`（`end_turn` / `max_tokens` / `tool_use` / `stop_sequence`）映射为 `finish_reason`。
- **流式**：Anthropic SSE 事件（`message_start`、`content_block_delta`、`input_json_delta` 等）逐条转成 chat.completions chunk；下游要 Responses 格式时再转成 `response.output_text.delta`、`response.function_call_arguments.delta` 等事件。
- **`/v1/models`**：Anthropic 节点的模型列表转成 OpenAI `{"object":"list"}` 形状。

`FORCE_BUFFER` 只作用于 StepFun 节点路径；Anthropic 节点天然给完整 tool_call 事件，无需缓冲重放。

### 流式：什么时候逐字返回，什么时候整段返回

代理对上游 SSE 是**逐事件转发**的（HTTP/1.1 chunked），思考和正文会一个一个字到达：

| 场景 | 行为 |
|------|------|
| StepFun 节点 · `stream` 不带 tools | 逐事件转发，思考/正文逐字到达 |
| StepFun 节点 · `stream` + tools · `FORCE_BUFFER=0` | 逐事件转发（sanitize 去掉空 id/type/name） |
| StepFun 节点 · `stream` + tools · `FORCE_BUFFER=1`（默认） | **整段到达**：该模式刻意让上游非流式以拿到完整 tool_call，拿到完整响应后才合成 SSE 下发 |
| Anthropic 节点 · `stream` | 逐事件转发，思考/正文/tool 参数逐条到达 |
| 非 `stream` 请求 | 本来就是一次性返回 |

也就是说：StepFun 节点 + tools + 默认 `FORCE_BUFFER=1` 的组合会看不到逐字效果，想要逐字就把 `FORCE_BUFFER=0`（仍会修空字段 400）；Anthropic 节点默认就是逐字。

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

# 可选：Anthropic 节点默认地址（StepFun「Claude Code / Anthropic SDK Base URL」，可被 WebUI 设置或每节点地址覆盖）
export ANTHROPIC_UPSTREAM='https://api.stepfun.ai/step_plan'

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

客户端调用（chat/completions 或 responses 均可，取决于客户端支持哪个）：

```bash
curl -s http://127.0.0.1:8722/v1/models \
  -H "Authorization: Bearer stp_你的客户端密钥"

curl -s http://127.0.0.1:8722/v1/chat/completions \
  -H "Authorization: Bearer stp_你的客户端密钥" \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-sonnet-4-5","messages":[{"role":"user","content":"hi"}]}'
```

---

## 环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `PROXY_HOST` | `0.0.0.0` | 监听地址 |
| `PROXY_PORT` | `8722` | 监听端口 |
| `STEP_UPSTREAM` | `https://api.stepfun.ai/step_plan/v1` | StepFun 节点默认 Base URL（无末尾 `/`）；可在 WebUI 改 |
| `ANTHROPIC_UPSTREAM` | `https://api.stepfun.ai/step_plan` | Anthropic 节点默认 Base URL（即 StepFun 页面上的 *Claude Code / Anthropic SDK Base URL*，不含 `/v1/messages`）；可在 WebUI 改或按节点覆盖；别名 `ANTHROPIC_BASE_URL` |
| `ANTHROPIC_MAX_TOKENS` | `8192` | Anthropic 请求缺失 `max_tokens` 时的补齐值（该字段上游必填） |
| `STEP_API_KEY` | （空） | 仅首次启动时种下一把上游密钥；之后以 WebUI / `data/upstreams.json` 为准 |
| `PROXY_MASTER_TOKEN` | （首次自动生成） | 管理密钥；未设置时生成并保存到 `data/master.token` |
| `FORCE_BUFFER` | `1` | 流式+tools 时缓冲重放（仅 StepFun 节点路径） |
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

2. **上游密钥（API Key，按节点类型）**
   - 代理访问上游时真正带上的密钥，可添加多把；每把独立限额 3 个有效下游密钥
   - 节点类型二选一：`stepfun`（`sk-...`，`Authorization: Bearer`）或 `anthropic`（StepFun `sk-...` 或中转的 `sk-ant-...`，`x-api-key` + `anthropic-version`）
   - Anthropic 节点填 StepFun「Step Plan Access Info」里的 *Claude Code / Anthropic SDK Base URL*（默认 `https://api.stepfun.ai/step_plan`，第三方中转填中转地址），留空走 `ANTHROPIC_UPSTREAM`
   - 撤销上游密钥会**移除该节点**（含明文密钥），并**级联撤销**它名下全部下游密钥
   - 存在 `data/upstreams.json`（明文，文件权限 600）；旧记录无 `type` 字段时按 `stepfun` 处理；历史遗留的软撤销记录会在启动时清理

3. **客户端密钥（Client Token）**
   - 形态：`stp_` + 随机串，发给 Grok Build 等客户端
   - 每条绑定一把上游密钥，请求按绑定关系选上游节点与协议
   - 明文保存在 `data/tokens.json`，WebUI 可随时复制
   - 升级前只存 sha256 的旧密钥仍能认证，但明文已丢失，无法再复制

管理密钥也可直接调 API 路由（方便调试，走第一把可用上游密钥），生产环境建议只给客户端密钥。

### WebUI 功能

- 粘贴管理密钥登录
- 查看状态：端口、上游主机、Anthropic 节点数、FORCE_BUFFER、思考回传、上游/下游密钥数
- 修改上游地址、Anthropic 节点默认地址、思考回传开关（立即生效，写入 `data/settings.json`）
- 添加上游密钥：先选节点类型（StepFun Plan / Claude Code / Anthropic SDK），Anthropic 节点可填节点地址（StepFun 默认 `https://api.stepfun.ai/step_plan`，代理自动补 `/v1/messages`）
- 撤销上游密钥：从列表移除该节点，其名下下游密钥同时失效
- 选择归属上游后签发客户端密钥；列表里随时复制或撤销

### 管理 API 摘要

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/login` | `{"master_token":"..."}` → Set-Cookie |
| `POST` | `/api/logout` | 清除会话 |
| `GET` | `/api/status` | 状态面板 JSON |
| `GET` | `/api/settings` | 当前上游地址 / Anthropic 默认地址 / 思考回传 |
| `POST` | `/api/settings` | `{"step_upstream","anthropic_upstream","forward_reasoning_history"}` 保存 |
| `GET` | `/api/upstreams` | 上游密钥列表（掩码，含类型与节点地址） |
| `POST` | `/api/upstreams` | `{"name","key","type","base_url"}` 添加；`type` 为 `stepfun` 或 `anthropic` |
| `POST` | `/api/upstreams/{id}/revoke` | 移除该上游节点（级联撤销其下游密钥） |
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

Anthropic 节点同理，把 `model` 换成上游支持的 Claude 模型名即可，客户端无需其它改动。

确保本机代理已启动且 `FORCE_BUFFER=1`（默认）。若仍异常，可临时设 `FORCE_BUFFER=0` 走 sanitize 路径对比。

### 思考过程什么时候会出现在界面

Grok Build 侧需要 `ui.show_thinking_blocks` 不是 `false`（默认开）。开启后，`reasoning_content` 会以思考块形式流式显示，`content` 照常输出。

### 关于 FORWARD_REASONING_HISTORY

默认 `0`（不回传）。原因是：

- Grok Build 多轮请求时**不会**把 `reasoning_content` 塞回 assistant 消息，开了也用不上；
- StepFun 对"带 `tool_calls` 的 assistant 消息又带 `reasoning_content`"会报错，开了反而可能 400。

只有当你的客户端确实会在多轮里回传 `reasoning_content` 时才打开它。

Anthropic 节点路径不回传思考块：Anthropic 要求 thinking 块携带原始签名，代理统一在转发前丢弃，避免上游 400。

---

## Docker

```bash
export STEP_API_KEY='sk-...'
export PROXY_MASTER_TOKEN='mtp_...'   # 可选
export ANTHROPIC_UPSTREAM='https://api.stepfun.ai/step_plan'   # 可选

docker compose up -d --build
# 或
docker build -t step-tool-proxy .
docker run --rm -p 8722:8722 \
  -e STEP_API_KEY="$STEP_API_KEY" \
  -e PROXY_MASTER_TOKEN="$PROXY_MASTER_TOKEN" \
  -e ANTHROPIC_UPSTREAM="$ANTHROPIC_UPSTREAM" \
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
| 上游 401/403（StepFun） | Step 密钥无效或过期；检查 `STEP_UPSTREAM` |
| 上游 401/403（Anthropic） | 密钥无效或节点地址错；确认节点类型选的是 Anthropic 且地址指向提供 `/v1/messages` 的中转 |
| Anthropic 上游 400 `max_tokens` | 客户端没带 `max_tokens` 且 `ANTHROPIC_MAX_TOKENS` 不合适，调大后重试 |
| 仍出现 tool_calls 400 | 确认 `FORCE_BUFFER=1`；看启动日志是否走 FORCE_BUFFER 路径 |
| 界面上看不到思考块 | 客户端是否开思考显示；上游返回里有没有 `reasoning` 字段 |
| Docker 丢密钥 | 是否挂载了 `data` 卷 |

日志打到 stdout，格式类似：

```text
21:35:01 step-tool-proxy v1.4.0 listening http://0.0.0.0:8722 -> https://api.stepfun.ai/step_plan/v1 | anthropic -> https://api.stepfun.ai/step_plan FORCE_BUFFER=1
```

---

## 目录结构

```text
step-tool-proxy/
  README.md
  LICENSE
  .gitignore
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
    anthropic_api.py
    responses_api.py
    proxy.py
    web.py
    server.py
```

---

## License

MIT © 2026 OnsWayn
