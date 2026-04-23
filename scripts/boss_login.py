"""Interactive BOSS login onboarding (headed Chromium).

One-time bootstrap: opens a headed Chromium pinned to the same persistent
profile that `pulse.mcp_servers.boss_platform_gateway` uses, lets the human
sign in (QR code / password), then persists the session cookie by closing
the context cleanly.

Workflow
--------
1. Kill any running BOSS MCP gateway (holds the profile dir with an
   exclusive lock).
2. Run this script, scan the QR with the BOSS mobile app, finish the
   login in Chromium.
3. Back in the terminal, press <Enter> — the script saves cookies and
   exits.
4. Restart BOSS MCP gateway; subsequent `scan_jobs` no longer hits
   `auth_required`.

Env contract
------------
- Shares `PULSE_BOSS_BROWSER_PROFILE_DIR` (default `~/.pulse/boss_browser_profile`)
  and `PULSE_BOSS_BROWSER_USER_AGENT` with the runtime to avoid a second
  fingerprint surface.
- Respects `PULSE_BOSS_BROWSER_HEADLESS`, but defaults to `false` here
  because a human must complete the flow.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pulse.mcp_servers import _boss_platform_runtime as runtime  # noqa: E402


_LOGIN_ENTRY_URL = "https://www.zhipin.com/web/user/"
_LOGIN_SUCCESS_URL = "https://www.zhipin.com/web/geek/chat"


def _assert_mcp_down() -> None:
    try:
        import socket

        with socket.create_connection(("127.0.0.1", 8811), timeout=0.5):
            print(
                "[FATAL] BOSS MCP gateway is running on :8811 — it holds the "
                "profile dir lock. Stop it first:",
                file=sys.stderr,
            )
            print(
                "  pkill -f 'pulse.mcp_servers.boss_platform_gateway'",
                file=sys.stderr,
            )
            sys.exit(2)
    except OSError:
        return


def main() -> int:
    os.environ.setdefault("PULSE_BOSS_BROWSER_HEADLESS", "false")
    _assert_mcp_down()

    profile_dir = runtime._browser_profile_dir()
    profile_dir.mkdir(parents=True, exist_ok=True)
    print(f"[login] profile dir: {profile_dir}")
    print(f"[login] headless:    {runtime._browser_headless()}")
    print(f"[login] UA:          {runtime._browser_user_agent() or '(chromium default)'}")

    try:
        from patchright.sync_api import sync_playwright
    except ImportError:
        print(
            "[FATAL] patchright not installed. Run:\n"
            "  pip install -e '.[browser]' && patchright install chromium",
            file=sys.stderr,
        )
        return 2

    with sync_playwright() as pw:
        launch_args: dict[str, object] = {
            "user_data_dir": str(profile_dir),
            "headless": runtime._browser_headless(),
            "no_viewport": True,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
                "--no-sandbox",
            ],
        }
        explicit_ua = runtime._browser_user_agent()
        if explicit_ua:
            launch_args["user_agent"] = explicit_ua
        channel = runtime._browser_channel()
        if channel:
            launch_args["channel"] = channel

        try:
            context = pw.chromium.launch_persistent_context(**launch_args)
        except TypeError:
            launch_args.pop("no_viewport", None)
            context = pw.chromium.launch_persistent_context(**launch_args)

        page = context.pages[0] if context.pages else context.new_page()
        print(f"[login] navigating to {_LOGIN_ENTRY_URL}")
        page.goto(_LOGIN_ENTRY_URL, wait_until="domcontentloaded", timeout=20000)

        print("")
        print("=" * 72)
        print("在弹出的 Chromium 窗口里用手机 BOSS App 扫码 (或账密) 登录。")
        print("登录成功后会跳到 geek/chat 页面。回到这里按 <Enter> 保存会话。")
        print("=" * 72)
        try:
            input("按 Enter 继续…")
        except EOFError:
            pass

        current_url = page.url
        print(f"[login] current url: {current_url}")
        if "/web/user" in current_url or "/login" in current_url or "passport.zhipin.com" in current_url:
            print(
                "[WARN] 仍然在登录页 — cookie 可能没写成功。你可以直接 Ctrl-C, "
                "或再回浏览器里完成登录后再按 Enter.",
                file=sys.stderr,
            )
            return 3

        try:
            context.close()
        except Exception as exc:
            print(f"[warn] context.close raised: {type(exc).__name__}: {exc}")

    print("[login] session persisted. restart BOSS MCP gateway now:")
    print("  nohup /root/.venvs/pulse/bin/python -m pulse.mcp_servers.boss_platform_gateway \\")
    print("    > /tmp/pulse_boss_mcp.log 2>&1 &")
    return 0


if __name__ == "__main__":
    sys.exit(main())
