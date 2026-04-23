"""WeCom (企业微信) channel adapter for Pulse Agent Runtime.

Handles:
  - Incoming message decryption and parsing
  - Outgoing message via WeCom API (text reply)
  - URL verification callback (echostr)
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from .base import BaseChannelAdapter, IncomingMessage, OutgoingMessage
from .wechat_work_crypto import WechatWorkCrypto, parse_text_message

logger = logging.getLogger(__name__)


class WechatWorkChannelAdapter(BaseChannelAdapter):
    name = "wechat-work"

    def __init__(
        self,
        *,
        corp_id: str = "",
        agent_id: str = "",
        secret: str = "",
        token: str = "",
        encoding_aes_key: str = "",
    ) -> None:
        super().__init__()
        self.corp_id = corp_id
        self.agent_id = agent_id
        self.secret = secret
        self.token = token
        self.encoding_aes_key = encoding_aes_key

        self._crypto: WechatWorkCrypto | None = None
        self._access_token: str = ""
        self._token_expires_at: float = 0.0

        if self.corp_id and self.token and self.encoding_aes_key:
            self._crypto = WechatWorkCrypto(
                token=self.token,
                encoding_aes_key=self.encoding_aes_key,
                corp_id=self.corp_id,
            )

    @property
    def configured(self) -> bool:
        return bool(self.corp_id and self.secret and self.token and self.encoding_aes_key)

    # ── URL verification (GET callback) ──

    def verify_url(self, msg_signature: str, timestamp: str, nonce: str, echostr: str) -> str | None:
        if not self._crypto:
            return None
        if not self._crypto.verify_signature(msg_signature, timestamp, nonce, echostr):
            return None
        decrypted, _ = self._crypto.decrypt(echostr)
        return decrypted

    # ── Incoming message ──

    def parse_incoming(self, payload: Any) -> IncomingMessage | None:
        if not isinstance(payload, dict):
            return None

        xml_body = payload.get("xml_body", "")
        msg_signature = payload.get("msg_signature", "")
        timestamp = payload.get("timestamp", "")
        nonce = payload.get("nonce", "")

        if not all([xml_body, msg_signature, timestamp, nonce]):
            return None
        if not self._crypto:
            return None

        try:
            decrypted_xml = self._crypto.decrypt_message(
                post_data=xml_body,
                msg_signature=msg_signature,
                timestamp=timestamp,
                nonce=nonce,
            )
        except Exception as e:
            logger.warning("wechat-work decrypt failed: %s", e)
            return None

        msg = parse_text_message(decrypted_xml)
        if msg["msg_type"] != "text" or not msg["content"]:
            logger.debug("wechat-work ignoring non-text message: %s", msg["msg_type"])
            return None

        return IncomingMessage(
            channel=self.name,
            user_id=msg["from_user"],
            text=msg["content"],
            metadata={
                "msg_id": msg["msg_id"],
                "agent_id": msg["agent_id"],
                "to_user": msg["to_user"],
                "create_time": msg["create_time"],
            },
            received_at=datetime.now(timezone.utc),
        )

    # ── Access token management ──

    def _refresh_access_token(self) -> str:
        if self._access_token and time.time() < self._token_expires_at:
            return self._access_token

        url = (
            f"https://qyapi.weixin.qq.com/cgi-bin/gettoken"
            f"?corpid={self.corp_id}&corpsecret={self.secret}"
        )
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            logger.error("wechat-work get access_token failed: %s", e)
            return ""

        if data.get("errcode", 0) != 0:
            logger.error("wechat-work access_token error: %s", data)
            return ""

        self._access_token = data["access_token"]
        self._token_expires_at = time.time() + data.get("expires_in", 7200) - 300
        return self._access_token

    # ── Outgoing message ──

    def send(self, message: OutgoingMessage) -> None:
        token = self._refresh_access_token()
        if not token:
            logger.error("wechat-work cannot send: no access_token")
            return

        url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}"
        payload = {
            "touser": message.target_id,
            "msgtype": "text",
            "agentid": int(self.agent_id) if self.agent_id.isdigit() else self.agent_id,
            "text": {"content": message.text},
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                if result.get("errcode", 0) != 0:
                    logger.error("wechat-work send failed: %s", result)
        except urllib.error.URLError as e:
            logger.error("wechat-work send error: %s", e)
