# Pulse MCP 优先实施方案

> 目标：让 Pulse 进入 **MCP-first** 架构，内部与外部能力统一按 MCP 接入，Brain 仅通过统一工具面执行能力。

---

## 1. 设计原则

1. **MCP 优先**：新能力默认先做 MCP server/tool，再接入 Brain。
2. **统一调用面**：无论内外部能力，都通过 ToolRegistry + MCP discovery + policy gate。
3. **安全先行**：所有可写动作必须具备 HITL（人工确认）和审计日志。
4. **可降级但不伪装**：降级路径必须显式标注 degraded，不将模拟结果伪装为真实执行。

---

## 2. 架构分层

- **L1 内核层**：`Brain` / `ToolRegistry` / `PolicyEngine` / `Memory`
- **L2 连接层**：`MCPClient` + `HttpMCPTransport` + `mcp_servers.yaml`
- **L3 能力层（MCP）**
  - 内部 MCP：`boss-platform`, `web-search`, `browser-use` 等
  - 外部 MCP：第三方服务（天气、航班、通知等）

---

## 3. 当前已落地（本轮）

- 新增 `config/mcp_servers.yaml`，支持 MCP server 清单配置。
- `create_app` 启动时优先读取 `mcp_servers.yaml` 构建 MCP transport。
- 招聘连接器 `BossPlatformConnector` 默认改为 **MCP 优先**（其次 OpenAPI）。
- 新增内部 `boss` MCP 能力入口：
  - `src/pulse/mcp_servers/boss_platform_server.py`（FastMCP）
  - `src/pulse/mcp_servers/boss_platform_gateway.py`（HTTP `/tools` + `/call` 网关）
  - `src/pulse/mcp_servers/_boss_platform_runtime.py`（统一运行时逻辑）
- `boss-platform` 已支持写动作执行模式：
  - `manual_required`：默认，要求人工执行
  - `log_only`：仅落审计，模拟成功
  - `browser`：使用持久化登录态进行真实浏览器执行（按 selector 配置）
- `boss-platform` 已提供会话自检与执行增强：
  - `check_login`：检测登录态是否可用
  - 写动作浏览器执行支持风险页识别、有限重试
  - `reply_conversation` 支持 `conversation_hint`（HR/公司/岗位）辅助定位会话
  - 反检测：统一使用 `patchright`（Playwright fork）在 Chromium 二进制层移除 CDP 可检测特征。不再维护 JS 层 `playwright_stealth` / `KILL_ZHIPIN_FRAME_JS` / `iframe-core` 拦截等 workaround。历史复盘与决策见 `docs/archive/debug-boss-antibot-postmortem.md`。
- `boss-platform` 已将读动作切换为 **浏览器优先真实抓取**：
  - `scan_jobs`：优先抓取职位列表页面，`web_search` 仅作降级兜底
  - `pull_conversations`：优先抓取聊天列表页面，本地 inbox 仅作降级兜底
  - 读动作策略可通过 `PULSE_BOSS_MCP_SCAN_MODE / PULSE_BOSS_MCP_PULL_MODE` 配置
  - 严格验收（`strict + skip_write`）已通过：`check_login -> scan_jobs -> pull_conversations` 全部走浏览器真实链路

---

## 4. 内部服务 MCP 化规范

每个内部服务都按以下模板接入：

1. `src/pulse/mcp_servers/<service>_server.py`：标准 MCP tools 定义。
2. `src/pulse/mcp_servers/_<service>_runtime.py`：纯业务逻辑（可测试）。
3. （可选）`src/pulse/mcp_servers/<service>_gateway.py`：提供 `/tools`、`/call` 给现有 HTTP transport。
4. 在 `config/mcp_servers.yaml` 注册服务。
5. 在 `PolicyEngine` 为写操作设置 `confirm`/`blocked` 策略。
6. 输出统一审计字段：`run_id`、`tool`、`args`、`status`、`error`、`latency_ms`。

---

## 5. 推荐推进顺序

1. `boss-platform`：先打通真实执行器（greet/reply）。
2. `email-tracker`：封装为 MCP tools（fetch/classify/ack）。
3. `intel-*`：封装 collect/query/ingest 为 MCP。
4. `memory/evolution`：关键写操作统一 MCP 化并加策略门控。

---

## 6. 启动示例（本地）

```powershell
# 启动 boss MCP HTTP 网关（给当前 HttpMCPTransport 用）
python -m pulse.mcp_servers.boss_platform_gateway

# 启动 Pulse API
uvicorn pulse.core.server:create_app --factory --host 127.0.0.1 --port 8010
```

可选：开启真实浏览器执行

```powershell
$env:PULSE_BOSS_MCP_GREET_MODE="browser"
$env:PULSE_BOSS_MCP_REPLY_MODE="browser"
$env:PULSE_BOSS_MCP_SCAN_MODE="browser_first"
$env:PULSE_BOSS_MCP_PULL_MODE="browser_first"
# 建议使用纯英文目录，避免浏览器在中文路径下启动失败
$env:PULSE_BOSS_BROWSER_PROFILE_DIR="$HOME/.pulse/boss_browser_profile"
```

MCP 端到端验收检查（建议每次改动后执行）

```powershell
python -m backend.mcp_boss_acceptance
# 兼容旧命令：
# python -m backend.mcp_boss_smoke
```

---

## 7. 验收标准（MCP-first）

- 默认配置下，Pulse 优先使用 `mcp_servers.yaml` 的 MCP 服务。
- 至少 1 条写操作链路具备：MCP 调用 + HITL + 审计。
- 对同一能力，Brain 不再依赖“模块内硬编码 provider”，而是可通过 MCP 服务替换实现。
