"""Intel domain skill schema.

Declares the *domain-level* capability surface for the intelligence domain
(面经 / 技术雷达 / 知识查询).  Consumed by the Brain / router to perform
two-level routing (domain → sub-capability).
"""

from __future__ import annotations

from typing import Any

SKILL_SCHEMA: dict[str, Any] = {
    "name": "intel",
    "description": (
        "情报域技能包：面向求职者与技术工程师的长期知识基建，覆盖面经收集、"
        "技术雷达信号与本地知识库语义检索。"
    ),
    "subcapabilities": [
        {
            "name": "interview",
            "module": "intel_interview",
            "description": "从 Web 搜索结果中抓取并结构化面经情报，可定时推送日报",
            "intents": [
                "intel.interview.collect",
                "intel.interview.report",
            ],
            "examples": [
                "收集 AI Agent 的最新面经",
                "给我一份 RAG 实习岗位的面经日报",
                "/intel interview collect AI Agent",
            ],
        },
        {
            "name": "techradar",
            "module": "intel_techradar",
            "description": "收集技术趋势信号并生成信号评分 + 行动建议",
            "intents": [
                "intel.techradar.collect",
                "intel.techradar.report",
            ],
            "examples": [
                "帮我扫一下 MCP 相关的技术雷达",
                "/intel radar collect Agent Observability",
            ],
        },
        {
            "name": "query",
            "module": "intel_query",
            "description": "对已收集的情报做语义检索 / 分类过滤",
            "intents": [
                "intel.query.search",
            ],
            "examples": [
                "查一下 Claude Code 对 MCP 的安全治理怎么做",
                "/intel query agent 超时",
            ],
        },
    ],
}
