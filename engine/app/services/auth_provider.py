"""Generic OAuth/SSO authentication provider framework.

This module provides a base class for all identity providers (Feishu, DingTalk, WeCom, etc.)
and concrete implementations for each supported provider.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import ClassVar, override
from urllib.parse import quote, urlencode

import httpx

from app.core.json_types import JsonObject
from app.core.logging import logger
from app.dao import identity_dao, identity_provider_dao, user_dao
from app.records.identity import IdentityProviderRecord
from app.records.user import UserRecord
from app.services.google_workspace_oauth import GOOGLE_HTTP_PROXY

type AuthProviderPayloadValue = (
    str | int | float | bool | list[AuthProviderPayloadValue] | dict[str, AuthProviderPayloadValue] | None
)
type AuthProviderPayload = dict[str, AuthProviderPayloadValue]


@dataclass
class ExternalUserInfo:
    """Standardized user info from external identity providers."""

    provider_type: str
    provider_union_id: str | None = None
    provider_user_id: str | None = None
    name: str = ""
    email: str = ""
    avatar_url: str = ""
    mobile: str = ""
    raw_data: AuthProviderPayload = field(default_factory=dict)


class BaseAuthProvider(ABC):
    """Abstract base class for all authentication providers."""

    provider_type: str = ""

    def __init__(
        self,
        provider: IdentityProviderRecord | None = None,
        config: JsonObject | None = None,
    ):
        """Initialize provider with optional config from database.

        Args:
            provider: IdentityProvider record from database
            config: Configuration dict (fallback if no provider record)
        """
        self.provider = provider
        self.config: JsonObject = config or {}
        if provider and provider.config:
            self.config = provider.config

    def _config_string(self, *keys: str) -> str:
        for key in keys:
            value = self.config.get(key)
            if isinstance(value, str):
                return value
        return ""

    def _config_scopes(self, key: str, default: str) -> str | list[str]:
        value = self.config.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return [item for item in value if isinstance(item, str)]
        return default

    @staticmethod
    def _payload_string(payload: AuthProviderPayload, key: str) -> str:
        value = payload.get(key)
        return value if isinstance(value, str) else ""

    @abstractmethod
    async def get_authorization_url(self, redirect_uri: str, state: str) -> str:
        """Generate OAuth authorization URL.

        Args:
            redirect_uri: Callback URL after authorization
            state: CSRF state parameter

        Returns:
            Authorization URL to redirect user to
        """

    @abstractmethod
    async def exchange_code_for_token(self, code: str, redirect_uri: str | None = None) -> AuthProviderPayload:
        """Exchange authorization code for access token.

        Args:
            code: Authorization code from OAuth callback

        Returns:
            Dict containing access_token and optionally refresh_token
        """

    @abstractmethod
    async def get_user_info(self, access_token: str) -> ExternalUserInfo:
        """Fetch user profile from provider API.

        Args:
            access_token: Valid access token

        Returns:
            ExternalUserInfo instance with user data
        """

    async def find_or_create_user(
        self,
        user_info: ExternalUserInfo,
        tenant_id: str | None = None,
        db: object | None = None,
    ) -> tuple[UserRecord, bool]:
        """Find existing user or create new one via Identity/OrgMember.

        ``db`` is accepted for call-site compatibility and ignored.
        """
        del db
        from app.services.registration_service import registration_service
        from app.services.sso_service import sso_service

        await self._ensure_provider(tenant_id)

        provider_user_id = user_info.provider_user_id or user_info.provider_union_id
        if provider_user_id is None:
            raise ValueError(f"{self.provider_type} user info does not include an external identity")
        user = await sso_service.resolve_user_identity(
            provider_user_id,
            self.provider_type,
            tenant_id=tenant_id,
            identity_data=dict(user_info.raw_data) if user_info.raw_data else None,
        )

        is_new = False
        if not user:
            if user_info.email:
                user = await sso_service.match_user_by_email(user_info.email, tenant_id)
            if not user and user_info.mobile and tenant_id:
                user = await sso_service.match_user_by_mobile(user_info.mobile, tenant_id)

            if user and tenant_id and str(user.tenant_id) != tenant_id:
                user = None

        if user:
            if not user.identity_id:
                identity = await registration_service.find_or_create_identity(
                    email=user_info.email, phone=user_info.mobile
                )
                user = await user_dao.update(db_obj=user, obj_in={"identity_id": identity.id})
                user.identity = identity
            user = await self._update_existing_user(user, user_info)
        else:
            user = await self._create_new_user(user_info, tenant_id)
            is_new = True

        await sso_service.link_identity(
            str(user.id),
            self.provider_type,
            provider_user_id,
            dict(user_info.raw_data) if user_info.raw_data else None,
            tenant_id=tenant_id,
        )
        await registration_service.ensure_web_org_member(user)
        return user, is_new

    async def _ensure_provider(self, tenant_id: str | None = None) -> IdentityProviderRecord:
        """Get or create IdentityProvider record."""
        if self.provider:
            return self.provider

        provider = await identity_provider_dao.get_preferred(self.provider_type, tenant_id)
        if not provider:
            provider = await identity_provider_dao.create(
                obj_in={
                    "provider_type": self.provider_type,
                    "name": self.provider_type.capitalize(),
                    "is_active": True,
                    "config": self.config or {},
                    "tenant_id": tenant_id,
                }
            )
        self.provider = provider
        return provider

    async def _find_user_by_legacy_fields(self, user_info: ExternalUserInfo) -> UserRecord | None:
        """Find user by legacy provider-specific fields (if any)."""
        del user_info
        return None

    async def _update_existing_user(self, user: UserRecord, user_info: ExternalUserInfo) -> UserRecord:
        """Update existing user with new info from provider."""
        user_fields: dict[str, object] = {}
        identity_fields: dict[str, object] = {}
        if user_info.name and not user.display_name:
            user_fields["display_name"] = user_info.name
        if user_info.avatar_url and not user.avatar_url:
            user_fields["avatar_url"] = user_info.avatar_url
        if user_info.email and not user.email:
            identity_fields["email"] = user_info.email
        if user_info.mobile and not user.primary_mobile:
            identity_fields["phone"] = user_info.mobile

        if identity_fields and user.identity is not None:
            user.identity = await identity_dao.update(db_obj=user.identity, obj_in=identity_fields)
        if user_fields:
            user = await user_dao.update(db_obj=user, obj_in=user_fields)
            if user.identity is None:
                loaded = await user_dao.get_with_identity(user.id)
                if loaded:
                    user = loaded
        await self._update_legacy_user_fields(user, user_info)
        return user

    async def _create_new_user(self, user_info: ExternalUserInfo, tenant_id: str | None) -> UserRecord:
        """Create new user from external identity."""
        import uuid

        from app.services.registration_service import registration_service

        effective_id = user_info.provider_user_id or user_info.provider_union_id or "unknown"
        identity = await registration_service.find_or_create_identity(
            email=user_info.email,
            phone=user_info.mobile,
            username=user_info.email.split("@")[0] if user_info.email else None,
        )

        username = user_info.email.split("@")[0] if user_info.email else f"{self.provider_type}_{effective_id[:8]}"
        if await identity_dao.is_username_taken(username):
            username = f"{username}_{uuid.uuid4().hex[:6]}"

        user = await user_dao.create(
            obj_in={
                "identity_id": identity.id,
                "display_name": user_info.name or username,
                "avatar_url": user_info.avatar_url or None,
                "registration_source": self.provider_type,
                "tenant_id": tenant_id,
                "is_active": True,
                "role": "member",
            }
        )
        user.identity = identity
        await self._set_legacy_user_fields(user, user_info)
        return user

    async def _update_legacy_user_fields(self, user: UserRecord, user_info: ExternalUserInfo):
        """Override in subclass to update provider-specific legacy fields."""
        del user, user_info
        return

    async def _set_legacy_user_fields(self, user: UserRecord, user_info: ExternalUserInfo):
        """Override in subclass to set provider-specific legacy fields on new user."""
        del user, user_info
        return


class FeishuAuthProvider(BaseAuthProvider):
    """Feishu (Lark) OAuth provider implementation."""

    provider_type = "feishu"

    FEISHU_OAUTH_ENDPOINT = "https://open.feishu.cn/open-apis/authen/v1/oidc/access_token"
    FEISHU_USER_INFO_URL = "https://open.feishu.cn/open-apis/authen/v1/user_info"
    FEISHU_APP_ACCESS_ENDPOINT = "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal"

    def __init__(self, provider: IdentityProviderRecord | None = None, config: JsonObject | None = None):
        super().__init__(provider, config)
        self.app_id = self._config_string("app_id")
        self.app_secret = self._config_string("app_secret")
        self._app_access_token: str | None = None

    @override
    async def get_authorization_url(self, redirect_uri: str, state: str) -> str:
        app_id = self.app_id or ""
        base_url = "https://open.feishu.cn/open-apis/authen/v1/authorize"
        params = f"app_id={app_id}&redirect_uri={redirect_uri}&state={state}"
        return f"{base_url}?{params}"

    async def get_app_access_token(self) -> str:
        if self._app_access_token:
            return self._app_access_token

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                self.FEISHU_APP_ACCESS_ENDPOINT,
                json={"app_id": self.app_id, "app_secret": self.app_secret},
            )
            app_access_token = resp.json().get("app_access_token")
            if not isinstance(app_access_token, str):
                return ""
            self._app_access_token = app_access_token
            return app_access_token

    @override
    async def exchange_code_for_token(self, code: str, redirect_uri: str | None = None) -> AuthProviderPayload:
        app_token = await self.get_app_access_token()

        async with httpx.AsyncClient() as client:
            token_resp = await client.post(
                self.FEISHU_OAUTH_ENDPOINT,
                json={"grant_type": "authorization_code", "code": code},
                headers={"Authorization": f"Bearer {app_token}"},
            )
            token_data = token_resp.json()
            return token_data.get("data", {})

    @override
    async def get_user_info(self, access_token: str) -> ExternalUserInfo:
        async with httpx.AsyncClient() as client:
            info_resp = await client.get(self.FEISHU_USER_INFO_URL, headers={"Authorization": f"Bearer {access_token}"})
            info_data = info_resp.json().get("data", {})
            logger.info("Feishu user info received union_id={}", info_data.get("union_id"))

            return ExternalUserInfo(
                provider_type=self.provider_type,
                provider_union_id=info_data.get("union_id"),
                name=info_data.get("name", ""),
                email=info_data.get("email", ""),
                avatar_url=info_data.get("avatar_url", ""),
                mobile=info_data.get("mobile", ""),
                raw_data=info_data,
            )

    @override
    async def _find_user_by_legacy_fields(self, user_info: ExternalUserInfo) -> UserRecord | None:
        """Feishu legacy lookup removed (open_id/union_id no longer stored on User)."""
        return None

    @override
    async def _update_legacy_user_fields(self, user: UserRecord, user_info: ExternalUserInfo):
        """No-op: legacy Feishu fields removed from User."""
        return

    @override
    async def _set_legacy_user_fields(self, user: UserRecord, user_info: ExternalUserInfo):
        """No-op: legacy Feishu fields removed from User."""
        return


class DingTalkAuthProvider(BaseAuthProvider):
    """DingTalk OAuth provider implementation."""

    provider_type = "dingtalk"

    DINGTALK_OAUTH_ENDPOINT = "https://api.dingtalk.com/v1.0/oauth2/userAccessToken"
    DINGTALK_USER_INFO_URL = "https://api.dingtalk.com/v1.0/contact/users/me"

    def __init__(self, provider: IdentityProviderRecord | None = None, config: JsonObject | None = None):
        super().__init__(provider, config)
        self.app_key = self._config_string("app_key")
        self.app_secret = self._config_string("app_secret")
        self.corp_id = self._config_string("corp_id")

    @override
    async def get_authorization_url(self, redirect_uri: str, state: str) -> str:
        app_id = self.app_key or ""
        base_url = "https://login.dingtalk.com/oauth2/auth"
        # Contact.User.Read is required for GET /v1.0/contact/users/me (user info on callback)
        # contact.user.mobile requires the fieldMobile permission in DingTalk console
        # fieldEmail requires the fieldEmail permission in DingTalk console
        scope = "openid corpid Contact.User.Read fieldEmail contact.user.mobile"
        params = (
            f"client_id={app_id}&redirect_uri={quote(redirect_uri)}&"
            f"state={state}&response_type=code&scope={quote(scope)}&prompt=consent"
        )
        # corp_id is optional: restricts the login page to a specific enterprise.
        # If not configured, DingTalk shows a company picker (still works for SSO).
        if self.corp_id:
            params = f"corpId={self.corp_id}&" + params
        return f"{base_url}?{params}"

    @override
    async def exchange_code_for_token(self, code: str, redirect_uri: str | None = None) -> AuthProviderPayload:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                self.DINGTALK_OAUTH_ENDPOINT,
                json={
                    "clientId": self.app_key,
                    "clientSecret": self.app_secret,
                    "code": code,
                    "grantType": "authorization_code",
                },
            )
            resp_data = resp.json()
            if resp.status_code != 200:
                logger.error(f"DingTalk token exchange failed (HTTP {resp.status_code}): {resp_data}")
                return {}

            # New DingTalk OAuth2 returns flat JSON with camelCase fields
            return {
                "access_token": resp_data.get("accessToken"),
                "refresh_token": resp_data.get("refreshToken"),
                "expires_in": resp_data.get("expireIn"),
            }

    @override
    async def get_user_info(self, access_token: str) -> ExternalUserInfo:
        async with httpx.AsyncClient() as client:
            headers = {"x-acs-dingtalk-access-token": access_token}
            info_resp = await client.get(self.DINGTALK_USER_INFO_URL, headers=headers)
            info_data = info_resp.json()
            if info_resp.status_code != 200:
                # Common error: errCode=403 means Contact.User.Read scope not granted.
                # Ensure 'Contact.User.Read' is included in the OAuth scope AND
                # that the app has been authorized by the employee in the login flow.
                err_msg = info_data.get("message") or info_data.get("errmsg") or str(info_data)
                logger.error(
                    f"DingTalk user info fetch failed (HTTP {info_resp.status_code}): {info_data}. "
                    "This usually means the 'Contact.User.Read' OAuth scope is missing from "
                    "the authorization URL, or the app lacks the corresponding permission."
                )
                raise Exception(f"Failed to fetch user info: {err_msg}")

            # DingTalk new OAuth2 returns openId, unionId, nick, avatarUrl, mobile, email
            logger.info(f"DingTalk user info: {info_data}")
            return ExternalUserInfo(
                provider_type=self.provider_type,
                provider_union_id=info_data.get("unionId"),
                name=info_data.get("nick", ""),
                email=info_data.get("email", ""),
                avatar_url=info_data.get("avatarUrl", ""),
                mobile=info_data.get("mobile", ""),
                raw_data=info_data,
            )


class WeComAuthProvider(BaseAuthProvider):
    """WeCom (Enterprise WeChat) OAuth provider implementation.

    Authentication flow:
    1. gettoken (corp_id + secret) -> access_token
    2. auth/getuserinfo (access_token + OAuth code) -> userid + user_ticket
    3. auth/getuserdetail (access_token + user_ticket) -> avatar, email, mobile
    4. user/get (access_token + userid) -> name, position (non-sensitive fields)

    Note: Steps 3 and 4 require the calling server IP to be whitelisted in the
    WeCom self-built app settings. This is a one-time setup per tenant.
    (Contrast with getuserinfo in step 2, which only requires trusted domain,
    not IP whitelist.)
    """

    provider_type = "wecom"

    # All WeCom self-built app API calls go to qyapi.weixin.qq.com
    # The old api.weixin.qq.com endpoints are legacy WeCom Public Account APIs
    # and no longer work for self-built apps.
    WECOM_ACCESS_ENDPOINT = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
    WECOM_USER_INFO_URL = "https://qyapi.weixin.qq.com/cgi-bin/auth/getuserinfo"
    WECOM_USER_DETAIL_URL = "https://qyapi.weixin.qq.com/cgi-bin/auth/getuserdetail"
    WECOM_USER_GET_URL = "https://qyapi.weixin.qq.com/cgi-bin/user/get"

    def __init__(self, provider: IdentityProviderRecord | None = None, config: JsonObject | None = None):
        super().__init__(provider, config)
        # corp_id and agent_id are used for the OAuth redirect URL
        self.corp_id = self._config_string("corp_id", "app_id")
        # secret is the self-built app's AgentSecret (not the contact-sync secret)
        self.secret = self._config_string("secret", "app_secret")
        self.agent_id = self._config_string("agent_id")

    @override
    async def get_authorization_url(self, redirect_uri: str, state: str) -> str:
        """Construct the WeCom web-login SSO redirect URL.

        Uses the 'Scan QR Code to Login' flow (CorpPinCorp), which redirects users
        to authenticate with their WeCom account then returns them to redirect_uri
        with a code parameter.
        """
        base_url = "https://open.work.weixin.qq.com/wwlogin/sso/login"
        params = (
            f"loginType=CorpPinCorp"
            f"&appid={self.corp_id}"
            f"&agentid={self.agent_id}"
            f"&redirect_uri={quote(redirect_uri)}"
            f"&state={state}"
        )
        return f"{base_url}?{params}"

    @override
    async def exchange_code_for_token(self, code: str, redirect_uri: str | None = None) -> AuthProviderPayload:
        """Exchange OAuth code for a packed token string containing all user data.

        Three sequential API calls:
          1. gettoken -> access_token
          2. auth/getuserinfo (code) -> userid + user_ticket
          3a. auth/getuserdetail (user_ticket) -> avatar, email, mobile [sensitive]
          3b. user/get (userid) -> name, position [non-sensitive, best-effort]

        Returns a packed JSON dict disguised as the access_token field so
        the existing BaseAuthProvider interface (get_user_info) can consume it.
        """
        import json

        async with httpx.AsyncClient(timeout=10) as client:
            # Step 1: Get app-level access token using corp credentials
            token_resp = await client.get(
                self.WECOM_ACCESS_ENDPOINT,
                params={"corpid": self.corp_id, "corpsecret": self.secret},
            )
            token_data = token_resp.json()
            access_token = token_data.get("access_token")
            if not access_token:
                logger.error(f"[WeCom SSO] gettoken failed: {token_data}")
                return {}

            # Step 2: Exchange OAuth code for userid + user_ticket
            # auth/getuserinfo returns userid (lowercase 'u') for internal employees.
            # user_ticket is a temporary credential (valid ~1800s) representing
            # the employee's own OAuth authorization, required for sensitive fields.
            info_resp = await client.get(
                self.WECOM_USER_INFO_URL,
                params={"access_token": access_token, "code": code},
            )
            info_data = info_resp.json()
            # The key is lowercase 'userid' in the new auth endpoint (not 'UserId')
            userid = info_data.get("userid") or info_data.get("UserId", "")
            user_ticket = info_data.get("user_ticket", "")
            if not userid:
                logger.error(f"[WeCom SSO] getuserinfo missing userid: {info_data}")
                return {}

            # Step 3a: Fetch sensitive profile fields using user_ticket.
            # Since June 2022, new self-built apps cannot get avatar/email/mobile
            # from user/get directly. The user_ticket (from OAuth consent) unlocks them.
            # Returns: userid, gender, avatar, qr_code, mobile, email, biz_mail, address
            sensitive_data: AuthProviderPayload = {}
            if user_ticket:
                try:
                    detail_resp = await client.post(
                        self.WECOM_USER_DETAIL_URL,
                        params={"access_token": access_token},
                        json={"user_ticket": user_ticket},
                    )
                    detail_json = detail_resp.json()
                    if detail_json.get("errcode") == 0:
                        sensitive_data = detail_json
                        logger.info(f"[WeCom SSO] getuserdetail succeeded for {userid}")
                    else:
                        logger.warning(f"[WeCom SSO] getuserdetail failed: {detail_json}")
                except Exception as e:
                    logger.warning(f"[WeCom SSO] getuserdetail error: {e}")
            else:
                logger.info(
                    f"[WeCom SSO] No user_ticket for {userid}; "
                    "sensitive fields (avatar/email/mobile) will be unavailable. "
                    "Ensure the WeCom app has 'snsapi_privateinfo' scope."
                )

            # Step 3b: Fetch non-sensitive profile fields from user/get (name, position).
            # These fields are NOT restricted by the June 2022 policy and are available
            # via the standard app access token (IP whitelist required).
            basic_data: AuthProviderPayload = {}
            try:
                get_resp = await client.get(
                    self.WECOM_USER_GET_URL,
                    params={"access_token": access_token, "userid": userid},
                )
                get_json = get_resp.json()
                if get_json.get("errcode") == 0:
                    basic_data = get_json
                    logger.info(f"[WeCom SSO] user/get succeeded for {userid}")
                else:
                    logger.warning(f"[WeCom SSO] user/get failed: {get_json}")
            except Exception as e:
                logger.warning(f"[WeCom SSO] user/get error: {e}")

            # Pack all data for get_user_info() to consume
            packed_token = json.dumps(
                {
                    "userid": userid,
                    "sensitive": sensitive_data,  # from getuserdetail (avatar, email, mobile)
                    "basic": basic_data,  # from user/get (name, position)
                }
            )
            return {"access_token": packed_token}

    @override
    async def get_user_info(self, access_token: str) -> ExternalUserInfo:
        """Parse the packed token into a standardized ExternalUserInfo.

        Priority for each field:
          - email: sensitive_data (getuserdetail) > biz_mail > basic_data (user/get)
          - avatar: sensitive_data > basic_data
          - mobile: sensitive_data only (restricted post-2022 in user/get)
          - name: basic_data (non-sensitive, from user/get)
        """
        import json

        try:
            data = json.loads(access_token)
            userid = data.get("userid", "")
            sensitive = data.get("sensitive", {})
            basic = data.get("basic", {})

            # Name from user/get (non-sensitive, always available when IP is whitelisted)
            name = basic.get("name") or f"WeCom {userid}"

            # Email: prefer personal email from getuserdetail, fall back to biz_mail
            email = (
                sensitive.get("email") or sensitive.get("biz_mail") or basic.get("email") or basic.get("biz_mail") or ""
            )

            # Avatar from getuserdetail (restricted post-2022 in user/get)
            avatar_url = sensitive.get("avatar") or basic.get("avatar") or ""

            # Mobile only from getuserdetail (restricted post-2022 in user/get)
            mobile = sensitive.get("mobile") or ""

            # Merge raw_data so OrgMember has full context
            raw = {**basic, **sensitive, "userid": userid}

            return ExternalUserInfo(
                provider_type=self.provider_type,
                provider_user_id=userid,
                name=name,
                email=email,
                avatar_url=avatar_url,
                mobile=mobile,
                raw_data=raw,
            )
        except Exception as e:
            logger.error(f"[WeCom SSO] get_user_info parse error: {e}")
            return ExternalUserInfo(
                provider_type=self.provider_type,
                provider_user_id="",
                name="",
                raw_data={"error": str(e)},
            )


class GoogleWorkspaceAuthProvider(BaseAuthProvider):
    """Google Workspace OAuth provider implementation for SSO login."""

    provider_type = "google_workspace"

    GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
    GOOGLE_OAUTH_ENDPOINT = "https://oauth2.googleapis.com/token"
    GOOGLE_USER_INFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
    GOOGLE_SSO_SCOPE = "openid email profile"
    GOOGLE_ADMIN_SCOPES: ClassVar[tuple[str, ...]] = (
        "openid",
        "email",
        "profile",
        "https://www.googleapis.com/auth/admin.directory.user.readonly",
        "https://www.googleapis.com/auth/admin.directory.orgunit.readonly",
    )

    def __init__(self, provider: IdentityProviderRecord | None = None, config: JsonObject | None = None):
        super().__init__(provider, config)
        self.client_id = self._config_string("client_id", "sso_client_id", "app_id")
        self.client_secret = self._config_string("client_secret", "sso_client_secret", "app_secret")
        self.scope = self._config_scopes("sso_scope", self._config_string("scope") or self.GOOGLE_SSO_SCOPE)

    def _build_authorization_url(
        self,
        redirect_uri: str,
        state: str,
        *,
        scopes: str | list[str] | tuple[str, ...] | None = None,
        access_type: str = "online",
        prompt: str = "select_account",
    ) -> str:

        scope_value = scopes or self.scope
        if isinstance(scope_value, (list, tuple)):
            scope_value = " ".join(scope_value)

        self.config["redirect_uri"] = redirect_uri
        params = (
            f"client_id={quote(self.client_id or '')}"
            f"&redirect_uri={quote(redirect_uri)}"
            f"&response_type=code"
            f"&scope={quote(scope_value)}"
            f"&state={quote(state or '')}"
            f"&access_type={quote(access_type)}"
            f"&include_granted_scopes=true"
            f"&prompt={quote(prompt)}"
        )
        return f"{self.GOOGLE_AUTHORIZE_URL}?{params}"

    @override
    async def get_authorization_url(self, redirect_uri: str, state: str) -> str:
        return self._build_authorization_url(
            redirect_uri,
            state,
            scopes=self.scope,
            access_type="online",
            prompt="select_account",
        )

    async def get_admin_authorization_url(self, redirect_uri: str, state: str) -> str:
        return self._build_authorization_url(
            redirect_uri,
            state,
            scopes=self.GOOGLE_ADMIN_SCOPES,
            access_type="offline",
            prompt="consent",
        )

    @override
    async def exchange_code_for_token(self, code: str, redirect_uri: str | None = None) -> AuthProviderPayload:
        async with httpx.AsyncClient(timeout=15, proxy=GOOGLE_HTTP_PROXY) as client:
            resp = await client.post(
                self.GOOGLE_OAUTH_ENDPOINT,
                data={
                    "code": code,
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "grant_type": "authorization_code",
                    "redirect_uri": redirect_uri or self._config_string("redirect_uri"),
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            resp.raise_for_status()
            return resp.json()

    async def refresh_access_token(self, refresh_token: str) -> AuthProviderPayload:
        async with httpx.AsyncClient(timeout=15, proxy=GOOGLE_HTTP_PROXY) as client:
            resp = await client.post(
                self.GOOGLE_OAUTH_ENDPOINT,
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            resp.raise_for_status()
            return resp.json()

    async def fetch_openid_profile(self, access_token: str) -> AuthProviderPayload:
        async with httpx.AsyncClient(timeout=15, proxy=GOOGLE_HTTP_PROXY) as client:
            resp = await client.get(
                self.GOOGLE_USER_INFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            resp.raise_for_status()
            return resp.json()

    @override
    async def get_user_info(self, access_token: str) -> ExternalUserInfo:
        info = await self.fetch_openid_profile(access_token)
        return ExternalUserInfo(
            provider_type=self.provider_type,
            provider_user_id=self._payload_string(info, "sub"),
            name=self._payload_string(info, "name") or self._payload_string(info, "email"),
            email=self._payload_string(info, "email"),
            avatar_url=self._payload_string(info, "picture"),
            raw_data=info,
        )


class MicrosoftTeamsAuthProvider(BaseAuthProvider):
    """Microsoft Teams OAuth provider implementation."""

    provider_type = "microsoft_teams"

    # Will be implemented when needed
    @override
    async def get_authorization_url(self, redirect_uri: str, state: str) -> str:
        raise NotImplementedError("Microsoft Teams OAuth not yet implemented")

    @override
    async def exchange_code_for_token(self, code: str, redirect_uri: str | None = None) -> AuthProviderPayload:
        raise NotImplementedError("Microsoft Teams OAuth not yet implemented")

    @override
    async def get_user_info(self, access_token: str) -> ExternalUserInfo:
        raise NotImplementedError("Microsoft Teams OAuth not yet implemented")


class GoogleAuthProvider(BaseAuthProvider):
    """Google OAuth provider implementation."""

    provider_type = "google"

    GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
    GOOGLE_OAUTH_ENDPOINT = "https://oauth2.googleapis.com/token"
    GOOGLE_USER_INFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"

    def __init__(self, provider: IdentityProviderRecord | None = None, config: JsonObject | None = None):
        super().__init__(provider, config)
        self.client_id = self._config_string("client_id", "app_id")
        self.client_secret = self._config_string("client_secret", "app_secret")
        self.scope = self._config_string("scope") or "openid profile email"

    @override
    async def get_authorization_url(self, redirect_uri: str, state: str) -> str:
        params = {
            "client_id": self.client_id or "",
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": self.scope,
            "access_type": "offline",
            "prompt": "consent",
        }
        if state:
            params["state"] = state
        return f"{self.GOOGLE_AUTHORIZE_URL}?{urlencode(params)}"

    @override
    async def exchange_code_for_token(self, code: str, redirect_uri: str | None = None) -> AuthProviderPayload:
        async with httpx.AsyncClient(timeout=15, proxy=GOOGLE_HTTP_PROXY) as client:
            resp = await client.post(
                self.GOOGLE_OAUTH_ENDPOINT,
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "code": code,
                    "redirect_uri": redirect_uri or "",
                    "grant_type": "authorization_code",
                },
            )
            data = resp.json()
            if resp.status_code != 200:
                logger.error(f"Google token exchange failed (HTTP {resp.status_code}): {data}")
                return {}
            return data

    @override
    async def get_user_info(self, access_token: str) -> ExternalUserInfo:
        async with httpx.AsyncClient(timeout=15, proxy=GOOGLE_HTTP_PROXY) as client:
            resp = await client.get(
                self.GOOGLE_USER_INFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            data = resp.json()
            if resp.status_code != 200:
                raise Exception(
                    data.get("error_description") or data.get("error") or "Failed to fetch Google user info"
                )

            return ExternalUserInfo(
                provider_type=self.provider_type,
                provider_user_id=data.get("sub", ""),
                name=data.get("name", ""),
                email=data.get("email", ""),
                avatar_url=data.get("picture", ""),
                raw_data=data,
            )


class GitHubAuthProvider(BaseAuthProvider):
    """GitHub OAuth provider implementation."""

    provider_type = "github"

    GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
    GITHUB_OAUTH_ENDPOINT = "https://github.com/login/oauth/access_token"
    GITHUB_USER_INFO_URL = "https://api.github.com/user"
    GITHUB_EMAILS_URL = "https://api.github.com/user/emails"

    def __init__(self, provider: IdentityProviderRecord | None = None, config: JsonObject | None = None):
        super().__init__(provider, config)
        self.client_id = self._config_string("client_id", "app_id")
        self.client_secret = self._config_string("client_secret", "app_secret")
        self.scope = self._config_string("scope") or "read:user user:email"

    @override
    async def get_authorization_url(self, redirect_uri: str, state: str) -> str:
        params = {
            "client_id": self.client_id or "",
            "redirect_uri": redirect_uri,
            "scope": self.scope,
        }
        if state:
            params["state"] = state
        return f"{self.GITHUB_AUTHORIZE_URL}?{urlencode(params)}"

    @override
    async def exchange_code_for_token(self, code: str, redirect_uri: str | None = None) -> AuthProviderPayload:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                self.GITHUB_OAUTH_ENDPOINT,
                headers={"Accept": "application/json"},
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "code": code,
                },
            )
            data = resp.json()
            if resp.status_code != 200:
                logger.error(f"GitHub token exchange failed (HTTP {resp.status_code}): {data}")
                return {}
            return data

    @override
    async def get_user_info(self, access_token: str) -> ExternalUserInfo:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/vnd.github+json",
        }
        async with httpx.AsyncClient(timeout=15) as client:
            user_resp = await client.get(self.GITHUB_USER_INFO_URL, headers=headers)
            user_data = user_resp.json()
            if user_resp.status_code != 200:
                raise Exception(user_data.get("message") or "Failed to fetch GitHub user info")

            email = user_data.get("email") or ""
            if not email:
                emails_resp = await client.get(self.GITHUB_EMAILS_URL, headers=headers)
                emails_data = emails_resp.json()
                if emails_resp.status_code == 200 and isinstance(emails_data, list):
                    primary = next((item for item in emails_data if item.get("primary")), None)
                    verified = next((item for item in emails_data if item.get("verified")), None)
                    fallback = primary or verified or (emails_data[0] if emails_data else {})
                    email = fallback.get("email", "")

            return ExternalUserInfo(
                provider_type=self.provider_type,
                provider_user_id=str(user_data.get("id", "")),
                name=user_data.get("name") or user_data.get("login") or "",
                email=email,
                avatar_url=user_data.get("avatar_url", ""),
                raw_data=user_data,
            )


# Provider class mapping
PROVIDER_CLASSES = {
    "feishu": FeishuAuthProvider,
    "dingtalk": DingTalkAuthProvider,
    "wecom": WeComAuthProvider,
    "google_workspace": GoogleWorkspaceAuthProvider,
    "microsoft_teams": MicrosoftTeamsAuthProvider,
    "google": GoogleAuthProvider,
    "github": GitHubAuthProvider,
}
