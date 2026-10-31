import re
from time import monotonic
from typing import Annotated, ClassVar, Literal, TypedDict, get_args
from uuid import UUID

from cryptography.exceptions import InvalidTag
from fastapi import HTTPException
from psycopg import Error as PsycopgError
from pydantic import ConfigDict, Field, JsonValue, TypeAdapter, ValidationError

from app.core.logging import logger
from app.db.errors import DbError
from app.records.whatsapp_personal import PersonalConnection
from app.services.channels.whatsapp_personal_events import handle_frame
from app.services.channels.whatsapp_personal_protocol import AuthGet, AuthSet, ChildFrame, CredsSet, Frame

Category = Literal[
    "pre-key",
    "session",
    "sender-key",
    "sender-key-memory",
    "app-state-sync-key",
    "app-state-sync-version",
    "lid-mapping",
    "device-list",
    "tctoken",
    "identity-key",
    "creds",
    "other",
]
_CATEGORIES = frozenset(get_args(Category))
DropReason = Literal[
    "not_notify",
    "identity_unavailable",
    "non_self",
    "missing_content",
    "stub",
    "content_shape",
    "text",
    "quoted_or_forwarded",
    "timestamp",
]
ErrorKind = Literal["validation", "json", "timeout", "parent_rejected", "closed", "database", "crypto", "http", "other"]


class ErrorSummary(TypedDict, total=False):
    error_kind: ErrorKind
    sqlstate: str
    status_code: int


def error_summary(error: BaseException) -> ErrorSummary:
    if isinstance(error, BaseExceptionGroup):
        return error_summary(error.exceptions[0])
    if isinstance(error, DbError):
        return error_summary(error.orig) if isinstance(error.orig, PsycopgError) else {"error_kind": "database"}
    if isinstance(error, PsycopgError):
        state = error.sqlstate
        if state is not None and re.fullmatch(r"[A-Z0-9]{5}", state):
            return {"error_kind": "database", "sqlstate": state}
        return {"error_kind": "database"}
    if isinstance(error, InvalidTag):
        return {"error_kind": "crypto"}
    if isinstance(error, ValidationError):
        return {"error_kind": "validation"}
    if isinstance(error, HTTPException):
        return {"error_kind": "http", "status_code": error.status_code}
    if isinstance(error, TimeoutError):
        return {"error_kind": "timeout"}
    return {"error_kind": "other"}


class Diagnostic(Frame):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid", strict=True)
    type: Literal["diagnostic"]
    stage: Literal[
        "init",
        "auth_initial_decode",
        "auth_read",
        "auth_key_decode",
        "auth_write_encode",
        "auth_rpc",
        "connection",
        "reconnect",
        "identity",
        "intake",
        "send",
        "stop",
        "bridge_eof",
        "worker_exit",
        "parent_start",
        "parent_eof",
        "parent_exit",
        "parent_initial_read",
        "parent_failure",
        "parent_frame",
    ]
    event: Literal["start", "committed", "failure", "update", "finished"]
    operation: Literal["auth_get", "auth_set", "creds_set"] | None = None
    frame_type: Literal["ready", "qr", "identity", "message", "closed", "send_result"] | None = None
    category: Category | None = None
    categories: list[Category] | None = Field(default=None, max_length=12)
    rpc_id: UUID | None = None
    count: int | None = Field(default=None, ge=0, le=1000000)
    dropped: int | None = Field(default=None, ge=0, le=1000000)
    drop_reasons: list[DropReason] | None = Field(default=None, max_length=9)
    event_kind: Literal["notify", "append"] | None = None
    has_request_id: bool | None = None
    elapsed_ms: int | None = Field(default=None, ge=0, le=86400000)
    retry_count: int | None = Field(default=None, ge=0, le=1000)
    delay_ms: int | None = Field(default=None, ge=0, le=86400000)
    error_kind: ErrorKind | None = None
    value_shape: Literal["null", "bytes", "array", "object", "string", "number", "boolean", "other"] | None = None
    sqlstate: str | None = Field(default=None, pattern=r"^[A-Z0-9]{5}$")
    issue_code: (
        Literal[
            "invalid_type",
            "too_big",
            "too_small",
            "invalid_format",
            "not_multiple_of",
            "unrecognized_keys",
            "invalid_union",
            "invalid_key",
            "invalid_element",
            "invalid_value",
            "custom",
        ]
        | None
    ) = None
    status_code: int | None = Field(default=None, ge=0, le=999)
    exit_code: int | None = Field(default=None, ge=-255, le=255)
    connection_state: Literal["connecting", "open", "close", "unknown"] | None = None
    qr_available: bool | None = None
    identity_accepted: bool | None = None
    outcome: Literal["accepted", "rejected", "uncertain"] | None = None
    close_reason: (
        Literal[
            "logged_out", "replaced", "forbidden", "transport_failed", "auth_store_failed", "lease_expired", "shutdown"
        ]
        | None
    ) = None


diagnostic_frame = TypeAdapter(Annotated[ChildFrame | Diagnostic, Field(discriminator="type")])


def log_diagnostic(connection: PersonalConnection, run_id: UUID, frame: Diagnostic) -> None:
    fields = frame.model_dump(exclude={"type", "id", "token"}, exclude_none=True, mode="json")
    context = {
        "config_id": str(connection.config_id),
        "agent_id": str(connection.agent_id),
        "generation": str(connection.generation),
        "run_id": str(run_id),
        **fields,
    }
    message = " ".join(f"{key}={value}" for key, value in context.items())
    bound = logger.bind(**context)
    if frame.event == "failure":
        bound.warning("[WhatsAppPersonal] diagnostic {}", message)
    elif frame.stage == "auth_rpc" or frame.frame_type in {"message", "send_result"}:
        bound.debug("[WhatsAppPersonal] diagnostic {}", message)
    else:
        bound.info("[WhatsAppPersonal] diagnostic {}", message)


def auth_event(
    connection: PersonalConnection,
    run_id: UUID,
    frame: AuthGet | AuthSet | CredsSet,
    event: Literal["start", "committed", "failure"],
    started: float,
    error: BaseException | None = None,
) -> None:
    try:
        rpc_id = UUID(frame.id)
    except ValueError:
        rpc_id = None
    category = frame.category if isinstance(frame, AuthGet) else "creds" if isinstance(frame, CredsSet) else "other"
    if category not in _CATEGORIES:
        category = "other"
    count = (
        len(frame.ids)
        if isinstance(frame, AuthGet)
        else 1
        if isinstance(frame, CredsSet)
        else sum(len(b.values) for b in frame.batches)
    )
    data = {
        "type": "diagnostic",
        "id": "internal",
        "token": "internal",
        "stage": "auth_rpc",
        "event": event,
        "operation": frame.type,
        "category": category,
        "categories": list(dict.fromkeys(b.category if b.category in _CATEGORIES else "other" for b in frame.batches))
        if isinstance(frame, AuthSet)
        else [],
        "rpc_id": rpc_id,
        "count": count,
        "elapsed_ms": min(round((monotonic() - started) * 1000), 86400000),
        **(error_summary(error) if error is not None else {}),
    }
    log_diagnostic(connection, run_id, Diagnostic.model_validate(data))


def lifecycle(connection: PersonalConnection, run_id: UUID, stage: str, event: str, code: int | None = None) -> None:
    data = {
        "type": "diagnostic",
        "id": "internal",
        "token": "internal",
        "stage": stage,
        "event": event,
        "exit_code": code if code is not None and -255 <= code <= 255 else None,
    }
    log_diagnostic(connection, run_id, Diagnostic.model_validate(data))


def failed(connection: PersonalConnection, run_id: UUID, error: BaseException) -> None:
    data = {
        "type": "diagnostic",
        "id": "internal",
        "token": "internal",
        "stage": "parent_failure",
        "event": "failure",
        **error_summary(error),
    }
    log_diagnostic(connection, run_id, Diagnostic.model_validate(data))


async def observed_frame(connection: PersonalConnection, run_id: UUID, frame: ChildFrame) -> JsonValue:
    if not isinstance(frame, (AuthGet, AuthSet, CredsSet)):
        context = {
            "type": "diagnostic",
            "id": "internal",
            "token": "internal",
            "stage": "parent_frame",
            "frame_type": frame.type,
        }
        try:
            result = await handle_frame(connection, frame)
        except Exception as error:
            log_diagnostic(
                connection, run_id, Diagnostic.model_validate({**context, "event": "failure", **error_summary(error)})
            )
            raise
        log_diagnostic(connection, run_id, Diagnostic.model_validate({**context, "event": "finished"}))
        return result
    started = monotonic()
    auth_event(connection, run_id, frame, "start", started)
    try:
        result = await handle_frame(connection, frame)
    except Exception as error:
        auth_event(connection, run_id, frame, "failure", started, error)
        raise
    auth_event(connection, run_id, frame, "committed", started)
    return result
