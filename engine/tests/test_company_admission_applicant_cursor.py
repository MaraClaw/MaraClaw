from base64 import urlsafe_b64encode
from datetime import timedelta, timezone
from json import dumps
from uuid import UUID

import pytest
from fastapi import HTTPException
from pydantic import JsonValue

from app.records.company_admission import AdmissionStatus
from app.services.company_admission_cursor import ApplicantCursor, ReviewCursor
from applicant_admission_fixtures import STAMP, applicant_cursor


@pytest.mark.parametrize("status", [None, *AdmissionStatus])
def test_applicant_cursor_preserves_canonical_filter_and_microseconds(status: AdmissionStatus | None):
    # Given a typed owner-bound position with full PostgreSQL timestamp precision.
    position = ApplicantCursor(
        version=1, kind="applicant", user_id=UUID(int=1000), status=status, created_at=STAMP, id=UUID(int=2)
    )

    # When it travels through the opaque wire representation.
    result = ApplicantCursor.decode(position.encode())

    # Then no field or microseconds are lost, including the explicit all-filter null.
    assert result == position
    assert result.created_at.microsecond == 123456
    assert result.model_dump(mode="json")["status"] == status


def test_applicant_cursor_preserves_an_aware_non_utc_timestamp():
    # Given a cursor timestamp with an explicit non-UTC offset.
    stamp = STAMP.astimezone(timezone(timedelta(hours=5, minutes=30)))

    # When decoding its JSON timestamp.
    result = ApplicantCursor.decode(applicant_cursor({"created_at": stamp.isoformat()}))

    # Then the same instant and subsecond seek position survive.
    assert result.created_at == STAMP
    assert result.created_at.utcoffset() == timedelta(hours=5, minutes=30)


@pytest.mark.parametrize(
    "cursor",
    ["", "%%%", "é", "a", "_w", urlsafe_b64encode(b"not json").decode(), urlsafe_b64encode(b"[]").decode()],
)
def test_applicant_cursor_rejects_malformed_wire_values_uniformly(cursor: str):
    # Given malformed base64, UTF-8, or JSON.
    # When it crosses the applicant cursor boundary.
    with pytest.raises(HTTPException) as error:
        ApplicantCursor.decode(cursor)

    # Then all malformed encodings have the same client-visible error.
    assert (error.value.status_code, error.value.detail) == (400, "Invalid cursor")


@pytest.mark.parametrize(
    "changes",
    [
        {"version": 2},
        {"version": True},
        {"version": 1.0},
        {"version": "1"},
        {"kind": "review"},
        {"kind": None},
        {"kind": 1},
        {"user_id": "invalid"},
        {"user_id": None},
        {"user_id": 1},
        {"status": "all"},
        {"status": "invalid"},
        {"status": 1},
        {"created_at": "2026-10-21T12:00:00"},
        {"created_at": 1792584000},
        {"created_at": None},
        {"id": "invalid"},
        {"id": 1},
        {"id": None},
        {"unexpected": "field"},
    ],
)
def test_applicant_cursor_rejects_invalid_types_versions_kinds_and_extras(changes: dict[str, JsonValue]):
    # Given an otherwise valid payload containing one invalid cursor field.
    cursor = applicant_cursor(changes)

    # When parsing strictly rather than coercing payload fields.
    with pytest.raises(HTTPException) as error:
        ApplicantCursor.decode(cursor)

    # Then the caller receives the uniform invalid-cursor error.
    assert (error.value.status_code, error.value.detail) == (400, "Invalid cursor")


@pytest.mark.parametrize("field", ["version", "kind", "user_id", "status", "created_at", "id"])
def test_applicant_cursor_requires_every_scope_and_position_field(field: str):
    # Given a decoded position missing a required field, including the nullable filter.
    valid = ApplicantCursor.decode(applicant_cursor()).model_dump(mode="json")
    del valid[field]
    cursor = urlsafe_b64encode(dumps(valid).encode()).decode().rstrip("=")

    # When the incomplete cursor is parsed.
    with pytest.raises(HTTPException) as error:
        ApplicantCursor.decode(cursor)

    # Then omitted status cannot silently mean the canonical all-filter.
    assert (error.value.status_code, error.value.detail) == (400, "Invalid cursor")


def test_applicant_and_review_cursor_types_are_not_interchangeable():
    # Given independently scoped applicant and reviewer positions.
    review = ReviewCursor(
        version=1, tenant_id=UUID(int=1000), status=AdmissionStatus.PENDING, created_at=STAMP, id=UUID(int=2)
    )

    # When either position is submitted to the other cursor parser.
    with pytest.raises(HTTPException) as applicant_error:
        ApplicantCursor.decode(review.encode())
    with pytest.raises(HTTPException) as review_error:
        ReviewCursor.decode(applicant_cursor())

    # Then coincidentally equal tenant/user UUIDs cannot cross cursor domains.
    assert (applicant_error.value.status_code, review_error.value.status_code) == (400, 400)
