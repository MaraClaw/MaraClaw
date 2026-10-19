"""Authenticate Feishu HTTP callbacks without affecting SDK websocket events."""

import base64
import hashlib
import hmac
from collections.abc import Mapping

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

from app.core.json_types import JsonObject, is_json_object, json_loads_value, json_object_from
from app.records.channel_config import ChannelConfigRecord


class FeishuAuthenticationError(ValueError):
    """The callback cannot be authenticated with the channel credentials."""


def authenticate_feishu_callback(
    config: ChannelConfigRecord, raw_body: bytes, headers: Mapping[str, str]
) -> JsonObject:
    token = config.verification_token or ""
    encrypt_key = config.encrypt_key or ""
    if not token:
        raise FeishuAuthenticationError("Missing verification token")
    try:
        body = json_loads_value(raw_body)
        if not is_json_object(body):
            raise FeishuAuthenticationError("Invalid callback body")
        if encrypt_key:
            encrypted = body.get("encrypt")
            if not isinstance(encrypted, str):
                raise FeishuAuthenticationError("Encrypted callback required")
            ciphertext = base64.b64decode(encrypted, validate=True)
            if len(ciphertext) < 32 or len(ciphertext) % AES.block_size:
                raise FeishuAuthenticationError("Invalid encrypted callback")
            key = hashlib.sha256(encrypt_key.encode()).digest()
            plaintext = AES.new(key, AES.MODE_CBC, ciphertext[:16]).decrypt(ciphertext[16:])
            body = json_loads_value(unpad(plaintext, AES.block_size))
            if not is_json_object(body):
                raise FeishuAuthenticationError("Invalid decrypted callback")
        elif "encrypt" in body:
            raise FeishuAuthenticationError("Missing encryption key")

        supplied_token = (
            json_object_from(body.get("header")).get("token")
            if body.get("schema") == "2.0" and body.get("type") != "url_verification"
            else body.get("token")
        )
        if not isinstance(supplied_token, str) or not hmac.compare_digest(token.encode(), supplied_token.encode()):
            raise FeishuAuthenticationError("Invalid verification token")

        if body.get("type") == "url_verification":
            if not isinstance(body.get("challenge"), str):
                raise FeishuAuthenticationError("Invalid URL verification challenge")
        else:
            timestamp = headers.get("x-lark-request-timestamp", "")
            nonce = headers.get("x-lark-request-nonce", "")
            signature = headers.get("x-lark-signature", "")
            expected = hashlib.sha256((timestamp + nonce + encrypt_key).encode() + raw_body).hexdigest()
            if not timestamp or not nonce or not hmac.compare_digest(expected.encode(), signature.encode()):
                raise FeishuAuthenticationError("Invalid callback signature")
        return body
    except (ValueError, UnicodeError) as error:
        raise FeishuAuthenticationError("Invalid Feishu callback") from error
