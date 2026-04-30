"""One-shot runtime fix: persist the user's "放弃大厂 / 找小厂或初创"
preferences into JobMemory via the public REST API.

Used after the 2026-04-28 post-mortem (trace_753fecf70cc5) to recover the
runtime state that the LLM extractor failed to capture during the original
turn. Safe to re-run — JobProfileService.record_item upserts by content.
"""
from __future__ import annotations

import json
import sys
from urllib import request

BASE = "http://127.0.0.1:8010/api/modules/job/profile/memory/record"

PAYLOADS = [
    {
        "type": "avoid_trait",
        "target": "大厂",
        "content": "暂时战略性放弃大厂暑期实习, 想先积累小厂/初创的深度垂直实习经验, 秋招再冲大厂",
        "raw_text": "我目前实力不够暂时战略性放弃大厂暑期实习",
    },
    {
        "type": "favor_trait",
        "target": "小厂或初创",
        "content": "想积累一段小厂或初创公司的深度垂直 Agent 实习, 业务垂直匹配, 大模型应用前沿",
        "raw_text": "我希望积累一段小厂或者初创的深度垂直实习Agent机会",
    },
]


def main() -> int:
    for payload in PAYLOADS:
        body = json.dumps(payload).encode("utf-8")
        req = request.Request(
            BASE, data=body, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with request.urlopen(req, timeout=15) as resp:
            print(json.dumps(json.loads(resp.read()), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
