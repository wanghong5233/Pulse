"""SafetyPlane · Resume 回路.

Suspend-Ask-Resume-Reexecute 四段的后三段落到 IM 通道层. 架构全貌见
``docs/adr/ADR-006-v2-SafetyPlane.md``:

    Module service 产生 side-effect 前
       → policy function 评估 → Decision.ask
       → SuspendedTaskStore.create (挂起并记录已处理状态)
    ─────────────────────────────────────────────────
    ↓ outbound (render_ask_for_im)
    Notifier / adapter.send OutgoingMessage 给用户
    ─────────────────────────────────────────────────
    ↓ 用户下一条 IM 消息 (本模块 try_resume_suspended_turn)
       → store.resolve(payload)
       → 查 ``executors`` 映射 → 立即回调 ``ResumedTaskExecutor`` 重跑 intent
       → 把执行摘要附在确认消息里发给用户

resume intent 名 ``system.task.resume`` 是 ResumeHandle 的路由 key, 并非
IntentRouter 里的真实 intent —— Pulse 的 IntentRouter 是前缀路由器,
Resume 语义 ("workspace 有 awaiting 时下条消息就是答案") 不需要 NLU,
所以落地为入站前置函数, 不改 IntentRouter 表.

**为什么 Resume 要立即重跑 intent, 而不是等下轮 patrol?**

最初 v2 设计是"resolve + 归档 + 等下轮 patrol 重评估", 但实测发现:
周期性任务可能反复看到同一外部输入, 或者因为挂起时已标记为 processed
而不再看到它。两种情况都会让用户确认和真实副作用脱节。

所以 Resume 必须当场回调业务侧把 intent 重跑. Executor 接口让本层保持
"只认 SuspendedTask / 只回 IM" 的单一职责, 真正的外部动作由业务模块
自己实现 —— core/safety 不 import 任何 business module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
from typing import Any, Literal, Mapping, Protocol, runtime_checkable

from pulse.core.safety.decision import AskRequest
from pulse.core.safety.suspended import (
    SuspendedTask,
    SuspendedTaskStore,
    TaskAlreadyTerminalError,
    TaskNotFoundError,
)

_LOGGER = logging.getLogger(__name__)

__all__ = (
    "DEFAULT_PAYLOAD_SCHEMA",
    "SUPPORTED_PAYLOAD_SCHEMAS",
    "ResumedExecution",
    "ResumedExecutionStatus",
    "ResumedTaskExecutor",
    "ResumeOutcome",
    "ResumeOutcomeKind",
    "build_resume_payload",
    "render_ask_for_im",
    "try_resume_suspended_turn",
)


# MVP 只实现 "safety.v1.user_answer" schema: 把用户整条消息当 answer, 附
# received_at. 未来按 domain 扩展时, 新 schema 先在此处登记再发新 payload
# 约定. payload_schema 字段用字符串而不是 Enum, 是为了跨进程序列化稳定
# (ResumeHandle.payload_schema 会写入 SuspendedTask.to_dict -> workspace_facts).
#
# 命名必须与 ``policies.DEFAULT_RESUME_PAYLOAD_SCHEMA`` 一致: AskRequest 的
# 生产端 (policies._ask) 用这个 schema 构造 ResumeHandle, 消费端 (本模块
# build_resume_payload) 也必须认同一个 schema —— 否则每次 ask 到 resume
# 都会被这里拒成 "schema_rejected".
DEFAULT_PAYLOAD_SCHEMA = "safety.v1.user_answer"
SUPPORTED_PAYLOAD_SCHEMAS: frozenset[str] = frozenset((DEFAULT_PAYLOAD_SCHEMA,))

ResumeOutcomeKind = Literal[
    "no_awaiting",        # workspace 当前没有挂起任务, 本条消息不是 resume
    "resolved",           # 正常 resolve, SuspendedTask 归档为 resumed
    "ambiguous",          # 同一 workspace 多个挂起任务, 需要用户带 task_id 回复
    "schema_rejected",    # payload_schema 不支持, 明文回 IM 告知用户
    "task_terminal",      # 竞态: 同一任务已被 timeout/deny/resumed
    "store_error",        # 持久化层抛错, 留 warn 给运维 (fail-loud 语义)
]

ResumedExecutionStatus = Literal[
    # 确认后成功把 intent 跑完 —— 副作用落地, status + ok 透传业务层
    "executed",
    # 用户明确拒绝 ("n" / "不" / "取消" 等), 没有执行
    "declined",
    # 用户答的文本既不像确认也不像拒绝 —— 任务已 resume (不再挂起), 但也
    # 没执行. 下一轮 patrol / 下条 HR 消息会重新征询.
    "undetermined",
    # 无业务层 executor 注册这个 module (配置问题 / 回滚了 module),
    # 走 resolve 但不执行. 运维可从日志看到 warn.
    "executor_missing",
    # executor 抛异常 / 业务侧执行失败. 归档仍成功, 细节留日志 + user_reply.
    "failed",
]


@dataclass(frozen=True, slots=True)
class ResumedExecution:
    """业务模块回调重跑 intent 的结构化结果.

    Field 语义:

    * ``status`` / ``ok``: status 是分类, ok 是布尔——仅 ``executed`` 且
      ``ok=True`` 才代表"已发到 HR"。``declined`` 也允许 ``ok=True``
      (用户主动拒绝是合理结果, 不是异常).
    * ``summary``: 一句话反馈, 会被 ``try_resume_suspended_turn`` 拼到
      确认消息后面发给用户. 禁止包含 PII / 内部 trace_id; 业务侧自己把文
      案写成面向用户的中文口吻.
    * ``detail``: 供审计事件消费的结构化字段(intent 名、最终状态、错误
      摘要等), 不会出现在 IM 文案里.
    """

    status: ResumedExecutionStatus
    ok: bool
    summary: str
    detail: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class ResumedTaskExecutor(Protocol):
    """业务模块把"一次已 resolve 的任务"接回去重跑的回调.

    **契约**:

    * **不抛异常** —— 内部失败必须包成 ``ResumedExecution(status="failed")``
      返回, 否则 safety core 会把整个 resolve 路径打断, 用户只能看见一个
      空错误.
    * **同步执行**: safety core 从 IM 入站线程直接调, 如果业务侧要做长
      耗时 IO 自己安排 thread pool, 不要阻塞住 IM 响应超过 5s.
    * **幂等**: 同一 ``(workspace_id, task_id)`` 被重复喂入时, 业务侧应
      识别 task 已 ``resumed`` 并返回既有结果而非再发一次. 防御 IM 重投
      / 用户连点 y.
    """

    def __call__(
        self,
        *,
        task: SuspendedTask,
        user_answer: str,
    ) -> ResumedExecution: ...


@dataclass(frozen=True, slots=True)
class ResumeOutcome:
    """try_resume_suspended_turn 的结构化返回.

    调用方根据 ``kind`` 决定下一步:

    * ``no_awaiting``: 继续常规 Brain 链路, 本消息与 Resume 无关.
    * ``resolved``: 给用户回确认消息 ``user_reply``, **不**再跑 Brain.
      若附带 ``execution``, 说明 ``executors`` 注册表命中了该 module 的
      业务回调, 原 intent 已就地重跑 —— user_reply 已经带上摘要, 不需要
      调用方再追加反馈.
    * ``ambiguous`` / ``schema_rejected`` / ``task_terminal`` / ``store_error``:
      给用户回明文解释 (不得悄悄失败), **不**再跑 Brain, 也不触达 executor.
    """

    kind: ResumeOutcomeKind
    task: SuspendedTask | None = None
    user_reply: str = ""
    execution: ResumedExecution | None = None

    @property
    def should_reply(self) -> bool:
        """是否需要给用户发一条 IM 消息."""
        return bool(self.user_reply)

    @property
    def should_skip_brain(self) -> bool:
        """是否应绕过本轮 Brain 调用."""
        return self.kind != "no_awaiting"


# ── IM 渲染 ─────────────────────────────────────────────────


def render_ask_for_im(
    ask: AskRequest,
    *,
    channel: str = "",
    include_task_id: bool = True,
) -> str:
    """把 AskRequest 渲染成纯文本 IM 消息 (企业微信主力).

    企业微信 / 飞书 / CLI 当前都走 text-only 通道 (``channel/base.py``
    ``OutgoingMessage.text``), 所以这里不假设富卡片能力 —— 若未来上卡片,
    新增一个 ``render_ask_for_card`` 即可, 不改本函数。

    结构:

        [需要你确认]
        {question}

        建议回复:
        {draft}

        ---
        任务 ID: {task_id}
        超时: {timeout_seconds} 秒内未回复将视为未确认

    设计取舍:
    * **question / draft 分开两段**, 不把 draft 拼到 question 里, 让用户
      一眼能区分"机器问什么"和"机器建议怎么答"。
    * **task_id 默认出现**, 方便用户在同一会话有多条挂起时手动 @。
      调用方验证过 workspace 唯一 awaiting 时可关掉 (``include_task_id=False``)。
    * **不**发送 ask.context, 因为 context 里常含内部 metadata (trace_id /
      session_approvals), 不适合直接打给用户; 调用方若需要展示片段, 在
      ask.draft 里自行拼接。
    """
    if not isinstance(ask, AskRequest):
        raise TypeError(
            f"render_ask_for_im requires AskRequest, got {type(ask).__name__}"
        )

    lines: list[str] = ["[需要你确认]", ask.question.strip()]
    if ask.draft:
        lines.extend(["", "建议回复:", ask.draft.strip()])
    lines.extend([
        "",
        "---",
        f"超时: {ask.timeout_seconds} 秒内未回复将视为未确认",
    ])
    if include_task_id:
        lines.append(f"任务 ID: {ask.resume_handle.task_id}")
    # channel 当前不影响渲染 (text 通用), 预留签名便于后续按通道差异化.
    _ = channel
    return "\n".join(lines)


# ── 用户回复 → resolve payload ──────────────────────────────


def build_resume_payload(
    *,
    user_text: str,
    schema: str,
    received_at: datetime | None = None,
) -> dict[str, Any]:
    """按 ResumeHandle.payload_schema 把用户回复构造成 resolve() 的 payload.

    MVP 只支持 ``safety.v1.user_answer``:

        {"schema": "safety.v1.user_answer",
         "answer": <user_text>,
         "received_at": <isoformat utc>}

    * answer **保留原始文本**, 不做 trim —— 历史对话里空白/换行有语义
      (用户发 "好的 "和 "好的" 在审计时需要可区分)。但空字符串拒收,
      因为"什么都没填"等于无效答案, 应该走 timeout 路径, 不该被当 resume。
    * received_at 缺省时取 UTC now。调用方 (Channel Adapter) 若有权威的
      远端时间戳 (如企业微信 msg.create_time), 应传进来, 审计一致。
    * 未知 schema 抛 ValueError, 让上层生成 ResumeOutcome(schema_rejected)。
    """
    if schema not in SUPPORTED_PAYLOAD_SCHEMAS:
        raise ValueError(
            f"unsupported resume payload schema: {schema!r}; "
            f"supported={sorted(SUPPORTED_PAYLOAD_SCHEMAS)}"
        )
    if not isinstance(user_text, str) or not user_text.strip():
        raise ValueError("resume payload rejects empty user_text")
    if received_at is None:
        received_at = datetime.now(timezone.utc)
    elif received_at.tzinfo is None:
        raise ValueError("received_at must be timezone-aware")
    return {
        "schema": schema,
        "answer": user_text,
        "received_at": received_at.isoformat(),
    }


# ── 入站前置钩子 ────────────────────────────────────────────


def try_resume_suspended_turn(
    *,
    store: SuspendedTaskStore,
    workspace_id: str,
    user_text: str,
    received_at: datetime | None = None,
    executors: Mapping[str, ResumedTaskExecutor] | None = None,
) -> ResumeOutcome:
    """若 workspace 有唯一 awaiting 任务, 视本消息为 Resume 答复.

    同一 workspace 内如果有多个 awaiting 任务, 本函数不会猜测用户要确认
    哪一个, 而是返回 ``kind="ambiguous"`` 并要求用户带 task_id 重新回复。
    这是外部副作用系统的安全边界:宁可多问一次, 不把确认应用到错误任务。

    入参:
    * ``workspace_id``: 通常等于 session_id (``<channel>:<user_id>``),
      见 server.py 里 session_id → workspace_id 的映射。
    * ``user_text``: 用户在 IM 里发的整条消息文本。
    * ``received_at``: IM 通道的权威时间戳, 缺省取 UTC now。
    * ``executors``: ``module -> ResumedTaskExecutor`` 注册表. 若 target
      task.module 命中, resolve 之后会**就地回调**让业务侧重跑 intent。
      未传或未命中时退化为"仅归档", 运维从日志看到警告; 这一
      退化路径只应在单元测试 / module 未接通时出现, 生产环境必须注册.

    Returns: :class:`ResumeOutcome`, 不抛异常 (底层 store 抛都转成
    ``kind="store_error"``, 审计由 store 自己的 logger 承担)。
    """
    if not isinstance(workspace_id, str) or not workspace_id.strip():
        # workspace 未解析时, 无从查挂起任务 → 按 "无挂起" 处理, 不打断链路.
        # (MVP 里仅 IM 通道触发, 都能拿到 session_id, 这里是防御性兜底.)
        return ResumeOutcome(kind="no_awaiting")

    try:
        awaiting = store.list_awaiting(workspace_id=workspace_id)
    except Exception:  # noqa: BLE001
        _LOGGER.exception(
            "safety.resume.list_awaiting_failed workspace_id=%s",
            workspace_id,
        )
        return ResumeOutcome(
            kind="store_error",
            user_reply=(
                "抱歉, 我暂时无法确认是否有待确认任务 (存储故障)。"
                "请稍后重试, 或联系运维。"
            ),
        )
    if not awaiting:
        return ResumeOutcome(kind="no_awaiting")

    if len(awaiting) > 1:
        _LOGGER.warning(
            "safety.resume.ambiguous workspace_id=%s awaiting_count=%d task_ids=%s",
            workspace_id,
            len(awaiting),
            [task.task_id for task in awaiting],
        )
        return ResumeOutcome(
            kind="ambiguous",
            user_reply=_render_ambiguous_message(awaiting),
        )

    target = awaiting[0]
    schema = target.ask_request.resume_handle.payload_schema

    try:
        payload: Mapping[str, Any] = build_resume_payload(
            user_text=user_text,
            schema=schema,
            received_at=received_at,
        )
    except ValueError as exc:
        reason = str(exc)
        _LOGGER.warning(
            "safety.resume.schema_rejected task_id=%s schema=%s reason=%s",
            target.task_id,
            schema,
            reason,
        )
        return ResumeOutcome(
            kind="schema_rejected",
            task=target,
            user_reply=_render_reject_message(target, reason=reason),
        )

    try:
        resolved = store.resolve(
            workspace_id=workspace_id,
            task_id=target.task_id,
            payload=payload,
        )
    except TaskAlreadyTerminalError:
        _LOGGER.warning(
            "safety.resume.task_terminal task_id=%s workspace_id=%s",
            target.task_id,
            workspace_id,
        )
        return ResumeOutcome(
            kind="task_terminal",
            task=target,
            user_reply=(
                f"你的回答已收到, 但任务 {target.task_id} 已在此之前结束"
                f" (可能已超时或被取消), 本次回答不会被执行。"
            ),
        )
    except TaskNotFoundError:
        # 竞态: list_awaiting 后任务已被清除. 走 no_awaiting 语义 —— 对用户
        # 最友好 (保留"下一轮可能是新 turn"的可能性), 而不是误导 "resolved".
        return ResumeOutcome(kind="no_awaiting")
    except Exception:  # noqa: BLE001
        _LOGGER.exception(
            "safety.resume.resolve_failed task_id=%s workspace_id=%s",
            target.task_id,
            workspace_id,
        )
        return ResumeOutcome(
            kind="store_error",
            task=target,
            user_reply=(
                "抱歉, 保存你的回答时出错。请稍后重发, "
                "若仍失败请联系运维。"
            ),
        )

    execution = _invoke_executor(
        executors=executors,
        task=resolved,
        user_answer=user_text,
    )
    _LOGGER.info(
        "safety.resume.resolved task_id=%s module=%s intent=%s "
        "execution_status=%s execution_ok=%s",
        resolved.task_id,
        resolved.module,
        resolved.original_intent.name,
        execution.status if execution is not None else None,
        execution.ok if execution is not None else None,
    )
    return ResumeOutcome(
        kind="resolved",
        task=resolved,
        user_reply=_render_confirm_message(resolved, execution=execution),
        execution=execution,
    )


# ── 内部渲染 helpers ─────────────────────────────────────────


def _invoke_executor(
    *,
    executors: Mapping[str, ResumedTaskExecutor] | None,
    task: SuspendedTask,
    user_answer: str,
) -> ResumedExecution | None:
    """查注册表找到 task.module 的 executor 并同步回调, 捕获一切异常.

    该函数是 Resume → Re-execute 回路里"业务和 safety 的唯一 seam", 它
    必须:

    * **不抛异常**: executor 即便违反契约也要被降级为
      ``status="failed"``, 否则整个 resolve 链路 (已写盘归档) 会在调用
      点报错, 用户只会看到一个 IM 空文案.
    * **不触达 IntentRouter**: 本层不构造 IncomingMessage, 不经过
      Brain —— 正因业务侧可以直接把 session_approvals 注入并回跑 intent,
      就不需要再经过 "识别意图" 这一步.
    """
    if executors is None:
        return None
    executor = executors.get(task.module)
    if executor is None:
        _LOGGER.warning(
            "safety.resume.executor_missing module=%s task_id=%s intent=%s "
            "(task resumed but business-side re-execute not registered; "
            "fallback: wait for next patrol / HR message)",
            task.module,
            task.task_id,
            task.original_intent.name,
        )
        return ResumedExecution(
            status="executor_missing",
            ok=False,
            summary=(
                "已记录你的回复, 但当前没有自动重发通道, 将在下次巡检时"
                "重新评估。"
            ),
        )
    try:
        result = executor(task=task, user_answer=user_answer)
    except Exception as exc:  # noqa: BLE001
        _LOGGER.exception(
            "safety.resume.executor_raised module=%s task_id=%s intent=%s",
            task.module,
            task.task_id,
            task.original_intent.name,
        )
        return ResumedExecution(
            status="failed",
            ok=False,
            summary=(
                "已记录你的回复, 但继续执行时出错, 请稍后检查日志。"
            ),
            detail={"error": repr(exc)},
        )
    if not isinstance(result, ResumedExecution):
        _LOGGER.error(
            "safety.resume.executor_contract_violation module=%s task_id=%s "
            "returned=%r (must be ResumedExecution)",
            task.module,
            task.task_id,
            type(result).__name__,
        )
        return ResumedExecution(
            status="failed",
            ok=False,
            summary=(
                "已记录你的回复, 但继续执行的返回值不符契约, 已中止。"
            ),
        )
    return result


def _render_confirm_message(
    task: SuspendedTask,
    *,
    execution: ResumedExecution | None,
) -> str:
    """用户答完后的确认回执 (放进 outbound IM).

    行为分叉:

    * 有 executor 反馈 (``execution is not None``): 基于 status 给用户
      明确交代 —— 已发出 / 已取消 / 未识别 / 执行失败. 这里是 "Resume
      → Re-execute" 对用户的唯一可见承诺, 必须直白.
    * 无 executor 反馈 (``execution is None``): 退化为旧文案 "已收到,
      继续跟进". 只应在测试 / module 未注册 executor 时出现.
    """
    head = "收到你的回复, 我会据此继续跟进。"
    if execution is None:
        return f"{head}\n(任务: {task.original_intent.name})"
    summary = execution.summary.strip() or "已处理。"
    return f"{summary}\n(任务: {task.original_intent.name})"


def _render_ambiguous_message(awaiting: list[SuspendedTask]) -> str:
    lines = [
        "当前有多个待确认任务。为避免执行错任务, 我不会把这条回复应用到任何一个任务。",
        "请复制对应任务 ID 一起回复, 或先处理其中一个任务。",
        "",
        "待确认任务:",
    ]
    for task in sorted(awaiting, key=lambda item: item.suspended_at)[:5]:
        lines.append(f"- {task.task_id}: {task.original_intent.name}")
    if len(awaiting) > 5:
        lines.append(f"- ... 还有 {len(awaiting) - 5} 个")
    return "\n".join(lines)


def _render_reject_message(task: SuspendedTask, *, reason: str) -> str:
    """Resume 失败必须回 IM 明文 "无法恢复任务 X, 原因: Y" —— 不悄悄失败."""
    task_id = task.task_id
    intent_name = task.original_intent.name
    # reason 可能较长, 截断防止 IM 消息超限.
    reason_short = (reason or "").strip()
    if len(reason_short) > 200:
        reason_short = reason_short[:197] + "..."
    return (
        f"抱歉, 无法把你的回答应用到任务 {task_id} ({intent_name})。\n"
        f"原因: {reason_short or '未知'}"
    )
