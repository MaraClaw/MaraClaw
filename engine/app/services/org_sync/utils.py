"""Shared organization sync utility helpers."""

import importlib
from datetime import UTC, datetime
from types import ModuleType


def _load_optional_module(name: str) -> ModuleType | None:
    try:
        return importlib.import_module(name)
    except ImportError:  # pragma: no cover - lightweight fallback for minimal test envs
        return None


_anyascii_module = _load_optional_module("anyascii")
_pypinyin_module = _load_optional_module("pypinyin")


def _anyascii(value: str) -> str:
    if _anyascii_module is None:
        return value
    converter = getattr(_anyascii_module, "anyascii", None)
    if not callable(converter):
        return value
    converted = converter(value)
    return converted if isinstance(converted, str) else value


class _FallbackStyle:
    FIRST_LETTER = "first_letter"


Style = getattr(_pypinyin_module, "Style", _FallbackStyle) if _pypinyin_module else _FallbackStyle


def lazy_pinyin(value: str, errors: str = "default") -> list[str]:
    if _pypinyin_module is not None:
        converter = getattr(_pypinyin_module, "lazy_pinyin", None)
        if callable(converter):
            converted = converter(value, errors=errors)
            if isinstance(converted, list):
                return [str(item) for item in converted]

    ascii_value = _anyascii(value)
    return list(ascii_value) if ascii_value else list(value)


def pinyin(value: str, style: str | None = None) -> list[list[str]]:
    if _pypinyin_module is not None:
        converter = getattr(_pypinyin_module, "pinyin", None)
        if callable(converter):
            converted = converter(value, style=style)
            if isinstance(converted, list):
                return [[str(part) for part in item] for item in converted if isinstance(item, list)]

    ascii_value = _anyascii(value) or value
    if style == Style.FIRST_LETTER:
        return [[ch.lower()] for ch in ascii_value if ch.strip()]
    return [[ascii_value]]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _normalize_contact(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None
