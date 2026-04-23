"""Fixture-backed DOM contract for `_enrich_rows_with_latest_hr_message`.

Pins the live-DOM anchors that the enrich step depends on when it opens
each unread conversation's detail pane and reads the last HR bubble:

  .chat-conversation .im-list li.message-item.item-friend .message-content

Source of truth: docs/dom-specs/boss/chat-detail/20260422T073442Z.html
(real chat-detail dump captured on 2026-04-22). If BOSS drifts these
classes, the regression surfaces here rather than silently collapsing
planner input to the left-pane system placeholder "您正在与Boss X沟通"
(root cause of trace_1de26da60367 / trace_ac5e1ccf06ab where every
new conversation was ignore'd without ever opening the detail page).

Do not delete or relax — if the test fires, re-dump the live DOM first,
then update the code + this test in the same PR.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from bs4 import BeautifulSoup

_CHAT_DETAIL_DUMP = (
    Path(__file__).resolve().parents[3]
    / "docs"
    / "dom-specs"
    / "boss"
    / "chat-detail"
    / "20260422T073442Z.html"
)


@pytest.fixture(scope="module")
def dump_soup() -> BeautifulSoup:
    if not _CHAT_DETAIL_DUMP.exists():
        pytest.skip(f"chat-detail dump missing: {_CHAT_DETAIL_DUMP}")
    outer = BeautifulSoup(
        _CHAT_DETAIL_DUMP.read_text(encoding="utf-8"), "html.parser"
    )
    pres = outer.find_all("pre")
    assert pres, f"dump malformed: no <pre> block found in {_CHAT_DETAIL_DUMP}"
    inner_html = pres[-1].get_text()
    assert "chat-conversation" in inner_html, (
        "dump's last <pre> MUST hold the raw outerHTML snapshot of "
        ".chat-conversation; dump fixture is corrupted or truncated."
    )
    return BeautifulSoup(inner_html, "html.parser")


class TestChatDetailHrMessageSelector:
    def test_im_list_holds_hr_bubbles(self, dump_soup: BeautifulSoup) -> None:
        friends = dump_soup.select(
            ".chat-conversation .im-list li.message-item.item-friend"
        )
        assert len(friends) >= 1, (
            "Regression: `.chat-conversation .im-list li.message-item.item-friend` "
            "now returns 0. Enrich will always fall back to '' and planner "
            "will keep reading the left-pane placeholder. Re-dump DOM and "
            "update the selector together."
        )

    def test_hr_and_my_bubbles_are_disjoint(self, dump_soup: BeautifulSoup) -> None:
        friends = dump_soup.select(
            ".chat-conversation .im-list li.message-item.item-friend"
        )
        myself = dump_soup.select(
            ".chat-conversation .im-list li.message-item.item-myself"
        )
        overlap = {id(el) for el in friends} & {id(el) for el in myself}
        assert not overlap, (
            "item-friend and item-myself must never overlap; if they do, the "
            "enrich step would read our own reply back as the HR utterance."
        )

    def test_last_hr_bubble_has_readable_text(
        self, dump_soup: BeautifulSoup
    ) -> None:
        friends = dump_soup.select(
            ".chat-conversation .im-list li.message-item.item-friend"
        )
        assert friends, "no HR bubbles to anchor on (see prior assertion)"
        last = friends[-1]
        content = last.select_one(".message-content")
        assert content is not None, (
            "Last HR bubble has no `.message-content` — enrich will return "
            "empty string and planner loses real HR text. BOSS likely "
            "renamed the inner container class."
        )
        text = content.get_text(strip=True)
        assert text, (
            "Last HR bubble text is empty after stripping. Either BOSS "
            "started rendering message body in a different node, or the "
            "dump is stale."
        )
        # The captured dump's last HR bubble is the "我想要一份您的附件简历…"
        # card prompt. The keyword "简历" is the single most stable token
        # in HR → job-seeker exchanges on BOSS; asserting it pins that the
        # selector is actually reaching the semantic payload, not a time
        # stamp or avatar alt text.
        assert "简历" in text, (
            f"last HR bubble text does not mention '简历'; got {text[:80]!r}. "
            "Either BOSS replaced the dump's anchor conversation (re-dump "
            "and repoint the fixture) or the selector is now reaching a "
            "decorative element instead of the message body."
        )

    def test_top_info_exposes_hr_name_for_detail_sync_probe(
        self, dump_soup: BeautifulSoup
    ) -> None:
        # enrich waits for the detail pane to swap to the targeted row
        # by polling `.chat-conversation .top-info-content .name-text`.
        # If this anchor disappears, the wait_for_function in enrich will
        # time out on every row and fail loudly.
        name_el = dump_soup.select_one(
            ".chat-conversation .top-info-content .name-text"
        )
        assert name_el is not None, (
            "`.chat-conversation .top-info-content .name-text` is the "
            "only signal used by enrich to confirm the detail pane has "
            "switched to the targeted conversation. Removing it silently "
            "would collapse enrich into racing reads."
        )
        assert name_el.get_text(strip=True), (
            "HR name anchor is present but empty; enrich's equality check "
            "against target.hr_name would never succeed."
        )

    def test_pre_loading_indicator_is_part_of_detail_dom(
        self, dump_soup: BeautifulSoup
    ) -> None:
        # trace_1470872176ba proved enrich reads text_len=0 when we stop
        # waiting after .top-info is swapped but before .im-list is actually
        # populated. BOSS renders a `.pre-loading` loader inside
        # `.chat-record` during the fetch window; enrich's wait gate
        # explicitly hides on (loader absent OR loader offscreen). This
        # test pins that `.pre-loading` is a real DOM node in the dump,
        # so the wait contract has something concrete to key off of.
        loader = dump_soup.select_one(".chat-conversation .pre-loading")
        assert loader is not None, (
            "BOSS chat-detail dump no longer exposes `.pre-loading`. "
            "enrich's gate assumed this loader exists and uses its "
            "(in)visibility as the 'messages fetched?' signal. Either BOSS "
            "dropped the loader (update the wait condition + this test) or "
            "the dump is stale."
        )


class TestEnrichWaitContractShape:
    """The await_detail_js inside `_enrich_rows_with_latest_hr_message`
    MUST gate on three conditions together, because trace_1470872176ba
    proved any weaker gate reads `.im-list` before it is populated and
    collapses `latest_message` to empty string.
    """

    def test_await_detail_js_requires_all_three_gates(self) -> None:
        from pulse.mcp_servers import _boss_platform_runtime as runtime
        import inspect

        source = inspect.getsource(runtime._enrich_rows_with_latest_hr_message)
        # Gate A: top-info HR name equality with target.
        assert ".top-info-content .name-text" in source, (
            "enrich gate A lost: must key off .top-info-content .name-text "
            "to confirm SPA swapped conversations."
        )
        assert "expected" in source, (
            "enrich gate A must compare against the target HR name, not a "
            "constant — otherwise every row passes the same gate."
        )
        # Gate B: loader visibility check (pre-loading).
        assert ".pre-loading" in source, (
            "enrich gate B lost: must wait for .pre-loading to disappear so "
            "we do not read a still-fetching .im-list."
        )
        # Gate C: non-empty message-item in im-list.
        assert ".im-list .message-item" in source, (
            "enrich gate C lost: must require .im-list .message-item count "
            "> 0, otherwise empty-list read returns text_len=0 (trace "
            "trace_1470872176ba root cause)."
        )

    def test_empty_read_emits_diagnostic_not_silent_placeholder(self) -> None:
        from pulse.mcp_servers import _boss_platform_runtime as runtime
        import inspect

        source = inspect.getsource(runtime._enrich_rows_with_latest_hr_message)
        # When read_hr_js returns empty, enrich must log a diagnostic with
        # DOM state. We check for the diagnose payload keys so a refactor
        # cannot silently drop the observability.
        assert "diagnose_hr_js" in source, (
            "enrich empty-read path lost its DOM diagnostic helper; "
            "next regression will be invisible again."
        )
        for key in ("friend_items", "myself_items", "last_friend_class"):
            assert key in source, (
                f"diagnose_hr_js must expose `{key}` so the WARNING log "
                "distinguishes 'class drift' vs 'nothing rendered' vs "
                "'content subtree missing'. Dropping this key reverts us "
                "to the blind state of trace_1470872176ba."
            )
