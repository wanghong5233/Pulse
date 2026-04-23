"""ActionReport 契约层单测 — ADR-002.

这里只覆盖 ``core/action_report.py`` 本身的契约:

* ``ActionDetail`` / ``ActionReport`` 的 dataclass 不变性与字段约束
* ``ActionReport.build`` 的 status 自动推断规则 (succeeded/partial/failed)
  与显式覆盖 (preview/skipped)
* ``ActionReport`` ↔ ``dict`` 往返稳定 (用于跨 JSON 序列化的场景)
* ``ActionReport.to_prompt_lines`` 的 LLM-facing 渲染包含所有关键事实
* ``ActionReport.to_receipt_facts`` 的 Verifier-facing 字段投影
* ``extract_action_report`` 的三种兼容形态识别
* 通用性: 同一 schema 可以承载 job.greet / game.checkin / trip.plan
  三种差别很大的 action 场景 (不靠业务字段特判)

不涉及 Brain / Verifier / 具体 module 的集成 — 那些在各自的测试文件里覆盖.
"""

from __future__ import annotations

import pytest

from pulse.core.action_report import (
    ACTION_REPORT_KEY,
    ActionDetail,
    ActionReport,
    extract_action_report,
)


# ──────────────────────────────────────────────────────────────
# 1. ActionDetail 基础契约
# ──────────────────────────────────────────────────────────────


def test_action_detail_minimal_fields_and_frozenness() -> None:
    detail = ActionDetail(target="AIGC视觉生成实习", status="succeeded")

    assert detail.target == "AIGC视觉生成实习"
    assert detail.status == "succeeded"
    assert detail.reason is None
    assert detail.url is None
    assert detail.extras == {}

    with pytest.raises(Exception):  # frozen dataclass 禁止再赋值
        detail.target = "other"  # type: ignore[misc]


def test_action_detail_to_dict_drops_empty_optional_fields() -> None:
    detail = ActionDetail(target="后端实习", status="failed", reason="已联系过")

    assert detail.to_dict() == {
        "target": "后端实习",
        "status": "failed",
        "reason": "已联系过",
    }


def test_action_detail_from_dict_rejects_invalid_status() -> None:
    with pytest.raises(ValueError):
        ActionDetail.from_dict({"target": "x", "status": "exploded"})


def test_action_detail_roundtrip_preserves_all_fields() -> None:
    detail = ActionDetail(
        target="签到:原神",
        status="succeeded",
        reason=None,
        url="https://act.hoyoverse.com/...",
        extras={"reward_id": "101"},
    )
    back = ActionDetail.from_dict(detail.to_dict())
    assert back == detail


# ──────────────────────────────────────────────────────────────
# 2. ActionReport.build — status 推断规则 (details 聚合)
# ──────────────────────────────────────────────────────────────


def test_build_infers_succeeded_when_all_details_succeeded() -> None:
    report = ActionReport.build(
        action="job.greet",
        summary="投递了 2/2 个岗位",
        details=[
            ActionDetail(target="A", status="succeeded"),
            ActionDetail(target="B", status="succeeded"),
        ],
    )
    assert report.status == "succeeded"


def test_build_infers_partial_when_mixed_details() -> None:
    report = ActionReport.build(
        action="job.greet",
        summary="投递了 1/2",
        details=[
            ActionDetail(target="A", status="succeeded"),
            ActionDetail(target="B", status="failed", reason="已联系过"),
        ],
    )
    assert report.status == "partial"


def test_build_infers_failed_when_no_detail_succeeded() -> None:
    report = ActionReport.build(
        action="job.greet",
        summary="0/2",
        details=[
            ActionDetail(target="A", status="failed"),
            ActionDetail(target="B", status="skipped"),
        ],
    )
    assert report.status == "failed"


def test_build_infers_skipped_when_no_details() -> None:
    report = ActionReport.build(action="noop", summary="nothing to do")
    assert report.status == "skipped"


def test_build_allows_explicit_status_override_for_preview() -> None:
    # preview / skipped 这两种状态无法从 details 推出, 必须显式传.
    report = ActionReport.build(
        action="job.greet",
        summary="预览 2 个候选",
        status="preview",
        details=[
            ActionDetail(target="A", status="succeeded"),
            ActionDetail(target="B", status="succeeded"),
        ],
    )
    assert report.status == "preview"


def test_build_rejects_invalid_status() -> None:
    with pytest.raises(ValueError):
        ActionReport.build(
            action="x",
            summary="x",
            status="exploded",  # type: ignore[arg-type]
        )


# ──────────────────────────────────────────────────────────────
# 3. to_dict / from_dict 往返
# ──────────────────────────────────────────────────────────────


def test_action_report_roundtrip_preserves_semantics() -> None:
    report = ActionReport.build(
        action="job.greet",
        summary="投递了 1/1 个岗位",
        details=[
            ActionDetail(
                target="AIGC视觉生成实习",
                status="succeeded",
                url="https://www.zhipin.com/job_detail/xxx.html",
                extras={"company": "某公司", "match_score": 0.82},
            )
        ],
        metrics={"attempted": 1, "succeeded": 1, "failed": 0},
        next_steps=("HR 回复会自动推送",),
        evidence={"source": "boss_mcp_browser_scan"},
    )

    back = ActionReport.from_dict(report.to_dict())
    assert back == report


def test_action_report_to_dict_omits_empty_collections() -> None:
    report = ActionReport.build(
        action="system.login",
        summary="登录成功",
        details=[ActionDetail(target="boss", status="succeeded")],
    )
    payload = report.to_dict()
    # 默认空集合不应污染 JSON, 这样 prompt / audit 不会被噪声淹没.
    assert "metrics" not in payload
    assert "next_steps" not in payload
    assert "evidence" not in payload


# ──────────────────────────────────────────────────────────────
# 4. to_prompt_lines — LLM-facing 渲染
# ──────────────────────────────────────────────────────────────


def test_prompt_lines_contain_core_facts_for_llm_grounding() -> None:
    report = ActionReport.build(
        action="job.greet",
        summary="投递了 1/1 个合适岗位",
        details=[
            ActionDetail(
                target="AIGC视觉生成实习(Agent方向)",
                status="succeeded",
                url="https://www.zhipin.com/job_detail/xxx.html",
            )
        ],
        metrics={"attempted": 1, "succeeded": 1, "failed": 0},
        evidence={"trace_id": "trace_34682759d5e7"},
    )
    text = "\n".join(report.to_prompt_lines())

    # LLM grounding 的四个关键事实必须能通过子串定位到 (不经过 json):
    assert "Action Report" in text
    assert "action: job.greet" in text
    assert "status: succeeded" in text
    assert "AIGC视觉生成实习(Agent方向)" in text
    assert "succeeded=1" in text
    assert "trace_34682759d5e7" in text
    assert "MUST be grounded in this report" in text


def test_prompt_lines_renders_failure_reason_and_url() -> None:
    report = ActionReport.build(
        action="job.greet",
        summary="0/1",
        details=[
            ActionDetail(
                target="后端实习",
                status="failed",
                reason="已联系过, 触发去重",
                url="https://...",
            )
        ],
    )
    text = "\n".join(report.to_prompt_lines())

    assert "[failed] 后端实习" in text
    assert "已联系过, 触发去重" in text


def test_prompt_lines_renders_primitive_extras_to_llm() -> None:
    """extras 里 primitive 业务字段必须能流到 LLM prompt.

    trace_fe19c3ab1e43 薪资缺失的直接回归点 — 当时 salary 进了
    ActionDetail.extras 但 to_prompt_lines 不渲染 extras, LLM 拿
    不到业务字段, 在 reply 里就漏报薪资.

    这里**只断言业务字段到 prompt 的传输契约**(salary 出现了), 不锁
    具体排序 / 格式 / 上限 — 那些是本文件外的实现细节, 一起锁等于用
    test 复读 code.
    """
    report = ActionReport.build(
        action="job.greet",
        summary="已投递 1 个岗位",
        details=[
            ActionDetail(
                target="后端开发实习",
                status="succeeded",
                url="https://example.com/job/1",
                extras={"company": "字节跳动", "salary": "590-600元/天"},
            )
        ],
    )
    text = "\n".join(report.to_prompt_lines())

    assert "后端开发实习" in text
    assert "字节跳动" in text
    assert "590-600元/天" in text


def test_prompt_lines_sanitizes_pua_private_use_area_runs() -> None:
    """招聘平台反爬的字体加密会塞 Private Use Area (U+E000..U+F8FF)
    私有码点 (例: salary=``\\ue035\\ue039\\ue031-\\ue036\\ue031\\ue031``).

    如果原样喂给 LLM, 会被当作占位符默默吞掉/乱写, 对用户就是"没有
    薪资". ActionReport 层的合约: 所有流向 prompt 的字符串都要过
    sanitize, 把连续 PUA 段替换成明示的 ``«encoded»`` marker, 这样
    LLM 会诚实告诉用户"该字段被平台加密".
    """
    encoded_salary = "\ue035\ue039\ue031-\ue036\ue031\ue031元/天"
    report = ActionReport.build(
        action="job.greet",
        summary=f"已投递 {encoded_salary}",  # summary 也可能被污染
        details=[
            ActionDetail(
                target="后端实习",
                status="succeeded",
                extras={"salary": encoded_salary},
            )
        ],
    )
    text = "\n".join(report.to_prompt_lines())

    # PUA 码点不得原样出现在 prompt 里 (LLM/日志都不应看到它)
    assert "\ue035" not in text
    assert "\ue031" not in text
    # marker 必须可见; 非 PUA 的 "元/天" 不受影响
    assert "«encoded»" in text
    assert "元/天" in text


def test_prompt_lines_extras_silently_drops_nested_payload() -> None:
    """extras 里的 dict / list 必须**静默丢弃**, 不能把嵌套 payload 泄
    漏进 LLM prompt — 否则外部 MCP 返回的任意脏 obs 可以污染 LLM 上下文.

    真实防线: LLM prompt 的可信边界; 不测"primitive → str 怎么格式化"
    (那是实现细节), 只测"非 primitive 不得穿透".
    """
    report = ActionReport.build(
        action="job.greet",
        summary="x",
        details=[
            ActionDetail(
                target="A",
                status="succeeded",
                extras={
                    "company": "字节",
                    "nested": {"leak": "sensitive"},
                    "listy": ["also_leak"],
                },
            )
        ],
    )
    text = "\n".join(report.to_prompt_lines())

    assert "字节" in text
    assert "sensitive" not in text
    assert "also_leak" not in text


# ──────────────────────────────────────────────────────────────
# 5. to_receipt_facts — Verifier-facing 投影
# ──────────────────────────────────────────────────────────────


def test_receipt_facts_expose_action_status_and_numeric_metrics() -> None:
    report = ActionReport.build(
        action="job.greet",
        summary="x",
        details=[ActionDetail(target="A", status="succeeded")],
        metrics={"attempted": 1, "succeeded": 1, "ratio": 1.0, "note": "drop_me"},
    )
    facts = report.to_receipt_facts()

    assert facts["action"] == "job.greet"
    assert facts["status"] == "succeeded"
    assert facts["attempted"] == 1
    assert facts["succeeded"] == 1
    assert facts["ratio"] == 1.0
    # 非数值字段不进入 Receipt.extracted_facts, 避免污染 judge 判据.
    assert "note" not in facts


# ──────────────────────────────────────────────────────────────
# 6. extract_action_report — 三形态兼容
# ──────────────────────────────────────────────────────────────


def _sample_report() -> ActionReport:
    return ActionReport.build(
        action="job.greet",
        summary="投递了 1/1",
        details=[ActionDetail(target="A", status="succeeded")],
        metrics={"succeeded": 1},
    )


def test_extract_returns_none_for_non_dict_observation() -> None:
    assert extract_action_report("not a dict") is None
    assert extract_action_report(None) is None
    assert extract_action_report(123) is None


def test_extract_returns_none_when_key_missing() -> None:
    assert extract_action_report({"ok": True, "greeted": 1}) is None


def test_extract_when_observation_carries_action_report_instance() -> None:
    report = _sample_report()
    obs = {"ok": True, "greeted": 1, ACTION_REPORT_KEY: report}

    got = extract_action_report(obs)
    assert got is report  # 原对象直通, 不做额外序列化.


def test_extract_when_observation_carries_action_report_dict() -> None:
    report = _sample_report()
    obs = {"ok": True, "greeted": 1, ACTION_REPORT_KEY: report.to_dict()}

    got = extract_action_report(obs)
    assert got is not None
    assert got == report


def test_extract_returns_none_for_malformed_dict_without_raising() -> None:
    # observation 来自外部 MCP 进程时可能是损坏的 JSON; 抽取函数必须静默失败
    # (上游走 fallback 路径), 不得把一个不完整的 dict 当好报告用.
    obs = {ACTION_REPORT_KEY: {"action": "x", "status": "NOT_A_VALID_STATUS"}}
    assert extract_action_report(obs) is None


def test_extract_when_observation_itself_is_action_report() -> None:
    report = _sample_report()
    assert extract_action_report(report) is report


# ──────────────────────────────────────────────────────────────
# 7. 通用性: 同一 schema 跨 module 场景
# ──────────────────────────────────────────────────────────────


def test_universal_shape_supports_job_greet_checkin_trip_login() -> None:
    """契约通用性 smoke: 四个异构 module 都能用同一 dataclass 表达, 不需
    要业务特判 / schema 分叉. 这是 ADR-002 的通用性论证的代码锚点.
    """

    # job.greet — 投递岗位
    job_greet = ActionReport.build(
        action="job.greet",
        summary="投递了 1/1",
        details=[ActionDetail(target="岗位A", status="succeeded", url="https://...")],
        metrics={"attempted": 1, "succeeded": 1, "failed": 0},
    )

    # game.checkin — 游戏签到 (部分已签过)
    game_checkin = ActionReport.build(
        action="game.checkin",
        summary="签到 1 成功, 1 已签过",
        details=[
            ActionDetail(target="原神", status="succeeded", extras={"reward": "60 原石"}),
            ActionDetail(target="绝区零", status="skipped", reason="今日已签"),
        ],
        metrics={"attempted": 2, "success": 1, "already_done": 1},
    )

    # trip.plan — 行程规划 (没有成功/失败, 仅生成预览)
    trip_plan = ActionReport.build(
        action="trip.plan",
        summary="生成了 3 天成都行程草案",
        status="preview",  # 此类"非 mutating, 但用户需要知道报告"也走同一层
        details=[
            ActionDetail(target="D1 (机场 → 春熙路)", status="succeeded"),
            ActionDetail(target="D2 (大熊猫基地)", status="succeeded"),
            ActionDetail(target="D3 (都江堰)", status="succeeded"),
        ],
        metrics={"days_planned": 3, "est_budget_cny": 2400},
        next_steps=("确认后我可以帮你订酒店",),
    )

    # system.login — 登录 (单步 action, 没有多 item)
    system_login = ActionReport.build(
        action="system.login",
        summary="已登录 BOSS 直聘",
        details=[ActionDetail(target="zhipin.com", status="succeeded")],
        metrics={"attempted": 1, "succeeded": 1},
    )

    # 都能 roundtrip, 都能渲染, 都能投射到 receipt facts —
    # 就是"通用抽象"的定义.
    for report, expected_action in (
        (job_greet, "job.greet"),
        (game_checkin, "game.checkin"),
        (trip_plan, "trip.plan"),
        (system_login, "system.login"),
    ):
        assert ActionReport.from_dict(report.to_dict()) == report
        facts = report.to_receipt_facts()
        assert facts["action"] == expected_action
        assert facts["status"] == report.status
        lines = report.to_prompt_lines()
        assert any(report.summary in line for line in lines)
