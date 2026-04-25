"""Pulse ActionReport — 通用执行结果报告契约 (ADR-002).

所有 ``mutating`` / ``multi-step`` module 的 tool handler, 在执行完业务
副作用之后, 必须返回一个结构化的 ``ActionReport``, 描述"刚刚真正
发生了什么"。

为什么需要这一层
=================

问题场景 (trace_34682759d5e7)::

    LLM → job.greet.trigger(confirm_execute=true)
        → 浏览器真实投递 1 个岗位 (greeted=1, status=ok)
        → observation 返回 JSON 大杂烩 (matched_details / counts / ...)
        → LLM 基于 observation 自己"回忆"生成回复
        → CommitmentVerifier (另一个 LLM) 基于 extracted_facts "审计"
        → ❌ 两个 LLM 看到的不是同一份事实, 审计误判为"未完成"

根因是缺少一个"执行结果报告层": LLM 和 Verifier 都在做 projection,
projection 之间天然会漂移。成熟的 Agent 产品 (Claude Code / Cursor /
LangGraph ToolMessage) 都在 tool execution → final reply 之间插入一层
**结构化执行报告**, 把"到底发生了什么"固化成可验证的事实, LLM 只负责
把报告翻译成自然语言, Verifier 也拿同一份报告作为 Grounding.

通用性
======

``ActionReport`` 在 ``core`` 层定义, 不含任何业务词汇, 覆盖所有
mutating / multi-step module:

==================  ===============  ===========================  ===========================
module              action           details[*].target             典型 metrics
==================  ===============  ===========================  ===========================
job.greet           "job.greet"      岗位标题                       attempted / succeeded / failed
game.checkin        "game.checkin"   游戏名                         attempted / success / already_done
trip.plan           "trip.plan"     日期 D1/D2/D3                  days_planned / total_budget
system.login        "system.login"   平台名                         attempted / succeeded
notification.send   "notification    收件人                         total / delivered / failed
                     .send"
==================  ===============  ===========================  ===========================

协议
====

Module 在 tool handler 的返回 dict 里用 :data:`ACTION_REPORT_KEY` 挂一份
``ActionReport``, 例如::

    return {
        "ok": True,
        "greeted": 1,
        ...既有业务字段, 保留向后兼容...,
        ACTION_REPORT_KEY: ActionReport.build(
            action="job.greet",
            summary="投递了 1/1 个合适岗位",
            details=[
                ActionDetail(target="AIGC视觉生成实习", status="succeeded",
                             url="https://..."),
            ],
            metrics={"attempted": 1, "succeeded": 1, "failed": 0},
        ),
    }

Brain 的 ReAct loop 识别到 ``ACTION_REPORT_KEY`` 会:

1. 用 :meth:`ActionReport.to_prompt_lines` 渲染成一条 ``SystemMessage``,
   追加到消息流, 强制 LLM 引用 (不再"自己回忆");
2. 把 :meth:`ActionReport.to_dict` 的结果挂到 ``Receipt.action_report``,
   供 Verifier 的 judge prompt 作为**首选证据**消费.

向后兼容
========

没有返回 ``ActionReport`` 的旧 tool handler 不受影响: Brain 走原路径
(observation → extract_facts → Receipt), Verifier 走原判决逻辑.
ActionReport 是**增量能力**, 不破坏存量契约.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal

__all__ = (
    "ACTION_REPORT_KEY",
    "ActionStatus",
    "ActionDetail",
    "ActionReport",
    "extract_action_report",
)


# ──────────────────────────────────────────────────────────────
# Prompt sanitize — 通用防御: 把外部平台反爬字体的 Private Use Area
# 私有码点 (U+E000..U+F8FF) 明示为 ``«encoded»``, 而不是让乱码直接
# 流到 LLM prompt 里 (会被 LLM 默默吞掉或当成占位符原样回吐).
#
# 这个 sanitize 是 ActionReport 层的 **通用** 防线, 不针对具体平台:
# BOSS / 拉勾 / 58 等招聘站都用 PUA 做薪资数字反爬, 字体会变, 解码
# 需要运行时下载 .woff 并解析 cmap (独立课题, 见 ADR-003.1 TODO).
# 在没有解码器之前, 显式告诉 LLM "这段被平台加密了" 远比喂乱码安全.
# ──────────────────────────────────────────────────────────────

_PUA_PATTERN = re.compile(r"[\ue000-\uf8ff]+")


def _sanitize_prompt_str(value: str) -> str:
    """Replace contiguous PUA runs with ``«encoded»`` marker.

    纯文本扫描, 不修改其它字符, 对 ASCII / CJK 完全透明. 调用方
    在把字符串写进 Brain/Verifier prompt 之前统一过一遍.
    """
    if not value:
        return value
    return _PUA_PATTERN.sub("«encoded»", value)


# Detail.extras 渲染进 prompt 的白名单策略:
#
# * 只渲染 primitive (str / int / float / bool), dict/list 就不渲染,
#   避免一个 fat extras 把 prompt 撑炸 + LLM 误以为 details 行本身是
#   嵌套结构.
# * str 值统一过 PUA sanitize, 防止反爬乱码污染 prompt.
# * 丢弃空串 / None — 语义等价于"没有此字段".
# * 单条 detail 最多渲染 8 个 extras 键 (业务字段通常只有 3-5 个,
#   设这个上限防御恶意/爆炸 payload).
_MAX_EXTRAS_PER_DETAIL = 8


# ``observation`` 里挂载 ActionReport 的 key. 双下划线前缀避免与任何业务
# 字段冲突, 并且明确表示"由 runtime 识别, 不是业务数据".
ACTION_REPORT_KEY = "__action_report__"


ActionStatus = Literal[
    "succeeded",  # 所有子动作成功
    "partial",    # 有子动作成功也有失败/跳过
    "failed",     # 所有子动作失败, 或整体失败
    "preview",    # 仅生成了预览, 没有实际执行 (例: confirm_execute=false)
    "skipped",    # 被幂等检查 / 前置条件短路掉了, 未执行
    "denied",     # 被 SafetyPlane 授权层拒绝, 未触及副作用
    "suspended",  # 被 SafetyPlane 挂起等待用户裁决
]

_VALID_ACTION_STATUSES: frozenset[str] = frozenset(ActionStatus.__args__)  # type: ignore[attr-defined]


@dataclass(frozen=True, slots=True)
class ActionDetail:
    """一个子动作的结构化结果.

    ``target`` 是 **用户可读标识**, 用于直接渲染进 reply / 通知卡片
    (岗位标题 / 游戏名 / 日期 / 收件人), 不要塞技术 id.
    """

    target: str
    status: ActionStatus
    reason: str | None = None
    url: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"target": self.target, "status": self.status}
        if self.reason:
            out["reason"] = self.reason
        if self.url:
            out["url"] = self.url
        if self.extras:
            out["extras"] = dict(self.extras)
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ActionDetail":
        status = data.get("status", "succeeded")
        if status not in _VALID_ACTION_STATUSES:
            raise ValueError(f"invalid ActionDetail.status: {status!r}")
        return cls(
            target=str(data.get("target", "") or ""),
            status=status,  # type: ignore[arg-type]
            reason=data.get("reason") or None,
            url=data.get("url") or None,
            extras=dict(data.get("extras") or {}),
        )


@dataclass(frozen=True, slots=True)
class ActionReport:
    """Module 的执行结果报告 (用户可见面 + LLM/Verifier 可消费面).

    ``action`` 是结构化 module 名, 用 ``<domain>.<verb>`` 记号
    (``job.greet`` / ``game.checkin`` / ``trip.plan``), 避免按模块
    硬编码判据.

    ``summary`` 是一句话的 **用户可读结论** ("投递了 1/1 个岗位"),
    不是技术日志; LLM 允许直接引用或改写, 但不得声明 summary 里
    没有的事实.

    ``details`` 是逐项子动作 (有顺序), ``metrics`` 是数量/比率快照;
    两者语义上互补 — details 回答 "做了哪些件", metrics 回答 "做成了多少".
    """

    action: str
    status: ActionStatus
    summary: str
    details: tuple[ActionDetail, ...] = ()
    metrics: dict[str, int | float] = field(default_factory=dict)
    next_steps: tuple[str, ...] = ()
    evidence: dict[str, Any] = field(default_factory=dict)

    # ---------- 构造入口 ---------------------------------------------------

    @classmethod
    def build(
        cls,
        *,
        action: str,
        summary: str,
        details: Iterable[ActionDetail] = (),
        status: ActionStatus | None = None,
        metrics: dict[str, int | float] | None = None,
        next_steps: Iterable[str] = (),
        evidence: dict[str, Any] | None = None,
    ) -> "ActionReport":
        """推荐入口: 自动把 iterable 收敛成 tuple, 并按 details 推断 status.

        手动传 ``status`` 会覆盖推断 (例如 ``preview`` / ``skipped``
        这两种状态无法从 details 推出, 必须显式指定).
        """
        details_t = tuple(details)
        resolved = status if status is not None else cls.infer_status(details_t)
        if resolved not in _VALID_ACTION_STATUSES:
            raise ValueError(f"invalid ActionReport.status: {resolved!r}")
        return cls(
            action=str(action),
            status=resolved,
            summary=str(summary),
            details=details_t,
            metrics=dict(metrics or {}),
            next_steps=tuple(next_steps),
            evidence=dict(evidence or {}),
        )

    @staticmethod
    def infer_status(details: tuple[ActionDetail, ...]) -> ActionStatus:
        """按 details 聚合总体 status.

        约定:

        - 空 details → ``skipped`` (复杂 module 若不想走这个默认,
          请显式传 ``status=...``);
        - 全部 ``succeeded`` → ``succeeded``;
        - 至少一个 ``succeeded`` + 存在非 ``succeeded`` → ``partial``;
        - 其它 (全部 failed / skipped / 混合但无 succeeded) → ``failed``.
        """
        if not details:
            return "skipped"
        statuses = {d.status for d in details}
        if statuses == {"succeeded"}:
            return "succeeded"
        if "succeeded" in statuses:
            return "partial"
        return "failed"

    # ---------- 序列化 -----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly shape. Used for:

        * ``Receipt.action_report`` (verifier judge prompt 消费)
        * audit log / event payload
        * Brain step snapshot
        """
        out: dict[str, Any] = {
            "action": self.action,
            "status": self.status,
            "summary": self.summary,
        }
        if self.details:
            out["details"] = [d.to_dict() for d in self.details]
        if self.metrics:
            out["metrics"] = dict(self.metrics)
        if self.next_steps:
            out["next_steps"] = list(self.next_steps)
        if self.evidence:
            out["evidence"] = dict(self.evidence)
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ActionReport":
        status = data.get("status", "succeeded")
        if status not in _VALID_ACTION_STATUSES:
            raise ValueError(f"invalid ActionReport.status: {status!r}")
        details = tuple(
            ActionDetail.from_dict(d)
            for d in data.get("details") or ()
            if isinstance(d, dict)
        )
        return cls(
            action=str(data.get("action", "") or ""),
            status=status,  # type: ignore[arg-type]
            summary=str(data.get("summary", "") or ""),
            details=details,
            metrics=dict(data.get("metrics") or {}),
            next_steps=tuple(data.get("next_steps") or ()),
            evidence=dict(data.get("evidence") or {}),
        )

    # ---------- LLM prompt 渲染 -------------------------------------------

    def to_prompt_lines(self) -> list[str]:
        """渲染成 markdown-ish 纯文本行, 供 Brain 注入 SystemMessage.

        LLM 对这种 '标题 + 分级 list + 数值' 的形态比对 JSON 更容易抽取
        事实, 且不会尝试"改写"数值.

        所有字符串字段在进入 prompt 前统一过 :func:`_sanitize_prompt_str`,
        把招聘平台字体反爬的 PUA 私有码点替换成 ``«encoded»`` marker,
        让 LLM 能诚实地告诉用户"该字段被平台加密", 而不是吐乱码.

        ``details[*].extras`` 里的 primitive 业务字段 (salary / match_score
        / company / ...) 会以 ``k=v`` 形式追加到 detail 行末, 这是 LLM
        在 reply 里援引业务字段的**唯一**授权出口 — 没出现在这里的
        字段, LLM 不得在 reply 中提 (由 IMPORTANT 下方的 guardrail
        兜底).
        """
        lines: list[str] = [
            "[Action Report — ground-truth facts of what just happened]",
            f"- action: {_sanitize_prompt_str(self.action)}",
            f"- status: {self.status}",
            f"- summary: {_sanitize_prompt_str(self.summary)}",
        ]
        if self.metrics:
            metric_str = ", ".join(f"{k}={v}" for k, v in self.metrics.items())
            lines.append(f"- metrics: {metric_str}")
        if self.details:
            lines.append("- details:")
            for idx, detail in enumerate(self.details, start=1):
                head = (
                    f"  {idx}. [{detail.status}] "
                    f"{_sanitize_prompt_str(detail.target)}"
                )
                trailers: list[str] = []
                if detail.reason:
                    trailers.append(_sanitize_prompt_str(detail.reason))
                if detail.url:
                    trailers.append(detail.url)
                if detail.extras:
                    for key, value in _iter_renderable_extras(detail.extras):
                        trailers.append(f"{key}={value}")
                if trailers:
                    head += " — " + " — ".join(trailers)
                lines.append(head)
        if self.next_steps:
            lines.append("- next_steps:")
            for step in self.next_steps:
                lines.append(f"  - {_sanitize_prompt_str(step)}")
        if self.evidence:
            for k, v in self.evidence.items():
                rendered = _sanitize_prompt_str(str(v)) if isinstance(v, str) else v
                lines.append(f"- evidence.{k}: {rendered}")
        lines.append(
            "IMPORTANT: Your next reply to the user MUST be grounded in this "
            "report. Do NOT invent actions, counts, or field values not listed above."
        )
        return lines

    # ---------- Verifier 对接 ---------------------------------------------

    def to_receipt_facts(self) -> dict[str, Any]:
        """把 ActionReport 压平成 ``Receipt.extracted_facts`` 可以直接吃的 dict.

        用于**没有显式 ``ToolSpec.extract_facts`` 钩子**的 tool: 直接让
        ActionReport 当作事实来源. 已有 extract_facts 钩子的 tool 会自行
        合并其既有字段 (不会被覆盖).
        """
        facts: dict[str, Any] = {
            "action": self.action,
            "status": self.status,
        }
        for key, value in (self.metrics or {}).items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                facts[str(key)] = value
        return facts


# ──────────────────────────────────────────────────────────────
# Extras 渲染器 — 白名单 primitive, PUA sanitize, 截断上限
# ──────────────────────────────────────────────────────────────


def _iter_renderable_extras(
    extras: dict[str, Any],
) -> Iterable[tuple[str, str]]:
    """Yield ``(key, rendered_value)`` pairs for prompt rendering.

    策略:

    * 只保留 primitive (``str`` / ``int`` / ``float`` / ``bool``),
      其它类型静默丢弃;
    * ``bool`` 渲染成 ``true`` / ``false`` (统一给 LLM 一个习惯的形态);
    * ``str`` 过 :func:`_sanitize_prompt_str`, 去首尾空白;
    * 空串 / ``None`` 直接跳过;
    * 单条 detail 最多 yield :data:`_MAX_EXTRAS_PER_DETAIL` 条.

    顺序: 按原 ``extras`` dict 的插入顺序 (Python 3.7+ 保序), module
    层填 extras 时自己把"用户最关心的字段"排前面 (例如 salary > match).
    """
    yielded = 0
    for key, value in extras.items():
        if yielded >= _MAX_EXTRAS_PER_DETAIL:
            break
        if value is None:
            continue
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, (int, float)):
            rendered = str(value)
        elif isinstance(value, str):
            cleaned = _sanitize_prompt_str(value).strip()
            if not cleaned:
                continue
            rendered = cleaned
        else:
            continue
        yield str(key), rendered
        yielded += 1


# ──────────────────────────────────────────────────────────────
# Tool observation → ActionReport 抽取
# ──────────────────────────────────────────────────────────────


def extract_action_report(observation: Any) -> ActionReport | None:
    """从 tool observation 里抽出 ActionReport, 兼容三种形态:

    1. observation 本身就是 ``ActionReport`` 实例 (rare);
    2. observation 是 dict, 其中 ``ACTION_REPORT_KEY`` 挂着 ``ActionReport``;
    3. observation 是 dict, 其中 ``ACTION_REPORT_KEY`` 挂着
       ``ActionReport.to_dict()`` 的结果 (跨进程 / 跨 JSON 序列化时).

    其它情况返回 ``None``, 调用方走向后兼容路径.
    """
    if isinstance(observation, ActionReport):
        return observation
    if not isinstance(observation, dict):
        return None
    raw = observation.get(ACTION_REPORT_KEY)
    if raw is None:
        return None
    if isinstance(raw, ActionReport):
        return raw
    if isinstance(raw, dict):
        try:
            return ActionReport.from_dict(raw)
        except (ValueError, TypeError, KeyError):
            return None
    return None
