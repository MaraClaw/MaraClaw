"""Record-bound authenticated envelopes for automation credentials only."""

import base64
import binascii
import hashlib
import json
import os
from dataclasses import dataclass
from typing import Final, Self, final, override

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import get_settings

_PREFIX: Final = "mcred:v1:"


class StoredCredentialError(ValueError):
    @override
    def __str__(self) -> str:
        return "Stored credential authentication failed; replace or recapture the credential."


@dataclass(frozen=True, slots=True)
class CredentialContext:
    purpose: str
    owner: str
    record: str
    field: tuple[str, ...]
    channel: str = ""

    def aad(self) -> bytes:
        return json.dumps(
            ["maraclaw.stored-credential", 1, self.purpose, self.owner, self.record, self.channel, self.field],
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")


@final
class PreservedCredential(str):
    """A display marker that can preserve rejected bytes only in their original storage context."""

    __slots__ = ("context", "stored")

    def __new__(cls, stored: str, context: CredentialContext) -> Self:
        value = super().__new__(cls, "***")
        value.stored = stored
        value.context = context
        return value

    stored: str
    context: CredentialContext


def seal_credential(value: str, context: CredentialContext) -> str:
    if isinstance(value, PreservedCredential):
        if value.context != context:
            raise StoredCredentialError
        return value.stored
    key = hashlib.sha256(get_settings().SECRET_KEY.encode()).digest()
    nonce = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(nonce, value.encode("utf-8"), context.aad())
    return _PREFIX + base64.b64encode(nonce + ciphertext).decode("ascii")


def open_credential(value: str, context: CredentialContext) -> str:
    """Authenticate against caller-owned context, never context from storage."""
    if not value.startswith(_PREFIX):
        raise StoredCredentialError
    try:
        packed = base64.b64decode(value[len(_PREFIX) :], validate=True)
        if len(packed) < 28:
            raise StoredCredentialError
        key = hashlib.sha256(get_settings().SECRET_KEY.encode()).digest()
        return AESGCM(key).decrypt(packed[:12], packed[12:], context.aad()).decode("utf-8")
    except binascii.Error, InvalidTag, UnicodeError, ValueError:
        raise StoredCredentialError from None
