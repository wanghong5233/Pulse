# Pulse 文档导航

## 目录结构

```
docs/
├── README.md                       本文件
├── code-review-checklist.md        编码 / 测试宪法（根级,代码中大量引用）
├── Pulse实施计划.md                M0-M7 里程碑（根级,规则引用）
│
├── Pulse架构方案.md                全局架构结论
├── V2架构设计.md                   架构决策演进（Why）
├── Pulse命名规则.md                品牌/包名/CLI 命名
│
├── Pulse-内核架构总览.md           内核分层审计基线
├── Pulse-AgentRuntime设计.md       AgentOS Kernel 设计
├── Pulse-MemoryRuntime设计.md      MemoryRuntime 主设计
├── Pulse-DomainMemory与Tool模式.md Domain memory / tool 契约
├── Pulse-MCP优先实施方案.md        MCP-first 实施基线
│
├── adr/                            Architecture Decision Records
├── dom-specs/                      外部页面 DOM 快照（selector fixture）
├── engineering/                    工程化实践（测试/代码审查...）
├── modules/                        按业务模块组织的设计文档
│   └── job/                        Job 模块专属架构
├── handoff/                        单次会话交接、快照类文件
└── archive/                        已完成的阶段性审查/清单
```

## 各分区职责

| 分区 | 放什么 | 不放什么 | 负责人 |
|---|---|---|---|
| 根级 `*.md` | 项目级、被代码/规则引用的长期文档 | 某次会话产物、模块专属文档 | 架构维护者 |
| `adr/` | 一次独立的架构决策（immutable） | 普通设计说明 | 提案人 |
| `dom-specs/` | 真实外部页面 dump + selector README | 手搓 HTML | 谁抓谁存 |
| `engineering/` | 跨模块复用的工程实践（测试/审查/观测/CI） | 单模块实现细节 | 工程化维护者 |
| `modules/<mod>/` | 单个 module 的设计、表、契约 | core 级内容 | 模块 owner |
| `handoff/` | 按日期命名的会话交接、memory 快照、面试草稿 | 长期文档 | 生成者（短期） |
| `archive/` | 已关闭的审查清单、历史方案 | 仍需维护的内容 | 不维护 |

**新增文档决策流程**：

1. 被代码 `docs/xxx.md` 形式长期引用 → 根级
2. 一次有明确 id 的架构决策 → `adr/ADR-NNN-<title>.md`
3. 某个 `modules/<mod>/` 下代码用到的设计 → `modules/<mod>/`
4. 通用工程实践（测试 / code review / CI） → `engineering/`
5. 日期驱动的临时产物（交接、复盘草稿） → `handoff/YYYY-MM-DD-<slug>.md`
6. 过期但想留档 → `archive/`

---

## A. 全局架构与演进

| 文档 | 视角 | 内容 |
|---|---|---|
| [V2架构设计.md](./V2架构设计.md) | Why | OfferPilot → 通用 Agent 的决策推导 |
| [Pulse架构方案.md](./Pulse架构方案.md) | What | 模块分层、阶段路线、技术选型 |
| [Pulse实施计划.md](./Pulse实施计划.md) | How | M0-M7 里程碑与验收条件 |

## B. 内核设计三件套

| 文档 | 角色 |
|---|---|
| [Pulse-AgentRuntime设计.md](./Pulse-AgentRuntime设计.md) | AgentOS Kernel、调度、patrol、模块接入 |
| [Pulse-MemoryRuntime设计.md](./Pulse-MemoryRuntime设计.md) | Memory 五层、压缩/晋升、Prompt Contract |
| [Pulse-内核架构总览.md](./Pulse-内核架构总览.md) | 当前最新内核边界与数据流 |

阅读顺序：`Pulse架构方案` → `Pulse-内核架构总览` → 按需深入 AgentRuntime / MemoryRuntime。

## C. 补充

| 文档 | 内容 |
|---|---|
| [Pulse-DomainMemory与Tool模式.md](./Pulse-DomainMemory与Tool模式.md) | DomainMemory 单元、Tool 契约 |
| [Pulse-MCP优先实施方案.md](./Pulse-MCP优先实施方案.md) | MCP-first 分层与落地验收 |
| [Pulse命名规则.md](./Pulse命名规则.md) | 品牌、包名、CLI、目录命名 |
| [code-review-checklist.md](./code-review-checklist.md) | 编码 / 测试宪法（每次 PR 必过） |

## D. Architecture Decision Records

| ADR | 主题 | 状态 |
|---|---|---|
| [ADR-001](./adr/ADR-001-ToolUseContract.md) | ToolUseContract — 推理↔动作一致性三契约 | Accepted |
| [ADR-002](./adr/ADR-002-BrowserDriver-Patchright.md) | BOSS 浏览器驱动统一 patchright | Accepted |
| [ADR-003](./adr/ADR-003-ActionReport.md) | ActionReport — 长任务结构化执行报告 | Accepted |
| [ADR-004](./adr/ADR-004-AutoReplyContract.md) | BOSS 自动回复契约 + Patrol 控制面 | Proposed(§6.1 已落地) |
| [ADR-005](./adr/ADR-005-Observability.md) | 统一可观测性（trace_id / 日志结构） | Accepted |

## E. 工程化实践（`engineering/`）

| 文档 | 内容 |
|---|---|
| [testing-guide.md](./engineering/testing-guide.md) | 合同测试四形态 + LLM 应用特有测试点 + 反模式 |

## F. 业务模块设计（`modules/`）

| 模块 | 文档 |
|---|---|
| Job | [modules/job/architecture.md](./modules/job/architecture.md) |

## G. 外部页面快照（`dom-specs/`）

`dom-specs/<platform>/<page>/<UTC>.html|json` + 每个 page 的 `README.md` 说明 selector 约定。见 [dom-specs/README.md](./dom-specs/README.md)。

## H. 会话交接（`handoff/`）

按日期命名的单次会话产物。不长期维护。

| 文件 | 用途 |
|---|---|
| [handoff/2026-04-21.md](./handoff/2026-04-21.md) | 2026-04-21 BOSS 自动回复调试交接 |
| [handoff/memory-snapshot-for-interview.txt](./handoff/memory-snapshot-for-interview.txt) | 面试用项目记忆快照 |

## I. 归档（`archive/`）

已完成的审查清单、历史方案。保留仅供回溯，不再维护。详见目录。

---

## 快速启动

```bash
export DATABASE_URL="postgresql://user:pass@localhost:5432/pulse"
export PULSE_MODEL_API_KEY="sk-xxx"

python -m pulse start
# 或
uvicorn pulse.core.server:create_app --factory --host 0.0.0.0 --port 8000
```

详细环境配置见仓库根 `.env.example`。
