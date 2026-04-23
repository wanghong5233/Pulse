"""BOSS 自动回复 dry-run 端到端脚本 (ADR-004 Step A.7 验收入口).

## 定位

这是**用户在真正让 BOSS 帮 HR 点按钮之前**的审阅闸门. 不是自动化测试的
一部分, 不在 CI 跑. 目的就是:

  1. 用真实登录态 Chromium 打开 BOSS 会话列表
  2. 对 "未读" tab 扫 ≤N 条 + 逐条打开 chat-detail 抽状态 + 规则决策
  3. **不实际点击任何按钮**, 只把决策列出来给用户看
  4. 用户确认决策和心智一致后, 再切 ``--live`` 让 runtime 真点

## 为什么要手动眼审

"点同意" 等于真发简历给 HR, **不可撤回**. 规则决策的单测只能守住
"state X → 决策 Y" 的对应, 守不住 "真实 chat-detail DOM 被解析出来的
state 是不是 X". 把这两端拼上的唯一办法就是在真 BOSS 上跑一次, 人肉
比对浏览器画面和 stdout 决策 — 这是宪法 §测试分层#3 所说的 "真实
trace 回放".

## 使用

    # dry-run (默认, 不点任何按钮, 不写 audit)
    python scripts/smoke_auto_reply.py

    # 扫描 10 条未读
    python scripts/smoke_auto_reply.py --max 10

    # 切 live (真点; 需手动打开开关, 不允许 env 直接继承)
    python scripts/smoke_auto_reply.py --live --max 2

## 前置

* ``~/.pulse/boss_browser_profile`` 已登录 BOSS
* Pulse backend 若在 scan 会抢 SingletonLock, 建议先停
* ``--live`` 仅在用户确认 dry-run 决策符合预期后使用

## 产出

stdout 打印:
  * banner (版本 / dry_run / 扫描参数)
  * 决策 table (idx, hr/company, decision_kind, reason)
  * live 模式下附带 executed/errors 细节 + boss_mcp_actions.jsonl 路径

同时在 ``--live`` 模式下, runtime 层会写入 ``boss_mcp_actions.jsonl``
的 ``auto_reply_result`` 条目 (含幂等 key + 截图路径).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pulse.mcp_servers._boss_platform_runtime import (  # noqa: E402
    _action_audit_path,
    _autoreply_disabled,
    _autoreply_force_dry_run,
    run_auto_reply_cycle,
)


_BANNER_WIDTH = 72


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="BOSS auto-reply dry-run smoke (ADR-004)",
    )
    parser.add_argument(
        "--max",
        type=int,
        default=5,
        help="max conversations to scan in the '未读' tab (1..20, default 5)",
    )
    parser.add_argument(
        "--tab",
        type=str,
        default="未读",
        help="chat-list tab name (default '未读')",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help=(
            "actually click BOSS buttons (default is dry-run). "
            "Only use after you审过 dry-run 决策."
        ),
    )
    parser.add_argument(
        "--run-id",
        type=str,
        default="",
        help="optional run_id written into audit rows",
    )
    return parser.parse_args()


def _banner(text: str) -> None:
    line = "=" * _BANNER_WIDTH
    print(line)
    for row in text.strip().splitlines():
        print(row)
    print(line)


def _print_decision_table(decisions: list[dict]) -> None:
    if not decisions:
        print("(no decisions — chat-list '未读' tab 为空)")
        return
    header = f"{'idx':<4} {'decision':<22} {'hr/company':<30} reason"
    print(header)
    print("-" * _BANNER_WIDTH)
    for row in decisions:
        idx = row.get("item_index", "")
        kind = str(row.get("decision_kind", ""))
        hr = str(row.get("hr_name", ""))[:12]
        company = str(row.get("company", ""))[:16]
        hr_co = f"{hr} / {company}"
        reason = str(row.get("reason", ""))[:80]
        print(f"{idx:<4} {kind:<22} {hr_co:<30} {reason}")


def _print_execution_detail(label: str, rows: list[dict]) -> None:
    if not rows:
        return
    print()
    print(f"--- {label} ({len(rows)}) ---")
    for row in rows:
        summary = {
            "idx": row.get("item_index"),
            "conversation_id": row.get("conversation_id"),
            "decision_kind": row.get("decision_kind"),
            "execute_status": row.get("execute_status"),
            "error": row.get("error"),
            "screenshot_path": row.get("screenshot_path"),
        }
        print(json.dumps(summary, ensure_ascii=False))


def main() -> int:
    args = _parse_args()
    dry_run = not bool(args.live)

    if _autoreply_disabled():
        _banner(
            "Auto-reply is GLOBALLY DISABLED via PULSE_BOSS_AUTOREPLY=off.\n"
            "Nothing to do. Unset the env var if you want to run."
        )
        return 2

    if _autoreply_force_dry_run() and args.live:
        _banner(
            "WARNING: --live requested but PULSE_BOSS_AUTOREPLY_FORCE_DRY_RUN=on\n"
            "is set. Runtime will override dry_run=True. This is intentional\n"
            "(ops killswitch). Unset the env var to actually click."
        )

    _banner(
        f"BOSS auto-reply smoke\n"
        f"  dry_run   = {dry_run}\n"
        f"  chat_tab  = {args.tab}\n"
        f"  max       = {args.max}\n"
        f"  run_id    = {args.run_id or '(none)'}\n"
        f"  audit_path= {_action_audit_path()}\n"
        f"\n"
        f"NOTE: dry_run 仍会**打开会话** (BOSS 会显示 '对方在看你的主页');\n"
        f"      只是不点最后一下按钮. --live 才会真点."
    )

    result = run_auto_reply_cycle(
        max_conversations=args.max,
        chat_tab=args.tab,
        dry_run=dry_run,
        run_id=args.run_id,
    )

    print()
    print(f"status      : {result.get('status')}")
    print(f"ok          : {result.get('ok')}")
    print(f"dry_run     : {result.get('dry_run')}")
    print(f"scanned     : {result.get('scanned')}")
    print(f"chat_tab    : {result.get('chat_tab')}")
    print()

    decisions = list(result.get("decisions") or [])
    _print_decision_table(decisions)

    _print_execution_detail("executed", list(result.get("executed") or []))
    _print_execution_detail("skipped", list(result.get("skipped") or []))
    _print_execution_detail("errors", list(result.get("errors") or []))

    print()
    if dry_run:
        _banner(
            "DRY RUN complete. Review the decisions above.\n"
            "If everything makes sense, rerun with --live to actually click."
        )
        return 0

    _banner(
        "LIVE run complete. Audit rows were written; screenshots saved.\n"
        f"Inspect {_action_audit_path()} for the '_result' entries."
    )
    return 0 if bool(result.get("ok")) else 1


if __name__ == "__main__":
    sys.exit(main())
