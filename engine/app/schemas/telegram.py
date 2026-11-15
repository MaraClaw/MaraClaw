from typing import Annotated, ClassVar, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

type TelegramBotToken = Annotated[
    str, StringConstraints(strict=True, pattern=r"^[1-9][0-9]*:[A-Za-z0-9_-]+$", max_length=512)
]
type TelegramSecretToken = Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Za-z0-9_-]{1,256}$")]


class TelegramChannelConfigUpdate(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    bot_token: TelegramBotToken | None = Field(default=None, repr=False)
    secret_token: TelegramSecretToken | None = Field(default=None, repr=False)


class TelegramCredentials(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    bot_token: TelegramBotToken = Field(repr=False)
    secret_token: TelegramSecretToken = Field(repr=False)


class TelegramSender(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, strict=True, extra="ignore", hide_input_in_errors=True)

    id: int = Field(gt=0)
    is_bot: bool


class TelegramChat(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, strict=True, extra="ignore", hide_input_in_errors=True)

    id: int
    type: str


class TelegramMessage(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, strict=True, extra="ignore", hide_input_in_errors=True)

    message_id: int = Field(gt=0)
    chat: TelegramChat
    sender: TelegramSender | None = Field(default=None, alias="from")
    sender_chat: TelegramChat | None = None
    text: str | None = None
    date: int | None = Field(default=None, ge=0, le=253402300799)


class TelegramUpdate(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, strict=True, extra="ignore", hide_input_in_errors=True)

    update_id: int = Field(ge=0)
    message: TelegramMessage | None = None


class TelegramWebhookReply(TypedDict):
    method: Literal["sendMessage"]
    chat_id: str
    text: str


class TelegramSentMessage(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, strict=True, extra="ignore", hide_input_in_errors=True)

    message_id: int = Field(gt=0)


class TelegramResponseParameters(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, strict=True, extra="ignore", hide_input_in_errors=True)

    retry_after: int | None = Field(default=None, ge=0, le=2147483647)


class TelegramSendResponse(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, strict=True, extra="ignore", hide_input_in_errors=True)

    ok: bool
    result: TelegramSentMessage | None = None
    error_code: int | None = None
    parameters: TelegramResponseParameters | None = None
