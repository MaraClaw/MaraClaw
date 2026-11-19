"""ILIKE candidate patterns for resolving Clawbook @mention tokens in SQL.

Mentions match by Python's ``name.lower() in tokens``. SQL only narrows the roster with ``name ILIKE ANY(patterns)``,
so the patterns must return a superset of the exact matches: PostgreSQL lowercases by the database's character type
(a ``C`` collation folds ASCII only, Turkish locales do not fold ``I`` to ``i``) while Python lowercases by Unicode.
A character therefore stays literal only when no folding can disagree about it, and every other one becomes ``_``.
Lowercasings that change a string's length become ``%``: U+0130 lowers to ``i`` + U+0307 (ICU) or ``i`` (libc), the
Lithuanian tailoring adds U+0307 after ``i``, ``j`` and ``į`` with an accent above and expands ``ì``, ``í`` and ``ĩ``,
and the Turkish tailoring drops a U+0307 that follows ``I``. Every other lowering, in any collation, keeps a character
one character long. No SQL lives here.
"""

from collections.abc import Iterable

_LIKE_ESCAPED = frozenset("%_\\")
_ANY_RUN = frozenset("ijįìíĩ\u0307")
# The Kelvin sign lowercases to k in Python but not in a C collation.
_ASCII_WILDCARDS = frozenset("k")


def _char_pattern(char: str) -> str:
    if char in _LIKE_ESCAPED:
        return "\\" + char
    if char in _ANY_RUN:
        return "%"
    if char.isascii():
        return "_" if char in _ASCII_WILDCARDS else char
    return char if char.lower() == char and char.upper() == char else "_"


def _token_pattern(token: str) -> str:
    parts: list[str] = []
    for char in token:
        part = _char_pattern(char)
        if part != "%" or not parts or parts[-1] != "%":
            parts.append(part)
    return "".join(parts)


def like_patterns(tokens: Iterable[str]) -> list[str]:
    """One sorted ILIKE pattern per distinct token, which must already be lowercased with ``str.lower``."""
    return sorted({_token_pattern(token) for token in tokens})
