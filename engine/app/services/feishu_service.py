"""Feishu (Lark) OAuth and API integration service."""

from __future__ import annotations

import asyncio
import json
from collections import OrderedDict
from pathlib import Path
from typing import ClassVar, NotRequired, Protocol, TypedDict, TypeIs
from uuid import UUID

import httpx

from app.core.logging import logger

try:
    import lark_oapi

    _HAS_LARK = True
except ImportError:
    lark_oapi = None
    _HAS_LARK = False

from app.config import get_settings
from app.core.json_types import (
    JsonObject,
    json_as_str,
    json_as_str_or,
    json_object_from_response,
    json_value_from_response,
    mapping_from_row,
    object_list_from_row,
)
from app.core.security import create_access_token
from app.dao import identity_dao, identity_provider_dao, org_member_dao, user_dao
from app.records.user import UserRecord


def _is_json_object(value: object) -> TypeIs[JsonObject]:
    return isinstance(value, dict)


def _json_object(value: object) -> JsonObject:
    return value if _is_json_object(value) else mapping_from_row(value)


def _response_json_object(resp: httpx.Response) -> JsonObject:
    return json_object_from_response(resp)


def _app_access_token(resp: httpx.Response) -> str:
    return json_as_str_or(_response_json_object(resp).get("app_access_token"), "")


def _json_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _first_user_id(data: JsonObject) -> str | None:
    for user in object_list_from_row(_json_object(data.get("data")).get("user_list")):
        uid = json_as_str(_json_object(user).get("user_id"))
        if uid:
            return uid
    return None


class _LarkApiResponse(Protocol):
    code: object
    msg: object
    data: object

    def success(self) -> bool: ...


class _LarkCardResource(Protocol):
    async def acreate(self, request: object) -> _LarkApiResponse: ...

    async def asettings(self, request: object) -> _LarkApiResponse: ...

    async def aupdate(self, request: object) -> _LarkApiResponse: ...


class _LarkCardElementResource(Protocol):
    async def acontent(self, request: object) -> _LarkApiResponse: ...


class _LarkCardkitV1(Protocol):
    card: _LarkCardResource
    card_element: _LarkCardElementResource


class _LarkCardkitService(Protocol):
    v1: _LarkCardkitV1


class LarkClient(Protocol):
    cardkit: _LarkCardkitService | None


class _LarkClientBuilder(Protocol):
    def app_id(self, app_id: str) -> _LarkClientBuilder: ...

    def app_secret(self, app_secret: str) -> _LarkClientBuilder: ...

    def build(self) -> object: ...


def _is_lark_client(value: object) -> TypeIs[LarkClient]:
    return hasattr(value, "cardkit")


def _is_lark_builder(value: object) -> TypeIs[_LarkClientBuilder]:
    return callable(getattr(value, "app_id", None)) and callable(getattr(value, "build", None))


def _require_lark_client_cls() -> type[object]:
    if not _HAS_LARK or lark_oapi is None:
        raise RuntimeError("lark-oapi package is not installed. Install with: pip install lark-oapi")
    client_cls: object = getattr(lark_oapi, "Client", None)
    if not isinstance(client_cls, type):
        raise RuntimeError("lark-oapi package is missing Client")
    return client_cls


settings = get_settings()

FEISHU_OAUTH_ENDPOINT = "https://open.feishu.cn/open-apis/authen/v1/oidc/access_token"
FEISHU_USER_INFO_URL = "https://open.feishu.cn/open-apis/authen/v1/user_info"
FEISHU_APP_ACCESS_ENDPOINT = "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal"
FEISHU_SEND_MSG_URL = "https://open.feishu.cn/open-apis/im/v1/messages"


class FeishuOAuthUser(TypedDict):
    open_id: str
    union_id: str
    user_id: str
    name: str
    email: str
    avatar_url: str
    mobile: NotRequired[str]


class FeishuAPIError(RuntimeError):
    """Structured Feishu API error that preserves provider-returned details."""

    def __init__(
        self,
        *,
        stage: str,
        http_status: int | None = None,
        code: int | None = None,
        msg: str = "",
        log_id: str | None = None,
        troubleshooter: str | None = None,
        message_id: str | None = None,
    ):
        self.stage: str = stage
        self.http_status: int | None = http_status
        self.code: int | None = code
        self.msg: str = msg or "Unknown Feishu error"
        self.log_id: str | None = log_id
        self.troubleshooter: str | None = troubleshooter
        self.message_id: str | None = message_id

        parts = [f"Feishu {stage} failed"]
        if self.http_status is not None:
            parts.append(f"HTTP {self.http_status}")
        if self.code is not None:
            parts.append(f"code={self.code}")
        parts.append(f"msg={self.msg}")
        if self.log_id:
            parts.append(f"log_id={self.log_id}")
        if self.troubleshooter:
            parts.append(f"troubleshooter={self.troubleshooter}")
        super().__init__(", ".join(parts))

    @property
    def user_message(self) -> str:
        base = self.msg
        if self.code is not None:
            base = f"{base} (code {self.code})"
        if self.troubleshooter:
            return f"{base}\n{self.troubleshooter}"
        return base


class FeishuService:
    """Service for Feishu OAuth login and message API."""

    # Maximum number of lark SDK client instances to keep alive simultaneously.
    # Each entry corresponds to a unique (app_id, app_secret) pair.  Excess entries
    # are evicted in LRU order (oldest-accessed first) to bound memory usage in
    # long-running multi-tenant deployments.
    _LARK_CLIENT_CACHE_MAX: ClassVar[int] = 50

    def __init__(self) -> None:
        self.app_id: str = settings.FEISHU_APP_ID
        self.app_secret: str = settings.FEISHU_APP_SECRET
        self._app_access_token: str | None = None
        # OrderedDict is used as a simple LRU cache: move_to_end() on each hit
        # keeps the most-recently-used entries at the tail so we can evict from
        # the head when the cache is full.
        self._lark_clients: OrderedDict[str, LarkClient] = OrderedDict()

    @staticmethod
    def _parse_api_response(
        resp: httpx.Response,
        *,
        stage: str,
        message_id: str | None = None,
    ) -> JsonObject:
        """Parse Feishu API response and verify both HTTP status and business code."""
        try:
            raw = json_value_from_response(resp)
        except Exception as e:
            logger.warning(
                f"[Feishu] {stage} returned non-JSON response "
                + f"(http_status={resp.status_code}, message_id={message_id}): {e}"
            )
            raise RuntimeError(f"Feishu {stage} returned invalid JSON") from e

        if not isinstance(raw, dict):
            raise RuntimeError(f"Feishu {stage} returned a non-object JSON payload")
        data_obj = _json_object(raw)
        error_info = _json_object(data_obj.get("error"))
        log_id = json_as_str(error_info.get("log_id"))
        troubleshooter = json_as_str(error_info.get("troubleshooter"))

        if resp.status_code >= 400:
            logger.warning(
                f"[Feishu] {stage} HTTP failure "
                + f"(http_status={resp.status_code}, message_id={message_id}, body={str(raw)[:300]})"
            )

        code: object = data_obj.get("code")
        msg = json_as_str_or(data_obj.get("msg"), "")
        if code is not None and code != 0:
            logger.warning(f"[Feishu] {stage} business failure (message_id={message_id}, code={code}, msg={msg})")
            raise FeishuAPIError(
                stage=stage,
                http_status=resp.status_code,
                code=_json_int(code),
                msg=msg,
                log_id=log_id,
                troubleshooter=troubleshooter,
                message_id=message_id,
            )

        return data_obj

    async def get_app_access_token(self) -> str:
        """Get or refresh the app-level access token. Deprecated: Use get_tenant_access_token instead."""
        return await self.get_tenant_access_token(self.app_id, self.app_secret)

    async def get_tenant_access_token(self, app_id: str | None = None, app_secret: str | None = None) -> str:
        """Get or refresh the app-level access token (tenant_access_token)."""
        target_app_id = app_id or self.app_id
        target_app_secret = app_secret or self.app_secret

        from app.services.im_token_cache import get_cached_im_token, refresh_ttl, set_cached_im_token

        if target_app_id:
            cached = await get_cached_im_token("feishu", target_app_id, secret=target_app_secret or "")
            if cached:
                if not app_id:
                    self._app_access_token = cached
                return cached

        async with httpx.AsyncClient() as client:
            typed_client: httpx.AsyncClient = client
            resp: httpx.Response = await typed_client.post(
                FEISHU_APP_ACCESS_ENDPOINT,
                json={
                    "app_id": target_app_id,
                    "app_secret": target_app_secret,
                },
            )
            data = _response_json_object(resp)

            token = json_as_str_or(data.get("tenant_access_token") or data.get("app_access_token"), "")
            if not app_id:  # only cache default app token
                self._app_access_token = token
            if token and target_app_id:
                await set_cached_im_token(
                    "feishu",
                    target_app_id,
                    token,
                    secret=target_app_secret or "",
                    ttl=refresh_ttl(_json_int(data.get("expire"))),
                )
            elif target_app_id:
                from app.services.im_token_cache import drop_cached_im_token

                await drop_cached_im_token("feishu", target_app_id, secret=target_app_secret or "")

            return token

    async def exchange_code_for_user(self, code: str) -> FeishuOAuthUser:
        """Exchange OAuth authorization code for user info.

        Returns dict with: open_id, union_id, user_id, name, email, avatar_url
        """
        app_token = await self.get_app_access_token()

        async with httpx.AsyncClient() as client:
            typed_client: httpx.AsyncClient = client
            # Get user access token
            token_resp: httpx.Response = await typed_client.post(
                FEISHU_OAUTH_ENDPOINT,
                json={
                    "grant_type": "authorization_code",
                    "code": code,
                },
                headers={"Authorization": f"Bearer {app_token}"},
            )
            token_data = _json_object(_response_json_object(token_resp).get("data"))
            user_access_token = json_as_str_or(token_data.get("access_token"), "")

            # Get user info
            info_resp: httpx.Response = await typed_client.get(
                FEISHU_USER_INFO_URL,
                headers={
                    "Authorization": f"Bearer {user_access_token}",
                },
            )
            info_data = _json_object(_response_json_object(info_resp).get("data"))

            return {
                "open_id": json_as_str_or(info_data.get("open_id"), ""),
                "union_id": json_as_str_or(info_data.get("union_id"), ""),
                "user_id": json_as_str_or(info_data.get("user_id"), ""),
                "name": json_as_str_or(info_data.get("name"), ""),
                "email": json_as_str_or(info_data.get("email"), ""),
                "avatar_url": json_as_str_or(info_data.get("avatar_url"), ""),
            }

    async def login_or_register(
        self, db: object | None, feishu_user: FeishuOAuthUser, tenant_id: str | None = None
    ) -> tuple[UserRecord, str]:
        """Login existing user or register new one via Feishu SSO.

        Uses OrgMember as the identity anchor (synced from Feishu org directory).
        Returns (user, jwt_token). ``db`` is accepted for call-site compatibility.
        """
        del db
        open_id = feishu_user["open_id"]
        user_id = feishu_user.get("user_id", "")
        fs_email = feishu_user.get("email", "")
        fs_name = feishu_user.get("name", "")
        fs_avatar = feishu_user.get("avatar_url", "")
        tenant_uuid: UUID | None = None
        if tenant_id:
            try:
                tenant_uuid = UUID(str(tenant_id))
            except ValueError, TypeError:
                tenant_uuid = None

        provider = await identity_provider_dao.get_by_type_and_tenant("feishu", tenant_uuid)
        if not provider:
            provider = await identity_provider_dao.create(
                obj_in={
                    "provider_type": "feishu",
                    "name": "Feishu",
                    "is_active": True,
                    "config": {"app_id": self.app_id, "app_secret": self.app_secret},
                    "tenant_id": tenant_uuid,
                }
            )

        member = None
        if open_id:
            member = await org_member_dao.find_active_by_any_ids(
                provider_id=provider.id,
                open_id=open_id,
            )
        if not member and user_id:
            member = await org_member_dao.find_active_by_any_ids(
                provider_id=provider.id,
                external_id=user_id,
            )

        user: UserRecord | None = None
        if member and member.user_id:
            user = await user_dao.get_with_identity(member.user_id)

        if not user and fs_email:
            user = await user_dao.get_by_email_and_tenant(fs_email, tenant_uuid)
            if user:
                user = await user_dao.get_with_identity(user.id)

        if user:
            updates: dict[str, str] = {}
            if fs_avatar:
                updates["avatar_url"] = fs_avatar
            if fs_name:
                updates["display_name"] = fs_name
            if updates:
                user = await user_dao.update(db_obj=user, obj_in=updates)
            if user.identity_id and fs_email and (not user.email or str(user.email).endswith("@feishu.local")):
                identity = await identity_dao.get(user.identity_id)
                if identity:
                    _ = await identity_dao.update(db_obj=identity, obj_in={"email": fs_email})
                    user = await user_dao.get_with_identity(user.id) or user
            if member and not member.user_id:
                _ = await org_member_dao.update(db_obj=member, obj_in={"user_id": user.id})
        else:
            username = fs_email.split("@")[0] if fs_email else f"feishu_{open_id[:8]}"
            email = fs_email or f"{username}@feishu.local"

            existing = await user_dao.get_by_identity_username(username)
            if existing and (tenant_uuid is None or existing.tenant_id == tenant_uuid):
                import uuid as _uuid

                username = f"{username}_{_uuid.uuid4().hex[:6]}"

            from app.services.registration_service import registration_service

            identity = await registration_service.find_or_create_identity(
                email=email,
                phone=feishu_user.get("mobile"),
                username=username,
                password=open_id,
            )

            user = await user_dao.create(
                obj_in={
                    "identity_id": identity.id,
                    "display_name": fs_name or username,
                    "avatar_url": fs_avatar or None,
                    "registration_source": "feishu",
                    "tenant_id": tenant_uuid,
                    "is_active": True,
                }
            )
            user = await user_dao.get_with_identity(user.id) or user
            if member:
                _ = await org_member_dao.update(db_obj=member, obj_in={"user_id": user.id})

        token = create_access_token(str(user.id), user.role)
        return user, token

    async def send_message(
        self,
        app_id: str | None,
        app_secret: str | None,
        receive_id: str,
        msg_type: str,
        content: str,
        receive_id_type: str = "open_id",
        stage: str = "send_message",
    ) -> JsonObject:
        """Send a message via a specific Feishu bot (per-agent credentials).

        Args:
            app_id: The Feishu app's App ID (per-agent)
            app_secret: The Feishu app's App Secret (per-agent)
            receive_id: Target user's open_id
            msg_type: "text", "interactive", etc.
            content: JSON string of message content
            receive_id_type: "open_id" or "chat_id"
        """
        # Get app access token for this specific agent's bot
        async with httpx.AsyncClient() as client:
            typed_client: httpx.AsyncClient = client
            token_resp: httpx.Response = await typed_client.post(
                FEISHU_APP_ACCESS_ENDPOINT,
                json={
                    "app_id": app_id,
                    "app_secret": app_secret,
                },
            )
            app_token = _app_access_token(token_resp)

            resp: httpx.Response = await typed_client.post(
                f"{FEISHU_SEND_MSG_URL}?receive_id_type={receive_id_type}",
                json={
                    "receive_id": receive_id,
                    "msg_type": msg_type,
                    "content": content,
                },
                headers={"Authorization": f"Bearer {app_token}"},
            )
            return self._parse_api_response(resp, stage=stage)

    async def patch_message(
        self,
        app_id: str,
        app_secret: str,
        message_id: str,
        content: str,
        stage: str = "patch_message",
    ) -> JsonObject:
        """Patch an existing message (e.g. updating an interactive card for streaming)."""
        async with httpx.AsyncClient() as client:
            typed_client: httpx.AsyncClient = client
            token_resp: httpx.Response = await typed_client.post(
                FEISHU_APP_ACCESS_ENDPOINT,
                json={
                    "app_id": app_id,
                    "app_secret": app_secret,
                },
            )
            app_token = _app_access_token(token_resp)

            resp: httpx.Response = await typed_client.patch(
                f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}",
                json={
                    "content": content,
                },
                headers={"Authorization": f"Bearer {app_token}"},
            )
            return self._parse_api_response(resp, stage=stage, message_id=message_id)

    async def resolve_open_id(
        self, app_id: str, app_secret: str, email: str | None = None, mobile: str | None = None
    ) -> str | None:
        """Resolve a user's open_id for a specific app using email or mobile.

        Each Feishu app gets a unique open_id per user. This method looks up the
        correct open_id for the given app's credentials.
        """
        if not email and not mobile:
            return None

        async with httpx.AsyncClient() as client:
            typed_client: httpx.AsyncClient = client
            token_resp: httpx.Response = await typed_client.post(
                FEISHU_APP_ACCESS_ENDPOINT,
                json={
                    "app_id": app_id,
                    "app_secret": app_secret,
                },
            )
            app_token = _app_access_token(token_resp)

            body: JsonObject = {}
            if email:
                body["emails"] = [email]
            if mobile:
                body["mobiles"] = [mobile]

            resp: httpx.Response = await typed_client.post(
                "https://open.feishu.cn/open-apis/contact/v3/users/batch_get_id",
                json=body,
                headers={"Authorization": f"Bearer {app_token}"},
                params={"user_id_type": "open_id"},
            )
            data = _response_json_object(resp)
            if data.get("code") != 0:
                return None
            return _first_user_id(data)

    async def resolve_user_id(
        self, app_id: str, app_secret: str, email: str | None = None, mobile: str | None = None
    ) -> str | None:
        """Resolve a user's tenant-level user_id using email or mobile.

        Unlike open_id, user_id is stable across all apps within the same tenant.
        Requires contact:user.employee_id:readonly permission.
        """
        if not email and not mobile:
            return None

        async with httpx.AsyncClient() as client:
            typed_client: httpx.AsyncClient = client
            token_resp: httpx.Response = await typed_client.post(
                FEISHU_APP_ACCESS_ENDPOINT,
                json={
                    "app_id": app_id,
                    "app_secret": app_secret,
                },
            )
            app_token = _app_access_token(token_resp)

            body: JsonObject = {}
            if email:
                body["emails"] = [email]
            if mobile:
                body["mobiles"] = [mobile]

            resp: httpx.Response = await typed_client.post(
                "https://open.feishu.cn/open-apis/contact/v3/users/batch_get_id",
                json=body,
                headers={"Authorization": f"Bearer {app_token}"},
                params={"user_id_type": "user_id"},
            )
            data = _response_json_object(resp)
            if data.get("code") != 0:
                return None
            return _first_user_id(data)

    async def send_approval_card(
        self,
        app_id: str,
        app_secret: str,
        creator_open_id: str,
        agent_name: str,
        action_type: str,
        details: str,
        approval_id: str,
    ) -> JsonObject:
        """Send an interactive approval card to the agent creator via Feishu."""
        import json

        # Simplified - in production, use Feishu interactive card JSON
        text_content = json.dumps(
            {"text": f"🔴 [{agent_name}] 请求审批\n操作: {action_type}\n详情: {details}\n\n请在 MaraClaw 平台审批。"}
        )
        return await self.send_message(app_id, app_secret, creator_open_id, "text", text_content)

    async def download_message_resource(
        self, app_id: str, app_secret: str, message_id: str, file_key: str, resource_type: str = "file"
    ) -> bytes:
        """Download a file or image from a Feishu message.

        Args:
            resource_type: "file" or "image"
        Returns raw file bytes.
        """
        async with httpx.AsyncClient(timeout=30) as client:
            typed_client: httpx.AsyncClient = client
            token_resp: httpx.Response = await typed_client.post(
                FEISHU_APP_ACCESS_ENDPOINT,
                json={
                    "app_id": app_id,
                    "app_secret": app_secret,
                },
            )
            app_token = _app_access_token(token_resp)
            resp: httpx.Response = await typed_client.get(
                f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/resources/{file_key}",
                params={"type": resource_type},
                headers={"Authorization": f"Bearer {app_token}"},
            )
            _ = resp.raise_for_status()
            return resp.content

    async def upload_and_send_file(
        self,
        app_id: str | None,
        app_secret: str | None,
        receive_id: str,
        file_path: str | Path,
        receive_id_type: str = "open_id",
        accompany_msg: str = "",
    ) -> JsonObject:
        """Upload a local file to Feishu and send it as a file message.

        Returns the send_message response dict.
        """
        import json as _json

        fp = Path(file_path)
        async with httpx.AsyncClient(timeout=60) as client:
            typed_client: httpx.AsyncClient = client
            # Get token
            token_resp: httpx.Response = await typed_client.post(
                FEISHU_APP_ACCESS_ENDPOINT,
                json={
                    "app_id": app_id,
                    "app_secret": app_secret,
                },
            )
            app_token = _app_access_token(token_resp)
            headers = {"Authorization": f"Bearer {app_token}"}

            # Upload file
            file_bytes = await asyncio.to_thread(fp.read_bytes)
            # Determine file type for Feishu upload
            ext = fp.suffix.lower()
            feishu_file_type = "stream"  # generic binary
            if ext in (".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt", ".txt", ".md"):
                feishu_file_type = "stream"
            upload_resp: httpx.Response = await typed_client.post(
                "https://open.feishu.cn/open-apis/im/v1/files",
                files={"file": (fp.name, file_bytes, "application/octet-stream")},
                data={"file_type": feishu_file_type, "file_name": fp.name},
                headers=headers,
            )
            upload_data = _response_json_object(upload_resp)
            if upload_data.get("code") != 0:
                raise RuntimeError(f"Feishu file upload failed: {upload_data.get('msg')}")
            file_key = _json_object(upload_data["data"])["file_key"]

            # Send text accompany message first if provided
            if accompany_msg:
                text_resp: httpx.Response = await typed_client.post(
                    f"{FEISHU_SEND_MSG_URL}?receive_id_type={receive_id_type}",
                    json={
                        "receive_id": receive_id,
                        "msg_type": "text",
                        "content": _json.dumps({"text": accompany_msg}),
                    },
                    headers=headers,
                )
                if text_resp.status_code != 200:
                    logger.error(
                        "[Feishu] Failed to send text accompany message: "
                        + f"status={text_resp.status_code}, body={text_resp.text}, "
                        + f"receive_id={receive_id}, receive_id_type={receive_id_type}"
                    )

            # Send file message
            resp: httpx.Response = await typed_client.post(
                f"{FEISHU_SEND_MSG_URL}?receive_id_type={receive_id_type}",
                json={"receive_id": receive_id, "msg_type": "file", "content": _json.dumps({"file_key": file_key})},
                headers=headers,
            )
            if resp.status_code != 200:
                logger.error(
                    "[Feishu] Failed to send file message: "
                    + f"status={resp.status_code}, body={resp.text}, "
                    + f"receive_id={receive_id}, receive_id_type={receive_id_type}, "
                    + f"file_key={file_key}"
                )
            return _response_json_object(resp)

    # --- Bitable API ---

    async def bitable_list_tables(self, app_id: str, app_secret: str, app_token: str) -> JsonObject:
        """List all tables in a Bitable app."""
        tenant_token = await self.get_tenant_access_token(app_id, app_secret)
        async with httpx.AsyncClient(timeout=15) as client:
            resp: httpx.Response = await client.get(
                f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables",
                headers={"Authorization": f"Bearer {tenant_token}"},
            )
            return _response_json_object(resp)

    async def bitable_list_fields(self, app_id: str, app_secret: str, app_token: str, table_id: str) -> JsonObject:
        """List all fields in a specific table."""
        tenant_token = await self.get_tenant_access_token(app_id, app_secret)
        async with httpx.AsyncClient(timeout=15) as client:
            resp: httpx.Response = await client.get(
                f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields",
                headers={"Authorization": f"Bearer {tenant_token}"},
            )
            return _response_json_object(resp)

    async def bitable_query_records(
        self, app_id: str, app_secret: str, app_token: str, table_id: str, filters: JsonObject | None = None
    ) -> JsonObject:
        """Query records in a specific table."""
        tenant_token = await self.get_tenant_access_token(app_id, app_secret)
        body: JsonObject = {}
        if filters:
            body = filters
        async with httpx.AsyncClient(timeout=30) as client:
            resp: httpx.Response = await client.post(
                f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/search",
                json=body,
                headers={"Authorization": f"Bearer {tenant_token}"},
            )
            return _response_json_object(resp)

    async def bitable_create_record(
        self, app_id: str, app_secret: str, app_token: str, table_id: str, fields: JsonObject
    ) -> JsonObject:
        """Create a new record in a specific table."""
        tenant_token = await self.get_tenant_access_token(app_id, app_secret)
        async with httpx.AsyncClient(timeout=15) as client:
            resp: httpx.Response = await client.post(
                f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records",
                json={"fields": fields},
                headers={"Authorization": f"Bearer {tenant_token}"},
            )
            return _response_json_object(resp)

    async def bitable_update_record(
        self, app_id: str, app_secret: str, app_token: str, table_id: str, record_id: str, fields: JsonObject
    ) -> JsonObject:
        """Update an existing record in a specific table."""
        tenant_token = await self.get_tenant_access_token(app_id, app_secret)
        async with httpx.AsyncClient(timeout=15) as client:
            resp: httpx.Response = await client.put(
                f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}",
                json={"fields": fields},
                headers={"Authorization": f"Bearer {tenant_token}"},
            )
            return _response_json_object(resp)

    async def bitable_delete_record(
        self, app_id: str, app_secret: str, app_token: str, table_id: str, record_id: str
    ) -> JsonObject:
        """Delete an existing record in a specific table."""
        tenant_token = await self.get_tenant_access_token(app_id, app_secret)
        async with httpx.AsyncClient(timeout=15) as client:
            resp: httpx.Response = await client.delete(
                f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}",
                headers={"Authorization": f"Bearer {tenant_token}"},
            )
            return _response_json_object(resp)

    async def bitable_create_app(self, app_id: str, app_secret: str, name: str, folder_token: str = "") -> JsonObject:
        """Create a new Bitable app.

        Uses the Bitable v1 apps API: POST /open-apis/bitable/v1/apps
        If folder_token is empty, the file is created in the root 'My Drive'.

        Args:
            name:         The display name of the new Bitable (max 255 chars).
            folder_token: Parent folder token (optional). Leave empty for root.
        Returns:
            API response dict containing 'data.app.app_token' as the new app_token.
        """
        tenant_token = await self.get_tenant_access_token(app_id, app_secret)
        body: JsonObject = {"name": name}
        if folder_token:
            body["folder_token"] = folder_token
        async with httpx.AsyncClient(timeout=15) as client:
            resp: httpx.Response = await client.post(
                "https://open.feishu.cn/open-apis/bitable/v1/apps",
                json=body,
                headers={"Authorization": f"Bearer {tenant_token}"},
            )
            return _response_json_object(resp)

    # --- Docs API ---
    async def read_feishu_doc(self, app_id: str, app_secret: str, document_id: str) -> JsonObject:
        """Get pure text content of a new-version Feishu Doc (docx)."""
        tenant_token = await self.get_tenant_access_token(app_id, app_secret)
        async with httpx.AsyncClient(timeout=30) as client:
            resp: httpx.Response = await client.get(
                f"https://open.feishu.cn/open-apis/docx/v1/documents/{document_id}/raw_content",
                headers={"Authorization": f"Bearer {tenant_token}"},
            )
            return _response_json_object(resp)

    async def create_feishu_doc(
        self, app_id: str, app_secret: str, folder_token: str | None = None, title: str = "Untitled Document"
    ) -> JsonObject:
        """Create a new Feishu Doc (docx)."""
        tenant_token = await self.get_tenant_access_token(app_id, app_secret)
        body: JsonObject = {"title": title}
        if folder_token:
            body["folder_token"] = folder_token
        async with httpx.AsyncClient(timeout=15) as client:
            resp: httpx.Response = await client.post(
                "https://open.feishu.cn/open-apis/docx/v1/documents",
                json=body,
                headers={"Authorization": f"Bearer {tenant_token}"},
            )
            return _response_json_object(resp)

    async def append_feishu_doc(self, app_id: str, app_secret: str, document_id: str, content: str) -> JsonObject:
        """Append text to the end of a Feishu Doc (document_id is also the root block_id)."""
        tenant_token = await self.get_tenant_access_token(app_id, app_secret)
        # Convert plain text to a text block
        body: JsonObject = {
            "children": [
                {
                    "block_type": 2,  # Text block (paragraph)
                    "text": {"elements": [{"text_run": {"content": content}}]},
                }
            ]
        }
        async with httpx.AsyncClient(timeout=15) as client:
            resp: httpx.Response = await client.post(
                f"https://open.feishu.cn/open-apis/docx/v1/documents/{document_id}/blocks/{document_id}/children",
                json=body,
                headers={"Authorization": f"Bearer {tenant_token}"},
            )
            return _response_json_object(resp)

    async def append_feishu_doc_blocks(
        self, app_id: str, app_secret: str, document_id: str, block_id: str, blocks: list[JsonObject]
    ) -> JsonObject:
        """Append pre-parsed Markdown blocks to a Feishu doc block (e.g., body_block_id)."""
        tenant_token = await self.get_tenant_access_token(app_id, app_secret)
        async with httpx.AsyncClient(timeout=20) as client:
            resp: httpx.Response = await client.post(
                f"https://open.feishu.cn/open-apis/docx/v1/documents/{document_id}/blocks/{block_id}/children",
                json={"children": blocks},
                headers={"Authorization": f"Bearer {tenant_token}"},
            )
            return _response_json_object(resp)

    # --- Approval API ---
    async def create_approval_instance(
        self, app_id: str, app_secret: str, approval_code: str, user_id: str, form_data: str
    ) -> JsonObject:
        """Create a Feishu approval instance."""
        tenant_token = await self.get_tenant_access_token(app_id, app_secret)
        body: JsonObject = {"approval_code": approval_code, "user_id": user_id, "form": form_data}
        async with httpx.AsyncClient(timeout=15) as client:
            resp: httpx.Response = await client.post(
                "https://open.feishu.cn/open-apis/approval/v4/instances",
                json=body,
                headers={"Authorization": f"Bearer {tenant_token}"},
            )
            return _response_json_object(resp)

    async def query_approval_instances(
        self, app_id: str, app_secret: str, approval_code: str, status: str | None = None
    ) -> JsonObject:
        """Query Feishu approval instances."""
        tenant_token = await self.get_tenant_access_token(app_id, app_secret)
        body: JsonObject = {"approval_code": approval_code}
        if status:
            body["status"] = status
        async with httpx.AsyncClient(timeout=15) as client:
            resp: httpx.Response = await client.post(
                "https://open.feishu.cn/open-apis/approval/v4/instances/query",
                json=body,
                headers={"Authorization": f"Bearer {tenant_token}"},
            )
            return _response_json_object(resp)

    async def get_approval_instance(self, app_id: str, app_secret: str, instance_id: str) -> JsonObject:
        """Get details of a specific Feishu approval instance."""
        tenant_token = await self.get_tenant_access_token(app_id, app_secret)
        async with httpx.AsyncClient(timeout=15) as client:
            resp: httpx.Response = await client.get(
                f"https://open.feishu.cn/open-apis/approval/v4/instances/{instance_id}",
                headers={"Authorization": f"Bearer {tenant_token}"},
            )
            return _response_json_object(resp)

    # --- CardKit Streaming API ---

    def _get_lark_client(self, app_id: str, app_secret: str) -> LarkClient:
        """Get or create a cached lark-oapi SDK client for the given app credentials.

        Implements a simple LRU eviction policy: when the cache exceeds
        _LARK_CLIENT_CACHE_MAX entries, the least-recently-used client is removed.
        """
        client_cls = _require_lark_client_cls()
        cache_key = f"{app_id}:{app_secret}"
        cached = self._lark_clients.get(cache_key)
        if cached is None:
            # Evict the oldest entry if the cache is at capacity.
            if len(self._lark_clients) >= self._LARK_CLIENT_CACHE_MAX:
                evicted_key, _ = self._lark_clients.popitem(last=False)
                logger.debug(f"[Feishu] _lark_clients LRU evict: {evicted_key[:8]}...")
            builder_fn: object = getattr(client_cls, "builder", None)
            if not callable(builder_fn):
                raise RuntimeError("lark-oapi Client is missing builder()")
            started: object = builder_fn()
            if not _is_lark_builder(started):
                raise RuntimeError("lark-oapi Client.builder() returned an unexpected object")
            built: object = started.app_id(app_id).app_secret(app_secret).build()
            if not _is_lark_client(built):
                raise RuntimeError("lark-oapi Client.builder() did not return a client")
            self._lark_clients[cache_key] = built
            return built
        # Move hit entry to the tail so it is considered most-recently-used.
        self._lark_clients.move_to_end(cache_key)
        return cached

    async def create_card_entity(
        self,
        app_id: str,
        app_secret: str,
        card_dict: JsonObject,
    ) -> str:
        """Create a CardKit card entity and return its card_id."""
        from lark_oapi.api.cardkit.v1.model import (
            CreateCardRequest,
            CreateCardRequestBody,
        )

        client = self._get_lark_client(app_id, app_secret)
        cardkit = client.cardkit
        if cardkit is None:
            raise RuntimeError("lark-oapi client is missing the cardkit service")
        body: object = CreateCardRequestBody.builder().type("card_json").data(json.dumps(card_dict)).build()
        request: object = CreateCardRequest.builder().request_body(body).build()

        try:
            resp = await cardkit.v1.card.acreate(request)
            logger.info(f"[Feishu CardKit] create_card_entity response: code={resp.code}, msg={resp.msg}")
            if not resp.success():
                raise RuntimeError(f"Feishu CardKit create_card_entity failed: code={resp.code}, msg={resp.msg}")
            card_id: object = getattr(getattr(resp, "data", None), "card_id", None)
            if not isinstance(card_id, str) or not card_id:
                raise RuntimeError("Feishu CardKit create_card_entity returned no card_id")
            return card_id
        except Exception as e:
            if isinstance(e, RuntimeError):
                raise
            logger.error(f"[Feishu CardKit] create_card_entity error: {e}")
            raise RuntimeError(f"Feishu CardKit create_card_entity error: {e}") from e

    async def send_card_by_card_id(
        self,
        app_id: str,
        app_secret: str,
        receive_id: str,
        card_id: str,
        receive_id_type: str = "open_id",
    ) -> None:
        """Send an interactive message referencing an existing card_id."""
        content = json.dumps(
            {
                "type": "card",
                "data": {"card_id": card_id},
            }
        )
        _ = await self.send_message(
            app_id=app_id,
            app_secret=app_secret,
            receive_id=receive_id,
            msg_type="interactive",
            content=content,
            receive_id_type=receive_id_type,
            stage="send_card_by_card_id",
        )

    async def stream_card_content(
        self,
        app_id: str,
        app_secret: str,
        card_id: str,
        element_id: str,
        content: str,
        sequence: int,
    ) -> None:
        """Stream content to a specific card element via CardKit API."""
        from lark_oapi.api.cardkit.v1.model import (
            ContentCardElementRequest,
            ContentCardElementRequestBody,
        )

        client = self._get_lark_client(app_id, app_secret)
        cardkit = client.cardkit
        if cardkit is None:
            raise RuntimeError("lark-oapi client is missing the cardkit service")
        body: object = ContentCardElementRequestBody.builder().content(content).sequence(sequence).build()
        request: object = (
            ContentCardElementRequest.builder().card_id(card_id).element_id(element_id).request_body(body).build()
        )

        try:
            resp = await cardkit.v1.card_element.acontent(request)
            logger.info(
                "[Feishu CardKit] stream_card_content response: "
                + f"code={resp.code}, msg={resp.msg}, card_id={card_id}, "
                + f"element_id={element_id}, sequence={sequence}"
            )
            if not resp.success():
                raise RuntimeError(f"Feishu CardKit stream_card_content failed: code={resp.code}, msg={resp.msg}")
        except Exception as e:
            if isinstance(e, RuntimeError):
                raise
            logger.error(f"[Feishu CardKit] stream_card_content error: {e}")
            raise RuntimeError(f"Feishu CardKit stream_card_content error: {e}") from e

    async def set_card_streaming_mode(
        self,
        app_id: str,
        app_secret: str,
        card_id: str,
        streaming_mode: int,
        sequence: int,
    ) -> None:
        """Toggle streaming mode on a card via CardKit settings API."""
        from lark_oapi.api.cardkit.v1.model import (
            SettingsCardRequest,
            SettingsCardRequestBody,
        )

        client = self._get_lark_client(app_id, app_secret)
        cardkit = client.cardkit
        if cardkit is None:
            raise RuntimeError("lark-oapi client is missing the cardkit service")
        body: object = (
            SettingsCardRequestBody.builder()
            .settings(json.dumps({"streaming_mode": streaming_mode}))
            .sequence(sequence)
            .build()
        )
        request: object = SettingsCardRequest.builder().card_id(card_id).request_body(body).build()

        try:
            resp = await cardkit.v1.card.asettings(request)
            logger.info(
                "[Feishu CardKit] set_card_streaming_mode response: "
                + f"code={resp.code}, msg={resp.msg}, card_id={card_id}, "
                + f"streaming_mode={streaming_mode}, sequence={sequence}"
            )
            if not resp.success():
                raise RuntimeError(f"Feishu CardKit set_card_streaming_mode failed: code={resp.code}, msg={resp.msg}")
        except Exception as e:
            if isinstance(e, RuntimeError):
                raise
            logger.error(f"[Feishu CardKit] set_card_streaming_mode error: {e}")
            raise RuntimeError(f"Feishu CardKit set_card_streaming_mode error: {e}") from e

    async def update_cardkit_card(
        self,
        app_id: str,
        app_secret: str,
        card_id: str,
        card_dict: JsonObject,
        sequence: int,
    ) -> None:
        """Full card update via CardKit API."""
        from lark_oapi.api.cardkit.v1.model import (
            Card,
            UpdateCardRequest,
            UpdateCardRequestBody,
        )

        client = self._get_lark_client(app_id, app_secret)
        cardkit = client.cardkit
        if cardkit is None:
            raise RuntimeError("lark-oapi client is missing the cardkit service")
        card: object = Card.builder().type("card_json").data(json.dumps(card_dict)).build()
        body: object = UpdateCardRequestBody.builder().card(card).sequence(sequence).build()
        request: object = UpdateCardRequest.builder().card_id(card_id).request_body(body).build()

        try:
            resp = await cardkit.v1.card.aupdate(request)
            logger.info(
                "[Feishu CardKit] update_cardkit_card response: "
                + f"code={resp.code}, msg={resp.msg}, card_id={card_id}, "
                + f"sequence={sequence}"
            )
            if not resp.success():
                raise RuntimeError(f"Feishu CardKit update_cardkit_card failed: code={resp.code}, msg={resp.msg}")
        except Exception as e:
            if isinstance(e, RuntimeError):
                raise
            logger.error(f"[Feishu CardKit] update_cardkit_card error: {e}")
            raise RuntimeError(f"Feishu CardKit update_cardkit_card error: {e}") from e


feishu_service = FeishuService()
