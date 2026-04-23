from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from ..core.tool import tool
from ._helpers import safe_int


@tool(
    name="alarm.create",
    description="Create a local alarm reminder (relative minutes from now, 1~1440).",
    when_to_use=(
        "以当前 UTC 为基准, N 分钟 (1 ≤ N ≤ 1440) 后在本地触发一条提醒。"
        "参数要求: minutes (整数分钟) + message (提醒文本), 两者缺一报错。"
        "副作用: 仅本进程 / 本机, 不跨设备, 不发送到任何外部 channel。"
    ),
    when_not_to_use=(
        "能力边界外: 1) 绝对时间点 (API 不接受 ISO 时间戳, 只接受 minutes 增量); "
        "2) 周期性 / cron 语义的提醒 (一次性触发); "
        "3) 需要跨设备或云端推送的场景 (无持久化、无 channel 路由)。"
    ),
    ring="ring1_builtin",
    schema={
        "type": "object",
        "properties": {
            "minutes": {"type": "integer", "minimum": 1, "maximum": 1440},
            "message": {"type": "string"},
        },
    },
)
def alarm_create(args: dict[str, Any]) -> dict[str, Any]:
    minutes = safe_int(args.get("minutes"), 10, min_value=1, max_value=1440)
    message = str(args.get("message") or "Reminder").strip() or "Reminder"
    now = datetime.now(timezone.utc)
    run_at = now + timedelta(minutes=minutes)
    return {
        "ok": True,
        "minutes": minutes,
        "message": message,
        "created_at": now.isoformat(),
        "run_at": run_at.isoformat(),
    }
