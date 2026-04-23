from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

logger = logging.getLogger(__name__)

EmitFunc = Callable[[str, str, dict[str, Any] | None], None]
NotifyFunc = Callable[[], None]
ResetFunc = Callable[[], None]
NowFunc = Callable[[], datetime]

DEFAULT_LOGIN_MARKERS = ("/web/user/", "/login", "passport.zhipin.com")
DEFAULT_LOGIN_SELECTOR = ".login-form, .qr-code-area, .scan-login, .login-container"


def check_login_required(
    page: Any,
    *,
    login_markers: Iterable[str] = DEFAULT_LOGIN_MARKERS,
    login_selector: str = DEFAULT_LOGIN_SELECTOR,
) -> bool:
    """Detect whether current page indicates an unauthenticated state."""
    try:
        current_url = getattr(page, "url", "") or ""
        for marker in login_markers:
            if marker and marker in current_url:
                return True
        locator = page.locator(login_selector)
        if locator.count() > 0:
            return True
    except Exception as exc:
        logger.warning("check_login_required probe swallowed exception: %s", exc)
    return False


def handle_cookie_expired(
    page: Any,
    *,
    operation: str,
    screenshot_dir: Path | None = None,
    screenshot_prefix: str = "cookie_expired",
    emit: EmitFunc | None = None,
    notify: NotifyFunc | None = None,
    reset_session: ResetFunc | None = None,
    now: NowFunc | None = None,
) -> Path | None:
    """
    Unified cookie-expired handler.

    Returns screenshot path when successfully captured; otherwise None.
    """
    if emit is not None:
        emit("error", f"Cookie 已过期，{operation} 无法继续，请重新登录", None)

    screenshot_path: Path | None = None
    if screenshot_dir is not None:
        try:
            screenshot_dir.mkdir(parents=True, exist_ok=True)
            clock = now() if now is not None else datetime.utcnow()
            suffix = clock.strftime("%Y%m%d_%H%M%S")
            screenshot_path = screenshot_dir / f"{screenshot_prefix}_{suffix}.png"
            page.screenshot(path=str(screenshot_path))
            if emit is not None:
                emit("browser_screenshot", "Cookie 过期截图", {"path": str(screenshot_path)})
        except Exception as exc:
            logger.warning(
                "handle_cookie_expired screenshot failed (operation=%s): %s",
                operation, exc,
            )
            screenshot_path = None

    if notify is not None:
        try:
            notify()
        except Exception as exc:
            logger.warning(
                "handle_cookie_expired notify failed (operation=%s): %s",
                operation, exc,
            )

    if reset_session is not None:
        try:
            reset_session()
        except Exception as exc:
            logger.warning(
                "handle_cookie_expired reset_session failed (operation=%s): %s",
                operation, exc,
            )

    return screenshot_path
