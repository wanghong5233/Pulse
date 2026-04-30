"""Dry-run a real greet match through the production matcher LLM.

WHAT THIS DOES (and does NOT do):

  ✅ pulls real BOSS scan results via the live backend (same data the patrol sees)
  ✅ loads the real ``JobMemorySnapshot`` straight from PostgreSQL
     (same hard_constraints + memory items the matcher sees in production)
  ✅ instantiates the real ``LLMRouter`` and ``JobSnapshotMatcher``;
     the LLM call is a real OpenAI call with the actual ``job_match`` route
  ✅ captures and prints (a) the system+user prompt sent to the LLM,
     (b) the model selected for the route, (c) the raw LLM response,
     (d) the parsed ``MatchResult``

  ❌ does NOT send any greet message
  ❌ does NOT mutate JobMemory / actions log / any persistent state
  ❌ does NOT flip patrol enable state
  ❌ does NOT add a regression test or ship debug-only code paths
     into production. This is a one-shot diagnostic — read-only.

Usage (run inside WSL where backend env vars are set):

    /root/.venvs/pulse/bin/python3 scripts/_dryrun_match.py \\
        --keyword '大模型应用开发 agent' --n 3

Prereqs:
  * Backend is up at http://127.0.0.1:8010 (we use it only to fetch scan
    candidates, since browser-driven scan needs the live boss_mcp session).
  * ``PULSE_DATABASE_URL`` and OpenAI keys are set in the same env (i.e.
    ``source .env`` first if you run this manually).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib import request

# Make the in-tree ``pulse`` package importable when this script is invoked
# directly via the venv interpreter from outside an editable install.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

# 同步加载 .env 进 process env, 与 backend uvicorn 一致.
def _load_dotenv() -> None:
    env_path = _REPO_ROOT / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()

from pulse.core.llm.router import LLMRouter  # noqa: E402
from pulse.core.storage.engine import DatabaseEngine  # noqa: E402
from pulse.modules.job.greet.matcher import JobSnapshotMatcher  # noqa: E402
from pulse.modules.job.memory import JobMemory  # noqa: E402


# ─────────────────────────────────────────────────────────────
# Capturing wrapper around the real LLMRouter
# ─────────────────────────────────────────────────────────────


class CapturingLLMRouter:
    """Transparent proxy: forwards every call to the real ``LLMRouter`` while
    recording the messages we send and the raw response we receive.

    This is **not** a stub. Every call hits the same model the production
    matcher would hit — we just keep a transcript so we can show it to the
    operator afterwards.
    """

    def __init__(self, inner: LLMRouter) -> None:
        self._inner = inner
        self.calls: list[dict[str, Any]] = []

    def invoke_json(
        self,
        prompt_value: Any,
        *,
        route: str = "default",
        default: Any = None,
    ) -> Any:
        messages = list(prompt_value)
        record: dict[str, Any] = {
            "route": route,
            "model": self._inner.primary_model(route),
            "candidate_models": self._inner.candidate_models(route),
            "messages": [
                {
                    "role": type(m).__name__,
                    "content": str(getattr(m, "content", "") or ""),
                }
                for m in messages
            ],
        }
        try:
            raw = self._inner.invoke_text(prompt_value, route=route)
            record["raw_response"] = raw
            cleaned = self._inner.strip_code_fence(raw)
            try:
                parsed = json.loads(cleaned) if cleaned else default
            except json.JSONDecodeError as exc:
                record["parse_error"] = str(exc)
                parsed = default
            record["parsed"] = parsed
            self.calls.append(record)
            return parsed
        except Exception as exc:
            record["exception"] = f"{type(exc).__name__}: {exc}"
            self.calls.append(record)
            return default


# ─────────────────────────────────────────────────────────────
# Backend HTTP helpers (read-only)
# ─────────────────────────────────────────────────────────────


_BACKEND = os.getenv("PULSE_BACKEND_URL", "http://127.0.0.1:8010")


def _http_post(path: str, body: dict[str, Any], timeout: int = 60) -> dict[str, Any]:
    req = request.Request(
        f"{_BACKEND}{path}",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def fetch_scan_candidates(
    *, keyword: str, n: int, fetch_detail: bool,
) -> list[dict[str, Any]]:
    payload = {
        "keyword": keyword,
        "max_items": max(1, min(n, 10)),
        "max_pages": 1,
        "job_type": "all",
        "fetch_detail": fetch_detail,
    }
    print(f">>> POST /api/modules/job/greet/scan {payload}", file=sys.stderr)
    out = _http_post("/api/modules/job/greet/scan", payload, timeout=120)
    items = out.get("items") or out.get("result", {}).get("items") or []
    if not items and "matches" in out:
        items = out["matches"]
    return items[:n]


# Representative sample candidates (offline mode).
# Each item mirrors the shape that ``GreetService._normalize_scan_item`` produces.
# Mix: 3 大厂 (should hit avoid_trait if the matcher LLM does its job),
# 1 中型 / 1 早期初创 (should pass / favored). Used only when --sample is set.
_SAMPLE_CANDIDATES: list[dict[str, Any]] = [
    {
        "title": "AI Agent 研发实习生",
        "company": "字节跳动",
        "salary": "300-500元/天",
        "snippet": "负责豆包 / 抖音相关大模型 Agent 落地, 实习6个月以上, 北京/杭州.",
        "source_url": "https://example.local/sample/bytedance",
    },
    {
        "title": "大模型应用开发实习生",
        "company": "阿里云",
        "salary": "400元/天",
        "snippet": "通义千问应用层研发, 杭州西溪园区, 实习时间6个月+.",
        "source_url": "https://example.local/sample/aliyun",
    },
    {
        "title": "AI Agent 工程实习",
        "company": "腾讯",
        "salary": "300-400元/天",
        "snippet": "微信 / 元宝 Agent 方向, 上海/深圳, 一周4天起.",
        "source_url": "https://example.local/sample/tencent",
    },
    {
        "title": "LLM Agent 算法实习生",
        "company": "宜芯视界",
        "salary": "300元/天",
        "snippet": "早期 AI 创业公司, 做行业垂直 Agent, 上海, 直接对接 CTO.",
        "source_url": "https://example.local/sample/yixinshijie",
    },
    {
        "title": "大模型应用研发实习生 (Agent 方向)",
        "company": "某 AI 早期初创 (15 人)",
        "salary": "350元/天",
        "snippet": "AI Agent 工具链, 杭州, 团队主要是清北 + 大厂出来, A轮.",
        "source_url": "https://example.local/sample/early-startup",
    },
]


# ─────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────


def _truncate(text: str, limit: int = 1800) -> str:
    if len(text) <= limit:
        return text
    half = (limit - 50) // 2
    return text[:half] + f"\n...[truncated {len(text) - 2 * half} chars]...\n" + text[-half:]


def render_call(idx: int, job: dict[str, Any], call: dict[str, Any]) -> str:
    title = job.get("title") or "(no title)"
    company = job.get("company") or "(no company)"
    salary = job.get("salary") or "(not provided)"
    parts: list[str] = []
    parts.append(f"\n{'=' * 78}")
    parts.append(f"[Candidate {idx}]  {company} · {title}  (salary: {salary})")
    parts.append(f"  source_url: {job.get('source_url') or '(none)'}")
    parts.append(f"{'=' * 78}")
    parts.append(f"  route: {call.get('route')}")
    parts.append(f"  model: {call.get('model')}")
    parts.append(f"  candidate_models: {call.get('candidate_models')}")
    for m in call.get("messages") or []:
        parts.append(f"\n--- {m['role']} ---")
        parts.append(_truncate(m["content"], 2400))
    parts.append("\n--- raw LLM response ---")
    parts.append(_truncate(str(call.get("raw_response", "")), 1500))
    parts.append("\n--- parsed MatchResult ---")
    parts.append(json.dumps(call.get("parsed"), ensure_ascii=False, indent=2))
    if call.get("parse_error"):
        parts.append(f"\n!!! parse_error: {call['parse_error']}")
    if call.get("exception"):
        parts.append(f"\n!!! exception: {call['exception']}")
    return "\n".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keyword", default="大模型应用开发 agent")
    parser.add_argument("--n", type=int, default=2, help="number of candidates (max 5)")
    parser.add_argument("--no-detail", action="store_true", help="skip fetch_detail in scan")
    parser.add_argument("--workspace", default="job.default")
    parser.add_argument(
        "--sample", action="store_true",
        help="use 5 representative offline candidates instead of live BOSS scan; "
             "useful when the backend is down or you want a deterministic input set.",
    )
    args = parser.parse_args()

    n = max(1, min(args.n, len(_SAMPLE_CANDIDATES) if args.sample else 5))

    if args.sample:
        candidates = _SAMPLE_CANDIDATES[:n]
        print(
            f">>> sample mode: using {len(candidates)} offline candidates "
            f"(no BOSS scan)", file=sys.stderr,
        )
    else:
        candidates = fetch_scan_candidates(
            keyword=args.keyword, n=n, fetch_detail=not args.no_detail,
        )
        if not candidates:
            print("ERROR: scan returned 0 candidates; cannot dry-run.", file=sys.stderr)
            return 2
        print(f"\n>>> got {len(candidates)} scan candidates from backend", file=sys.stderr)

    # 2. Real JobMemorySnapshot from PostgreSQL (same path as production).
    db = DatabaseEngine()
    job_memory = JobMemory.from_engine(db, workspace_id=args.workspace)
    snapshot = job_memory.snapshot()
    print(
        f">>> loaded snapshot: {len(snapshot.memory_items)} memory_items, "
        f"hard_constraints={snapshot.hard_constraints.to_dict()}",
        file=sys.stderr,
    )

    # 3. Real LLM router + matcher, wrapped to capture the conversation.
    real_router = LLMRouter()
    capturing = CapturingLLMRouter(real_router)
    matcher = JobSnapshotMatcher(capturing)  # type: ignore[arg-type]

    print(f"\n>>> running matcher.match() on {len(candidates)} candidates "
          f"(no greet will be sent)\n", file=sys.stderr)

    results: list[dict[str, Any]] = []
    for idx, job in enumerate(candidates, 1):
        result = matcher.match(job=job, snapshot=snapshot, keyword=args.keyword)
        results.append({"job": job, "match": result.to_dict()})

    # 4. Pretty-print the captured conversations.
    for idx, (job, call) in enumerate(zip(candidates, capturing.calls), 1):
        print(render_call(idx, job, call))

    # 5. Verdict summary.
    print(f"\n{'=' * 78}\nSUMMARY\n{'=' * 78}")
    for idx, item in enumerate(results, 1):
        m = item["match"]
        company = item["job"].get("company") or "(no company)"
        print(
            f"  {idx}. {company:<30}  verdict={m['verdict']:<6}  "
            f"score={m['score']:>5.1f}  reason={m['reason'][:70]}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
