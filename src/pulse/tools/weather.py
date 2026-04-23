from __future__ import annotations

import os
import urllib.parse
from datetime import datetime, timezone
from typing import Any

from ..core.tool import tool
from ._helpers import http_get_json, safe_float


def _weather_code_text(code: int) -> str:
    mapping = {
        0: "Clear", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
        45: "Fog", 48: "Rime fog",
        51: "Light drizzle", 53: "Drizzle", 55: "Dense drizzle",
        56: "Freezing drizzle", 57: "Dense freezing drizzle",
        61: "Slight rain", 63: "Rain", 65: "Heavy rain",
        66: "Freezing rain", 67: "Heavy freezing rain",
        71: "Slight snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains",
        80: "Rain showers", 81: "Heavy rain showers", 82: "Violent rain showers",
        85: "Snow showers", 86: "Heavy snow showers",
        95: "Thunderstorm", 96: "Thunderstorm with hail", 99: "Heavy thunderstorm with hail",
    }
    return mapping.get(int(code), f"WeatherCode({int(code)})")


@tool(
    name="weather.current",
    description="Get current weather for a city via Open-Meteo (geocode + forecast).",
    when_to_use=(
        "输入一个城市名 (location, 中英文均可), 返回该城市**当前**实况: "
        "温度 / 湿度 / weather_code / 语义 condition。内部先做 geocoding 再查 current 点, "
        "无本地状态副作用。"
    ),
    when_not_to_use=(
        "能力边界外: 1) 未来多日 / 逐小时预报 (API 只返回 current 单点); "
        "2) 历史天气回查; "
        "3) location 字段为空或无法地理编码时 API 返回 ok=false, 调用方不得据此编造天气。"
    ),
    ring="ring1_builtin",
    schema={
        "type": "object",
        "properties": {
            "location": {"type": "string"},
        },
    },
)
def weather_current(args: dict[str, Any]) -> dict[str, Any]:
    location = str(args.get("location") or "Beijing").strip() or "Beijing"
    timeout_sec = safe_float(os.getenv("PULSE_WEATHER_TIMEOUT_SEC", "8"), 8.0, min_value=2.0, max_value=20.0)
    encoded = urllib.parse.quote(location)
    geocode_url = (
        "https://geocoding-api.open-meteo.com/v1/search"
        f"?name={encoded}&count=1&language=en&format=json"
    )
    try:
        geocode_payload = http_get_json(geocode_url, timeout_sec=timeout_sec)
        results = geocode_payload.get("results") if isinstance(geocode_payload, dict) else None
        if not isinstance(results, list) or not results:
            return {
                "ok": False, "location": location,
                "error": "location not found by geocoding service",
                "source": "open-meteo",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        first = results[0] if isinstance(results[0], dict) else {}
        latitude = float(first.get("latitude"))
        longitude = float(first.get("longitude"))
        resolved_name = str(first.get("name") or location).strip() or location
        country = str(first.get("country") or "").strip()
        admin1 = str(first.get("admin1") or "").strip()
        resolved_parts = [item for item in [resolved_name, admin1, country] if item]
        resolved_location = ", ".join(resolved_parts) if resolved_parts else resolved_name
        forecast_url = (
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={latitude}&longitude={longitude}"
            "&current=temperature_2m,relative_humidity_2m,weather_code"
            "&timezone=auto"
        )
        forecast_payload = http_get_json(forecast_url, timeout_sec=timeout_sec)
        current = forecast_payload.get("current") if isinstance(forecast_payload, dict) else None
        if not isinstance(current, dict):
            return {
                "ok": False, "location": resolved_location,
                "error": "missing current weather payload",
                "source": "open-meteo",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        weather_code = int(current.get("weather_code", 0))
        return {
            "ok": True,
            "location": resolved_location,
            "latitude": latitude,
            "longitude": longitude,
            "condition": _weather_code_text(weather_code),
            "weather_code": weather_code,
            "temperature_c": float(current.get("temperature_2m", 0.0)),
            "humidity": float(current.get("relative_humidity_2m", 0.0)),
            "source": "open-meteo",
            "timestamp": str(current.get("time") or datetime.now(timezone.utc).isoformat()),
        }
    except Exception as exc:
        return {
            "ok": False, "location": location,
            "error": str(exc)[:300],
            "source": "open-meteo",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
