"""WeCom AI Bot long-connection channel adapter for Pulse Agent Runtime.

Uses the official wecom-aibot-sdk-python to maintain a WebSocket
long-connection with WeCom's intelligent robot platform.
No domain, no ICP filing, no public IP required.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Callable

from .base import BaseChannelAdapter, IncomingMessage

logger = logging.getLogger(__name__)


class WechatWorkBotAdapter(BaseChannelAdapter):
    """Channel adapter using WeCom AI Bot WebSocket long-connection."""

    name = "wechat-work-bot"

    def __init__(self, *, bot_id: str = "", bot_secret: str = "") -> None:
        super().__init__()
        self.bot_id = bot_id
        self.bot_secret = bot_secret
        self._client: Any = None
        self._task: asyncio.Task | None = None

    @property
    def configured(self) -> bool:
        return bool(self.bot_id and self.bot_secret)

    def parse_incoming(self, payload: Any) -> IncomingMessage | None:
        if not isinstance(payload, dict):
            return None
        text = str(payload.get("text", "")).strip()
        if not text:
            return None
        return IncomingMessage(
            channel=self.name,
            user_id=payload.get("user_id", ""),
            text=text,
            metadata=payload.get("metadata", {}),
            received_at=datetime.now(timezone.utc),
        )

    async def start(self, dispatch_fn: Callable | None = None) -> None:
        """Start the WebSocket long-connection to WeCom.

        Fail-fast 契约: 调用方已经通过 ``self.configured`` 判定 True 才会进来.
        因此:
          - SDK 缺失直接 ``raise RuntimeError``, 让 uvicorn 启动失败并打印红字修复提示,
            而不是 ``return`` 后假装服务 OK (用户永远等不到回复).
          - configured=False 的情况由调用方(lifespan)跳过, 这里不再静默 return.
        """
        if not self.configured:
            raise RuntimeError(
                "WechatWorkBotAdapter.start() called but not configured; "
                "caller should check .configured first"
            )

        try:
            from wecom_aibot_sdk import WSClient  # noqa: F401 — 仅用于探测是否可导入
        except ImportError as exc:
            raise RuntimeError(
                "wechat-work-bot configured (bot_id/bot_secret set) but "
                "'wecom-aibot-sdk-python' is NOT installed. "
                "Fix: `pip install wecom-aibot-sdk-python` "
                "(or install the Pulse extra: `pip install -e '.[channels-wecom]'`). "
                "Refusing to start silently — the bot would never receive messages."
            ) from exc
        from wecom_aibot_sdk import WSClient  # re-import for real use

        if dispatch_fn is not None:
            self.set_handler(dispatch_fn)

        self._client = WSClient({
            "bot_id": self.bot_id,
            "secret": self.bot_secret,
        })

        async def on_text(frame: Any) -> None:
            body = frame.body if hasattr(frame, "body") else {}
            text_obj = body.get("text", {})
            content = text_obj.get("content", "")
            if not content:
                return

            sender = body.get("sender", {})
            user_id = sender.get("user_id", "") or sender.get("name", "unknown")
            chat_id = body.get("chat_id", "")

            logger.info(
                "wechat.msg.received user=%s chat=%s text_chars=%d preview=%s",
                user_id,
                chat_id or "-",
                len(content),
                content[:80],
            )

            message = self.parse_incoming({
                "text": content,
                "user_id": user_id,
                "metadata": {
                    "chat_id": chat_id,
                    "sender": sender,
                    "frame_headers": dict(frame.headers) if hasattr(frame, "headers") else {},
                },
            })
            if message is None:
                return

            result = self.dispatch(message)
            if asyncio.iscoroutine(result) or asyncio.isfuture(result):
                result = await result

            reply_text = _extract_reply(result)
            if reply_text and self._client:
                try:
                    from wecom_aibot_sdk import generate_req_id
                    stream_id = generate_req_id("stream")
                    await self._client.reply_stream(
                        frame, stream_id, reply_text, finish=True
                    )
                    logger.info(
                        "wechat.msg.reply.ok user=%s reply_chars=%d",
                        user_id,
                        len(reply_text),
                    )
                except Exception:
                    logger.exception("wechat.msg.reply.failed user=%s", user_id)

        async def on_enter(frame: Any) -> None:
            try:
                await self._client.reply_welcome(frame, {
                    "msgtype": "text",
                    "text": {"content": "你好，我是 Pulse AI 助手，有什么可以帮你的？"},
                })
            except Exception as e:
                logger.warning("wechat-work-bot welcome failed: %s", e)

        async def on_connected(*args: Any) -> None:
            logger.info("wechat-work-bot WebSocket connected")

        async def on_authenticated(*args: Any) -> None:
            logger.info("wechat-work-bot authenticated successfully")

        async def on_disconnected(*args: Any) -> None:
            logger.warning("wechat-work-bot disconnected: %s", args)

        async def on_error(*args: Any) -> None:
            logger.error("wechat-work-bot error: %s", args)

        self._client.on("message.text", on_text)
        self._client.on("event.enter_chat", on_enter)
        self._client.on("connected", on_connected)
        self._client.on("authenticated", on_authenticated)
        self._client.on("disconnected", on_disconnected)
        self._client.on("error", on_error)

        logger.info("wechat-work-bot connecting (bot_id=%s)...", self.bot_id[:8])
        self._task = asyncio.create_task(self._run_forever())

    async def _run_forever(self) -> None:
        try:
            await self._client.connect_async()
            while self._client and self._client.is_connected:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            logger.info("wechat-work-bot task cancelled")
        except Exception as e:
            logger.error("wechat-work-bot connection error: %s", e)

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._client:
            try:
                await self._client.disconnect()
            except Exception:
                pass
        logger.info("wechat-work-bot stopped")


def _extract_reply(result: Any) -> str:
    """Extract reply text from Brain dispatch result."""
    if not result:
        logger.debug("_extract_reply: empty result")
        return ""
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        # Try nested result.result first (standard Brain response)
        brain_result = result.get("result")
        if isinstance(brain_result, dict):
            text = str(
                brain_result.get("answer")
                or brain_result.get("text")
                or brain_result.get("reply")
                or ""
            )
            if not text:
                logger.warning("_extract_reply: brain_result dict has no answer/text/reply keys: %s", list(brain_result.keys()))
            return text
        if isinstance(brain_result, str):
            return brain_result
        # Fallback: top-level keys
        text = str(result.get("answer") or result.get("text") or "")
        if not text:
            logger.warning("_extract_reply: unrecognized result structure: %s", list(result.keys()))
        return text
    logger.warning("_extract_reply: unexpected type %s", type(result).__name__)
    return ""
