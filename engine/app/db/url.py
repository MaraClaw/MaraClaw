"""DATABASE_URL normalization for psycopg3."""

from __future__ import annotations

from urllib.parse import parse_qsl, urlparse


def normalize_psycopg_conninfo(url: str) -> str:
    """Return a conninfo string accepted by psycopg3.

    Accepts native PostgreSQL URLs and libpq keyword conninfo only.
    Driver-qualified URL schemes and the retired ``ssl`` query alias are rejected.
    """
    raw = url.strip()
    if not raw:
        raise ValueError("DATABASE_URL is empty")

    parsed = urlparse(raw)
    if parsed.scheme not in {"postgresql", "postgres"}:
        # Allow full libpq keyword conninfo (no URL scheme).
        if "://" not in raw and "=" in raw:
            return raw
        raise ValueError(f"Unsupported DATABASE_URL scheme: {parsed.scheme!r}")

    if any(key.lower() == "ssl" for key, _ in parse_qsl(parsed.query, keep_blank_values=True)):
        message = "Unsupported DATABASE_URL query option 'ssl'; use native 'sslmode' instead"
        raise ValueError(message)

    return raw
