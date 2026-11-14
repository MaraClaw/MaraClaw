"""Signed price quotes of organization admission requests.

The public quote endpoint prices a request on the server and hands the visitor this token; the submit endpoint trusts
nothing but what verifies inside it (the amount, the rate, the recipient, the transfer reference and the expiry).

Format: ``base64url(payload).base64url(HMAC-SHA256(payload text))`` over the JSON of ``OrganizationAdmissionQuoteTerms``.
The key is derived from ``JWT_SECRET_KEY`` with a fixed purpose label, so a quote token never verifies as an access
token (different key, and an access token has three segments) and an access token never verifies as a quote token.
"""

import base64
import binascii
import hashlib
import hmac
from datetime import datetime
from typing import Final

from pydantic import ValidationError

from app.config import get_settings
from app.schemas.organization_admission import OrganizationAdmissionQuoteTerms

KEY_PURPOSE: Final = b"organization-admission-quote"
MAX_TOKEN_LENGTH: Final = 4096


class QuoteTokenInvalidError(ValueError):
    """The token is malformed, was not signed by this engine for quotes, or its content is not a quote."""


class QuoteTokenExpiredError(ValueError):
    """The token is genuine but its quote is past ``expires_at``."""


def _key() -> bytes:
    return hmac.new(get_settings().JWT_SECRET_KEY.encode(), KEY_PURPOSE, hashlib.sha256).digest()


def _sign(payload_text: str) -> bytes:
    return hmac.new(_key(), payload_text.encode("ascii"), hashlib.sha256).digest()


def _encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _decode(text: str) -> bytes:
    try:
        data = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except binascii.Error, ValueError:
        raise QuoteTokenInvalidError from None
    if _encode(data) != text:
        raise QuoteTokenInvalidError
    return data


def mint_quote_token(terms: OrganizationAdmissionQuoteTerms) -> str:
    payload_text = _encode(terms.model_dump_json().encode())
    return f"{payload_text}.{_encode(_sign(payload_text))}"


def verify_quote_token(token: str, *, now: datetime) -> OrganizationAdmissionQuoteTerms:
    """The quote the token binds, or ``QuoteTokenInvalidError`` / ``QuoteTokenExpiredError`` (``now`` is injected)."""
    payload_text, separator, signature_text = token.partition(".")
    if not separator or not token.isascii() or len(token) > MAX_TOKEN_LENGTH or "." in signature_text:
        raise QuoteTokenInvalidError
    if not hmac.compare_digest(_decode(signature_text), _sign(payload_text)):
        raise QuoteTokenInvalidError
    try:
        terms = OrganizationAdmissionQuoteTerms.model_validate_json(_decode(payload_text))
    except ValidationError:
        raise QuoteTokenInvalidError from None
    if now >= terms.expires_at:
        raise QuoteTokenExpiredError
    return terms
