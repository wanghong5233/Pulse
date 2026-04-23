# Pulse 最终收尾清单

> 目标：在 `M0-M7` 架构重构完成后，明确 **“可运行架构基线”** 与 **“完整产品功能”** 之间的剩余差距。  
> 判定口径：只有当 `P0` 项全部完成，才可以对外宣称 **Pulse 第一版完整产品已落实到位**。

---

## 0. 当前结论

- `M0-M7` 已完成，说明 Pulse 已具备统一架构、模块体系、Brain、Memory、Evolution、Skill Generator 等核心骨架。
- 当前代码已经从 OfferPilot 旧实现中显著抽离，且 `Channel -> Brain`、`Ring1/2/3`、`intel_*`、`email_tracker`、Phase1 去桥接化等关键基础改造已落地。
- 但当前版本仍然应定义为：**可运行的产品化基线**，而不是 **完整终态产品**。
- 目前最大差距不在“有没有模块”，而在“是否具备真实执行闭环、标准协议兼容、生产级存储与发布面收口”。

---

## 1. P0 必须完成

## P0-1 招聘平台真实执行闭环

**当前缺口**

- `boss_greet` 目前是 **通用 Web 搜索 + 打分 + 本地日志**，不是招聘平台专用连接器。
- `boss_chat` 目前是 **本地 inbox / 手工 ingest / 规则动作建议**，不是真实聊天页拉取与执行。
- 当前 Phase1 的“真实”能力已经去掉 legacy bridge，但还没有收敛成真正的平台执行器。

**涉及位置**

- `src/pulse/modules/boss_greet/module.py`
- `src/pulse/modules/boss_chat/module.py`

**必须补齐**

- 引入招聘平台专用 provider/connector，替代通用 `search_web`。
- 真正实现职位抓取、职位详情读取、去重、配额、失败重试、速率限制。
- 真正实现“发起沟通 / 回复消息 / 标记已处理 / 回滚重试”执行动作。
- 将聊天动作从“建议”升级为“可执行动作 + HITL 门控 + 审计”。
- 明确登录态、Cookie、过期恢复、会话失效和风控异常路径。

**完成标准**

- 单独配置登录态后，可稳定完成真实职位扫描。
- 可对真实职位执行一次打招呼，并记录完整审计。
- 可从真实会话源拉取消息并执行受控回复。
- 无需依赖旧 OfferPilot 后端或手工注入 jsonl 才能运行。

**当前进度（2026-03-14）**

- 已完成：
  - 引入 `BossPlatformConnector` 并默认 MCP 优先（`mcp -> openapi -> web_search`）。
  - `boss-platform` MCP runtime 新增浏览器执行器与 `check_login`，支持风险识别、重试、审计。
  - `boss-platform` 网关调用已切到线程池执行，避免 Sync Playwright 与事件循环冲突。
  - 浏览器 profile 默认目录已调整为 `~/.pulse/boss_browser_profile`，规避中文工作目录下 Chromium 启动异常。
  - `scan_jobs` 已切换为 **浏览器真实抓取优先**，并补充多入口抓取（职位页直达、聊天页跳转、聊天线索回退），`strict` 验收下已稳定返回 `source=boss_mcp_browser_scan`。
  - `pull_conversations` 已切换为 **浏览器真实抓取优先**，并增加页面文本回退解析；在 strict 验收中已能返回 `source=boss_mcp_browser_chat`。
  - `boss_greet` / `boss_chat` 已支持可执行动作 + HITL + 审计闭环。
- 待完成：
  - 写动作 strict（`greet/reply`）仍需在真实账号可执行且风控可控时完成一次稳定通过记录，作为 P0-1 最终验收证据。
  - 当前默认仍是 `manual_required`（安全优先），需要在明确授权场景切到 `browser` 并完成受控验收。

---

## P0-2 前端彻底重写并对齐当前 Pulse API

**当前缺口**

- 前端仍包含旧 V1 / 已下线能力入口。
- 仍请求旧接口，例如 `/api/jd/analyze`、材料审批等。
- 当前后端与当前前端默认组合并不能形成一致产品体验。

**涉及位置**

- `frontend/src/app/page.tsx`

**必须补齐**

- 删除所有已下线或未实现的旧页面入口。
- 基于当前 `Pulse` API 重写主工作台。
- 前端至少覆盖：`Brain`、`Channel`、`Memory`、`Evolution`、`intel_*`、`boss_*`、`email_tracker` 当前真实能力。
- 所有页面状态、按钮、表单、错误提示与当前 API 契约一致。

**完成标准**

- 默认前端打开后，不再调用任何旧接口。
- 前端与当前后端联调全绿。
- 用户可以通过前端完成当前 Pulse 第一版的核心操作路径。

---

## P0-3 MCP 标准化，而不是自定义兼容层

**当前缺口**

- 当前已经有 Ring3 和外部工具门控，但 MCP 实现仍偏自定义网关。
- 当前 `MCPServerAdapter` 仍是 `MCP-like`，不是标准 MCP Server。
- 当前 transport 仅支持自定义 `/tools`、`/call`，没有 `mcp_servers.yaml` 和标准传输协议矩阵。

**涉及位置**

- `src/pulse/core/mcp_server.py`
- `src/pulse/core/mcp_transport_http.py`
- `src/pulse/core/mcp_client.py`

**必须补齐**

- 支持标准 MCP 配置方式，例如 `mcp_servers.yaml`。
- 支持标准 MCP 传输而非仅自定义 HTTP 网关。
- 对外提供标准 MCP Server 暴露自身工具。
- 对内将外部 MCP 工具纳入标准发现、调用、错误处理与权限控制链。

**完成标准**

- 至少 1 个标准外部 MCP Server 可被发现、调用、门控。
- Pulse 自身工具可被标准 MCP Client 连接与调用。
- 不再需要依赖“自定义兼容层文档解释”才能接入。

---

## P0-4 Skill Generator 真实化

**当前缺口**

- 生成逻辑仍以模板渲染为主，默认输出 `generated-mock`。
- 当前更像“可热加载演示技能”，不是真正的“自然语言 -> 新能力”。

**涉及位置**

- `src/pulse/core/skill_generator.py`
- `src/pulse/core/sandbox.py`

**必须补齐**

- 接入真实 LLM 代码生成链路。
- 为技能生成增加最小规格化设计：输入需求 -> 能力分析 -> 工具接口 -> 代码生成 -> 安全检查 -> 沙箱运行 -> 激活。
- 生成后的能力必须依赖真实 provider/API，而不是固定 mock 数据。
- 对生成结果补最小 smoke test，而非只校验 AST。

**完成标准**

- 用户自然语言描述一个新需求，系统能生成并激活真实可用工具。
- 默认生成结果不再包含 `generated-mock`。
- 失败时能给出清晰的安全或运行错误，而不是悄悄退化为模板演示。

---

## P0-5 Memory / Evolution 存储产品化

**当前缺口**

- `RecallMemory`、`ArchivalMemory` 仍以本地文件为主。
- 向量检索默认仍偏 in-memory / deterministic embedding。
- 与架构文档中的 PostgreSQL facts、向量索引、时序事实图存在明显差距。

**涉及位置**

- `src/pulse/core/memory/recall_memory.py`
- `src/pulse/core/memory/archival_memory.py`
- `src/pulse/core/storage/vector.py`

**必须补齐**

- 将 Recall / Archival 迁移到真实持久化层。
- 明确事实表结构、迁移脚本、索引、备份、恢复策略。
- 向量检索使用真实持久化集合或真实向量数据库路径。
- 为长期记忆增加容量治理、历史裁剪、重建索引工具。

**完成标准**

- 服务重启后，记忆、事实、规则版本、审计均可稳定恢复。
- 语义检索结果不依赖进程内对象。
- 文档与代码中对存储形态的描述一致。

**当前进度（2026-03-14）**

- 已完成：
  - `RecallMemory` 已切换为 PostgreSQL `conversations` 主存 + Chroma 向量检索。
  - `ArchivalMemory` 已切换为 PostgreSQL `facts` 主存 + Chroma 向量检索。
  - 服务初始化已接入统一 `StorageEngine(DatabaseEngine + LocalVectorStore)`，不再走本地 sqlite/jsonl 记忆后端。
  - `init_db.sql` 已补齐 `conversations / tool_calls / pipeline_runs / corrections / facts` 核心表结构。
- 待完成：
  - 向量检索层仍是本地 deterministic embedding，尚未接入 PostgreSQL/专用向量库持久化索引。
  - 仍需补数据库迁移脚本、备份恢复 SOP、容量治理与重建索引工具。

---

## P0-6 可观测性闭环

**当前缺口**

- `EventBus` 已存在，但业务模块尚未真正接成统一事件流。
- 目前缺少一个覆盖 Brain、Module、外部工具、通知、记忆写入的统一观测面。

**涉及位置**

- `src/pulse/core/events.py`
- `src/pulse/core/server.py`
- `src/pulse/modules/*`

**必须补齐**

- 为核心执行路径接入事件上报。
- 统一记录：输入、路由、决策、工具调用、结果、错误、耗时、审计 ID。
- 至少提供服务内可用的观察接口或流式事件出口。

**完成标准**

- 任意一次 Brain/Module 执行可以被完整追踪。
- 错误、外部调用、关键状态变化有统一事件记录。
- 发布后能靠观测信息定位问题，而不是只靠 print/log。

**当前进度（2026-03-14）**

- 已完成：
  - `EventBus` 扩展为可全局订阅，并引入 `InMemoryEventStore` 形成统一事件时间线。
  - `channel -> route/policy -> brain -> tool -> mcp` 主链路已接入统一事件上报（含 `trace_id`、耗时、错误、结果摘要）。
  - 新增观测接口：
    - `GET /api/system/events/recent`
    - `GET /api/system/events/stats`
    - `POST /api/system/events/clear`
  - `api/brain/run` 与 `api/mcp/call` 已返回 `trace_id` + `latency_ms`，便于串联追踪。
- 待完成：
  - 事件流目前为进程内内存缓冲，尚未接入持久化/外部观测后端（如时序库、日志平台）。
  - 仍需把更多模块级业务事件（例如通知与长期记忆写入细分事件）补齐为统一 schema。

---

## P0-7 发布面与仓库外围收口

**当前缺口**

- 仓库外围仍残留大量旧项目、旧路径、旧品牌、旧脚本、旧配置示例。
- 这会直接影响新用户理解、部署和发布可信度。

**涉及位置**

- `README_EN.md`
- `scripts/*.sh`
- `.env.example`
- `Makefile`
- 旧的 OfferPilot / OpenClaw 说明文档

**必须补齐**

- 清理或归档旧文档，区分“历史参考”与“当前产品”。
- 修复所有旧路径、旧数据库名、旧服务名。
- 让英文 README 与当前 Pulse 状态一致。
- 对外默认入口只保留当前产品相关文档与命令。

**完成标准**

- 新用户只看根 README 和 docs，不会被旧栈误导。
- 默认脚本、配置、启动命令与当前 Pulse 一致。
- 旧 OfferPilot/OpenClaw 文档若保留，必须明确标注为历史资料。

**当前进度（2026-03-14）**

- 已完成：
  - `.env.example` 顶部模板说明已从 OfferPilot 更新为 Pulse。
  - `.env.example` 默认数据库示例已从 `offerpilot` 切换为 `pulse`。
  - 前端默认演示会话标识由 `offerpilot-demo` 更新为 `pulse-demo`。
  - `README_EN.md` 已重写为当前 Pulse 架构与启动入口，不再描述 OfferPilot 旧栈。
  - `scripts/*`（`start/setup/pulsectl/demo/openclaw` 相关脚本）已去除旧路径与旧品牌默认值。
  - `Makefile` 已改为项目相对路径调用，不再依赖固定 OfferPilot 绝对路径。
  - `infra/docker-compose.yml` 已更新为 Pulse 命名与当前 API 入口。
  - 新增 `docs/README.md`，明确“当前产品文档 / 历史参考文档”边界。
- 待完成：
  - 将历史参考文档进一步迁移到独立 `docs/archive/`（可选优化，不影响当前发布入口）。

---

## 2. P1 应该完成

## P1-1 Docker / Compose 生产化

- 在 `docker-compose.yml` 中补齐真实持久化依赖与必要服务。
- 让 Compose 结果不只是启动 `pulse-api` 壳，而是可形成完整最小产品环境。

## P1-2 Notification / Webhook 真集成

- 将 `ConsoleNotifier` 覆盖为真实通知后端。
- 给关键链路补真实 Feishu / Webhook 冒烟验证。

## P1-3 Weather / Flight Provider 加固

- `weather.current` 已接真实天气源，但仍需容灾和 provider 抽象。
- `flight.search` 当前依赖外部配置提供者，需补专用 provider 约定与测试。

## P1-4 测试升级为产品级回归

- 增加前后端联调测试。
- 增加标准 MCP 联调测试。
- 增加真实邮箱、真实 provider 的可选集成测试。
- 将关键能力从“结构正确”提升为“真实路径可验证”。

## P1-5 数据迁移与运维工具

- 增加数据迁移脚本、索引重建工具、备份恢复脚本。
- 为关键本地文件态能力提供迁移到生产存储的路径。

---

## 3. P2 可后置

## P2-1 多平台扩展

- 拉勾、猎聘等新的平台 provider。

## P2-2 本地模型 / 离线模式

- Ollama 或本地推理链路。

## P2-3 移动端与多端体验

- 当前前端稳定后，再考虑移动端收口。

## P2-4 更强评测体系

- 自动评测、离线数据集、回放测试、DPO 数据质量看板。

---

## 4. 推荐执行顺序

1. 先做 `P0-1`：招聘平台真实执行闭环。
2. 再做 `P0-2`：前端重写与 API 收口。
3. 然后做 `P0-5`：Memory / Evolution 存储产品化。
4. 同步推进 `P0-3`：标准 MCP。
5. 再推进 `P0-4`：Skill Generator 真实化。
6. 最后完成 `P0-6` 与 `P0-7`：观测与发布面收尾。

---

## 5. 通过标准

满足以下全部条件，才可以认定 **Pulse 第一版完整产品功能已落实到位**：

- `P0` 全部完成
- 默认前端与默认后端完全匹配
- 至少一条真实招聘平台执行链可稳定跑通
- 至少一条真实外部 MCP 工具链可稳定跑通
- Skill Generator 默认不再生成 mock 技能
- Memory / Evolution 使用真实持久化链路
- README / docs / scripts / env 样例全部与当前 Pulse 一致

---

## 6. 一句话定义当前状态

当前 Pulse 不是“未完成的半成品”，而是：

> **架构底座已经成型、核心链路已经打通、但仍需完成最后一轮产品化收尾的真实系统。**
