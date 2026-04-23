"""WeCom (企业微信) message encryption/decryption.

Implements the official WXBizMsgCrypt protocol:
  - AES-256-CBC with PKCS#7 padding
  - XML envelope parsing
  - Signature verification

Reference: https://developer.work.weixin.qq.com/document/path/90968
"""

from __future__ import annotations

import base64
import hashlib
import os
import struct
import time
import xml.etree.ElementTree as ET
from typing import Any

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7


class WechatWorkCrypto:
    """Encrypt / decrypt messages for WeCom callback API."""

    BLOCK_SIZE = 32  # AES block size in bytes

    def __init__(self, token: str, encoding_aes_key: str, corp_id: str) -> None:
        self.token = token
        self.corp_id = corp_id
        self.aes_key = base64.b64decode(encoding_aes_key + "=")
        self.iv = self.aes_key[:16]

    # ── signature ──

    @staticmethod
    def _sha1(*args: str) -> str:
        return hashlib.sha1("".join(sorted(args)).encode("utf-8")).hexdigest()

    def verify_signature(self, signature: str, timestamp: str, nonce: str, echostr: str) -> bool:
        computed = self._sha1(self.token, timestamp, nonce, echostr)
        return computed == signature

    # ── decrypt ──

    def _pkcs7_unpad(self, data: bytes) -> bytes:
        pad_len = data[-1]
        if pad_len < 1 or pad_len > self.BLOCK_SIZE:
            return data
        return data[:-pad_len]

    def decrypt(self, encrypted: str) -> tuple[str, str]:
        """Decrypt an encrypted message string.

        Returns (plaintext_xml, from_corp_id).
        """
        cipher = Cipher(algorithms.AES(self.aes_key), modes.CBC(self.iv))
        decryptor = cipher.decryptor()
        raw = decryptor.update(base64.b64decode(encrypted)) + decryptor.finalize()
        raw = self._pkcs7_unpad(raw)

        # layout: 16-byte random + 4-byte msg_len (network order) + msg + corp_id
        msg_len = struct.unpack("!I", raw[16:20])[0]
        msg = raw[20 : 20 + msg_len].decode("utf-8")
        from_corp_id = raw[20 + msg_len :].decode("utf-8")
        return msg, from_corp_id

    def decrypt_message(
        self,
        post_data: str,
        msg_signature: str,
        timestamp: str,
        nonce: str,
    ) -> str:
        """Verify signature and decrypt the XML body from WeCom callback.

        Returns the decrypted XML string.
        """
        root = ET.fromstring(post_data)
        encrypt_node = root.find("Encrypt")
        if encrypt_node is None or not encrypt_node.text:
            raise ValueError("missing <Encrypt> in XML body")
        encrypt_text = encrypt_node.text

        if not self.verify_signature(msg_signature, timestamp, nonce, encrypt_text):
            raise ValueError("signature verification failed")

        xml_content, from_corp = self.decrypt(encrypt_text)
        if from_corp != self.corp_id:
            raise ValueError(f"corp_id mismatch: expected {self.corp_id}, got {from_corp}")
        return xml_content

    # ── encrypt ──

    def _pkcs7_pad(self, data: bytes) -> bytes:
        padder = PKCS7(self.BLOCK_SIZE * 8).padder()
        return padder.update(data) + padder.finalize()

    def encrypt(self, plaintext: str) -> str:
        """Encrypt a plaintext reply XML."""
        random_bytes = os.urandom(16)
        msg_bytes = plaintext.encode("utf-8")
        corp_bytes = self.corp_id.encode("utf-8")
        body = random_bytes + struct.pack("!I", len(msg_bytes)) + msg_bytes + corp_bytes
        padded = self._pkcs7_pad(body)

        cipher = Cipher(algorithms.AES(self.aes_key), modes.CBC(self.iv))
        encryptor = cipher.encryptor()
        encrypted = encryptor.update(padded) + encryptor.finalize()
        return base64.b64encode(encrypted).decode("utf-8")

    def build_reply_xml(self, encrypt_text: str, nonce: str) -> str:
        """Build the encrypted reply XML envelope."""
        timestamp = str(int(time.time()))
        signature = self._sha1(self.token, timestamp, nonce, encrypt_text)
        return (
            "<xml>"
            f"<Encrypt><![CDATA[{encrypt_text}]]></Encrypt>"
            f"<MsgSignature><![CDATA[{signature}]]></MsgSignature>"
            f"<TimeStamp>{timestamp}</TimeStamp>"
            f"<Nonce><![CDATA[{nonce}]]></Nonce>"
            "</xml>"
        )


def parse_text_message(xml_content: str) -> dict[str, Any]:
    """Parse a decrypted WeCom text message XML into a dict."""
    root = ET.fromstring(xml_content)
    return {
        "to_user": root.findtext("ToUserName", ""),
        "from_user": root.findtext("FromUserName", ""),
        "create_time": root.findtext("CreateTime", ""),
        "msg_type": root.findtext("MsgType", ""),
        "content": root.findtext("Content", ""),
        "msg_id": root.findtext("MsgId", ""),
        "agent_id": root.findtext("AgentID", ""),
    }
