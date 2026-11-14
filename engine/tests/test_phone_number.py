"""The shared contact phone type: international input in, compact E.164 out, schema carries the constraints."""

import pytest
from pydantic import TypeAdapter, ValidationError

from app.schemas.phone import PhoneNumber

PHONE = TypeAdapter(PhoneNumber)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+84912345678", "+84912345678"),
        (" +84 912 345 678 ", "+84912345678"),
        ("+84-912-345-678", "+84912345678"),
        ("+1 (650) 253-0000", "+16502530000"),
    ],
)
def test_valid_international_numbers_are_normalized_to_e164(raw: str, expected: str):
    assert PHONE.validate_python(raw) == expected


@pytest.mark.parametrize(
    "raw", ["+84123", "0912345678", "84912345678", "+0912345678", "+", "", "   ", "phone", None, 84912345678]
)
def test_invalid_numbers_are_rejected(raw: object):
    with pytest.raises(ValidationError):
        _ = PHONE.validate_python(raw)


def test_an_invalid_number_reports_the_enter_a_valid_phone_number_message():
    with pytest.raises(ValidationError) as exc:
        _ = PHONE.validate_python("+84123")

    assert [error["msg"] for error in exc.value.errors()] == ["Enter a valid phone number"]


def test_the_json_schema_carries_the_length_and_pattern_constraints():
    assert PHONE.json_schema() == {"type": "string", "maxLength": 16, "pattern": r"^\+[1-9][0-9]{6,14}$"}
