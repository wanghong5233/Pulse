"""P0 集成验证 — 验证 TaskContext → Brain → Memory 新链路的 import 和数据流。

运行方式：python -m pytest tests/test_p0_integration.py -v
或直接：python tests/test_p0_integration.py
"""

from __future__ import annotations

import os
import sys

# Ensure src/ is on the path for direct execution
_src = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
if _src not in sys.path:
    sys.path.insert(0, _src)


def test_task_context_imports():
    """验证 TaskContext 模块可以正常 import。"""
    from pulse.core.task_context import (
        ExecutionMode,
        ExecutionRequest,
        IsolationLevel,
        StopReason,
        TaskContext,
        create_heartbeat_context,
        create_interactive_context,
        create_patrol_context,
        create_subagent_context,
        create_resumed_context,
    )
    assert TaskContext is not None
    assert ExecutionMode.interactive_turn.value == "interactiveTurn"
    assert len(ExecutionMode) == 5
    assert len(IsolationLevel) == 3
    assert len(StopReason) == 13
    print("  [PASS] TaskContext imports OK")


def test_task_context_factory():
    """验证工厂函数生成正确的 TaskContext。"""
    from pulse.core.task_context import (
        ExecutionMode,
        IsolationLevel,
        create_interactive_context,
        create_patrol_context,
    )

    ctx = create_patrol_context(task_name="boss_greet.patrol")
    assert ctx.task_id == "patrol:boss_greet.patrol"
    assert ctx.mode == ExecutionMode.detached_scheduled_task
    assert ctx.isolation_level == IsolationLevel.isolated
    assert ctx.prompt_contract == "taskPrompt"
    assert ctx.trace_id.startswith("trace_")
    assert ctx.run_id.startswith("run_")
    assert ctx.token_budget == 4000

    ctx2 = create_interactive_context(session_id="cli:user1")
    assert ctx2.session_id == "cli:user1"
    assert ctx2.mode == ExecutionMode.interactive_turn
    assert ctx2.token_budget == 8000

    print("  [PASS] TaskContext factory OK")


def test_task_context_id_dict():
    """验证 id_dict() 返回完整 ID 集合。"""
    from pulse.core.task_context import create_patrol_context

    ctx = create_patrol_context(task_name="test", workspace_id="ws_001")
    ids = ctx.id_dict()
    assert "trace_id" in ids
    assert "run_id" in ids
    assert ids["task_id"] == "patrol:test"
    assert ids["workspace_id"] == "ws_001"
    print("  [PASS] TaskContext.id_dict() OK")


def test_task_context_budget():
    """验证 token budget 追踪。"""
    from pulse.core.task_context import create_interactive_context

    ctx = create_interactive_context(token_budget=1000)
    assert ctx.budget_remaining == 1000
    assert not ctx.over_budget

    ctx.consume_tokens(600)
    assert ctx.tokens_used == 600
    assert ctx.budget_remaining == 400
    assert not ctx.over_budget

    ctx.consume_tokens(500)
    assert ctx.over_budget
    assert ctx.budget_remaining == 0
    print("  [PASS] TaskContext budget tracking OK")


def test_task_context_ctx_required():
    """验证 brain.run() 签名中 ctx 是必传参数。"""
    import inspect as _inspect
    from pulse.core.task_context import TaskContext

    try:
        from pulse.core.brain import Brain
    except ImportError:
        print("  [SKIP] brain.py import skipped (langchain_core not installed)")
        return

    sig = _inspect.signature(Brain.run)
    params = sig.parameters
    assert "ctx" in params, "Brain.run() must have 'ctx' parameter"
    assert "task_context" not in params, "Brain.run() should not have legacy 'task_context' parameter"
    ctx_param = params["ctx"]
    assert ctx_param.default is _inspect.Parameter.empty, "ctx must be required (no default)"
    print("  [PASS] Brain.run(ctx=TaskContext) is required, no legacy params")


def test_memory_envelope_imports():
    """验证 MemoryEnvelope 模块可以正常 import。"""
    from pulse.core.memory.envelope import (
        MemoryEnvelope,
        MemoryKind,
        MemoryLayer,
        MemoryScope,
        conversation_envelope,
        envelope_from_task_context,
        fact_envelope,
        tool_call_envelope,
    )
    assert MemoryEnvelope is not None
    assert len(MemoryLayer) == 6
    assert len(MemoryScope) == 5
    assert len(MemoryKind) == 10
    print("  [PASS] MemoryEnvelope imports OK")


def test_envelope_from_task_context():
    """验证 envelope 工厂函数与 TaskContext 的联动。"""
    from pulse.core.memory.envelope import (
        MemoryKind,
        MemoryLayer,
        MemoryScope,
        conversation_envelope,
        fact_envelope,
    )
    from pulse.core.task_context import create_patrol_context

    ctx = create_patrol_context(task_name="test_patrol", workspace_id="ws_1")
    ids = ctx.id_dict()

    env = conversation_envelope(ids, role="user", text="hello")
    assert env.trace_id == ctx.trace_id
    assert env.run_id == ctx.run_id
    assert env.task_id == "patrol:test_patrol"
    assert env.workspace_id == "ws_1"
    assert env.kind == MemoryKind.conversation
    assert env.layer == MemoryLayer.recall
    assert env.scope == MemoryScope.session
    assert env.content["role"] == "user"
    assert env.content["text"] == "hello"

    fact = fact_envelope(
        ids,
        subject="user",
        predicate="prefers",
        object_value="dark mode",
        evidence_refs=["conv_123"],
    )
    assert fact.kind == MemoryKind.fact
    assert fact.layer == MemoryLayer.archival
    assert fact.evidence_refs == ["conv_123"]
    print("  [PASS] envelope_from_task_context OK")


def test_envelope_serialization():
    """验证 envelope 序列化/反序列化。"""
    from pulse.core.memory.envelope import MemoryEnvelope, conversation_envelope
    from pulse.core.task_context import create_interactive_context

    ctx = create_interactive_context(session_id="s1")
    env = conversation_envelope(ctx.id_dict(), role="assistant", text="hi there")

    d = env.to_dict()
    assert d["kind"] == "conversation"
    assert d["layer"] == "recall"
    assert d["trace_id"] == ctx.trace_id

    restored = MemoryEnvelope.from_dict(d)
    assert restored.trace_id == env.trace_id
    assert restored.content == env.content
    print("  [PASS] MemoryEnvelope serialization OK")


def test_execution_request():
    """验证 ExecutionRequest 数据结构。"""
    from pulse.core.task_context import ExecutionRequest, create_patrol_context

    ctx = create_patrol_context(task_name="intel.scan")
    req = ExecutionRequest(context=ctx, query="scan news", handler_name="intel.scan")
    assert req.context.task_id == "patrol:intel.scan"
    assert req.query == "scan news"
    print("  [PASS] ExecutionRequest OK")


def test_runtime_imports_task_context():
    """验证 runtime.py 可以正常 import TaskContext 相关内容。"""
    from pulse.core.runtime import AgentRuntime, RuntimeConfig
    assert AgentRuntime is not None
    print("  [PASS] runtime.py imports OK")


def test_no_legacy_compat():
    """验证 TaskContext 没有 to_legacy_metadata 兼容方法。"""
    from pulse.core.task_context import TaskContext
    assert not hasattr(TaskContext, "to_legacy_metadata"), "to_legacy_metadata should be removed"
    print("  [PASS] No legacy compat methods on TaskContext")


def test_runtime_handler_signature():
    """验证 runtime handler 签名是 Callable[[TaskContext], Any]，无 inspect 猜测。"""
    import inspect as _inspect
    from pulse.core.runtime import AgentRuntime
    sig = _inspect.signature(AgentRuntime.register_patrol)
    handler_param = sig.parameters["handler"]
    hint = handler_param.annotation
    hint_str = str(hint)
    assert "TaskContext" in hint_str, f"handler type hint should reference TaskContext, got: {hint_str}"
    print("  [PASS] runtime handler signature is Callable[[TaskContext], Any]")


def test_envelope_store_methods_exist():
    """验证 RecallMemory 和 ArchivalMemory 都有 store_envelope 方法。"""
    from pulse.core.memory.recall_memory import RecallMemory
    from pulse.core.memory.archival_memory import ArchivalMemory
    assert hasattr(RecallMemory, "store_envelope"), "RecallMemory must have store_envelope"
    assert hasattr(ArchivalMemory, "store_envelope"), "ArchivalMemory must have store_envelope"
    print("  [PASS] store_envelope() exists on both memory classes")


def test_brain_imports_task_context():
    """验证 brain.py 可以正常 import TaskContext。"""
    try:
        from pulse.core.brain import Brain
        assert Brain is not None
        print("  [PASS] brain.py imports OK")
    except ImportError as e:
        if "langchain" in str(e):
            print("  [SKIP] brain.py import skipped (langchain_core not installed in this env)")
        else:
            raise


# ── P1 Tests ──────────────────────────────────────────────

def test_prompt_contract_builder():
    """验证 PromptContractBuilder 能根据 ExecutionMode 生成不同 prompt。"""
    from pulse.core.prompt_contract import PromptContractBuilder, ContractType
    from pulse.core.task_context import (
        create_interactive_context,
        create_patrol_context,
        create_heartbeat_context,
    )

    builder = PromptContractBuilder(tool_names=["search", "calculator"])

    ctx_interactive = create_interactive_context(session_id="s1")
    contract = builder.build(ctx_interactive, "hello")
    assert contract.contract_type == ContractType.system
    assert "Pulse" in contract.text
    assert "search" in contract.text
    assert contract.token_estimate > 0

    ctx_patrol = create_patrol_context(task_name="boss_greet.patrol")
    contract_task = builder.build(ctx_patrol, "scan jobs")
    assert contract_task.contract_type == ContractType.task
    assert "scheduled task" in contract_task.text.lower()

    ctx_hb = create_heartbeat_context()
    contract_hb = builder.build(ctx_hb, "")
    assert contract_hb.contract_type == ContractType.heartbeat
    assert "heartbeat" in contract_hb.text.lower()

    print("  [PASS] PromptContractBuilder generates mode-specific prompts")


def test_hook_registry():
    """验证 HookRegistry 注册、触发、阻断。"""
    from pulse.core.hooks import HookRegistry, HookPoint, HookContext, HookResult
    from pulse.core.task_context import create_interactive_context

    registry = HookRegistry()
    call_log = []

    def observer(hctx: HookContext) -> HookResult:
        call_log.append(hctx.point.value)
        return HookResult()

    def blocker(hctx: HookContext) -> HookResult:
        return HookResult(block=True, reason="test block")

    registry.register(HookPoint.after_tool_use, observer, name="obs")
    registry.register(HookPoint.before_tool_use, blocker, name="blk")

    ctx = create_interactive_context()

    result_observe = registry.fire(HookPoint.after_tool_use, ctx, {"tool": "x"})
    assert not result_observe.block
    assert "afterToolUse" in call_log

    result_block = registry.fire(HookPoint.before_tool_use, ctx, {"tool": "y"})
    assert result_block.block
    assert result_block.reason == "test block"

    result_no_hooks = registry.fire(HookPoint.on_recovery, ctx)
    assert not result_no_hooks.block

    hooks_list = registry.list_hooks()
    assert "afterToolUse" in hooks_list
    assert "beforeToolUse" in hooks_list

    print("  [PASS] HookRegistry register/fire/block works correctly")


def test_compaction_engine():
    """验证 CompactionEngine turn→taskRun 压缩。"""
    from pulse.core.compaction import CompactionEngine, CompactionLevel
    from pulse.core.task_context import create_interactive_context

    engine = CompactionEngine()
    ctx = create_interactive_context(session_id="s1")

    steps = [
        {"tool_name": "search", "observation": "found 3 results about AI", "action": "use_tool"},
        {"tool_name": "calculator", "observation": "42", "action": "use_tool"},
        {"action": "respond", "answer": "The answer is 42."},
    ]

    output = engine.compact_turn(ctx, steps)
    assert output.level == CompactionLevel.turn_to_taskrun
    assert "search" in output.summary
    assert "calculator" in output.summary
    assert output.token_estimate > 0

    envelope = engine.to_envelope(ctx, output)
    assert envelope.task_id == ctx.task_id
    assert envelope.trace_id == ctx.trace_id
    assert envelope.content["summary"] == output.summary

    print("  [PASS] CompactionEngine turn→taskRun compression works")


def test_memory_reader_adapter():
    """验证 MemoryReaderAdapter 在无 memory 时返回空。"""
    from pulse.core.memory_reader import MemoryReaderAdapter

    adapter = MemoryReaderAdapter()
    assert adapter.read_core_snapshot() == {}
    assert adapter.read_recent("s1", 5) == []
    assert adapter.search_recall("test", "s1", 3) == []
    assert adapter.search_archival("test", 3) == []
    assert adapter.read_workspace_essentials("ws1") == {}
    print("  [PASS] MemoryReaderAdapter graceful empty returns")


# ── P2 Tests ──────────────────────────────────────────────

def test_promotion_engine():
    """验证 PromotionEngine 事实提取与晋升流程。"""
    from pulse.core.promotion import (
        PromotionEngine, RulePromotionStrategy, FactCandidate,
        PromotionPath, RiskLevel,
    )
    from pulse.core.task_context import create_interactive_context

    strategy = RulePromotionStrategy(min_occurrences=2, min_confidence=0.5)
    engine = PromotionEngine(strategy=strategy)
    ctx = create_interactive_context(session_id="s1")

    # 构造重复出现的事实
    entries = [
        {"text": "Python is a programming language", "id": "e1"},
        {"text": "Python is a programming language", "id": "e2"},
        {"text": "Python is a programming language", "id": "e3"},
        {"text": "random noise without pattern", "id": "e4"},
    ]

    results = engine.promote(ctx, entries)
    # 至少应该提取出 "Python is ..." 这个事实
    promoted = [r for r in results if r.promoted]
    assert len(promoted) >= 1, f"Expected at least 1 promotion, got {len(promoted)}"
    assert promoted[0].path == PromotionPath.recall_to_archival
    assert len(promoted[0].candidate.evidence_refs) >= 2
    print("  [PASS] PromotionEngine extracts and promotes facts")


def test_compaction_session_workspace():
    """验证 CompactionEngine 的 session 和 workspace 级压缩。"""
    from pulse.core.compaction import CompactionEngine, CompactionLevel
    from pulse.core.task_context import create_interactive_context

    engine = CompactionEngine()
    ctx = create_interactive_context(session_id="s1")

    # taskRun → session
    task_summaries = [
        "Searched for AI jobs, found 3 results",
        "Sent greeting to 2 candidates",
    ]
    session_output = engine.compact_session(ctx, task_summaries, outcome="completed")
    assert session_output.level == CompactionLevel.taskrun_to_session
    assert "AI jobs" in session_output.summary or "greeting" in session_output.summary
    assert session_output.token_estimate > 0

    # session → workspace
    session_summaries = [
        session_output.summary,
        "Another session: reviewed 5 resumes",
    ]
    ws_output = engine.compact_workspace(ctx, session_summaries)
    assert ws_output.level == CompactionLevel.session_to_workspace
    assert ws_output.token_estimate > 0

    # to_envelope 应该根据 level 选择正确的 layer/scope
    from pulse.core.memory.envelope import MemoryLayer, MemoryScope
    env_session = engine.to_envelope(ctx, session_output)
    assert env_session.layer == MemoryLayer.recall
    assert env_session.scope == MemoryScope.session

    env_ws = engine.to_envelope(ctx, ws_output)
    assert env_ws.layer == MemoryLayer.workspace
    assert env_ws.scope == MemoryScope.workspace

    print("  [PASS] CompactionEngine session/workspace levels work correctly")


def test_workspace_memory():
    """验证 WorkspaceMemory 类存在且对外只暴露统一的 Any-in/Any-out facts API。"""
    from pulse.core.memory.workspace_memory import WorkspaceMemory
    import inspect as _inspect

    assert hasattr(WorkspaceMemory, "get_summary")
    assert hasattr(WorkspaceMemory, "set_summary")
    assert hasattr(WorkspaceMemory, "get_fact")
    assert hasattr(WorkspaceMemory, "set_fact")
    assert hasattr(WorkspaceMemory, "list_facts_by_prefix")
    assert hasattr(WorkspaceMemory, "delete_fact")
    assert hasattr(WorkspaceMemory, "delete_facts_by_prefix")
    assert hasattr(WorkspaceMemory, "read_essentials")

    for removed in ("list_facts", "get_facts", "add_fact", "set_fact_json", "get_fact_value"):
        assert not hasattr(WorkspaceMemory, removed), (
            f"WorkspaceMemory should not expose legacy {removed!r}; "
            "API is unified on set_fact(Any) / get_fact(...) -> Any"
        )

    sig = _inspect.signature(WorkspaceMemory.read_essentials)
    params = list(sig.parameters.keys())
    assert "workspace_id" in params, f"read_essentials should accept workspace_id, got {params}"

    set_sig = _inspect.signature(WorkspaceMemory.set_fact)
    assert "value" in set_sig.parameters
    assert "source" in set_sig.parameters

    print("  [PASS] WorkspaceMemory class and method signatures OK")


def test_session_isolation():
    """验证 IsolatedMemoryReader 按隔离级别过滤读取。"""
    from pulse.core.memory_reader import MemoryReaderAdapter, IsolatedMemoryReader
    from pulse.core.task_context import TaskContext, ExecutionMode, IsolationLevel

    # 构造一个 mock adapter，所有方法返回非空
    class MockAdapter:
        def read_core_snapshot(self):
            return {"soul": {"name": "Pulse"}}
        def read_recent(self, session_id, limit):
            return [{"role": "user", "text": "hello"}]
        def search_recall(self, query, session_id, top_k):
            return [{"text": "match", "similarity": 0.9}]
        def search_archival(self, query, limit):
            return [{"subject": "X", "predicate": "is", "object": "Y"}]
        def read_workspace_essentials(self, workspace_id):
            return {"summary": "ws summary", "facts": []}

    mock = MockAdapter()

    # shared: 全部可读
    ctx_shared = TaskContext(
        mode=ExecutionMode.interactive_turn,
        isolation_level=IsolationLevel.shared,
    )
    reader_shared = IsolatedMemoryReader(mock, ctx_shared)
    assert reader_shared.read_core_snapshot() != {}
    assert len(reader_shared.read_recent("s1", 5)) == 1
    assert len(reader_shared.search_recall("q", "s1", 3)) == 1
    assert len(reader_shared.search_archival("q", 3)) == 1
    assert reader_shared.read_workspace_essentials(None) != {}

    # light_context: core + workspace，recall/archival 为空
    ctx_light = TaskContext(
        mode=ExecutionMode.heartbeat_turn,
        isolation_level=IsolationLevel.light_context,
    )
    reader_light = IsolatedMemoryReader(mock, ctx_light)
    assert reader_light.read_core_snapshot() != {}
    assert reader_light.read_recent("s1", 5) == []
    assert reader_light.search_recall("q", "s1", 3) == []
    assert reader_light.search_archival("q", 3) == []
    assert reader_light.read_workspace_essentials(None) != {}

    # isolated: 只有 core
    ctx_isolated = TaskContext(
        mode=ExecutionMode.detached_scheduled_task,
        isolation_level=IsolationLevel.isolated,
    )
    reader_isolated = IsolatedMemoryReader(mock, ctx_isolated)
    assert reader_isolated.read_core_snapshot() != {}
    assert reader_isolated.read_recent("s1", 5) == []
    assert reader_isolated.search_recall("q", "s1", 3) == []
    assert reader_isolated.search_archival("q", 3) == []
    assert reader_isolated.read_workspace_essentials(None) == {}

    print("  [PASS] IsolatedMemoryReader enforces isolation levels correctly")


# ── P3 Tests ──────────────────────────────────────────────

def test_recovery_ladder():
    """验证 Recovery Ladder 四级恢复 (L0 Skip → L1 Retry → L2 Degrade → L3 Abort)。"""
    from pulse.core.runtime import AgentRuntime, RuntimeConfig, RecoveryLevel, PatrolOutcome
    from pulse.core.task_context import TaskContext

    config = RuntimeConfig()
    config.max_consecutive_errors = 3
    rt = AgentRuntime(config=config)

    call_count = 0

    def failing_handler(ctx: TaskContext) -> dict:
        nonlocal call_count
        call_count += 1
        raise RuntimeError("test error")

    rt.register_patrol(
        name="test_fail.patrol",
        handler=failing_handler,
        peak_interval=60,
        offpeak_interval=120,
    )

    # 第 1 次失败: L1 retry with backoff
    rt._execute_patrol("test_fail.patrol", failing_handler)
    stats = rt._patrol_stats.get("test_fail.patrol", {})
    assert stats.get("recovery_level") == RecoveryLevel.L1_retry.value, \
        f"Expected L1_retry, got {stats.get('recovery_level')}"
    assert rt._consecutive_errors.get("test_fail.patrol") == 1
    assert stats.get("total_runs") == 1
    assert stats.get("total_turns") == 1
    assert stats.get("total_errors") == 1

    # 退避窗口内再次触发: 只记 turn，不应再次执行 handler / 记 error
    rt._execute_patrol("test_fail.patrol", failing_handler)
    stats = rt._patrol_stats.get("test_fail.patrol", {})
    assert call_count == 1, f"Expected backoff skip to avoid handler rerun, got {call_count}"
    assert stats.get("recovery_level") == RecoveryLevel.L1_retry.value
    assert stats.get("total_runs") == 1
    assert stats.get("total_turns") == 2
    assert stats.get("total_errors") == 1

    # 第 2 次失败: 清除 backoff 后再次实际执行，仍然 L1
    rt._retry_backoff.pop("test_fail.patrol", None)
    rt._execute_patrol("test_fail.patrol", failing_handler)
    assert rt._consecutive_errors.get("test_fail.patrol") == 2

    # 第 3 次失败: L3 abort + circuit break
    rt._retry_backoff.pop("test_fail.patrol", None)
    rt._execute_patrol("test_fail.patrol", failing_handler)
    stats = rt._patrol_stats.get("test_fail.patrol", {})
    assert stats.get("circuit_open") is True
    assert stats.get("recovery_level") == RecoveryLevel.L3_abort.value

    # 第 4 次: L0 skip (circuit open)
    rt._execute_patrol("test_fail.patrol", failing_handler)
    stats = rt._patrol_stats.get("test_fail.patrol", {})
    # call_count 不应增加（被 circuit breaker 跳过）
    assert call_count == 3, f"Expected 3 calls, got {call_count}"
    assert stats.get("recovery_level") == RecoveryLevel.L0_skip.value
    assert stats.get("total_runs") == 3
    assert stats.get("total_turns") == 5
    assert stats.get("total_errors") == 3

    # L2 degrade: handler 返回 {ok: false}
    def degraded_handler(ctx: TaskContext) -> dict:
        return {"ok": False, "errors": "partial failure"}

    rt.register_patrol(
        name="test_degrade.patrol",
        handler=degraded_handler,
        peak_interval=60,
        offpeak_interval=120,
    )
    rt._execute_patrol("test_degrade.patrol", degraded_handler)
    stats_d = rt._patrol_stats.get("test_degrade.patrol", {})
    assert stats_d.get("recovery_level") == RecoveryLevel.L2_degrade.value

    print("  [PASS] Recovery Ladder L0/L1/L2/L3 all work correctly")


def test_heartbeat_loop():
    """验证 HeartbeatLoop 内核自检。"""
    from pulse.core.runtime import AgentRuntime, RuntimeConfig

    rt = AgentRuntime(config=RuntimeConfig())

    result = rt.heartbeat()
    assert result["heartbeat_count"] == 1
    assert isinstance(result["is_active"], bool)
    assert result["total_patrols"] == 0
    assert result["healthy_patrols"] == 0
    assert result["circuit_open_tasks"] == []
    assert result["elapsed_ms"] >= 0

    # 第二次心跳
    result2 = rt.heartbeat()
    assert result2["heartbeat_count"] == 2
    assert "__runtime_heartbeat__" in rt.runner.engine.list_tasks()

    print("  [PASS] HeartbeatLoop self-check works correctly")


def test_manual_wake():
    """验证 ManualWake 手动触发完整 heartbeat turn + 真正执行 patrol。"""
    from pulse.core.runtime import AgentRuntime, RuntimeConfig
    from pulse.core.task_context import TaskContext

    events = []
    def mock_emitter(event_type: str, payload: dict):
        events.append(event_type)

    call_count = 0
    def ok_handler(ctx: TaskContext) -> dict:
        nonlocal call_count
        call_count += 1
        return {"ok": True}

    rt = AgentRuntime(
        config=RuntimeConfig(),
        event_emitter=mock_emitter,
    )
    rt.register_patrol(
        name="wake_test.patrol",
        handler=ok_handler,
        peak_interval=60,
        offpeak_interval=120,
        # ADR-004 §6.1.1: manual_wake / heartbeat Stage 5 MUST honor
        # ScheduleTask.enabled. This test exercises the positive path
        # (user has already enabled the patrol via IM), so we flip it
        # on at register time. The negative contract (disabled patrols
        # are skipped) is pinned by
        # test_agent_runtime_patrol_control.py::
        # test_manual_wake_skips_disabled_patrol_and_runs_enabled_one.
        enabled=True,
    )
    # 先执行一次让 stats 存在
    rt._execute_patrol("wake_test.patrol", ok_handler)
    assert call_count == 1

    result = rt.manual_wake()
    assert result["manual_wake"] is True
    assert "wake_test.patrol" in result["triggered_patrols"]
    assert call_count >= 2, f"Expected handler called at least twice, got {call_count}"
    assert "runtime.heartbeat" in events
    assert "runtime.manual_wake" in events

    print("  [PASS] ManualWake triggers heartbeat + actually executes patrols")


def test_runtime_hooks_integration():
    """验证 Runtime patrol 执行触发 Hook。"""
    from pulse.core.runtime import AgentRuntime, RuntimeConfig
    from pulse.core.hooks import HookRegistry, HookPoint, HookContext, HookResult
    from pulse.core.task_context import TaskContext

    hooks = HookRegistry()
    hook_log = []

    def log_hook(hctx: HookContext) -> HookResult:
        hook_log.append(hctx.point.value)
        return HookResult()

    hooks.register(HookPoint.before_task_start, log_hook, name="test_log")
    hooks.register(HookPoint.on_task_end, log_hook, name="test_end_log")

    rt = AgentRuntime(config=RuntimeConfig(), hooks=hooks)

    def ok_handler(ctx: TaskContext) -> dict:
        return {"ok": True}

    rt.register_patrol(
        name="test_hook.patrol",
        handler=ok_handler,
        peak_interval=60,
        offpeak_interval=120,
    )
    rt._execute_patrol("test_hook.patrol", ok_handler)

    assert "beforeTaskStart" in hook_log, f"Expected beforeTaskStart in hook_log, got {hook_log}"
    assert "onTaskEnd" in hook_log, f"Expected onTaskEnd in hook_log, got {hook_log}"

    # 测试 Hook 阻断
    def blocker(hctx: HookContext) -> HookResult:
        return HookResult(block=True, reason="test block")

    hooks.register(HookPoint.before_task_start, blocker, name="blocker", priority=1)

    hook_log.clear()
    rt._execute_patrol("test_hook.patrol", ok_handler)
    stats = rt._patrol_stats.get("test_hook.patrol", {})
    assert stats.get("last_outcome") == "skipped_not_ready"
    assert stats.get("recovery_level") == "L0_skip"
    assert stats.get("total_runs") == 1, f"Blocked turn should not count as run, got {stats.get('total_runs')}"
    assert stats.get("total_turns") == 2, f"Blocked turn should still count as turn, got {stats.get('total_turns')}"

    print("  [PASS] Runtime patrol triggers Hook lifecycle correctly")


# ── P4 Tests ──────────────────────────────────────────────────────────────


def test_subagent_context_factory():
    """验证 create_subagent_context 工厂函数。"""
    from pulse.core.task_context import (
        ExecutionMode,
        IsolationLevel,
        create_subagent_context,
    )

    ctx = create_subagent_context(
        parent_task_id="patrol:boss_greet",
        parent_session_id="sess_abc",
        workspace_id="ws_1",
        token_budget=3000,
    )
    assert ctx.mode == ExecutionMode.subagent_task
    assert ctx.isolation_level == IsolationLevel.isolated
    assert ctx.parent_task_id == "patrol:boss_greet"
    assert ctx.session_id == "sess_abc"
    assert ctx.workspace_id == "ws_1"
    assert ctx.token_budget == 3000
    assert ctx.task_id.startswith("subagent:")
    print("  [PASS] create_subagent_context OK")


def test_resumed_context_factory():
    """验证 create_resumed_context 工厂函数。"""
    from pulse.core.task_context import (
        ExecutionMode,
        IsolationLevel,
        create_resumed_context,
    )

    ctx = create_resumed_context(
        original_task_id="patrol:boss_greet",
        original_trace_id="trace_abc123",
        session_id="sess_1",
        checkpoint_data={"step_index": 5},
    )
    assert ctx.mode == ExecutionMode.resumed_task
    assert ctx.isolation_level == IsolationLevel.shared
    assert ctx.trace_id == "trace_abc123"
    assert ctx.task_id == "resumed:patrol:boss_greet"
    assert ctx.extra["checkpoint"]["step_index"] == 5
    print("  [PASS] create_resumed_context OK")


def test_subagent_lifecycle():
    """验证 Runtime 的 subagent 派生/回收生命周期。"""
    from pulse.core.runtime import AgentRuntime, RuntimeConfig, SubagentRecord

    config = RuntimeConfig()
    config.enabled = False
    rt = AgentRuntime(config=config)

    call_log = []

    def sub_handler(ctx):
        call_log.append(ctx.task_id)
        return {"answer": "sub done"}

    record = rt.spawn_subagent(
        parent_task_id="patrol:boss",
        handler=sub_handler,
        workspace_id="ws_1",
    )
    assert isinstance(record, SubagentRecord)
    assert record.status == "completed"
    assert record.result == {"answer": "sub done"}
    assert len(call_log) == 1
    assert call_log[0].startswith("subagent:")

    collected = rt.collect_subagent(record.subagent_task_id)
    assert collected is not None
    assert collected.status == "completed"

    subs = rt.list_subagents(parent_task_id="patrol:boss")
    assert len(subs) == 1
    assert subs[0]["parent_task_id"] == "patrol:boss"

    print("  [PASS] Subagent lifecycle OK")


def test_checkpoint_and_resume():
    """验证 checkpoint 保存和 resume 恢复。"""
    from pulse.core.runtime import AgentRuntime, RuntimeConfig, TaskCheckpoint

    config = RuntimeConfig()
    config.enabled = False
    rt = AgentRuntime(config=config)

    cp = TaskCheckpoint(
        task_id="patrol:boss_greet",
        trace_id="trace_xyz",
        session_id="sess_1",
        stopped_reason="budget_exhausted",
        step_index=3,
    )
    rt.save_checkpoint(cp)

    cps = rt.list_checkpoints()
    assert len(cps) == 1
    assert cps[0]["task_id"] == "patrol:boss_greet"

    resume_log = []

    def resume_handler(ctx):
        resume_log.append(ctx.trace_id)
        assert ctx.extra["checkpoint"]["step_index"] == 3
        return {"answer": "resumed ok"}

    result = rt.resume_task("patrol:boss_greet", resume_handler)
    assert result["ok"] is True
    assert result["result"] == {"answer": "resumed ok"}
    assert len(resume_log) == 1
    assert resume_log[0] == "trace_xyz"

    assert len(rt.list_checkpoints()) == 0

    result2 = rt.resume_task("nonexistent", resume_handler)
    assert result2["ok"] is False

    print("  [PASS] Checkpoint and resume OK")


def test_manual_takeover():
    """验证 manual takeover 请求/释放。"""
    from pulse.core.runtime import AgentRuntime, RuntimeConfig, TakeoverState

    config = RuntimeConfig()
    config.enabled = False
    rt = AgentRuntime(config=config)

    assert rt.takeover_state == TakeoverState.autonomous

    result = rt.request_takeover(reason="emergency")
    assert result["state"] == "human_control"
    assert rt.takeover_state == TakeoverState.human_control

    status = rt.status()
    assert status["takeover_state"] == "human_control"

    call_log = []

    def handler(ctx):
        call_log.append(1)

    rt.register_patrol(
        name="test_patrol",
        handler=handler,
        peak_interval=60,
        offpeak_interval=120,
    )
    rt._execute_patrol("test_patrol", handler)
    assert len(call_log) == 0, "Patrol should be skipped during takeover"

    stats = rt._patrol_stats.get("test_patrol", {})
    assert stats.get("total_turns") == 1
    assert stats.get("total_runs") == 0

    try:
        rt.spawn_subagent(parent_task_id="test", handler=handler)
        assert False, "Should have raised RuntimeError"
    except RuntimeError:
        pass

    release_result = rt.release_takeover(auto_restart=False)
    assert release_result["state"] == "autonomous"
    assert rt.takeover_state == TakeoverState.autonomous

    print("  [PASS] Manual takeover OK")


# ── Final Audit Tests ─────────────────────────────────────────────────────


def test_stopreason_completeness():
    """验证 StopReason 枚举包含 compacted 和 parent_cancelled。"""
    from pulse.core.task_context import StopReason
    assert hasattr(StopReason, "compacted"), "Missing StopReason.compacted"
    assert hasattr(StopReason, "parent_cancelled"), "Missing StopReason.parent_cancelled"
    assert len(StopReason) == 13
    print("  [PASS] StopReason completeness OK")


def test_envelope_new_fields():
    """验证 MemoryEnvelope 包含所有设计文档要求的字段。

    Pulse 采用 agentic search，envelope 不含 embedding 字段
    （参见 docs/Pulse-MemoryRuntime设计.md 附录 B）。
    """
    from pulse.core.memory.envelope import MemoryEnvelope
    env = MemoryEnvelope()
    assert not hasattr(env, "embedding"), "embedding should be removed (agentic search)"
    assert hasattr(env, "status"), "Missing status"
    assert hasattr(env, "updated_at"), "Missing updated_at"
    assert hasattr(env, "valid_from"), "Missing valid_from"
    assert hasattr(env, "valid_to"), "Missing valid_to"
    assert env.status == "active"
    d = env.to_dict()
    assert "embedding" not in d
    assert "status" in d
    assert "updated_at" in d
    assert "valid_from" in d
    assert "valid_to" in d
    # content 支持 str
    env2 = MemoryEnvelope(content="plain text")
    assert env2.content == "plain text"
    print("  [PASS] Envelope new fields OK")


def test_operational_memory():
    """验证 OperationalMemory 基本读写。"""
    from pulse.core.memory.operational_memory import OperationalMemory
    om = OperationalMemory(max_entries_per_task=5)
    om.write("task1", "key1", "value1")
    om.write("task1", "key2", {"nested": True})
    assert om.read("task1", "key1") == "value1"
    assert om.read("task1", "key2") == {"nested": True}
    assert om.read("task1", "missing", "default") == "default"
    all_data = om.read_all("task1")
    assert len(all_data) == 2
    stats = om.stats()
    assert stats["active_tasks"] == 1
    assert stats["total_entries"] == 2
    cleared = om.clear("task1")
    assert cleared == 2
    assert om.read("task1", "key1") is None
    print("  [PASS] OperationalMemory OK")


def test_promotion_three_paths():
    """验证 PromotionEngine 三条路径枚举和 _resolve_path。"""
    from pulse.core.promotion import PromotionEngine, PromotionPath, FactCandidate, RiskLevel
    engine = PromotionEngine()
    c1 = FactCandidate(
        subject="user", predicate="prefers", object_value="dark mode",
        confidence=0.9, risk=RiskLevel.low, evidence_refs=["e1"],
    )
    c2 = FactCandidate(
        subject="company", predicate="founded", object_value="2020",
        confidence=0.9, risk=RiskLevel.low, evidence_refs=["e1"],
    )
    assert engine._resolve_path(c1) == PromotionPath.recall_to_core
    assert engine._resolve_path(c2) == PromotionPath.recall_to_archival
    print("  [PASS] Promotion three paths OK")


def test_workspace_memory_fact_roundtrip():
    """验证 WorkspaceMemory 统一的 set_fact(Any) → get_fact() -> Any 契约。

    历史上这里有 get_facts / add_fact / set_fact_json / get_fact_value 等别名,
    统一后只保留 set_fact / get_fact / list_facts_by_prefix, value 列永远 JSON 编码。
    """
    from pulse.core.memory.workspace_memory import WorkspaceMemory, Fact
    import inspect as _inspect

    sig_get = _inspect.signature(WorkspaceMemory.get_fact)
    assert "default" in sig_get.parameters, (
        "get_fact must accept a default sentinel for missing keys"
    )
    sig_set = _inspect.signature(WorkspaceMemory.set_fact)
    # value 是 Any, 不应再限制为 str
    assert sig_set.parameters["value"].annotation in (_inspect.Parameter.empty,) or \
        "str" not in str(sig_set.parameters["value"].annotation), \
        "set_fact.value should accept Any (JSON-serialisable), not be pinned to str"

    assert Fact.__annotations__["value"] is not str, (
        "Fact.value should be Any after decoding, never raw str"
    )
    print("  [PASS] WorkspaceMemory unified fact API signature OK")


def test_archival_supersede_method():
    """验证 ArchivalMemory 有 supersede_fact 方法。"""
    from pulse.core.memory.archival_memory import ArchivalMemory
    assert hasattr(ArchivalMemory, "supersede_fact"), "Missing supersede_fact"
    print("  [PASS] ArchivalMemory.supersede_fact exists OK")


def test_envelope_roundtrip():
    """验证 MemoryEnvelope to_dict/from_dict 往返一致性。"""
    from pulse.core.memory.envelope import MemoryEnvelope, MemoryKind, MemoryLayer, MemoryScope
    env = MemoryEnvelope(
        kind=MemoryKind.fact,
        layer=MemoryLayer.archival,
        scope=MemoryScope.workspace,
        content={"subject": "test", "predicate": "is", "object": "good"},
        source="test",
        confidence=0.95,
        status="active",
        evidence_refs=["e1", "e2"],
        superseded_by="mem_abc",
    )
    d = env.to_dict()
    env2 = MemoryEnvelope.from_dict(d)
    assert env2.kind == env.kind
    assert env2.layer == env.layer
    assert env2.scope == env.scope
    assert env2.content == env.content
    assert env2.confidence == env.confidence
    assert env2.status == env.status
    assert env2.evidence_refs == env.evidence_refs
    assert env2.superseded_by == env.superseded_by
    assert env2.source == env.source
    print("  [PASS] Envelope roundtrip OK")


def test_pause_patrols():
    """验证 pause_patrols 暂停 patrol 但 heartbeat 仍可执行。"""
    from pulse.core.runtime import AgentRuntime, RuntimeConfig, TakeoverState
    config = RuntimeConfig()
    config.enabled = False
    rt = AgentRuntime(config=config)

    result = rt.pause_patrols(reason="test_pause")
    assert result["state"] == "paused"
    assert rt.takeover_state == TakeoverState.paused

    call_log = []

    def handler(ctx):
        call_log.append(ctx.task_id)

    rt.register_patrol(
        name="test_patrol",
        handler=handler,
        peak_interval=60,
        offpeak_interval=120,
    )
    rt._execute_patrol("test_patrol", handler)
    assert len(call_log) == 0, "Patrol should be skipped during pause"

    # subagent should still work during pause (unlike human_control)
    def sub_handler(ctx):
        return "ok"

    record = rt.spawn_subagent(parent_task_id="test", handler=sub_handler)
    assert record.status == "completed"

    rt.release_takeover(auto_restart=False)
    assert rt.takeover_state == TakeoverState.autonomous
    print("  [PASS] Pause patrols OK")


if __name__ == "__main__":
    tests = [
        test_task_context_imports,
        test_task_context_factory,
        test_task_context_id_dict,
        test_task_context_budget,
        test_task_context_ctx_required,
        test_no_legacy_compat,
        test_memory_envelope_imports,
        test_envelope_from_task_context,
        test_envelope_serialization,
        test_execution_request,
        test_runtime_imports_task_context,
        test_runtime_handler_signature,
        test_envelope_store_methods_exist,
        test_brain_imports_task_context,
        # P1
        test_prompt_contract_builder,
        test_hook_registry,
        test_compaction_engine,
        test_memory_reader_adapter,
        # P2
        test_promotion_engine,
        test_compaction_session_workspace,
        test_workspace_memory,
        test_session_isolation,
        # P3
        test_recovery_ladder,
        test_heartbeat_loop,
        test_manual_wake,
        test_runtime_hooks_integration,
        # P4
        test_subagent_context_factory,
        test_resumed_context_factory,
        test_subagent_lifecycle,
        test_checkpoint_and_resume,
        test_manual_takeover,
        # Final Audit
        test_stopreason_completeness,
        test_envelope_new_fields,
        test_operational_memory,
        test_promotion_three_paths,
        test_workspace_memory_fact_roundtrip,
        test_archival_supersede_method,
        test_envelope_roundtrip,
        test_pause_patrols,
    ]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except Exception as e:
            print(f"  [FAIL] {t.__name__}: {e}")
            failed += 1

    print(f"\n{'='*50}")
    print(f"P0+P1+P2+P3+P4+Final 集成验证: {passed} passed, {failed} failed")
    if failed:
        sys.exit(1)
    print("All integration checks passed!")
