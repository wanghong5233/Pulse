"""Intel domain skill schema.

Declares the *domain-level* capability surface for the intelligence domain.
After the 2026 refactor the domain has a single capability — `digest` — that
runs the deterministic six-stage workflow per declared topic, plus a
search tool for ad-hoc retrieval over the persisted corpus.
"""

from __future__ import annotations

from typing import Any

SKILL_SCHEMA: dict[str, Any] = {
    "name": "intel",
    "description": (
        "情报域技能包：以确定性 workflow 订阅多渠道信号，按主题去重 / 评分 / "
        "摘要，产出日报推送并把高分条目沉淀到长期记忆。新增主题只需追加一个 "
        "YAML 文件，不改代码。"
    ),
    "subcapabilities": [
        {
            "name": "digest",
            "module": "intel",
            "description": (
                "按主题运行 fetch → dedup → score → summarize → diversify → "
                "publish 六步 workflow，列出 / 取最新 / 立即触发。"
            ),
            "intents": [
                "intel.digest.list",
                "intel.digest.latest",
                "intel.digest.run",
            ],
            "examples": [
                "看一下最新的大模型前沿情报",
                "立刻跑一遍秋招主题",
                "/intel digest list",
            ],
        },
        {
            "name": "search",
            "module": "intel",
            "description": "跨主题关键词检索已落库的 intel 文档，供 Brain ReAct 引用。",
            "intents": ["intel.search"],
            "examples": [
                "你之前看到的那篇 MCP 安全治理文章是哪一篇？",
                "/intel search agent observability",
            ],
        },
    ],
}
