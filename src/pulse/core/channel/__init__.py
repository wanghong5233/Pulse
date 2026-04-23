"""Channel ingress abstractions and adapters."""

from .base import BaseChannelAdapter, IncomingMessage, OutgoingMessage
from .cli import CliChannelAdapter
from .feishu import FeishuChannelAdapter, verify_feishu_signature
from .wechat_work import WechatWorkChannelAdapter
from .wechat_work_bot import WechatWorkBotAdapter

__all__ = [
    "BaseChannelAdapter",
    "IncomingMessage",
    "OutgoingMessage",
    "CliChannelAdapter",
    "FeishuChannelAdapter",
    "verify_feishu_signature",
    "WechatWorkChannelAdapter",
    "WechatWorkBotAdapter",
]
