"""Bounded, bootstrap-guarded native gateway execution in an owned guest."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from time import monotonic
from typing import Annotated, ClassVar, Literal, assert_never
from uuid import uuid4

import anyio
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
from pywhales import InspectionPolicy, observation
from pywhales.exceptions import DockerException

from app.dao.agent_dao import agent_dao
from app.records.agent import AgentRecord
from app.records.gateway_message import GatewayMessageRecord
from app.services.agent_setup_lock import drain_on_cancel
from app.services.guest_bootstrap import BOOTSTRAP_PENDING_EXIT, BOOTSTRAP_PENDING_STDERR, guarded_argv
from app.services.guest_identity import GuestOwnershipError, resolve_guest
from app.services.openclaw_turns import runtime_session_key
from app.services.openclaw_update_exclusion import guest_write_section
from app.services.openclaw_update_gate import assert_work_allowed


@dataclass(frozen=True, slots=True)
class TerminalOutcome:
    message_id: str
    run_id: str
    status: Literal["ok", "error", "timeout"]
    text: str
    code: str | None
    kind: Literal["terminal"] = field(default="terminal", init=False)
    admission: Literal["accepted"] = field(default="accepted", init=False)


@dataclass(frozen=True, slots=True)
class NotAdmittedOutcome:
    message_id: str
    admission: Literal["not_submitted", "rejected"]
    code: str
    kind: Literal["not_admitted"] = field(default="not_admitted", init=False)


@dataclass(frozen=True, slots=True)
class UncertainOutcome:
    message_id: str
    admission: Literal["unknown", "accepted"]
    run_id: str | None
    code: str
    kind: Literal["uncertain"] = field(default="uncertain", init=False)


NativeTurnOutcome = TerminalOutcome | NotAdmittedOutcome | UncertainOutcome


class _WireBase(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True, strict=True)
    version: Literal[1]
    message_id: str = Field(alias="messageId")


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
_policy = InspectionPolicy(max_references=16, max_argv_bytes=32768, max_response_bytes=4_194_304)


def _parse_outcome(output: str, message_id: str) -> NativeTurnOutcome:
    try:
        outcome = _wire.validate_json(output)
    except ValidationError:
        return UncertainOutcome(message_id, "unknown", None, "invalid_output")
    if outcome.message_id != message_id:
        return UncertainOutcome(message_id, "unknown", None, "message_mismatch")
    match outcome:
        case _WireTerminal(run_id=run_id, status=status, text=text, code=code):
            if run_id != message_id:
                return UncertainOutcome(message_id, "unknown", None, "run_mismatch")
            return TerminalOutcome(message_id, run_id, status, text, code)
        case _WireNotAdmitted(admission=admission, code=code):
            return NotAdmittedOutcome(message_id, admission, code)
        case _WireUncertain(admission=admission, run_id=run_id, code=code):
            if run_id is not None and run_id != message_id:
                return UncertainOutcome(message_id, "unknown", None, "run_mismatch")
            return UncertainOutcome(message_id, admission, run_id, code)
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


async def execute_gateway_turn(agent: AgentRecord, message: GatewayMessageRecord) -> NativeTurnOutcome:
    """Execute one claimed turn without settling or replaying the durable message."""
    from app.services.agent_manager import agent_manager
    from app.services.openclaw_gateway_config import prepare_native_gateway_files

    message_id = str(message.id)
    client = agent_manager.docker_client
    if client is None:
        return NotAdmittedOutcome(message_id, "not_submitted", "guest_unavailable")
    async with guest_write_section(agent.id):
        await assert_work_allowed(agent.id)
        fresh = await agent_dao.get(agent.id)
        if fresh is None or fresh.creator_id != agent.creator_id or fresh.tenant_id != agent.tenant_id:
            return NotAdmittedOutcome(message_id, "not_submitted", "agent_changed")
        try:

            def inspect():
                with observation(_policy, deadline=monotonic() + 10):
                    return resolve_guest(client, fresh)

            guest = await drain_on_cancel(asyncio.to_thread(inspect))
        except DockerException, GuestOwnershipError:
            return NotAdmittedOutcome(message_id, "not_submitted", "guest_unavailable")
        if guest is None:
            return NotAdmittedOutcome(message_id, "not_submitted", "guest_unavailable")
        try:
            agent_dir = agent_manager._agent_dir(agent.id)
            prepare_native_gateway_files(agent_dir)
            payload_name = f".maraclaw-rpc-{uuid4()}.json"
            payload_path = Path(agent_dir) / payload_name
            payload = {
                "version": 1,
                "messageId": message_id,
                "sessionKey": runtime_session_key(message),
                "message": message.content,
                "modelRef": message.guest_model_ref,
                "timeoutSeconds": 900,
            }
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
            task = asyncio.create_task(asyncio.to_thread(_execute, guest.id, payload_name, cancel))
            try:
                output = await asyncio.shield(task)
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
                if exc.return_code == BOOTSTRAP_PENDING_EXIT and (exc.stderr or "").startswith(
                    BOOTSTRAP_PENDING_STDERR
                ):
                    return NotAdmittedOutcome(message_id, "not_submitted", "bootstrap_pending")
                return UncertainOutcome(message_id, "unknown", None, "exec_failed")
            return _parse_outcome(output, message_id)
        finally:
            payload_path.unlink(missing_ok=True)
