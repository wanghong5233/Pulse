from __future__ import annotations

from typing import Any

from ..core.tool import tool
from ..core.tools.web_search import search_web
from ._helpers import safe_int


@tool(
    name="web.search",
    description="Search the public web and return top N (title, url, snippet) results.",
    when_to_use=(
        "对**公共互联网**做全文检索, 返回 top N (title, url, snippet)。"
        "query 应为一个完整语义短句 (非关键词拼接), 按最相关返回。"
        "用于所有**已接入 domain 覆盖不到**的泛信息查询。"
    ),
    when_not_to_use=(
        "已有 domain 专用通道的场景**必须**走对应工具, 不得用 web.search 绕过:\n"
        "- 招聘 / 岗位 / 投递 / HR 沟通 → `job.*` (greet/chat/profile/hard_constraint/resume);\n"
        "- 本地记忆 / 偏好 / 画像 / 历史对话 → `memory_read` / `memory_update` / `memory_search` / `memory_archive`;\n"
        "- 天气 → `weather.current`; 航班 → `flight.search`; 提醒 → `alarm.create`;\n"
        "- 任何**真实副作用**动作 (发消息 / 投简历 / 改偏好等) → 对应 domain 工具, web.search 只读取不执行。"
    ),
    ring="ring1_builtin",
    schema={
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 12},
        },
    },
)
def web_search_tool(args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ValueError("query is required")
    max_results = safe_int(args.get("max_results"), 5, min_value=1, max_value=12)
    rows = search_web(query, max_results=max_results)
    return {
        "query": query,
        "total": len(rows),
        "items": [
            {"title": item.title, "url": item.url, "snippet": item.snippet}
            for item in rows
        ],
    }
