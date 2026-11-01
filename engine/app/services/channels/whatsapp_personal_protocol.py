from typing import Annotated, ClassVar, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, TypeAdapter


class Frame(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid", strict=True)
    id: str = Field(min_length=1, max_length=256)
    token: str = Field(min_length=1, max_length=256)


class AuthGet(Frame):
    type: Literal["auth_get"]
    category: str = Field(min_length=1, max_length=256)
    ids: list[str] = Field(max_length=10000)


class AuthBatch(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid", strict=True)
    category: str = Field(min_length=1, max_length=256)
    values: dict[str, JsonValue]


class AuthSet(Frame):
    type: Literal["auth_set"]
    batches: list[AuthBatch] = Field(min_length=1)


class CredsSet(Frame):
    type: Literal["creds_set"]
    creds: str = Field(min_length=1, max_length=1000000)


class Ready(Frame):
    type: Literal["ready"]


class QR(Frame):
    type: Literal["qr"]
    value: str = Field(min_length=1, max_length=8192)
    expires_at: AwareDatetime


class Identity(Frame):
    type: Literal["identity"]
    account_id: str = Field(min_length=1, max_length=256)
    pn_jid: str | None
    lid_jid: str | None


class Message(Frame):
    type: Literal["message"]
    message_id: str = Field(min_length=1, max_length=256)
    remote_jid: str = Field(min_length=1, max_length=256)
    participant: str | None
    from_me: bool
    timestamp: int
    text: str = Field(min_length=1, max_length=4096)
    event_kind: Literal["notify"]
    is_history: Literal[False]


class Closed(Frame):
    type: Literal["closed"]
    reason: Literal[
        "logged_out", "replaced", "forbidden", "transport_failed", "auth_store_failed", "lease_expired", "shutdown"
    ]


class SendResult(Frame):
    type: Literal["send_result"]
    command_id: str
    message_id: str
    outcome: Literal["accepted", "rejected", "uncertain"]


type ChildFrame = AuthGet | AuthSet | CredsSet | Ready | QR | Identity | Message | Closed | SendResult
child_frame = TypeAdapter(Annotated[ChildFrame, Field(discriminator="type")])
