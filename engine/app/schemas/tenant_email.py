"""Organization SMTP boundary models; response models never contain credentials."""

import ipaddress
import re
from typing import Annotated, ClassVar, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    StringConstraints,
    TypeAdapter,
    field_validator,
    model_validator,
)


class TenantEmailFields(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    enabled: bool = False
    from_address: str = Field(default="", max_length=320)
    from_name: str = Field(default="", max_length=200)
    smtp_host: str = Field(default="", max_length=253)
    smtp_port: int = Field(default=465, ge=1, le=65535)
    smtp_username: str = Field(default="", max_length=320)
    smtp_ssl: bool = True
    smtp_timeout_seconds: int = Field(default=15, ge=1, le=30)

    @field_validator("from_name", "smtp_username")
    @classmethod
    def header_safe(cls, value: str) -> str:
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("Control characters are not allowed")
        return value

    @field_validator("from_address")
    @classmethod
    def sender_address(cls, value: str) -> str:
        return str(TypeAdapter(EmailStr).validate_python(value)) if value else ""

    @field_validator("smtp_host")
    @classmethod
    def smtp_hostname(cls, value: str) -> str:
        if not value:
            return ""
        try:
            return str(ipaddress.ip_address(value))
        except ValueError:
            hostname = value.rstrip(".").encode("idna").decode("ascii").lower()
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", hostname) or any(
            not label or len(label) > 63 or label.startswith("-") or label.endswith("-")
            for label in hostname.split(".")
        ):
            raise ValueError("SMTP host must be a hostname or IP address, not a URL")
        return hostname


class TenantEmailUpdate(TenantEmailFields):
    password: Annotated[str, StringConstraints(strip_whitespace=False)] | None = Field(default=None, max_length=4096)

    @model_validator(mode="after")
    def enabled_transport(self) -> Self:
        if self.enabled and not (self.from_address and self.smtp_host):
            raise ValueError("Enabled SMTP requires a sender address and host")
        return self


class TenantEmailSettings(TenantEmailFields):
    """Decrypted internal draft; public-only provenance is never client-controlled."""

    password: Annotated[str, StringConstraints(strip_whitespace=False)] = ""
    public_only: bool = True


class TenantEmailOut(TenantEmailFields):
    has_password: bool = False
    effective_source: Literal["tenant", "none"] = "none"


class TenantEmailTestOut(BaseModel):
    message: str
