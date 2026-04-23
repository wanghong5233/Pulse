"""BOSS chat-list timing/resilience guards (ADR-004 §6.1.8 B1+B2).

Scope (宪法 §测试宪法):
    Pins *two* real defence lines born from a concrete production miss
    (trace_7a4be7958c5b, 2026-04-22):
      1. Tab-switch DOM repaint is asynchronous — a single-shot extractor
         fires against the stale tab and returns [] even though the
         conversation list re-populates ~1-2 s later.
      2. The "did the list change?" detector must be content-sensitive
         at the first-row level, otherwise the tab-switch probe can't
         tell "I landed on the new tab" from "I was already here."

    Both tests drive the real module functions with a tiny Playwright
    mimic (`_FakePage`) that replays only the methods our code actually
    calls. No monkeypatching of private internals — we steer behavior
    by what the fake returns from ``eval_on_selector`` /
    ``eval_on_selector_all``, which is the real integration seam.

What this file does NOT cover:
    Real Playwright networkidle semantics, real BOSS XHR timing, real
    SPA virtualization. Those live in the live-smoke path
    (``scripts/smoke_auto_reply.py --live``) and are out of scope for
    pytest.
"""
from __future__ import annotations

from typing import Any

import pytest

from pulse.mcp_servers._boss_platform_runtime import (
    _chat_list_first_row_signature,
    _diagnose_empty_chat_list,
    _ensure_chat_list_hydrated,
    _extract_conversations_from_page,
    _resilient_extract_conversations_from_page,
)


class _FakePage:
    """Minimal Playwright ``Page`` double.

    We only implement the three hooks the code under test actually
    calls: ``eval_on_selector`` (single-row signature probe),
    ``eval_on_selector_all`` (full list extraction), and the two wait
    helpers. ``eval_on_selector_all`` yields successive values from
    ``extract_script`` so tests can script the "empty, empty, then
    populated" pattern that triggers the retry path.
    """

    def __init__(
        self,
        *,
        extract_script: list[list[dict[str, Any]]],
        first_row_script: list[str] | None = None,
    ) -> None:
        self._extract_script = list(extract_script)
        self._first_row_script = list(first_row_script or [])
        self.extract_calls = 0
        self.first_row_calls = 0
        self.networkidle_calls = 0
        self.timeout_calls: list[int] = []

    def eval_on_selector_all(self, selector: str, _script: str) -> list[dict[str, Any]]:
        self.extract_calls += 1
        if not self._extract_script:
            return []
        return self._extract_script.pop(0)

    def eval_on_selector(self, selector: str, _script: str) -> str:
        self.first_row_calls += 1
        if not self._first_row_script:
            # Playwright raises when zero elements match; our code
            # tolerates this by walking to the next selector. Mimic the
            # "no match" branch via an exception.
            raise RuntimeError("eval_on_selector: no element matched")
        return self._first_row_script.pop(0)

    def wait_for_load_state(self, state: str, *, timeout: int = 0) -> None:
        if state == "networkidle":
            self.networkidle_calls += 1

    def wait_for_timeout(self, ms: int) -> None:
        self.timeout_calls.append(int(ms))


# ---------------------------------------------------------------------------
# _resilient_extract_conversations_from_page (B2)
# ---------------------------------------------------------------------------


_REAL_CHAT_ROW = {
    "hr_name": "周晟业",
    "company": "杭州未知科技有限公司",
    "job_title": "AI Agent 实习生",
    "latest_message": "您好，方便聊聊吗？",
    "latest_time": "10:42",
    "unread_count": 2,
    "my_last_sent_status": "",
}


# The single-shot extractor walks its PULSE_BOSS_CHAT_ROW_SELECTORS chain
# and only breaks once one of them returns a non-empty list. The default
# chain is now pinned to the DOM contract (README L11, ``.user-list > li``)
# and has exactly 1 selector — so one "attempt" = 1 eval_on_selector_all
# call. See _default_chat_row_selectors() post-mortem note
# (trace_ff91c91b0aaf, 2026-04-22): older default had 4 never-matching
# guesses which is what made rows=0 look like "empty inbox" in prod.
_SELECTORS_PER_ATTEMPT = 1


def test_resilient_extract_recovers_after_empty_first_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Real trace: after ``_switch_chat_tab`` returns, BOSS has not yet
    repainted the ``<li>`` nodes. The first single-shot extract sees
    zero rows across every selector candidate; 700-900 ms later the
    list fills in. This test pins that the resilient wrapper waits +
    retries and ultimately returns the repopulated row — not an empty
    list that upstream would translate to ``no_result`` for the user.
    """
    monkeypatch.setenv("PULSE_BOSS_CHAT_EXTRACT_ATTEMPTS", "3")
    # attempt 0 = all 4 selectors return [] (full miss, triggers retry);
    # attempt 1 = first selector hits, chain breaks early.
    attempt_0 = [[]] * _SELECTORS_PER_ATTEMPT
    attempt_1 = [[_REAL_CHAT_ROW]]
    page = _FakePage(extract_script=attempt_0 + attempt_1)

    out = _resilient_extract_conversations_from_page(page, max_items=5)

    assert len(out) == 1
    assert out[0]["hr_name"] == "周晟业"
    assert out[0]["unread_count"] == 2
    assert page.extract_calls == _SELECTORS_PER_ATTEMPT + 1, (
        "attempt 0 must walk the full selector chain (4 misses); attempt 1 "
        "breaks on the first hit — anything higher means we wasted a slot."
    )
    assert page.networkidle_calls >= 1, (
        "between empty attempts the wrapper must soft-wait networkidle; "
        "otherwise we'd be busy-polling at CPU speed."
    )
    assert page.timeout_calls, "back-off sleep must run between attempts"


def test_resilient_extract_caps_attempts_and_fails_loud_on_sustained_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the DOM genuinely stays empty (tab has no unread), we must
    give up after the configured cap and return [] — not loop forever,
    not fabricate rows. Fail-loud empty is the intended signal for
    upstream "no unread messages" handling.
    """
    monkeypatch.setenv("PULSE_BOSS_CHAT_EXTRACT_ATTEMPTS", "3")
    total_calls = 3 * _SELECTORS_PER_ATTEMPT
    page = _FakePage(extract_script=[[]] * total_calls)

    out = _resilient_extract_conversations_from_page(page, max_items=5)

    assert out == []
    assert page.extract_calls == total_calls, (
        "each attempt must fully exhaust the selector chain before the "
        "outer retry kicks in; fewer means we bailed early on a miss, "
        "more means we ignored the attempt cap."
    )


def test_resilient_extract_single_attempt_mode_bypasses_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operator override: ``PULSE_BOSS_CHAT_EXTRACT_ATTEMPTS=1`` turns
    the wrapper into a thin passthrough. Useful when debugging raw
    extractor behavior; must actually disable the retry loop.
    """
    monkeypatch.setenv("PULSE_BOSS_CHAT_EXTRACT_ATTEMPTS", "1")
    page = _FakePage(extract_script=[[]] * _SELECTORS_PER_ATTEMPT)

    out = _resilient_extract_conversations_from_page(page, max_items=5)

    assert out == []
    assert page.extract_calls == _SELECTORS_PER_ATTEMPT, (
        "a single attempt still walks the selector chain — retry_off means "
        "no OUTER retry, not no INNER selector fallback."
    )


# ---------------------------------------------------------------------------
# _chat_list_first_row_signature (B1 building block)
# ---------------------------------------------------------------------------


def test_first_row_signature_is_deterministic_for_same_dom() -> None:
    """Given the same rendered first row, the signature must be equal.
    This is what the ``_switch_chat_tab`` poll loop relies on to tell
    "nothing changed yet" — if equal DOM produced different strings
    we'd spam false-positive tab-switched signals."""
    page = _FakePage(
        extract_script=[],
        first_row_script=["周晟业|Acme|2|10:42", "周晟业|Acme|2|10:42"],
    )
    sig_a = _chat_list_first_row_signature(page)
    sig_b = _chat_list_first_row_signature(page)
    assert sig_a == sig_b
    assert sig_a == "周晟业|Acme|2|10:42"


def test_first_row_signature_differs_when_row_content_changes() -> None:
    """Tab switch contract: when BOSS re-renders with a different
    top row (different HR / different badge count / different time),
    the signature MUST change. This is the positive signal the poll
    loop waits for."""
    before = _FakePage(extract_script=[], first_row_script=["周晟业|Acme|2|10:42"])
    after = _FakePage(extract_script=[], first_row_script=["胡女士|Beta|1|10:50"])
    assert _chat_list_first_row_signature(before) != _chat_list_first_row_signature(after)


def test_first_row_signature_returns_empty_when_list_has_no_rows() -> None:
    """When every candidate selector returns no match (empty list tab),
    the signature must be ``""`` rather than an exception. This lets
    ``_switch_chat_tab`` treat "was empty, still empty" as
    snapshot_unchanged and fall through to the extractor's own retry,
    instead of crashing the whole pull path."""
    page = _FakePage(extract_script=[], first_row_script=[])
    assert _chat_list_first_row_signature(page) == ""


# ---------------------------------------------------------------------------
# _extract_conversations_from_page contract sanity (parity check)
# ---------------------------------------------------------------------------


def test_single_shot_extractor_still_returns_contract_row_for_valid_input() -> None:
    """Resilience wrapping must not change the row shape the old
    single-shot extractor emits — downstream (``pull_conversations``)
    still filters/normalizes on the exact keys pinned by
    ``docs/dom-specs/boss/chat-list/README.md``.
    """
    page = _FakePage(extract_script=[[_REAL_CHAT_ROW]])
    out = _extract_conversations_from_page(page, max_items=5)

    assert len(out) == 1
    row = out[0]
    assert set(row.keys()) >= {
        "conversation_id",
        "hr_name",
        "company",
        "job_title",
        "latest_message",
        "latest_time",
        "unread_count",
        "my_last_sent_status",
        "source",
    }
    assert row["source"] == "boss_mcp_browser_chat"
    assert len(row["conversation_id"]) == 16


def test_single_shot_extractor_raises_on_eval_failure() -> None:
    """Fail-loud contract: selector eval crash (target closed / execution
    context gone / invalid selector) is an executor incident, not empty inbox.
    """

    class _BrokenEvalPage(_FakePage):
        def __init__(self) -> None:
            super().__init__(extract_script=[])

        def eval_on_selector_all(self, selector: str, _script: str) -> list[dict[str, Any]]:
            raise RuntimeError("Target page, context or browser has been closed")

    with pytest.raises(RuntimeError, match="Target page, context or browser has been closed"):
        _extract_conversations_from_page(_BrokenEvalPage(), max_items=5)


# ---------------------------------------------------------------------------
# _switch_chat_tab baseline contract (00:47 trace post-mortem)
#
# Bug report: multiple traces logged ``chat_tab switched waited_ms=0`` even
# when the tab click had no visible effect. Root cause: ``before_sig`` was
# empty (list not yet rendered), then the first poll saw a freshly rendered
# non-empty sig and declared "changed". A correct signature-change detector
# must refuse to compare against an empty baseline.
# ---------------------------------------------------------------------------


class _RecordingLocator:
    def __init__(self) -> None:
        self.click_calls = 0

    def click(self, *_a: Any, **_kw: Any) -> None:
        self.click_calls += 1


def test_switch_chat_tab_skips_click_when_baseline_never_populates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Baseline contract: if ``.user-list`` never renders a row within the
    baseline window, tab-switch cannot be verified and we must NOT log a
    fake "switched waited_ms=0". Return empty selector so the extractor
    (which anchors on ``.notice-badge``) becomes the sole authority.

    Real 00:47 trace: first-row signature was empty at sample time; the
    post-click poll found a non-empty sig and we wrongly claimed success.
    """
    from pulse.mcp_servers import _boss_platform_runtime as rt

    class _Page:
        def __init__(self) -> None:
            self.timeouts: list[int] = []

        def wait_for_selector(self, *_a: Any, **_kw: Any) -> Any:
            return None

        def locator(self, *_a: Any, **_kw: Any) -> Any:
            class _L:
                @property
                def first(self) -> Any:
                    return _RecordingLocator()
            return _L()

        def eval_on_selector(self, *_a: Any, **_kw: Any) -> str:
            raise RuntimeError("no element matched")

        def wait_for_timeout(self, ms: int) -> None:
            self.timeouts.append(int(ms))

    page = _Page()
    recorder = _RecordingLocator()
    monkeypatch.setattr(rt, "_wait_for_any_selector", lambda *_a, **_kw: (recorder, "text=未读"))
    monkeypatch.setenv("PULSE_BOSS_CHAT_BASELINE_MS", "600")

    result = rt._switch_chat_tab(page, chat_tab="未读")

    assert result == "", (
        "baseline never rendered — function must fail loud via empty "
        "return, not click blindly and claim a verified switch."
    )
    assert recorder.click_calls == 0, (
        "without a valid baseline, the click-and-verify contract can't be "
        "honored; clicking would hide the ambiguity."
    )


def test_switch_chat_tab_records_switch_when_baseline_then_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Positive path: baseline non-empty, click lands, post-click sig changes.
    Must log the real ``waited_ms`` (not the false ``0`` that empty→filled
    would produce)."""
    from pulse.mcp_servers import _boss_platform_runtime as rt

    class _Page:
        def __init__(self) -> None:
            self._sig_script = [
                "周晟业|Acme|2|10:42",  # baseline probe
                "周晟业|Acme|2|10:42",  # poll 0 (not yet changed)
                "胡女士|Beta|1|10:50",  # poll 1 (changed!)
            ]
            self.timeouts: list[int] = []

        def wait_for_selector(self, *_a: Any, **_kw: Any) -> Any:
            return None

        def locator(self, *_a: Any, **_kw: Any) -> Any:
            class _L:
                @property
                def first(self) -> Any:
                    return recorder
            return _L()

        def eval_on_selector(self, *_a: Any, **_kw: Any) -> str:
            if not self._sig_script:
                raise RuntimeError("no element matched")
            return self._sig_script.pop(0)

        def wait_for_timeout(self, ms: int) -> None:
            self.timeouts.append(int(ms))

        def wait_for_load_state(self, *_a: Any, **_kw: Any) -> None:
            return None

    page = _Page()
    recorder = _RecordingLocator()
    monkeypatch.setattr(rt, "_wait_for_any_selector", lambda *_a, **_kw: (recorder, "text=未读"))

    result = rt._switch_chat_tab(page, chat_tab="未读")

    assert result == "text=未读"
    assert recorder.click_calls == 1


# ---------------------------------------------------------------------------
# _pull_conversations_via_browser fail-loud contract
#
# Checklist §类型A, "伪造空结果零容忍": a playwright error mid-pull (page
# died / JS eval threw) MUST surface as status=executor_error, NOT as a
# silent rows=0 "empty inbox" dict. The prior implementation of
# _ensure_chat_list_hydrated / _diagnose_empty_chat_list used three
# try/except Exception: pass blocks that collapsed real failures into
# a rows=0 response — this test pins the fix against regression.
# ---------------------------------------------------------------------------


class _DeadPage:
    """Playwright page that pretends goto succeeded but every subsequent
    DOM interaction raises. Simulates a crashed/closed tab."""

    def __init__(self) -> None:
        self.url = "https://www.zhipin.com/web/geek/chat"

    def goto(self, *_a: Any, **_kw: Any) -> None:
        return None

    def inner_text(self, *_a: Any, **_kw: Any) -> str:
        return ""

    def bring_to_front(self) -> None:
        raise RuntimeError("Target page, context or browser has been closed")

    def evaluate(self, *_a: Any, **_kw: Any) -> Any:
        raise RuntimeError("Target page, context or browser has been closed")

    def wait_for_selector(self, *_a: Any, **_kw: Any) -> Any:
        raise RuntimeError("Target page, context or browser has been closed")

    def locator(self, *_a: Any, **_kw: Any) -> Any:
        raise RuntimeError("Target page, context or browser has been closed")


def test_pull_conversations_translates_playwright_error_to_executor_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pulse.mcp_servers import _boss_platform_runtime as rt

    monkeypatch.setattr(rt, "_ensure_browser_page", lambda: _DeadPage())
    monkeypatch.setattr(rt, "_detect_runtime_risk", lambda page, *, current_url: "")

    result = rt._pull_conversations_via_browser(
        max_conversations=5,
        unread_only=True,
        fetch_latest_hr=False,
        chat_tab="未读",
    )

    assert result["ok"] is False, (
        "A dead page mid-pull must NOT be reported as ok=True with rows=0; "
        "that's the exact 'fake empty result' pattern the checklist forbids."
    )
    assert result["status"] == "executor_error", (
        f"expected status=executor_error, got {result['status']!r}. "
        "Playwright errors must surface, not be laundered into rows=0."
    )
    assert result["items"] == []
    assert result["errors"], "error detail must be propagated to caller"


def test_pull_conversations_recovers_with_unread_badge_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pulse.mcp_servers import _boss_platform_runtime as rt

    class _Page:
        def __init__(self) -> None:
            self.url = "https://www.zhipin.com/web/geek/chat"

        def goto(self, *_a: Any, **_kw: Any) -> None:
            return None

        def inner_text(self, *_a: Any, **_kw: Any) -> str:
            return ""

    monkeypatch.setattr(rt, "_ensure_browser_page", lambda: _Page())
    monkeypatch.setattr(rt, "_detect_runtime_risk", lambda page, *, current_url: "")
    monkeypatch.setattr(rt, "_ensure_chat_list_hydrated", lambda page: ".user-list")
    monkeypatch.setattr(rt, "_switch_chat_tab", lambda page, *, chat_tab: "text=未读")

    # Primary extractor produced rows but marked every row unread_count=0,
    # which used to be filtered into an empty inbox.
    monkeypatch.setattr(
        rt,
        "_resilient_extract_conversations_from_page",
        lambda page, *, max_items: [
            {
                "conversation_id": "aaaaaaaaaaaaaaaa",
                "hr_name": "韦先生",
                "company": "杭州流亮科技有限公司",
                "job_title": "招聘者",
                "latest_message": "你好",
                "latest_time": "23:28",
                "unread_count": 0,
                "my_last_sent_status": "",
                "source": "boss_mcp_browser_chat",
            }
        ],
    )
    monkeypatch.setattr(
        rt,
        "_extract_unread_conversations_by_badge",
        lambda page, *, max_items: [
            {
                "conversation_id": "bbbbbbbbbbbbbbbb",
                "hr_name": "韦先生",
                "company": "杭州流亮科技有限公司",
                "job_title": "招聘者",
                "latest_message": "你好",
                "latest_time": "23:28",
                "unread_count": 1,
                "my_last_sent_status": "",
                "source": "boss_mcp_browser_chat",
            }
        ],
    )

    result = rt._pull_conversations_via_browser(
        max_conversations=20,
        unread_only=True,
        fetch_latest_hr=True,
        chat_tab="未读",
    )

    assert result["ok"] is True
    assert result["status"] == "ready"
    assert len(result["items"]) == 1
    assert result["items"][0]["unread_count"] == 1
    assert result["unread_total"] == 1


def test_pull_conversations_diagnose_failure_does_not_override_no_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Diagnosis is observability-only. If page closes while collecting the
    empty snapshot, business result stays no_result instead of being rewritten
    to executor_error by the logger path.
    """
    from pulse.mcp_servers import _boss_platform_runtime as rt

    class _Page:
        def __init__(self) -> None:
            self.url = "https://www.zhipin.com/web/geek/chat"

        def goto(self, *_a: Any, **_kw: Any) -> None:
            return None

        def inner_text(self, *_a: Any, **_kw: Any) -> str:
            return ""

    monkeypatch.setattr(rt, "_ensure_browser_page", lambda: _Page())
    monkeypatch.setattr(rt, "_detect_runtime_risk", lambda page, *, current_url: "")
    monkeypatch.setattr(rt, "_ensure_chat_list_hydrated", lambda page: ".user-list")
    monkeypatch.setattr(rt, "_switch_chat_tab", lambda page, *, chat_tab: "text=未读")
    monkeypatch.setattr(rt, "_resilient_extract_conversations_from_page", lambda page, *, max_items: [])
    monkeypatch.setattr(rt, "_extract_unread_conversations_by_badge", lambda page, *, max_items: [])
    monkeypatch.setattr(
        rt,
        "_diagnose_empty_chat_list",
        lambda page: (_ for _ in ()).throw(RuntimeError("Target page, context or browser has been closed")),
    )

    result = rt._pull_conversations_via_browser(
        max_conversations=20,
        unread_only=True,
        fetch_latest_hr=True,
        chat_tab="未读",
    )

    # no_result is a legitimate success state (inbox truly empty of unread)
    # — ok=True is the signal that "executor path ran, no incident occurred,
    # and there was simply nothing to show". A diagnostic throw must never
    # flip this to executor_error.
    assert result["ok"] is True
    assert result["status"] == "no_result"
    assert result["items"] == []
    assert result["errors"] == []


def test_pull_conversations_no_result_reports_empty_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Genuinely-empty unread tab must NOT surface as ``errors_total=1``
    upstream. Trace 00:47 post-mortem: a prior successful pass read all
    3 unread conversations; the next tick correctly saw zero badges but
    the gateway was stamping ``browser_status=no_result`` into ``errors``
    because ``_pull_conversations_via_browser`` returned ok=False. That
    made the agent say "执行失败" when the real answer was "已全部读完".
    """
    from pulse.mcp_servers import _boss_platform_runtime as rt

    class _Page:
        def __init__(self) -> None:
            self.url = "https://www.zhipin.com/web/geek/chat"

        def goto(self, *_a: Any, **_kw: Any) -> None:
            return None

        def inner_text(self, *_a: Any, **_kw: Any) -> str:
            return ""

    monkeypatch.setattr(rt, "_ensure_browser_page", lambda: _Page())
    monkeypatch.setattr(rt, "_detect_runtime_risk", lambda page, *, current_url: "")
    monkeypatch.setattr(rt, "_ensure_chat_list_hydrated", lambda page: ".user-list")
    monkeypatch.setattr(rt, "_switch_chat_tab", lambda page, *, chat_tab: "text=未读")
    monkeypatch.setattr(rt, "_resilient_extract_conversations_from_page", lambda page, *, max_items: [])
    monkeypatch.setattr(rt, "_extract_unread_conversations_by_badge", lambda page, *, max_items: [])
    monkeypatch.setattr(
        rt,
        "_diagnose_empty_chat_list",
        lambda page: {
            "url": "https://www.zhipin.com/web/geek/chat",
            "ready_state": "complete",
            "visible": True,
            "body_li": 40,
            "pinned_row_count": 40,
            "visible_row_count": 40,
            "unread_badge_count": 0,
            "visible_unread_badge_count": 0,
            "containers": [],
        },
    )

    result = rt._pull_conversations_via_browser(
        max_conversations=20,
        unread_only=True,
        fetch_latest_hr=True,
        chat_tab="未读",
    )

    assert result["ok"] is True, (
        "empty inbox is not a failure; ok=False here was the root cause of "
        "the '系统无法支持该操作' false-negative message."
    )
    assert result["status"] == "no_result"
    assert result["items"] == []
    assert result["unread_total"] == 0
    assert result["errors"] == []


# ---------------------------------------------------------------------------
# Code ↔ DOM-spec README consistency
#
# Checklist §类型C / §契约消费方 + dom-specs/README.md: the row selector is
# *the* anchor that ties four things together — (a) what the DOM dump
# stored under docs/dom-specs/boss/chat-list/ actually matches, (b) what
# _extract_conversations_from_page queries, (c) what the README markdown
# says in the selector table, (d) what the auto-reply E2E depends on.
# 2026-04-22 trace_ff91c91b0aaf post-mortem: drift on (b) alone (default
# was ".friend-list li" + three siblings, none of which matched BOSS's
# real DOM) was enough to produce rows=0 forever, and every defensive
# wait/retry on top of it was wasted work. Pin both ends of the chain:
# ---------------------------------------------------------------------------


def test_default_chat_row_selector_matches_live_dom_and_dump() -> None:
    """Row selector must mirror the **live BOSS DOM**, not the markdown
    README. trace_24ecd22aa795 (2026-04-22) proved the point: direct-child
    ``.user-list > li`` matched 0 rows while descendant ``.user-list li``
    matched 2 rows (aligned with user-visible "未读(2)" tab). BOSS wraps
    <li> inside a nested element, so descendant match is required.

    Contract order (reversed from the abandoned 2026-04-22 morning attempt):
    live DOM → dump fixture JSON → code default → markdown README.
    Docs are evidence, not authority. The dump JSON's ``hit_selector``
    is the closest serialised snapshot of live DOM and is what this test
    binds to — not the README prose (which had drifted to direct-child
    shorthand and caused a false "fix" round).
    """
    import json
    from pathlib import Path

    from pulse.mcp_servers._boss_platform_runtime import (
        _default_chat_row_selectors,
    )

    repo_root = Path(__file__).resolve().parents[3]
    dump = repo_root / "docs/dom-specs/boss/chat-list/20260422T072555Z.json"
    assert dump.exists(), f"dom-spec dump missing: {dump}"
    data = json.loads(dump.read_text(encoding="utf-8"))
    dump_selector = str(data.get("hit_selector") or "")

    code_default = _default_chat_row_selectors()

    # Invariant: the code's primary selector MUST equal the dump's
    # hit_selector exactly. If BOSS changes DOM, re-dump live, and the
    # dump will flow into this assertion on the next run.
    assert code_default == [dump_selector], (
        f"code default {code_default!r} diverged from live-DOM dump "
        f"hit_selector {dump_selector!r}. Do NOT edit this test — "
        f"re-dump chat-list against live BOSS, update the JSON's "
        f"hit_selector to whatever actually matched, then mirror it "
        f"in _default_chat_row_selectors()."
    )


# Keep symbols imported so the module graph stays wired; real behavior
# for hydrate/diagnose is pinned by the live smoke path, not pytest.
_ = (_ensure_chat_list_hydrated, _diagnose_empty_chat_list)
