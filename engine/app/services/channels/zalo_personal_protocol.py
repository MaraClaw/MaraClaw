import base64
from typing import Annotated, ClassVar, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationInfo,
    field_validator,
    model_validator,
)

from app.records.zalo_personal import ProviderUID


class Frame(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid", strict=True)
    id: str = Field(min_length=1, max_length=256)
    token: str = Field(min_length=1, max_length=512)

    @field_validator("id", "token")
    @classmethod
    def utf16_bound(cls, value: str, info: ValidationInfo) -> str:
        if len(value.encode("utf-16-le")) // 2 > (512 if info.field_name == "token" else 256):
            raise ValueError("frame_identifier_limit")
        return value


class CredsSet(Frame):
    type: Literal["creds_set"]
    creds: str = Field(min_length=1, max_length=768000)

    @field_validator("creds")
    @classmethod
    def credential_bound(cls, value: str) -> str:
        if len(value.encode("utf-16-le")) // 2 > 768000:
            raise ValueError("credentials_limit")
        return value


class QR(Frame):
    type: Literal["qr"]
    value: str = Field(min_length=1, max_length=768000)
    expires_at: AwareDatetime

    @field_validator("value")
    @classmethod
    def png(cls, value: str) -> str:
        try:
            decoded = base64.b64decode(value, validate=True)
        except ValueError:
            raise ValueError("qr_png_invalid") from None
        if not decoded.startswith(b"\x89PNG\r\n\x1a\n") or base64.b64encode(decoded).decode("ascii") != value:
            raise ValueError("qr_png_invalid")
        return value


class Identity(Frame):
    type: Literal["identity"]
    account_id: ProviderUID


class Ready(Frame):
    type: Literal["ready"]


class Heartbeat(Frame):
    type: Literal["heartbeat"]
    ok: bool


class Message(Frame):
    type: Literal["message"]
    message_id: str = Field(min_length=1, max_length=256)
    sender_id: ProviderUID
    thread_id: ProviderUID
    from_me: bool
    timestamp: int = Field(ge=0, le=9007199254740991)
    text: str = Field(max_length=131072)
    media: MediaOffer | None = None

    @model_validator(mode="after")
    def limits(self) -> Message:
        if (
            (not self.text.strip() and self.media is None)
            or len(self.text.encode("utf-16-le")) // 2 > 131072
            or len(self.message_id.encode("utf-16-le")) // 2 > 256
        ):
            raise ValueError("message_limit")
        return self


class MediaOffer(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid", strict=True)
    source_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    kind: Literal["image", "file"]
    filename: str = Field(min_length=3, max_length=180, pattern=r"^[A-Za-z0-9_][A-Za-z0-9_. -]*\.[A-Za-z0-9]{1,16}$")

    @field_validator("filename")
    @classmethod
    def safe_basename(cls, value: str) -> str:
        if ".." in value:
            raise ValueError("media_filename_invalid")
        return value


class MediaTransfer(Frame):
    transfer_id: str = Field(min_length=1, max_length=256)

    @field_validator("transfer_id")
    @classmethod
    def transfer_bound(cls, value: str) -> str:
        if len(value.encode("utf-16-le")) // 2 > 256:
            raise ValueError("media_transfer_id_limit")
        return value


class MediaChunk(MediaTransfer):
    type: Literal["media_chunk"]
    seq: int = Field(ge=0, le=800)
    data: str = Field(min_length=4, max_length=87384)

    @field_validator("data")
    @classmethod
    def canonical_chunk(cls, value: str) -> str:
        decoded = base64.b64decode(value, validate=True)
        if not 0 < len(decoded) <= 65536 or base64.b64encode(decoded).decode("ascii") != value:
            raise ValueError("media_chunk_invalid")
        return value


class MediaEnd(MediaTransfer):
    type: Literal["media_end"]
    size: int = Field(ge=1, le=50 * 1024 * 1024)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class MediaAck(MediaTransfer):
    type: Literal["media_ack"]
    seq: int = Field(ge=0, le=800)


class MediaReady(MediaTransfer):
    type: Literal["media_ready"]
    phase: Literal["receiving", "complete"]


type MediaCode = Literal[
    "invalid", "expired", "forbidden", "too_large", "download_failed", "digest_mismatch", "unsupported"
]


class MediaError(MediaTransfer):
    type: Literal["media_error"]
    code: MediaCode


class Closed(Frame):
    type: Literal["closed"]
    reason: Literal[
        "shutdown",
        "lease_expired",
        "protocol_error",
        "login_failed",
        "auth_store_failed",
        "transport_failed",
        "qr_expired",
        "qr_declined",
        "session_replaced",
    ]


class SendResult(Frame):
    type: Literal["send_result"]
    command_id: str = Field(min_length=1, max_length=256)
    message_id: str = Field(max_length=256)
    outcome: Literal["accepted", "rejected", "uncertain"]

    @model_validator(mode="after")
    def accepted_id(self) -> SendResult:
        if (
            (self.outcome == "accepted" and not self.message_id)
            or len(self.message_id.encode("utf-16-le")) // 2 > 256
            or len(self.command_id.encode("utf-16-le")) // 2 > 256
        ):
            raise ValueError("send_result_invalid")
        return self


Message.model_rebuild()
type TransferFrame = MediaChunk | MediaEnd | MediaAck | MediaReady | MediaError
type ChildFrame = CredsSet | QR | Identity | Ready | Heartbeat | Message | Closed | SendResult | TransferFrame
child_frame = TypeAdapter(Annotated[ChildFrame, Field(discriminator="type")])
