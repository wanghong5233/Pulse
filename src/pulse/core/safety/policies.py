"""SafetyPlane · Policy 函数.

三条产生外部副作用的动作各自对应一枚纯函数 policy:

* :func:`reply_policy` —— 代表用户回 HR 文字消息
* :func:`send_resume_policy` —— 发简历附件给 HR
* :func:`card_policy` —— 点击面试 / 换简历卡片 (不可逆)

**为什么 policy 是 Python 函数, 不是 YAML 规则?**

Pulse 是单用户自部署, 规则条目 ≤ 10 条, 3 个 intent 写死. 这种规模下:

* YAML DSL 的代价 (schema 校验器 / predicate 注册表 / rule_engine 求值器)
  比收益 (运营改规则不碰代码) 大一个数量级, 且本系统从不打算支持 "运营改
  规则" 这个场景.
* Python 函数天然拿到: mypy/IDE 导航 / 单测直接调用 / 重构器可追踪 ——
  这些在 YAML 路径下要重新造轮子.
* Claude Code / OpenAI Operator / AWS AgentCore 的工业界经验是: 凡自用
  / 单租户 agent, policy 都直接写在代码里; YAML/Rego 的场景是多租户平台
  策略商店 —— 不是 Pulse 的形态.

参见 ``docs/adr/ADR-006-v2-SafetyPlane.md`` 与
``docs/engineering/safety-rules-vs-intelligence.md``.

**三个 policy 的共同契约**:

* 入参 ``(intent: Intent, ctx: PermissionContext) -> Decision``.
* 永不抛异常 —— 即便内部分支断言失败, 也必须包装成 ``Decision.ask`` 返回,
  让调用方 (service 层) 始终拿到合法 Decision. 让 Policy 异常冒到 service
  层等同于把"安全面故障"升级为"业务流中断", 违反 "fail-to-ask" 原则.
* 纯函数: 不持有状态, 不读环境变量, 不触达外部 IO. 任何状态都由 ctx 传入.

**判决原则** (Deny-First → Allow → Ask):

1. 先看显式禁令 (当前 MVP 未登记任何 deny, 留给未来打开)
2. 再看显式豁免: session_approvals / profile 举证
3. 兜底 Ask —— 不确定的一律问用户, 永不沉默放行

MVP 仍不打算实现 "代写用户声明" 类 Allow 分支 —— 那些需要极强 profile
schema 才能避免 LLM 编造, 属于 Phase C 以后的扩展.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from pulse.core.safety.context import PermissionContext
from pulse.core.safety.decision import AskRequest, Decision, ResumeHandle
from pulse.core.safety.intent import Intent

__all__ = (
    "DEFAULT_ASK_TIMEOUT_SECONDS",
    "DEFAULT_RESUME_INTENT",
    "DEFAULT_RESUME_PAYLOAD_SCHEMA",
    "profile_covers",
    "session_approved",
    "reply_policy",
    "send_resume_policy",
    "card_policy",
)


# IM 里超过这个时长仍未回复的 Ask, Store.timeout() 会把它标 timed_out.
# 2 小时的量级是 Claude Code / Cursor 等工具用户体验观察得出的妥协:
# 短 (<30min) 会在用户打个电话期间就超时; 长 (>1 day) 让 Brain 占着半完成的
# 对话不推进, 影响下一轮 patrol 重新评估.
DEFAULT_ASK_TIMEOUT_SECONDS = 7200

# Resume 路径的静态元数据, 与 resume.py 的 DEFAULT_PAYLOAD_SCHEMA 对齐.
# intent 名在 Pulse 的 IntentRouter 里不真的存在 —— 它只是 ResumeHandle 的
# 路由 key, 审计里区分"这是一次 Resume 而不是普通 intent"即可.
DEFAULT_RESUME_INTENT = "system.task.resume"
DEFAULT_RESUME_PAYLOAD_SCHEMA = "safety.v1.user_answer"


# ── 辅助 ────────────────────────────────────────────────────────────────


def profile_covers(profile: Mapping[str, Any], keys: Iterable[str]) -> bool:
    """Profile 是否为给定字段都给出了非空证据.

    "覆盖" 的定义故意严格: key 必须存在, 值必须非 None, 若是 str 则去空白
    后非空. LLM 有时会吐空串或 "未知" 字样, 那种答案不该算举证成功, 否则
    policy 就把决策权交回给 LLM 自己, 失去 deterministic gate 的意义.
    """
    for key in keys:
        if not isinstance(key, str) or not key.strip():
            return False
        if key not in profile:
            return False
        value = profile[key]
        if value is None:
            return False
        if isinstance(value, str) and not value.strip():
            return False
    return True


def session_approved(ctx: PermissionContext, token: str) -> bool:
    """用户在本会话内是否显式放行过指定 token (rule_id 或动作键).

    Context.session_approvals 是 frozenset[str], 预期 token 的规范形如
    ``reply:<conversation_id>:<draft_hash>`` 或 ``resume:<hr_id>`` —— 由
    Resume 路径在用户答 "y" 时注入. MVP 的 session_approvals 总是空,
    本函数等价于常数 False, 预留接口给 B.4 之后的 session 半信任机制.
    """
    if not isinstance(token, str) or not token.strip():
        return False
    return token in ctx.session_approvals


def _ask(
    *,
    question: str,
    task_id: str,
    module: str,
    draft: str | None = None,
    context: Mapping[str, Any] | None = None,
    timeout_seconds: int = DEFAULT_ASK_TIMEOUT_SECONDS,
    reason: str,
    rule_id: str,
) -> Decision:
    """包装 ``Decision.ask`` 构造器, 统一 ResumeHandle 的路由元数据.

    让三个 policy 都走同一个出口, 避免 handle 字段手抖漂移 (曾经
    payload_schema 一度出现 ``safety.resume.v1`` / ``safety.v1.user_answer``
    两个值并存的幽灵 bug).
    """
    return Decision.ask(
        reason=reason,
        rule_id=rule_id,
        ask_request=AskRequest(
            question=question,
            resume_handle=ResumeHandle(
                task_id=task_id,
                module=module,
                intent=DEFAULT_RESUME_INTENT,
                payload_schema=DEFAULT_RESUME_PAYLOAD_SCHEMA,
            ),
            timeout_seconds=timeout_seconds,
            draft=draft,
            context=dict(context or {}),
        ),
    )


# ── Policies ────────────────────────────────────────────────────────────


def reply_policy(intent: Intent, ctx: PermissionContext) -> Decision:
    """代表用户回 HR 一条文字消息.

    Intent.args 约定字段:

    * ``conversation_id`` (str): 会话主键, 同时用作 session_approvals key 的
      一部分, 允许用户一次授权只对一个对话生效.
    * ``hr_label`` (str): HR 侧可读标签 (HR 名 / 公司), 用于 Ask 文案.
    * ``hr_message`` (str): HR 最近一条消息, 转述给用户确认.
    * ``draft_text`` (str): 要代发的草稿原文.
    * ``draft_hash`` (str): draft_text 的稳定哈希, 用作一次性 session
      approval 的 key, 避免 "用户同意发内容 A, Agent 偷换成 B" 的绕过.

    **判决优先级**:

    1. session_approved(``reply:<conversation_id>:<draft_hash>``) → allow.
    2. profile 对 intent.evidence_keys 给出完整证据 → allow (MVP 不走此分支,
       evidence_keys 默认空, 留给 Phase C 扩展).
    3. 其它一律 ask —— 让用户看草稿再定.
    """
    args = intent.args
    conversation_id = str(args.get("conversation_id") or "").strip()
    draft_hash = str(args.get("draft_hash") or "").strip()

    if conversation_id and draft_hash:
        token = f"reply:{conversation_id}:{draft_hash}"
        if session_approved(ctx, token):
            return Decision.allow(
                reason="session_approved",
                rule_id="job_chat.reply.session_approval",
            )

    if intent.evidence_keys and profile_covers(
        ctx.profile_view, intent.evidence_keys
    ):
        return Decision.allow(
            reason="profile_evidenced",
            rule_id="job_chat.reply.profile_evidence",
        )

    hr_label = str(args.get("hr_label") or "HR").strip() or "HR"
    hr_message = str(args.get("hr_message") or "").strip()
    draft_text = str(args.get("draft_text") or "").strip()
    question_lines: list[str] = [
        f"HR {hr_label} 需要你确认是否代回复。",
    ]
    if hr_message:
        question_lines.append(f"HR 刚发: {hr_message}")
    if draft_text:
        question_lines.append(f"草稿: {draft_text}")
    question_lines.append("回 'y' 发送, 回 'n' 拒绝, 或直接发新内容改写。")
    return _ask(
        question="\n".join(question_lines),
        task_id=ctx.task_id,
        module=ctx.module,
        draft=draft_text or None,
        context={
            "conversation_id": conversation_id,
            "hr_label": hr_label,
            "hr_message": hr_message,
        },
        reason="job_chat.reply.requires_user_confirmation",
        rule_id="job_chat.reply.ask_default",
    )


def send_resume_policy(intent: Intent, ctx: PermissionContext) -> Decision:
    """发简历附件给某位 HR.

    Intent.args 约定字段:

    * ``conversation_id`` (str): 会话主键.
    * ``hr_id`` (str): HR 侧唯一标识, 作为一次性 session_approval 的主键 ——
      用户说"对这位 HR 都自动发"只对 hr_id 生效, 不泛化到其它 HR.
    * ``hr_label`` (str): 展示名.
    * ``resume_profile_id`` (str): 简历档案 id, 仅用于审计追踪, 不参与判决.

    **判决优先级**: session_approved → allow, 否则 ask. 简历是一次性可见的
    强副作用, 无 profile 证据分支 —— 不存在 "代写简历 = 用户早已声明的事
    实" 这种 Allow 语义.
    """
    args = intent.args
    hr_id = str(args.get("hr_id") or "").strip()
    if hr_id and session_approved(ctx, f"resume:{hr_id}"):
        return Decision.allow(
            reason="session_approved_for_hr",
            rule_id="job_chat.send_resume.session_approval",
        )

    hr_label = str(args.get("hr_label") or "HR").strip() or "HR"
    question = f"HR {hr_label} 要简历, 是否发送? 回 'y' 确认, 回 'n' 拒绝。"
    return _ask(
        question=question,
        task_id=ctx.task_id,
        module=ctx.module,
        context={"hr_label": hr_label, "hr_id": hr_id},
        reason="job_chat.send_resume.requires_user_confirmation",
        rule_id="job_chat.send_resume.ask_default",
    )


def card_policy(intent: Intent, ctx: PermissionContext) -> Decision:
    """点击 HR 发来的面试 / 换简历等卡片.

    Intent.args 约定字段:

    * ``conversation_id`` (str): 会话主键.
    * ``card_type`` (str): 卡片类型枚举值 (interview / exchange_resume / ...).
    * ``card_title`` (str): 卡片标题, 用于 Ask 文案.
    * ``card_type_human`` (str): 卡片类型的人类可读名, 如 "面试邀请".
    * ``suggested_action`` (str): 建议操作文案, 如 "接受" / "拒绝".

    **判决**: 永远 ask. 卡片动作往往绑定面试时间 / 抉择意图, 用户本人才能
    判断, Agent 永无豁免. 未来若出现高频重复点 (如每天 15 个换简历卡片)
    再考虑 session_approved 分支 —— 目前 YAGNI.
    """
    _ = ctx  # card_policy 目前不消费 ctx 的任何字段, 保留签名以保持一致性.
    args = intent.args
    card_type_human = str(args.get("card_type_human") or "卡片").strip() or "卡片"
    card_title = str(args.get("card_title") or "").strip()
    suggested = str(args.get("suggested_action") or "").strip()
    parts: list[str] = [f"HR 发来{card_type_human}"]
    if card_title:
        parts.append(f"「{card_title}」")
    question_lines: list[str] = ["".join(parts) + "。"]
    if suggested:
        question_lines.append(f"当前建议: {suggested}。")
    question_lines.append("回 'y' 接受, 回 'n' 拒绝, 或发消息让我先替你问清楚。")
    return _ask(
        question="\n".join(question_lines),
        task_id=ctx.task_id,
        module=ctx.module,
        context={
            "card_type": str(args.get("card_type") or ""),
            "card_title": card_title,
        },
        reason="job_chat.card.requires_user_confirmation",
        rule_id="job_chat.card.ask_default",
    )
