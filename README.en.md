<h1 align="center">Pulse</h1>

<p align="center">
  <strong>Open-source long-running personal AI assistant &middot; persistent memory &middot; proactive execution &middot; kernel + skill-pack architecture</strong>
</p>

<p align="center">
  <a href="./README.md">中文</a>&nbsp;·&nbsp;
  <a href="#what-is-pulse">What</a>&nbsp;·&nbsp;
  <a href="#why-pulse">Why</a>&nbsp;·&nbsp;
  <a href="#core-capabilities">Capabilities</a>&nbsp;·&nbsp;
  <a href="#system-architecture">Architecture</a>&nbsp;·&nbsp;
  <a href="#four-pillars-of-the-agent-kernel">Kernel</a>&nbsp;·&nbsp;
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
  <img src="https://img.shields.io/badge/Status-Alpha%20%E2%80%A2%20usable-2ea44f" alt="Status" />
  <img src="https://img.shields.io/badge/PRs-welcome-ff69b4" alt="PRs welcome" />
  <img src="https://img.shields.io/badge/License-MIT-yellow" alt="License" />
</p>

<p align="center">
  Keywords: <code>AI Agent</code> · <code>Personal AI Assistant</code> · <code>Self-Hosted AI</code> · <code>JARVIS</code> ·
  <code>Self-Evolving Agent</code> · <code>ReAct</code> · <code>Tool Use Contract</code> · <code>Agent Memory</code> ·
  <code>Model Context Protocol (MCP)</code> · <code>Agent Skill Pack</code> ·
  <code>Game Automation</code> · <code>Life Assistant</code> · <code>Patrol / Heartbeat</code>
</p>

---

## What is Pulse

Pulse is a **personal AI assistant that runs on your own machine** — long-lived, truly understanding of you, and takes busywork off your plate. The goal is a "JARVIS of your own": not a one-shot chatbot, but a long-running system that executes proactively, keeps learning, and grows to understand you over time.

To do that, Pulse cleanly separates a generic **agent kernel** from **domain skill packs** you plug in by area:

- **Kernel** (`core/`) is domain-agnostic — long-horizon scheduling, five-layer memory, contract-based tool use, event auditing, and personality evolution in one place.
- **Skill packs** (`modules/`) are grouped by domain; new scenarios add directories without changing the framework.
- **Shipped skill packs**: job search (BOSS Zhipin JD scan / proactive outreach / HR auto-reply / résumé delivery), intel digest (multi-topic YAML packs driving a six-stage pipeline + cross-topic search), and email tracking (IMAP classification + schedule extraction).
- **The real technical depth** sits in three places — three-contract tool use (against agent hallucination), Layer × Scope five-layer memory, and the long-running `AgentRuntime` kernel (patrol + circuit breaking + event bus).

This is only the beginning. **Every life workflow worth automating becomes Pulse's next destination** — automating daily game check-ins, generating end-to-end travel plans, weekly finance reports, orchestrating smart-home devices, even synthesizing new skills and accumulating habits and preferences uniquely yours. One kernel, an unbounded set of skill packs.

---

## Why Pulse

Today’s agent projects fall into three buckets, each with sharp pain points:

| Kind | Examples | Pain |
|---|---|---|
| Vertical automation scripts | BOSS / résumé / e-commerce bots | Rewrite per scenario; little reuse |
| Agent frameworks | LangChain / LangGraph / AutoGen | Focused on orchestrating one request; still need a layer of product glue to become **your** long-running AI assistant |
| Chat assistants | ChatGPT / Claude Desktop | No long-lived presence, no scheduled patrols, no durable memory or closed business loops |

Pulse aims to **decouple the agent from the chat window and turn it into an assistant that stays with you long-term**:

- **Proactive execution**: `AgentRuntime` stays in the background, patrolling for new messages and scheduled intel during work hours — no manual refresh required.
- **Persistent memory**: Layer × Scope dual-axis memory preserves user profile, preferences, and context across sessions — no reset between turns.
- **Verifiable commitments**: The three-contract architecture (ADR-001) constrains "the LLM claimed a tool call but never issued one"-style hallucinations to an observable event stream.
- **Ecosystem fit**: MCP Server outward, MCP Client inward — plug-and-play with Claude Desktop, Cursor, or any MCP client.
- **Self-extensible**: Skill Generator turns natural-language requirements into AST-validated, sandbox-tested tools that hot-load into the registry.

> "Agent OS" is not a marketing label. `core/` implements an actual scheduling kernel (`core/runtime.py`), process isolation, an event bus, circuit breaking, and a patrol lifecycle — and `core/` **must not contain any business vocabulary** (BOSS / résumé / job search), enforced as a lint rule.

---

## Core capabilities

| Capability | Description | Status |
|---|---|---|
| **Agent OS kernel** | Long-lived `AgentRuntime`, self-registering patrol tasks, active hours + circuit breaking + event bus | ✅ |
| **ReAct + three-ring tools** | Brain loop · Ring 1 built-ins · Ring 2 modules · Ring 3 external MCP | ✅ |
| **Three-contract tool use** | Description (`when_to_use`) + Call (`tool_choice`) + Execution Verifier (commitment audit) | ✅ |
| **Five-layer memory** | Operational / Recall / Workspace / Archival / Core + Layer × Scope | ✅ |
| **Observability plane** | Standalone event bus + daily-rotating JSONL audit + in-memory sliding window + SSE live stream | ✅ |
| **MCP Server + Client** | Built-in tools are MCP tools for Claude Desktop / Cursor; internal side also consumes external MCP | ✅ |
| **Skill Generator** | Natural language → code → AST allowlist → sandbox → hot load | ✅ |
| **Self-evolution engine** | SOUL `[CORE]` / `[MUTABLE]` tiers · Autonomous / Supervised / Gated governance · preference learning Track A · DPO collection Track B | ✅ |
| **Browser automation** | Patchright (Playwright fork, CDP anti-fingerprinting), validated on BOSS Zhipin | ✅ |
| **Multi-channel ingress** | HTTP / SSE / CLI / WeCom / Feishu · intent routing exact → prefix → LLM | ✅ |
| **HITL governance** | Policy Engine L0–L5 gates · approval / rollback / versioned rules + diff | ✅ |
| **SafetyPlane v2** | Service-layer side-effect gates · **four-step** Suspend-Ask-Resume-**Reexecute** primitive · idempotent Ask dedup · Beijing-time work window (VPN-safe) | ✅ (end-to-end in `job_chat`) |

---

## System architecture

### Big picture

```
┌────────────────────────────────────────────────────────────────────────────┐
│  Pulse main Python process   (FastAPI / uvicorn · single proc + asyncio    │
│                               + 1 background daemon thread)               │
│                                                                            │
│  ┌──────────────────────── Ingress ────────────────────────┐              │
│  │  HTTP API │ SSE │ CLI │ WeCom/Feishu adapter │ MCP      │              │
│  └───────────────────────────┬─────────────────────────────┘              │
│                              │                                             │
│                              ▼                                             │
│  ┌──────────────────── Agent OS kernel ───────────────────┐               │
│  │  AgentRuntime    ──→ scheduling / active hours / patrol │               │
│  │    · daemon thread `pulse-scheduler-runner` (tick≈15s) │               │
│  │    · each tick runs due patrols with asyncio           │               │
│  │                                                         │               │
│  │  Brain (task executor) ──→ ReAct loop                 │               │
│  │                            + ToolUseContract (A/B/C)  │               │
│  │  Memory Runtime       ──→ Layer × Scope five layers    │               │
│  │  Observability Plane  ──→ EventBus + JSONL + InMemStore│               │
│  │  SafetyPlane v2       ──→ Service gates + Suspend-Ask- │               │
│  │                           Resume-Reexecute four-step   │               │
│  └───────────────────────────┬─────────────────────────────┘              │
│                              │                                             │
│                              ▼                                             │
│  ┌──────────── Three-ring capability (Brain tool list) ────┐              │
│  │  Ring 1 Tool         light built-ins (alarm/weather/web)│              │
│  │  Ring 2 Module       domain packs (job/intel/email/sys) │              │
│  │  Ring 3 External MCP any MCP server (cross-process)     │              │
│  │  Meta   SkillGen     NL → hot-loaded new tools          │              │
│  └─────────────────────────────────────────────────────────┘              │
│                                                                            │
│  ┌──────────── Capability layer (domain-agnostic) ────────┐               │
│  │  LLM Router · Browser Pool · Storage · Notify · Sched  │               │
│  │  Channel · Policy · EventBus · Cost · Config           │               │
│  └────────────────────────────────────────────────────────┘               │
└───────────┬─────────────────────┬──────────────────────┬──────────────────┘
            │  stdio / subprocess │  CDP (WebSocket)     │  TCP / Unix sock
            ▼                     ▼                      ▼
   ┌────────────────┐  ┌──────────────────────┐  ┌──────────────────────┐
   │ External MCP   │  │  Chromium OS subproc │  │  PostgreSQL          │
   │ (own processes)│  │  (spawned by         │  │  app data + memory   │
   │ GitHub/Notion..│  │   Patchright / BOSS) │  │  audit via JSONL     │
   └────────────────┘  └──────────────────────┘  └──────────────────────┘
```

> **Concurrency model in one line**: all Pulse Python code lives in **one process**; every patrol (BOSS auto-reply, auto-apply, ...) is driven by asyncio on a single daemon thread — Pulse **never** spawns a new Python process per patrol. The only real OS subprocesses are **Chromium** (launched by Patchright for web automation) and **external MCP Servers** (attached over stdio). This is the same deployment topology used by Claude Desktop / Cursor and is a standard pattern under Python's GIL.

### Five kernel layers (`docs/Pulse-内核架构总览.md`)

| Layer | Responsibility | Out of scope | Status |
|---|---|---|---|
| **Agent OS** | Long-run patrol scheduling, active hours, circuit breaking, event fan-out | prompt assembly, fact promotion, user-pref writes | ✅ |
| **Task Runtime** | Single-turn state machine, tool loop, hooks, budget, stop reason, three contracts | cron scheduling, long-term fact schemas | ✅ |
| **Memory Runtime** | Five-layer R/W, compaction, promotion, evidence tracing | task scheduling, final answer synthesis, audit persistence | ✅ |
| **Observability Plane** | Event bus + append-only audit + subscription push | storing business data, making decisions | ✅ |
| **SafetyPlane** | policy gate, approval, rollback, manual takeover | proactive business execution | ⏳ planned |

---

## Three pillars of the agent kernel

This is where Pulse differentiates itself from "yet another LangChain agent wrapper". Each pillar is backed by an independent ADR / design doc — not a placeholder.

### 1. ToolUseContract — three orthogonal contracts against agent hallucination

**Problem**: The most common agent bug is not a broken tool — it is the LLM **claiming** an action without **invoking** a tool (e.g. "I saved your preference" while `tool_calls=[]` and memory remains empty). Prompt-only guardrails inevitably fail in complex multi-turn conversations.

**Pulse’s answer** ([ADR-001](./docs/adr/ADR-001-ToolUseContract.md)): three orthogonal contracts; if one slips, the next layer catches it.

| Contract | Where it lives | What it does |
|---|---|---|
| **A. Description** | `ToolSpec.when_to_use / when_not_to_use` + PromptContract three-part rendering + counter-example few-shots | Turns “guess which tool” into “tools declare their preconditions” |
| **B. Call** | `LLMRouter.invoke_chat(tool_choice=...)` + structured ReAct-step escalation + hand-off | On a “text-only, empty tool” turn, escalate to `tool_choice="required"`; pass `scan_handle` between tools to reuse results |
| **C. Execution Verifier** | Before reply, LLM self-audits `commitment vs used_tools`; on mismatch, rewrites to an honest statement + emits `brain.commitment.unfulfilled` to audit | Catches edge cases where A and B both fail — unfulfilled commitments are rewritten to truthful statements, eliminating user-facing bluffs |

**Invariant**: semantic judgment stays with the LLM; structural judgment stays in Python. The host **must not** regex/keyword-match user intent to force tool calls.

Every contract is introduced in response to real production traces (the ADR records cases such as `trace_e48a6be0c90e / 16e97afe3ffc / 4890841c2322`). These defenses have been **hardened by real production incidents**.

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

The kernel that keeps Pulse running continuously in the background — substantially more than `cron` + `while True`.

- **Self-registration**: any module calls `register_patrol(...)` in `on_startup`; the kernel imports no business modules.
- **Active hours**: weekdays 9–22, weekends 10–20 — elevated patrol frequency when active, quiesced at night — avoids triggering BOSS risk control during off-hours.
- **Conversational control plane** ([ADR-004](./docs/adr/ADR-004-AutoReplyContract.md) §6.1): control via IM commands ("list running patrol tasks", "disable BOSS auto-reply") — no admin UI required. `system.patrol.*` IntentSpec exposes `list / status / enable / disable / trigger`.
- **Circuit breaking + recovery ladder**: `retry` / `degrade` / `skip` / `abort` / `rollback` / `circuitBreak` / `manualTakeover`.
- **Observability plane is decoupled**: every layer publishes via `EventBus.publish`; default subscribers: `InMemoryEventStore` (2000-event sliding window for WS/SSE) + `JsonlEventSink` (daily rotation, persists only `llm./tool./memory./policy./promotion.*` prefixes).

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

A single Module assembled from topic YAMLs — adding a new topic (autumn recruiting, interview prep, LLM frontier, …) is a config change, not a code change.

| Capability | What it does |
|---|---|
| **digest** | Per-topic deterministic six-stage pipeline: fetch → dedup → score → summarize → diversify → publish, across RSS / GitHub Trending / Web Search. Anti-cocoon controls (source quotas + serendipity slots + contrarian bonus) are first-class workflow citizens; high-score items get promoted into ArchivalMemory. |
| **search** | Cross-topic keyword retrieval (LLM extracts terms → SQL ILIKE), reachable from Brain ReAct and any external MCP client (Claude Desktop / Cursor). |

### Email domain `modules/email/tracker/`

Read-only IMAP → LLM email classify (invite / rejection / more materials) → structured calendar extraction → state sync + IM alerts (WeCom / Feishu).

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

# After restart, AgentRuntime enters active hours; one patrol every 15 minutes during peak window
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
│   │   ├── channel/             multi-channel (HTTP/CLI/WeCom/Feishu)
│   │   ├── browser/             Patchright pool
│   │   ├── llm/                 multi-model routing + failover
│   │   ├── skill_generator.py   NL → hot-loaded tools
│   │   └── sandbox.py           AST + subprocess sandbox
│   │
│   ├── modules/                 ← domain skill packs (pluggable)
│   │   ├── job/                 job: greet / chat / profile / _connectors/boss
│   │   ├── intel/               intel: digest pipeline + search (multi-topic via YAML)
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
| Deploy | single Python process · Docker Compose | No K8s required — runs out of the box |

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
- Engineering constitution (code / tests / comments / system shape): [`docs/code-review-checklist.md`](./docs/code-review-checklist.md); Pulse-specific checklist: [`docs/engineering/pulse-conventions.md`](./docs/engineering/pulse-conventions.md)

---

## Roadmap

> Job search is the first stop, but the stage Pulse leaves room for is much bigger. Below are the pluggable skill-pack directions — each is an independent Module directory, each welcomes PRs.

### Released · v0 baseline

- [x] Generic Agent OS kernel (AgentRuntime + event bus + patrol lifecycle)
- [x] ReAct + three-ring tools (Ring 1/2/3 + Meta SkillGen)
- [x] Three-contract tool use (ADR-001 A/B/C) against agent hallucination
- [x] Layer × Scope five-layer memory + separate Observability plane
- [x] SOUL tiers + Evolution Engine + Gated / Supervised / Autonomous governance
- [x] MCP Server + Client (bidirectional)
- [x] Job skill pack: BOSS JD scan / outreach / HR auto-reply / résumé send / email tracking

### In progress · kernel hardening

- [ ] **Ship SafetyPlane** — subscribe to EventBus for policy gates + manual takeover + risk sandbox
- [ ] **More channels** — Discord · Telegram · WeCom WebSocket long-lived bot (HTTPS webhook WeCom / Feishu already shipped)
- [ ] **Job intel v2** — real interview sites (Nowcoder / Maimai), deeper company research module
- [ ] **Local offline mode** — Ollama + local Qwen so Pulse survives without the internet

---

### 🎮 Game automation skill pack

Make Pulse the **digital workhorse that handles the daily grind for SLG / MMO / card games**. The Patchright + DOM scaffolding that cracked BOSS anti-scraping becomes a reusable toolkit for game scenarios.

- [ ] **Generic game module skeleton** — multi-account management · cookie persistence · window matrix · captcha/slider strategies
- [ ] **Dailies DSL** — describe "login → sign-in → mail → dungeons → gacha pity tracking → bag cleanup" in YAML per game
- [ ] **Cross-server event alerts** — subscribe to official announcements + push "limited discounts", "first-charge bonuses", "new banner open" to WeCom / Feishu
- [ ] **Strategic gacha log** — record per-card probability distribution + luck statistics so you can whale (or not) with data
- [ ] **One subdirectory per game** — `modules/game/<game_name>/`, merge PR to go live. Adapters for *Genshin Impact*, *Arknights*, *Honkai: Star Rail*, *FGO*, *Blue Archive*, and more are waiting to be written

### ✈️ End-to-end life assistant

Not a "Q&A chatbot" — **a closed loop from one line of intent to a full plan**.

- [ ] **End-to-end travel planning** — say "I want to visit Dali for the National Day holiday" and Pulse plans flights, hotels, weather watch, visa/doc reminders, must-see spots + hidden gems + food, calendar entries, and a 24-hour pre-departure sanity recheck
- [ ] **Finance weekly** — bank / broker / Alipay APIs · LLM flags anomalies · weekend digest
- [ ] **Health guardian** — sleep / HR data · structured physical reports · proactive nudges on anomalies
- [ ] **Smart home orchestration** — Home Assistant / Matter MCP · conversational lights / HVAC / power usage
- [ ] **Inbox copilot** — auto-classify interview invites / bills / subscriptions · calendar important events · mute noise
- [ ] **Study companion** — subscribe to courses / papers / newsletters · daily key-point extraction · mistake-book-style proactive review

### 🧑‍💻 Extend & self-extend

- [ ] **Skill Generator v2** — evolve from "generate Ring 1 tool" to "**LLM generates a full module directory**" (patrol / memory / intent included) — the agent literally grows new skills
- [ ] **10k+ MCP ecosystem plug-and-play** — GitHub / Notion / Slack / Linear / Sentry — anything Claude Desktop can talk to, Pulse can too
- [ ] **Cross-device sync** — lightweight mobile channel + cloud Pulse primary, enabling real-time response on the go
- [ ] **DPO Track B closed loop** — turn real dialogue into preference pairs automatically and feed a fine-tuning pipeline — the agent learns to sound like you

### 🧬 Frontier explorations · toward a true JARVIS

No timeline commitments here — directions Pulse is committed to keep researching:

- [ ] **Emotional Intelligence** — beyond friendly tone: read your mood and adapt strategy (no harassing you during low periods, mute noisy sources during anxiety)
- [ ] **Personality Evolution** — SOUL `[MUTABLE]` beliefs truly co-evolve with you over time while `[CORE]` beliefs hold the line
- [ ] **Sleep-Time Consolidation** — inspired by Letta's Sleep-Time Compute: nightly consolidation of Recall → Archival → Core, the way humans sleep on memory
- [ ] **Embodied Agent** — robot / desktop control (OpenAI Operator style) — upgrade the agent from "watches" to "acts"
- [ ] **Agent Society** — multiple Pulse instances collaborate across users (job agent ↔ recruiter agent / travel agent ↔ hotel agent) via MCP + A2A
- [ ] **Memory watermark & right-to-forget** — GDPR-style "user-level one-click forget", audit chain for memory rollback

> Frontier directions are not vague aspirations. The SOUL evolution, five-layer memory, EventBus audit, and governance gate that underpin them **are already implemented in the current codebase** — the frontier work evolves on top of these foundations, not on a rewrite.

---

## Contributing

Pulse is at **v0** — early contributors have the opportunity to shape the project's core architecture.

Contributors from different backgrounds can find a meaningful place:

- 🎮 **Hardcore mobile gamers** — ship a `modules/game/<game_name>/` skill pack that automates sign-in / dailies / gacha. The Patchright anti-scrape scaffolding is already validated in the job domain and can be reused directly
- ✈️ **Life-automation enthusiasts** — pick a domain (travel / health / home / finance) and close the loop from "one sentence of intent → full plan"
- 🧑‍💻 **Architecture / kernel contributors** — Brain / Memory / AgentRuntime / SafetyPlane / MCP all have open problems worth depth, each backed by its own ADR
- 🎨 **Prompt / LLM researchers** — SOUL evolution, preference learning Track A, DPO collection Track B, and the three-contract Commitment Verifier are frontier problems in LLM engineering
- 📚 **Docs / evangelists** — bilingual README, ADR translations, tutorial videos, example projects — any form of contribution is welcome

Before opening a PR, read [`docs/code-review-checklist.md`](./docs/code-review-checklist.md) — the **engineering constitution**: maintainability, tests, comments, and **operable system shape** (config, observability, shutdown). Repository-level items are in [`docs/engineering/pulse-conventions.md`](./docs/engineering/pulse-conventions.md). Quality bars are high — suited to contributors who value code quality.

For architecture changes, open an RFC-style issue first; see the ADR process under [`docs/adr/`](./docs/adr/).

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
  <strong>If Pulse resonates with you, a Star ⭐ is the most tangible support for the project</strong>
  <br/>
  <sub>Built for people who want an AI that stays, not one that forgets every conversation.</sub>
</p>
