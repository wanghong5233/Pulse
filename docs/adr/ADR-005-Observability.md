# ADR-005: Per-Trace Observability

| 字段 | 值 |
|---|---|
| 状态 | Accepted |
| 作用域 | `src/pulse/core/logging_config.py`、`src/pulse/core/server.py`、`src/pulse/core/module.py`、`src/pulse/core/mcp_transport_http.py`、`src/pulse/core/channel/wechat_work_bot.py`、`src/pulse/core/runtime.py`、`src/pulse/mcp_servers/boss_platform_gateway.py`、`src/pulse/modules/job/_connectors/boss/connector.py`、`scripts/start.sh`、`tests/pulse/core/test_observability.py` |
| 关联 | `ADR-004-AutoReplyContract.md`、`docs/code-review-checklist.md` |

---

## 1. 现状

所有可观测信号以 **`trace_id` 为主键**:

- 每条用户消息进入时 `_dispatch_channel_message` 立刻 `set_trace_id(ctx.trace_id)`, 沿 `ContextVar` 贯穿 `brain.run` → `module.emit_stage_event` → `connector._invoke` → `mcp_transport_http` → `boss_platform_gateway.call_tool` → `_boss_platform_runtime` 线程池 worker 整条链路.
- 任意 `trace_id != "-"` 的日志记录由 `_TraceBucketHandler` 同时复制一份到 `logs/traces/<trace_id>/<service>.log`; 用户一次消息对应一个独立目录.
- 用户 turn 完成后由 `_write_turn_meta` 将 `{user_text, latency_ms, used_tools, tool_calls, answer, route, policy}` 落到 `logs/traces/<trace_id>/meta.json`.

```
logs/
├── pulse.log              主进程时序 (全部 trace 交织, 按时间排)
├── boss_mcp.log           boss MCP 子进程时序
└── traces/
    └── trace_<id>/
        ├── pulse.log      该 trace 在主进程产生的所有日志
        ├── boss_mcp.log   该 trace 在子进程产生的所有日志
        └── meta.json      该 turn 的结构化摘要
```

跨进程链路通过 HTTP header `X-Pulse-Trace-Id` 传递: 主进程 `HttpMCPTransport._open_response` 自动注入, 子进程 `/call` endpoint 读取后 `set_trace_id` 绑定到该请求的 async 上下文, executor worker 再 rebind 一次.

---

## 2. 分层职责

| 层 | 负责 | 不负责 |
|---|---|---|
| `logging_config` | `set_trace_id` contextvar、`_TraceIdFilter` 注入、`_TraceBucketHandler` 按 trace 分桶、`setup_logging(service_name)` 参数化 | 业务埋点内容、跨进程传递 |
| `mcp_transport_http` | 在出站 HTTP header 注入当前 trace | 本地日志写入、子进程行为 |
| `boss_platform_gateway` | 入站 header → `set_trace_id`; executor worker rebind; 单进程 `setup_logging("boss_mcp")` | 业务调用细节 |
| `BaseModule.emit_stage_event` | 每个 stage 事件镜像为 `logger.info("stage module=... stage=... status=... trace=... payload=...")` | stage 本身的语义 |
| 业务代码 (channel / connector / runtime) | 在契约边界打 `logger.info(...)` (10 个关键节点, 见 §4) | 自行拼 trace_id |
| 测试 `test_observability.py` | 锁死三条硬合同: 桶目录存在、主链完整、不串桶 | 桶清理策略、告警 |

---

## 3. 第一性原理

| 维度 | 分析 | 结论 |
|---|---|---|
| 事件主键 | 用户一次 turn = 一次独立故障域, 跨 turn 交织毫无调试价值 | `trace_id` 是唯一可靠主键, 其余维度 (user_id / session_id / module) 都是次级 |
| 分桶成本 | 单 turn 日志行数 ≤ 10³, 文件数 ≤ 2; open/close FD 在 turn 尺度可忽略 | 为每个 trace 独立开 `FileHandler` 成本可接受 |
| 保留策略 | 桶是短寿 artifact (post-mortem 后可删); rotate 单桶无意义 | 主 `pulse.log` 走 `TimedRotatingFileHandler`; 桶文件平铺, 清理由未来 cron 负责 |
| 跨进程传递 | MCP 协议 body 是 JSON-RPC 规范定义, 不可扩展; HTTP header 是唯一合规注入面 | `X-Pulse-Trace-Id` header, 空 trace (`-`) 不注入以免污染 |
| 宪法合规 | `code-review-checklist.md` §4/§1 可观测、fail-fast 与「不伪造静默成功」的取向; event bus 不写 logger 是静默黑洞 | `emit_stage_event` 必须镜像一条 `logger.info`, 否则所有 service 层 stage 事件完全不在 `pulse.log` 中 |

---

## 4. 接口契约

### 4.1 `logging_config.setup_logging(service_name)`

```python
setup_logging(service_name: str = "pulse") -> None
```

**不变式**:

- 幂等: 再次调用会清理已注册的 root handler, 重建以当前 `PULSE_LOG_DIR` / `PULSE_LOG_LEVEL` 为准的 handler 集. 测试必须通过 `monkeypatch.setenv` + `setup_logging()` 切换目录.
- 子进程必须显式调用 (backend 不会代它初始化). `boss_platform_gateway` 在 module import 时调用, 若检测到已有 handler (被父进程 import) 则跳过.
- 根 logger 始终含: 一个 WARNING 级 stdout 控制台 handler、一个 DEBUG 级 `<log_dir>/<service>.log` daily rotating handler、一个 DEBUG 级 `_TraceBucketHandler`.
- 不再注册 `brain.log` / `boss.log` / `memory.log` / `wechat.log` 这类 domain-splitter (全量子集, 纯噪音).

### 4.2 `logging_config.set_trace_id(trace_id)` / `get_trace_id()`

```python
set_trace_id(trace_id: str | None) -> None   # "" or None → "-"
get_trace_id() -> str
```

**不变式**:

- 必须在用户 turn / patrol tick / subagent spawn 的**单一入口**调用. 重复调用会覆盖当前 async 上下文的绑定.
- `ContextVar` 语义: async task 内继承, executor 线程不继承 — 跨线程场景必须手工 rebind (见 `_run_tool_handler_bound_to_trace`).
- `-` 是 "无 trace 绑定" 哨兵, 不会被 `_TraceBucketHandler` 分桶.

### 4.3 跨进程传递

```text
main process HttpMCPTransport._open_response
   → HTTP header: X-Pulse-Trace-Id: <trace_id>
child process /call endpoint (Header alias)
   → set_trace_id(x_pulse_trace_id)
   → executor worker rebinds inside ThreadPool
```

**不变式**:

- 空 trace (`"-"`) 不写 header, 避免覆盖 downstream 已有 trace.
- child 进程的 `meta.json` 不由 child 写, 仅由 parent 侧 `_write_turn_meta` 在 turn 结束时产出.

### 4.4 10 个关键节点埋点

每个契约边界至少一条 `logger.info`, 名称采用 `<layer>.<event>.<status>` 三段式:

| # | logger 名 | 事件键 | 触发点 |
|---|---|---|---|
| 1 | `pulse.core.channel.wechat_work_bot` | `wechat.msg.received` / `wechat.msg.reply.ok` / `wechat.msg.reply.failed` | WebSocket frame → dispatch 前 / 回复后 |
| 2 | `pulse.core.server` | `channel.msg.received` / `channel.msg.completed` | `_dispatch_channel_message` 入口 / 出口 |
| 3 | `pulse.core.brain` | `brain_run_start` (原有) | `Brain.run()` 入口 |
| 4 | `pulse.core.brain` | `brain.tool.call` (原有) | 每次 LLM 决定调用 tool |
| 5 | `pulse.modules.<name>` | `stage module=... stage=... status=... payload=...` | `BaseModule.emit_stage_event` 自动镜像 |
| 6 | `pulse.modules.job._connectors.boss.connector` | `boss.call.start` / `boss.call.end` | `_invoke` 进入 / 返回 |
| 7 | `pulse.modules.job._connectors.boss.connector` | `boss %s retryable error` (原有) | `_invoke` 重试 |
| 8 | `pulse.mcp_servers.boss_platform_gateway` | `mcp.call.start` / `mcp.call.end` / `mcp.call.error` | `/call` 入/出/异常 |
| 9 | `pulse.mcp_servers._boss_platform_runtime` | `chat_tab switched` / `chat_tab snapshot_unchanged` (原有) | `_switch_chat_tab` |
| 10 | `pulse.mcp_servers._boss_platform_runtime` | `chat_list extract attempt / empty` (原有) | `_resilient_extract_conversations_from_page` |

**不变式**: 任何新增契约边界必须同时补一条 `logger.info`; 仅走 event bus 不走 logger 的埋点视为 **静默黑洞** 直接拒绝合并.

### 4.5 `meta.json` schema

```json
{
  "trace_id": "trace_abc123",
  "channel": "cli | wechat | feishu | ...",
  "user_id": "string",
  "user_text": "原始用户输入",
  "latency_ms": 12345,
  "handled": true,
  "answer": "brain 最终答复文本",
  "used_tools": ["module.hello", "module.job_chat"],
  "stopped_reason": "answered | ...",
  "tool_calls": [
    {"index": 1, "tool": "module.xxx", "args": {...}}
  ],
  "route": {"intent": "...", "target": "...", "method": "..."},
  "policy": {"action": "safe", ...}
}
```

**不变式**: meta 失败不得阻塞主流程, 落盘异常以 `logger.exception("trace.meta.write.failed")` 留痕.

---

## 5. 硬合同测试

`tests/pulse/core/test_observability.py` 对三条硬合同做真实 on-disk 断言, 不 mock handler:

1. 一次 CLI ingest → `logs/traces/<tid>/pulse.log` + `meta.json` 必在, 且 `pulse.log` 含 `channel.msg.received` + `channel.msg.completed`.
2. 同一 bucket 内所有行必须 `trace=<tid>`, 无 `trace=-` 泄露.
3. 两次并发 turn 的 bucket 互不串台.

回归任何 "静默黑洞" (logger 无 handler / 子进程 trace 丢失 / emit_stage_event 不镜像) 均会在这三个测试中断言失败.

---

## 6. 可逆性 / 重评触发条件

当前选择基于 **文件系统 + stdlib logging**, 零额外依赖. 重评触发条件:

1. 单进程日活 trace > 5×10⁴ 且磁盘 FD 数超出 ulimit.
2. 分布式部署 (多 backend 实例) 出现 → `trace_id` 需统一收敛到集中式存储 (Loki / Tempo).
3. `meta.json` 成为结构化查询主要入口 → 迁移到 `pipeline_runs` 表 (见 ADR-003 预留设计).

此前以现有实现为准, 不提前引入 ClickHouse / Tempo 等重依赖.
