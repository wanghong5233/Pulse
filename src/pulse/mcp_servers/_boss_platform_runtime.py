from __future__ import annotations

import atexit
import hashlib
import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Literal

from pulse.core.tokenizer import token_preview
from pulse.core.tools.web_search import search_web

logger = logging.getLogger(__name__)

_SEED_JOBS: tuple[tuple[str, str, str], ...] = (
    ("AI Agent Intern", "Pulse Labs", "200-300/天"),
    ("LLM Application Engineer (Intern)", "NovaMind", "180-280/天"),
    ("AI 产品实习生", "DeepBridge", "150-220/天"),
    ("RAG Engineer (Intern)", "VectorWorks", "220-320/天"),
    ("Backend Engineer (Python)", "Orbit AI", "160-240/天"),
    ("MCP Tooling Intern", "Signal Stack", "200-260/天"),
)

_LOGIN_MARKERS = ("/web/user/", "/login", "passport.zhipin.com")
_DEFAULT_BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
# CDP fingerprint 反爬由 patchright 在 Chromium 二进制层处理; 不再维护
# `_KILL_ZHIPIN_FRAME_JS` / `iframe-core` 路由拦截 / `playwright_stealth`
# 这些 JS 层 workaround. 历史根因与迁移过程见
# `docs/archive/debug-boss-antibot-postmortem.md`.
_BROWSER_LOCK = Lock()
_PLAYWRIGHT_MANAGER = None
_PLAYWRIGHT = None
_CONTEXT = None
_PAGE = None
_BROWSER_LAST_USED_MONO = 0.0


def _safe_int(raw: Any, default: int, *, min_value: int, max_value: int) -> int:
    try:
        value = int(raw)
    except Exception:
        value = default
    return max(min_value, min(value, max_value))


def _safe_bool(raw: Any, *, default: bool) -> bool:
    value = str(raw or "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _resolve_path(raw_path: str | None, *, default_path: Path) -> Path:
    value = str(raw_path or "").strip()
    if not value:
        return default_path
    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        return candidate
    return (_repo_root() / candidate).resolve()


def _guess_title(raw_title: str, *, keyword: str) -> str:
    title = re.sub(r"\s+", " ", str(raw_title or "").strip())
    if not title:
        return f"{keyword} 招聘信息"
    for sep in (" - ", " | ", " _ ", "｜", "|", "-", "_"):
        if sep in title:
            candidate = title.split(sep, 1)[0].strip()
            if len(candidate) >= 4:
                return candidate[:120]
    return title[:120]


# BOSS 地址形态锚点: 来自 26/26 条真实 `boss_mcp_actions.jsonl` 样本,
# 全部匹配 "省·市·区/街道" 三段式 (`上海·浦东新区·张江` / `杭州·余杭区·仓前`).
# 公司名不会包含 "·" 中点 (2026-04 全网真实抽样核对).
_ADDRESS_DOT_PATTERN = re.compile(r"^[^·\s]{2,}(·[^·\s]{2,}){1,4}$")


def _looks_like_address(text: str) -> bool:
    """判定一个字符串更像 BOSS 地址节点内容,而非公司名.

    历史根因(``trace: boss_mcp_actions.jsonl`` 26/26 全误判): 老 scan
    selector ``[class*='company']`` 精确命中 ``.company-location``
    (地址 span, class 字面含 "company"), 导致 ``row["company"]`` 拿到
    `"上海·浦东新区·张江"` 这种 3 段式地址. 真正的公司名节点是
    ``.boss-name`` — 已在 selector 侧修复.

    本函数是 defense-in-depth: 即便 BOSS 未来再改版 CSS class, 只要
    被误抓的仍是地址节点, 就能用内容形态兜住 — company 字段会回落
    到空字符串,让下游 ``_guess_company`` / matcher 感知到 "公司名未
    成功采集" 这个事实,而不是把地址当公司灌给 LLM.
    """
    s = (text or "").strip()
    if not s:
        return False
    return bool(_ADDRESS_DOT_PATTERN.match(s))


def _guess_company(title: str, url: str) -> str:
    """标题启发式切分兜底 — scan selector 完全未命中时的最后手段.

    原则: **宁可返回空字符串,也不要伪造**. 历史上曾把 URL 的 host
    (`www.zhipin.com`) 当公司名返回,那是典型"防御式逃避 + 伪造空结果"
    (code-review-checklist §类型 A/B),已移除 — 让 company_missing 信号
    自然向下游传,matcher 会把缺失项放进 ``concerns`` 让用户感知.
    """
    for sep in (" - ", " | ", " _ ", "｜", "|", "-", "_"):
        if sep in title:
            parts = [item.strip() for item in title.split(sep) if item.strip()]
            if len(parts) >= 2:
                candidate = parts[1][:80]
                if not _looks_like_address(candidate):
                    return candidate
    return ""


def _clean_html(text: str) -> str:
    text = re.sub(r"(?is)<script.*?>.*?</script>", " ", text)
    text = re.sub(r"(?is)<style.*?>.*?</style>", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&quot;", '"')
    return re.sub(r"\s+", " ", text).strip()


def _csv_list(raw: str) -> list[str]:
    return [item.strip() for item in str(raw or "").split(",") if item.strip()]


def _browser_profile_dir() -> Path:
    explicit = str(os.getenv("PULSE_BOSS_BROWSER_PROFILE_DIR", "") or "").strip()
    if not explicit:
        explicit = str(os.getenv("BOSS_BROWSER_PROFILE_DIR", "") or "").strip()
    return _resolve_path(explicit, default_path=Path.home() / ".pulse" / "boss_browser_profile")


def _browser_headless() -> bool:
    raw = str(os.getenv("PULSE_BOSS_BROWSER_HEADLESS", "") or "").strip()
    if raw:
        return _safe_bool(raw, default=False)
    return _safe_bool(os.getenv("BOSS_HEADLESS", "false"), default=False)


def _browser_timeout_ms() -> int:
    return _safe_int(
        os.getenv("PULSE_BOSS_BROWSER_TIMEOUT_MS", "20000"),
        20000,
        min_value=3000,
        max_value=90000,
    )


def _browser_idle_close_sec() -> int:
    """Idle timeout (seconds) before recycling the persistent browser session.

    ``0`` means disabled. A non-zero value keeps the runtime warm for short
    bursts (chat patrol loops) but releases resources after inactivity.
    """
    return _safe_int(
        os.getenv("PULSE_BOSS_BROWSER_IDLE_CLOSE_SEC", "900"),
        900,
        min_value=0,
        max_value=86400,
    )


def _mark_browser_used(*, now_mono: float | None = None) -> None:
    global _BROWSER_LAST_USED_MONO
    _BROWSER_LAST_USED_MONO = (
        float(now_mono)
        if now_mono is not None
        else float(time.monotonic())
    )


def _browser_idle_elapsed_sec(*, now_mono: float | None = None) -> float:
    last = float(_BROWSER_LAST_USED_MONO)
    if last <= 0.0:
        return 0.0
    now_value = (
        float(now_mono)
        if now_mono is not None
        else float(time.monotonic())
    )
    return max(0.0, now_value - last)


def _should_recycle_browser_for_idle(*, now_mono: float | None = None) -> bool:
    if _PAGE is None:
        return False
    idle_limit = _browser_idle_close_sec()
    if idle_limit <= 0:
        return False
    return _browser_idle_elapsed_sec(now_mono=now_mono) >= float(idle_limit)


def _browser_screenshot_dir() -> Path | None:
    configured = str(os.getenv("PULSE_BOSS_MCP_SCREENSHOT_DIR", "") or "").strip()
    if not configured:
        configured = str(os.getenv("BOSS_SCREENSHOT_DIR", "") or "").strip()
    if not configured:
        return None
    return _resolve_path(configured, default_path=_repo_root() / "backend" / "exports" / "screenshots")


def _browser_channel() -> str:
    return str(os.getenv("PULSE_BOSS_BROWSER_CHANNEL", "") or "").strip()


def _browser_user_agent() -> str | None:
    """Explicit UA override only; empty → let Chromium decide.

    patchright ship 自带较新的 Chromium, 传死 UA 会让 navigator.userAgent
    与 User-Agent Client Hints / 真实内核版本错位, 反而制造指纹缺陷. 所以
    默认返回 ``None``, 只有用户在 ``PULSE_BOSS_BROWSER_USER_AGENT`` 里显式
    指定时才覆盖. 保留老的 `_DEFAULT_BROWSER_UA` 仅用于 `health()` 展示.
    """
    value = str(os.getenv("PULSE_BOSS_BROWSER_USER_AGENT", "") or "").strip()
    return value or None


def _browser_stealth_enabled() -> bool:
    """Deprecated no-op kept for historical env files.

    Stealth 现在由 patchright 在 Chromium 二进制层内建, 不再由本模块负责.
    保留函数以避免外部 health 消费方崩溃; 始终返回 ``True`` 表示"已内建".
    `PULSE_BOSS_BROWSER_STEALTH_ENABLED` 环境变量将被忽略; 在启动时如探测到
    非空值, 在 ``_ensure_browser_page`` 里 fail-loud 提示迁移.
    """
    return True


def _legacy_stealth_env_override() -> str:
    return str(os.getenv("PULSE_BOSS_BROWSER_STEALTH_ENABLED", "") or "").strip()


def _legacy_block_iframe_core_env_override() -> str:
    return str(os.getenv("PULSE_BOSS_BROWSER_BLOCK_IFRAME_CORE", "") or "").strip()


def _browser_block_iframe_core() -> bool:
    """Deprecated no-op kept for historical env files.

    `iframe-core.js` 是 BOSS 登录/搜索 SPA 的 Vue Router 包; 拦截它会让页面卡在
    "加载中". patchright 已在 CDP 协议层绕过检测, 不需要也**不应再**阻断该脚本.
    函数保留是为了老 config 的 health 输出不报错; 值始终是 ``False``.
    """
    return False


def _is_login_page(url: str) -> bool:
    value = str(url or "").strip().lower()
    return any(marker in value for marker in _LOGIN_MARKERS)


def _is_security_page(url: str) -> bool:
    value = str(url or "").strip().lower()
    return any(
        marker in value
        for marker in (
            "/web/passport/zp/security",
            "passport/zp/security.html",
            "_security_check=",
            "code=37",
        )
    )


def _default_greet_button_selectors() -> list[str]:
    return [
        "button:has-text('立即沟通')",
        "button:has-text('立即沟通') span",
        "button:has-text('发起沟通')",
        "button:has-text('立即开聊')",
        "a:has-text('立即沟通')",
    ]


def _default_chat_input_selectors() -> list[str]:
    return [
        "textarea",
        "[contenteditable='true']",
        ".chat-input",
        ".input-area",
    ]


def _default_chat_send_selectors() -> list[str]:
    return [
        "button:has-text('发送')",
        ".send-message",
        ".send-btn",
    ]


def _default_chat_item_selectors(conversation_id: str) -> list[str]:
    safe = str(conversation_id or "").strip()
    if not safe:
        return []
    return [
        f"[data-conversation-id='{safe}']",
        f"[data-id='{safe}']",
        f"li[data-id='{safe}']",
    ]


def _scan_mode() -> str:
    value = str(os.getenv("PULSE_BOSS_MCP_SCAN_MODE", "browser_only") or "").strip().lower()
    if value in {"browser_only", "browser_first", "web_search_only"}:
        return value
    return "browser_only"


def _pull_mode() -> str:
    value = str(os.getenv("PULSE_BOSS_MCP_PULL_MODE", "browser_only") or "").strip().lower()
    if value in {"browser_only", "browser_first", "local_only"}:
        return value
    return "browser_only"


def _allow_seed_fallback() -> bool:
    return _safe_bool(os.getenv("PULSE_BOSS_ALLOW_SEED_FALLBACK", "false"), default=False)


def _search_url_template() -> str:
    return str(
        os.getenv("PULSE_BOSS_SEARCH_URL_TEMPLATE", "https://www.zhipin.com/web/geek/jobs?query={keyword}") or ""
    ).strip()


# BOSS 直聘城市 → city code 映射.
#
# BOSS 的搜索 URL 接受形如 ``?query=python&city=101210100`` 的参数, 这些数字
# 来自 `www.zhipin.com/wapi/zpgeek/search/joblist/city.json` 返回的城市字典.
# 这里只录入一线 + 强二线 + 新一线常用 ~30 个城市作为 MVP, 覆盖 95% 求职
# 意向; 未命中的城市名会降级成全国搜索 (``city`` 参数不拼进 URL) — 这是
# fail-soft 而非 silent-fail: 全国搜索仍会返回结果, 服务侧 hard_constraint
# 也会拦住非偏好城市的条目.
_BOSS_CITY_CODES: dict[str, str] = {
    # 直辖市 / 一线
    "北京": "101010100",
    "上海": "101020100",
    "天津": "101030100",
    "重庆": "101040100",
    "广州": "101280100",
    "深圳": "101280600",
    # 新一线 / 强二线
    "杭州": "101210100",
    "成都": "101270100",
    "南京": "101190100",
    "武汉": "101200100",
    "苏州": "101190400",
    "西安": "101110100",
    "长沙": "101250100",
    "郑州": "101180100",
    "青岛": "101120200",
    "宁波": "101210400",
    "厦门": "101230200",
    "合肥": "101220100",
    "福州": "101230100",
    "济南": "101120100",
    "东莞": "101281600",
    "佛山": "101280800",
    "无锡": "101190200",
    "珠海": "101280700",
    "沈阳": "101070100",
    "大连": "101070200",
    "长春": "101060100",
    "哈尔滨": "101050100",
    "昆明": "101290100",
    "南昌": "101240100",
    "石家庄": "101090101",
}


def _resolve_city_code(city: str | None) -> str | None:
    """Map a human city name (中文/英文全小写) to BOSS city code, or None."""
    if not city:
        return None
    key = str(city).strip()
    if not key:
        return None
    # 既接受 "杭州"/"上海" 也接受 "杭州市"/"上海市" 带后缀; 先精准匹配, 再
    # strip 掉 "市" 后再查一次, 避免假阴性.
    if key in _BOSS_CITY_CODES:
        return _BOSS_CITY_CODES[key]
    stripped = key.rstrip("市")
    return _BOSS_CITY_CODES.get(stripped)


def _build_search_url(*, keyword: str, page: int, city: str | None = None) -> str:
    template = _search_url_template()
    encoded_keyword = urllib.parse.quote_plus(str(keyword or "").strip())
    safe_page = max(1, int(page))
    if "{keyword}" in template:
        template = template.replace("{keyword}", encoded_keyword)
    if "{page}" in template:
        template = template.replace("{page}", str(safe_page))
    city_code = _resolve_city_code(city)
    if city_code:
        separator = "&" if "?" in template else "?"
        template = f"{template}{separator}city={city_code}"
    return template


def _chat_list_url() -> str:
    return str(os.getenv("PULSE_BOSS_CHAT_LIST_URL", "https://www.zhipin.com/web/geek/chat") or "").strip()


def _default_job_card_selectors() -> list[str]:
    return [
        ".job-list-box li",
        ".job-list li",
        ".job-card-wrapper",
        ".job-card-box",
        ".search-job-result .job-card",
    ]


def _default_job_next_page_selectors() -> list[str]:
    return [
        "a[ka='page-next']",
        "a:has-text('下一页')",
        ".options-pages a.next",
        ".page a.next",
    ]


def _default_job_nav_selectors() -> list[str]:
    return [
        "a:has-text('职位')",
        "button:has-text('职位')",
        "text=职位",
        "a[href*='/web/geek/jobs']",
        "a[href*='/web/geek/job']",
    ]


def _default_job_search_input_selectors() -> list[str]:
    return [
        "input[placeholder*='搜索职位']",
        "input[placeholder*='关键词']",
        "input[placeholder*='搜索']",
        "input[type='search']",
        "input[type='text']",
    ]


def _default_chat_row_selectors() -> list[str]:
    # Ground truth = live BOSS DOM, observed 2026-04-22 via trace_24ecd22aa795
    # diagnose: `.user-list` container exists, `.user-list > li` (direct
    # child) = 0, `.user-list li` (descendant) = 2 (matches user-visible
    # "未读(2)" tab). BOSS wraps its <li> rows inside a nested element,
    # so descendant match is required. Dump fixture
    # docs/dom-specs/boss/chat-list/20260422T072555Z.json L6 already
    # recorded hit_selector=".user-list li" — code must mirror the live
    # DOM + dump; docs are evidence, not authority.
    return [".user-list li"]


def _browser_executor_retry_count() -> int:
    return _safe_int(
        os.getenv("PULSE_BOSS_BROWSER_EXECUTOR_RETRY_COUNT", "1"),
        1,
        min_value=0,
        max_value=4,
    )


def _browser_executor_retry_backoff_ms() -> int:
    return _safe_int(
        os.getenv("PULSE_BOSS_BROWSER_EXECUTOR_RETRY_BACKOFF_MS", "700"),
        700,
        min_value=100,
        max_value=8000,
    )


def _risk_keywords() -> list[str]:
    configured = _csv_list(os.getenv("PULSE_BOSS_RISK_KEYWORDS", ""))
    if configured:
        return [item.lower() for item in configured if item]
    return [
        "验证码",
        "人机验证",
        "访问受限",
        "异常访问",
        "风险提示",
        "请完成验证",
        "captcha",
        "access denied",
    ]


def _contains_risk_keywords(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(keyword in lowered for keyword in _risk_keywords())


def _read_page_text(url: str, *, max_chars: int = 2500) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            )
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=12) as response:
        raw = response.read().decode("utf-8", errors="ignore")
    return _clean_html(raw)[: max(500, min(max_chars, 8000))]


def _shutdown_browser_runtime_unlocked(*, reason: str) -> dict[str, Any]:
    """Close cached playwright objects.

    Caller must hold ``_BROWSER_LOCK``.
    """
    global _PLAYWRIGHT_MANAGER, _PLAYWRIGHT, _CONTEXT, _PAGE, _BROWSER_LAST_USED_MONO
    safe_reason = str(reason or "").strip() or "unspecified"
    page = _PAGE
    context = _CONTEXT
    playwright_manager = _PLAYWRIGHT_MANAGER

    _PAGE = None
    _CONTEXT = None
    _PLAYWRIGHT = None
    _PLAYWRIGHT_MANAGER = None
    _BROWSER_LAST_USED_MONO = 0.0

    closed_page = False
    closed_context = False
    closed_playwright = False
    errors: list[str] = []

    if page is not None:
        try:
            is_closed = bool(page.is_closed())
        except (RuntimeError, OSError, AttributeError, ValueError):
            is_closed = False
        if not is_closed:
            try:
                page.close()
                closed_page = True
            except (RuntimeError, OSError, AttributeError, ValueError) as exc:
                errors.append(f"page_close:{exc}")

    if context is not None:
        try:
            context.close()
            closed_context = True
        except (RuntimeError, OSError, AttributeError, ValueError) as exc:
            errors.append(f"context_close:{exc}")

    if playwright_manager is not None:
        try:
            playwright_manager.stop()
            closed_playwright = True
        except (RuntimeError, OSError, AttributeError, ValueError) as exc:
            errors.append(f"playwright_stop:{exc}")

    if errors:
        logger.warning(
            "boss browser cleanup finished with warnings reason=%s errors=%s",
            safe_reason,
            errors[:3],
        )
    else:
        logger.info(
            "boss browser cleanup completed reason=%s page=%s context=%s playwright=%s",
            safe_reason,
            closed_page,
            closed_context,
            closed_playwright,
        )
    return {
        "ok": not errors,
        "reason": safe_reason,
        "closed_page": closed_page,
        "closed_context": closed_context,
        "closed_playwright": closed_playwright,
        "errors": errors,
    }


def reset_browser_session(*, reason: str = "manual") -> dict[str, Any]:
    """Manual cleanup entry for operators and runtime self-healing."""
    with _BROWSER_LOCK:
        return _shutdown_browser_runtime_unlocked(reason=reason)


def _cleanup_browser_on_process_exit() -> None:
    with _BROWSER_LOCK:
        _shutdown_browser_runtime_unlocked(reason="process_exit")


atexit.register(_cleanup_browser_on_process_exit)


def _ensure_browser_page():
    global _PLAYWRIGHT_MANAGER, _PLAYWRIGHT, _CONTEXT, _PAGE
    now_mono = float(time.monotonic())
    with _BROWSER_LOCK:
        if _PAGE is not None:
            try:
                if not _PAGE.is_closed():
                    if _should_recycle_browser_for_idle(now_mono=now_mono):
                        idle_elapsed = _browser_idle_elapsed_sec(now_mono=now_mono)
                        _shutdown_browser_runtime_unlocked(
                            reason=f"idle_timeout_{idle_elapsed:.1f}s"
                        )
                    else:
                        _mark_browser_used(now_mono=now_mono)
                        return _PAGE
                else:
                    _shutdown_browser_runtime_unlocked(reason="stale_page_handle")
            except (RuntimeError, OSError, AttributeError, ValueError):
                _shutdown_browser_runtime_unlocked(reason="page_probe_failed")

        try:
            from patchright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "patchright is not available; install `pip install -e '.[browser]'` "
                "and run `patchright install chromium`. BOSS 反爬在 CDP 协议层检测, "
                "原生 playwright 会被重定向到 about:blank; 历史根因见 "
                "docs/archive/debug-boss-antibot-postmortem.md."
            ) from exc

        stealth_override = _legacy_stealth_env_override()
        if stealth_override and stealth_override.lower() not in {"", "true", "1", "yes", "on"}:
            raise RuntimeError(
                f"PULSE_BOSS_BROWSER_STEALTH_ENABLED={stealth_override!r} is no longer honored; "
                "stealth 已由 patchright 在二进制层内建. 从 .env 里删除这一行."
            )
        block_iframe_override = _legacy_block_iframe_core_env_override()
        if block_iframe_override and block_iframe_override.lower() in {"true", "1", "yes", "on"}:
            raise RuntimeError(
                f"PULSE_BOSS_BROWSER_BLOCK_IFRAME_CORE={block_iframe_override!r} is no longer honored; "
                "patchright 绕过检测后不应再阻断 iframe-core.js (该脚本是 BOSS 登录页 SPA 路由, "
                "阻断会导致页面卡在加载中). 从 .env 里删除这一行."
            )

        profile_dir = _browser_profile_dir()
        profile_dir.mkdir(parents=True, exist_ok=True)
        if _PLAYWRIGHT_MANAGER is None or _PLAYWRIGHT is None:
            _PLAYWRIGHT_MANAGER = sync_playwright()
            _PLAYWRIGHT = _PLAYWRIGHT_MANAGER.start()
        if _PLAYWRIGHT is None:
            raise RuntimeError("patchright runtime is empty after start()")

        launch_args: dict[str, Any] = {
            "user_data_dir": str(profile_dir),
            "headless": _browser_headless(),
            "no_viewport": True,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
                "--no-sandbox",
            ],
        }
        explicit_ua = _browser_user_agent()
        if explicit_ua:
            launch_args["user_agent"] = explicit_ua
        channel = _browser_channel()
        if channel:
            launch_args["channel"] = channel
        try:
            _CONTEXT = _PLAYWRIGHT.chromium.launch_persistent_context(**launch_args)
        except TypeError:
            launch_args.pop("no_viewport", None)
            _CONTEXT = _PLAYWRIGHT.chromium.launch_persistent_context(**launch_args)
        _PAGE = _CONTEXT.pages[0] if _CONTEXT.pages else _CONTEXT.new_page()
        _PAGE.set_default_timeout(_browser_timeout_ms())
        _close_orphan_tabs(_CONTEXT, _PAGE)
        _mark_browser_used(now_mono=float(time.monotonic()))
        return _PAGE


def _close_orphan_tabs(context: Any, main_page: Any) -> int:
    """Close tabs restored from the persistent profile we do not drive.

    Chromium's ``launch_persistent_context`` restores every tab that was
    open when the previous session ended (via ``user_data_dir`` +
    ``Sessions/Session Storage``). patchright only binds its automation
    hooks to the single ``_PAGE`` we keep; all other restored tabs turn
    into "WSLg renders a window, but no CDP driver is attached" zombie
    views that the user cannot close through the window decoration ×
    button because the render process is still owned by a CDP-attached
    Chromium. Symptom observed on the user's trace (image attached):
    a frozen ``zhipin.com/web/geek/jobs?_security_check=...`` tab on top
    of the real chat window.

    Fix: at startup we ``close()`` every non-main page. This is safe and
    idempotent — new tabs created inside the driven flow are created on
    ``_PAGE`` via ``page.goto(...)``, not via ``context.new_page()``.
    Returns the number of tabs closed, for tests.
    """
    closed = 0
    for orphan in list(getattr(context, "pages", ()) or ()):
        if orphan is main_page:
            continue
        try:
            orphan.close()
        except Exception as close_exc:  # noqa: BLE001 — narrow-logged
            _append_action_log(
                {
                    "action": "browser_orphan_tab_close_failed",
                    "ok": False,
                    "error": str(close_exc)[:200],
                    "url": str(getattr(orphan, "url", "") or ""),
                }
            )
            continue
        closed += 1
        _append_action_log(
            {
                "action": "browser_orphan_tab_closed",
                "ok": True,
                "url": str(getattr(orphan, "url", "") or ""),
            }
        )
    return closed


def _wait_for_any_selector(page: Any, selectors: list[str], *, timeout_ms: int) -> tuple[Any, str]:
    for selector in selectors:
        try:
            page.wait_for_selector(selector, timeout=timeout_ms)
            return page.locator(selector).first, selector
        except Exception:
            continue
    return None, ""


def _fill_text(locator: Any, text: str) -> None:
    try:
        locator.fill(text, timeout=min(_browser_timeout_ms(), 10000))
        return
    except Exception:
        pass
    locator.click(timeout=min(_browser_timeout_ms(), 10000))
    locator.type(text, delay=20, timeout=min(_browser_timeout_ms(), 10000))


def _take_browser_screenshot(page: Any, *, prefix: str) -> str | None:
    directory = _browser_screenshot_dir()
    if directory is None:
        return None
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_prefix = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in prefix).strip("_") or "boss"
    target = directory / f"{stamp}_{safe_prefix}.png"
    try:
        page.screenshot(path=str(target), full_page=True)
    except Exception:
        return None
    return str(target)


def _build_chat_url(conversation_id: str) -> str:
    template = str(os.getenv("PULSE_BOSS_CHAT_URL_TEMPLATE", "") or "").strip()
    if not template:
        template = "https://www.zhipin.com/web/geek/chat?conversationId={conversation_id}"
    safe_id = str(conversation_id or "").strip()
    if "{conversation_id}" in template:
        return template.replace("{conversation_id}", safe_id)
    if safe_id and "conversationId=" not in template and "?" in template:
        return f"{template}&conversationId={safe_id}"
    if safe_id and "conversationId=" not in template:
        return f"{template}?conversationId={safe_id}"
    return template


def _build_search_url_candidates(
    *, keyword: str, page: int, city: str | None = None
) -> list[str]:
    primary = _build_search_url(keyword=keyword, page=page, city=city)
    candidates: list[str] = [primary]
    if "/web/geek/job?" in primary:
        candidates.append(primary.replace("/web/geek/job?", "/web/geek/jobs?"))
    if "/web/geek/jobs?" in primary:
        candidates.append(primary.replace("/web/geek/jobs?", "/web/geek/job?"))
    encoded_keyword = urllib.parse.quote_plus(str(keyword or "").strip())
    city_code = _resolve_city_code(city)
    city_suffix = f"&city={city_code}" if city_code else ""
    if encoded_keyword:
        candidates.append(
            f"https://www.zhipin.com/web/geek/jobs?query={encoded_keyword}{city_suffix}"
        )
        candidates.append(
            f"https://www.zhipin.com/web/geek/job?query={encoded_keyword}{city_suffix}"
        )
    deduped: list[str] = []
    seen: set[str] = set()
    for url in candidates:
        safe = str(url or "").strip()
        if not safe or safe in seen:
            continue
        seen.add(safe)
        deduped.append(safe)
    return deduped


def _extract_jobs_with_retries(
    page: Any,
    *,
    keyword: str,
    max_items: int,
    seen_keys: set[str],
) -> list[dict[str, Any]]:
    attempts = max(1, _safe_int(os.getenv("PULSE_BOSS_SCAN_EXTRACT_ATTEMPTS", "4"), 4, min_value=1, max_value=8))
    rows: list[dict[str, Any]] = []
    for attempt in range(attempts):
        rows = _extract_jobs_from_page(page, keyword=keyword, max_items=max_items, seen_keys=seen_keys)
        if rows:
            return rows
        try:
            page.mouse.wheel(0, 1200)
        except Exception:
            pass
        try:
            page.wait_for_load_state("networkidle", timeout=min(_browser_timeout_ms(), 3000))
        except Exception:
            pass
        page.wait_for_timeout(900 + min(900, attempt * 250))
    return rows


def _navigate_jobs_from_chat(page: Any, *, keyword: str) -> tuple[bool, str]:
    try:
        page.goto(_chat_list_url(), wait_until="domcontentloaded", timeout=_browser_timeout_ms())
    except Exception:
        return False, ""
    current_url = str(page.url or "")
    risk_status = _detect_runtime_risk(page, current_url=current_url)
    if risk_status:
        return False, current_url

    nav_selectors = _csv_list(os.getenv("PULSE_BOSS_JOB_NAV_SELECTORS", ""))
    if not nav_selectors:
        nav_selectors = _default_job_nav_selectors()
    nav_loc, _nav_selector = _wait_for_any_selector(page, nav_selectors, timeout_ms=min(_browser_timeout_ms(), 4500))
    if nav_loc is not None:
        try:
            nav_loc.click(timeout=min(_browser_timeout_ms(), 6000))
            try:
                page.wait_for_load_state("domcontentloaded", timeout=min(_browser_timeout_ms(), 6000))
            except Exception:
                pass
            page.wait_for_timeout(900)
        except Exception:
            pass

    safe_keyword = str(keyword or "").strip()
    if safe_keyword:
        input_selectors = _csv_list(os.getenv("PULSE_BOSS_JOB_SEARCH_INPUT_SELECTORS", ""))
        if not input_selectors:
            input_selectors = _default_job_search_input_selectors()
        input_loc, _input_selector = _wait_for_any_selector(
            page,
            input_selectors,
            timeout_ms=min(_browser_timeout_ms(), 4500),
        )
        if input_loc is not None:
            try:
                _fill_text(input_loc, safe_keyword)
                input_loc.press("Enter", timeout=min(_browser_timeout_ms(), 5000))
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=min(_browser_timeout_ms(), 8000))
                except Exception:
                    pass
                page.wait_for_timeout(1200)
            except Exception:
                pass

    return True, str(page.url or "")


def _extract_jobs_from_page(
    page: Any,
    *,
    keyword: str,
    max_items: int,
    seen_keys: set[str],
) -> list[dict[str, Any]]:
    selectors = _csv_list(os.getenv("PULSE_BOSS_JOB_CARD_SELECTORS", ""))
    if not selectors:
        selectors = _default_job_card_selectors()
    raw_rows: list[dict[str, Any]] = []
    for selector in selectors:
        try:
            # Selector contract 来自真实抽样 (`logs/boss_card_dump_*.json`,
            # 2026-04-22 BOSS 搜索页 3 张 card):
            #   .job-card-box > .job-info > a.job-name         ← 岗位标题
            #                            > span.job-salary     ← 薪资 (平台端可能脱敏为 "-元/天")
            #                > .job-card-footer > a.boss-info > span.boss-name         ← 真公司名 ★
            #                                    span.company-location                 ← 地址 (不要和公司混)
            # 历史误判: 老 selector 用 `[class*='company']` 宽松匹配, 精确
            # 命中 `.company-location` 导致 26/26 条真实投递 company=地址.
            # 修法: company 精确走 `.boss-name`, 同时另开 location 字段,
            # 让下游 matcher / ActionReport 能区分二者.
            rows = page.eval_on_selector_all(
                selector,
                """nodes => nodes.map(node => {
                    const text = (node.innerText || "").replace(/\\s+/g, " ").trim();
                    const titleEl = node.querySelector(".job-name,.job-title");
                    const companyEl = node.querySelector(".boss-name,.company-name,.company-text");
                    const salaryEl = node.querySelector(".job-salary,.salary");
                    const locationEl = node.querySelector(".company-location,.job-area,.job-location");
                    const linkEl = node.querySelector("a.job-name[href*='/job_detail'],a[href*='/job_detail'],a[href*='/web/geek/job'],a[href]");
                    const href = linkEl ? linkEl.href : "";
                    return {
                        title: titleEl ? (titleEl.innerText || "").trim() : "",
                        company: companyEl ? (companyEl.innerText || "").trim() : "",
                        salary: salaryEl ? (salaryEl.innerText || "").trim() : "",
                        location: locationEl ? (locationEl.innerText || "").trim() : "",
                        source_url: href || "",
                        snippet: text.slice(0, 1000),
                    };
                })""",
            )
        except Exception:
            rows = []
        if isinstance(rows, list) and rows:
            for row in rows:
                if isinstance(row, dict):
                    raw_rows.append(dict(row))
            break

    if not raw_rows:
        try:
            anchor_rows = page.eval_on_selector_all(
                "a[href*='job_detail'],a[href*='/web/geek/job']",
                """nodes => nodes.map(node => {
                    const href = node.href || "";
                    const ownText = (node.innerText || "").replace(/\\s+/g, " ").trim();
                    const parent = node.closest("li,div,article,section") || node.parentElement || node;
                    const parentText = parent && parent.innerText ? parent.innerText.replace(/\\s+/g, " ").trim() : ownText;
                    return {
                        title: ownText || parentText.slice(0, 60),
                        company: "",
                        salary: "",
                        source_url: href,
                        snippet: parentText.slice(0, 1000),
                    };
                })""",
            )
        except Exception:
            anchor_rows = []
        if isinstance(anchor_rows, list):
            for row in anchor_rows:
                if isinstance(row, dict):
                    raw_rows.append(dict(row))

    result: list[dict[str, Any]] = []
    for row in raw_rows:
        source_url = str(row.get("source_url") or "").strip()
        title_raw = str(row.get("title") or "").strip()
        snippet = str(row.get("snippet") or "").strip()
        dedupe_key = (source_url or title_raw or snippet).lower()
        if not dedupe_key or dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)
        title = _guess_title(title_raw, keyword=keyword)
        # Defense-in-depth: 即便 selector 被 BOSS 下次改版再次错位,
        # 只要抓到的仍是地址形态(3 段 `·` 切分), 也要把它当"未采集"处理.
        raw_company = str(row.get("company") or "").strip()
        if _looks_like_address(raw_company):
            raw_company = ""
        company = raw_company or _guess_company(title_raw, source_url)
        raw_location = str(row.get("location") or "").strip()
        if not source_url:
            source_url = f"https://www.zhipin.com/job_detail/{hashlib.sha1(dedupe_key.encode('utf-8')).hexdigest()[:16]}"
        result.append(
            {
                "job_id": hashlib.sha1(source_url.encode("utf-8")).hexdigest()[:16],
                "title": title,
                "company": company,
                "salary": str(row.get("salary") or "").strip() or None,
                "location": raw_location or None,
                "source_url": source_url,
                "snippet": token_preview(snippet, max_tokens=700),
                "source": "boss_mcp_browser_scan",
                "collected_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        if len(result) >= max(1, max_items):
            break
    return result


# 滚动驱动 scan 的硬天花板. evaluation_cap 通常远小于这个值, 但当
# BOSS 反爬异常导致 plateau 检测失效时, 这条线兜底防止死循环.
_SCAN_ABSOLUTE_SCROLL_CAP: int = 60


def _scan_jobs_via_browser(
    *,
    keyword: str,
    target_count: int,
    evaluation_cap: int,
    scroll_plateau_rounds: int,
    city: str | None = None,
) -> dict[str, Any]:
    """Streaming scan: scroll the search sidebar until enough cards collected.

    BOSS 直聘搜索结果页是 SPA 无限滚动, 同一关键词下侧栏向下滑动会持续
    加载更多 JD. 不应该回退到"按下一页按钮 + max_pages"的分页假设 —
    那会让我们卡在首屏几个候选, 与平台真实 UI 行为不符.

    停止条件 (任一即停, 顺序为评估顺序):

    1. ``len(rows) >= target_count``  目标候选数已满 (常态成功路径).
    2. ``len(rows) >= evaluation_cap`` 总评估上限触顶, 由调用方控制成本.
    3. 连续 ``scroll_plateau_rounds`` 次滚动后无新增卡片 → 真到底 (返回
       ``exhausted=True``, 调用方据此决定是否进入关键词演化兜底).
    4. ``_SCAN_ABSOLUTE_SCROLL_CAP`` 防御性硬上限.
    """
    safe_keyword = str(keyword or "").strip() or "AI Agent 实习"
    safe_target = _safe_int(target_count, 10, min_value=1, max_value=200)
    safe_cap = _safe_int(evaluation_cap, max(safe_target, 30), min_value=safe_target, max_value=200)
    safe_plateau = _safe_int(scroll_plateau_rounds, 3, min_value=1, max_value=8)
    safe_city = (str(city).strip() or None) if city else None
    try:
        page = _ensure_browser_page()
    except Exception as exc:
        return {
            "ok": False,
            "status": "executor_unavailable",
            "items": [],
            "scroll_count": 0,
            "exhausted": False,
            "source": "boss_mcp_browser_scan",
            "errors": [str(exc)[:300]],
        }

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    errors: list[str] = []
    scroll_count = 0
    plateau_streak = 0
    exhausted = False

    candidates = _build_search_url_candidates(
        keyword=safe_keyword, page=1, city=safe_city
    )
    page_ready = False
    for target_url in candidates:
        try:
            page.goto(target_url, wait_until="domcontentloaded", timeout=_browser_timeout_ms())
            page_ready = True
        except Exception as exc:
            errors.append(f"page navigation failed: {str(exc)[:220]}")
            continue
        risk_status = _detect_runtime_risk(page, current_url=str(page.url or ""))
        if risk_status:
            errors.append(f"risk status={risk_status}; url={str(page.url or '')[:160]}")
            page_ready = False
            continue
        break

    if not page_ready:
        nav_ok, nav_url = _navigate_jobs_from_chat(page, keyword=safe_keyword)
        if nav_ok and not _detect_runtime_risk(page, current_url=nav_url):
            page_ready = True

    if not page_ready:
        return {
            "ok": False,
            "status": "navigate_failed",
            "items": [],
            "scroll_count": 0,
            "exhausted": False,
            "source": "boss_mcp_browser_scan",
            "errors": errors or ["browser scan failed: page not reachable"],
        }

    initial = _extract_jobs_with_retries(
        page,
        keyword=safe_keyword,
        max_items=safe_cap,
        seen_keys=seen,
    )
    rows.extend(initial)

    while (
        len(rows) < safe_target
        and len(rows) < safe_cap
        and scroll_count < _SCAN_ABSOLUTE_SCROLL_CAP
    ):
        try:
            page.mouse.wheel(0, 1500)
        except Exception as exc:
            errors.append(f"scroll wheel failed: {str(exc)[:200]}")
            break
        try:
            page.wait_for_load_state("networkidle", timeout=min(_browser_timeout_ms(), 2500))
        except Exception:
            pass
        page.wait_for_timeout(700)
        scroll_count += 1

        risk_status = _detect_runtime_risk(page, current_url=str(page.url or ""))
        if risk_status:
            errors.append(f"risk status={risk_status}; aborted at scroll={scroll_count}")
            break

        added = _extract_jobs_from_page(
            page,
            keyword=safe_keyword,
            max_items=safe_cap - len(rows),
            seen_keys=seen,
        )
        if added:
            rows.extend(added)
            plateau_streak = 0
        else:
            plateau_streak += 1
            if plateau_streak >= safe_plateau:
                exhausted = True
                break

    items = rows[:safe_cap]
    if not items:
        return {
            "ok": False,
            "status": "no_result",
            "items": [],
            "scroll_count": scroll_count,
            "exhausted": exhausted or plateau_streak >= safe_plateau,
            "source": "boss_mcp_browser_scan",
            "errors": errors or ["browser scan returned no jobs"],
        }
    return {
        "ok": True,
        "status": "ready",
        "items": items,
        "scroll_count": scroll_count,
        "exhausted": exhausted,
        "source": "boss_mcp_browser_scan",
        "errors": errors,
    }


_CHAT_HYDRATE_FOCUS_JS = """() => {
    try { Object.defineProperty(document, 'hidden', {value: false, configurable: true}); } catch (e) {}
    try { Object.defineProperty(document, 'visibilityState', {value: 'visible', configurable: true}); } catch (e) {}
    document.dispatchEvent(new Event('visibilitychange'));
    window.dispatchEvent(new Event('focus'));
}"""


_CHAT_LIST_DIAGNOSE_JS = """() => {
    const isVisible = (el) => {
        if (!el || !(el instanceof Element)) return false;
        if (el.getClientRects().length === 0) return false;
        let cur = el;
        while (cur && cur instanceof Element) {
            const style = window.getComputedStyle(cur);
            if (style.display === "none" || style.visibility === "hidden") return false;
            cur = cur.parentElement;
        }
        return true;
    };
    // Authoritative container per docs/dom-specs/boss/chat-list/README.md L10.
    const candidates = [".user-list"];
    const seen = [];
    for (const sel of candidates) {
        const el = document.querySelector(sel);
        if (!el) continue;
        const rect = el.getBoundingClientRect();
        seen.push({
            sel, cls: (el.className || "").slice(0, 100),
            li_count: el.querySelectorAll("li").length,
            child_count: el.childElementCount,
            h: Math.round(rect.height), w: Math.round(rect.width),
            hidden: rect.width === 0 || rect.height === 0
        });
    }
    // `.user-list li` (descendant) is the live-DOM row selector as of
    // 2026-04-22 (trace_24ecd22aa795: direct-child `>` matched 0 while
    // descendant matched 2, aligned with user-visible "未读(2)" tab).
    // If this count > 0 but _extract returned [] → row-level field
    // selectors drifted. If 0 while body_li > 0 → BOSS re-wrapped the
    // list container; re-dump live DOM and mirror it in dom-specs.
    const pinnedRowCount = document.querySelectorAll(".user-list li").length;
    const visibleRowCount = Array.from(
        document.querySelectorAll(".user-list li")
    ).filter(isVisible).length;
    const unreadBadgeCount = document.querySelectorAll(
        ".user-list .figure .notice-badge"
    ).length;
    const visibleUnreadBadgeCount = Array.from(
        document.querySelectorAll(".user-list .figure .notice-badge")
    ).filter(isVisible).length;
    return {
        ready_state: document.readyState,
        visible: !document.hidden,
        body_li: document.querySelectorAll("body li").length,
        pinned_row_count: pinnedRowCount,
        visible_row_count: visibleRowCount,
        unread_badge_count: unreadBadgeCount,
        visible_unread_badge_count: visibleUnreadBadgeCount,
        containers: seen
    };
}"""


def _ensure_chat_list_hydrated(page: Any) -> str:
    """Kick BOSS chat SPA into rendering the list; return matched container
    selector or ``""`` if budget exhausted. See ADR-005 §7.6.

    Fail-loud contract: if playwright raises (page closed, evaluate crashes),
    let it propagate — that IS an executor_error, not "empty inbox".
    """
    t0 = time.monotonic()
    page.bring_to_front()
    page.evaluate(_CHAT_HYDRATE_FOCUS_JS)

    # Single-selector hydrate: ``.user-list`` is the authoritative container
    # per docs/dom-specs/boss/chat-list/README.md L10. The previous 5-selector
    # candidate list spent 8s-per-selector timeouts before landing on the
    # right one (post-mortem trace_ff91c91b0aaf: hydrate elapsed_ms=17462
    # because the first two misses each burned 8s). VPN-friendly budget
    # default bumped 8000 → 15000 (still env-overridable via
    # PULSE_BOSS_CHAT_HYDRATE_MS). Fail-loud semantics unchanged.
    budget_ms = _safe_int(
        os.getenv("PULSE_BOSS_CHAT_HYDRATE_MS", "15000"),
        15000, min_value=1000, max_value=60000,
    )
    selectors = _csv_list(os.getenv("PULSE_BOSS_CHAT_LIST_CONTAINER_SELECTORS", "")) or [
        ".user-list",
    ]
    _, matched = _wait_for_any_selector(page, selectors, timeout_ms=budget_ms)
    logger.info(
        "chat_list hydrate elapsed_ms=%d matched=%s",
        int((time.monotonic() - t0) * 1000), matched or "none",
    )
    return matched


def _diagnose_empty_chat_list(page: Any) -> dict[str, Any]:
    """Dump chat-list container DOM state (sel / cls / li_count / size /
    hidden) for log when rows==0. Fail-loud: page death propagates."""
    dom = page.evaluate(_CHAT_LIST_DIAGNOSE_JS) or {}
    return {
        "url": str(page.url or "")[:180],
        "ready_state": dom.get("ready_state", ""),
        "visible": bool(dom.get("visible", False)),
        "body_li": int(dom.get("body_li") or 0),
        "pinned_row_count": int(dom.get("pinned_row_count") or 0),
        "visible_row_count": int(dom.get("visible_row_count") or 0),
        "unread_badge_count": int(dom.get("unread_badge_count") or 0),
        "visible_unread_badge_count": int(dom.get("visible_unread_badge_count") or 0),
        "containers": list(dom.get("containers") or []),
    }


def _chat_list_first_row_signature(page: Any) -> str:
    """Content-hash-like signature of the first chat-list row.

    Purpose: lightweight "did the list re-render?" probe used inside
    ``_switch_chat_tab`` after clicking a tab. Reads only the top row's
    ``hr_name`` / ``company`` / ``notice-badge`` / ``time`` — same fields
    the chat-list DOM contract anchors on (see
    ``docs/dom-specs/boss/chat-list/README.md``), but no dedupe / no
    slice-to-max / no sha1, so it stays cheap to call in a 200 ms poll.

    Returns ``""`` when the list is empty or every candidate selector
    failed. Callers use equality on the **string** — empty→empty still
    compares equal, which is the correct "nothing changed" answer.
    """
    selectors = _csv_list(os.getenv("PULSE_BOSS_CHAT_ROW_SELECTORS", ""))
    if not selectors:
        selectors = _default_chat_row_selectors()
    for selector in selectors:
        try:
            sig = page.eval_on_selector(
                selector,
                """(node) => {
                    const textOf = el => el ? (el.innerText || "").trim() : "";
                    const nameEl = node.querySelector(".name-box > .name-text");
                    const otherSpans = Array.from(
                        node.querySelectorAll(".name-box > span")
                    ).filter(s => !s.classList.contains("name-text"));
                    const companyEl = otherSpans[0] || null;
                    const badgeEl = node.querySelector(".figure .notice-badge");
                    const badge = badgeEl ? (badgeEl.innerText || "").trim() : "";
                    return [
                        textOf(nameEl),
                        textOf(companyEl),
                        badge,
                        textOf(node.querySelector(".time")),
                    ].join("|");
                }""",
            )
            if isinstance(sig, str):
                return sig
        except Exception:
            # eval_on_selector throws when zero elements match — try next
            # selector in the chain before giving up.
            continue
    return ""


def _switch_chat_tab(page: Any, *, chat_tab: str) -> str:
    """Click the requested BOSS inbox tab and wait until the list really
    re-renders.

    Why snapshot-based wait instead of ``wait_for_timeout(700)``:
    BOSS inbox tab switch is a client-side filter. The active-tab class
    and the exact XHR that repaints the list are NOT part of our locked
    chat-list DOM contract (see README §跨 tab 未验证). The one signal
    we can trust without a fresh DOM dump is "the first row's content
    changed from its pre-click snapshot".

    Branches:
      * snapshot changed within ``PULSE_BOSS_CHAT_TAB_WAIT_MS`` → success,
        return the tab selector we clicked.
      * timeout without change → either we were already on the target
        tab (click is idempotent) OR render is genuinely slow. Either
        way the extractor's own resilient retry covers residual risk;
        log a debug line and return the selector (expected path, not
        fail-loud).
      * click itself raised → fail-loud, return ``""``.
    """
    safe_tab = str(chat_tab or "").strip()
    if not safe_tab:
        return ""
    safe_tab_selector = safe_tab.replace("\\", "\\\\").replace("'", "\\'")
    selectors = _csv_list(os.getenv("PULSE_BOSS_CHAT_TAB_SELECTORS", ""))
    if not selectors:
        selectors = [
            f"text={safe_tab}",
            f"button:has-text('{safe_tab_selector}')",
            f"a:has-text('{safe_tab_selector}')",
            f"li:has-text('{safe_tab_selector}')",
        ]
    loc, selector = _wait_for_any_selector(page, selectors, timeout_ms=min(3500, _browser_timeout_ms()))
    if loc is None:
        return ""

    # A valid baseline signature is non-empty: it proves ".user-list" has
    # at least one rendered row, so a later "signature changed" poll can
    # distinguish "tab filter re-rendered the list" from "list just
    # finished hydrating from empty". Without this guard, empty→filled
    # was silently logged as "chat_tab switched waited_ms=0", misleading
    # every post-mortem (checklist §证据先于推断).
    before_sig = ""
    baseline_wait_ms = _safe_int(
        os.getenv("PULSE_BOSS_CHAT_BASELINE_MS", "2000"),
        2000,
        min_value=300,
        max_value=8000,
    )
    baseline_waited = 0
    poll_ms = 150
    while baseline_waited < baseline_wait_ms:
        before_sig = _chat_list_first_row_signature(page)
        if before_sig:
            break
        try:
            page.wait_for_timeout(poll_ms)
        except Exception:
            time.sleep(poll_ms / 1000.0)
        baseline_waited += poll_ms

    if not before_sig:
        # List never rendered any row within the baseline window. Downstream
        # extractor still anchors on ".notice-badge" so it can cope with a
        # still-hydrating list, but we refuse to pretend the tab click had
        # a verified effect.
        logger.warning(
            "chat_tab baseline_empty tab=%s baseline_waited_ms=%d "
            "(click skipped; extractor anchors on notice-badge)",
            safe_tab,
            baseline_waited,
        )
        return ""

    try:
        loc.click(timeout=min(_browser_timeout_ms(), 6000))
    except Exception:
        return ""

    # Soft-wait the filter XHR; ignored if BOSS keeps long-poll open
    # (which it does) — the snapshot poll below is the real judge.
    try:
        page.wait_for_load_state("networkidle", timeout=min(_browser_timeout_ms(), 2000))
    except Exception:
        pass

    wait_ms = _safe_int(
        os.getenv("PULSE_BOSS_CHAT_TAB_WAIT_MS", "3000"),
        3000,
        min_value=300,
        max_value=10000,
    )
    poll_ms = 200
    waited = 0
    while waited < wait_ms:
        current_sig = _chat_list_first_row_signature(page)
        if current_sig and current_sig != before_sig:
            logger.info(
                "chat_tab switched tab=%s waited_ms=%d",
                safe_tab,
                waited,
            )
            return selector
        try:
            page.wait_for_timeout(poll_ms)
        except Exception:
            time.sleep(poll_ms / 1000.0)
        waited += poll_ms

    # Snapshot unchanged for the whole budget. Expected when the user was
    # already on this tab; also happens on an unexpectedly slow render.
    # Downstream resilient extract absorbs the latter, so we do not raise.
    logger.info(
        "chat_tab snapshot_unchanged tab=%s waited_ms=%d sig_head=%s",
        safe_tab,
        waited,
        (before_sig or "")[:60],
    )
    return selector


def _looks_like_chat_time_token(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    if text in {"刚刚", "昨天", "前天"}:
        return True
    if re.match(r"^\d{1,2}:\d{2}$", text):
        return True
    if re.match(r"^\d{1,2}月\d{1,2}日$", text):
        return True
    if re.match(r"^\d{4}-\d{1,2}-\d{1,2}$", text):
        return True
    return False


def _parse_conversation_header(header: str) -> tuple[str, str, str]:
    text = re.sub(r"\s+", " ", str(header or "").strip())
    if not text:
        return "Unknown HR", "Unknown", "Unknown Job"

    hr_name = text[:20]
    tail = ""
    for marker in ("先生", "女士", "老师", "经理", "总监", "主管"):
        idx = text.find(marker)
        if idx > 0:
            hr_name = text[: idx + len(marker)]
            tail = text[idx + len(marker) :].strip()
            break
    if not tail:
        if len(text) >= 5:
            hr_name = text[:3]
            tail = text[3:].strip()
        else:
            tail = ""

    company = tail[:40] if tail else "Unknown"
    job_title = "招聘沟通" if ("招聘" in text or "hr" in text.lower()) else "Unknown Job"
    return hr_name[:40], company[:80], job_title[:80]


def _extract_conversations_from_body_text(page: Any, *, max_items: int) -> list[dict[str, Any]]:
    try:
        body = str(page.inner_text("body") or "")
    except Exception:
        body = ""
    if not body.strip():
        return []

    lines = [re.sub(r"\s+", " ", line).strip() for line in body.splitlines()]
    lines = [line for line in lines if line]
    skip_tokens = {
        "首页",
        "职位",
        "公司",
        "校园",
        "APP",
        "有了",
        "海外",
        "无障碍专区",
        "在线客服",
        "消息",
        "简历",
        "全部",
        "未读",
        "新招呼",
        "更多",
        "AI筛选",
    }
    cleaned = [line for line in lines if line not in skip_tokens]

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    index = 0
    while index < len(cleaned) and len(rows) < max(1, max_items):
        token = cleaned[index]
        if not _looks_like_chat_time_token(token):
            index += 1
            continue

        header = cleaned[index + 1] if index + 1 < len(cleaned) else ""
        message = ""
        probe = index + 2
        while probe < len(cleaned):
            candidate = cleaned[probe]
            if _looks_like_chat_time_token(candidate):
                break
            if candidate in {"[送达]", "[已读]", "[未读]"}:
                probe += 1
                continue
            message = candidate
            break

        if header and message:
            hr_name, company, job_title = _parse_conversation_header(header)
            conversation_id = hashlib.sha1(f"{token}-{header}-{message}".encode("utf-8")).hexdigest()[:16]
            if conversation_id not in seen:
                seen.add(conversation_id)
                rows.append(
                    {
                        "conversation_id": conversation_id,
                        "hr_name": hr_name,
                        "company": company,
                        "job_title": job_title,
                        "latest_message": token_preview(message, max_tokens=1000),
                        "latest_time": token[:40],
                        "unread_count": 0,
                        "source": "boss_mcp_browser_chat",
                    }
                )
        index = max(index + 1, probe if probe > index else index + 1)

    return rows[: max(1, max_items)]


def _extract_job_leads_from_chat_page(
    page: Any,
    *,
    keyword: str,
    max_items: int,
    seen_keys: set[str],
) -> list[dict[str, Any]]:
    conversations = _extract_conversations_from_body_text(page, max_items=max(1, max_items * 2))
    if not conversations:
        return []
    rows: list[dict[str, Any]] = []
    for conv in conversations:
        conversation_id = str(conv.get("conversation_id") or "").strip()
        company = str(conv.get("company") or "").strip() or "Unknown"
        job_title = str(conv.get("job_title") or "").strip()
        if not job_title or job_title.lower().startswith("unknown"):
            job_title = f"{keyword} 相关岗位"
        latest_message = str(conv.get("latest_message") or "").strip()
        source_url = _build_chat_url(conversation_id) if conversation_id else _chat_list_url()
        dedupe_key = f"{company}-{job_title}-{source_url}".lower()
        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)
        rows.append(
            {
                "job_id": hashlib.sha1(source_url.encode("utf-8")).hexdigest()[:16],
                "title": job_title[:120],
                "company": company[:80],
                "salary": None,
                "source_url": source_url,
                "snippet": token_preview(latest_message, max_tokens=700),
                "source": "boss_mcp_browser_chat_lead",
                "collected_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        if len(rows) >= max(1, max_items):
            break
    return rows


def _extract_conversations_from_page(page: Any, *, max_items: int) -> list[dict[str, Any]]:
    """Read BOSS chat-list rows according to the locked DOM contract.

    Contract source of truth: ``docs/dom-specs/boss/chat-list/README.md``.

    Row shape (per item):
        - ``conversation_id``: content-hash id (chat-list ``<li>`` has no
          ``data-*`` attribute; SPA click is the only way to open it, so a
          stable hash over visible fields is the physical ground truth).
        - ``hr_name`` / ``company`` / ``job_title``: real innerText or ``""``.
          Empty string means the DOM genuinely didn't expose it — callers
          upstream (``pull_conversations`` in this same module, line ~2529)
          already filter out rows with any of these four empty. We fail loud
          instead of filling ``"Unknown"`` placeholders.
        - ``latest_message`` / ``latest_time``: real innerText or ``""``.
        - ``unread_count``: integer parsed from ``.notice-badge`` innerText;
          ``0`` when the red dot is absent.
        - ``my_last_sent_status``: ``"status-read"`` / ``"status-delivery"`` /
          ``""``. Non-empty iff the preview line shows the "已读/未读" icon
          (i.e. *my* last outgoing message on this row). Consumers can use it
          to tell "HR hasn't opened my greet yet" vs. "HR read and moved on".
        - ``source``: fixed ``"boss_mcp_browser_chat"``.

    Design notes (see ``docs/code-review-checklist.md`` §类型A/B):
      - No ``"Unknown HR"`` / ``"刚刚"`` placeholder falsification.
      - No text-body fallback on empty result: the body_text path produces
        regex-guessed ``"Unknown *"`` tokens that silently poison downstream
        matcher/dedupe. If the DOM reader returns nothing the caller sees
        an empty list and handles it loudly.
    """

    selectors = _csv_list(os.getenv("PULSE_BOSS_CHAT_ROW_SELECTORS", ""))
    if not selectors:
        selectors = _default_chat_row_selectors()
    raw_rows: list[dict[str, Any]] = []
    for selector in selectors:
        rows = page.eval_on_selector_all(
            selector,
            # Contract mapping (chat-list README §Selector 合同):
            #   .name-box > .name-text              → HR 姓名
            #   .name-box > span:not(.name-text)    → 公司 (第0) / 岗位 (第1),
            #                                         无 class 靠位置
            #   .figure > .notice-badge             → 未读红点, 存在即未读
            #   .last-msg-text                      → 最后消息预览
            #   .time                               → 最后时间
            #   .message-status.status-read|...     → 我方送达/已读 (iff 我发的)
            """nodes => nodes.map(node => {
                if (!node) return null;
                const role = (node.getAttribute("role") || "").toLowerCase();
                if (role && role !== "listitem") return null;
                if (node.getClientRects().length === 0) return null;
                let cur = node;
                while (cur && cur instanceof Element) {
                    const style = window.getComputedStyle(cur);
                    if (style.display === "none" || style.visibility === "hidden") {
                        return null;
                    }
                    cur = cur.parentElement;
                }
                const textOf = el => el ? (el.innerText || "").trim() : "";
                const nameEl = node.querySelector(".name-box > .name-text");
                if (!nameEl) return null;
                const otherSpans = Array.from(
                    node.querySelectorAll(".name-box > span")
                ).filter(s => !s.classList.contains("name-text"));
                const companyEl = otherSpans[0] || null;
                const jobEl = otherSpans[1] || null;
                const badgeEl = node.querySelector(".figure .notice-badge");
                let unread = 0;
                if (badgeEl) {
                    const raw = (badgeEl.innerText || "").trim();
                    const m = raw.match(/\\d+/);
                    unread = m ? parseInt(m[0], 10) : 1;
                }
                const statusEl = node.querySelector(".message-status");
                let myStatus = "";
                if (statusEl) {
                    if (statusEl.classList.contains("status-read")) myStatus = "status-read";
                    else if (statusEl.classList.contains("status-delivery")) myStatus = "status-delivery";
                }
                return {
                    hr_name: textOf(nameEl),
                    company: textOf(companyEl),
                    job_title: textOf(jobEl),
                    latest_message: textOf(node.querySelector(".last-msg-text")),
                    latest_time: textOf(node.querySelector(".time")),
                    unread_count: unread,
                    my_last_sent_status: myStatus,
                };
            }).filter(Boolean)""",
        )
        if isinstance(rows, list) and rows:
            for row in rows:
                if isinstance(row, dict):
                    raw_rows.append(dict(row))
            break

    return _normalize_conversation_rows(raw_rows, max_items=max_items)


def _normalize_conversation_rows(
    raw_rows: list[dict[str, Any]],
    *,
    max_items: int,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in raw_rows:
        hr_name = str(row.get("hr_name") or "").strip()
        company = str(row.get("company") or "").strip()
        job_title = str(row.get("job_title") or "").strip()
        latest_message = str(row.get("latest_message") or "").strip()
        seed = f"{hr_name}|{company}|{job_title}|{latest_message}"
        if not seed.replace("|", "").strip():
            continue
        conversation_id = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]
        if conversation_id in seen:
            continue
        seen.add(conversation_id)
        result.append(
            {
                "conversation_id": conversation_id,
                "conversation_url": _build_chat_url(conversation_id),
                "hr_name": hr_name[:80],
                "company": company[:120],
                "job_title": job_title[:160],
                "latest_message": token_preview(latest_message, max_tokens=1000),
                "latest_time": str(row.get("latest_time") or "").strip()[:40],
                "unread_count": max(
                    0,
                    min(_safe_int(row.get("unread_count"), 0, min_value=0, max_value=99), 99),
                ),
                "my_last_sent_status": str(row.get("my_last_sent_status") or "").strip()[:32],
                "source": "boss_mcp_browser_chat",
            }
        )
        if len(result) >= max(1, max_items):
            break
    return result


def _extract_unread_conversations_by_badge(
    page: Any,
    *,
    max_items: int,
) -> list[dict[str, Any]]:
    cap = max(1, int(max_items))
    try:
        rows = page.evaluate(
            """(maxItems) => {
                const textOf = el => el ? (el.innerText || "").trim() : "";
                const cap = Number(maxItems) > 0 ? Number(maxItems) : 20;
                const isVisible = (el) => {
                    if (!el || !(el instanceof Element)) return false;
                    if (el.getClientRects().length === 0) return false;
                    let cur = el;
                    while (cur && cur instanceof Element) {
                        const style = window.getComputedStyle(cur);
                        if (style.display === "none" || style.visibility === "hidden") return false;
                        cur = cur.parentElement;
                    }
                    return true;
                };
                const badgeNodes = Array.from(
                    document.querySelectorAll(".user-list .figure .notice-badge")
                );
                const rowNodes = [];
                const seenNode = new Set();
                for (const badgeEl of badgeNodes) {
                    if (!isVisible(badgeEl)) continue;
                    const row = badgeEl.closest("li");
                    if (!row || seenNode.has(row)) continue;
                    const role = (row.getAttribute("role") || "").toLowerCase();
                    if (role && role !== "listitem") continue;
                    if (!isVisible(row)) continue;
                    seenNode.add(row);
                    rowNodes.push(row);
                }
                return rowNodes.slice(0, cap).map(node => {
                    const nameEl = node.querySelector(".name-box > .name-text");
                    if (!nameEl) return null;
                    const otherSpans = Array.from(
                        node.querySelectorAll(".name-box > span")
                    ).filter(s => !s.classList.contains("name-text"));
                    const companyEl = otherSpans[0] || null;
                    const jobEl = otherSpans[1] || null;
                    const badgeEl = node.querySelector(".figure .notice-badge");
                    const rawBadge = textOf(badgeEl);
                    const m = rawBadge.match(/\\d+/);
                    const unread = m ? parseInt(m[0], 10) : 1;
                    const statusEl = node.querySelector(".message-status");
                    let myStatus = "";
                    if (statusEl) {
                        if (statusEl.classList.contains("status-read")) myStatus = "status-read";
                        else if (statusEl.classList.contains("status-delivery")) myStatus = "status-delivery";
                    }
                    return {
                        hr_name: textOf(nameEl),
                        company: textOf(companyEl),
                        job_title: textOf(jobEl),
                        latest_message: textOf(node.querySelector(".last-msg-text")),
                        latest_time: textOf(node.querySelector(".time")),
                        unread_count: unread,
                        my_last_sent_status: myStatus,
                    };
                }).filter(Boolean);
            }""",
            cap,
        )
    except Exception:
        return []
    if not isinstance(rows, list):
        return []
    safe_rows = [dict(row) for row in rows if isinstance(row, dict)]
    return _normalize_conversation_rows(safe_rows, max_items=cap)


def _resilient_extract_conversations_from_page(
    page: Any,
    *,
    max_items: int,
) -> list[dict[str, Any]]:
    """Call :func:`_extract_conversations_from_page` with short retries.

    Rationale: after a BOSS tab switch the conversation ``<li>`` nodes
    are re-rendered asynchronously; ``_switch_chat_tab`` tries to wait
    for the pre/post snapshot to differ, but in two cases a single-shot
    extractor still reads nothing:

    * the snapshot-wait path timed out because BOSS held the long-poll
      open past our 3 s budget;
    * the user was already on the target tab, so the first snapshot
      comparison could never differ — we returned immediately but the
      list itself is also still in a transient empty layout.

    We mirror the structure of ``_resilient_extract_jobs_from_page``
    (this same module) so both scanners age together. No ``mouse.wheel``
    here — the BOSS unread inbox fits above the fold and is virtualized
    the same way whether we scroll or not.

    Attempts are capped by ``PULSE_BOSS_CHAT_EXTRACT_ATTEMPTS``
    (default 3, min 1, max 6); each miss waits up to 2 s for
    ``networkidle`` plus a 300 / 500 / 700 … ms progressive back-off.
    """

    attempts = max(
        1,
        _safe_int(
            os.getenv("PULSE_BOSS_CHAT_EXTRACT_ATTEMPTS", "3"),
            3,
            min_value=1,
            max_value=6,
        ),
    )
    rows: list[dict[str, Any]] = []
    for attempt in range(attempts):
        rows = _extract_conversations_from_page(page, max_items=max_items)
        if rows:
            if attempt > 0:
                logger.info(
                    "chat_list extract recovered attempt=%d rows=%d",
                    attempt + 1,
                    len(rows),
                )
            return rows
        try:
            page.wait_for_load_state(
                "networkidle", timeout=min(_browser_timeout_ms(), 2000)
            )
        except Exception:
            pass
        try:
            page.wait_for_timeout(300 + min(900, attempt * 200))
        except Exception:
            time.sleep((300 + min(900, attempt * 200)) / 1000.0)
    logger.info("chat_list extract empty attempts=%d", attempts)
    return rows


# ---------------------------------------------------------------------------
# chat-detail Reader (纯读 API)
#
# 合同: docs/dom-specs/boss/chat-detail/README.md  (A / B / C / D 分区)
#
# 范围约定: 本层只提供 "把当前展开的 chat-conversation 渲染结果结构化" 的
# 能力, 不做任何 Actuator (点击 / 输入 / 导航). Actuator 留给 ADR-004
# (尚未立项) 配合决策层一起落地.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChatDetailLastMessage:
    """合同 §C 中 ``<li.message-item>`` 的最后一条消息快照."""

    sender: str  # "me" | "friend" | "bot" | "unknown"
    kind: str    # "text" | "card" | "unknown"
    text: str    # 文字消息的 innerText, 或卡片的标题行
    data_mid: str  # BOSS 后端消息 id; 空串=DOM 未带 data-mid


@dataclass(frozen=True)
class ChatDetailPendingRespond:
    """合同 §C.4 的 ``.respond-popover`` 浮动响应条."""

    text: str       # 例: "我想要一份您的附件简历，您是否同意"
    has_agree: bool
    has_refuse: bool


@dataclass(frozen=True)
class ChatDetailState:
    """对当前 chat-detail 面板的只读视图.

    字段覆盖合同 A~D 中 "Reader 可靠拿到 / 自动回复决策会读" 的部分; 不包
    含完整消息时间线 (YAGNI: ADR-004 决策层决定需要再扩) 和多媒体消息形态
    (尚未采样).
    """

    hr_name: str          # 合同 §A: .base-info .name-text
    hr_company: str       # 合同 §A: .base-info > span:not(.base-title)[0]
    hr_title: str         # 合同 §A: .base-info .base-title
    position_name: str    # 合同 §B: .chat-position-content .position-name
    position_salary: str  # 合同 §B: 未脱敏真实薪资 ★ greet 修正应走这里
    position_city: str    # 合同 §B: .chat-position-content .city
    last_message: ChatDetailLastMessage | None
    pending_respond: ChatDetailPendingRespond | None
    send_resume_button_present: bool  # 合同 §D.1 发简历按钮 (文本定位)


_CHAT_DETAIL_JS = r"""
() => {
    const root = document.querySelector(".chat-conversation");
    if (!root) return null;
    const textOf = el => el ? (el.innerText || "").trim() : "";

    // ---- §A HR 顶部身份 ---------------------------------------------------
    const baseInfo = root.querySelector(".base-info");
    const hrName = textOf(root.querySelector(".base-info .name-text"));
    const hrTitle = textOf(root.querySelector(".base-info .base-title"));
    let hrCompany = "";
    if (baseInfo) {
        // 合同 §A: 公司无 class, 排除 .base-title 后第 0 个直接子 span
        const spans = Array.from(baseInfo.querySelectorAll(":scope > span"))
            .filter(s => !s.classList.contains("base-title"));
        hrCompany = textOf(spans[0]);
    }

    // ---- §B 岗位摘要 (真实薪资) -------------------------------------------
    const positionName = textOf(root.querySelector(".chat-position-content .position-name"));
    const positionSalary = textOf(root.querySelector(".chat-position-content .salary"));
    const positionCity = textOf(root.querySelector(".chat-position-content .city"));

    // ---- §C 最后一条消息 --------------------------------------------------
    const lis = root.querySelectorAll(".im-list > li.message-item");
    let lastMessage = null;
    if (lis.length > 0) {
        const li = lis[lis.length - 1];
        const isMyself = li.classList.contains("item-myself");
        const isFriend = li.classList.contains("item-friend");
        const hrCard = li.querySelector(".message-card-wrap.boss-green");
        const botCard = li.querySelector(".message-card-wrap.blue");
        let sender = "unknown";
        if (isMyself) sender = "me";
        else if (isFriend) sender = botCard ? "bot" : "friend";

        let kind = "text";
        let textVal = "";
        if (hrCard) {
            kind = "card";
            textVal = textOf(hrCard.querySelector(".message-card-top-title"));
        } else if (botCard) {
            kind = "card";
            textVal = textOf(botCard.querySelector(".message-card-top-title"));
        } else {
            // 文字消息: 合同 §C.1 的 p > span 嵌套, 退 p 也可
            const span = li.querySelector(".text p span");
            const p = li.querySelector(".text p");
            textVal = textOf(span || p);
        }
        lastMessage = {
            sender: sender,
            kind: kind,
            text: textVal,
            data_mid: li.getAttribute("data-mid") || "",
        };
    }

    // ---- §C.4 浮动响应条 (自动回复 strong signal) -------------------------
    const popover = root.querySelector(".message-tip-bar .respond-popover");
    let pending = null;
    if (popover) {
        pending = {
            text: textOf(popover.querySelector(".text")),
            has_agree: !!popover.querySelector(".btn.btn-agree"),
            has_refuse: !!popover.querySelector(".btn.btn-refuse"),
        };
    }

    // ---- §D.1 "发简历" 按钮存在性 -----------------------------------------
    // 合同: 无独立 class, 必须靠 innerText === "发简历" 精确匹配.
    let sendResumePresent = false;
    const toolbarBtns = root.querySelectorAll(".chat-controls .toolbar-btn");
    toolbarBtns.forEach(btn => {
        if ((btn.innerText || "").trim() === "发简历") {
            sendResumePresent = true;
        }
    });

    return {
        hr_name: hrName,
        hr_company: hrCompany,
        hr_title: hrTitle,
        position_name: positionName,
        position_salary: positionSalary,
        position_city: positionCity,
        last_message: lastMessage,
        pending_respond: pending,
        send_resume_button_present: sendResumePresent,
    };
}
"""


def extract_chat_detail_state(page: Any) -> ChatDetailState | None:
    """Read structured state of the currently-opened chat-detail panel.

    Returns ``None`` iff ``.chat-conversation`` root is absent, i.e. no
    conversation is opened (left list only, or page just navigated and SPA
    hasn't rendered yet). All fields are a direct, non-falsifying map of the
    contract in ``docs/dom-specs/boss/chat-detail/README.md``; empty strings
    mean "DOM didn't expose this field in this sample" (fail-loud rather than
    synthesize placeholders — see ``docs/code-review-checklist.md`` §类型A).

    This function does **not** click, type, or navigate; Actuator-layer
    operations (``click_respond_popover`` / ``click_send_resume`` / ...) are
    deferred to ADR-004 together with the auto-reply decision module.
    """

    payload = page.evaluate(_CHAT_DETAIL_JS)
    if not isinstance(payload, dict):
        return None

    last_raw = payload.get("last_message")
    last_message: ChatDetailLastMessage | None = None
    if isinstance(last_raw, dict):
        last_message = ChatDetailLastMessage(
            sender=str(last_raw.get("sender") or "unknown"),
            kind=str(last_raw.get("kind") or "unknown"),
            text=str(last_raw.get("text") or ""),
            data_mid=str(last_raw.get("data_mid") or ""),
        )

    pending_raw = payload.get("pending_respond")
    pending: ChatDetailPendingRespond | None = None
    if isinstance(pending_raw, dict):
        pending = ChatDetailPendingRespond(
            text=str(pending_raw.get("text") or ""),
            has_agree=bool(pending_raw.get("has_agree")),
            has_refuse=bool(pending_raw.get("has_refuse")),
        )

    return ChatDetailState(
        hr_name=str(payload.get("hr_name") or ""),
        hr_company=str(payload.get("hr_company") or ""),
        hr_title=str(payload.get("hr_title") or ""),
        position_name=str(payload.get("position_name") or ""),
        position_salary=str(payload.get("position_salary") or ""),
        position_city=str(payload.get("position_city") or ""),
        last_message=last_message,
        pending_respond=pending,
        send_resume_button_present=bool(payload.get("send_resume_button_present")),
    )


# ---------------------------------------------------------------------------
# Auto-reply decision layer (pure function; no IO, no page access)
#
# Spec: docs/adr/ADR-004-AutoReplyContract.md §4.1 / §4.2
# DOM contract: docs/dom-specs/boss/chat-detail/README.md §E (decision tree)
# ---------------------------------------------------------------------------


AutoReplyKind = Literal[
    "skip",
    "click_respond_agree",
    "click_respond_refuse",
    "click_send_resume",
]


@dataclass(frozen=True)
class AutoReplyDecision:
    """Single-conversation decision produced by ``decide_auto_reply_action``.

    ``trigger_mid`` is the ``data-mid`` of the HR message that drove the
    decision; combined with (conversation_id, kind) it forms the idempotency
    key so the same incoming message never gets responded twice within the
    MCP idempotency window (see ADR-001 §6 P3e).
    """

    kind: AutoReplyKind
    reason: str
    trigger_mid: str


def _decision_skip(reason: str, *, trigger_mid: str = "") -> AutoReplyDecision:
    return AutoReplyDecision(kind="skip", reason=reason[:160], trigger_mid=trigger_mid)


def decide_auto_reply_action(state: ChatDetailState | None) -> AutoReplyDecision:
    """Rule-based decision for one chat-detail snapshot.

    v1 intentionally does NOT call any LLM (see ADR-004 §3); the two
    high-signal cases (HR 简历请求卡 / HR 纯文字招呼) map cleanly to two
    concrete click actions. Everything else → SKIP with a human-readable
    reason for audit.

    The function is pure: given the same ``ChatDetailState`` it always
    returns the same ``AutoReplyDecision``. This makes it trivially unit
    testable from real chat-detail DOM dumps.
    """

    if state is None:
        return _decision_skip("chat-detail 根节点未渲染")

    trigger_mid = ""
    if state.last_message is not None:
        trigger_mid = state.last_message.data_mid

    if state.pending_respond is not None:
        popover_text = state.pending_respond.text
        if "简历" in popover_text and state.pending_respond.has_agree:
            return AutoReplyDecision(
                kind="click_respond_agree",
                reason=f"HR 简历请求卡,text={popover_text[:80]}"[:160],
                trigger_mid=trigger_mid,
            )
        return _decision_skip(
            f"未识别的动作卡类型,text={popover_text[:80]}",
            trigger_mid=trigger_mid,
        )

    last = state.last_message
    if last is None:
        return _decision_skip("消息流为空")
    if last.sender == "me":
        return _decision_skip("最后一条是我方,已回过", trigger_mid=trigger_mid)
    if last.sender == "bot":
        return _decision_skip(
            f"机器人卡,忽略,text={last.text[:60]}",
            trigger_mid=trigger_mid,
        )
    if last.sender == "friend" and last.kind == "text":
        if state.send_resume_button_present:
            return AutoReplyDecision(
                kind="click_send_resume",
                reason=f"HR 纯文字,主动发简历,text={last.text[:80]}"[:160],
                trigger_mid=trigger_mid,
            )
        return _decision_skip(
            "HR 发文字但底部工具栏无'发简历'按钮",
            trigger_mid=trigger_mid,
        )

    return _decision_skip(
        f"未识别形态 sender={last.sender} kind={last.kind}",
        trigger_mid=trigger_mid,
    )


def _enrich_rows_with_latest_hr_message(
    page: Any,
    rows: list[dict[str, Any]],
) -> None:
    """点开每条未读对话,用右栏真实 HR 气泡覆盖 row.latest_message.

    契约锚点 (docs/dom-specs/boss/chat-detail/20260422T073442Z.html):
      左栏 row         -> .user-list li
      右栏主容器       -> .chat-conversation
      顶部 HR 姓名     -> .chat-conversation .top-info-content .name-text
      消息流容器       -> .chat-conversation .im-list
      HR 气泡 (我们要) -> li.message-item.item-friend .message-content
      我方气泡 (排除)  -> li.message-item.item-myself
      最新 HR 一条     -> 最后一个 .item-friend

    为什么必须进详情而不是沿用左栏 .last-msg-text:
      左栏预览在新会话 / HR 还没实际说话时会被 BOSS 渲染成系统占位
      ("您正在与Boss X沟通"), planner 误判 ignore. 人类处理未读的
      基本姿势是点开看真话, 这里复现同一路径 (ADR-004 §3).

    副作用: 点开一条 = BOSS 标已读. 这是期望行为 —— 确保下一轮 patrol
    不会重复处理同一条消息 (@code-review-checklist.md §A 避免重复侧效应).

    Fail-loud: click / wait / read 任一失败把原因写进 row["enrich_error"],
    不 swallow, service 层可感知并走错误上报.
    """
    if not rows:
        return
    locate_js = """(target) => {
        const items = Array.from(document.querySelectorAll(".user-list li"));
        const clean = (s) => ((s || "") + "").trim();
        return items.find(li => {
            const nameEl = li.querySelector(".name-box > .name-text");
            if (!nameEl) return false;
            const otherSpans = Array.from(
                li.querySelectorAll(".name-box > span")
            ).filter(s => !s.classList.contains("name-text"));
            const companyText = clean((otherSpans[0] || {}).innerText);
            const jobText = clean((otherSpans[1] || {}).innerText);
            return clean(nameEl.innerText) === target.hr_name
                && companyText === target.company
                && jobText === target.job_title;
        }) || null;
    }"""
    # 详情渲染是两步异步: 1) SPA 先换 .top-info-content 2) 再 fetch+渲染
    # .im-list 消息流. trace_1470872176ba 证实只等 #1 会在 im-list 还空时
    # 读到 text_len=0, planner 当系统占位 ignore. 所以这里必须三条齐备:
    #   a. top-info 切换到目标 HR
    #   b. .pre-loading loader 已经消失 (或本就不存在)
    #   c. .im-list 至少有 1 条 .message-item 节点
    await_detail_js = """([expected]) => {
        const root = document.querySelector(".chat-conversation");
        if (!root) return false;
        const name = root.querySelector(".top-info-content .name-text");
        if (!name || ((name.innerText || "") + "").trim() !== expected) {
            return false;
        }
        const loader = root.querySelector(".pre-loading");
        if (loader) {
            const rects = loader.getClientRects ? loader.getClientRects() : [];
            if (rects.length > 0) {
                const style = window.getComputedStyle(loader);
                if (style.display !== "none" && style.visibility !== "hidden") {
                    return false;
                }
            }
        }
        const items = root.querySelectorAll(".im-list .message-item");
        return items.length > 0;
    }"""
    read_hr_js = """() => {
        const items = Array.from(
            document.querySelectorAll(
                ".chat-conversation .im-list li.message-item.item-friend"
            )
        );
        if (!items.length) return "";
        const last = items[items.length - 1];
        const el = last.querySelector(".message-content");
        return el ? ((el.innerText || "") + "").trim() : "";
    }"""
    # 诊断用: enrich 读到空时一眼看清到底是"没 item-friend"、"有但
    # .message-content 是空"、还是整个 .im-list 没渲染. 下次不用再来回猜.
    diagnose_hr_js = """() => {
        const root = document.querySelector(".chat-conversation");
        if (!root) return { ok: false, reason: "no .chat-conversation root" };
        const list = root.querySelector(".im-list");
        if (!list) return { ok: false, reason: "no .im-list inside root" };
        const all = list.querySelectorAll(".message-item");
        const friends = list.querySelectorAll("li.message-item.item-friend");
        const myself = list.querySelectorAll("li.message-item.item-myself");
        const lastFriend = friends[friends.length - 1] || null;
        const lastFriendClass = lastFriend
            ? (lastFriend.className || "")
            : "";
        const lastFriendContentExists = lastFriend
            ? !!lastFriend.querySelector(".message-content")
            : false;
        const lastFriendInnerText = lastFriend
            ? ((lastFriend.innerText || "") + "").slice(0, 200)
            : "";
        return {
            ok: true,
            all_items: all.length,
            friend_items: friends.length,
            myself_items: myself.length,
            last_friend_class: lastFriendClass,
            last_friend_has_message_content: lastFriendContentExists,
            last_friend_inner_text: lastFriendInnerText,
        };
    }"""
    click_timeout_ms = min(_browser_timeout_ms(), 6000)
    detail_timeout_ms = min(_browser_timeout_ms(), 8000)
    for row in rows:
        target = {
            "hr_name": str(row.get("hr_name") or "").strip(),
            "company": str(row.get("company") or "").strip(),
            "job_title": str(row.get("job_title") or "").strip(),
        }
        if not target["hr_name"]:
            row["enrich_error"] = "row missing hr_name"
            continue
        li_handle = None
        try:
            li_handle = page.evaluate_handle(locate_js, target)
            element = li_handle.as_element() if li_handle is not None else None
            if element is None:
                row["enrich_error"] = "left-pane row not found in DOM"
                continue
            element.click(timeout=click_timeout_ms)
            page.wait_for_function(
                await_detail_js,
                arg=[target["hr_name"]],
                timeout=detail_timeout_ms,
            )
            current_chat_url = str(getattr(page, "url", "") or "").strip()
            if current_chat_url:
                row["conversation_url"] = current_chat_url
            text = str(page.evaluate(read_hr_js) or "").strip()
            row["latest_message"] = token_preview(text, max_tokens=1000)
            row["hr_has_spoken"] = bool(text)
            if text:
                logger.info(
                    "pull_conversations.enrich ok hr=%s company=%s text_len=%d",
                    target["hr_name"][:30],
                    target["company"][:40],
                    len(text),
                )
            else:
                # fail-loud: 读到空时必须把 DOM 现状抛出来, 不写入
                # row["enrich_error"] 避免上游把"新会话 HR 还没说话"
                # 当失败处理; 但 WARNING 级日志必须有, 才能下次一眼区分
                # "DOM 没渲染好 vs HR 真的没说话".
                try:
                    diag = page.evaluate(diagnose_hr_js)
                except Exception as diag_exc:
                    diag = {"ok": False, "reason": f"diag failed: {diag_exc}"}
                logger.warning(
                    "pull_conversations.enrich empty hr=%s company=%s diag=%s",
                    target["hr_name"][:30],
                    target["company"][:40],
                    diag,
                )
        except Exception as exc:
            row["enrich_error"] = f"enrich failed: {str(exc)[:240]}"
            logger.warning(
                "pull_conversations.enrich_failed hr=%s company=%s reason=%s",
                target["hr_name"][:30],
                target["company"][:40],
                row["enrich_error"],
            )
        finally:
            if li_handle is not None:
                try:
                    li_handle.dispose()
                except Exception:
                    pass


def _pull_conversations_via_browser(
    *,
    max_conversations: int,
    unread_only: bool,
    fetch_latest_hr: bool,
    chat_tab: str,
) -> dict[str, Any]:
    safe_max = _safe_int(max_conversations, 20, min_value=1, max_value=200)
    try:
        page = _ensure_browser_page()
    except Exception as exc:
        return {
            "ok": False,
            "status": "executor_unavailable",
            "items": [],
            "unread_total": 0,
            "source": "boss_mcp_browser_chat",
            "errors": [str(exc)[:300]],
        }

    target_url = _chat_list_url()
    try:
        page.goto(target_url, wait_until="domcontentloaded", timeout=_browser_timeout_ms())
    except Exception as exc:
        return {
            "ok": False,
            "status": "executor_error",
            "items": [],
            "unread_total": 0,
            "source": "boss_mcp_browser_chat",
            "errors": [f"chat page navigation failed: {str(exc)[:250]}"],
        }

    current_url = str(page.url or "")
    risk_status = _detect_runtime_risk(page, current_url=current_url)
    if risk_status:
        return {
            "ok": False,
            "status": risk_status,
            "items": [],
            "unread_total": 0,
            "source": "boss_mcp_browser_chat",
            "errors": [f"risk status={risk_status}"],
            "url": current_url,
        }

    # ADR-005 §7.6: hydrate + switch tab + extract + diagnose 整段在同一
    # 个 executor_error 边界里. 任何 playwright 异常 (page 已 close / JS
    # 执行失败) 必须翻译成 status=executor_error, 绝不能让异常吞成 rows=0
    # 的"空结果"伪装 (checklist §类型A "伪造空结果"零容忍).
    try:
        list_container = _ensure_chat_list_hydrated(page)
        if not list_container:
            return {
                "ok": False,
                "status": "no_result",
                "items": [],
                "unread_total": 0,
                "source": "boss_mcp_browser_chat",
                "errors": ["chat list container selector did not match"],
                "url": current_url,
            }
        tab_selector = _switch_chat_tab(page, chat_tab=chat_tab)
        rows = _resilient_extract_conversations_from_page(
            page, max_items=max(10, safe_max * 2)
        )
        raw_rows = len(rows)
        raw_unread_rows = sum(
            1
            for item in rows
            if _safe_int(item.get("unread_count"), 0, min_value=0, max_value=999) > 0
        )
        if unread_only:
            # First-principles contract for unread mode: source of truth is
            # the red badge currently visible in the unread tab list, not the
            # full cached conversation pool that BOSS may keep hidden in DOM.
            badge_rows = _extract_unread_conversations_by_badge(
                page, max_items=max(10, safe_max * 2)
            )
            if badge_rows:
                badge_unread_rows = sum(
                    1
                    for item in badge_rows
                    if _safe_int(item.get("unread_count"), 0, min_value=0, max_value=999) > 0
                )
                logger.info(
                    "pull_conversations unread_badge_primary raw_rows=%d selected_rows=%d "
                    "selected_unread_rows=%d tab=%s",
                    raw_rows,
                    len(badge_rows),
                    badge_unread_rows,
                    chat_tab,
                )
                rows = badge_rows
                raw_rows = len(rows)
                raw_unread_rows = badge_unread_rows
            elif raw_rows > 0 and raw_unread_rows == 0:
                logger.warning(
                    "pull_conversations unread_badge_primary_empty raw_rows=%d tab=%s",
                    raw_rows,
                    chat_tab,
                )
        if unread_only:
            rows = [
                item
                for item in rows
                if _safe_int(item.get("unread_count"), 0, min_value=0, max_value=999) > 0
            ]
        rows = rows[:safe_max]
        unread_total = sum(int(item.get("unread_count") or 0) for item in rows)
        logger.info(
            "pull_conversations_via_browser tab=%s unread_only=%s raw_rows=%d "
            "raw_unread_rows=%d rows=%d unread_total=%d fetch_latest_hr=%s",
            chat_tab, unread_only, raw_rows, raw_unread_rows, len(rows),
            unread_total, fetch_latest_hr,
        )
        if fetch_latest_hr and rows:
            _enrich_rows_with_latest_hr_message(page, rows)
            enriched_ok = sum(1 for item in rows if not item.get("enrich_error"))
            enriched_fail = len(rows) - enriched_ok
            hr_has_spoken = sum(1 for item in rows if item.get("hr_has_spoken"))
            logger.info(
                "pull_conversations_via_browser enrich_done rows=%d ok=%d "
                "fail=%d hr_has_spoken=%d",
                len(rows), enriched_ok, enriched_fail, hr_has_spoken,
            )
        if not rows:
            # Diagnostic snapshot is observability-only. If page dies after core
            # extraction finished, do not flip business outcome ("no_result")
            # into executor_error just because logging failed.
            try:
                diagnosis = _diagnose_empty_chat_list(page)
            except Exception as diag_exc:
                logger.warning(
                    "pull_conversations empty_snapshot skipped tab=%s reason=%s",
                    chat_tab,
                    str(diag_exc)[:220],
                )
            else:
                logger.warning(
                    "pull_conversations empty_snapshot tab=%s url=%s ready_state=%s "
                    "visible=%s body_li=%s pinned_row_count=%s visible_row_count=%s "
                    "unread_badge_count=%s visible_unread_badge_count=%s containers=%s",
                    chat_tab,
                    diagnosis["url"], diagnosis["ready_state"], diagnosis["visible"],
                    diagnosis["body_li"], diagnosis["pinned_row_count"],
                    diagnosis["visible_row_count"],
                    diagnosis["unread_badge_count"],
                    diagnosis["visible_unread_badge_count"],
                    diagnosis["containers"],
                )
    except Exception as exc:
        logger.exception("pull_conversations_via_browser playwright error")
        return {
            "ok": False,
            "status": "executor_error",
            "items": [],
            "unread_total": 0,
            "source": "boss_mcp_browser_chat",
            "errors": [f"chat extract failed: {str(exc)[:250]}"],
            "url": current_url,
        }
    return {
        # no_result (inbox genuinely empty of unread) is a legitimate success
        # state, not a failure. Only executor / auth / risk paths set ok=False.
        # See code-review-checklist.md §A "伪造空结果零容忍" (reverse face:
        # 不要把"真实空"伪装成"失败").
        "ok": True,
        "status": "ready" if rows else "no_result",
        "items": rows,
        "unread_total": unread_total,
        "source": "boss_mcp_browser_chat",
        "errors": [],
        "tab_selector": tab_selector or None,
    }


def _detect_runtime_risk(page: Any, *, current_url: str) -> str:
    if _is_login_page(current_url):
        return "auth_required"
    if _is_security_page(current_url):
        return "risk_blocked"
    if _contains_risk_keywords(current_url):
        return "risk_blocked"
    try:
        body_text = str(page.inner_text("body") or "")[:2500]
    except Exception:
        body_text = ""
    if _contains_risk_keywords(body_text):
        return "risk_blocked"
    return ""


def _run_browser_executor_with_retry(operation_name: str, executor: Any) -> dict[str, Any]:
    retry_count = _browser_executor_retry_count()
    backoff_ms = _browser_executor_retry_backoff_ms()
    final_result: dict[str, Any] = {}
    for attempt in range(retry_count + 1):
        result = executor()
        safe_result = dict(result) if isinstance(result, dict) else {"ok": False, "status": "executor_error"}
        safe_result["attempt"] = attempt + 1
        safe_result["max_attempts"] = retry_count + 1
        if bool(safe_result.get("ok")):
            final_result = safe_result
            break
        status = str(safe_result.get("status") or "").strip()
        retryable = status in {"executor_error", "selector_missing"}
        if attempt >= retry_count or not retryable:
            final_result = safe_result
            break
        sleep_ms = backoff_ms * (attempt + 1)
        time.sleep(max(0.05, sleep_ms / 1000.0))
    _append_action_log(
        {
            "action": f"{operation_name}_attempt_summary",
            "status": str(final_result.get("status") or ""),
            "ok": bool(final_result.get("ok")),
            "attempt": int(final_result.get("attempt") or 0),
            "max_attempts": int(final_result.get("max_attempts") or 0),
            "source": str(final_result.get("source") or ""),
            "error": str(final_result.get("error") or "")[:300] or None,
        }
    )
    return final_result


def _try_click_conversation_by_hint(page: Any, hint: dict[str, Any]) -> tuple[bool, str]:
    candidates: list[str] = []
    for key in ("hr_name", "company", "job_title", "hint_text"):
        value = str(hint.get(key) or "").strip()
        if value and value not in candidates:
            candidates.append(value)
    for text in candidates:
        try:
            locator = page.get_by_text(text, exact=False).first
            locator.wait_for(timeout=min(_browser_timeout_ms(), 3500))
            locator.click(timeout=min(_browser_timeout_ms(), 6000))
            return True, f"text:{text}"
        except Exception:
            continue
    return False, ""


def check_login(*, check_url: str = "") -> dict[str, Any]:
    target = str(check_url or "").strip() or str(
        os.getenv("PULSE_BOSS_LOGIN_CHECK_URL", "https://www.zhipin.com/web/geek/chat")
    ).strip()
    try:
        page = _ensure_browser_page()
    except Exception as exc:
        return {
            "ok": False,
            "status": "executor_unavailable",
            "source": "boss_mcp_browser",
            "error": str(exc)[:300],
            "url": target,
        }
    try:
        page.goto(target, wait_until="domcontentloaded", timeout=_browser_timeout_ms())
        current_url = str(page.url or "")
        risk_status = _detect_runtime_risk(page, current_url=current_url)
        if risk_status == "auth_required":
            return {
                "ok": False,
                "status": "auth_required",
                "source": "boss_mcp_browser",
                "error": "boss login is required",
                "url": current_url,
            }
        if risk_status == "risk_blocked":
            return {
                "ok": False,
                "status": "risk_blocked",
                "source": "boss_mcp_browser",
                "error": "risk or captcha page detected",
                "url": current_url,
            }
        return {
            "ok": True,
            "status": "ready",
            "source": "boss_mcp_browser",
            "error": None,
            "url": current_url,
        }
    except Exception as exc:
        return {
            "ok": False,
            "status": "executor_error",
            "source": "boss_mcp_browser",
            "error": str(exc)[:300],
            "url": str(getattr(page, "url", "") or target),
        }


def _execute_browser_reply(
    *,
    conversation_id: str,
    reply_text: str,
    profile_id: str,
    conversation_hint: dict[str, Any] | None = None,
) -> dict[str, Any]:
    _ = profile_id
    safe_conversation_id = str(conversation_id or "").strip()
    safe_reply = str(reply_text or "").strip()
    if not safe_conversation_id:
        return {
            "ok": False,
            "status": "failed",
            "source": "boss_mcp_browser",
            "error": "conversation_id is required",
        }
    if not safe_reply:
        return {
            "ok": False,
            "status": "failed",
            "source": "boss_mcp_browser",
            "error": "reply_text is required",
        }

    try:
        page = _ensure_browser_page()
    except Exception as exc:
        return {
            "ok": False,
            "status": "executor_unavailable",
            "source": "boss_mcp_browser",
            "error": str(exc)[:300],
        }

    url = _build_chat_url(safe_conversation_id)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=_browser_timeout_ms())
        current_url = str(page.url or "")
        risk_status = _detect_runtime_risk(page, current_url=current_url)
        if risk_status == "auth_required":
            return {
                "ok": False,
                "status": "auth_required",
                "source": "boss_mcp_browser",
                "error": "boss login is required",
                "url": current_url,
            }
        if risk_status == "risk_blocked":
            return {
                "ok": False,
                "status": "risk_blocked",
                "source": "boss_mcp_browser",
                "error": "risk or captcha page detected",
                "url": current_url,
            }

        item_selectors = _csv_list(os.getenv("PULSE_BOSS_CHAT_ITEM_SELECTORS", ""))
        if not item_selectors:
            item_selectors = _default_chat_item_selectors(safe_conversation_id)
        item_loc, item_selector = _wait_for_any_selector(
            page,
            item_selectors,
            timeout_ms=min(2500, _browser_timeout_ms()),
        )
        if item_loc is not None:
            try:
                item_loc.click(timeout=min(_browser_timeout_ms(), 8000))
            except Exception:
                item_selector = ""
        if item_loc is None and isinstance(conversation_hint, dict) and conversation_hint:
            clicked, hint_selector = _try_click_conversation_by_hint(page, conversation_hint)
            if clicked:
                item_selector = hint_selector

        input_selectors = _csv_list(os.getenv("PULSE_BOSS_CHAT_INPUT_SELECTORS", ""))
        if not input_selectors:
            input_selectors = _default_chat_input_selectors()
        input_loc, input_selector = _wait_for_any_selector(
            page,
            input_selectors,
            timeout_ms=min(5000, _browser_timeout_ms()),
        )
        if input_loc is None:
            return {
                "ok": False,
                "status": "selector_missing",
                "source": "boss_mcp_browser",
                "error": "chat input selector not found",
                "url": current_url,
            }
        _fill_text(input_loc, safe_reply)

        send_selectors = _csv_list(os.getenv("PULSE_BOSS_CHAT_SEND_SELECTORS", ""))
        if not send_selectors:
            send_selectors = _default_chat_send_selectors()
        send_loc, send_selector = _wait_for_any_selector(
            page,
            send_selectors,
            timeout_ms=min(5000, _browser_timeout_ms()),
        )
        if send_loc is None:
            return {
                "ok": False,
                "status": "selector_missing",
                "source": "boss_mcp_browser",
                "error": "chat send selector not found",
                "url": current_url,
                "input_selector": input_selector,
            }
        # 和 send_resume 同一条不变式: click send 按钮 ≠ 平台真的下发。
        # 必须等到 DOM 层面看到自己那条消息出现 (my_items 增加 / last_my_text
        # 变成刚发的 safe_reply) 才能宣布 sent, 否则 reply 路径可能在风控 /
        # rate-limit / silent rejection 场景下复现 send_resume 的假绿 bug。
        before_reply_state = _snapshot_resume_send_state(page)
        send_loc.click(timeout=min(_browser_timeout_ms(), 8000))
        send_verify = _wait_direct_resume_send_effect(
            page,
            before=before_reply_state,
            timeout_ms=min(_browser_timeout_ms(), 8000),
        )
        if not bool(send_verify.get("ok")):
            logger.warning(
                "reply_conversation.verify_failed conv=%s send_selector=%s verify=%s",
                safe_conversation_id[:20],
                send_selector,
                send_verify,
            )
            return {
                "ok": False,
                "status": "verify_failed",
                "source": "boss_mcp_browser",
                "error": (
                    "reply click produced no observable DOM delta; "
                    "platform did not echo the sent message into chat"
                ),
                "url": current_url,
                "input_selector": input_selector,
                "send_selector": send_selector,
                "send_verify": send_verify,
            }
        screenshot_path = _take_browser_screenshot(page, prefix=f"reply_{safe_conversation_id}")
        logger.info(
            "reply_conversation.sent conv=%s input=%s send=%s verify_reason=%s",
            safe_conversation_id[:20],
            input_selector,
            send_selector,
            str(send_verify.get("reason") or "-"),
        )
        return {
            "ok": True,
            "status": "sent",
            "source": "boss_mcp_browser",
            "error": None,
            "url": current_url,
            "conversation_hint": dict(conversation_hint or {}),
            "conversation_selector": item_selector or None,
            "input_selector": input_selector,
            "send_selector": send_selector,
            "send_verify": send_verify,
            "screenshot_path": screenshot_path,
        }
    except Exception as exc:
        return {
            "ok": False,
            "status": "executor_error",
            "source": "boss_mcp_browser",
            "error": str(exc)[:300],
            "url": str(getattr(page, "url", "") or ""),
        }


def _greet_followup_enabled() -> bool:
    """Whether to type + send a follow-up chat message after the greet click.

    Default ``off``. Modern BOSS 账号在 APP / web 设置里预先配置了一条
    "打招呼话术", 用户点 "立即沟通" / "打招呼" 按钮时, **BOSS 平台自身**
    会把这条预设话术作为第一条消息代发给 HR (图证见用户 trace 2026-04-21:
    HR 聊天窗显示 "[送达] 您好, 27 应届硕士..." 是 BOSS 代发的预设语). 如果
    Pulse 在 click 之后再 fill + send 一条 greeting_text, HR 会看到 **两条**
    同质自我介绍 ——— 产品层 defect.

    ``on`` 保留给 "账号没设预设话术" 的场景: 由 Pulse 生成并发送 followup
    (等同旧行为). 默认 ``off`` 与绝大多数真实 BOSS 账户的实际配置一致.
    """
    raw = str(os.getenv("PULSE_BOSS_MCP_GREET_FOLLOWUP", "off") or "").strip().lower()
    if raw in {"", "off", "0", "false", "no"}:
        return False
    if raw in {"on", "1", "true", "yes"}:
        return True
    raise RuntimeError(
        f"PULSE_BOSS_MCP_GREET_FOLLOWUP={raw!r} not recognised; "
        "expected one of: on / off. 默认 off: 相信 BOSS 平台代发 APP 预设话术. "
        "如果你账号没设预设话术, 设为 on 让 Pulse 发 followup."
    )


def _execute_browser_greet(
    *,
    run_id: str,
    job_id: str,
    source_url: str,
    greeting_text: str,
) -> dict[str, Any]:
    _ = run_id, job_id
    safe_url = str(source_url or "").strip()
    safe_text = str(greeting_text or "").strip()
    if not safe_url:
        return {
            "ok": False,
            "status": "failed",
            "source": "boss_mcp_browser",
            "error": "source_url is required",
        }
    try:
        page = _ensure_browser_page()
    except Exception as exc:
        return {
            "ok": False,
            "status": "executor_unavailable",
            "source": "boss_mcp_browser",
            "error": str(exc)[:300],
        }
    try:
        page.goto(safe_url, wait_until="domcontentloaded", timeout=_browser_timeout_ms())
        current_url = str(page.url or "")
        risk_status = _detect_runtime_risk(page, current_url=current_url)
        if risk_status == "auth_required":
            return {
                "ok": False,
                "status": "auth_required",
                "source": "boss_mcp_browser",
                "error": "boss login is required",
                "url": current_url,
            }
        if risk_status == "risk_blocked":
            return {
                "ok": False,
                "status": "risk_blocked",
                "source": "boss_mcp_browser",
                "error": "risk or captcha page detected",
                "url": current_url,
            }
        greet_selectors = _csv_list(os.getenv("PULSE_BOSS_GREET_BUTTON_SELECTORS", ""))
        if not greet_selectors:
            greet_selectors = _default_greet_button_selectors()
        greet_loc, greet_selector = _wait_for_any_selector(
            page,
            greet_selectors,
            timeout_ms=min(6000, _browser_timeout_ms()),
        )
        if greet_loc is None:
            return {
                "ok": False,
                "status": "selector_missing",
                "source": "boss_mcp_browser",
                "error": "greet button selector not found",
                "url": current_url,
            }
        greet_loc.click(timeout=min(_browser_timeout_ms(), 8000))

        followup_enabled = _greet_followup_enabled()
        input_selector = ""
        send_selector = ""
        if followup_enabled and safe_text:
            input_selectors = _csv_list(os.getenv("PULSE_BOSS_CHAT_INPUT_SELECTORS", ""))
            if not input_selectors:
                input_selectors = _default_chat_input_selectors()
            input_loc, input_selector = _wait_for_any_selector(
                page,
                input_selectors,
                timeout_ms=min(5000, _browser_timeout_ms()),
            )
            if input_loc is not None:
                _fill_text(input_loc, safe_text)
                send_selectors = _csv_list(os.getenv("PULSE_BOSS_CHAT_SEND_SELECTORS", ""))
                if not send_selectors:
                    send_selectors = _default_chat_send_selectors()
                send_loc, send_selector = _wait_for_any_selector(
                    page,
                    send_selectors,
                    timeout_ms=min(5000, _browser_timeout_ms()),
                )
                if send_loc is not None:
                    send_loc.click(timeout=min(_browser_timeout_ms(), 8000))
        screenshot_path = _take_browser_screenshot(page, prefix=f"greet_{job_id or 'job'}")
        # Strategy = button_only 时, BOSS 平台代发 APP 预设话术即视为 "sent";
        # button_and_followup 沿用旧语义 (send 点了算 sent, 未点算 clicked).
        strategy = "button_and_followup" if followup_enabled else "button_only"
        if followup_enabled:
            status = "sent" if send_selector else "clicked"
        else:
            status = "sent"
        return {
            "ok": True,
            "status": status,
            "source": "boss_mcp_browser",
            "error": None,
            "url": current_url,
            "greet_selector": greet_selector,
            "input_selector": input_selector or None,
            "send_selector": send_selector or None,
            "screenshot_path": screenshot_path,
            "greet_strategy": strategy,
        }
    except Exception as exc:
        return {
            "ok": False,
            "status": "executor_error",
            "source": "boss_mcp_browser",
            "error": str(exc)[:300],
            "url": str(getattr(page, "url", "") or ""),
        }


def _resume_attachment_path(resume_profile_id: str) -> Path | None:
    """Resolve the resume file to upload for a given ``resume_profile_id``.

    Two env knobs are supported:

      * ``PULSE_BOSS_RESUME_ATTACHMENT_PATH`` — single default file
      * ``PULSE_BOSS_RESUME_ATTACHMENT_MAP`` — CSV ``id=path,id=path``

    The map takes precedence; if the profile is missing from the map we
    fall back to the single default. Returning ``None`` signals that no
    attachment is configured and the executor must escalate.
    """
    key = (resume_profile_id or "default").strip() or "default"
    mapping_raw = str(os.getenv("PULSE_BOSS_RESUME_ATTACHMENT_MAP", "") or "").strip()
    if mapping_raw:
        for item in mapping_raw.split(","):
            item = item.strip()
            if not item or "=" not in item:
                continue
            id_part, path_part = item.split("=", 1)
            if id_part.strip() == key and path_part.strip():
                resolved = _resolve_path(path_part.strip(), default_path=Path("."))
                return resolved if resolved.is_file() else None
    single_raw = str(os.getenv("PULSE_BOSS_RESUME_ATTACHMENT_PATH", "") or "").strip()
    if not single_raw:
        return None
    resolved = _resolve_path(single_raw, default_path=Path("."))
    return resolved if resolved.is_file() else None


def _default_attach_trigger_selectors() -> list[str]:
    # 两类入口,顺序决定哪条路径先尝试,命中即结束。
    # - Route B (现代主流 UI):聊天底部 .chat-controls 里 "发简历" 文字按钮,
    #   一步直达,无菜单。锚点来源: docs/dom-specs/boss/chat-detail/20260422T073442Z.html
    # - Route A (旧附件图标):回形针图标 -> 弹菜单 -> 选简历, 保留兼容其他账号类型/AB桶。
    return [
        ".chat-controls .toolbar-btn-content:has-text('发简历')",
        ".chat-controls .toolbar-btn:has-text('发简历')",
        ".message-controls .toolbar-btn:has-text('发简历')",
        "[data-v] .icon-attach",
        ".icon-attach",
        ".chat-attach",
        "button[aria-label*='附件']",
        "button[title*='附件']",
        "i[class*='attach']",
    ]


def _default_attach_resume_menu_selectors() -> list[str]:
    return [
        "text=发送简历",
        "text=简历",
        ".attach-menu :text('简历')",
    ]


def _default_attach_confirm_selectors() -> list[str]:
    # 对齐 chat-detail §D.1: "发简历" 按钮按下后弹出的 `.sentence-popover` /
    # `.pop-wrap` 需要点 `.btn-sure` 才真正投递,纯文本选择器在现代 UI 下会漏
    # (按钮文案是 "确认" 不是 "确定发送").
    return [
        ".sentence-popover .btn.btn-sure",
        ".pop-wrap .btn.btn-sure",
        ".sentence-popover .btn-sure",
        ".pop-wrap .btn-sure",
        "button:has-text('确认')",
        "text=确定发送",
        "text=发送",
        "text=确定",
        ".btn-sure",
    ]


def _default_file_input_selectors() -> list[str]:
    return [
        "input[type='file']",
    ]


def _default_card_selectors_for_type(card_type: str) -> list[str]:
    kind = (card_type or "").strip().lower()
    mapping = {
        "exchange_resume": [
            "PULSE_BOSS_CARD_EXCHANGE_RESUME_SELECTORS",
            ["text=交换简历", ".im-card:has-text('交换简历')"],
        ],
        "exchange_contact": [
            "PULSE_BOSS_CARD_EXCHANGE_CONTACT_SELECTORS",
            ["text=交换联系方式", ".im-card:has-text('联系方式')"],
        ],
        "interview_invite": [
            "PULSE_BOSS_CARD_INTERVIEW_INVITE_SELECTORS",
            ["text=面试邀请", ".im-card:has-text('面试')"],
        ],
        "job_recommend": [
            "PULSE_BOSS_CARD_JOB_RECOMMEND_SELECTORS",
            ["text=推荐职位", ".im-card:has-text('职位')"],
        ],
    }
    env_key, defaults = mapping.get(kind, [None, []])
    if env_key:
        override = _csv_list(os.getenv(env_key, ""))
        if override:
            return override
    if defaults:
        return list(defaults)
    return [f".im-card:has-text('{kind}')"] if kind else []


def _default_card_action_selectors(action: str) -> list[str]:
    kind = (action or "").strip().lower()
    if kind == "accept":
        return [
            "text=同意",
            "text=接受",
            "text=确认",
            "button:has-text('同意')",
        ]
    if kind == "reject":
        return [
            "text=拒绝",
            "text=不方便",
            "text=暂不考虑",
            "button:has-text('拒绝')",
        ]
    if kind == "view":
        return [
            "button:has-text('查看')",
            "a:has-text('查看')",
        ]
    return []


def _snapshot_resume_send_state(page: Any) -> dict[str, Any]:
    state = page.evaluate(
        """() => {
        const root = document.querySelector(".chat-conversation");
        const list = root ? root.querySelector(".im-list") : null;
        const allItems = list ? list.querySelectorAll(".message-item") : [];
        const myItems = list ? list.querySelectorAll("li.message-item.item-myself") : [];
        const agreeButtons = document.querySelectorAll(
            ".message-tip-bar .btn-agree, .respond-popover .btn-agree, span.btn.btn-agree"
        );
        const lastMy = myItems.length ? myItems[myItems.length - 1] : null;
        const msg = lastMy ? lastMy.querySelector(".message-content") : null;
        const lastMyText = ((msg ? msg.innerText : (lastMy ? lastMy.innerText : "")) || "").trim();
        return {
            message_items: allItems.length,
            my_items: myItems.length,
            agree_buttons: agreeButtons.length,
            last_my_text: lastMyText.slice(0, 500),
        };
    }"""
    )
    return {
        "message_items": int((state or {}).get("message_items") or 0),
        "my_items": int((state or {}).get("my_items") or 0),
        "agree_buttons": int((state or {}).get("agree_buttons") or 0),
        "last_my_text": str((state or {}).get("last_my_text") or ""),
    }


def _resume_send_effect_reason(before: dict[str, Any], after: dict[str, Any]) -> str | None:
    before_items = int(before.get("message_items") or 0)
    before_my = int(before.get("my_items") or 0)
    before_agree = int(before.get("agree_buttons") or 0)
    before_last = str(before.get("last_my_text") or "")
    after_items = int(after.get("message_items") or 0)
    after_my = int(after.get("my_items") or 0)
    after_agree = int(after.get("agree_buttons") or 0)
    after_last = str(after.get("last_my_text") or "")
    if after_items > before_items:
        return "message_items_increased"
    if after_my > before_my:
        return "my_message_items_increased"
    if before_agree > 0 and after_agree < before_agree:
        return "agree_button_disappeared"
    if after_last and after_last != before_last:
        return "last_my_message_changed"
    return None


def _wait_direct_resume_send_effect(
    page: Any, *, before: dict[str, Any], timeout_ms: int
) -> dict[str, Any]:
    deadline = time.time() + max(0, timeout_ms) / 1000.0
    last = _snapshot_resume_send_state(page)
    reason = _resume_send_effect_reason(before, last)
    if reason:
        return {"ok": True, "reason": reason, "state": last}
    while time.time() < deadline:
        time.sleep(0.25)
        current = _snapshot_resume_send_state(page)
        reason = _resume_send_effect_reason(before, current)
        if reason:
            return {"ok": True, "reason": reason, "state": current}
        last = current
    return {
        "ok": False,
        "reason": "no_observable_dom_delta_after_direct_resume_click",
        "before": before,
        "current": last,
    }


# HR 用 "交换简历" 卡片邀约时,唯一可靠的发简历路径是直接点击会话里
# 这张卡片自带的 "同意" 按钮 — BOSS 平台会把默认附件简历原生下发,不经过
# 工具栏二次选择弹层。DOM 锚点见 docs/dom-specs/boss/chat-detail/20260422T073442Z.html
# (`.message-tip-bar > .respond-popover > .op > span.btn.btn-agree`).
_RESUME_CARD_AGREE_SELECTORS: tuple[str, ...] = (
    ".message-tip-bar .respond-popover .btn.btn-agree",
    ".respond-popover .btn.btn-agree",
    ".message-tip-bar .respond-popover span.btn-agree",
)


def _locate_resume_card_agree(page: Any) -> tuple[Any, str]:
    for selector in _RESUME_CARD_AGREE_SELECTORS:
        locator = page.locator(selector).first
        try:
            if locator.count() == 0:
                continue
            if not locator.is_visible():
                continue
        except Exception:
            continue
        return locator, selector
    return None, ""


def _click_resume_card_agree(
    page: Any, *, locator: Any, selector: str, conversation_id: str
) -> dict[str, Any]:
    before = _snapshot_resume_send_state(page)
    locator.click(timeout=min(_browser_timeout_ms(), 6000))

    # 卡片点同意后的成功信号: 卡片本身消失,并且页面产生一次可观察的 DOM delta
    # (my_items 增加 / agree_buttons 减少 / last_my_text 变化).
    popover_detached = False
    try:
        page.wait_for_selector(
            ".message-tip-bar .respond-popover",
            state="detached",
            timeout=min(_browser_timeout_ms(), 6000),
        )
        popover_detached = True
    except Exception:
        popover_detached = False

    effect = _wait_direct_resume_send_effect(
        page,
        before=before,
        timeout_ms=min(_browser_timeout_ms(), 6000),
    )
    effect["popover_detached"] = popover_detached

    if not popover_detached and not bool(effect.get("ok")):
        return {
            "ok": False,
            "status": "verify_failed",
            "source": "boss_mcp_browser",
            "error": "respond-popover agree click produced no observable effect",
            "trigger_selector": selector,
            "delivery_path": "respond_popover_agree",
            "send_verify": effect,
        }

    screenshot_path = _take_browser_screenshot(
        page, prefix=f"card_agree_{conversation_id}"
    )
    return {
        "ok": True,
        "status": "sent",
        "source": "boss_mcp_browser",
        "error": None,
        "trigger_selector": selector,
        "confirm_selector": None,
        "delivery_path": "respond_popover_agree",
        "send_verify": {
            "ok": True,
            "reason": effect.get("reason") or "popover_detached",
            "popover_detached": popover_detached,
            "state": effect.get("state"),
        },
        "screenshot_path": screenshot_path,
    }


def _recover_resume_send_via_card_agree(
    *,
    page: Any,
    conversation_id: str,
    current_url: str,
    conversation_hint: dict[str, Any] | None,
    resume_profile_id: str,
    send_verify: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Fallback after direct-button verify_failed.

    When BOSS keeps an in-thread "交换简历" card alive, toolbar "发简历"
    clicks can produce no DOM delta while the card "同意" path still works.
    Only attempt recovery when the verifier snapshot reports visible agree
    buttons; otherwise keep the original fail-loud verify_failed contract.
    """
    verify = send_verify if isinstance(send_verify, dict) else {}
    current = verify.get("current") if isinstance(verify.get("current"), dict) else {}
    before = verify.get("before") if isinstance(verify.get("before"), dict) else {}
    agree_now = int(current.get("agree_buttons") or 0)
    agree_before = int(before.get("agree_buttons") or 0)
    if max(agree_now, agree_before) <= 0:
        return None

    card_loc, card_selector = _locate_resume_card_agree(page)
    if card_loc is None:
        return None
    logger.info(
        "send_resume.recover_via_card conv=%s agree_now=%s agree_before=%s card_selector=%s",
        conversation_id[:20],
        agree_now,
        agree_before,
        card_selector or "-",
    )
    card_result = _click_resume_card_agree(
        page,
        locator=card_loc,
        selector=card_selector,
        conversation_id=conversation_id,
    )
    card_result.setdefault("url", current_url)
    card_result.setdefault("conversation_hint", dict(conversation_hint or {}))
    card_result.setdefault("resume_profile_id", resume_profile_id)
    card_result.setdefault("attachment_path", None)
    card_result.setdefault("recovered_from", "direct_resume_verify_failed")
    return card_result


def _execute_browser_send_resume_attachment(
    *,
    conversation_id: str,
    resume_profile_id: str,
    conversation_hint: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Open the conversation and upload / send the resume attachment.

    The BOSS UI offers multiple delivery paths depending on the account
    type and A/B test bucket. This executor tries, in order:

      1. If a **file upload** flow is detected (``input[type=file]`` is
         reachable after clicking the attach-trigger), upload
         ``PULSE_BOSS_RESUME_ATTACHMENT_PATH``.
      2. Otherwise the "built-in resume" flow: click the attach trigger,
         pick the 简历 menu item, click confirm.

    Selectors are all configurable via ``PULSE_BOSS_ATTACH_*`` env vars.
    """
    safe_conv = (conversation_id or "").strip()
    safe_profile = (resume_profile_id or "default").strip() or "default"
    if not safe_conv:
        return {
            "ok": False,
            "status": "failed",
            "source": "boss_mcp_browser",
            "error": "conversation_id is required",
        }

    try:
        page = _ensure_browser_page()
    except Exception as exc:
        return {
            "ok": False,
            "status": "executor_unavailable",
            "source": "boss_mcp_browser",
            "error": str(exc)[:300],
        }

    url = _build_chat_url(safe_conv)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=_browser_timeout_ms())
        current_url = str(page.url or "")
        risk = _detect_runtime_risk(page, current_url=current_url)
        if risk == "auth_required":
            return {
                "ok": False,
                "status": "auth_required",
                "source": "boss_mcp_browser",
                "error": "boss login is required",
                "url": current_url,
            }
        if risk == "risk_blocked":
            return {
                "ok": False,
                "status": "risk_blocked",
                "source": "boss_mcp_browser",
                "error": "risk or captcha page detected",
                "url": current_url,
            }

        # Make sure the specific conversation is selected.
        item_selectors = _csv_list(os.getenv("PULSE_BOSS_CHAT_ITEM_SELECTORS", ""))
        if not item_selectors:
            item_selectors = _default_chat_item_selectors(safe_conv)
        item_loc, _ = _wait_for_any_selector(
            page,
            item_selectors,
            timeout_ms=min(2500, _browser_timeout_ms()),
        )
        if item_loc is not None:
            try:
                item_loc.click(timeout=min(_browser_timeout_ms(), 5000))
            except Exception:
                pass
        elif isinstance(conversation_hint, dict) and conversation_hint:
            _try_click_conversation_by_hint(page, conversation_hint)

        # Route C (最高优先级) — HR 用 "交换简历" 卡片邀约时,走会话内置的
        # "同意" 按钮是唯一可靠的原生投递路径。工具栏 "发简历" 在这种场景下
        # 会走二次选择弹层、甚至被平台拦截,所以必须先看场上有没有卡片。
        card_loc, card_selector = _locate_resume_card_agree(page)
        logger.info(
            "send_resume.probe conv=%s card_hit=%s card_selector=%s",
            safe_conv[:20],
            card_loc is not None,
            card_selector or "-",
        )
        if card_loc is not None:
            card_result = _click_resume_card_agree(
                page,
                locator=card_loc,
                selector=card_selector,
                conversation_id=safe_conv,
            )
            card_result.setdefault("url", current_url)
            card_result.setdefault("conversation_hint", dict(conversation_hint or {}))
            card_result.setdefault("resume_profile_id", safe_profile)
            card_result.setdefault("attachment_path", None)
            verify = card_result.get("send_verify") or {}
            logger.info(
                "send_resume.card_agree conv=%s ok=%s status=%s "
                "popover_detached=%s verify_reason=%s",
                safe_conv[:20],
                bool(card_result.get("ok")),
                str(card_result.get("status") or ""),
                bool(verify.get("popover_detached")) if isinstance(verify, dict) else False,
                str(verify.get("reason") or "-") if isinstance(verify, dict) else "-",
            )
            return card_result

        # Trigger the attach menu.
        trigger_selectors = _csv_list(os.getenv("PULSE_BOSS_ATTACH_TRIGGER_SELECTORS", ""))
        if not trigger_selectors:
            trigger_selectors = _default_attach_trigger_selectors()
        trigger_loc, trigger_selector = _wait_for_any_selector(
            page,
            trigger_selectors,
            timeout_ms=min(4000, _browser_timeout_ms()),
        )
        if trigger_loc is None:
            return {
                "ok": False,
                "status": "selector_missing",
                "source": "boss_mcp_browser",
                "error": "attach trigger selector not found",
                "url": current_url,
            }
        before_send_state = _snapshot_resume_send_state(page)
        trigger_loc.click(timeout=min(_browser_timeout_ms(), 6000))

        # Route B — 现代 UI: 工具栏直达 "发简历" 按钮。点击后通常直接弹 confirm,
        # 或 BOSS 已自动发送; 无 file input、无菜单。
        # Route A — 旧附件图标: 点击后弹菜单,需要再选 "简历" 菜单项或上传文件。
        is_direct_resume_button = "发简历" in trigger_selector
        attachment_path = _resume_attachment_path(safe_profile)
        file_selector = ""
        if is_direct_resume_button:
            used_path = "direct_resume_button"
        else:
            file_input_selectors = _csv_list(os.getenv("PULSE_BOSS_ATTACH_FILE_INPUT_SELECTORS", ""))
            if not file_input_selectors:
                file_input_selectors = _default_file_input_selectors()
            file_loc, file_selector = _wait_for_any_selector(
                page,
                file_input_selectors,
                timeout_ms=min(1500, _browser_timeout_ms()),
            )
            used_path = "file_upload"
            if file_loc is not None and attachment_path is not None:
                try:
                    file_loc.set_input_files(str(attachment_path))
                except Exception as exc:
                    return {
                        "ok": False,
                        "status": "upload_failed",
                        "source": "boss_mcp_browser",
                        "error": str(exc)[:300],
                        "url": current_url,
                        "file_selector": file_selector,
                        "attachment_path": str(attachment_path),
                    }
            else:
                used_path = "builtin_resume_menu"
                menu_selectors = _csv_list(os.getenv("PULSE_BOSS_ATTACH_RESUME_MENU_SELECTORS", ""))
                if not menu_selectors:
                    menu_selectors = _default_attach_resume_menu_selectors()
                menu_loc, _ = _wait_for_any_selector(
                    page,
                    menu_selectors,
                    timeout_ms=min(4000, _browser_timeout_ms()),
                )
                if menu_loc is None:
                    return {
                        "ok": False,
                        "status": "selector_missing",
                        "source": "boss_mcp_browser",
                        "error": "resume menu selector not found",
                        "url": current_url,
                        "trigger_selector": trigger_selector,
                    }
                menu_loc.click(timeout=min(_browser_timeout_ms(), 6000))

        confirm_selectors = _csv_list(os.getenv("PULSE_BOSS_ATTACH_CONFIRM_SELECTORS", ""))
        if not confirm_selectors:
            confirm_selectors = _default_attach_confirm_selectors()
        # 直达按钮路径: confirm 可能压根不弹(已自动发),允许 2.5s 内找不到就放行。
        # 菜单/上传路径: 需要较长超时等待"确定发送"弹窗。
        confirm_timeout_ms = 2500 if is_direct_resume_button else 5000
        confirm_loc, confirm_selector = _wait_for_any_selector(
            page,
            confirm_selectors,
            timeout_ms=min(confirm_timeout_ms, _browser_timeout_ms()),
        )
        send_verify: dict[str, Any] | None = None
        if confirm_loc is not None:
            # 点 confirm 只是触发平台下发,不等于平台真的下发成功。仍然必须等
            # 一次可观察的 DOM delta (my_items 增加 / 消息列表新增 / last_my_text
            # 变化) 才能宣称 sent,否则复现 trace_59effe763b0f 的假绿。
            confirm_loc.click(timeout=min(_browser_timeout_ms(), 6000))
            send_verify = _wait_direct_resume_send_effect(
                page,
                before=before_send_state,
                timeout_ms=min(_browser_timeout_ms(), 8000),
            )
            send_verify["confirm_clicked"] = True
            if not bool(send_verify.get("ok")):
                recovery = _recover_resume_send_via_card_agree(
                    page=page,
                    conversation_id=safe_conv,
                    current_url=current_url,
                    conversation_hint=conversation_hint,
                    resume_profile_id=safe_profile,
                    send_verify=send_verify,
                )
                if isinstance(recovery, dict) and bool(recovery.get("ok")):
                    logger.info(
                        "send_resume.recover_success conv=%s trigger=%s path=%s",
                        safe_conv[:20],
                        trigger_selector,
                        used_path,
                    )
                    return recovery
                logger.warning(
                    "send_resume.verify_failed conv=%s trigger=%s path=%s confirm=%s verify=%s",
                    safe_conv[:20],
                    trigger_selector,
                    used_path,
                    confirm_selector,
                    send_verify,
                )
                return {
                    "ok": False,
                    "status": "verify_failed",
                    "source": "boss_mcp_browser",
                    "error": (
                        "resume send effect not observed after confirm click; "
                        "platform did not echo the sent resume into chat"
                    ),
                    "url": current_url,
                    "trigger_selector": trigger_selector,
                    "confirm_selector": confirm_selector,
                    "delivery_path": used_path,
                    "send_verify": send_verify,
                    "recovery_result": recovery if isinstance(recovery, dict) else None,
                }
        elif not is_direct_resume_button:
            return {
                "ok": False,
                "status": "selector_missing",
                "source": "boss_mcp_browser",
                "error": "resume confirm selector not found",
                "url": current_url,
                "trigger_selector": trigger_selector,
                "delivery_path": used_path,
            }
        else:
            send_verify = _wait_direct_resume_send_effect(
                page,
                before=before_send_state,
                timeout_ms=min(_browser_timeout_ms(), 8000),
            )
            if not bool(send_verify.get("ok")):
                recovery = _recover_resume_send_via_card_agree(
                    page=page,
                    conversation_id=safe_conv,
                    current_url=current_url,
                    conversation_hint=conversation_hint,
                    resume_profile_id=safe_profile,
                    send_verify=send_verify,
                )
                if isinstance(recovery, dict) and bool(recovery.get("ok")):
                    logger.info(
                        "send_resume.recover_success conv=%s trigger=%s path=%s",
                        safe_conv[:20],
                        trigger_selector,
                        used_path,
                    )
                    return recovery
                logger.warning(
                    "send_resume.verify_failed conv=%s trigger=%s verify=%s",
                    safe_conv[:20],
                    trigger_selector,
                    send_verify,
                )
                return {
                    "ok": False,
                    "status": "verify_failed",
                    "source": "boss_mcp_browser",
                    "error": "resume send effect not observed after direct button click",
                    "url": current_url,
                    "trigger_selector": trigger_selector,
                    "delivery_path": used_path,
                    "send_verify": send_verify,
                    "recovery_result": recovery if isinstance(recovery, dict) else None,
                }

        screenshot_path = _take_browser_screenshot(
            page, prefix=f"attach_{safe_conv}"
        )
        logger.info(
            "send_resume.toolbar conv=%s ok=true status=sent trigger=%s "
            "confirm=%s delivery_path=%s verify_reason=%s",
            safe_conv[:20],
            trigger_selector,
            confirm_selector or "-",
            used_path,
            str((send_verify or {}).get("reason") or "-"),
        )
        return {
            "ok": True,
            "status": "sent",
            "source": "boss_mcp_browser",
            "error": None,
            "url": current_url,
            "conversation_hint": dict(conversation_hint or {}),
            "trigger_selector": trigger_selector,
            "confirm_selector": confirm_selector or None,
            "delivery_path": used_path,
            "attachment_path": str(attachment_path) if attachment_path else None,
            "resume_profile_id": safe_profile,
            "send_verify": send_verify or {},
            "screenshot_path": screenshot_path,
        }
    except Exception as exc:
        return {
            "ok": False,
            "status": "executor_error",
            "source": "boss_mcp_browser",
            "error": str(exc)[:300],
            "url": str(getattr(page, "url", "") or ""),
        }


def _execute_browser_click_card(
    *,
    conversation_id: str,
    card_id: str,
    card_type: str,
    action: str,
) -> dict[str, Any]:
    """Click an interactive card's accept / reject / view button."""
    safe_conv = (conversation_id or "").strip()
    safe_type = (card_type or "").strip()
    safe_action = (action or "").strip()
    if not safe_conv or not safe_type or not safe_action:
        return {
            "ok": False,
            "status": "failed",
            "source": "boss_mcp_browser",
            "error": "conversation_id, card_type, action are all required",
        }

    try:
        page = _ensure_browser_page()
    except Exception as exc:
        return {
            "ok": False,
            "status": "executor_unavailable",
            "source": "boss_mcp_browser",
            "error": str(exc)[:300],
        }

    url = _build_chat_url(safe_conv)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=_browser_timeout_ms())
        current_url = str(page.url or "")
        risk = _detect_runtime_risk(page, current_url=current_url)
        if risk == "auth_required":
            return {
                "ok": False,
                "status": "auth_required",
                "source": "boss_mcp_browser",
                "error": "boss login is required",
                "url": current_url,
            }
        if risk == "risk_blocked":
            return {
                "ok": False,
                "status": "risk_blocked",
                "source": "boss_mcp_browser",
                "error": "risk or captcha page detected",
                "url": current_url,
            }

        card_selectors = _default_card_selectors_for_type(safe_type)
        if not card_selectors:
            return {
                "ok": False,
                "status": "unsupported_card_type",
                "source": "boss_mcp_browser",
                "error": f"no default selectors for card_type={safe_type}",
                "url": current_url,
            }
        card_loc, card_selector = _wait_for_any_selector(
            page,
            card_selectors,
            timeout_ms=min(5000, _browser_timeout_ms()),
        )
        if card_loc is None:
            return {
                "ok": False,
                "status": "selector_missing",
                "source": "boss_mcp_browser",
                "error": f"card selector not found for type={safe_type}",
                "url": current_url,
            }

        if safe_action == "view":
            try:
                card_loc.click(timeout=min(_browser_timeout_ms(), 6000))
            except Exception as exc:
                return {
                    "ok": False,
                    "status": "executor_error",
                    "source": "boss_mcp_browser",
                    "error": str(exc)[:300],
                    "url": current_url,
                    "card_selector": card_selector,
                }
            screenshot_path = _take_browser_screenshot(page, prefix=f"card_view_{safe_conv}")
            return {
                "ok": True,
                "status": "clicked",
                "source": "boss_mcp_browser",
                "error": None,
                "url": current_url,
                "card_id": card_id,
                "card_type": safe_type,
                "action": safe_action,
                "card_selector": card_selector,
                "screenshot_path": screenshot_path,
            }

        action_selectors = _default_card_action_selectors(safe_action)
        if not action_selectors:
            return {
                "ok": False,
                "status": "unsupported_action",
                "source": "boss_mcp_browser",
                "error": f"unsupported action={safe_action}",
                "url": current_url,
                "card_selector": card_selector,
            }
        button_loc, button_selector = _wait_for_any_selector(
            page,
            action_selectors,
            timeout_ms=min(5000, _browser_timeout_ms()),
        )
        if button_loc is None:
            return {
                "ok": False,
                "status": "selector_missing",
                "source": "boss_mcp_browser",
                "error": f"action button selector not found for action={safe_action}",
                "url": current_url,
                "card_selector": card_selector,
            }
        button_loc.click(timeout=min(_browser_timeout_ms(), 6000))
        screenshot_path = _take_browser_screenshot(
            page, prefix=f"card_{safe_action}_{safe_conv}"
        )
        return {
            "ok": True,
            "status": "clicked",
            "source": "boss_mcp_browser",
            "error": None,
            "url": current_url,
            "card_id": card_id,
            "card_type": safe_type,
            "action": safe_action,
            "card_selector": card_selector,
            "button_selector": button_selector,
            "screenshot_path": screenshot_path,
        }
    except Exception as exc:
        return {
            "ok": False,
            "status": "executor_error",
            "source": "boss_mcp_browser",
            "error": str(exc)[:300],
            "url": str(getattr(page, "url", "") or ""),
        }


# ---------------------------------------------------------------------------
# Auto-reply Actuator (new; path-alongside the legacy _execute_browser_click_card).
#
# Unlike the legacy path these two helpers target the precise selectors
# frozen by the chat-detail contract (docs/dom-specs/boss/chat-detail/README.md
# §C.4 and §D.1). They do NOT perform conversation navigation — the caller
# must have the right chat-detail panel open (via the orchestrator in
# ``run_auto_reply_cycle``) and must have just inspected ``ChatDetailState``
# to confirm the relevant DOM element exists.
# ---------------------------------------------------------------------------


def _click_respond_popover_via_browser(
    page: Any,
    *,
    decision: Literal["agree", "refuse"],
    conversation_id: str,
) -> dict[str, Any]:
    """Click ``.respond-popover .btn.btn-agree`` or ``.btn.btn-refuse``.

    Contract: chat-detail §C.4. The popover disappears after a successful
    click; we wait for that to confirm the interaction actually landed.

    ``conversation_id`` is used only for the screenshot filename; the
    caller is responsible for having the correct conversation in view.
    """

    if decision not in ("agree", "refuse"):
        return {
            "ok": False,
            "status": "failed",
            "source": "boss_mcp_browser",
            "error": f"unsupported decision={decision!r}",
        }

    popover_selector = ".message-tip-bar .respond-popover"
    button_class = "btn-agree" if decision == "agree" else "btn-refuse"
    button_selector = f"{popover_selector} .btn.{button_class}"

    try:
        current_url = str(getattr(page, "url", "") or "")
        popover_loc, _ = _wait_for_any_selector(
            page,
            [popover_selector],
            timeout_ms=min(3500, _browser_timeout_ms()),
        )
        if popover_loc is None:
            return {
                "ok": False,
                "status": "selector_drift",
                "source": "boss_mcp_browser",
                "error": "respond-popover not present at click time",
                "url": current_url,
            }
        button_loc, resolved_selector = _wait_for_any_selector(
            page,
            [button_selector],
            timeout_ms=min(3500, _browser_timeout_ms()),
        )
        if button_loc is None:
            return {
                "ok": False,
                "status": "selector_drift",
                "source": "boss_mcp_browser",
                "error": f"respond-popover .{button_class} not found",
                "url": current_url,
            }
        button_loc.click(timeout=min(_browser_timeout_ms(), 6000))

        disappeared = False
        try:
            page.wait_for_selector(
                popover_selector,
                state="detached",
                timeout=min(_browser_timeout_ms(), 6000),
            )
            disappeared = True
        except Exception:
            disappeared = False

        screenshot_path = _take_browser_screenshot(
            page, prefix=f"autoreply_respond_{decision}_{conversation_id}"
        )
        return {
            "ok": True,
            "status": "clicked" if disappeared else "clicked_popover_persisted",
            "source": "boss_mcp_browser",
            "error": None,
            "url": current_url,
            "decision": decision,
            "button_selector": resolved_selector,
            "screenshot_path": screenshot_path,
        }
    except Exception as exc:
        return {
            "ok": False,
            "status": "executor_error",
            "source": "boss_mcp_browser",
            "error": str(exc)[:300],
            "url": str(getattr(page, "url", "") or ""),
        }


def _click_send_resume_via_browser(
    page: Any,
    *,
    conversation_id: str,
) -> dict[str, Any]:
    """Click the "发简历" button on ``.chat-controls`` toolbar.

    Contract: chat-detail §D.1. The button has no dedicated class — it is
    located by exact inner text inside ``.chat-controls .toolbar``. Some
    layouts display a confirm popover (``text=确认`` / ``.btn-sure``); we
    wait a short window for it and click through if present.
    """

    try:
        current_url = str(getattr(page, "url", "") or "")

        toolbar_loc = page.locator(".chat-controls .toolbar")
        button_loc = toolbar_loc.locator(
            "div.toolbar-btn", has_text="发简历"
        ).first
        try:
            button_loc.wait_for(
                state="visible",
                timeout=min(_browser_timeout_ms(), 3500),
            )
        except Exception:
            return {
                "ok": False,
                "status": "selector_missing",
                "source": "boss_mcp_browser",
                "error": "'发简历' toolbar button not visible",
                "url": current_url,
            }
        button_loc.click(timeout=min(_browser_timeout_ms(), 6000))

        confirm_selectors = [
            ".pop-wrap .btn.btn-sure",
            ".sentence-popover .btn-sure",
            "text=确认",
            "text=确定",
        ]
        confirm_loc, confirm_selector = _wait_for_any_selector(
            page,
            confirm_selectors,
            timeout_ms=min(1500, _browser_timeout_ms()),
        )
        confirm_clicked = False
        if confirm_loc is not None:
            try:
                confirm_loc.click(timeout=min(_browser_timeout_ms(), 5000))
                confirm_clicked = True
            except Exception:
                confirm_clicked = False

        screenshot_path = _take_browser_screenshot(
            page, prefix=f"autoreply_send_resume_{conversation_id}"
        )
        return {
            "ok": True,
            "status": "clicked",
            "source": "boss_mcp_browser",
            "error": None,
            "url": current_url,
            "confirm_selector": confirm_selector if confirm_clicked else None,
            "screenshot_path": screenshot_path,
        }
    except Exception as exc:
        return {
            "ok": False,
            "status": "executor_error",
            "source": "boss_mcp_browser",
            "error": str(exc)[:300],
            "url": str(getattr(page, "url", "") or ""),
        }


def _action_audit_path() -> Path:
    return _resolve_path(
        os.getenv("PULSE_BOSS_MCP_ACTION_AUDIT_PATH", "").strip(),
        default_path=Path.home() / ".pulse" / "boss_mcp_actions.jsonl",
    )


def _append_action_log(row: dict[str, Any]) -> None:
    path = _action_audit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(row)
    payload["logged_at"] = datetime.now(timezone.utc).isoformat()
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _idempotency_window_sec() -> int:
    """Lookback window for MUTATING idempotency guard; see ADR-001 §6 P3e."""
    return _safe_int(
        os.getenv("PULSE_BOSS_MCP_IDEMPOTENCY_WINDOW_SEC", "300"),
        300,
        min_value=0,
        max_value=3600,
    )


def _find_recent_successful_action(
    *,
    operation: str,
    match: dict[str, str],
    within_sec: int,
) -> dict[str, Any] | None:
    """Return the most recent ``<operation>_result`` row in the audit log
    whose ``match`` key/value pairs match and ``ok == True``, logged within
    ``within_sec`` seconds; else ``None``.

    Used as the MCP-side idempotency barrier for MUTATING tools (greet_job,
    reply_conversation, send_resume_attachment). Rationale: HTTP clients
    will retry on timeout even though we kept the browser-side click
    running to completion; we MUST NOT re-click the send button. The audit
    log is the single source of truth and an append-only file, so this
    guard is robust to process restarts within the window.

    Implementation note: we only read the audit tail (~64 KB) because
    realistic workloads produce ~100 bytes/row and the window is short.
    This keeps the guard O(1) with respect to total audit size while
    remaining correct for the observed workload.
    """
    if within_sec <= 0 or not match:
        return None
    result_action = f"{operation}_result"
    path = _action_audit_path()
    if not path.exists():
        return None
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(max(0, size - 65536))
            tail = handle.read()
    except OSError:
        return None
    now_ts = datetime.now(timezone.utc).timestamp()
    cutoff = now_ts - within_sec
    # Scan newest-first within the tail window.
    for raw_line in reversed(tail.decode("utf-8", errors="replace").splitlines()):
        line = raw_line.strip()
        if not line.startswith("{"):
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("action") != result_action:
            continue
        mismatched = False
        for key, expected in match.items():
            if str(row.get(key, "")) != str(expected):
                mismatched = True
                break
        if mismatched:
            continue
        if not bool(row.get("ok")):
            continue
        ts_raw = str(row.get("logged_at", ""))
        try:
            ts = datetime.fromisoformat(ts_raw).timestamp()
        except ValueError:
            continue
        if ts < cutoff:
            # keep scanning — timestamps inside the tail are usually
            # monotonic but we don't rely on that assumption.
            continue
        return row
    return None


def scan_jobs(
    *,
    keyword: str,
    max_items: int | None = None,
    max_pages: int | None = None,
    target_count: int | None = None,
    evaluation_cap: int | None = None,
    scroll_plateau_rounds: int | None = None,
    job_type: str = "all",
    city: str | None = None,
) -> dict[str, Any]:
    """Scan jobs via streaming scroll (preferred) or web-search fallback.

    Parameter contract:
      * ``target_count`` — desired # of cards before stopping early. Falls
        back to ``max_items`` for back-compat.
      * ``evaluation_cap`` — hard ceiling on how many cards we will return,
        regardless of target. Defaults to ``max(target_count, 60)`` so a
        small ``target`` still gives the host enough headroom for
        list-stage filtering. ``max_pages`` is no longer used as a
        pagination knob (BOSS uses infinite scroll); it survives only as
        a deprecated alias for ``evaluation_cap`` budgeting.
      * ``scroll_plateau_rounds`` — # of scrolls with zero new cards
        before declaring the sidebar exhausted (defaults to 3).

    Returned payload always includes ``exhausted: bool`` so the host can
    distinguish "ran out of budget" from "list truly ended".
    """
    safe_keyword = str(keyword or "").strip() or "AI Agent 实习"
    legacy_max_items = _safe_int(max_items, 10, min_value=1, max_value=200) if max_items is not None else 10
    safe_target = _safe_int(target_count, legacy_max_items, min_value=1, max_value=200)
    default_cap = max(safe_target, 60)
    if evaluation_cap is None and max_pages is not None:
        # Back-compat: legacy callers passed max_pages~=2; we treat it as a
        # rough budget signal and inflate it to a per-card cap.
        default_cap = max(default_cap, _safe_int(max_pages, 2, min_value=1, max_value=8) * 30)
    safe_cap = _safe_int(evaluation_cap, default_cap, min_value=safe_target, max_value=200)
    safe_plateau = _safe_int(scroll_plateau_rounds, 3, min_value=1, max_value=8)
    _ = str(job_type or "all").strip() or "all"
    safe_city = (str(city).strip() or None) if city else None
    mode = _scan_mode()
    browser_errors: list[str] = []

    if mode in {"browser_only", "browser_first"}:
        browser_result = _scan_jobs_via_browser(
            keyword=safe_keyword,
            target_count=safe_target,
            evaluation_cap=safe_cap,
            scroll_plateau_rounds=safe_plateau,
            city=safe_city,
        )
        browser_items = browser_result.get("items")
        if isinstance(browser_items, list) and browser_items:
            logger.info(
                "boss.scan.browser.ready keyword=%s city=%s target=%d cap=%d "
                "scroll=%d exhausted=%s items=%d",
                safe_keyword,
                safe_city or "nationwide",
                safe_target,
                safe_cap,
                _safe_int(browser_result.get("scroll_count"), 0, min_value=0, max_value=200),
                bool(browser_result.get("exhausted")),
                len(browser_items),
            )
            return {
                "ok": True,
                "items": browser_items[:safe_cap],
                "scroll_count": _safe_int(browser_result.get("scroll_count"), 0, min_value=0, max_value=200),
                "exhausted": bool(browser_result.get("exhausted")),
                # pages_scanned is preserved only for back-compat with
                # observers/dashboards that read the old field.
                "pages_scanned": 1 + _safe_int(browser_result.get("scroll_count"), 0, min_value=0, max_value=200),
                "source": str(browser_result.get("source") or "boss_mcp_browser_scan"),
                "errors": list(browser_result.get("errors") or []),
                "mode": mode,
            }
        browser_errors.extend(str(err)[:300] for err in list(browser_result.get("errors") or []))
        browser_status = str(browser_result.get("status") or "").strip()
        browser_url = str(browser_result.get("url") or "").strip()
        if browser_status:
            browser_errors.append(f"browser_status={browser_status}")
        if browser_url:
            browser_errors.append(f"browser_url={browser_url}")
        if mode == "browser_only":
            logger.warning(
                "boss.scan.browser.failed keyword=%s city=%s target=%d cap=%d status=%s",
                safe_keyword,
                safe_city or "nationwide",
                safe_target,
                safe_cap,
                str(browser_result.get("status") or "unknown"),
            )
            return {
                "ok": bool(browser_result.get("ok")),
                "items": list(browser_items or []),
                "scroll_count": _safe_int(browser_result.get("scroll_count"), 0, min_value=0, max_value=200),
                "exhausted": bool(browser_result.get("exhausted")),
                "pages_scanned": 1 + _safe_int(browser_result.get("scroll_count"), 0, min_value=0, max_value=200),
                "source": str(browser_result.get("source") or "boss_mcp_browser_scan"),
                "errors": browser_errors or [f"browser scan failed: {str(browser_result.get('status') or 'unknown')}"],
                "mode": mode,
            }

    query_pool = (
        f"site:zhipin.com {safe_keyword} 实习",
        f"site:zhipin.com {safe_keyword} 招聘",
        f"site:zhipin.com {safe_keyword} 岗位",
        f"{safe_keyword} BOSS直聘",
    )
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    errors: list[str] = list(browser_errors)
    queries_run = 0
    for query in query_pool:
        if len(rows) >= safe_target:
            break
        queries_run += 1
        try:
            hits = search_web(query, max_results=min(12, safe_cap * 2))
        except Exception as exc:
            errors.append(str(exc)[:300])
            continue
        for hit in hits:
            if len(rows) >= safe_cap:
                break
            source_url = str(hit.url or "").strip()
            title_raw = str(hit.title or "").strip()
            if not source_url and not title_raw:
                continue
            dedupe_key = (source_url or title_raw).lower()
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            title = _guess_title(title_raw, keyword=safe_keyword)
            company = _guess_company(title_raw, source_url)
            if not source_url:
                source_url = f"https://www.zhipin.com/job_detail/{hashlib.sha1(dedupe_key.encode('utf-8')).hexdigest()[:16]}"
            rows.append(
                {
                    "job_id": hashlib.sha1(source_url.encode("utf-8")).hexdigest()[:16],
                    "title": title,
                    "company": company,
                    "salary": None,
                    "source_url": source_url,
                    "snippet": token_preview(str(hit.snippet or ""), max_tokens=700),
                    "source": "boss_mcp_web_search",
                    "collected_at": datetime.now(timezone.utc).isoformat(),
                }
            )
    if not rows and _allow_seed_fallback():
        seeded = int(hashlib.sha1(safe_keyword.encode("utf-8")).hexdigest()[:8], 16)
        for idx in range(safe_target):
            title, company, salary = _SEED_JOBS[(seeded + idx) % len(_SEED_JOBS)]
            source_url = f"https://www.zhipin.com/job_detail/seed_{seeded}_{idx}"
            rows.append(
                {
                    "job_id": hashlib.sha1(source_url.encode("utf-8")).hexdigest()[:16],
                    "title": title,
                    "company": company,
                    "salary": salary,
                    "source_url": source_url,
                    "snippet": f"{company} 正在招聘 {title}，关键词：{safe_keyword}",
                    "source": "boss_mcp_seed",
                    "collected_at": datetime.now(timezone.utc).isoformat(),
                }
            )
        errors.append("web search unavailable, switched to seed dataset")
    elif not rows:
        errors.append("web search returned no jobs and seed fallback is disabled")
    source = "boss_mcp_web_search"
    if rows and rows[0].get("source") == "boss_mcp_seed":
        source = "boss_mcp_seed"
    # Web-search fallback has no scrolling concept; report exhausted=True
    # so reflection layer can decide whether to evolve keywords.
    return {
        "ok": bool(rows),
        "items": rows[:safe_cap],
        "scroll_count": 0,
        "exhausted": True,
        "pages_scanned": max(1, queries_run),
        "source": source,
        "errors": errors,
        "mode": mode,
    }


def job_detail(*, job_id: str, source_url: str) -> dict[str, Any]:
    safe_job_id = str(job_id or "").strip()
    safe_url = str(source_url or "").strip()
    if not safe_job_id and safe_url:
        safe_job_id = hashlib.sha1(safe_url.encode("utf-8")).hexdigest()[:16]
    if not safe_url:
        return {
            "ok": False,
            "detail": {},
            "error": "source_url is required",
            "source": "boss_mcp",
        }
    try:
        page_text = _read_page_text(safe_url, max_chars=2200)
        return {
            "ok": True,
            "detail": {
                "job_id": safe_job_id,
                "source_url": safe_url,
                "page_summary": page_text,
            },
            "source": "boss_mcp",
        }
    except Exception as exc:
        return {
            "ok": False,
            "detail": {},
            "error": str(exc)[:300],
            "source": "boss_mcp",
        }


def _inbox_path() -> Path:
    raw = os.getenv("PULSE_BOSS_CHAT_INBOX_PATH", "").strip()
    return _resolve_path(raw, default_path=Path.home() / ".pulse" / "boss_chat_inbox.jsonl")


def pull_conversations(
    *,
    max_conversations: int,
    unread_only: bool,
    fetch_latest_hr: bool,
    chat_tab: str,
) -> dict[str, Any]:
    safe_fetch_latest = bool(fetch_latest_hr)
    safe_chat_tab = str(chat_tab or "").strip()
    mode = _pull_mode()
    browser_errors: list[str] = []

    if mode in {"browser_only", "browser_first"}:
        browser_result = _pull_conversations_via_browser(
            max_conversations=max_conversations,
            unread_only=unread_only,
            fetch_latest_hr=safe_fetch_latest,
            chat_tab=safe_chat_tab,
        )
        browser_items = browser_result.get("items")
        if isinstance(browser_items, list) and browser_result.get("ok"):
            return {
                "ok": True,
                "items": browser_items,
                "unread_total": _safe_int(browser_result.get("unread_total"), 0, min_value=0, max_value=9999),
                "source": str(browser_result.get("source") or "boss_mcp_browser_chat"),
                "errors": list(browser_result.get("errors") or []),
                "mode": mode,
            }
        browser_errors.extend(str(err)[:300] for err in list(browser_result.get("errors") or []))
        browser_status = str(browser_result.get("status") or "").strip()
        browser_url = str(browser_result.get("url") or "").strip()
        if browser_status:
            browser_errors.append(f"browser_status={browser_status}")
        if browser_url:
            browser_errors.append(f"browser_url={browser_url}")
        if mode == "browser_only":
            return {
                "ok": bool(browser_result.get("ok")),
                "items": list(browser_items or []),
                "unread_total": _safe_int(browser_result.get("unread_total"), 0, min_value=0, max_value=9999),
                "source": str(browser_result.get("source") or "boss_mcp_browser_chat"),
                "errors": browser_errors or [f"browser pull failed: {str(browser_result.get('status') or 'unknown')}"],
                "mode": mode,
            }

    path = _inbox_path()
    rows: list[dict[str, Any]] = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            raw = line.strip()
            if not raw:
                continue
            try:
                item = json.loads(raw)
            except Exception:
                continue
            if not isinstance(item, dict):
                continue
            conversation_id = str(item.get("conversation_id") or "").strip()
            if not conversation_id:
                seed = f"{item.get('company')}-{item.get('job_title')}-{item.get('hr_name')}"
                conversation_id = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]
            row = {
                "conversation_id": conversation_id,
                "hr_name": str(item.get("hr_name") or "").strip(),
                "company": str(item.get("company") or "").strip(),
                "job_title": str(item.get("job_title") or "").strip(),
                "latest_message": str(item.get("latest_message") or "").strip(),
                "latest_time": str(item.get("latest_time") or "刚刚"),
                "unread_count": max(0, min(int(item.get("unread_count") or 0), 99)),
            }
            if row["hr_name"] and row["company"] and row["job_title"] and row["latest_message"]:
                rows.append(row)
    if unread_only:
        rows = [item for item in rows if int(item.get("unread_count") or 0) > 0]
    rows.reverse()
    safe_max = _safe_int(max_conversations, 20, min_value=1, max_value=200)
    rows = rows[:safe_max]
    return {
        "ok": True,
        "items": rows,
        "unread_total": sum(int(item.get("unread_count") or 0) for item in rows),
        "source": "boss_mcp_local_inbox",
        "errors": browser_errors,
        "mode": mode,
    }


def reply_conversation(
    *,
    conversation_id: str,
    reply_text: str,
    profile_id: str,
    conversation_hint: dict[str, Any] | None = None,
) -> dict[str, Any]:
    safe_conversation_id = str(conversation_id or "").strip()
    safe_reply_text = str(reply_text or "").strip()
    safe_profile_id = str(profile_id or "default").strip() or "default"
    if not safe_conversation_id:
        return {"ok": False, "status": "failed", "error": "conversation_id is required"}
    if not safe_reply_text:
        return {"ok": False, "status": "failed", "error": "reply_text is required"}
    # ADR-001 §6 P3e idempotency barrier. Key = (profile_id, conversation_id,
    # reply_text): an upstream HTTP retry sends the same payload verbatim,
    # so exact reply_text match is both safe (legitimate follow-up messages
    # differ) and strict enough to prevent duplicate sends.
    replay = _find_recent_successful_action(
        operation="reply_conversation",
        match={
            "profile_id": safe_profile_id,
            "conversation_id": safe_conversation_id,
            "reply_text": safe_reply_text,
        },
        within_sec=_idempotency_window_sec(),
    )
    if replay is not None:
        _append_action_log(
            {
                "action": "reply_conversation_idempotent_replay",
                "conversation_id": safe_conversation_id,
                "profile_id": safe_profile_id,
                "replay_of_logged_at": replay.get("logged_at"),
            }
        )
        return {
            "ok": True,
            "status": str(replay.get("status") or "sent"),
            "source": "boss_mcp",
            "error": None,
            "idempotent_replay": True,
            "screenshot_path": replay.get("screenshot_path"),
        }
    _append_action_log(
        {
            "action": "reply_conversation",
            "conversation_id": safe_conversation_id,
            "profile_id": safe_profile_id,
            "reply_text": safe_reply_text,
            "conversation_hint": dict(conversation_hint or {}),
        }
    )
    mode = str(os.getenv("PULSE_BOSS_MCP_REPLY_MODE", "browser") or "").strip().lower()
    if mode in {"log_only", "dry_run_ok"}:
        # Killswitch 干跑: status 必须是 "logged_only" (不是普通 "logged"),
        # 让 service.py 通过 _TRUE_DELIVERY_STATUSES 白名单识别为非发送。
        # 同时 WARN 一条明显的日志, 日志审查时一眼能看到当前环境是 dry-run。
        logger.warning(
            "boss_mcp.killswitch.dry_run op=reply_conversation mode=%s "
            "conv=%s — no browser action, returning logged_only",
            mode,
            safe_conversation_id[:20],
        )
        result = {
            "ok": True,
            "status": "logged_only",
            "source": "boss_mcp",
            "error": None,
        }
    elif mode in {"browser", "playwright"}:
        result = _run_browser_executor_with_retry(
            "reply_conversation",
            lambda: _execute_browser_reply(
                conversation_id=safe_conversation_id,
                reply_text=safe_reply_text,
                profile_id=safe_profile_id,
                conversation_hint=dict(conversation_hint or {}),
            ),
        )
    else:
        result = {
            "ok": False,
            "status": "manual_required",
            "source": "boss_mcp",
            "error": "reply executor is not configured yet; action is logged for audit",
        }
    _append_action_log(
        {
            "action": "reply_conversation_result",
            "conversation_id": safe_conversation_id,
            "profile_id": safe_profile_id,
            "mode": mode,
            "status": str(result.get("status") or ""),
            "ok": bool(result.get("ok")),
            "error": str(result.get("error") or "")[:300] or None,
            "source": str(result.get("source") or "boss_mcp"),
            "conversation_hint": dict(conversation_hint or {}),
            "screenshot_path": str(result.get("screenshot_path") or "") or None,
        }
    )
    return result


def greet_job(
    *,
    run_id: str,
    job_id: str,
    source_url: str,
    job_title: str,
    company: str,
    greeting_text: str,
) -> dict[str, Any]:
    safe_run_id = str(run_id or "").strip()
    safe_job_id = str(job_id or "").strip()
    safe_source_url = str(source_url or "").strip()
    if not safe_job_id and safe_source_url:
        safe_job_id = hashlib.sha1(safe_source_url.encode("utf-8")).hexdigest()[:16]
    # ADR-001 §6 P3e idempotency barrier: HTTP clients retry on timeout
    # (observed in trace_a9bbc29a245c where one imperative turn produced 4 ×
    # sent=True audit rows while the backend connector saw 3 × "timed out").
    # Guarding on (run_id, job_id) — the natural duplicate key for a single
    # batch — prevents a second real greeting from reaching the HR.
    if safe_run_id and safe_job_id:
        replay = _find_recent_successful_action(
            operation="greet_job",
            match={"run_id": safe_run_id, "job_id": safe_job_id},
            within_sec=_idempotency_window_sec(),
        )
        if replay is not None:
            _append_action_log(
                {
                    "action": "greet_job_idempotent_replay",
                    "run_id": safe_run_id,
                    "job_id": safe_job_id,
                    "source_url": safe_source_url,
                    "replay_of_logged_at": replay.get("logged_at"),
                }
            )
            return {
                "ok": True,
                "status": str(replay.get("status") or "sent"),
                "source": "boss_mcp",
                "error": None,
                "idempotent_replay": True,
                "screenshot_path": replay.get("screenshot_path"),
            }
    _append_action_log(
        {
            "action": "greet_job",
            "run_id": safe_run_id,
            "job_id": safe_job_id,
            "source_url": safe_source_url,
            "job_title": str(job_title or "").strip(),
            "company": str(company or "").strip(),
            "greeting_text": str(greeting_text or "").strip(),
        }
    )
    mode = str(os.getenv("PULSE_BOSS_MCP_GREET_MODE", "browser") or "").strip().lower()
    if mode in {"log_only", "dry_run_ok"}:
        result = {
            "ok": True,
            "status": "logged",
            "source": "boss_mcp",
            "error": None,
        }
    elif mode in {"browser", "playwright"}:
        result = _run_browser_executor_with_retry(
            "greet_job",
            lambda: _execute_browser_greet(
                run_id=safe_run_id,
                job_id=safe_job_id,
                source_url=safe_source_url,
                greeting_text=str(greeting_text or "").strip(),
            ),
        )
    else:
        # Fail-loud: caller configured an unrecognised mode; we refuse to
        # silently pretend-success (previous "manual_required" default would
        # return ok=False + friendly text, which downstream matcher+LLM
        # still narrates as "尝试投递" → Contract C flags as false_absence).
        result = {
            "ok": False,
            "status": "mode_not_configured",
            "source": "boss_mcp",
            "error": (
                f"PULSE_BOSS_MCP_GREET_MODE={mode!r} not recognised; "
                "expected one of: browser / playwright / log_only / dry_run_ok"
            ),
        }
    _append_action_log(
        {
            "action": "greet_job_result",
            "run_id": safe_run_id,
            "job_id": safe_job_id,
            "source_url": safe_source_url,
            "mode": mode,
            "status": str(result.get("status") or ""),
            "ok": bool(result.get("ok")),
            "error": str(result.get("error") or "")[:300] or None,
            "source": str(result.get("source") or "boss_mcp"),
            "screenshot_path": str(result.get("screenshot_path") or "") or None,
            # P3f: 明确此次是 BOSS 平台代发预设话术 (button_only) 还是 Pulse
            # 追加了 followup (button_and_followup). 复盘 "HR 为什么收到两条 / 一条"
            # 直接看这个字段, 不用再去 grep chrome 截图.
            "greet_strategy": str(result.get("greet_strategy") or "") or None,
        }
    )
    return result


def send_resume_attachment(
    *,
    conversation_id: str,
    resume_profile_id: str = "default",
    conversation_hint: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Send the user's resume as an attachment to an HR conversation.

    Mirrors the surface of :func:`reply_conversation` but the delivery
    channel is an actual file upload / built-in resume menu click rather
    than a plain-text message. Unknown executor modes fail closed so the
    business layer never mistakes a dry run for delivery.
    """
    safe_conv = str(conversation_id or "").strip()
    safe_profile = str(resume_profile_id or "default").strip() or "default"
    if not safe_conv:
        return {"ok": False, "status": "failed", "error": "conversation_id is required"}

    # ADR-001 §6 P3e idempotency barrier. Key = (conversation_id,
    # resume_profile_id): an attachment upload is inherently one-shot per
    # (HR × resume version) pair; an upstream HTTP retry must not deliver
    # the file twice.
    replay = _find_recent_successful_action(
        operation="send_resume_attachment",
        match={
            "conversation_id": safe_conv,
            "resume_profile_id": safe_profile,
        },
        within_sec=_idempotency_window_sec(),
    )
    if replay is not None:
        _append_action_log(
            {
                "action": "send_resume_attachment_idempotent_replay",
                "conversation_id": safe_conv,
                "resume_profile_id": safe_profile,
                "replay_of_logged_at": replay.get("logged_at"),
            }
        )
        return {
            "ok": True,
            "status": str(replay.get("status") or "sent"),
            "source": "boss_mcp",
            "error": None,
            "idempotent_replay": True,
            "resume_profile_id": safe_profile,
            "delivery_path": replay.get("delivery_path"),
            "attachment_path": replay.get("attachment_path"),
            "screenshot_path": replay.get("screenshot_path"),
        }

    _append_action_log(
        {
            "action": "send_resume_attachment",
            "conversation_id": safe_conv,
            "resume_profile_id": safe_profile,
            "conversation_hint": dict(conversation_hint or {}),
        }
    )

    mode = str(os.getenv("PULSE_BOSS_MCP_REPLY_MODE", "browser") or "").strip().lower()
    if mode in {"log_only", "dry_run_ok"}:
        logger.warning(
            "boss_mcp.killswitch.dry_run op=send_resume_attachment mode=%s "
            "conv=%s resume=%s — no browser action, returning logged_only",
            mode,
            safe_conv[:20],
            safe_profile,
        )
        result = {
            "ok": True,
            "status": "logged_only",
            "source": "boss_mcp",
            "error": None,
            "resume_profile_id": safe_profile,
        }
    elif mode in {"browser", "playwright"}:
        result = _run_browser_executor_with_retry(
            "send_resume_attachment",
            lambda: _execute_browser_send_resume_attachment(
                conversation_id=safe_conv,
                resume_profile_id=safe_profile,
                conversation_hint=dict(conversation_hint or {}),
            ),
        )
    else:
        result = {
            "ok": False,
            "status": "manual_required",
            "source": "boss_mcp",
            "error": "resume attachment executor is not configured; action is logged for audit",
            "resume_profile_id": safe_profile,
        }
    _append_action_log(
        {
            "action": "send_resume_attachment_result",
            "conversation_id": safe_conv,
            "resume_profile_id": safe_profile,
            "mode": mode,
            "status": str(result.get("status") or ""),
            "ok": bool(result.get("ok")),
            "error": str(result.get("error") or "")[:300] or None,
            "source": str(result.get("source") or "boss_mcp"),
            "delivery_path": str(result.get("delivery_path") or "") or None,
            "attachment_path": str(result.get("attachment_path") or "") or None,
            "screenshot_path": str(result.get("screenshot_path") or "") or None,
        }
    )
    return result


def click_conversation_card(
    *,
    conversation_id: str,
    card_id: str = "",
    card_type: str = "",
    action: str = "",
) -> dict[str, Any]:
    """Click an interactive card button (accept / reject / view)."""
    safe_conv = str(conversation_id or "").strip()
    safe_card_id = str(card_id or "").strip()
    safe_type = str(card_type or "").strip()
    safe_action = str(action or "").strip()
    if not safe_conv:
        return {"ok": False, "status": "failed", "error": "conversation_id is required"}
    if not safe_type:
        return {"ok": False, "status": "failed", "error": "card_type is required"}
    if not safe_action:
        return {"ok": False, "status": "failed", "error": "action is required"}

    _append_action_log(
        {
            "action": "click_conversation_card",
            "conversation_id": safe_conv,
            "card_id": safe_card_id,
            "card_type": safe_type,
            "card_action": safe_action,
        }
    )

    mode = str(os.getenv("PULSE_BOSS_MCP_REPLY_MODE", "browser") or "").strip().lower()
    if mode in {"log_only", "dry_run_ok"}:
        logger.warning(
            "boss_mcp.killswitch.dry_run op=click_conversation_card mode=%s "
            "conv=%s card_type=%s action=%s — no browser action, returning logged_only",
            mode,
            safe_conv[:20],
            safe_type,
            safe_action,
        )
        result = {
            "ok": True,
            "status": "logged_only",
            "source": "boss_mcp",
            "error": None,
            "card_type": safe_type,
            "card_action": safe_action,
        }
    elif mode in {"browser", "playwright"}:
        result = _run_browser_executor_with_retry(
            "click_conversation_card",
            lambda: _execute_browser_click_card(
                conversation_id=safe_conv,
                card_id=safe_card_id,
                card_type=safe_type,
                action=safe_action,
            ),
        )
    else:
        result = {
            "ok": False,
            "status": "manual_required",
            "source": "boss_mcp",
            "error": "card click executor is not configured; action is logged for audit",
            "card_type": safe_type,
            "card_action": safe_action,
        }
    _append_action_log(
        {
            "action": "click_conversation_card_result",
            "conversation_id": safe_conv,
            "card_id": safe_card_id,
            "card_type": safe_type,
            "card_action": safe_action,
            "mode": mode,
            "status": str(result.get("status") or ""),
            "ok": bool(result.get("ok")),
            "error": str(result.get("error") or "")[:300] or None,
            "screenshot_path": str(result.get("screenshot_path") or "") or None,
        }
    )
    return result


# ---------------------------------------------------------------------------
# Auto-reply orchestrator (ADR-004 Step A.4)
# ---------------------------------------------------------------------------


def _autoreply_disabled() -> bool:
    return str(os.getenv("PULSE_BOSS_AUTOREPLY", "on") or "on").strip().lower() == "off"


def _autoreply_force_dry_run() -> bool:
    flag = str(os.getenv("PULSE_BOSS_AUTOREPLY_FORCE_DRY_RUN", "off") or "off").strip().lower()
    return flag == "on"


def _summarize_autoreply_status(
    dry_run: bool,
    decisions: list[dict[str, Any]],
    executed: list[dict[str, Any]],
    errors: list[dict[str, Any]],
) -> str:
    if dry_run:
        return "preview"
    if errors and not executed:
        return "failed"
    if errors and executed:
        return "partial"
    if executed:
        return "succeeded"
    if decisions:
        return "skipped"
    return "no_result"


def run_auto_reply_cycle(
    *,
    max_conversations: int = 5,
    chat_tab: str = "未读",
    dry_run: bool = True,
    profile_id: str = "default",
    run_id: str = "",
) -> dict[str, Any]:
    """Scan unread chat-list → decide per-conversation action → optionally execute.

    ``dry_run=True`` (default) collects decisions without clicking or writing
    audit; ``dry_run=False`` executes agreed decisions behind an idempotency
    barrier keyed on ``(conversation_id, decision_kind, trigger_mid)``. See
    ``docs/adr/ADR-004-AutoReplyContract.md`` §4.4 for the full pipeline
    contract.
    """

    if _autoreply_disabled():
        return {
            "ok": False,
            "status": "disabled",
            "source": "boss_mcp",
            "error": "auto-reply disabled via PULSE_BOSS_AUTOREPLY=off",
            "dry_run": True,
            "decisions": [],
            "executed": [],
            "skipped": [],
            "errors": [],
        }

    effective_dry_run = bool(dry_run) or _autoreply_force_dry_run()
    safe_max = _safe_int(max_conversations, 5, min_value=1, max_value=20)
    safe_tab = str(chat_tab or "未读").strip() or "未读"
    safe_run_id = str(run_id or "").strip()
    safe_profile = str(profile_id or "default").strip() or "default"

    pull_result = _pull_conversations_via_browser(
        max_conversations=safe_max,
        unread_only=True,
        fetch_latest_hr=False,
        chat_tab=safe_tab,
    )
    if not bool(pull_result.get("ok")):
        return {
            "ok": False,
            "status": str(pull_result.get("status") or "no_result"),
            "source": "boss_mcp_browser",
            "error": "; ".join(str(e) for e in (pull_result.get("errors") or []))[:300] or None,
            "url": str(pull_result.get("url") or ""),
            "dry_run": effective_dry_run,
            "decisions": [],
            "executed": [],
            "skipped": [],
            "errors": list(pull_result.get("errors") or []),
        }
    items = list(pull_result.get("items") or [])[:safe_max]

    try:
        page = _ensure_browser_page()
    except Exception as exc:
        return {
            "ok": False,
            "status": "executor_unavailable",
            "source": "boss_mcp_browser",
            "error": str(exc)[:300],
            "dry_run": effective_dry_run,
            "decisions": [],
            "executed": [],
            "skipped": [],
            "errors": [str(exc)[:300]],
        }

    decisions: list[dict[str, Any]] = []
    executed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for idx, item in enumerate(items):
        cid = str(item.get("conversation_id") or "").strip()
        hint = {
            "hr_name": str(item.get("hr_name") or ""),
            "company": str(item.get("company") or ""),
            "job_title": str(item.get("job_title") or ""),
        }
        clicked, hint_selector = _try_click_conversation_by_hint(page, hint)
        if not clicked:
            errors.append(
                {
                    "conversation_id": cid,
                    "item_index": idx,
                    "step": "open_conversation",
                    "error": "cannot locate conversation by hint",
                    "hint": hint,
                }
            )
            continue

        try:
            page.wait_for_selector(
                ".chat-conversation",
                state="visible",
                timeout=min(_browser_timeout_ms(), 5000),
            )
        except Exception as exc:
            errors.append(
                {
                    "conversation_id": cid,
                    "item_index": idx,
                    "step": "wait_chat_detail",
                    "error": str(exc)[:200],
                }
            )
            continue

        state = extract_chat_detail_state(page)
        decision = decide_auto_reply_action(state)

        decision_row: dict[str, Any] = {
            "conversation_id": cid,
            "item_index": idx,
            "hr_name": hint["hr_name"],
            "company": hint["company"],
            "job_title": hint["job_title"],
            "decision_kind": decision.kind,
            "reason": decision.reason,
            "trigger_mid": decision.trigger_mid,
            "hint_selector": hint_selector,
            "last_sender": state.last_message.sender if state and state.last_message else "",
            "last_kind": state.last_message.kind if state and state.last_message else "",
            "pending_respond_text": state.pending_respond.text if state and state.pending_respond else "",
        }
        decisions.append(decision_row)

        if effective_dry_run:
            continue

        if decision.kind == "skip":
            skipped.append({**decision_row, "execute_status": "decision_skip"})
            continue

        if not decision.trigger_mid:
            skipped.append({**decision_row, "execute_status": "skipped_no_trigger_mid"})
            _append_action_log(
                {
                    "action": "auto_reply_skipped",
                    "conversation_id": cid,
                    "decision_kind": decision.kind,
                    "reason": "empty trigger_mid",
                    "run_id": safe_run_id,
                }
            )
            continue

        match = {
            "conversation_id": cid,
            "decision_kind": decision.kind,
            "trigger_mid": decision.trigger_mid,
        }
        prior = _find_recent_successful_action(
            operation="auto_reply",
            match=match,
            within_sec=_idempotency_window_sec(),
        )
        if prior is not None:
            skipped.append({**decision_row, "execute_status": "idempotent_replay"})
            _append_action_log(
                {
                    "action": "auto_reply_result",
                    **match,
                    "ok": True,
                    "status": "idempotent_replay",
                    "run_id": safe_run_id,
                    "replayed_from": str(prior.get("logged_at") or ""),
                }
            )
            continue

        if decision.kind == "click_respond_agree":
            exec_result = _click_respond_popover_via_browser(
                page, decision="agree", conversation_id=cid
            )
        elif decision.kind == "click_respond_refuse":
            exec_result = _click_respond_popover_via_browser(
                page, decision="refuse", conversation_id=cid
            )
        elif decision.kind == "click_send_resume":
            exec_result = _click_send_resume_via_browser(page, conversation_id=cid)
        else:
            exec_result = {
                "ok": False,
                "status": "unsupported_kind",
                "error": f"orchestrator has no executor for {decision.kind!r}",
            }

        _append_action_log(
            {
                "action": "auto_reply_result",
                **match,
                "run_id": safe_run_id,
                "profile_id": safe_profile,
                "ok": bool(exec_result.get("ok")),
                "status": str(exec_result.get("status") or ""),
                "error": str(exec_result.get("error") or "")[:300] or None,
                "url": str(exec_result.get("url") or "") or None,
                "screenshot_path": str(exec_result.get("screenshot_path") or "") or None,
                "button_selector": str(exec_result.get("button_selector") or "") or None,
            }
        )
        if bool(exec_result.get("ok")):
            executed.append(
                {
                    **decision_row,
                    "execute_status": str(exec_result.get("status") or ""),
                    "screenshot_path": str(exec_result.get("screenshot_path") or "") or None,
                }
            )
        else:
            errors.append(
                {
                    **decision_row,
                    "step": "execute",
                    "execute_status": str(exec_result.get("status") or ""),
                    "error": str(exec_result.get("error") or "")[:300],
                }
            )

    status = _summarize_autoreply_status(effective_dry_run, decisions, executed, errors)
    return {
        "ok": True,
        "status": status,
        "source": "boss_mcp_browser",
        "dry_run": effective_dry_run,
        "scanned": len(items),
        "chat_tab": safe_tab,
        "run_id": safe_run_id,
        "decisions": decisions,
        "executed": executed,
        "skipped": skipped,
        "errors": errors,
    }


def mark_processed(*, conversation_id: str, run_id: str, note: str = "") -> dict[str, Any]:
    safe_conversation_id = str(conversation_id or "").strip()
    if not safe_conversation_id:
        return {"ok": False, "status": "failed", "error": "conversation_id is required"}
    _append_action_log(
        {
            "action": "mark_processed",
            "conversation_id": safe_conversation_id,
            "run_id": str(run_id or "").strip(),
            "note": str(note or "").strip(),
        }
    )
    return {
        "ok": True,
        "status": "marked",
        "source": "boss_mcp",
        "error": None,
    }


def health() -> dict[str, Any]:
    with _BROWSER_LOCK:
        runtime_open = False
        if _PAGE is not None:
            try:
                runtime_open = not bool(_PAGE.is_closed())
            except (RuntimeError, OSError, AttributeError, ValueError):
                runtime_open = True
        idle_elapsed_sec = round(_browser_idle_elapsed_sec(), 3)
        idle_close_sec = _browser_idle_close_sec()
    return {
        "ok": True,
        "source": "boss_mcp",
        "inbox_path": str(_inbox_path()),
        "action_audit_path": str(_action_audit_path()),
        "scan_mode": _scan_mode(),
        "pull_mode": _pull_mode(),
        "seed_fallback_enabled": _allow_seed_fallback(),
        # Defaults MUST stay in lock-step with mutating action readers.
        "reply_mode": str(os.getenv("PULSE_BOSS_MCP_REPLY_MODE", "browser")).strip() or "browser",
        "greet_mode": str(os.getenv("PULSE_BOSS_MCP_GREET_MODE", "browser")).strip() or "browser",
        # P3f: "off" = BOSS 平台代发 APP 预设话术 (默认, 与真实账号配置一致);
        # "on" = Pulse 点完 "立即沟通" 再 fill + send greeting_text 作为第二条.
        "greet_followup": str(os.getenv("PULSE_BOSS_MCP_GREET_FOLLOWUP", "off") or "off").strip() or "off",
        "browser": {
            "profile_dir": str(_browser_profile_dir()),
            "headless": _browser_headless(),
            "timeout_ms": _browser_timeout_ms(),
            "channel": _browser_channel() or None,
            "user_agent": _browser_user_agent() or "chromium-default",
            "stealth_enabled": _browser_stealth_enabled(),
            "block_iframe_core": _browser_block_iframe_core(),
            "login_check_url": str(os.getenv("PULSE_BOSS_LOGIN_CHECK_URL", "https://www.zhipin.com/web/geek/chat")),
            "chat_url_template": str(os.getenv("PULSE_BOSS_CHAT_URL_TEMPLATE", "") or "").strip() or None,
            "screenshot_dir": str(_browser_screenshot_dir()) if _browser_screenshot_dir() is not None else None,
            "executor_retry_count": _browser_executor_retry_count(),
            "executor_retry_backoff_ms": _browser_executor_retry_backoff_ms(),
            "idle_close_sec": idle_close_sec,
            "idle_elapsed_sec": idle_elapsed_sec,
            "runtime_open": runtime_open,
            "risk_keywords": _risk_keywords(),
            "greet_button_selectors": _csv_list(os.getenv("PULSE_BOSS_GREET_BUTTON_SELECTORS", "")),
            "chat_input_selectors": _csv_list(os.getenv("PULSE_BOSS_CHAT_INPUT_SELECTORS", "")),
            "chat_send_selectors": _csv_list(os.getenv("PULSE_BOSS_CHAT_SEND_SELECTORS", "")),
            "search_url_template": _search_url_template(),
            "search_next_selectors": _csv_list(os.getenv("PULSE_BOSS_SEARCH_NEXT_SELECTORS", "")),
            "job_card_selectors": _csv_list(os.getenv("PULSE_BOSS_JOB_CARD_SELECTORS", "")),
            "job_nav_selectors": _csv_list(os.getenv("PULSE_BOSS_JOB_NAV_SELECTORS", "")),
            "job_search_input_selectors": _csv_list(os.getenv("PULSE_BOSS_JOB_SEARCH_INPUT_SELECTORS", "")),
            "chat_list_url": _chat_list_url(),
            "chat_row_selectors": _csv_list(os.getenv("PULSE_BOSS_CHAT_ROW_SELECTORS", "")),
            "chat_tab_selectors": _csv_list(os.getenv("PULSE_BOSS_CHAT_TAB_SELECTORS", "")),
        },
    }
