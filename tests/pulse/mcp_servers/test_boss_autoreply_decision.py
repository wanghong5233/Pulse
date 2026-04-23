"""Pure-function guards for the auto-reply decision tree (ADR-004 §4.2).

Scope (宪法 §测试宪法):
    Every fixture `ChatDetailState` below is rebuilt from a **real** DOM dump
    observation already codified in
    ``docs/dom-specs/boss/chat-detail/README.md`` §C (baseline dump
    ``20260422T073442Z.json``). We pin exactly one thing per case — the
    ``AutoReplyDecision.kind`` returned by the rule engine — and we do not
    re-state the implementation's internal message wording.

    If the rule tree ever needs to grow (e.g. a new HR action-card variant
    from §H's未采样 list), a new case MUST cite the newly collected DOM
    dump path; synthetic speculation is forbidden.
"""

from __future__ import annotations

import pytest

from pulse.mcp_servers._boss_platform_runtime import (
    ChatDetailLastMessage,
    ChatDetailPendingRespond,
    ChatDetailState,
    decide_auto_reply_action,
)


def _build_state(
    *,
    pending: ChatDetailPendingRespond | None = None,
    last: ChatDetailLastMessage | None = None,
    send_resume_present: bool = True,
) -> ChatDetailState:
    return ChatDetailState(
        hr_name="刘女士",
        hr_company="字节跳动",
        hr_title="招聘者",
        position_name="AIGC视觉生成实习生（Agent方向）",
        position_salary="240-260元/天",
        position_city="上海",
        last_message=last,
        pending_respond=pending,
        send_resume_button_present=send_resume_present,
    )


class TestDecideAutoReplyAction:
    """Each case anchors ONE decision branch with a real-world observation."""

    def test_case_01_agree_when_resume_request_popover(self) -> None:
        """Dump ``20260422T073442Z.json``: §C.4 浮动条 + §C.3 卡片.

        真实观察: ``.respond-popover .text = '我想要一份您的附件简历，您是否同意'``
        + ``.btn-agree`` + ``.btn-refuse`` 两按钮俱全. 求职默认动作 → agree.
        """
        state = _build_state(
            pending=ChatDetailPendingRespond(
                text="我想要一份您的附件简历，您是否同意",
                has_agree=True,
                has_refuse=True,
            ),
            last=ChatDetailLastMessage(
                sender="friend",
                kind="card",
                text="我想要一份您的附件简历，您是否同意",
                data_mid="334613509312424",
            ),
        )

        decision = decide_auto_reply_action(state)

        assert decision.kind == "click_respond_agree"
        assert decision.trigger_mid == "334613509312424"

    def test_case_02_skip_when_popover_text_not_resume(self) -> None:
        """§H 未采样条目: 其他类型动作卡 (面试邀请/换电话/换微信) 的处理.

        合同明示 ``.dialog-icon`` 二级 class (``.resume`` / 等) 需随采样扩充.
        在合同补齐之前, 保守 SKIP, 让用户人肉处理并回补 dump.
        """
        state = _build_state(
            pending=ChatDetailPendingRespond(
                text="HR 想和你交换微信，是否同意",
                has_agree=True,
                has_refuse=True,
            ),
            last=ChatDetailLastMessage(
                sender="friend",
                kind="card",
                text="HR 想和你交换微信，是否同意",
                data_mid="334613509312999",
            ),
        )

        decision = decide_auto_reply_action(state)

        assert decision.kind == "skip"
        assert "未识别的动作卡" in decision.reason

    def test_case_03_skip_when_my_side_last(self) -> None:
        """Dump ``20260422T073442Z.json`` §C.1: ``data-mid=334613440820744``,
        ``item-myself``, text "您好，27 应届硕士，可尽快到岗...".

        我方已回, 再扫到同一个会话 → 无动作.
        """
        state = _build_state(
            last=ChatDetailLastMessage(
                sender="me",
                kind="text",
                text="您好，27 应届硕士，可尽快到岗，有多段 Agent 项目经验",
                data_mid="334613440820744",
            ),
        )

        decision = decide_auto_reply_action(state)

        assert decision.kind == "skip"
        assert "我方" in decision.reason

    def test_case_04_skip_when_bot_card_last(self) -> None:
        """Dump ``20260422T073442Z.json`` §C.2: 机器人 PK 卡 (``.blue``).

        真实观察: 卡片标题 "你与该职位竞争者PK情况". extract_chat_detail_state
        将此 li 归为 ``sender='bot', kind='card'``.
        """
        state = _build_state(
            last=ChatDetailLastMessage(
                sender="bot",
                kind="card",
                text="你与该职位竞争者PK情况",
                data_mid="334613500000000",
            ),
        )

        decision = decide_auto_reply_action(state)

        assert decision.kind == "skip"
        assert "机器人" in decision.reason

    def test_case_05_send_resume_when_friend_text_and_button_visible(self) -> None:
        """Dump ``20260422T073442Z.json`` §C.1: HR 纯文字消息, 无动作卡.

        真实观察: text "你好呀！方便发一份你的简历过来嘛", ``item-friend``,
        data-mid 后端生成. 底部工具栏 "发简历" 按钮在样本里存在.
        """
        state = _build_state(
            last=ChatDetailLastMessage(
                sender="friend",
                kind="text",
                text="你好呀！方便发一份你的简历过来嘛",
                data_mid="334613500111222",
            ),
            send_resume_present=True,
        )

        decision = decide_auto_reply_action(state)

        assert decision.kind == "click_send_resume"
        assert decision.trigger_mid == "334613500111222"

    def test_case_06_skip_when_friend_text_but_no_send_button(self) -> None:
        """§D.1 "发简历"按钮是文本定位, 不保证任何会话都有.

        未来如 BOSS 对部分账号隐藏该按钮, 我们不能盲目点其他按钮凑活.
        """
        state = _build_state(
            last=ChatDetailLastMessage(
                sender="friend",
                kind="text",
                text="你好呀！方便发一份你的简历过来嘛",
                data_mid="334613500111333",
            ),
            send_resume_present=False,
        )

        decision = decide_auto_reply_action(state)

        assert decision.kind == "skip"
        assert "发简历" in decision.reason

    def test_case_07_skip_when_state_is_none(self) -> None:
        """extract_chat_detail_state 返回 None 的唯一条件: ``.chat-conversation`` 未渲染.

        合同 §G 明示. 这时编排层不应该继续任何操作.
        """
        decision = decide_auto_reply_action(None)

        assert decision.kind == "skip"
        assert "未渲染" in decision.reason

    def test_case_08_skip_when_messages_empty(self) -> None:
        """边界: 会话被打开但消息流为空 (刚建立未互发).

        规则树需要显式兜底, 避免 NoneType 派生决策.
        """
        state = _build_state(last=None)

        decision = decide_auto_reply_action(state)

        assert decision.kind == "skip"
        assert "消息流" in decision.reason

    @pytest.mark.parametrize(
        "popover_text",
        [
            "我想要一份您的附件简历，您是否同意",
            "是否同意发送您的简历？",
        ],
    )
    def test_agree_branch_recognizes_resume_keyword(self, popover_text: str) -> None:
        """Rule is anchored on 关键字 '简历' + has_agree; guarding against
        future wording variance in BOSS UI (observed variant is case 01).
        """
        state = _build_state(
            pending=ChatDetailPendingRespond(
                text=popover_text,
                has_agree=True,
                has_refuse=True,
            ),
            last=ChatDetailLastMessage(
                sender="friend",
                kind="card",
                text=popover_text,
                data_mid="334613509312425",
            ),
        )

        decision = decide_auto_reply_action(state)

        assert decision.kind == "click_respond_agree"
