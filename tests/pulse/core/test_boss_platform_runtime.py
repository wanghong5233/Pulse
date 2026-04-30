from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


def _load_runtime_module():
    root = Path(__file__).resolve().parents[3]
    runtime_path = root / "src" / "pulse" / "mcp_servers" / "_boss_platform_runtime.py"
    spec = importlib.util.spec_from_file_location("pulse_boss_runtime_test", runtime_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("failed to load boss platform runtime module")
    module = importlib.util.module_from_spec(spec)
    # Must register in sys.modules BEFORE exec_module; otherwise @dataclass
    # on Python 3.12+ crashes inside _is_type() because it looks up
    # sys.modules[cls.__module__].__dict__ to resolve forward references.
    # Ref: https://docs.python.org/3/library/importlib.html#importing-a-source-file-directly
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


runtime = _load_runtime_module()


def test_runtime_reply_browser_mode_uses_executor(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("PULSE_BOSS_MCP_ACTION_AUDIT_PATH", str(tmp_path / "boss_actions.jsonl"))
    monkeypatch.setenv("PULSE_BOSS_MCP_REPLY_MODE", "browser")

    def _fake_execute_browser_reply(
        *,
        conversation_id: str,
        reply_text: str,
        profile_id: str,
        conversation_hint: dict | None = None,
    ) -> dict:
        assert conversation_id == "conv-1"
        assert "测试回复" in reply_text
        assert profile_id == "default"
        assert isinstance(conversation_hint, dict)
        return {
            "ok": True,
            "status": "sent",
            "source": "boss_mcp_browser",
            "error": None,
        }

    monkeypatch.setattr(runtime, "_execute_browser_reply", _fake_execute_browser_reply)
    result = runtime.reply_conversation(
        conversation_id="conv-1",
        reply_text="测试回复",
        profile_id="default",
    )
    assert result["ok"] is True
    assert result["status"] == "sent"
    assert result["source"] == "boss_mcp_browser"


def test_runtime_greet_browser_mode_uses_executor(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("PULSE_BOSS_MCP_ACTION_AUDIT_PATH", str(tmp_path / "boss_actions.jsonl"))
    monkeypatch.setenv("PULSE_BOSS_MCP_GREET_MODE", "browser")

    def _fake_execute_browser_greet(*, run_id: str, job_id: str, source_url: str, greeting_text: str) -> dict:
        assert run_id == "run-1"
        assert job_id == "job-1"
        assert source_url.startswith("https://")
        assert greeting_text
        return {
            "ok": True,
            "status": "sent",
            "source": "boss_mcp_browser",
            "error": None,
        }

    monkeypatch.setattr(runtime, "_execute_browser_greet", _fake_execute_browser_greet)
    result = runtime.greet_job(
        run_id="run-1",
        job_id="job-1",
        source_url="https://www.zhipin.com/job_detail/abc",
        job_title="AI Agent Intern",
        company="Pulse Labs",
        greeting_text="你好，我想了解岗位详情",
    )
    assert result["ok"] is True
    assert result["status"] == "sent"
    assert result["source"] == "boss_mcp_browser"


# ---------------------------------------------------------------------------
# P3e idempotency guard (audit trace_a9bbc29a245c): a previously successful
# MUTATING action in the audit log within the idempotency window MUST short-
# circuit a re-invocation with the same natural key, because the duplicate
# call is an upstream HTTP retry and the platform-side side-effect already
# happened. Without this guard the MCP would re-click the send button.
# ---------------------------------------------------------------------------


def _seed_greet_success_row(audit_path: Path, *, run_id: str, job_id: str) -> None:
    from datetime import datetime, timezone
    row = {
        "action": "greet_job_result",
        "run_id": run_id,
        "job_id": job_id,
        "source_url": f"https://www.zhipin.com/job/{job_id}",
        "mode": "browser",
        "status": "sent",
        "ok": True,
        "error": None,
        "source": "boss_mcp_browser",
        "screenshot_path": "/tmp/greet.png",
        "logged_at": datetime.now(timezone.utc).isoformat(),
    }
    audit_path.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")


def test_runtime_greet_idempotent_replay_skips_browser(monkeypatch, tmp_path) -> None:
    audit_path = tmp_path / "boss_actions.jsonl"
    _seed_greet_success_row(audit_path, run_id="run-r1", job_id="job-j1")
    monkeypatch.setenv("PULSE_BOSS_MCP_ACTION_AUDIT_PATH", str(audit_path))
    monkeypatch.setenv("PULSE_BOSS_MCP_GREET_MODE", "browser")
    monkeypatch.setenv("PULSE_BOSS_MCP_IDEMPOTENCY_WINDOW_SEC", "300")
    calls: list[int] = []

    def _must_not_be_called(**_: object) -> dict:
        calls.append(1)
        raise AssertionError(
            "_execute_browser_greet invoked despite successful audit row within window"
        )

    monkeypatch.setattr(runtime, "_execute_browser_greet", _must_not_be_called)
    result = runtime.greet_job(
        run_id="run-r1",
        job_id="job-j1",
        source_url="https://www.zhipin.com/job/job-j1",
        job_title="AI Agent Intern",
        company="Pulse Labs",
        greeting_text="hello again (retry)",
    )
    assert calls == []
    assert result["ok"] is True
    assert result["status"] == "sent"
    assert result.get("idempotent_replay") is True
    tail_lines = [
        json.loads(line)
        for line in audit_path.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("{")
    ]
    assert any(r.get("action") == "greet_job_idempotent_replay" for r in tail_lines), (
        "idempotent_replay event was not appended to the audit log"
    )


def test_runtime_greet_does_not_replay_when_window_expired(monkeypatch, tmp_path) -> None:
    audit_path = tmp_path / "boss_actions.jsonl"
    _seed_greet_success_row(audit_path, run_id="run-r2", job_id="job-j2")
    monkeypatch.setenv("PULSE_BOSS_MCP_ACTION_AUDIT_PATH", str(audit_path))
    monkeypatch.setenv("PULSE_BOSS_MCP_GREET_MODE", "browser")
    monkeypatch.setenv("PULSE_BOSS_MCP_IDEMPOTENCY_WINDOW_SEC", "0")
    calls: list[int] = []

    def _fake_exec(**_: object) -> dict:
        calls.append(1)
        return {"ok": True, "status": "sent", "source": "boss_mcp_browser", "error": None}

    monkeypatch.setattr(runtime, "_execute_browser_greet", _fake_exec)
    result = runtime.greet_job(
        run_id="run-r2",
        job_id="job-j2",
        source_url="https://www.zhipin.com/job/job-j2",
        job_title="AI Agent Intern",
        company="Pulse Labs",
        greeting_text="fresh send",
    )
    assert calls == [1]
    assert result.get("idempotent_replay") is None


def test_runtime_greet_does_not_replay_different_job_id(monkeypatch, tmp_path) -> None:
    audit_path = tmp_path / "boss_actions.jsonl"
    _seed_greet_success_row(audit_path, run_id="run-r3", job_id="job-A")
    monkeypatch.setenv("PULSE_BOSS_MCP_ACTION_AUDIT_PATH", str(audit_path))
    monkeypatch.setenv("PULSE_BOSS_MCP_GREET_MODE", "browser")
    monkeypatch.setenv("PULSE_BOSS_MCP_IDEMPOTENCY_WINDOW_SEC", "300")
    calls: list[int] = []

    def _fake_exec(**_: object) -> dict:
        calls.append(1)
        return {"ok": True, "status": "sent", "source": "boss_mcp_browser", "error": None}

    monkeypatch.setattr(runtime, "_execute_browser_greet", _fake_exec)
    result = runtime.greet_job(
        run_id="run-r3",
        job_id="job-B",
        source_url="https://www.zhipin.com/job/job-B",
        job_title="AI Agent Intern",
        company="Pulse Labs",
        greeting_text="different JD, should really send",
    )
    assert calls == [1]
    assert result.get("idempotent_replay") is None


def test_runtime_health_includes_browser_config(monkeypatch) -> None:
    monkeypatch.setenv("PULSE_BOSS_BROWSER_PROFILE_DIR", "./data/.playwright/boss")
    monkeypatch.setenv("PULSE_BOSS_BROWSER_HEADLESS", "false")
    monkeypatch.setenv("PULSE_BOSS_BROWSER_TIMEOUT_MS", "15000")
    monkeypatch.delenv("PULSE_BOSS_MCP_REPLY_MODE", raising=False)
    monkeypatch.delenv("PULSE_BOSS_BROWSER_STEALTH_ENABLED", raising=False)
    monkeypatch.delenv("PULSE_BOSS_BROWSER_BLOCK_IFRAME_CORE", raising=False)
    health = runtime.health()
    assert health["ok"] is True
    assert "browser" in health
    assert isinstance(health["browser"]["profile_dir"], str)
    assert int(health["browser"]["timeout_ms"]) >= 3000
    assert "scan_mode" in health
    assert "pull_mode" in health
    assert health["reply_mode"] == "browser"
    assert health["browser"]["stealth_enabled"] is True
    assert health["browser"]["block_iframe_core"] is False


def test_runtime_health_includes_browser_idle_runtime_fields(monkeypatch) -> None:
    monkeypatch.setenv("PULSE_BOSS_BROWSER_IDLE_CLOSE_SEC", "600")
    health = runtime.health()
    assert int(health["browser"]["idle_close_sec"]) == 600
    assert "idle_elapsed_sec" in health["browser"]
    assert "runtime_open" in health["browser"]


def test_runtime_should_recycle_browser_for_idle(monkeypatch) -> None:
    monkeypatch.setenv("PULSE_BOSS_BROWSER_IDLE_CLOSE_SEC", "10")
    monkeypatch.setattr(runtime, "_PAGE", object(), raising=False)
    monkeypatch.setattr(runtime, "_BROWSER_LAST_USED_MONO", 1.0, raising=False)
    assert runtime._should_recycle_browser_for_idle(now_mono=12.5) is True
    assert runtime._should_recycle_browser_for_idle(now_mono=9.0) is False


def test_runtime_reset_browser_session_closes_handles(monkeypatch) -> None:
    class _FakePage:
        def __init__(self) -> None:
            self.closed = False

        def is_closed(self) -> bool:
            return self.closed

        def close(self) -> None:
            self.closed = True

    class _FakeContext:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    class _FakeManager:
        def __init__(self) -> None:
            self.stopped = False

        def stop(self) -> None:
            self.stopped = True

    page = _FakePage()
    context = _FakeContext()
    manager = _FakeManager()
    monkeypatch.setattr(runtime, "_PAGE", page, raising=False)
    monkeypatch.setattr(runtime, "_CONTEXT", context, raising=False)
    monkeypatch.setattr(runtime, "_PLAYWRIGHT", object(), raising=False)
    monkeypatch.setattr(runtime, "_PLAYWRIGHT_MANAGER", manager, raising=False)
    monkeypatch.setattr(runtime, "_BROWSER_LAST_USED_MONO", 123.0, raising=False)

    result = runtime.reset_browser_session(reason="unit_test")
    assert result["ok"] is True
    assert result["closed_page"] is True
    assert result["closed_context"] is True
    assert result["closed_playwright"] is True
    assert runtime._PAGE is None
    assert runtime._CONTEXT is None
    assert runtime._PLAYWRIGHT is None
    assert runtime._PLAYWRIGHT_MANAGER is None
    assert float(runtime._BROWSER_LAST_USED_MONO) == 0.0


def test_runtime_uses_patchright_not_playwright() -> None:
    """Regression guard: BOSS 反爬在 CDP 层检测, 必须用 patchright 而非原生 playwright.

    任何人试图把 `from patchright.sync_api` 改回 `from playwright.sync_api`
    都会触发这个测试; 决策背景见 docs/archive/debug-boss-antibot-postmortem.md.
    """
    source = Path(runtime.__file__).read_text(encoding="utf-8")
    assert "from patchright.sync_api import sync_playwright" in source, (
        "BOSS runtime must import sync_playwright from patchright; 见 "
        "docs/archive/debug-boss-antibot-postmortem.md"
    )
    assert "from playwright.sync_api import sync_playwright" not in source, (
        "Do not reintroduce native playwright here; patchright is the only "
        "approved browser driver for BOSS anti-bot reasons"
    )
    assert "from playwright_stealth import Stealth" not in source, (
        "playwright_stealth 不再使用; 由 patchright 二进制补丁代替"
    )


def _reset_browser_globals(monkeypatch) -> None:
    """Force _ensure_browser_page to re-run the env validation branch instead
    of returning a cached _PAGE from an earlier test."""
    monkeypatch.setattr(runtime, "_PAGE", None, raising=False)
    monkeypatch.setattr(runtime, "_CONTEXT", None, raising=False)
    monkeypatch.setattr(runtime, "_PLAYWRIGHT", None, raising=False)
    monkeypatch.setattr(runtime, "_PLAYWRIGHT_MANAGER", None, raising=False)


def test_runtime_fails_loud_on_legacy_stealth_env(monkeypatch, tmp_path) -> None:
    """把老 env 值设成奇怪字符串时, _ensure_browser_page 必须 fail-loud."""
    _reset_browser_globals(monkeypatch)
    monkeypatch.setenv("PULSE_BOSS_BROWSER_PROFILE_DIR", str(tmp_path / "profile"))
    monkeypatch.setenv("PULSE_BOSS_BROWSER_STEALTH_ENABLED", "maybe")
    import pytest

    with pytest.raises(RuntimeError, match="PULSE_BOSS_BROWSER_STEALTH_ENABLED"):
        runtime._ensure_browser_page()


def test_runtime_fails_loud_on_legacy_block_iframe_env(monkeypatch, tmp_path) -> None:
    _reset_browser_globals(monkeypatch)
    monkeypatch.setenv("PULSE_BOSS_BROWSER_PROFILE_DIR", str(tmp_path / "profile"))
    monkeypatch.delenv("PULSE_BOSS_BROWSER_STEALTH_ENABLED", raising=False)
    monkeypatch.setenv("PULSE_BOSS_BROWSER_BLOCK_IFRAME_CORE", "true")
    import pytest

    with pytest.raises(RuntimeError, match="PULSE_BOSS_BROWSER_BLOCK_IFRAME_CORE"):
        runtime._ensure_browser_page()


def test_runtime_check_login_ready(monkeypatch) -> None:
    class _FakePage:
        url = "https://www.zhipin.com/web/geek/chat"

        def goto(self, url: str, wait_until: str, timeout: int) -> None:  # noqa: ANN001
            _ = url, wait_until, timeout
            self.url = "https://www.zhipin.com/web/geek/chat"

        def inner_text(self, selector: str) -> str:
            _ = selector
            return "正常聊天页面"

    monkeypatch.setattr(runtime, "_ensure_browser_page", lambda: _FakePage())
    result = runtime.check_login(check_url="https://www.zhipin.com/web/geek/chat")
    assert result["ok"] is True
    assert result["status"] == "ready"


def test_runtime_check_login_auth_required(monkeypatch) -> None:
    class _FakePage:
        url = "https://www.zhipin.com/web/user/login"

        def goto(self, url: str, wait_until: str, timeout: int) -> None:  # noqa: ANN001
            _ = url, wait_until, timeout
            self.url = "https://www.zhipin.com/web/user/login"

        def inner_text(self, selector: str) -> str:
            _ = selector
            return "请登录"

    monkeypatch.setattr(runtime, "_ensure_browser_page", lambda: _FakePage())
    result = runtime.check_login(check_url="https://www.zhipin.com/web/geek/chat")
    assert result["ok"] is False
    assert result["status"] == "auth_required"


def test_runtime_scan_jobs_browser_first_prefers_browser(monkeypatch) -> None:
    monkeypatch.setenv("PULSE_BOSS_MCP_SCAN_MODE", "browser_first")

    def _fake_scan_jobs_via_browser(
        *, keyword: str, max_items: int, max_pages: int, city: str | None = None
    ) -> dict:
        assert "AI Agent" in keyword
        assert max_items == 3
        assert max_pages == 2
        return {
            "ok": True,
            "status": "ready",
            "items": [
                {
                    "job_id": "job-1",
                    "title": "AI Agent 实习生",
                    "company": "Pulse Labs",
                    "salary": "15K-25K",
                    "source_url": "https://www.zhipin.com/job_detail/1",
                    "snippet": "职位描述",
                    "source": "boss_mcp_browser_scan",
                }
            ],
            "pages_scanned": 1,
            "source": "boss_mcp_browser_scan",
            "errors": [],
        }

    monkeypatch.setattr(runtime, "_scan_jobs_via_browser", _fake_scan_jobs_via_browser)
    result = runtime.scan_jobs(keyword="AI Agent", max_items=3, max_pages=2)
    assert result["ok"] is True
    assert result["source"] == "boss_mcp_browser_scan"
    assert result["mode"] == "browser_first"
    assert len(result["items"]) == 1


def test_runtime_scan_jobs_browser_only_skips_web_search_fallback(monkeypatch) -> None:
    monkeypatch.setenv("PULSE_BOSS_MCP_SCAN_MODE", "browser_only")

    monkeypatch.setattr(
        runtime,
        "_scan_jobs_via_browser",
        lambda **_: {
            "ok": False,
            "status": "executor_unavailable",
            "items": [],
            "pages_scanned": 1,
            "source": "boss_mcp_browser_scan",
            "errors": ["browser down"],
        },
    )

    def _unexpected_search_web(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("search_web should not be called in browser_only mode")

    monkeypatch.setattr(runtime, "search_web", _unexpected_search_web)
    result = runtime.scan_jobs(keyword="AI Agent", max_items=3, max_pages=2)
    assert result["ok"] is False
    assert result["items"] == []
    assert result["source"] == "boss_mcp_browser_scan"
    assert result["mode"] == "browser_only"
    assert "browser down" in result["errors"]


def test_runtime_pull_conversations_browser_first_fallback_local(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("PULSE_BOSS_MCP_PULL_MODE", "browser_first")
    inbox_path = tmp_path / "boss_chat_inbox.jsonl"
    inbox_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "conversation_id": "conv-1",
                        "hr_name": "王HR",
                        "company": "Pulse Labs",
                        "job_title": "AI Agent 实习生",
                        "latest_message": "你好，方便聊一下吗",
                        "latest_time": "刚刚",
                        "unread_count": 2,
                    },
                    ensure_ascii=False,
                )
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PULSE_BOSS_CHAT_INBOX_PATH", str(inbox_path))

    monkeypatch.setattr(
        runtime,
        "_pull_conversations_via_browser",
        lambda **_: {
            "ok": False,
            "status": "executor_unavailable",
            "items": [],
            "unread_total": 0,
            "source": "boss_mcp_browser_chat",
            "errors": ["browser down"],
        },
    )

    result = runtime.pull_conversations(
        max_conversations=10,
        unread_only=False,
        fetch_latest_hr=False,
        chat_tab="all",
    )
    assert result["ok"] is True
    assert result["source"] == "boss_mcp_local_inbox"
    assert result["mode"] == "browser_first"
    assert "browser down" in result["errors"]
    assert len(result["items"]) == 1


def test_runtime_pull_conversations_browser_only_skips_local_fallback(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("PULSE_BOSS_MCP_PULL_MODE", "browser_only")
    inbox_path = tmp_path / "boss_chat_inbox.jsonl"
    inbox_path.write_text(
        json.dumps(
            {
                "conversation_id": "conv-1",
                "hr_name": "王HR",
                "company": "Pulse Labs",
                "job_title": "AI Agent 实习生",
                "latest_message": "你好，方便聊一下吗",
                "latest_time": "刚刚",
                "unread_count": 2,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PULSE_BOSS_CHAT_INBOX_PATH", str(inbox_path))
    monkeypatch.setattr(
        runtime,
        "_pull_conversations_via_browser",
        lambda **_: {
            "ok": False,
            "status": "executor_unavailable",
            "items": [],
            "unread_total": 0,
            "source": "boss_mcp_browser_chat",
            "errors": ["browser down"],
        },
    )

    result = runtime.pull_conversations(
        max_conversations=10,
        unread_only=False,
        fetch_latest_hr=False,
        chat_tab="all",
    )
    assert result["ok"] is False
    assert result["items"] == []
    assert result["source"] == "boss_mcp_browser_chat"
    assert result["mode"] == "browser_only"
    assert "browser down" in result["errors"]


def test_runtime_pull_conversations_browser_fails_loud_when_dom_empty(monkeypatch) -> None:
    """When chat-list DOM yields no row, pull must fail loudly.

    Anchors the contract in docs/dom-specs/boss/chat-list/README.md + the
    checklist §类型A rule: never synthesize ``Unknown HR`` placeholders just
    to keep a green return shape. The earlier version of this test froze the
    opposite (buggy) behavior where a regex text-fallback produced fake rows;
    any future patch that tries to re-introduce that fallback will make this
    test fail again, by design.
    """

    class _FakePage:
        url = "https://www.zhipin.com/web/geek/chat"

        def goto(self, url: str, wait_until: str, timeout: int) -> None:  # noqa: ANN001
            _ = url, wait_until, timeout
            self.url = "https://www.zhipin.com/web/geek/chat"

        def inner_text(self, selector: str) -> str:  # noqa: ANN001
            _ = selector
            return ""

        def eval_on_selector_all(self, selector: str, script: str):  # noqa: ANN001
            _ = selector, script
            return []

        def wait_for_selector(self, selector: str, timeout: int):  # noqa: ANN001
            _ = selector, timeout
            raise RuntimeError("selector not found")

        def locator(self, selector: str):  # noqa: ANN001
            _ = selector
            raise RuntimeError("selector not found")

        def wait_for_timeout(self, ms: int) -> None:
            _ = ms

        def bring_to_front(self) -> None:
            return None

        def evaluate(self, script: str):  # noqa: ANN001
            _ = script
            return {}

    monkeypatch.setattr(runtime, "_ensure_browser_page", lambda: _FakePage())
    result = runtime._pull_conversations_via_browser(
        max_conversations=5,
        unread_only=False,
        fetch_latest_hr=False,
        chat_tab="全部",
    )
    assert result["ok"] is False
    assert result["status"] == "no_result"
    assert result["items"] == []
    assert result["source"] == "boss_mcp_browser_chat"


def test_runtime_detect_runtime_risk_security_url() -> None:
    class _FakePage:
        def inner_text(self, selector: str) -> str:
            _ = selector
            return "正在加载中"

    status = runtime._detect_runtime_risk(
        _FakePage(),
        current_url="https://www.zhipin.com/web/passport/zp/security.html?code=37",
    )
    assert status == "risk_blocked"


def test_runtime_build_search_url_candidates() -> None:
    urls = runtime._build_search_url_candidates(keyword="AI Agent 实习", page=1)
    assert isinstance(urls, list)
    assert len(urls) >= 1
    assert any("/web/geek/jobs?" in url or "/web/geek/job?" in url for url in urls)


def test_runtime_extract_job_leads_from_chat_page(monkeypatch) -> None:
    class _FakePage:
        def inner_text(self, selector: str) -> str:
            _ = selector
            return (
                "首页\n消息\n全部\n未读\n新招呼\n"
                "00:18\n王雨城蜂屿科技创始人\nHi！王鸿，恭喜通过初筛。\n"
                "03月25日\n姚先生曹操出行高级招聘\n[送达]\n您好，方便沟通吗\n"
            )

    monkeypatch.setattr(runtime, "_build_chat_url", lambda cid: f"https://www.zhipin.com/web/geek/chat?conversationId={cid}")
    rows = runtime._extract_job_leads_from_chat_page(
        _FakePage(),
        keyword="AI Agent 实习",
        max_items=3,
        seen_keys=set(),
    )
    assert isinstance(rows, list)
    assert len(rows) >= 1
    first = rows[0]
    assert str(first.get("source") or "") == "boss_mcp_browser_chat_lead"
    assert str(first.get("source_url") or "").startswith("https://www.zhipin.com/web/geek/chat")


# ---------------------------------------------------------------------------
# P3f (trace 2026-04-21): 两条独立 bug fix 的回归锁
#   1. _close_orphan_tabs: chromium session restore 遗留的 tab 必须被关, 否则
#      WSLg 会渲染一个 patchright 不驱动的僵尸窗口挡住可见浏览器 (用户截图证).
#   2. _execute_browser_greet + PULSE_BOSS_MCP_GREET_FOLLOWUP: BOSS 平台在
#      点 "立即沟通" 按钮时会代发用户 APP 里预设的打招呼语; 默认 followup=off,
#      Pulse 不再追加第二条 greeting_text (避免 HR 收到重复自我介绍).
# ---------------------------------------------------------------------------


class _FakeTab:
    """Minimal playwright-Page surrogate for unit tests of _close_orphan_tabs."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.closed = False

    def close(self) -> None:
        if self.closed:
            raise AssertionError("tab closed twice — _close_orphan_tabs must be idempotent")
        self.closed = True


class _FakeContext:
    def __init__(self, pages: list) -> None:  # noqa: ANN001
        self.pages = pages


def test_close_orphan_tabs_keeps_main_closes_others(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("PULSE_BOSS_MCP_ACTION_AUDIT_PATH", str(tmp_path / "boss_actions.jsonl"))
    main = _FakeTab("https://www.zhipin.com/web/geek/chat")
    orphan_jobs = _FakeTab(
        "https://www.zhipin.com/web/geek/jobs?_security_check=1_1776770973897"
    )
    orphan_blank = _FakeTab("about:blank")
    ctx = _FakeContext([main, orphan_jobs, orphan_blank])
    closed = runtime._close_orphan_tabs(ctx, main)
    assert closed == 2
    assert main.closed is False, "main driven page must NOT be closed"
    assert orphan_jobs.closed is True
    assert orphan_blank.closed is True


def test_close_orphan_tabs_is_idempotent(monkeypatch, tmp_path) -> None:
    """Second call after the first (with the same context) closes 0 tabs."""
    monkeypatch.setenv("PULSE_BOSS_MCP_ACTION_AUDIT_PATH", str(tmp_path / "boss_actions.jsonl"))
    main = _FakeTab("https://www.zhipin.com/web/geek/chat")
    orphan = _FakeTab("https://www.zhipin.com/web/geek/jobs")
    ctx = _FakeContext([main, orphan])
    assert runtime._close_orphan_tabs(ctx, main) == 1
    ctx.pages = [main]
    assert runtime._close_orphan_tabs(ctx, main) == 0


def test_close_orphan_tabs_survives_close_exception(monkeypatch, tmp_path) -> None:
    """If one tab.close() raises, other orphans still get closed; audit records failure."""
    monkeypatch.setenv("PULSE_BOSS_MCP_ACTION_AUDIT_PATH", str(tmp_path / "boss_actions.jsonl"))

    class _BadTab(_FakeTab):
        def close(self) -> None:
            raise RuntimeError("CDP detached")

    main = _FakeTab("https://www.zhipin.com/web/geek/chat")
    bad = _BadTab("https://www.zhipin.com/bad")
    good = _FakeTab("https://www.zhipin.com/good")
    ctx = _FakeContext([main, bad, good])
    closed = runtime._close_orphan_tabs(ctx, main)
    assert closed == 1
    assert good.closed is True
    audit_rows = [
        json.loads(line)
        for line in Path(str(tmp_path / "boss_actions.jsonl")).read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("{")
    ]
    actions = [r.get("action") for r in audit_rows]
    assert "browser_orphan_tab_close_failed" in actions
    assert "browser_orphan_tab_closed" in actions


def test_greet_followup_default_is_off(monkeypatch) -> None:
    monkeypatch.delenv("PULSE_BOSS_MCP_GREET_FOLLOWUP", raising=False)
    assert runtime._greet_followup_enabled() is False


def test_greet_followup_env_on(monkeypatch) -> None:
    monkeypatch.setenv("PULSE_BOSS_MCP_GREET_FOLLOWUP", "on")
    assert runtime._greet_followup_enabled() is True


def test_greet_followup_env_invalid_fails_loud(monkeypatch) -> None:
    import pytest

    monkeypatch.setenv("PULSE_BOSS_MCP_GREET_FOLLOWUP", "maybe")
    with pytest.raises(RuntimeError, match="PULSE_BOSS_MCP_GREET_FOLLOWUP"):
        runtime._greet_followup_enabled()


class _FakeLocator:
    def __init__(self, name: str) -> None:
        self.name = name
        self.clicks: list[int] = []

    def click(self, *, timeout: int) -> None:
        self.clicks.append(timeout)


class _FakeGreetPage:
    url = "https://www.zhipin.com/job_detail/abc"

    def goto(self, url: str, *, wait_until: str, timeout: int) -> None:  # noqa: ANN001
        _ = url, wait_until, timeout


def test_execute_browser_greet_button_only_skips_followup(monkeypatch, tmp_path) -> None:
    """Default followup=off: only greet button click, no input fill, no send click.

    Regression guard for the user-reported duplicate-greeting bug
    (trace 2026-04-21, image attached): HR received TWO identical 自我介绍,
    first from BOSS platform's pre-configured template, second from Pulse.
    Default off stops the second from ever being typed.
    """
    monkeypatch.setenv("PULSE_BOSS_MCP_ACTION_AUDIT_PATH", str(tmp_path / "boss_actions.jsonl"))
    monkeypatch.delenv("PULSE_BOSS_MCP_GREET_FOLLOWUP", raising=False)

    greet_loc = _FakeLocator("greet")
    page = _FakeGreetPage()

    monkeypatch.setattr(runtime, "_ensure_browser_page", lambda: page)
    monkeypatch.setattr(runtime, "_detect_runtime_risk", lambda page, *, current_url: "")

    resolver_calls: list[list[str]] = []

    def _fake_waiter(_page, selectors, *, timeout_ms):  # noqa: ANN001
        resolver_calls.append(list(selectors))
        return greet_loc, selectors[0]

    monkeypatch.setattr(runtime, "_wait_for_any_selector", _fake_waiter)

    fill_calls: list[str] = []
    monkeypatch.setattr(runtime, "_fill_text", lambda loc, text: fill_calls.append(text))
    monkeypatch.setattr(
        runtime, "_take_browser_screenshot", lambda page, *, prefix: "/tmp/greet.png"
    )

    result = runtime._execute_browser_greet(
        run_id="run-1",
        job_id="job-1",
        source_url="https://www.zhipin.com/job_detail/abc",
        greeting_text="should-not-be-sent",
    )

    assert result["ok"] is True
    assert result["status"] == "sent", "button_only 视为 sent (BOSS 平台代发预设话术)"
    assert result["greet_strategy"] == "button_only"
    assert result["input_selector"] is None
    assert result["send_selector"] is None
    assert len(greet_loc.clicks) == 1
    assert fill_calls == [], "followup=off 下绝不能 fill greeting_text"
    assert len(resolver_calls) == 1, "只应查询 greet 按钮一个 selector, 不应该再找 input/send"


def test_execute_browser_greet_followup_on_fills_and_sends(monkeypatch, tmp_path) -> None:
    """followup=on: preserve legacy behavior for accounts without BOSS-side template."""
    monkeypatch.setenv("PULSE_BOSS_MCP_ACTION_AUDIT_PATH", str(tmp_path / "boss_actions.jsonl"))
    monkeypatch.setenv("PULSE_BOSS_MCP_GREET_FOLLOWUP", "on")

    greet_loc = _FakeLocator("greet")
    input_loc = _FakeLocator("input")
    send_loc = _FakeLocator("send")
    page = _FakeGreetPage()

    monkeypatch.setattr(runtime, "_ensure_browser_page", lambda: page)
    monkeypatch.setattr(runtime, "_detect_runtime_risk", lambda page, *, current_url: "")

    locators_in_order = [greet_loc, input_loc, send_loc]
    resolver_calls: list[list[str]] = []

    def _fake_waiter(_page, selectors, *, timeout_ms):  # noqa: ANN001
        idx = len(resolver_calls)
        resolver_calls.append(list(selectors))
        return locators_in_order[idx], selectors[0]

    monkeypatch.setattr(runtime, "_wait_for_any_selector", _fake_waiter)

    fill_calls: list[str] = []
    monkeypatch.setattr(runtime, "_fill_text", lambda loc, text: fill_calls.append(text))
    monkeypatch.setattr(
        runtime, "_take_browser_screenshot", lambda page, *, prefix: "/tmp/greet.png"
    )

    result = runtime._execute_browser_greet(
        run_id="run-1",
        job_id="job-1",
        source_url="https://www.zhipin.com/job_detail/abc",
        greeting_text="custom followup line",
    )

    assert result["ok"] is True
    assert result["status"] == "sent"
    assert result["greet_strategy"] == "button_and_followup"
    assert result["input_selector"] is not None
    assert result["send_selector"] is not None
    assert fill_calls == ["custom followup line"]
    assert len(greet_loc.clicks) == 1
    assert len(send_loc.clicks) == 1
    assert len(resolver_calls) == 3, "greet + input + send 三次 selector 查询"


def test_runtime_health_includes_greet_followup(monkeypatch) -> None:
    monkeypatch.delenv("PULSE_BOSS_MCP_GREET_FOLLOWUP", raising=False)
    health = runtime.health()
    assert str(health.get("greet_followup") or "") == "off"
    monkeypatch.setenv("PULSE_BOSS_MCP_GREET_FOLLOWUP", "on")
    health_on = runtime.health()
    assert str(health_on.get("greet_followup") or "") == "on"
