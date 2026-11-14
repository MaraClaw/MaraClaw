"""Shared request type for a contact phone number: international input, canonical E.164 on the wire and in storage."""

from typing import Annotated, Final

import phonenumbers
from pydantic import BeforeValidator, StringConstraints
from pydantic_core import PydanticCustomError

E164_PATTERN: Final = r"^\+[1-9][0-9]{6,14}$"
E164_MAX_LENGTH: Final = 16


def _to_e164(value: object) -> object:
    """Parse an international number (leading ``+``; spaces and dashes allowed) into compact E.164.

    Non-strings pass through so the string schema reports the type error.
    """
    if not isinstance(value, str):
        return value
    try:
        number = phonenumbers.parse(value, None)
    except phonenumbers.NumberParseException:
        raise PydanticCustomError("phone_number", "Enter a valid phone number") from None
    if not phonenumbers.is_valid_number(number):
        raise PydanticCustomError("phone_number", "Enter a valid phone number")
    return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)


PhoneNumber = Annotated[
    str, StringConstraints(max_length=E164_MAX_LENGTH, pattern=E164_PATTERN), BeforeValidator(_to_e164)
]
