"""DATABASE_URL normalization for psycopg3."""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

_SQLALCHEMY_DRIVER_PREFIXES: tuple[str, ...] = (
    "postgresql+asyncpg://",
    "postgresql+psycopg://",
    "postgresql+psycopg2://",
    "postgres+asyncpg://",
    "postgres+psycopg://",
)


def normalize_psycopg_conninfo(url: str) -> str:
    """Return a conninfo string accepted by psycopg3.

    Accepts SQLAlchemy-style driver URLs and common SSL query aliases used in
    this project (``ssl=require`` → ``sslmode=require``).
    """
    raw = (url or "").strip()
    if not raw:
        raise ValueError("DATABASE_URL is empty")

    for prefix in _SQLALCHEMY_DRIVER_PREFIXES:
        if raw.startswith(prefix):
            scheme = "postgresql://" if prefix.startswith("postgresql") else "postgres://"
            raw = scheme + raw[len(prefix) :]
            break

    parsed = urlparse(raw)
    if parsed.scheme not in {"postgresql", "postgres"}:
        # Allow full libpq keyword conninfo (no URL scheme).
        if "://" not in raw and "=" in raw:
            return raw
        raise ValueError(f"Unsupported DATABASE_URL scheme: {parsed.scheme!r}")

    query_items = list(parse_qsl(parsed.query, keep_blank_values=True))
    has_sslmode = any(k.lower() == "sslmode" for k, _ in query_items)
    normalized: list[tuple[str, str]] = []
    for key, value in query_items:
        lower = key.lower()
        if lower == "ssl":
            # SQLAlchemy/asyncpg often used ssl=require.
            if not has_sslmode:
                normalized.append(("sslmode", value or "require"))
            continue
        normalized.append((key, value))

    # Preserve explicit empty query vs normalized query.
    new_query = urlencode(normalized, doseq=True)
    return urlunparse(parsed._replace(query=new_query))
