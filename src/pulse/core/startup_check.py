"""Pulse 启动健康自检 (Startup self-check).

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
为什么要这个模块
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
过去的实际教训 (重要, 不要删):

  - ``wechat-work-bot`` ``configured=True`` 但 ``wecom-aibot-sdk-python``
    没装时, 代码 ``logger.error`` 后 ``return`` —— 服务照常起来, HTTP 路由
    全部 OK, 健康检查也绿, 但用户在企微发来的消息永远收不到回复, 且错误
    被混在 uvicorn 启动日志几十行 INFO 中央很难被注意到.
  - MCP transport 构造失败被 ``except Exception: pass`` 吞掉, 用户永远
    不知道是 URL 写错了还是服务没起.

本模块的契约:
  1. **已配置 (configured=True) 但起不来 → fatal**. 整进程退出, 在 stderr
     打出高对比度修复提示, 拒绝"假装在工作".
  2. **未配置 → skip, 但必须写进报告**. 用户能一眼看出"哦这个 channel 我没开".
  3. **配置好但连不上 (如 MCP connection refused) → 非 fatal 但醒目标红**.
     因为这类问题往往只是外部服务没起, 不应该让 Pulse 整体挂掉.
  4. 启动报告在 stderr 打印为一张 ASCII 表格, 便于人类快速扫视.

本模块只做**汇报**, 不做修复. 修复由具体组件 (比如 ``WechatWorkBotAdapter.start``)
自己负责 raise.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable

logger = logging.getLogger(__name__)


class CheckStatus(str, Enum):
    READY = "ready"        # 绿: 已配置且已就绪
    SKIPPED = "skipped"    # 灰: 未配置, 主动跳过 (正常)
    DEGRADED = "degraded"  # 黄: 已配置但外部依赖不可达 (连接拒绝等), 非 fatal
    FAILED = "failed"      # 红: 已配置但起不来, 通常 fatal


@dataclass(slots=True)
class HealthCheckItem:
    """一条健康检查结果."""

    category: str           # "channel" / "mcp" / "memory" / "observability" / "db"
    name: str               # "wechat-work-bot" / "boss" / "core_memory"
    status: CheckStatus
    detail: str = ""        # 简短原因/路径 (单行, ≤ 80 字符)
    fatal: bool = False     # True 时 check_and_abort 会退出进程


@dataclass(slots=True)
class StartupReport:
    items: list[HealthCheckItem] = field(default_factory=list)

    def add(self, item: HealthCheckItem) -> None:
        self.items.append(item)

    def has_fatal(self) -> bool:
        return any(i.fatal and i.status == CheckStatus.FAILED for i in self.items)

    def fatal_items(self) -> list[HealthCheckItem]:
        return [i for i in self.items if i.fatal and i.status == CheckStatus.FAILED]


# ──────────────────────────────────────────────────────────────────────
# 渲染
# ──────────────────────────────────────────────────────────────────────

_STATUS_GLYPH: dict[CheckStatus, str] = {
    CheckStatus.READY: "[OK]  ",
    CheckStatus.SKIPPED: "[SKIP]",
    CheckStatus.DEGRADED: "[WARN]",
    CheckStatus.FAILED: "[FAIL]",
}


def render_report(report: StartupReport) -> str:
    """把报告渲染为一张 ASCII 表 (用于 stderr)."""
    if not report.items:
        return "[pulse] startup self-check: (no items)\n"

    # 按 category 聚合排序, 保持稳定顺序
    category_order = ["channel", "mcp", "memory", "db", "observability", "misc"]
    by_cat: dict[str, list[HealthCheckItem]] = {}
    for it in report.items:
        by_cat.setdefault(it.category, []).append(it)

    lines: list[str] = []
    lines.append("=" * 72)
    lines.append(" Pulse startup self-check")
    lines.append("=" * 72)
    for cat in list(category_order) + [c for c in by_cat if c not in category_order]:
        items = by_cat.get(cat)
        if not items:
            continue
        lines.append(f" [{cat}]")
        for it in items:
            glyph = _STATUS_GLYPH[it.status]
            detail = f"  — {it.detail}" if it.detail else ""
            lines.append(f"   {glyph}  {it.name:<24}{detail}")
    lines.append("=" * 72)

    fatals = report.fatal_items()
    if fatals:
        lines.append(" FATAL failures detected:")
        for it in fatals:
            lines.append(f"   - {it.category}/{it.name}: {it.detail}")
        lines.append(" → refusing to start. Fix the items above and retry.")
        lines.append("=" * 72)
    else:
        degraded = [i for i in report.items if i.status == CheckStatus.DEGRADED]
        if degraded:
            lines.append(" Degraded (non-fatal, service will keep running):")
            for it in degraded:
                lines.append(f"   - {it.category}/{it.name}: {it.detail}")
            lines.append("=" * 72)

    return "\n".join(lines) + "\n"


def emit_report(report: StartupReport, *, stream=sys.stderr) -> None:
    """把报告写到 stderr, 同时也写一条 logger.info 以便落日志文件."""
    text = render_report(report)
    stream.write(text)
    stream.flush()
    # 同步落一条到 app 日志, 方便 grep/回查 (stderr 可能被 systemd 截断).
    logger.info("startup self-check report:\n%s", text)


def check_and_abort(report: StartupReport) -> None:
    """有 fatal 就 raise RuntimeError, 让 uvicorn 启动失败退出.

    设计上不直接 ``sys.exit``, 而是 raise —— 这样 FastAPI lifespan 能把异常
    传播给 uvicorn, 后者会打印 traceback 并以非零码退出, 符合"进程级健康"
    的契约 (systemd/k8s/docker-compose 能正确识别启动失败).
    """
    if not report.has_fatal():
        return
    fatals = report.fatal_items()
    summary = "; ".join(f"{i.category}/{i.name}: {i.detail}" for i in fatals)
    raise RuntimeError(f"Pulse startup self-check failed ({len(fatals)} fatal): {summary}")


# ──────────────────────────────────────────────────────────────────────
# 具体 check helpers (给 server.py 调用, 不强制使用)
# ──────────────────────────────────────────────────────────────────────


def check_channel_wechat_bot(*, configured: bool, sdk_importable: bool | None = None) -> HealthCheckItem:
    """企业微信 AI 机器人 channel 的健康检查.

    参数:
      configured: bot_id 和 bot_secret 是否都提供.
      sdk_importable: 若 None, 自动尝试 ``import wecom_aibot_sdk``. 测试可注入.
    """
    if not configured:
        return HealthCheckItem(
            category="channel",
            name="wechat-work-bot",
            status=CheckStatus.SKIPPED,
            detail="bot_id/bot_secret not set",
        )
    if sdk_importable is None:
        try:
            import wecom_aibot_sdk  # noqa: F401
            sdk_importable = True
        except ImportError:
            sdk_importable = False
    if not sdk_importable:
        return HealthCheckItem(
            category="channel",
            name="wechat-work-bot",
            status=CheckStatus.FAILED,
            detail=(
                "SDK missing: pip install wecom-aibot-sdk-python "
                "(or `pip install -e '.[channels-wecom]'`)"
            ),
            fatal=True,
        )
    # 长连建立结果异步, 这里只检查到可 import 即算 ready; 真实的 connected
    # 事件由 ``on_connected`` / ``on_authenticated`` 回调再发 EventBus.
    return HealthCheckItem(
        category="channel",
        name="wechat-work-bot",
        status=CheckStatus.READY,
        detail="SDK loaded, WebSocket will connect async",
    )


def check_mcp_transport(
    *,
    name: str,
    built: bool,
    url: str = "",
    error: str = "",
) -> HealthCheckItem:
    """MCP 单个 transport 的健康检查.

    参数:
      name: server 名字 (boss / web-search / _default).
      built: transport 是否成功构造. (不代表对端真的活着, 那个要运行时才知道)
      url: 目标 URL, 用于诊断信息.
      error: 若构造失败, 这里是简短错误信息.
    """
    if not built:
        return HealthCheckItem(
            category="mcp",
            name=name,
            status=CheckStatus.FAILED,
            detail=f"transport build failed: {error}" if error else "transport build failed",
            fatal=False,  # MCP 挂一个不拖垮整服务, 只是工具面能力降级
        )
    return HealthCheckItem(
        category="mcp",
        name=name,
        status=CheckStatus.READY,
        detail=f"url={url}" if url else "stdio",
    )


def mark_mcp_degraded_from_logs(report: StartupReport, names_with_conn_refused: Iterable[str]) -> None:
    """把已经构造出 transport 但 list_tools 全部 connection refused 的 server
    标记为 degraded. 由调用方自己判定哪些是 refused 后调用."""
    refused = set(names_with_conn_refused)
    for it in report.items:
        if it.category == "mcp" and it.name in refused and it.status == CheckStatus.READY:
            it.status = CheckStatus.DEGRADED
            it.detail = (it.detail + "; ") if it.detail else ""
            it.detail += "list_tools connection refused (外部 MCP server 未启动)"
