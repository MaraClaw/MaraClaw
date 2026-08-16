"""Complexity preflight: pick primary vs secondary before a conversational turn.

Fallback is a failure lane, not a complexity lane. This module never uses
fallback as a cheap-model substitute.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any

from app.core.logging import logger
from app.records.agent import AgentRecord
from app.records.llm import LLMModelRecord
from app.services.llm.turn import Complexity, ModelBundle, ModelSlot
from app.services.llm.types import OpenAIMessage

CLASSIFIER_TIMEOUT_SECONDS = 1.5
CLASSIFIER_MAX_TOKENS = 16
CLASSIFIER_INPUT_CHARS = 2000
HEURISTIC_COMPLEX_CHARS = 1200
HEURISTIC_MANAGEABLE_CHARS = 160
RECENT_HISTORY_TURNS = 6

CLASSIFIER_SYSTEM_PROMPT = (
    "You classify one user request for a digital employee.\n"
    "Reply with JSON only: {\"complexity\":\"complex\"} or {\"complexity\":\"manageable\"}.\n"
    "manageable: greetings, acknowledgements, single-fact answers, short rewrites, "
    "short translations, format conversion, yes/no, or a one-step lookup that needs "
    "at most one tool.\n"
    "complex: multi-step work, planning, research, coding with tools, policy / legal / "
    "OKR, long documents, ambiguous goals, or anything likely to need more than two "
    "tool rounds.\n"
    "If unsure, answer complex."
)

_COMPLEX_WORDS = frozenset(
    {
        "plan",
        "design",
        "investigate",
        "debug",
        "compare",
        "tradeoff",
        "architecture",
        "migration",
        "spec",
        "policy",
        "okr",
        "roadmap",
        "implement",
        "refactor",
        "analyze",
        "research",
    }
)
_COMPLEX_PHRASES = (
    "root cause",
    "write a",
    "draft a",
    "in the repo",
    "across all agents",
)
_WORKSPACE_HINTS = ("read_file", "in the repo", "across all agents")
_MANAGEABLE_EXACT = frozenset(
    {
        "ok",
        "okay",
        "k",
        "kk",
        "yes",
        "y",
        "no",
        "n",
        "thanks",
        "thank you",
        "thx",
        "ty",
        "hello",
        "hi",
        "hey",
        "yo",
        "ping",
        "pong",
        "good morning",
        "good afternoon",
        "good evening",
        "gm",
        "got it",
        "cool",
        "great",
        "sure",
        "np",
        "please",
    }
)
_MANAGEABLE_PREFIXES = (
    "what time",
    "translate this:",
    "translate:",
    "what did i just say",
)
_WORD_RE = re.compile(r"[a-z0-9']+")
_JSON_OBJECT_RE = re.compile(r"\{[^{}]*\}")
_IMAGE_PAYLOAD_RE = re.compile(
    r"\[image_data:[^\]]*\]|data:image/[A-Za-z0-9.+-]+;base64,[A-Za-z0-9+/=]+",
    re.IGNORECASE,
)


@dataclass(slots=True)
class TurnSelection:
    """Result of preflight routing for one inbound user turn."""

    model: LLMModelRecord | None
    slot: ModelSlot
    complexity: Complexity | None
    reason: str
    failover_model: LLMModelRecord | None = None
    classifier_ms: int | None = None
    classifier_tokens: int | None = None


def _usable(model: LLMModelRecord | None) -> LLMModelRecord | None:
    if model is None:
        return None
    if getattr(model, "enabled", True) is False:
        return None
    return model


def _model_id(model: LLMModelRecord | None) -> uuid.UUID | None:
    return getattr(model, "id", None) if model is not None else None


async def load_agent_model_bundle(
    agent: AgentRecord,
    *,
    primary: LLMModelRecord | None = None,
    secondary: LLMModelRecord | None = None,
    fallback: LLMModelRecord | None = None,
) -> ModelBundle:
    """Load any missing assigned models for ``agent``."""
    from app.dao.llm_dao import llm_model_dao

    missing_ids = [
        mid
        for mid, loaded in (
            (getattr(agent, "primary_model_id", None), primary),
            (getattr(agent, "secondary_model_id", None), secondary),
            (getattr(agent, "fallback_model_id", None), fallback),
        )
        if mid and loaded is None
    ]
    loaded: dict[Any, LLMModelRecord] = {}
    if missing_ids:
        loaded = {row.id: row for row in await llm_model_dao.get_many(missing_ids)}
    if primary is None and getattr(agent, "primary_model_id", None):
        primary = loaded.get(agent.primary_model_id)
    if secondary is None and getattr(agent, "secondary_model_id", None):
        secondary = loaded.get(agent.secondary_model_id)
    if fallback is None and getattr(agent, "fallback_model_id", None):
        fallback = loaded.get(agent.fallback_model_id)
    return ModelBundle(primary=primary, secondary=secondary, fallback=fallback)


def message_text(content: object) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return str(content)


def content_has_images(content: object) -> bool:
    if isinstance(content, str):
        return "[image_data:" in content or "data:image/" in content
    if isinstance(content, list):
        return any(isinstance(part, dict) and part.get("type") in {"image_url", "image"} for part in content)
    return False


def turn_has_images(user_text: str, history: list[OpenAIMessage] | None = None) -> bool:
    """True only when the *current* user text carries an image. History is ignored."""
    del history
    return content_has_images(user_text)


def history_without_current_user(
    history: list[OpenAIMessage] | None,
    user_text: str,
) -> list[OpenAIMessage]:
    """Drop a trailing user turn that duplicates ``user_text`` (WS appends first)."""
    if not history:
        return []
    out = list(history)
    current = (user_text or "").strip()
    while out:
        last = out[-1]
        if last.get("role") != "user":
            break
        if message_text(last.get("content")).strip() != current:
            break
        out.pop()
    return out


def strip_image_payloads(text: str) -> str:
    return _IMAGE_PAYLOAD_RE.sub("[image attached]", text)


def recent_has_tools(history: list[OpenAIMessage] | None, last_n: int = RECENT_HISTORY_TURNS) -> bool:
    for msg in (history or [])[-last_n:]:
        if msg.get("tool_calls"):
            return True
        role = msg.get("role")
        if role in {"tool", "tool_call"}:
            return True
    return False


def parse_complexity_label(text: str | None) -> Complexity | None:
    if not text:
        return None
    stripped = text.strip()
    candidates = [stripped]
    match = _JSON_OBJECT_RE.search(stripped)
    if match:
        candidates.append(match.group(0))
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            value = str(data.get("complexity", "")).strip().lower()
            if value in {"complex", "manageable"}:
                return value  # type: ignore[return-value]
        if isinstance(data, str) and data.strip().lower() in {"complex", "manageable"}:
            return data.strip().lower()  # type: ignore[return-value]
    word = stripped.strip("\"'` \n\t{}").lower()
    if word in {"complex", "manageable"}:
        return word  # type: ignore[return-value]
    return None


def heuristic_complexity(
    user_text: str,
    *,
    history: list[OpenAIMessage] | None = None,
    has_images: bool = False,
) -> Complexity | None:
    """Return a confident label, or None when the LLM classifier should run."""
    text = (user_text or "").strip()
    lowered = text.lower()
    words = set(_WORD_RE.findall(lowered))
    tools_recent = recent_has_tools(history)

    if len(text) > HEURISTIC_COMPLEX_CHARS:
        return "complex"
    if tools_recent:
        return "complex"
    if words & _COMPLEX_WORDS:
        return "complex"
    if any(phrase in lowered for phrase in _COMPLEX_PHRASES):
        return "complex"
    if any(hint in lowered for hint in _WORKSPACE_HINTS):
        return "complex"
    if text.count("?") > 1 and ("\n" in text or bool(re.search(r"^\s*\d+[.)-]", text, re.MULTILINE))):
        return "complex"

    if has_images:
        return None
    if len(text) > HEURISTIC_MANAGEABLE_CHARS:
        return None
    compact = " ".join(lowered.split())
    if compact in _MANAGEABLE_EXACT:
        return "manageable"
    if any(compact.startswith(prefix) for prefix in _MANAGEABLE_PREFIXES):
        return "manageable"
    if text.endswith("?") and len(text) <= HEURISTIC_MANAGEABLE_CHARS and not (words & _COMPLEX_WORDS):
        return "manageable"
    return None


def _failover_for(
    *,
    selected: LLMModelRecord | None,
    slot: ModelSlot,
    bundle: ModelBundle,
) -> LLMModelRecord | None:
    fallback = _usable(bundle.fallback)
    primary = _usable(bundle.primary)
    selected_id = _model_id(selected)
    if fallback is not None and _model_id(fallback) != selected_id:
        return fallback
    if slot == "secondary" and primary is not None and _model_id(primary) != selected_id:
        return primary
    return None


def _selection(
    model: LLMModelRecord | None,
    slot: ModelSlot,
    complexity: Complexity | None,
    reason: str,
    bundle: ModelBundle,
    *,
    classifier_ms: int | None = None,
    classifier_tokens: int | None = None,
) -> TurnSelection:
    return TurnSelection(
        model=model,
        slot=slot,
        complexity=complexity,
        reason=reason,
        failover_model=_failover_for(selected=model, slot=slot, bundle=bundle),
        classifier_ms=classifier_ms,
        classifier_tokens=classifier_tokens,
    )


def _default_worker(bundle: ModelBundle) -> tuple[LLMModelRecord | None, ModelSlot]:
    primary = _usable(bundle.primary)
    if primary is not None:
        return primary, "primary"
    fallback = _usable(bundle.fallback)
    if fallback is not None:
        return fallback, "fallback"
    secondary = _usable(bundle.secondary)
    if secondary is not None:
        return secondary, "secondary"
    return None, "primary"


def classifier_user_payload(user_text: str, history: list[OpenAIMessage] | None) -> str:
    prior = history_without_current_user(history, user_text)
    previous = ""
    for msg in reversed(prior):
        if msg.get("role") == "user":
            previous = message_text(msg.get("content"))
            break
    current = strip_image_payloads((user_text or "")[:CLASSIFIER_INPUT_CHARS])
    previous = strip_image_payloads(previous[:CLASSIFIER_INPUT_CHARS])
    if previous:
        return f"Previous user turn:\n{previous}\n\nCurrent user turn:\n{current}"
    return current


async def _classify_with_llm(
    secondary: LLMModelRecord,
    user_text: str,
    history: list[OpenAIMessage] | None,
    agent_id: uuid.UUID | None,
) -> tuple[Complexity | None, int, int]:
    from app.services.llm.utils import create_llm_client, get_model_api_key
    from app.services.token_tracker import extract_token_usage, record_token_usage

    started = time.perf_counter()
    tokens = 0
    try:
        client = create_llm_client(
            provider=secondary.provider,
            api_key=get_model_api_key(secondary),
            model=secondary.model,
            base_url=secondary.base_url,
            timeout=CLASSIFIER_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        logger.warning("[LLM route] classifier client failed: {}", exc)
        return None, int((time.perf_counter() - started) * 1000), 0

    from app.services.llm.utils import LLMMessage

    try:
        response = await asyncio.wait_for(
            client.complete(
                messages=[
                    LLMMessage(role="system", content=CLASSIFIER_SYSTEM_PROMPT),
                    LLMMessage(role="user", content=classifier_user_payload(user_text, history)),
                ],
                tools=None,
                temperature=0,
                max_tokens=CLASSIFIER_MAX_TOKENS,
            ),
            timeout=CLASSIFIER_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        logger.warning("[LLM route] classifier timed out")
        await client.close()
        return None, int((time.perf_counter() - started) * 1000), 0
    except Exception as exc:
        logger.warning("[LLM route] classifier error: {}", exc)
        await client.close()
        return None, int((time.perf_counter() - started) * 1000), 0

    await client.close()
    usage = extract_token_usage(getattr(response, "usage", None))
    if usage:
        tokens = usage.total_tokens
        if agent_id and tokens > 0:
            await record_token_usage(agent_id, usage)
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return parse_complexity_label(getattr(response, "content", None)), elapsed_ms, tokens


async def select_turn_model(
    bundle: ModelBundle,
    *,
    user_text: str,
    history: list[OpenAIMessage] | None = None,
    skip_tools: bool = False,
    has_images: bool | None = None,
    force_primary: bool = False,
    agent_id: uuid.UUID | None = None,
) -> TurnSelection:
    """Pick the model for one inbound user turn."""
    prior_history = history_without_current_user(history, user_text)
    images = turn_has_images(user_text) if has_images is None else has_images
    primary = _usable(bundle.primary)
    secondary = _usable(bundle.secondary)
    default_model, default_slot = _default_worker(bundle)
    primary_vision = bool(primary and getattr(primary, "supports_vision", False))
    secondary_vision = bool(secondary and getattr(secondary, "supports_vision", False))

    if force_primary:
        selected = _selection(default_model, default_slot, "complex", "force_primary", bundle)
    elif skip_tools:
        if secondary is not None:
            selected = _selection(secondary, "secondary", "manageable", "greeting", bundle)
        else:
            selected = _selection(default_model, default_slot, "manageable", "greeting", bundle)
    elif secondary is None:
        selected = _selection(default_model, default_slot, None, "no_secondary", bundle)
    elif images and primary_vision and not secondary_vision:
        selected = _selection(primary or default_model, "primary", "complex", "vision", bundle)
    elif images and secondary_vision and not primary_vision:
        selected = _selection(secondary, "secondary", "complex", "vision", bundle)
    else:
        guessed = heuristic_complexity(user_text, history=prior_history, has_images=images)
        if guessed == "complex":
            selected = _selection(primary or default_model, "primary" if primary else default_slot, "complex", "heuristic_complex", bundle)
        elif guessed == "manageable":
            selected = _selection(secondary, "secondary", "manageable", "heuristic_manageable", bundle)
        else:
            label, classifier_ms, classifier_tokens = await _classify_with_llm(
                secondary, user_text, prior_history, agent_id
            )
            if label == "manageable":
                selected = _selection(
                    secondary,
                    "secondary",
                    "manageable",
                    "classifier",
                    bundle,
                    classifier_ms=classifier_ms,
                    classifier_tokens=classifier_tokens,
                )
            else:
                reason = "classifier" if label == "complex" else "fail_closed"
                selected = _selection(
                    primary or default_model,
                    "primary" if primary else default_slot,
                    "complex",
                    reason,
                    bundle,
                    classifier_ms=classifier_ms,
                    classifier_tokens=classifier_tokens,
                )

    logger.info(
        "[LLM route] agent={} complexity={} slot={} reason={} classifier_ms={} tokens={}",
        agent_id,
        selected.complexity,
        selected.slot,
        selected.reason,
        selected.classifier_ms,
        selected.classifier_tokens,
    )
    return selected
