from __future__ import annotations

import os
import urllib.parse
from typing import Any

from ..core.tool import tool
from ._helpers import http_get_json, safe_float, safe_int


@tool(
    name="flight.search",
    description="Search flights via externally configured provider (requires PULSE_FLIGHT_SEARCH_BASE_URL).",
    when_to_use=(
        "查询**民航航班**售票信息 (依赖外部 provider, 需 PULSE_FLIGHT_SEARCH_BASE_URL)。"
        "query 应包含起点 / 终点 / 日期三要素; 要素缺失时由调用方先澄清, 不填默认值。"
        "返回 items 数组为 provider 原样透传, 不做本地 rerank。"
    ),
    when_not_to_use=(
        "能力边界外: 1) 仅涵盖航空, 不支持高铁 / 火车 / 大巴等陆面交通; "
        "2) 未配置 PULSE_FLIGHT_SEARCH_BASE_URL 时返回 ok=false, 调用方不得据此伪造航班号; "
        "3) 无法单独做价格预测 / 经济性建议 (provider 不提供这类语义)。"
    ),
    ring="ring1_builtin",
    schema={
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 8},
        },
    },
)
def flight_search(args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query") or "").strip() or "Beijing -> Shanghai"
    max_results = safe_int(args.get("max_results"), 3, min_value=1, max_value=8)
    base_url = str(os.getenv("PULSE_FLIGHT_SEARCH_BASE_URL", "")).strip().rstrip("/")
    auth_token = str(os.getenv("PULSE_FLIGHT_SEARCH_TOKEN", "")).strip()
    timeout_sec = safe_float(
        os.getenv("PULSE_FLIGHT_SEARCH_TIMEOUT_SEC", "8"), 8.0, min_value=2.0, max_value=20.0,
    )
    if not base_url:
        return {
            "ok": False, "query": query, "total": 0, "items": [],
            "source": "external_api",
            "error": "PULSE_FLIGHT_SEARCH_BASE_URL is not configured",
        }
    params = urllib.parse.urlencode({"query": query, "max_results": str(max_results)})
    url = f"{base_url}?{params}" if "?" not in base_url else f"{base_url}&{params}"
    headers: dict[str, str] = {}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"
    try:
        payload = http_get_json(url, timeout_sec=timeout_sec, headers=headers)
        if isinstance(payload, list):
            items = payload
        elif isinstance(payload, dict):
            items_raw = payload.get("items")
            items = items_raw if isinstance(items_raw, list) else []
        else:
            items = []
        safe_items = [item for item in items if isinstance(item, dict)][:max_results]
        return {
            "ok": True, "query": query, "total": len(safe_items),
            "items": safe_items, "source": "external_api",
        }
    except Exception as exc:
        return {
            "ok": False, "query": query, "total": 0, "items": [],
            "source": "external_api", "error": str(exc)[:300],
        }
