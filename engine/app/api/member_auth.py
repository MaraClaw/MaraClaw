"""Member auth routes select their audience before invoking shared record flows."""

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status

from app.api import auth
from app.core.security import get_authenticated_user, get_current_user
from app.dao import user_dao
from app.records.user import UserRecord
from app.schemas.member_auth import MemberAuthUser, MemberRegisterInitResponse, MemberTokenResponse
from app.schemas.schemas import (
    IdentityBindRequest,
    IdentityUnbindRequest,
    MultiTenantResponse,
    OAuthCallbackRequest,
    RegisterInitRequest,
    UserLogin,
    UserUpdate,
    VerifyEmailRequest,
)
from app.services.auth_audience import AuthAudience, require_member_user

router = APIRouter(prefix="/member/auth", tags=["member-auth"])


async def get_member_user(current_user: UserRecord = Depends(get_authenticated_user)) -> UserRecord:
    user = await user_dao.get_with_identity(current_user.id, fresh=True)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    require_member_user(user)
    return user


async def get_current_member(current_user: UserRecord = Depends(get_current_user)) -> UserRecord:
    require_member_user(current_user)
    return current_user


@router.post("/login", response_model=MemberTokenResponse | MultiTenantResponse)
async def login(
    data: UserLogin, background_tasks: BackgroundTasks, request: Request
) -> MemberTokenResponse | MultiTenantResponse:
    result = await auth.login_flow(data, background_tasks, request, AuthAudience.MEMBER)
    if isinstance(result, MultiTenantResponse):
        return result
    return MemberTokenResponse.from_session(result)


@router.post("/register/init", response_model=MemberRegisterInitResponse, status_code=status.HTTP_201_CREATED)
async def register_init(
    data: RegisterInitRequest, background_tasks: BackgroundTasks, request: Request
) -> MemberRegisterInitResponse:
    result = await auth.register_init_flow(data, background_tasks, request, AuthAudience.MEMBER)
    return MemberRegisterInitResponse.from_registration(result)


@router.post("/verify-email", response_model=MemberTokenResponse)
async def verify_email(data: VerifyEmailRequest) -> MemberTokenResponse:
    return MemberTokenResponse.from_session(await auth.verify_email_flow(data, AuthAudience.MEMBER))


@router.post("/{provider}/callback", response_model=MemberTokenResponse | MultiTenantResponse)
async def oauth_callback(provider: str, data: OAuthCallbackRequest) -> MemberTokenResponse | MultiTenantResponse:
    result = await auth.oauth_callback_flow(provider, data, AuthAudience.MEMBER)
    if isinstance(result, MultiTenantResponse):
        return result
    return MemberTokenResponse.from_session(result)


@router.get("/me", response_model=MemberAuthUser)
async def get_me(current_user: UserRecord = Depends(get_member_user)) -> MemberAuthUser:
    return MemberAuthUser.model_validate(current_user)


@router.patch("/me", response_model=MemberAuthUser)
async def update_me(data: UserUpdate, current_user: UserRecord = Depends(get_current_member)) -> MemberAuthUser:
    user = await auth.update_me_flow(data, current_user, AuthAudience.MEMBER)
    return MemberAuthUser.model_validate(user)


@router.post("/{provider}/bind", response_model=MemberAuthUser)
async def bind_identity(
    provider: str, data: IdentityBindRequest, current_user: UserRecord = Depends(get_current_member)
) -> MemberAuthUser:
    user = await auth.bind_identity_flow(provider, data, current_user, AuthAudience.MEMBER)
    return MemberAuthUser.model_validate(user)


@router.post("/{provider}/unbind", response_model=MemberAuthUser)
async def unbind_identity(
    provider: str, data: IdentityUnbindRequest, current_user: UserRecord = Depends(get_current_member)
) -> MemberAuthUser:
    user = await auth.unbind_identity_flow(provider, data, current_user, AuthAudience.MEMBER)
    return MemberAuthUser.model_validate(user)
