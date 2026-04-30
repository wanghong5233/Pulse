from __future__ import annotations

from types import SimpleNamespace

from pulse.core.server import (
    _patch_job_chat_manual_links,
    _patch_job_greet_detail_links,
    _synthesize_reply_from_brain_result,
)


def test_synthesize_reply_prefers_action_report_summary() -> None:
    brain_result = SimpleNamespace(
        steps=[
            SimpleNamespace(
                action_report={
                    "action": "job.greet.trigger",
                    "status": "succeeded",
                    "summary": "已完成 5 个岗位投递",
                    "metrics": {"succeeded": 5, "failed": 0},
                }
            )
        ],
        used_tools=["job.greet.trigger"],
    )

    reply = _synthesize_reply_from_brain_result(brain_result)
    assert "已完成 5 个岗位投递" in reply
    assert "succeeded=5" in reply


def test_synthesize_reply_falls_back_to_used_tools_when_no_report() -> None:
    brain_result = SimpleNamespace(
        steps=[SimpleNamespace(action_report=None)],
        used_tools=["job.greet.trigger"],
    )

    reply = _synthesize_reply_from_brain_result(brain_result)
    assert "job.greet.trigger" in reply


def test_synthesize_reply_returns_empty_when_no_action_evidence() -> None:
    brain_result = SimpleNamespace(steps=[], used_tools=[])
    assert _synthesize_reply_from_brain_result(brain_result) == ""


def test_patch_job_greet_detail_links_replaces_view_detail_placeholder() -> None:
    brain_result = SimpleNamespace(
        steps=[
            SimpleNamespace(
                action_report={
                    "action": "job.greet",
                    "details": [
                        {"target": "岗位A", "url": "https://www.zhipin.com/job_detail/a"},
                        {"target": "岗位B", "url": "https://www.zhipin.com/job_detail/b"},
                    ],
                }
            )
        ],
        used_tools=["job.greet.trigger"],
    )
    reply = (
        "我已经帮你投递了2个岗位：\n"
        "1. 岗位A，岗位详情页：查看详情\n"
        "2. 岗位B，岗位详情页：查看详情"
    )

    patched = _patch_job_greet_detail_links(reply, brain_result)
    assert "查看详情" not in patched
    assert "https://www.zhipin.com/job_detail/a" in patched
    assert "https://www.zhipin.com/job_detail/b" in patched


def test_patch_job_greet_detail_links_appends_when_no_placeholder() -> None:
    brain_result = SimpleNamespace(
        steps=[
            SimpleNamespace(
                action_report={
                    "action": "job.greet",
                    "details": [
                        {"target": "岗位A", "url": "https://www.zhipin.com/job_detail/a"},
                    ],
                }
            )
        ],
        used_tools=["job.greet.trigger"],
    )

    patched = _patch_job_greet_detail_links("我已经投递了1个岗位。", brain_result)
    assert "岗位详情链接：" in patched
    assert "https://www.zhipin.com/job_detail/a" in patched


def test_patch_job_greet_detail_links_wraps_existing_real_urls() -> None:
    brain_result = SimpleNamespace(
        steps=[
            SimpleNamespace(
                action_report={
                    "action": "job.greet",
                    "details": [
                        {"target": "岗位A", "url": "https://www.zhipin.com/job_detail/a"},
                    ],
                }
            )
        ],
        used_tools=["job.greet.trigger"],
    )
    original = "岗位详情页：https://www.zhipin.com/job_detail/a"
    patched = _patch_job_greet_detail_links(original, brain_result)
    assert patched == "岗位详情页：[查看职位](https://www.zhipin.com/job_detail/a)"


def test_patch_job_chat_manual_links_appends_failed_conversation_urls() -> None:
    brain_result = SimpleNamespace(
        steps=[
            SimpleNamespace(
                action_report={
                    "action": "job.chat",
                    "details": [
                        {
                            "target": "A",
                            "status": "failed",
                            "url": "https://www.zhipin.com/web/geek/chat?conversationId=abc",
                            "extras": {"manual_required": True},
                        },
                        {
                            "target": "B",
                            "status": "succeeded",
                            "url": "https://www.zhipin.com/web/geek/chat?conversationId=def",
                            "extras": {"manual_required": False},
                        },
                    ],
                }
            )
        ],
        used_tools=["job.chat.process_once"],
    )
    patched = _patch_job_chat_manual_links("已检查未读消息。", brain_result)
    assert "待你人工处理的会话链接：" in patched
    assert "[查看会话](https://www.zhipin.com/web/geek/chat?conversationId=abc)" in patched
    assert "conversationId=def" not in patched


def test_patch_job_chat_manual_links_wraps_existing_raw_url() -> None:
    brain_result = SimpleNamespace(
        steps=[
            SimpleNamespace(
                action_report={
                    "action": "job.chat",
                    "details": [
                        {
                            "target": "A",
                            "status": "failed",
                            "url": "https://www.zhipin.com/web/geek/chat?conversationId=abc",
                            "extras": {"manual_required": True},
                        },
                    ],
                }
            )
        ],
        used_tools=["job.chat.process_once"],
    )
    original = "这条需要你手动处理：https://www.zhipin.com/web/geek/chat?conversationId=abc"
    patched = _patch_job_chat_manual_links(original, brain_result)
    assert patched == "这条需要你手动处理：[查看会话](https://www.zhipin.com/web/geek/chat?conversationId=abc)"


def test_patch_job_chat_manual_links_appends_title_and_conversation_id_for_list_url() -> None:
    brain_result = SimpleNamespace(
        steps=[
            SimpleNamespace(
                action_report={
                    "action": "job.chat",
                    "details": [
                        {
                            "target": "潘女士 / SenseTime / HR",
                            "status": "failed",
                            "url": "https://www.zhipin.com/web/geek/chat",
                            "extras": {
                                "manual_required": True,
                                "conversation_id": "conv_123",
                                "card_title": "交换简历",
                            },
                        },
                    ],
                }
            )
        ],
        used_tools=["job.chat.process_once"],
    )
    patched = _patch_job_chat_manual_links("请你手动处理以下会话。", brain_result)
    assert "潘女士 / SenseTime / HR | 卡片: 交换简历" in patched
    assert "会话ID: conv_123" in patched
    assert "conversationId=conv_123" in patched
