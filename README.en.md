<h1 align="center">Pulse</h1>

<p align="center">
  <strong>Open-source “Iron Man JARVIS” — a personal AI assistant that stays with you and knows you better over time</strong>
</p>

<p align="center">
  <a href="./README.md">中文</a>&nbsp;·&nbsp;
  <a href="#what-is-pulse">What</a>&nbsp;·&nbsp;
  <a href="#why-pulse">Why</a>&nbsp;·&nbsp;
  <a href="#core-capabilities">Capabilities</a>&nbsp;·&nbsp;
  <a href="#system-architecture">Architecture</a>&nbsp;·&nbsp;
  <a href="#three-pillars-of-the-agent-kernel">Kernel</a>&nbsp;·&nbsp;
  <a href="#quick-start">Quick start</a>&nbsp;·&nbsp;
  <a href="#feature-list">Features</a>&nbsp;·&nbsp;
  <a href="#roadmap">Roadmap</a>&nbsp;·&nbsp;
  <a href="./docs/README.md">Docs</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white" alt="Python" />
  <img src="https://img.shields.io/badge/FastAPI-async-009688?logo=fastapi&logoColor=white" alt="FastAPI" />
  <img src="https://img.shields.io/badge/MCP-Server%20%26%20Client-8A2BE2" alt="MCP" />
  <img src="https://img.shields.io/badge/LLM-OpenAI%20%7C%20Qwen3--Max-orange" alt="LLM" />
  <img src="https://img.shields.io/badge/Browser-Patchright%20CDP%20Stealth-FF6A00" alt="Patchright" />
  <img src="https://img.shields.io/badge/Milestone-M0--M8%20%E2%9C%93-success" alt="Milestone" />
  <img src="https://img.shields.io/badge/License-MIT-yellow" alt="License" />
</p>

<p align="center">
  Keywords: <code>AI Agent</code> · <code>Personal AI Assistant</code> · <code>JARVIS</code> · <code>Self-Evolving Agent</code> ·
  <code>ReAct</code> · <code>Tool Use Contract</code> · <code>Agent Memory</code> ·
  <code>Model Context Protocol (MCP)</code> · <code>Patrol / Heartbeat</code>
</p>

---

## What is Pulse

Pulse is a **personal AI assistant that runs on your own machine** — built to become a JARVIS that stays online, truly understands you, and takes the busywork off your plate.

To do that, Pulse cleanly separates a generic **agent kernel** from **domain skill packs** you plug in by area:

- **Kernel** (`core/`) is domain-agnostic — long-horizon scheduling, five-layer memory, contract-based tool use, event auditing, and personality evolution in one place.
- **Skill packs** (`modules/`) are grouped by domain; new scenarios add directories without changing the framework.
- **The first shipped skill pack is job search** — BOSS Zhipin JD scanning, proactive outreach, auto-replies in HR chats, resume delivery, and email tracking end-to-end.
- **The real technical depth** sits in three places — three-contract tool use (against agent hallucination), Layer × Scope five-layer memory, and the long-running `AgentRuntime` kernel (patrol + circuit breaking + event bus).

Job search is only the first scenario. Once the kernel stabilizes, weather, travel planning, company research, auto check-ins, and even agents that grow new skills will all run on the same core.

---

## Why Pulse

Today’s agent projects fall into three buckets, each with sharp pain points:

| Kind | Examples | Pain |
|---|---|---|
| Vertical automation scripts | BOSS / résumé / e-commerce bots | Rewrite per scenario; little reuse |
| Agent frameworks | LangChain / LangGraph / AutoGen | A toolbox, not a product — you still build from zero |
| Chat assistants | ChatGPT / Claude Desktop | No long-lived presence, no scheduled patrols, no durable memory or closed business loops |

Pulse aims to **pull the agent out of the chat window and turn it into an assistant that stays with you**:

- **Proactive work**: `AgentRuntime` stays in the background, patrolling for new messages and scheduled intel during work hours — no manual refresh.
- **Real memory**: Layer × Scope dual-axis memory remembers who you are, which companies you dislike, and where you left off across sessions — not a reset every turn.
- **No fake moves**: The three-contract architecture (ADR-001) pins “the LLM promised a tool call but never called one”-style hallucinations to an observable event stream.
- **Ecosystem fit**: MCP Server outward, MCP Client inward — plug-and-play with Claude Desktop, Cursor, or any MCP client.
- **Growing new skills**: Skill Generator turns natural language into AST-checked, sandbox-tested tools hot-loaded into the registry.

> “Agent OS” is not marketing fluff. `core/` really has a scheduling kernel (`core/runtime.py`), process isolation, an event bus, circuit breaking, and patrol lifecycle — and `core/` **must not contain any business vocabulary** (BOSS / résumé / job search). That is a hard architectural rule.

---

## Core capabilities

| Capability | Description | Status |
|---|---|---|
| **Agent OS kernel** | Long-lived `AgentRuntime`, self-registering patrol tasks, active hours + circuit breaking + event bus | ✅ M0–M3 |
| **ReAct + three-ring tools** | Brain loop · Ring 1 built-ins · Ring 2 modules · Ring 3 external MCP | ✅ M4 |
| **Three-contract tool use** | Description (`when_to_use`) + Call (`tool_choice`) + Execution Verifier (commitment audit) | ✅ A · ✅ B · ✅ C v2.1 |
| **Five-layer memory** | Operational / Recall / Workspace / Archival / Core + Layer × Scope | ✅ M5 |
| **Observability plane** | Standalone event bus + daily-rotating JSONL audit + in-memory sliding window + SSE live stream | ✅ |
| **MCP Server + Client** | Built-in tools are MCP tools for Claude Desktop / Cursor; internal side also consumes external MCP | ✅ |
| **Skill Generator** | Natural language → code → AST allowlist → sandbox → hot load | ✅ M6 |
| **Self-evolution engine** | SOUL `[CORE]` / `[MUTABLE]` tiers · Autonomous / Supervised / Gated governance · preference learning Track A · DPO collection Track B | ✅ M7 |
| **Browser automation** | Patchright (Playwright fork, CDP anti-fingerprinting), validated on BOSS Zhipin | ✅ |
| **Multi-channel ingress** | HTTP / SSE / CLI / Feishu · intent routing exact → prefix → LLM | ✅ |
| **HITL governance** | Policy Engine L0–L5 gates · approval / rollback / versioned rules + diff | ✅ M7 |

---

## System architecture

### Big picture

```
┌──────────────────────────────────────────────────────────────────────────┐
│                     Pulse (single Python process)                        │
│                                                                          │
│  ┌──────────────────────── Ingress ────────────────────────────┐         │
│  │  HTTP API │ SSE stream │ CLI │ Feishu adapter │ MCP Server│         │
│  └──────────────────────────┬───────────────────────────────┘         │
│                              ↓                                           │
│  ┌──────────────────── Agent OS kernel ────────────────────────┐         │
│  │                                                            │         │
│  │  AgentRuntime  ──→  scheduling / active hours / patrol LC  │         │
│  │       │                                                    │         │
│  │       ▼                                                    │         │
│  │  Task Runtime (Brain)  ──→  ReAct loop                    │         │
│  │       │                      + ToolUseContract (A/B/C)    │         │
│  │       ▼                                                    │         │
│  │  Memory Runtime        ──→  Layer × Scope five layers      │         │
│  │       │                                                    │         │
│  │       ▼                                                    │         │
│  │  Observability Plane   ──→  EventBus + JSONL + InMemStore │         │
│  │       │                                                    │         │
│  │       ▼                                                    │         │
│  │  SafetyPlane (planned) ──→  subscribe + policy gating      │         │
│  │                                                            │         │
│  └────────────────────────────────────────────────────────────┘         │
│                              ↓                                           │
│  ┌──────────────── Three-ring capability (Brain tool list) ─┐         │
│  │  Ring 1 Tool         light built-ins (alarm/weather/web…) │         │
│  │  Ring 2 Module       domain packs (job/intel/email/sys)   │         │
│  │  Ring 3 External MCP any MCP server (GitHub/Notion…)      │         │
│  │  Meta   SkillGen     NL → hot-loaded new tools            │         │
│  └────────────────────────────────────────────────────────────┘         │
│                              ↓                                           │
│  ┌──────────────── Capability layer (domain-agnostic) ─────┐         │
│  │  LLM Router · Browser Pool · Storage · Notify · Scheduler│         │
│  │  Channel · Policy · EventBus · Cost · Config             │         │
│  └──────────────────────────────────────────────────────────┘         │
│                                                                          │
├──────────────────────────────────────────────────────────────────────────┤
│  PostgreSQL (app data + memory) │ JSON (Core Memory) │ External MCP     │
└──────────────────────────────────────────────────────────────────────────┘
```

### Five kernel layers (`docs/Pulse-内核架构总览.md`)

| Layer | Owns | Must not leak in | Shipped |
|---|---|---|---|
| **Agent OS** | Long-run patrol scheduling, active hours, circuit breaking, event fan-out | prompt assembly, fact promotion, user-pref writes | ✅ |
| **Task Runtime** | Single-turn state machine, tool loop, hooks, budget, stop reason, three contracts | cron scheduling, long-term fact schemas | ✅ |
| **Memory Runtime** | Five-layer R/W, compaction, promotion, evidence tracing | task scheduling, final answer synthesis, audit persistence | ✅ |
| **Observability Plane** | Event bus + append-only audit + subscription push | storing business data, making decisions | ✅ |
| **SafetyPlane** | policy gate, approval, rollback, manual takeover | proactive business execution | ⏳ planned |

---

## Three pillars of the agent kernel

This is where Pulse is more than “another LangChain agent wrapper.” Each pillar has its own ADR / design doc — not vaporware.

### 1. ToolUseContract — three orthogonal contracts against agent hallucination

**Problem**: The most common agent bug is not a broken tool — it’s the LLM **saying** it did something without **calling** a tool (e.g. “I saved your preference” while `tool_calls=[]` and memory is empty). Prompt-only guardrails fail in messy conversations.

**Pulse’s answer** ([ADR-001](./docs/adr/ADR-001-ToolUseContract.md)): three orthogonal contracts; if one slips, the next layer catches it.

| Contract | Where it lives | What it does |
|---|---|---|
| **A. Description** | `ToolSpec.when_to_use / when_not_to_use` + PromptContract three-part rendering + counter-example few-shots | Turns “guess which tool” into “tools declare their preconditions” |
| **B. Call** | `LLMRouter.invoke_chat(tool_choice=...)` + structured ReAct-step escalation + hand-off | On a “text-only, empty tool” turn, escalate to `tool_choice="required"`; pass `scan_handle` between tools to reuse results |
| **C. Execution Verifier** | Before reply, LLM self-audit `commitment vs used_tools`; on mismatch, rewrite honestly + emit `brain.commitment.unfulfilled` to audit | Catches edge cases where A and B both fail — no bluffing the user |

**Invariant**: semantic judgment stays with the LLM; structural judgment stays in Python. The host **must not** regex/keyword-match user intent to force tool calls.

Every contract failure is driven by real traces (ADR lists production cases like `trace_e48a6be0c90e / 16e97afe3ffc / 4890841c2322`). These defenses were **hardened by production bugs**.

### 2. Layer × Scope dual-axis memory

Mainstream agent memory is often “core + vector search,” stuffing everything into one “memory” bag. Pulse splits along **two axes**:

**Layer axis** (which tier → lifecycle):

| Layer | Lifecycle | Storage | Retrieval |
|---|---|---|---|
| Operational | within turn / run | in-memory | direct read |
| Recall | medium term | PostgreSQL | recent + agentic search (ILIKE) |
| Workspace | medium–long | PostgreSQL | KV facts + summary |
| Archival | long | PostgreSQL | SPO filters + agentic keywords |
| Core | long | JSON | blocks (SOUL / USER / PREFS / CONTEXT) |

**Scope axis** (which boundary → isolation): `turn` / `taskRun` / `session` / `workspace` / `global`

**Key unit** — `Workspace × workspace`: domain facades (job / mail / health …) hang here. The kernel does not know business semantics; it only offers KV/summary CRUD. A new skill pack = a new facade on this unit.

**No embeddings for now**: Pulse uses full agentic search (LLM keywords → SQL ILIKE). At single-user scale ≤ 10⁴ rows, P95 < 10 ms; vector DB upside does not beat dependency and training/inference cost. Rationale + future switch thresholds: appendix B in [`Pulse-MemoryRuntime设计.md`](./docs/Pulse-MemoryRuntime设计.md).

### 3. AgentRuntime — OS-grade long-horizon drive

The kernel that keeps Pulse “awake” in the background. Not just `cron` + `while True`.

- **Self-registration**: any module calls `register_patrol(...)` in `on_startup`; the kernel imports no business modules.
- **active hours**: weekdays 9–22, weekends 10–20 — more frequent patrols when active, deep sleep at night — avoids BOSS risk control at 3am.
- **Conversational control plane** ([ADR-004](./docs/adr/ADR-004-AutoReplyContract.md) §6.1): via IM — “what patrol tasks are running?”, “turn off BOSS auto-reply” — no admin UI required. `system.patrol.*` IntentSpec exposes `list / status / enable / disable / trigger`.
- **Circuit breaking + recovery ladder**: `retry` / `degrade` / `skip` / `abort` / `rollback` / `circuitBreak` / `manualTakeover`.
- **Observability plane is separate**: every layer publishes via `EventBus.publish`; default subscribers: `InMemoryEventStore` (2000-event window for WS/SSE) + `JsonlEventSink` (daily rotation, persists `llm./tool./memory./policy./promotion.*` prefixes only).

---

## Memory and evolution

```
┌─────────────────────────────────────────────────────────────────┐
│  Before each ReAct turn:                                        │
│    Load Core (SOUL + USER + PREFS + CONTEXT)                    │
│      + Recall last N summaries + Archival facts                 │
│      + DomainMemory facts in Workspace × workspace              │
│    → assemble system prompt for the LLM                         │
│                                                                  │
│  After reasoning:                                               │
│    Append dialogue + tool calls to Recall                       │
│    Clear Operational turn scratchpad                            │
│    Promote Workspace / Archival / Core via Promotion Pipeline   │
│    Mirror events to EventBus → InMemStore + JsonlSink          │
│                                                                  │
│  User correction ("stop recommending game studios"):            │
│    → Preference Track A: detect correction → extract rule → PREFS│
│    → Governance (Autonomous / Supervised / Gated) for apply-now │
│    → Audit log changes with rollback + version diff             │
└─────────────────────────────────────────────────────────────────┘
```

**Persona tiers**: each belief in SOUL is tagged

```yaml
values:
  - "[CORE]    User interest first; never harm the user"
  - "[CORE]    Be honest; say clearly when uncertain"
  - "[MUTABLE] Prefer remote-friendly opportunities"
```

`[CORE]` beliefs are never rewritten by the reflection pipeline; `[MUTABLE]` can evolve from feedback — knows you better while keeping guardrails.

---

## Feature list

Pulse’s current skill packs focus on job search, but the kernel is already generic — new domains are new directories.

### Job domain `modules/job/`

| Sub-capability | What it does | Entry |
|---|---|---|
| **greet** | BOSS role scan → two-stage JD funnel (rule hard filter + LLM binary judge) → full JD from detail page → proactive outreach | `job.greet.scan` / `job.greet.trigger` |
| **chat** | Pull unread → LLM intent → profile-matched reply / send résumé / HR card accept / escalate → post-reply DOM verify against fake delivery | `job.chat.run_process` / `system.patrol.*` |
| **profile** | JobMemory triple store: hard constraints / memory items / résumé text + parsed summary | `job.memory.record` / `job.hard_constraint.set` / `job.resume.update` |
| **connectors/boss** | Patchright persistent context · cookie SSO · risk signals · DOM-snapshot-driven selectors | domain-internal |

### Intel domain `modules/intel/`

| Sub-capability | What it does |
|---|---|
| **interview** | Interview intel harvest → LLM extract signals → aggregate by company → Feishu digest |
| **techradar** | Tech radar (RSS / GitHub Trending / WeChat) → LLM relevance → summary → digest |
| **query** | Semantic intel search + category filters |

### Email domain `modules/email/tracker/`

Read-only IMAP → LLM email classify (invite / rejection / more materials) → structured calendar extraction → state sync + Feishu alerts.

### System domain `modules/system/`

| Sub-capability | What it does |
|---|---|
| **hello** | Health probe |
| **feedback** | Feedback loop driving preference Track A |
| **patrol** | `system.patrol.*` conversational patrol surface (ADR-004 §6.1) |

---

## Quick start

### Prerequisites

- Python 3.11+
- PostgreSQL (local or Docker)
- OpenAI or Qwen3-Max API key (at least one)

### ~30 seconds (dev mode)

```bash
git clone https://github.com/<your-org>/pulse.git
cd pulse

cp .env.example .env
# Edit .env: OPENAI_API_KEY or DASHSCOPE_API_KEY, and DATABASE_URL

pip install -e .[dev]
pulse start
```

API docs: <http://localhost:8010/docs>  
Health: <http://localhost:8010/health>  
Live events (SSE): <http://localhost:8010/api/agent/events>

### Docker one-liner

```bash
docker compose up --build
```

### Optional: job automation

```bash
# First-time BOSS login (browser QR; cookies persist under ~/.pulse/boss_browser_profile)
./scripts/boss_login.sh

# Enable patrol in .env
PULSE_JOB_PATROL_GREET_ENABLED=true
PULSE_JOB_PATROL_CHAT_ENABLED=true
AGENT_RUNTIME_ENABLED=true

# After restart, AgentRuntime enters active hours; peak patrol ~15 min
```

### Claude Desktop / Cursor (as MCP Server)

All Ring 1 / Ring 2 tools registered via `@tool` are exposed as MCP tools. Example `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "pulse": {
      "command": "pulse",
      "args": ["mcp", "serve"]
    }
  }
}
```

Then call Pulse tools like `job.greet.scan`, `memory.search` from Claude / Cursor.

---

## Project structure

```
Pulse/
├── src/pulse/
│   ├── core/                    ← domain-agnostic kernel
│   │   ├── runtime.py           AgentRuntime OS kernel
│   │   ├── brain.py             ReAct loop
│   │   ├── task_context.py      Task Runtime state machine
│   │   ├── prompt_contract.py   Prompt Contract system
│   │   ├── verifier.py          CommitmentVerifier (contract C)
│   │   ├── memory/              five layers (operational/recall/workspace/archival/core)
│   │   ├── soul/                SOUL persona + evolution pipeline
│   │   ├── learning/            preference Track A + DPO Track B
│   │   ├── tool.py              ToolRegistry + @tool decorator
│   │   ├── mcp_client.py        MCP client (external servers)
│   │   ├── mcp_server.py        MCP server (expose Pulse tools)
│   │   ├── events.py            EventBus + InMemoryEventStore
│   │   ├── event_sinks.py       JsonlEventSink (daily audit rotation)
│   │   ├── policy.py            Policy Engine (L0–L5 gates)
│   │   ├── cost.py              daily LLM budget + auto degrade
│   │   ├── router.py            intent routing (exact/prefix/LLM)
│   │   ├── channel/             multi-channel (HTTP/CLI/Feishu)
│   │   ├── browser/             Patchright pool
│   │   ├── llm/                 multi-model routing + failover
│   │   ├── skill_generator.py   NL → hot-loaded tools
│   │   └── sandbox.py           AST + subprocess sandbox
│   │
│   ├── modules/                 ← domain skill packs (pluggable)
│   │   ├── job/                 job: greet / chat / profile / _connectors/boss
│   │   ├── intel/               intel: interview / techradar / query
│   │   ├── email/tracker/       email
│   │   └── system/              system: hello / feedback / patrol
│   │
│   ├── tools/                   ← Ring 1 built-ins (alarm/weather/web/flight…)
│   └── mcp_servers/             MCP server runtimes (BOSS browser runtime, etc.)
│
├── docs/
│   ├── README.md                docs index (Chinese nav)
│   ├── Pulse-内核架构总览.md    ★ top-level architecture index
│   ├── Pulse-AgentRuntime设计.md
│   ├── Pulse-MemoryRuntime设计.md
│   ├── Pulse-DomainMemory与Tool模式.md
│   ├── adr/                     ADR-001 ~ 005
│   ├── engineering/             engineering notes (testing guide, etc.)
│   ├── modules/                 module-level design docs
│   └── dom-specs/               real DOM snapshots (selector fixtures)
│
├── config/                      runtime config (router / policy / soul.yaml …)
├── tests/pulse/                 unit + integration + contract tests
├── scripts/                     ops scripts (start / boss_login …)
├── Dockerfile
├── docker-compose.yml
└── pyproject.toml
```

---

## Tech stack

| Layer | Choice | Notes |
|---|---|---|
| Agent kernel | In-house `core/runtime.py` | No framework lock-in; fully readable/forkable |
| Reasoning | ReAct + three-ring tools | Contracted tool use, not opaque chains |
| Models | OpenAI / Qwen3-Max / DeepSeek | LLMRouter primary/backup + task-type routing |
| Tool protocol | **MCP** | Expose outward + consume inward |
| Browser | **Patchright** | Playwright fork; CDP anti-fingerprinting |
| Backend | **FastAPI** + async | HTTP API + SSE streams |
| Storage | **PostgreSQL** | App data + Recall + Workspace + Archival + audit |
| Persona / core memory | YAML + JSON | Human-readable; version + governance rollback |
| Observability | EventBus + JSONL + InMemoryEventStore | Separate plane from memory |
| Deploy | single Python process · Docker Compose | No K8s required |

---

## Implementation milestones

Pulse ships **a runnable system at every milestone**. Current status:

| Milestone | Scope | Status |
|---|---|---|
| M0 | Skeleton · module system · EventBus | ✅ |
| M1 | Capability extraction (LLM/Storage/Browser/Scheduler/Notify) | ✅ |
| M2 | Module migration · V1 cleanup | ✅ |
| M3 | Ingress · intent routing · Policy Engine · Docker | ✅ |
| M4 | Brain ReAct · three-ring tools · MCP client/server · cost control | ✅ |
| M5 | Five-layer memory · SOUL · memory tools | ✅ |
| M6 | Skill Generator · AST + sandbox | ✅ |
| M7 | Evolution engine · governance · DPO collection · versioned rules | ✅ |
| M8 | ToolUseContract hardening (A/B/C) | ✅ except M8.A real-trace validation |

Details: [`docs/Pulse实施计划.md`](./docs/Pulse实施计划.md).

---

## Design decisions (why these choices)

| Decision | Choice | Rationale |
|---|---|---|
| Vector DB for memory | **No — agentic search** | Small per-user corpora; LLM semantics beat cheap similarity; smaller deps |
| Browser engine | **Patchright** over stock Playwright | CDP anti-detection; best pass rate on BOSS-class sites |
| Anti-hallucination | **Three orthogonal contracts** (ADR-001) | Prompt-only fails in production; structure + audit + tests |
| Memory model | **Layer × Scope** | One “memory” bag mixes concerns; dual axes separate lifecycle vs scope |
| Audit | **Observability plane ≠ Memory** | What the LLM reads vs what humans/compliance read should not share one schema |
| Tooling | **MCP-first** | Aligns with Claude Desktop / Cursor; room for many external servers |
| Long-run | **AgentRuntime** not cron | cron lacks active hours, circuit breaking, events, conversational control |
| Business vs kernel | **`core/` zero business words** (hard rule) | Enforced via `.cursor/rules/pulse-architecture.mdc` |

---

## Documentation

- Project index: [`docs/README.md`](./docs/README.md)
- Kernel architecture: [`docs/Pulse-内核架构总览.md`](./docs/Pulse-内核架构总览.md)
- AgentRuntime: [`docs/Pulse-AgentRuntime设计.md`](./docs/Pulse-AgentRuntime设计.md)
- Memory: [`docs/Pulse-MemoryRuntime设计.md`](./docs/Pulse-MemoryRuntime设计.md)
- DomainMemory spec: [`docs/Pulse-DomainMemory与Tool模式.md`](./docs/Pulse-DomainMemory与Tool模式.md)
- Testing engineering: [`docs/engineering/testing-guide.md`](./docs/engineering/testing-guide.md)
- ADRs: [`docs/adr/`](./docs/adr/)
- Coding / testing constitution: [`docs/code-review-checklist.md`](./docs/code-review-checklist.md)

---

## Roadmap

Job search is the first stop; the architecture already leaves room for much more.

**Done**

- [x] Generic Agent OS kernel (AgentRuntime + event bus + patrol lifecycle)
- [x] ReAct + three-ring tools (Ring 1/2/3 + Meta SkillGen)
- [x] Three-contract tool use (ADR-001 A/B/C)
- [x] Layer × Scope five-layer memory + separate Observability plane
- [x] SOUL tiers + Evolution Engine + Gated / Supervised / Autonomous governance
- [x] MCP Server + Client (bidirectional)
- [x] Job skill pack: BOSS JD scan / outreach / HR auto-reply / résumé send / email tracking

**In progress / near term**

- [ ] Ship SafetyPlane — subscribe to EventBus for policy gates + manual takeover
- [ ] More channels: WeCom long-lived bot, Discord, Telegram
- [ ] Job intel: real interview sites (Nowcoder / Maimai), deeper company research module

**New skill packs (architecture ready)**

- [ ] Calendar / alarm / weather / travel assistant (Ring 1 tools exist in `tools/`; productize compositions)
- [ ] Smart-home MCP (Home Assistant / Matter)
- [ ] Finance weekly report module (bank/broker APIs)
- [ ] Travel guide module (multi-source crawl + LLM synthesis)
- [ ] Game auto check-in / dailies module
- [ ] Local offline mode (Ollama + local Qwen)

**Longer term**

- [ ] Skill Generator generates full modules (today: Ring 1 tools only)
- [ ] Wire DPO Track B into a real training pipeline (Track A preferences shipped)
- [ ] Cross-device sync (light mobile channel + cloud Pulse primary)

---

## Contributing

Pulse is at **v0** — issues and PRs welcome; great time to jump in. Before a PR, read [`docs/code-review-checklist.md`](./docs/code-review-checklist.md) — coding + testing constitution (no swallowed exceptions, no silent fallbacks, no fake tests).

For architecture changes, open an RFC-style issue first; ADR process: [`docs/adr/`](./docs/adr/).

---

## Acknowledgments

Ideas borrowed or inspired by:

- **Letta / MemGPT** — core memory blocks and self-edit patterns
- **Claude Code agentic loop** — per-turn pipeline, nine `StopReason`s, cost-aware recovery
- **OpenClaw** — heartbeat = full agent turn + session serialization
- **Anthropic MCP** — tool protocol design
- **NabaOS Tool Receipts** — false-absence / hallucination detection
- **Bertrand Meyer, *Design by Contract*** — contract thinking

---

## License

[MIT](./LICENSE)

---

<p align="center">
  <strong>If Pulse helps you, a Star means a lot to open-source authors ⭐</strong>
  <br/>
  <sub>Built for people who want an AI that stays, not one that forgets every conversation.</sub>
</p>
