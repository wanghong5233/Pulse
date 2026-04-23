"""Job domain skill schema.

This file declares the *domain-level* capability surface for the job-search
domain.  It is consumed by the Brain / router to do two things:

  1. Understand what the domain can do without reading every submodule.
  2. Drive a two-level routing flow (domain → subcapability).

The schema is intentionally structured (dict, JSON-Schema-like) rather than
a free-form ``SKILL.md`` so that both humans and the LLM can rely on the
same source of truth.
"""

from __future__ import annotations

from typing import Any

SKILL_SCHEMA: dict[str, Any] = {
    "name": "job",
    "description": (
        "自动化求职技能包：覆盖岗位扫描、岗位匹配、打招呼、HR 对话处理、"
        "以及后续的申请追踪。面向 BOSS 直聘等招聘平台，支持多平台扩展。"
    ),
    "subcapabilities": [
        {
            "name": "greet",
            "module": "job_greet",
            "description": "搜索岗位并根据匹配度发送打招呼消息",
            "intents": ["job.scan", "job.greet.trigger"],
            "examples": [
                "帮我搜一下 AI Agent 实习岗位",
                "给匹配岗位自动打招呼",
                "/scan AI Agent 实习",
                "/greet Python 后端",
            ],
        },
        {
            "name": "chat",
            "module": "job_chat",
            "description": "处理 HR 招聘对话：拉取未读、规划回复动作、执行回复",
            "intents": ["job.chat.pull", "job.chat.process", "job.chat.execute"],
            "examples": [
                "看看 HR 有没有新消息",
                "帮我处理招聘对话",
                "/chat pull",
                "/chat process",
            ],
        },
        {
            "name": "profile",
            "module": "job_profile",
            "description": (
                "管理求职 domain memory：硬约束（城市/薪资下限/目标岗位/经验）、"
                "自然语言记忆项（回避公司/偏好/申请事件等）、简历原文与解析缓存。"
            ),
            "intents": [
                "job.memory.record",
                "job.memory.retire",
                "job.memory.supersede",
                "job.memory.list",
                "job.hard_constraint.set",
                "job.hard_constraint.unset",
                "job.resume.update",
                "job.resume.patch_parsed",
                "job.resume.get",
                "job.snapshot.get",
            ],
            "examples": [
                "目前我投递过字节，简历被锁，暂时不要投字节",
                "我不要投拼多多，笔试挂过",
                "薪资下限调到 25k",
                "意向城市改成上海",
                "更新我的简历",
                "看一下我的求职画像",
            ],
        },
    ],
    "platforms": ["boss"],
}
