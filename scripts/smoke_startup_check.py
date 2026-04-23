"""Smoke test for startup_check + server import compatibility.

Purpose: verify (1) server module imports (our new logging/startup_check imports
didn't break anything) and (2) render_report produces the expected table.
"""

from __future__ import annotations

import sys

from pulse.core import server as _server  # noqa: F401 — import side-effect only
from pulse.core.startup_check import (
    StartupReport,
    check_channel_wechat_bot,
    check_mcp_transport,
    render_report,
)


def main() -> int:
    r = StartupReport()
    # configured bot with missing SDK → fatal
    r.add(check_channel_wechat_bot(configured=True, sdk_importable=False))
    # not configured → skipped
    r.add(check_channel_wechat_bot(configured=False))
    # MCP transport build failure → non-fatal, degraded
    r.add(check_mcp_transport(
        name="boss", built=False, url="http://localhost:8765", error="conn refused"
    ))
    r.add(check_mcp_transport(
        name="web-search", built=True, url="http://localhost:8766"
    ))

    text = render_report(r)
    print(text)
    print(f"has_fatal={r.has_fatal()}")
    assert r.has_fatal() is True, "configured bot + missing SDK should be fatal"
    assert "[FAIL]" in text
    assert "[SKIP]" in text
    assert "[OK]" in text
    print("smoke ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
