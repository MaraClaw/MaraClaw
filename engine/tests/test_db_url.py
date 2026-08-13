"""Tests for DATABASE_URL normalization."""

import pytest

from app.db.url import normalize_psycopg_conninfo


def test_strips_asyncpg_driver_prefix() -> None:
    # Given
    url = "postgresql+asyncpg://user:pass@localhost:5432/db"

    # When
    result = normalize_psycopg_conninfo(url)

    # Then
    assert result == "postgresql://user:pass@localhost:5432/db"


def test_strips_psycopg_driver_prefix() -> None:
    assert normalize_psycopg_conninfo("postgresql+psycopg://u:p@h:1/db") == "postgresql://u:p@h:1/db"


def test_maps_ssl_require_to_sslmode() -> None:
    # Given
    url = "postgresql+asyncpg://u:p@h:5432/db?ssl=require"

    # When
    result = normalize_psycopg_conninfo(url)

    # Then
    assert result.startswith("postgresql://u:p@h:5432/db?")
    assert "sslmode=require" in result
    assert "ssl=require" not in result


def test_preserves_existing_sslmode() -> None:
    url = "postgresql://u:p@h:5432/db?sslmode=verify-full"
    assert normalize_psycopg_conninfo(url) == url


def test_accepts_libpq_keyword_conninfo() -> None:
    raw = "host=localhost dbname=app user=u password=p"
    assert normalize_psycopg_conninfo(raw) == raw


def test_rejects_empty_and_unknown_scheme() -> None:
    with pytest.raises(ValueError, match="empty"):
        normalize_psycopg_conninfo("  ")
    with pytest.raises(ValueError, match="Unsupported"):
        normalize_psycopg_conninfo("mysql://localhost/db")
