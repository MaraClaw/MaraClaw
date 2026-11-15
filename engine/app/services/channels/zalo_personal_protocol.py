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


class Message(Frame):
    type: Literal["message"]
    message_id: str = Field(min_length=1, max_length=256)
    sender_id: ProviderUID
    thread_id: ProviderUID
    from_me: bool
    timestamp: int = Field(ge=0, le=9007199254740991)
    text: str = Field(min_length=1, max_length=131072)

    @model_validator(mode="after")
    def limits(self) -> Message:
        if (
            not self.text.strip()
            or len(self.text.encode("utf-16-le")) // 2 > 131072
            or len(self.message_id.encode("utf-16-le")) // 2 > 256
        ):
            raise ValueError("message_limit")
        return self


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


type ChildFrame = CredsSet | QR | Identity | Ready | Message | Closed | SendResult
child_frame = TypeAdapter(Annotated[ChildFrame, Field(discriminator="type")])
