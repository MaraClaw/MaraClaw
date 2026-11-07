"""Bounded, bootstrap-guarded native gateway execution in an owned guest."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from time import monotonic
from typing import Annotated, ClassVar, Literal, assert_never, cast
from uuid import uuid4

import anyio
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    NonNegativeInt,
    TypeAdapter,
    ValidationError,
    ValidatorFunctionWrapHandler,
    field_validator,
)
from pywhales import ClientNotFoundError, observation
from pywhales.client_config import ParsingError
from pywhales.exceptions import DockerException, ObservationCancelled, ObservationLimitError, ObservationTimeout

from app.dao.agent_dao import agent_dao
from app.records.agent import AgentRecord
from app.records.gateway_message import GatewayMessageRecord
from app.services.agent_setup_lock import drain_on_cancel
from app.services.guest_bootstrap import BOOTSTRAP_PENDING_EXIT, BOOTSTRAP_PENDING_STDERR, guarded_argv
from app.services.guest_identity import GuestOwnershipError, guest_resolutions, resolve_guest
from app.services.onboarding_greeting import build_identity_context
from app.services.openclaw_gateway_stream import EXEC_POLICY, DeltaRelay, streams_to_web
from app.services.openclaw_turns import runtime_session_key
from app.services.openclaw_update_exclusion import guest_write_section
from app.services.openclaw_update_gate import assert_work_allowed
from app.services.response_identity import identity_prompt, response_guard
from app.services.turn_timing import AdapterTiming, TurnTimeline


@dataclass(frozen=True, slots=True)
class TerminalOutcome:
    message_id: str
    run_id: str
    status: Literal["ok", "error", "timeout"]
    text: str
    code: str | None
    timing: AdapterTiming | None = None
    kind: Literal["terminal"] = field(default="terminal", init=False)
    admission: Literal["accepted"] = field(default="accepted", init=False)


@dataclass(frozen=True, slots=True)
class NotAdmittedOutcome:
    message_id: str
    admission: Literal["not_submitted", "rejected"]
    code: str
    timing: AdapterTiming | None = None
    kind: Literal["not_admitted"] = field(default="not_admitted", init=False)


@dataclass(frozen=True, slots=True)
class UncertainOutcome:
    message_id: str
    admission: Literal["unknown", "accepted"]
    run_id: str | None
    code: str
    timing: AdapterTiming | None = None
    kind: Literal["uncertain"] = field(default="uncertain", init=False)


NativeTurnOutcome = TerminalOutcome | NotAdmittedOutcome | UncertainOutcome


class _WireTiming(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True, strict=True)
    hello_ok: NonNegativeInt | None = Field(default=None, alias="helloOk")
    accepted: NonNegativeInt | None = None
    terminal: NonNegativeInt | None = None


class _WireBase(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True, strict=True)
    version: Literal[1]
    message_id: str = Field(alias="messageId")
    timing: _WireTiming | None = None

    @field_validator("timing", mode="wrap")
    @classmethod
    def _timing_is_diagnostic(cls, value: object, handler: ValidatorFunctionWrapHandler) -> _WireTiming | None:
        """Malformed timing is dropped; it must never turn a valid outcome into an uncertain one."""
        try:
            return cast("_WireTiming | None", handler(value))
        except ValidationError:
            return None


class _WireTerminal(_WireBase):
    kind: Literal["terminal"]
    admission: Literal["accepted"]
    run_id: str = Field(alias="runId")
    status: Literal["ok", "error", "timeout"]
    text: str
    code: str | None


class _WireNotAdmitted(_WireBase):
    kind: Literal["not_admitted"]
    admission: Literal["not_submitted", "rejected"]
    code: str


class _WireUncertain(_WireBase):
    kind: Literal["uncertain"]
    admission: Literal["unknown", "accepted"]
    run_id: str | None = Field(alias="runId")
    code: str


_wire = TypeAdapter(Annotated[_WireTerminal | _WireNotAdmitted | _WireUncertain, Field(discriminator="kind")])
_policy = EXEC_POLICY


def _adapter_timing(wire: _WireTiming | None) -> AdapterTiming | None:
    return None if wire is None else AdapterTiming(wire.hello_ok, wire.accepted, wire.terminal)


def _parse_outcome(output: str, message_id: str) -> NativeTurnOutcome:
    try:
        outcome = _wire.validate_json(output)
    except ValidationError:
        return UncertainOutcome(message_id, "unknown", None, "invalid_output")
    if outcome.message_id != message_id:
        return UncertainOutcome(message_id, "unknown", None, "message_mismatch")
    timing = _adapter_timing(outcome.timing)
    match outcome:
        case _WireTerminal(run_id=run_id, status=status, text=text, code=code):
            if run_id != message_id:
                return UncertainOutcome(message_id, "unknown", None, "run_mismatch", timing)
            return TerminalOutcome(message_id, run_id, status, text, code, timing)
        case _WireNotAdmitted(admission=admission, code=code):
            return NotAdmittedOutcome(message_id, admission, code, timing)
        case _WireUncertain(admission=admission, run_id=run_id, code=code):
            if run_id is not None and run_id != message_id:
                return UncertainOutcome(message_id, "unknown", None, "run_mismatch", timing)
            return UncertainOutcome(message_id, admission, run_id, code, timing)
        case unreachable:
            assert_never(unreachable)


def _execute(container_id: str, payload_name: str, cancel: Event) -> str:
    from app.services.agent_manager import agent_manager

    client = agent_manager.docker_client
    if client is None:
        raise RuntimeError("Docker client unavailable")
    argv = guarded_argv(
        ["node", "/home/node/custom-state/.maraclaw-rpc.mjs", f"/home/node/custom-state/{payload_name}"]
    )
    with observation(_policy, deadline=monotonic() + 940, cancel=cancel):
        return client.container.execute(container_id, argv, stream=False, timeout=940)


async def execute_gateway_turn(
    agent: AgentRecord, message: GatewayMessageRecord, *, timing: TurnTimeline | None = None
) -> NativeTurnOutcome:
    """Execute one claimed turn without settling or replaying the durable message."""
    from app.services.agent_manager import agent_manager
    from app.services.openclaw_gateway_config import prepare_native_gateway_files

    message_id = str(message.id)
    client = agent_manager.docker_client
    if client is None:
        return NotAdmittedOutcome(message_id, "not_submitted", "guest_unavailable")
    stream_reply = await streams_to_web(message)
    guard = await response_guard(agent, message.guest_model_ref) if stream_reply else None
    async with guest_write_section(agent.id):
        fresh = await agent_dao.get(agent.id)
        if fresh is None or fresh.creator_id != agent.creator_id or fresh.tenant_id != agent.tenant_id:
            return NotAdmittedOutcome(message_id, "not_submitted", "agent_changed")
        guest = guest_resolutions.get(fresh)
        try:
            if guest is None:

                def inspect():
                    with observation(_policy, deadline=monotonic() + 10):
                        return resolve_guest(client, fresh)

                guest = await drain_on_cancel(asyncio.to_thread(inspect))
                if guest is None:
                    return NotAdmittedOutcome(message_id, "not_submitted", "guest_unavailable")
                guest_resolutions.put(fresh, guest)

            def inspect_running() -> bool:
                with observation(_policy, deadline=monotonic() + 10):
                    state = client.container.inspect(guest.id).state
                    return state is not None and state.running is True

            running = await drain_on_cancel(asyncio.to_thread(inspect_running))
        except (
            ClientNotFoundError,
            DockerException,
            GuestOwnershipError,
            ObservationCancelled,
            ObservationLimitError,
            ObservationTimeout,
            OSError,
            ParsingError,
            ValidationError,
        ):
            guest_resolutions.drop(agent.id)
            return NotAdmittedOutcome(message_id, "not_submitted", "guest_unavailable")
        if not running:
            guest_resolutions.drop(agent.id)
            return NotAdmittedOutcome(message_id, "not_submitted", "guest_unavailable")
        identity_context = await build_identity_context(fresh, message.sender_user_id)
        try:
            agent_dir = agent_manager._agent_dir(agent.id)
            prepare_native_gateway_files(agent_dir)
            payload_name = f".maraclaw-rpc-{uuid4()}.json"
            payload_path = Path(agent_dir) / payload_name
            payload = {
                "version": 2 if stream_reply else 1,
                "messageId": message_id,
                "sessionKey": runtime_session_key(message),
                "message": message.content,
                "modelRef": message.guest_model_ref,
                "extraSystemPrompt": f"{identity_prompt()}\n\n{identity_context}",
                "timeoutSeconds": 900,
            }
            if stream_reply:
                payload["stream"] = True
            fd = os.open(payload_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except OSError:
            return NotAdmittedOutcome(message_id, "not_submitted", "filesystem_error")
        try:
            try:
                try:
                    with os.fdopen(fd, "w", encoding="utf-8", closefd=False) as stream:
                        json.dump(payload, stream)
                finally:
                    os.close(fd)
            except OSError:
                return NotAdmittedOutcome(message_id, "not_submitted", "filesystem_error")
            await assert_work_allowed(agent.id)
            cancel = Event()
            if timing is not None:
                timing.mark("exec_started")
            relay = DeltaRelay(message, timing, guard) if stream_reply else None
            consumer = asyncio.create_task(relay.publish()) if relay is not None else None
            execute = relay.execute if relay is not None else _execute
            task = asyncio.create_task(asyncio.to_thread(execute, guest.id, payload_name, cancel))
            try:
                output = await asyncio.shield(task)
                if relay is not None and consumer is not None:
                    await relay.drain(consumer)
            except asyncio.CancelledError:
                cancel.set()
                with anyio.CancelScope(shield=True):
                    while not task.done():
                        try:
                            await asyncio.shield(asyncio.wait({task}))
                        except asyncio.CancelledError:
                            continue
                    if not task.cancelled():
                        _ = task.exception()
                raise
            except DockerException as exc:
                guest_resolutions.drop(agent.id)
                if exc.return_code == BOOTSTRAP_PENDING_EXIT and (exc.stderr or "").startswith(
                    BOOTSTRAP_PENDING_STDERR
                ):
                    return NotAdmittedOutcome(message_id, "not_submitted", "bootstrap_pending")
                return UncertainOutcome(message_id, "unknown", None, "exec_failed")
            finally:
                if consumer is not None:
                    consumer.cancel()
                    with suppress(asyncio.CancelledError):
                        await consumer
            return _parse_outcome(output, message_id)
        finally:
            payload_path.unlink(missing_ok=True)
