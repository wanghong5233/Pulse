"""Pure-function guards for the BOSS scan row assembler.

Scope (宪法 §测试宪法):
    These tests pin **one** real defence line — the address-vs-company shape
    heuristic born from a concrete 26/26-miss audit trace
    (``boss_mcp_actions.jsonl``, 2026-04-22). They do not assert wording, they
    do not duplicate the implementation, and they are not there to make the
    suite "feel thorough". If this file ever needs to grow, every new case
    must cite a distinct real-world sample — no synthetic speculation.

What the heuristic owns:
    BOSS 地址节点文本形态是 "省·市·区/街道" 的 2~5 段 "·" 分隔串
    ("上海·浦东新区·张江"). 公司名节点 (``.boss-name``) 从未包含中点.
    若未来 BOSS 再次改版 CSS class 让 scan selector 再次错位命中地址,
    该启发式会把 ``row["company"]`` 拦截为空,让下游 matcher 把空公司
    放进 ``concerns``, 而不是把地址伪造成公司名输给 LLM.
"""

from __future__ import annotations

import pytest

from pulse.mcp_servers._boss_platform_runtime import _looks_like_address


class TestLooksLikeAddressPinsRealDefenceLine:
    """Anchor inputs: real values actually observed in `boss_mcp_actions.jsonl`.

    Historical bug: 26/26 greet rows stored `company="上海·XX·YY"` because the
    old selector `[class*='company']` matched `.company-location`. This guard
    is what lets the row assembler reject that shape even if selectors regress.
    """

    @pytest.mark.parametrize(
        "address",
        [
            "杭州·余杭区·仓前",        # trace_fe19c3ab1e43
            "上海·浦东新区·张江",
            "上海·徐汇区·漕河泾",      # logs/boss_card_dump_20260422T065914Z.json card #1
            "上海·浦东新区·陆家嘴",    # same dump, card #2
            "上海·杨浦区·五角场",      # same dump, card #3
        ],
    )
    def test_three_segment_dot_addresses_are_flagged(self, address: str) -> None:
        assert _looks_like_address(address) is True

    @pytest.mark.parametrize(
        "company",
        [
            "上海觅深科技有限公司",    # real card #1 `.boss-name`
            "上海简文",                 # real card #2 `.boss-name`
            "字节跳动",                 # real card #3 `.boss-name`
            "阿里巴巴",                 # trace_fe19c3ab1e43 实际投递对象
            "Pulse Labs",               # seed 数据, 英文公司名
        ],
    )
    def test_real_company_names_pass_through(self, company: str) -> None:
        assert _looks_like_address(company) is False

    def test_empty_input_is_not_an_address(self) -> None:
        # Defence must not block empty strings — downstream distinguishes
        # "missing" (empty) from "looks like address" (non-empty but wrong).
        assert _looks_like_address("") is False
        assert _looks_like_address("   ") is False
