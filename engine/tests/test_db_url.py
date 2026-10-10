"""Tests for DATABASE_URL normalization."""

import pytest
from psycopg.conninfo import conninfo_to_dict

from app.db.url import normalize_psycopg_conninfo


@pytest.mark.parametrize("scheme", ["postgresql", "postgres"])
@pytest.mark.parametrize("sslmode", ["disable", "allow", "prefer", "require", "verify-ca", "verify-full"])
def test_preserves_native_url_when_sslmode_is_explicit(scheme: str, sslmode: str) -> None:
    # Given
    url = f"{scheme}://user:pass@localhost:5432/db?sslmode={sslmode}"

    # When
    result = normalize_psycopg_conninfo(url)

    # Then
    assert result == url


@pytest.mark.parametrize("scheme", ["postgresql", "postgres"])
def test_preserves_credentials_and_query_semantics_when_url_is_encoded(scheme: str) -> None:
    # Given
    url = (
        f"{scheme}://user%40team:p%40ss%3Aword%2F%2B@localhost:5432/db%20name"
        "?sslmode=verify-full&application_name=worker%20one&options=-c%20search_path%3Dpublic&sslrootcert="
    )

    # When
    result = normalize_psycopg_conninfo(url)

    # Then
    assert conninfo_to_dict(result) == {
        "user": "user@team",
        "password": "p@ss:word/+",
        "host": "localhost",
        "port": "5432",
        "dbname": "db name",
        "sslmode": "verify-full",
        "application_name": "worker one",
        "options": "-c search_path=public",
        "sslrootcert": "",
    }


def test_preserves_duplicate_query_parameters_when_url_is_native() -> None:
    # Given
    url = "postgresql://u:p@h:5432/db?application_name=first&application_name=second&sslmode=require"

    # When
    result = normalize_psycopg_conninfo(url)

    # Then
    assert result == url


def test_accepts_libpq_keyword_conninfo_when_values_are_quoted() -> None:
    # Given
    raw = (
        "host=localhost dbname=app user=u password='p with spaces' sslmode=verify-full options='-c search_path=public'"
    )

    # When
    result = normalize_psycopg_conninfo(raw)

    # Then
    assert result == raw


@pytest.mark.parametrize("scheme", ["postgresql", "postgres"])
@pytest.mark.parametrize("driver", ["asyncpg", "psycopg", "psycopg2"])
def test_rejects_driver_scheme_when_url_is_sqlalchemy_style(scheme: str, driver: str) -> None:
    # Given
    url = f"{scheme}+{driver}://u:p@localhost:5432/db?sslmode=require"

    # When / Then
    with pytest.raises(ValueError, match="Unsupported"):
        normalize_psycopg_conninfo(url)


@pytest.mark.parametrize("scheme", ["postgresql", "postgres"])
@pytest.mark.parametrize(
    "query", ["ssl=require", "ssl=", "SSL=require", "%73sl=require", "sslmode=verify-full&ssl=require"]
)
def test_rejects_ssl_alias_when_url_query_uses_retired_option(scheme: str, query: str) -> None:
    # Given
    url = f"{scheme}://u:p@localhost:5432/db?{query}"

    # When / Then
    with pytest.raises(ValueError, match="sslmode"):
        normalize_psycopg_conninfo(url)


@pytest.mark.parametrize("url", ["", "  ", "mysql://localhost/db"])
def test_rejects_input_when_empty_or_unsupported(url: str) -> None:
    # Given: an empty value or unsupported URL scheme.
    # When / Then
    with pytest.raises(ValueError):
        normalize_psycopg_conninfo(url)
