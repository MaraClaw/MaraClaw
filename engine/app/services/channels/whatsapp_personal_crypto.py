"""Authenticated, context-bound encryption for linked-device authentication."""

import hashlib
import json
import secrets
from dataclasses import dataclass
from uuid import UUID

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


@dataclass(frozen=True, slots=True)
class AuthKey:
    config_id: UUID
    auth_epoch: UUID
    category: str
    key_id: str

    def aad(self) -> bytes:
        return json.dumps(
            [str(self.config_id), str(self.auth_epoch), self.category, self.key_id, 1], separators=(",", ":")
        ).encode()


class AuthCipher:
    def __init__(self, secret: str) -> None:
        self._cipher: AESGCM = AESGCM(hashlib.sha256(b"maraclaw:whatsapp:auth:v1\0" + secret.encode()).digest())

    def seal(self, key: AuthKey, plaintext: str) -> bytes:
        nonce = secrets.token_bytes(12)
        return nonce + self._cipher.encrypt(nonce, plaintext.encode(), key.aad())

    def open(self, key: AuthKey, ciphertext: bytes) -> str:
        return self._cipher.decrypt(ciphertext[:12], ciphertext[12:], key.aad()).decode()
