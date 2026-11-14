"""Conceal the model and provider behind agent replies: prompt guidance plus deterministic redaction.

Only identifiers configured for the agent are redacted, as ``provider/model``, ``model`` and trailing-path forms.
Provider names, short or plain-word ids and everything else stay untouched; user messages are never passed through.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import final

from app.config import get_settings
from app.records.agent import AgentRecord
from app.services.llm.router import load_agent_model_bundle
from app.services.openclaw_hot_cache import get_cached_bundle

_MIN_ALIAS = 5
_MAX_ALIAS = 256
_BEFORE = r"(?<![A-Za-z0-9_.-])"
_AFTER = r"(?![A-Za-z0-9_-])(?!\.[A-Za-z0-9_])"
_UNDECIDED_TAIL = ("", ".")  # what follows a match while `_AFTER` still needs one more character


def _specific(alias: str) -> bool:
    """Long enough and shaped like an identifier (digit or separator), so ordinary words stay untouched."""
    return (
        _MIN_ALIAS <= len(alias) <= _MAX_ALIAS
        and any(char.isalpha() for char in alias)
        and any(char.isdigit() or char in "-_./:" for char in alias)
    )


def _aliases(identifier: str) -> set[str]:
    """``a/b/c:tag`` -> itself, ``b/c:tag``, ``c:tag`` and each without ``:tag``."""
    parts = identifier.strip().split("/")
    tails = {"/".join(parts[start:]) for start in range(len(parts))}
    tails |= {head for tail in tails if (head := tail.rpartition(":")[0])}
    return {tail for tail in tails if _specific(tail)}


def _brand() -> str:
    return get_settings().APP_NAME.strip()


@final
@dataclass(frozen=True, slots=True)
class ResponseGuard:
    """Compiled redaction for one agent's configured model identifiers."""

    pattern: re.Pattern[str] | None
    prefixes: frozenset[str]  # lowercase prefixes of every alias, to hold back a tail that may still become one
    longest: int
    replacement: str

    @classmethod
    def build(cls, identifiers: Iterable[str | None]) -> ResponseGuard:
        aliases = sorted(
            {alias for item in identifiers if item is not None for alias in _aliases(item)}, key=lambda a: (-len(a), a)
        )
        replacement = _brand() or "the assistant"
        if not aliases:
            return cls(None, frozenset(), 0, replacement)
        pattern = re.compile(
            _BEFORE + "(?:" + "|".join(map(re.escape, aliases)) + ")" + _AFTER, re.IGNORECASE | re.ASCII
        )
        prefixes = frozenset(alias.lower()[:size] for alias in aliases for size in range(1, len(alias) + 1))
        return cls(pattern, prefixes, len(aliases[0]), replacement)

    def redact(self, text: str) -> str:
        return text if self.pattern is None else self.pattern.sub(lambda _found: self.replacement, text)

    def held_length(self, text: str) -> int:
        """Length of the longest tail of ``text`` that could still grow into an alias."""
        tail = text[-self.longest :].lower() if self.longest else ""
        return next((size for size in range(len(tail), 0, -1) if tail[-size:] in self.prefixes), 0)

    def stream(self) -> StreamRedactor:
        return StreamRedactor(self)


@final
class StreamRedactor:
    """One turn's append/replace state; holds back only a tail shorter than the longest alias plus its boundary."""

    def __init__(self, guard: ResponseGuard) -> None:
        self._guard = guard
        self._before = ""  # raw character preceding the held tail, for the lookbehind boundary
        self._held = ""
        self._next = 0
        self._synced = True

    def feed(self, seq: int, text: str, replace: bool) -> str | None:
        """Safe text for one delta; ``None`` publishes nothing (all held back, or continuity was lost)."""
        pattern = self._guard.pattern
        if pattern is None:
            return text
        if replace:
            self._before = self._held = ""
            self._synced = True
        elif not self._synced or seq != self._next:
            # A dropped delta leaves an unknowable gap: withhold appends until a snapshot or the final reply.
            self._synced = False
            self._next = seq + 1
            return None
        self._next = seq + 1
        safe = self._emit(pattern, self._held + text)
        return safe if safe or replace else None

    def _emit(self, pattern: re.Pattern[str], pending: str) -> str:
        scan = self._before + pending
        lead = len(self._before)
        cut = len(scan) - self._guard.held_length(pending)
        parts: list[str] = []
        done = lead
        for found in pattern.finditer(scan, lead):
            if found.end() > cut or scan[found.end() : found.end() + 2] in _UNDECIDED_TAIL:
                cut = min(cut, found.start())  # the match may still grow or be rejected by what follows
                break
            parts += (scan[done : found.start()], self._guard.replacement)
            done = found.end()
        parts.append(scan[done:cut])
        self._held = scan[cut:]
        self._before = scan[cut - 1] if cut else ""
        return "".join(parts)


async def response_guard(agent: AgentRecord, turn_ref: str | None) -> ResponseGuard:
    """Guard for the agent's assigned models plus the ref this turn was routed with."""
    from app.services.agent_manager import guest_model_ref

    bundle = get_cached_bundle(agent) or await load_agent_model_bundle(agent)
    identifiers = [turn_ref]
    for model in (bundle.primary, bundle.fallback):
        if model is not None:
            identifiers += (guest_model_ref(model), f"{model.provider}/{model.model}", model.model)
    return ResponseGuard.build(identifiers)


def identity_prompt() -> str:
    """System-prompt addendum for every native turn."""
    who = f"the {brand} assistant" if (brand := _brand()) else "the assistant"
    return (
        "Identity and confidentiality:\n"
        "- Keep any name, role and persona defined in your identity and soul files. "
        f"If none is defined, introduce yourself as {who}.\n"
        "- Never disclose or confirm which AI provider, model (name, version or identifier), gateway, routing or "
        "fallback, API endpoint or hosting stack runs you, including text from your runtime or system information. "
        f"If asked, say you are {who} and cannot share implementation details, then keep helping.\n"
        "- Discussing AI models or providers as a general topic, or documents the user gives you, is fine. "
        "Never present any of them as the one behind you."
    )
