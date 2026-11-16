import base64
import re
from typing import Annotated, ClassVar, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    field_validator,
    model_validator,
)


class Frame(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid", strict=True)
    id: str = Field(min_length=1, max_length=256)
    token: str = Field(min_length=1, max_length=512)


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


class MediaOffer(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid", strict=True)
    source_id: str = Field(min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$")
    kind: Literal["image", "file", "video"]
    filename: str = Field(min_length=1, max_length=1024)
    content_type: str = Field(min_length=1, max_length=256)
    size: int = Field(ge=0, le=9007199254740991)


class Message(Frame):
    type: Literal["message"]
    message_id: str = Field(min_length=1, max_length=256)
    remote_jid: str = Field(min_length=1, max_length=256)
    participant: str | None
    from_me: bool
    timestamp: int = Field(ge=0, le=9007199254740991)
    text: str = Field(max_length=4096)
    event_kind: Literal["notify"]
    is_history: Literal[False]
    media: MediaOffer | None = None

    @model_validator(mode="after")
    def require_content(self) -> Self:
        if not self.text and self.media is None:
            raise ValueError("personal_message_content_required")
        return self


MediaCode = Literal["invalid", "expired", "forbidden", "too_large", "download_failed", "digest_mismatch"]


class MediaTransfer(Frame):
    transfer_id: str = Field(min_length=1, max_length=256)


class MediaGet(MediaTransfer):
    type: Literal["media_get"]
    message_id: str = Field(min_length=1, max_length=256)
    source_id: str = Field(min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$")
    limit: int = Field(ge=1, le=50 * 1024 * 1024)


class MediaAck(MediaTransfer):
    type: Literal["media_ack"]
    seq: int = Field(ge=0, le=50 * 1024 * 1024)


class MediaChunk(MediaTransfer):
    type: Literal["media_chunk"]
    seq: int = Field(ge=0, le=50 * 1024 * 1024)
    data: str = Field(min_length=4, max_length=87384)

    @field_validator("data")
    @classmethod
    def canonical_chunk(cls, value: str) -> str:
        if re.fullmatch(r"(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?", value) is None:
            raise ValueError("personal_media_base64")
        size = len(value) // 4 * 3 - (2 if value.endswith("==") else 1 if value.endswith("=") else 0)
        if (
            size < 1
            or size > 65536
            or base64.b64encode(base64.b64decode(value, validate=True)).decode("ascii") != value
        ):
            raise ValueError("personal_media_chunk_limit")
        return value


class MediaEnd(MediaTransfer):
    type: Literal["media_end"]
    seq: int = Field(ge=0, le=50 * 1024 * 1024)
    size: int = Field(ge=1, le=50 * 1024 * 1024)
    sha256: str = Field(min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$")


class MediaError(MediaTransfer):
    type: Literal["media_error"]
    code: MediaCode


type TransferFrame = MediaChunk | MediaEnd | MediaError


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


type ChildFrame = AuthGet | AuthSet | CredsSet | Ready | QR | Identity | Message | Closed | SendResult | TransferFrame
child_frame = TypeAdapter(Annotated[ChildFrame, Field(discriminator="type")])
