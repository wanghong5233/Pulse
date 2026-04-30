from __future__ import annotations

import asyncio

import pytest

from pulse.core.channel.wechat_work_bot import WechatWorkBotAdapter
from pulse.core.channel.wechat_work_bot import _extract_reply
from pulse.core.channel.base import IncomingMessage


def test_extract_reply_uses_nested_brain_answer() -> None:
    result = {
        "channel": "wechat-work-bot",
        "user_id": "u1",
        "text": "开启自动投递服务",
        "result": {"answer": "已开启自动投递服务。"},
    }

    assert _extract_reply(result) == "已开启自动投递服务。"


def test_extract_reply_prefers_standard_dispatch_reply_field() -> None:
    result = {
        "channel": "wechat-work-bot",
        "user_id": "u1",
        "text": "开启自动投递服务",
        "reply": "已开启后台任务 job_greet.patrol。",
        "result": {
            "ok": True,
            "name": "job_greet.patrol",
            "enabled": True,
        },
    }

    assert _extract_reply(result) == "已开启后台任务 job_greet.patrol。"


def test_extract_reply_never_echoes_dispatch_envelope_text() -> None:
    result = {
        "channel": "wechat-work-bot",
        "user_id": "u1",
        "text": "开启自动投递服务",
        "brain": {"answer": ""},
        "result": {"text": "业务结果里的 text 也不能发"},
        "error": "upstream failed",
    }

    assert _extract_reply(result) == ""


def test_extract_reply_accepts_top_level_answer_but_not_top_level_text() -> None:
    assert _extract_reply({"answer": "ok"}) == "ok"
    assert _extract_reply({"text": "user input"}) == ""


def test_extract_reply_surfaces_envelope_error_when_brain_absent() -> None:
    result = {
        "channel": "wechat-work-bot",
        "user_id": "u1",
        "text": "开启自动投递服务",
        "trace_id": "trace_xyz",
        "error": "RuntimeError: patrol not found",
        "mode": "brain",
    }

    reply = _extract_reply(result)
    assert "RuntimeError" in reply
    assert "trace_xyz" in reply


class _FakeReplyClient:
    def __init__(self, *, fail_times: int) -> None:
        self.fail_times = fail_times
        self.calls = 0
        self.is_connected = True
        self.stream_ids: list[str] = []
        self.finishes: list[bool] = []
        self.texts: list[str] = []

    async def reply_stream(self, frame: object, stream_id: str, text: str, finish: bool = True) -> None:
        self.calls += 1
        self.stream_ids.append(stream_id)
        self.finishes.append(bool(finish))
        self.texts.append(str(text))
        if self.calls <= self.fail_times:
            raise RuntimeError("simulated send failure")


@pytest.mark.asyncio
async def test_reply_stream_with_retry_succeeds_on_second_attempt() -> None:
    adapter = WechatWorkBotAdapter(bot_id="b", bot_secret="s")
    client = _FakeReplyClient(fail_times=1)
    adapter._client = client

    ok = await adapter._reply_stream_with_retry(
        frame=object(),
        reply_text="done",
        user_id="u1",
    )

    assert ok is True
    assert client.calls == 2


@pytest.mark.asyncio
async def test_reply_stream_with_retry_returns_false_after_retries() -> None:
    adapter = WechatWorkBotAdapter(bot_id="b", bot_secret="s")
    client = _FakeReplyClient(fail_times=3)
    adapter._client = client

    ok = await adapter._reply_stream_with_retry(
        frame=object(),
        reply_text="done",
        user_id="u1",
    )

    assert ok is False
    assert client.calls == 3


@pytest.mark.asyncio
async def test_reply_stream_with_retry_honors_finish_and_stream_id() -> None:
    adapter = WechatWorkBotAdapter(bot_id="b", bot_secret="s")
    client = _FakeReplyClient(fail_times=0)
    adapter._client = client

    ok = await adapter._reply_stream_with_retry(
        frame=object(),
        reply_text="processing",
        user_id="u1",
        finish=False,
        stream_id="stream_fixed",
        max_attempts=1,
    )

    assert ok is True
    assert client.calls == 1
    assert client.stream_ids == ["stream_fixed"]
    assert client.finishes == [False]


class _FakeFrame:
    def __init__(self) -> None:
        self.headers = {"req_id": "req_1"}


@pytest.mark.asyncio
async def test_handle_message_worker_sends_ack_then_final_reply() -> None:
    adapter = WechatWorkBotAdapter(bot_id="b", bot_secret="s")
    adapter._LONG_TASK_ACK_DELAY_SEC = 0.0
    client = _FakeReplyClient(fail_times=0)
    adapter._client = client
    stream_ids = iter(["stream_ack", "stream_final"])
    adapter._stream_id = lambda: next(stream_ids)  # type: ignore[method-assign]

    async def _dispatch(_message: IncomingMessage) -> dict[str, str]:
        await asyncio.sleep(0.01)
        return {"reply": "final-reply", "trace_id": "trace_x"}

    adapter.set_handler(_dispatch)
    msg = IncomingMessage(channel="wechat-work-bot", user_id="u1", text="hello")
    await adapter._handle_message_worker(frame=_FakeFrame(), message=msg, user_id="u1")

    assert client.calls == 2
    assert client.finishes == [True, True]
    assert client.stream_ids == ["stream_ack", "stream_final"]
    assert "处理中" in client.texts[0]
    assert client.texts[1] == "final-reply"


# ---------------------------------------------------------------------------
# Single-flight reconnect contract (terminal log 00:13:55 → 00:19:17 root cause)
#
# 在线日志 1-2 分钟一次的 "kicked by new connection" 自杀循环, 根因是
# 老 client 的 SDK 后台任务还活着, 已经被替换的 client 引用直接被丢, 两个
# client 同时握 bot_id 登入服务端, 服务端踢掉一方. 这里用 Fake client 锁
# 住一条不变量: **下一次重连开始之前, 上一个 client 必须被显式释放**.
# ---------------------------------------------------------------------------


class _FakeWSClient:
    """Minimal SDK-shaped fake.

    ``_run_forever`` 调用面: ``connect_async`` / ``is_connected`` /
    ``disconnect``. 为复现"连接断开 → 应触发 disconnect"这条路径, 我们
    在 ``connect_async`` 里把 ``is_connected`` 立刻翻 False; 这样
    ``_run_forever`` 的内层 while 一进入就退出, 走到清理分支.
    """

    def __init__(self, label: str) -> None:
        self.label = label
        self.is_connected = False
        self.connect_calls = 0
        self.disconnect_calls = 0

    async def connect_async(self) -> None:
        self.connect_calls += 1
        # 默认: 连上即断 — 模拟"对端立即关闭 1006"的场景.
        self.is_connected = False

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.is_connected = False


@pytest.mark.asyncio
async def test_run_forever_disconnects_old_client_before_creating_new_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    adapter = WechatWorkBotAdapter(bot_id="b", bot_secret="s")
    # 缩到 0 让单测秒过, 同时锁定 single-flight 行为而不是退避时长.
    monkeypatch.setattr(adapter, "_RECONNECT_INITIAL_DELAY", 0.0, raising=False)
    monkeypatch.setattr(adapter, "_RECONNECT_MAX_DELAY", 0.0, raising=False)

    created: list[_FakeWSClient] = []

    def _make() -> _FakeWSClient:
        client = _FakeWSClient(label=f"c{len(created)}")
        created.append(client)
        return client

    monkeypatch.setattr(adapter, "_create_client", _make)

    task = asyncio.create_task(adapter._run_forever())
    # 让 loop 至少跑过 3 个连接生命周期.
    for _ in range(20):
        await asyncio.sleep(0)
        if len(created) >= 3:
            break
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert len(created) >= 3, (
        f"expected the loop to recreate clients on each disconnect; got {len(created)}"
    )
    # 不变量: 第 N 个 client 在第 N+1 个被创建之前必须 disconnect 过.
    for older in created[:-1]:
        assert older.disconnect_calls >= 1, (
            f"client {older.label} was replaced without disconnect — would race "
            "the new client for the same bot_id and trigger 'kicked by new connection'"
        )


@pytest.mark.asyncio
async def test_run_forever_disposes_client_on_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lifespan shutdown 路径: 取消任务时也必须 disconnect, 否则下次
    启动会跟服务端残留会话抢登入."""
    import asyncio

    adapter = WechatWorkBotAdapter(bot_id="b", bot_secret="s")
    monkeypatch.setattr(adapter, "_RECONNECT_INITIAL_DELAY", 0.0, raising=False)
    monkeypatch.setattr(adapter, "_RECONNECT_MAX_DELAY", 0.0, raising=False)

    created: list[_FakeWSClient] = []

    class _StickyClient(_FakeWSClient):
        async def connect_async(self) -> None:
            self.connect_calls += 1
            self.is_connected = True
            await asyncio.sleep(3600)

    def _make() -> _StickyClient:
        client = _StickyClient(label=f"c{len(created)}")
        created.append(client)
        return client

    monkeypatch.setattr(adapter, "_create_client", _make)

    task = asyncio.create_task(adapter._run_forever())
    for _ in range(20):
        await asyncio.sleep(0)
        if created and created[-1].connect_calls >= 1:
            break
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert created, "loop must have created at least one client before cancel"
    assert created[-1].disconnect_calls >= 1, (
        "task cancel path must dispose current client; otherwise WeCom keeps "
        "the old session and the next process boot collides with it"
    )
