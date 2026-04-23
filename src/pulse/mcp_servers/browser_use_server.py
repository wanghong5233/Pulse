from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path
from threading import Lock

from fastmcp import FastMCP

_MCP = FastMCP("browser-use")
_LOCK = Lock()
_PLAYWRIGHT_MANAGER = None
_PLAYWRIGHT = None
_CONTEXT = None
_PAGE = None


def _headless() -> bool:
    return os.getenv("BOSS_HEADLESS", "false").strip().lower() in {"1", "true", "yes"}


def _profile_dir() -> Path:
    configured = os.getenv("BOSS_BROWSER_PROFILE_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path(__file__).resolve().parents[1] / ".playwright" / "boss_mcp").resolve()


def _screenshot_dir() -> Path | None:
    configured = os.getenv("BOSS_SCREENSHOT_DIR", "").strip() or os.getenv(
        "BROWSER_USE_SCREENSHOT_DIR", ""
    ).strip()
    if configured:
        return Path(configured).expanduser().resolve()
    enabled = os.getenv("BROWSER_USE_SCREENSHOTS_ENABLED", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if not enabled:
        return None
    return (Path(__file__).resolve().parents[1] / "logs" / "browser_use_screenshots").resolve()


def _ensure_page():
    global _PLAYWRIGHT_MANAGER, _PLAYWRIGHT, _CONTEXT, _PAGE
    with _LOCK:
        if _PAGE is not None:
            try:
                if not _PAGE.is_closed():
                    return _PAGE
            except Exception:
                pass

        from playwright.sync_api import sync_playwright

        profile_dir = _profile_dir()
        profile_dir.mkdir(parents=True, exist_ok=True)
        shot_dir = _screenshot_dir()
        if shot_dir is not None:
            shot_dir.mkdir(parents=True, exist_ok=True)

        if _PLAYWRIGHT_MANAGER is None:
            _PLAYWRIGHT_MANAGER = sync_playwright()
            _PLAYWRIGHT = _PLAYWRIGHT_MANAGER.start()

        _CONTEXT = _PLAYWRIGHT.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=_headless(),
        )
        _PAGE = _CONTEXT.pages[0] if _CONTEXT.pages else _CONTEXT.new_page()
        _PAGE.set_default_timeout(20000)
        return _PAGE


@_MCP.tool
def navigate(url: str) -> str:
    """Navigate to url and return current page title."""
    page = _ensure_page()
    page.goto(url, wait_until="domcontentloaded")
    try:
        title = page.title()
    except Exception:
        title = ""
    return f"url={page.url}; title={title}"


@_MCP.tool
def click(selector: str) -> str:
    """Click a CSS selector."""
    page = _ensure_page()
    page.click(selector)
    return f"clicked={selector}; url={page.url}"


@_MCP.tool
def extract_text(selector: str, max_chars: int = 2000) -> str:
    """Extract text content from the first matching selector."""
    page = _ensure_page()
    text = page.locator(selector).first.inner_text()
    return text[: max(100, min(max_chars, 10000))]


@_MCP.tool
def screenshot(name: str = "") -> str:
    """Capture a screenshot and return file path."""
    page = _ensure_page()
    shot_dir = _screenshot_dir()
    if shot_dir is None:
        return "screenshot disabled: set BROWSER_USE_SCREENSHOTS_ENABLED=true or BROWSER_USE_SCREENSHOT_DIR"
    shot_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in name).strip("_")
    file_name = f"{stamp}_{safe or 'mcp'}.png"
    path = shot_dir / file_name
    page.screenshot(path=str(path), full_page=True)
    return str(path)


@_MCP.tool
def wait(ms: int = 1000) -> str:
    """Wait given milliseconds."""
    bounded = max(0, min(ms, 60000))
    time.sleep(bounded / 1000.0)
    return f"waited_ms={bounded}"


@_MCP.tool
def current_url() -> str:
    """Return current page URL."""
    page = _ensure_page()
    return page.url


if __name__ == "__main__":
    _MCP.run()
