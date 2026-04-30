"""WeCom AI Bot long-connection channel adapter for Pulse Agent Runtime.

Uses the official wecom-aibot-sdk-python to maintain a WebSocket
long-connection with WeCom's intelligent robot platform.
No domain, no ICP filing, no public IP required.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
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
        self._inflight_tasks: set[asyncio.Task[Any]] = set()
        self._ws_client_cls: Any = None
        self._event_handlers: dict[str, Callable[..., Any]] = {}

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

        self._ws_client_cls = WSClient

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

            if len(self._inflight_tasks) >= self._MAX_INFLIGHT_MESSAGES:
                await self._reply_stream_with_retry(
                    frame=frame,
                    reply_text="当前请求较多，我已收到你的消息，请稍后再试。",
                    user_id=user_id,
                    max_attempts=1,
                )
                logger.warning(
                    "wechat.msg.drop_due_to_backpressure user=%s inflight=%d",
                    user_id,
                    len(self._inflight_tasks),
                )
                return

            worker = asyncio.create_task(
                self._handle_message_worker(
                    frame=frame,
                    message=message,
                    user_id=user_id,
                )
            )
            self._inflight_tasks.add(worker)
            worker.add_done_callback(self._on_worker_done)

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

        self._event_handlers = {
            "message.text": on_text,
            "event.enter_chat": on_enter,
            "connected": on_connected,
            "authenticated": on_authenticated,
            "disconnected": on_disconnected,
            "error": on_error,
        }
        self._client = self._create_client()

        logger.info("wechat-work-bot connecting (bot_id=%s)...", self.bot_id[:8])
        self._task = asyncio.create_task(self._run_forever())

    def _create_client(self) -> Any:
        if self._ws_client_cls is None:
            raise RuntimeError("wechat-work-bot client class is not initialized")
        client = self._ws_client_cls({
            "bot_id": self.bot_id,
            "secret": self.bot_secret,
        })
        for event, handler in self._event_handlers.items():
            client.on(event, handler)
        return client

    @staticmethod
    def _stream_id() -> str:
        try:
            from wecom_aibot_sdk import generate_req_id

            return str(generate_req_id("stream"))
        except Exception:
            return f"stream_{uuid.uuid4().hex[:12]}"

    async def _handle_message_worker(
        self,
        *,
        frame: Any,
        message: IncomingMessage,
        user_id: str,
    ) -> None:
        """Run dispatch in background and respond with staged feedback."""
        ack_sent = False
        finished = asyncio.Event()

        async def _delayed_ack() -> None:
            nonlocal ack_sent
            await asyncio.sleep(self._LONG_TASK_ACK_DELAY_SEC)
            if finished.is_set():
                return
            # Use a standalone finished stream so the ack is always flushed
            # as a complete message instead of being merged/truncated by
            # downstream incremental-render semantics.
            ack_sent = await self._reply_stream_with_retry(
                frame=frame,
                reply_text=self._LONG_TASK_ACK_TEXT,
                user_id=user_id,
                finish=True,
                max_attempts=1,
            )
            if ack_sent:
                logger.info("wechat.msg.reply.ack_sent user=%s", user_id)

        ack_task = asyncio.create_task(_delayed_ack())
        result: Any = None
        try:
            result = self.dispatch(message)
            if asyncio.iscoroutine(result) or asyncio.isfuture(result):
                result = await result
        finally:
            finished.set()
            ack_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ack_task

        reply_text = _extract_reply(result)
        if not reply_text and ack_sent:
            reply_text = "本次请求已处理完成。"
        if not reply_text:
            return

        delivered = await self._reply_stream_with_retry(
            frame=frame,
            reply_text=reply_text,
            user_id=user_id,
            finish=True,
        )
        trace_id = ""
        if isinstance(result, dict):
            trace_id = str(result.get("trace_id") or "").strip()
            result["reply_delivered"] = bool(delivered)
        req_id = ""
        if hasattr(frame, "headers") and isinstance(frame.headers, dict):
            req_id = str(frame.headers.get("req_id") or "").strip()
        if delivered:
            logger.info(
                "wechat.msg.reply.delivered trace=%s user=%s req_id=%s",
                trace_id or "-",
                user_id,
                req_id or "-",
            )
        else:
            logger.error(
                "wechat.msg.reply.drop trace=%s user=%s req_id=%s",
                trace_id or "-",
                user_id,
                req_id or "-",
            )

    def _on_worker_done(self, task: asyncio.Task[Any]) -> None:
        self._inflight_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("wechat.msg.worker.failed err=%s", exc, exc_info=exc)

    async def _reply_stream_with_retry(
        self,
        *,
        frame: Any,
        reply_text: str,
        user_id: str,
        finish: bool = True,
        stream_id: str | None = None,
        max_attempts: int = 3,
    ) -> bool:
        safe_reply = str(reply_text or "").strip()
        if not safe_reply:
            return False
        safe_attempts = max(1, int(max_attempts))
        stream = str(stream_id or "").strip() or self._stream_id()
        for attempt in range(1, safe_attempts + 1):
            client = self._client
            if client is None:
                logger.warning(
                    "wechat.msg.reply.skip_no_client user=%s attempt=%d",
                    user_id,
                    attempt,
                )
                # First miss: short wait for race window. Subsequent misses:
                # align with reconnect cadence (initial delay is 5s).
                await asyncio.sleep(0.6 if attempt == 1 else self._RECONNECT_INITIAL_DELAY)
                continue
            if not bool(getattr(client, "is_connected", True)):
                logger.warning(
                    "wechat.msg.reply.skip_disconnected_client user=%s attempt=%d",
                    user_id,
                    attempt,
                )
                await asyncio.sleep(self._RECONNECT_INITIAL_DELAY)
                continue
            try:
                await client.reply_stream(
                    frame,
                    stream,
                    safe_reply,
                    finish=bool(finish),
                )
                logger.info(
                    "wechat.msg.reply.ok user=%s reply_chars=%d attempt=%d finish=%s",
                    user_id,
                    len(safe_reply),
                    attempt,
                    bool(finish),
                )
                return True
            except (ConnectionError, OSError, RuntimeError, AttributeError, TimeoutError) as exc:
                logger.exception(
                    "wechat.msg.reply.failed user=%s attempt=%d finish=%s err=%s",
                    user_id,
                    attempt,
                    bool(finish),
                    exc,
                )
                # "WebSocket not connected" should wait for reconnect loop;
                # generic IO errors keep a short backoff.
                if "not connected" in str(exc).lower():
                    await asyncio.sleep(self._RECONNECT_INITIAL_DELAY)
                else:
                    await asyncio.sleep(0.8)
        return False

    # 重连退避: 5s 起步, 翻倍封顶 30s.
    # 历史 1s 起步会让 SDK 后台尚未释放老会话时新 client 就上线,
    # WeCom 服务端把双连接判成 "kicked by new connection", 反复自杀.
    _RECONNECT_INITIAL_DELAY = 5.0
    _RECONNECT_MAX_DELAY = 30.0
    _LONG_TASK_ACK_DELAY_SEC = 2.0
    _LONG_TASK_ACK_TEXT = "已收到，正在处理中，预计 1-3 分钟给你完整结果。"
    _MAX_INFLIGHT_MESSAGES = 32

    async def _run_forever(self) -> None:
        """Single-flight reconnect loop.

        关键不变量: 任何时刻只能存在一个仍在握 bot_id 的 WSClient. 拆解:
          1. 连接断开 (内层 while 退出 / 抛异常) 后, 必须先把当前 client
             显式 ``disconnect()`` 并清掉引用, 再创建新 client.
             否则老 client 内部还在重连, 跟新 client 抢同一个 bot_id ->
             服务端踢掉一方 ("Received disconnected_event, kicked by
             new connection"), 表现为日志里 1-2 分钟一次自杀循环.
          2. 重建之前显式 sleep 一段, 让 WeCom 那边把上一会话回收;
             立即重连只会再次拿到 kicked.
        """
        reconnect_delay = self._RECONNECT_INITIAL_DELAY
        while True:
            try:
                if self._client is None:
                    self._client = self._create_client()
                await self._client.connect_async()
                reconnect_delay = self._RECONNECT_INITIAL_DELAY
                while self._client and self._client.is_connected:
                    await asyncio.sleep(1)
                logger.warning(
                    "wechat-work-bot connection ended; "
                    "disposing old client before reconnect"
                )
            except asyncio.CancelledError:
                logger.info("wechat-work-bot task cancelled")
                await self._dispose_current_client()
                raise
            except (ConnectionError, OSError, RuntimeError) as exc:
                logger.warning(
                    "wechat-work-bot connection error, will dispose+retry: "
                    "err_type=%s err_repr=%r",
                    type(exc).__name__,
                    exc,
                    exc_info=exc,
                )

            await self._dispose_current_client()
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(
                reconnect_delay * 2, self._RECONNECT_MAX_DELAY
            )

    async def _dispose_current_client(self) -> None:
        """显式释放当前 SDK client, 避免与下一次连接抢同一个 bot_id.

        SDK 内部线程/任务可能还在重试; 这里 best-effort 调一次 disconnect.
        异常吞掉是有意的: 即便 disconnect 失败, 也要把 ``self._client``
        清成 ``None`` 让下一轮强制重建; 否则会卡在"老 client 半死不活,
        新 client 永远不会被建出来"的状态.
        """
        client = self._client
        if client is None:
            return
        self._client = None
        try:
            await client.disconnect()
        except (ConnectionError, OSError, RuntimeError, AttributeError) as exc:
            logger.warning(
                "wechat-work-bot dispose client soft-failed (continuing): "
                "err_type=%s err_repr=%r",
                type(exc).__name__,
                exc,
                exc_info=exc,
            )

    async def stop(self) -> None:
        if self._inflight_tasks:
            workers = list(self._inflight_tasks)
            for task in workers:
                task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            self._inflight_tasks.clear()
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
        self._client = None
        logger.info("wechat-work-bot stopped")


def _extract_reply(result: Any) -> str:
    """Extract reply text from Brain dispatch result."""
    if not result:
        logger.debug("_extract_reply: empty result")
        return ""
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        top_level_reply = str(result.get("reply") or result.get("answer") or "").strip()
        if top_level_reply:
            return top_level_reply
        if "brain" in result:
            # Successful brain envelope but empty answer — keep silent
            # (Brain decided not to reply). Don't surface the error field
            # here even if upstream attached one for audit; success path
            # is authoritative.
            logger.warning("_extract_reply: standard dispatch envelope has no reply")
            return ""
        # No brain field → server bailed out before Brain finished
        # (policy block / module not found / brain.run raised). Surface
        # the error so the bot doesn't go silent and hide backend bugs.
        envelope_error = str(result.get("error") or "").strip()
        if envelope_error:
            trace_id = str(result.get("trace_id") or "").strip()
            logger.warning(
                "_extract_reply: dispatch envelope reports error: %s (trace_id=%s)",
                envelope_error,
                trace_id or "-",
            )
            tail = f"，trace_id={trace_id}" if trace_id else ""
            return f"⚠️ 后端处理失败：{envelope_error[:300]}{tail}"
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
        # Top-level ``text`` in the standard channel dispatch envelope is the
        # user's original message, not an assistant reply. Never echo it.
        logger.warning("_extract_reply: unrecognized result structure: %s", list(result.keys()))
        return ""
    logger.warning("_extract_reply: unexpected type %s", type(result).__name__)
    return ""
