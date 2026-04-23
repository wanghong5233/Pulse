"""Fixture-backed selector contract for ``send_resume_attachment``.

These tests pin two invariants produced by the constitutional refactor of
``_boss_platform_runtime._execute_browser_send_resume_attachment``:

1.  **Selector routing list** (``_default_attach_trigger_selectors``) MUST
    expose the modern BOSS chat toolbar button (文字 "发简历") *before*
    the legacy paperclip-icon selectors, so the current production UI hits
    the fast path. Regression protection for trace_a825a6d00d13, where
    the list was icon-only and every send_resume call failed with
    ``"attach trigger selector not found"``.

2.  **DOM anchor reality** — the 工具栏 selector MUST actually match the
    "发简历" button node in the machine-generated dump at
    ``docs/dom-specs/boss/chat-detail/20260422T073442Z.html``. Per 编码宪法
    条款"以真实 DOM 为准、文档为次"(see ``docs/code-review-checklist.md``),
    we do NOT assert against hand-written markdown — we parse the real
    dump and check the node is reachable.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from pulse.mcp_servers._boss_platform_runtime import (
    _default_attach_trigger_selectors,
)

_CHAT_DETAIL_DUMP = (
    Path(__file__).resolve().parents[3]
    / "docs"
    / "dom-specs"
    / "boss"
    / "chat-detail"
    / "20260422T073442Z.html"
)


class TestAttachTriggerSelectorListShape:
    """The list's *ordering* is load-bearing — cost of a drift is a whole
    unread cycle burning on ``selector_missing`` before the fallback.
    """

    def test_first_entries_target_the_toolbar_resume_button(self) -> None:
        selectors = _default_attach_trigger_selectors()
        head = selectors[:3]
        joined = " | ".join(head)
        assert "发简历" in joined, (
            "first 3 trigger selectors MUST target 现代 UI 的工具栏 '发简历' "
            f"文字按钮; actual head = {head!r}. Removing these silently "
            "regresses trace_a825a6d00d13 ('attach trigger selector not found')."
        )
        assert all(".chat-controls" in s or ".message-controls" in s for s in head), (
            "the toolbar selectors must anchor under .chat-controls or "
            ".message-controls so they do not collide with HR cards "
            f"containing '发简历' text in their body; actual head = {head!r}"
        )

    def test_legacy_paperclip_selectors_remain_as_fallback(self) -> None:
        selectors = _default_attach_trigger_selectors()
        joined = " | ".join(selectors)
        assert "icon-attach" in joined, (
            "Do NOT drop the legacy paperclip selectors — another account type "
            "(or an A/B bucket) may still render the old menu entry point. "
            "Regression insurance."
        )


class TestAttachTriggerSelectorMatchesRealDom:
    """Anchor: the actual BOSS chat detail dump captured on 2026-04-22."""

    @pytest.fixture(scope="class")
    def dump_soup(self) -> BeautifulSoup:
        # The dump wraps the real DOM as entity-encoded text inside
        # <details><pre>…</pre></details>, not as live HTML nodes. Parse
        # the wrapper, pull the last <pre>'s text, then re-parse that.
        if not _CHAT_DETAIL_DUMP.exists():
            pytest.skip(f"chat-detail dump missing: {_CHAT_DETAIL_DUMP}")
        outer = BeautifulSoup(
            _CHAT_DETAIL_DUMP.read_text(encoding="utf-8"), "html.parser"
        )
        pres = outer.find_all("pre")
        assert pres, f"dump malformed: no <pre> block found in {_CHAT_DETAIL_DUMP}"
        inner_html = pres[-1].get_text()
        assert "<div" in inner_html, (
            "dump's last <pre> MUST hold the raw outerHTML snapshot; got "
            f"first 120 chars = {inner_html[:120]!r}"
        )
        return BeautifulSoup(inner_html, "html.parser")

    def test_chat_controls_container_is_present_in_dump(
        self, dump_soup: BeautifulSoup
    ) -> None:
        containers = dump_soup.select(".chat-controls")
        assert containers, (
            "Regression: the chat-detail dump no longer has .chat-controls. "
            "Re-dump the conversation page and pin a fresh fixture before "
            "touching send_resume selectors."
        )

    def test_resume_button_reachable_under_chat_controls(
        self, dump_soup: BeautifulSoup
    ) -> None:
        resume_nodes = [
            el
            for container in dump_soup.select(".chat-controls")
            for el in container.select(".toolbar-btn, .toolbar-btn-content")
            if "发简历" in el.get_text(strip=True)
        ]
        assert resume_nodes, (
            "The '发简历' text button MUST be reachable via "
            "'.chat-controls .toolbar-btn' or '.chat-controls .toolbar-btn-content'. "
            "If this fires, BOSS restructured the toolbar DOM and "
            "_default_attach_trigger_selectors needs a matching update — "
            "do NOT comment out the test."
        )


class TestDirectResumeVerifierContract:
    """Direct-button path must be observable, not optimistic."""

    def test_direct_button_path_has_post_click_verifier(self) -> None:
        from pulse.mcp_servers import _boss_platform_runtime as runtime
        import inspect

        source = inspect.getsource(runtime._execute_browser_send_resume_attachment)
        assert "_wait_direct_resume_send_effect" in source, (
            "Direct '发简历' path must run post-click DOM verification. "
            "Without it, no-confirm flows can return ok=true while nothing "
            "was actually sent."
        )
        assert '"status": "verify_failed"' in source, (
            "When direct-button click has no observable DOM delta, executor "
            "must fail-loud with status=verify_failed instead of silently "
            "returning sent."
        )

    def test_send_state_probe_tracks_message_and_agree_nodes(self) -> None:
        from pulse.mcp_servers import _boss_platform_runtime as runtime
        import inspect

        source = inspect.getsource(runtime._snapshot_resume_send_state)
        for anchor in (".message-item", "item-myself", "btn-agree"):
            assert anchor in source, (
                f"send-state probe lost DOM anchor `{anchor}`; direct-button "
                "verification would regress to blind success."
            )


class TestResumeCardAgreePriority:
    """HR 用 '交换简历' 卡片抛球时,唯一真正可靠的发简历路径是点击会话里
    内置的 '同意' 按钮 — 不是工具栏的 '发简历'。executor 必须先判定有没有
    这张卡片,命中就走 card 路径,不要再去按工具栏兜底。
    """

    def test_executor_probes_respond_popover_before_toolbar_trigger(self) -> None:
        from pulse.mcp_servers import _boss_platform_runtime as runtime
        import inspect

        source = inspect.getsource(runtime._execute_browser_send_resume_attachment)
        card_idx = source.find("_locate_resume_card_agree")
        trigger_idx = source.find("_default_attach_trigger_selectors")
        assert card_idx != -1, (
            "executor must probe HR's respond-popover via "
            "_locate_resume_card_agree before falling back to toolbar "
            "trigger. See trace_59effe763b0f: HR sent the exchange-resume "
            "card, toolbar click silently produced no send."
        )
        assert trigger_idx != -1, (
            "toolbar fallback (_default_attach_trigger_selectors) must "
            "still be present for non-card HR messages."
        )
        assert card_idx < trigger_idx, (
            "Ordering contract: card-agree probe MUST precede toolbar "
            "trigger — otherwise we regress to clicking toolbar '发简历' "
            "while HR's card is still unanswered."
        )

    def test_card_agree_selectors_anchor_on_respond_popover(self) -> None:
        from pulse.mcp_servers import _boss_platform_runtime as runtime

        selectors = runtime._RESUME_CARD_AGREE_SELECTORS
        assert selectors, "card-agree selector list must not be empty"
        for selector in selectors:
            assert "btn-agree" in selector, (
                f"selector {selector!r} must target the '同意' button class "
                "on HR's respond-popover card."
            )
        assert any("respond-popover" in s for s in selectors), (
            "at least one selector must anchor under .respond-popover to "
            "stay aligned with chat-detail §C.4."
        )

    def test_card_agree_selectors_resolve_on_real_dump(self) -> None:
        from pulse.mcp_servers._boss_platform_runtime import _RESUME_CARD_AGREE_SELECTORS

        if not _CHAT_DETAIL_DUMP.exists():
            pytest.skip(f"chat-detail dump missing: {_CHAT_DETAIL_DUMP}")
        outer = BeautifulSoup(
            _CHAT_DETAIL_DUMP.read_text(encoding="utf-8"), "html.parser"
        )
        pres = outer.find_all("pre")
        assert pres, f"dump malformed: no <pre> block found in {_CHAT_DETAIL_DUMP}"
        inner = BeautifulSoup(pres[-1].get_text(), "html.parser")

        # BS4 selector syntax does not parse Playwright-only extensions, so
        # we only assert selectors that are pure CSS. The important anchor
        # (.respond-popover .btn.btn-agree) is pure CSS.
        matched = False
        for selector in _RESUME_CARD_AGREE_SELECTORS:
            try:
                nodes = inner.select(selector)
            except Exception:
                continue
            if nodes:
                matched = True
                break
        assert matched, (
            "No selector in _RESUME_CARD_AGREE_SELECTORS matches the real "
            "chat-detail dump. BOSS restructured the HR card — re-dump and "
            "realign selectors, do NOT weaken the test."
        )

    def test_toolbar_confirm_selectors_cover_sentence_popover(self) -> None:
        from pulse.mcp_servers import _boss_platform_runtime as runtime

        selectors = runtime._default_attach_confirm_selectors()
        joined = " | ".join(selectors)
        assert ".sentence-popover" in joined, (
            "Toolbar fallback must know about BOSS's 现代 UI 确认弹层 "
            "(.sentence-popover .btn-sure) — pure 'text=确定发送' misses "
            "when button label is '确认'."
        )
        assert ".btn-sure" in joined, (
            ".btn-sure anchor must remain in confirm list; it is the "
            "class BOSS uses on the pop-wrap confirm primary button."
        )

    def test_confirm_clicked_path_also_waits_for_dom_delta(self) -> None:
        """Clicking confirm ≠ BOSS actually delivered; the executor MUST
        wait for an observable DOM delta after confirm too. This is the
        "两条路径都必须真发送成功" contract — card path and toolbar path
        both funnel through _wait_direct_resume_send_effect.
        """

        from pulse.mcp_servers import _boss_platform_runtime as runtime
        import inspect

        source = inspect.getsource(runtime._execute_browser_send_resume_attachment)
        confirm_block_idx = source.find("confirm_loc is not None")
        assert confirm_block_idx != -1, (
            "executor lost the confirm-click branch; resume send will "
            "regress on the toolbar path."
        )
        tail = source[confirm_block_idx:]
        next_branch = tail.find("elif not is_direct_resume_button")
        assert next_branch != -1, "confirm branch shape changed"
        confirm_body = tail[:next_branch]
        assert "_wait_direct_resume_send_effect" in confirm_body, (
            "confirm-click branch must call _wait_direct_resume_send_effect "
            "to verify BOSS actually echoed the resume; otherwise returning "
            "status='sent' after only a confirm click is a false positive."
        )
        assert '"status": "verify_failed"' in confirm_body, (
            "confirm-click branch must be able to fail-loud with "
            "status=verify_failed when no DOM delta appears."
        )


class TestKillswitchDryRunIsDistinguishable:
    """PULSE_BOSS_MCP_REPLY_MODE=log_only/dry_run_ok must return a status
    that downstream code can recognise as 'not delivered'. Returning plain
    'sent' or 'logged' was the root cause of 01:46~02:06 logs showing 10+
    1ms send_resume_attachment calls as 'ok' while BOSS received nothing.
    """

    def test_send_resume_dry_run_returns_logged_only_not_sent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from pulse.mcp_servers import _boss_platform_runtime as runtime

        monkeypatch.setenv("PULSE_BOSS_MCP_REPLY_MODE", "log_only")

        # The idempotency replay guard would short-circuit before the mode
        # branch runs, so monkey-patch it to force the env path.
        monkeypatch.setattr(
            runtime, "_find_recent_successful_action", lambda **_: None
        )
        monkeypatch.setattr(runtime, "_append_action_log", lambda _: None)

        result = runtime.send_resume_attachment(
            conversation_id="conv-dry-run",
            resume_profile_id="default",
            conversation_hint={},
        )

        assert result["status"] == "logged_only", (
            "Killswitch must emit distinct status 'logged_only' so service "
            "layer can separate dry-run from real browser delivery. "
            "Returning plain 'logged' regressed trace_f78829ce4576 "
            "(service layer laundered it to 'sent')."
        )
        assert result["status"] != "sent"
        assert result["ok"] is True  # contract: dry-run does not fail the RPC

    def test_reply_dry_run_returns_logged_only_not_sent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from pulse.mcp_servers import _boss_platform_runtime as runtime

        monkeypatch.setenv("PULSE_BOSS_MCP_REPLY_MODE", "dry_run_ok")
        monkeypatch.setattr(runtime, "_append_action_log", lambda _: None)

        result = runtime.reply_conversation(
            conversation_id="conv-dry-run",
            reply_text="test",
            profile_id="default",
            conversation_hint={},
        )

        assert result["status"] == "logged_only"
        assert result["status"] != "sent"
        assert result["ok"] is True

    def test_click_card_dry_run_returns_logged_only_not_sent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from pulse.mcp_servers import _boss_platform_runtime as runtime

        monkeypatch.setenv("PULSE_BOSS_MCP_REPLY_MODE", "log_only")
        monkeypatch.setattr(runtime, "_append_action_log", lambda _: None)

        result = runtime.click_conversation_card(
            conversation_id="conv-dry-run",
            card_id="card-1",
            card_type="exchange_resume",
            action="accept",
        )

        assert result["status"] == "logged_only"
        assert result["status"] != "sent"
        assert result["status"] != "clicked"
        assert result["ok"] is True


class TestReplyHasPostSendDomVerify:
    """reply_conversation must not regress to "click send → assume sent"
    — same failure mode as the original send_resume假绿. Anchor the
    invariant on the executor source.
    """

    def test_reply_executor_waits_for_dom_delta_after_send_click(self) -> None:
        from pulse.mcp_servers import _boss_platform_runtime as runtime
        import inspect

        source = inspect.getsource(runtime._execute_browser_reply)
        assert "_snapshot_resume_send_state" in source, (
            "reply executor must capture DOM state before clicking send so "
            "the post-click verifier has a baseline to diff against."
        )
        assert "_wait_direct_resume_send_effect" in source, (
            "reply executor must wait for an observable DOM delta after "
            "clicking send; otherwise risk-blocked / rate-limited replies "
            "silently report status=sent (same假绿 shape as send_resume "
            "before the verify_failed refactor)."
        )
        assert '"status": "verify_failed"' in source, (
            "reply executor must fail-loud with status=verify_failed when "
            "the send click produced no DOM delta; silently returning "
            "'sent' is the exact bug class we are pinning closed here."
        )
