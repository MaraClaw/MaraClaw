from typing import Literal, NotRequired

from typing_extensions import TypedDict


class RegistrationConfigResponse(TypedDict):
    invitation_code_required: bool


class ForgotPasswordResponse(TypedDict):
    ok: bool
    message: str


class ResetPasswordResponse(TypedDict):
    ok: bool


class ChangePasswordResponse(TypedDict):
    ok: bool
    must_change_password: bool
    access_token: NotRequired[str]


class EmailConflict(TypedDict):
    type: Literal["email"]
    scope: Literal["global"]
    message: str


class CheckDuplicateResponse(TypedDict):
    email_exists: bool
    conflicts: list[EmailConflict]
    has_conflict: bool


class AuthProviderResponse(TypedDict):
    id: str
    provider_type: str
    name: str
    is_active: bool


class ResendVerificationResponse(ForgotPasswordResponse):
    pass
