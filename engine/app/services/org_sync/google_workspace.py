"""Google Workspace organization sync adapter."""

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar, override

import httpx
from jose import jwt

from app.config import get_settings
from app.core.json_types import JsonObject, json_as_str_or
from app.core.logging import logger
from app.core.security import decrypt_data
from app.records.identity import IdentityProviderRecord
from app.services.auth_provider import GoogleWorkspaceAuthProvider
from app.services.google_workspace_oauth import GOOGLE_HTTP_PROXY

from .base import BaseOrgSyncAdapter
from .types import ExternalDepartment, ExternalUser


class GoogleWorkspaceOrgSyncAdapter(BaseOrgSyncAdapter):
    """Google Workspace organization sync adapter.

    Primary mode uses an admin-authorized refresh token obtained via OAuth.
    Legacy service-account delegation is kept only as a compatibility fallback.
    """

    provider_type: ClassVar[str] = "google_workspace"

    GOOGLE_TOKEN_URL: ClassVar[str] = "https://oauth2.googleapis.com/token"  # noqa: S105
    GOOGLE_DIRECTORY_BASE_URL: ClassVar[str] = "https://admin.googleapis.com/admin/directory/v1"
    GOOGLE_DIRECTORY_SCOPE: ClassVar[str] = "https://www.googleapis.com/auth/admin.directory.user.readonly"
    GOOGLE_ORGUNIT_SCOPE: ClassVar[str] = "https://www.googleapis.com/auth/admin.directory.orgunit.readonly"

    def __init__(
        self,
        provider: IdentityProviderRecord | None = None,
        config: JsonObject | None = None,
        tenant_id: uuid.UUID | None = None,
    ):
        super().__init__(provider, config, tenant_id)
        self.service_account: JsonObject = self._load_service_account(self.config)
        self.client_id: str = self._config_string("client_id", "sso_client_id")
        self.client_secret: str = self._config_string("client_secret", "sso_client_secret")
        self.customer_id: str = self._config_string("customer_id") or "my_customer"
        self.admin_refresh_token: str | None = self._load_admin_refresh_token(self.config)
        self.delegated_admin_email: str = self._config_string(
            "delegated_admin_email", "admin_email", "google_admin_authorized_email"
        )
        self._access_token: str | None = None
        self._token_expires_at: datetime | None = None
        self._org_unit_path_to_external_id: dict[str, str] = {"/": "root"}
        self._org_unit_path_to_display_path: dict[str, str] = {"/": "Root"}

    @property
    @override
    def api_base_url(self) -> str:
        return self.GOOGLE_DIRECTORY_BASE_URL

    def _load_service_account(self, config: JsonObject) -> JsonObject:
        raw = config.get("service_account_json") or config.get("service_account")
        if raw is None:
            # Backward compatibility for older configurations that stored
            # the service account JSON in client_secret.
            legacy_secret = config.get("client_secret")
            if isinstance(legacy_secret, str) and legacy_secret.lstrip().startswith("{"):
                raw = legacy_secret
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str) and raw.strip():
            try:
                return json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError("service_account_json must be valid JSON") from exc
        return {}

    def _load_admin_refresh_token(self, config: JsonObject) -> str:
        encrypted = config.get("google_admin_refresh_token_encrypted")
        if isinstance(encrypted, str) and encrypted:
            try:
                return decrypt_data(encrypted, get_settings().SECRET_KEY)
            except Exception as exc:
                logger.warning(f"Failed to decrypt Google admin refresh token: {exc}")
        raw = config.get("google_admin_refresh_token")
        return raw if isinstance(raw, str) else ""

    @override
    async def get_access_token(self) -> str:
        if self._access_token and self._token_expires_at and datetime.now(UTC) < self._token_expires_at:
            return self._access_token

        if self.admin_refresh_token:
            return await self._refresh_user_access_token()

        if self.service_account:
            logger.warning("Google Workspace org sync is using legacy service-account delegation fallback")
            return await self._get_legacy_service_account_access_token()

        raise ValueError("Google Workspace admin authorization is required before directory sync")

    async def _get_legacy_service_account_access_token(self) -> str:
        """Compatibility path for older Google Workspace configurations."""

        client_email_value = self.service_account.get("client_email")
        private_key_value = self.service_account.get("private_key")
        token_uri_value = self.service_account.get("token_uri")
        client_email = client_email_value if isinstance(client_email_value, str) else ""
        private_key = private_key_value if isinstance(private_key_value, str) else ""
        token_uri = token_uri_value if isinstance(token_uri_value, str) else self.GOOGLE_TOKEN_URL
        scopes = " ".join([self.GOOGLE_DIRECTORY_SCOPE, self.GOOGLE_ORGUNIT_SCOPE])

        if not client_email or not private_key:
            raise ValueError("Google Workspace service_account_json must include client_email and private_key")
        if not self.delegated_admin_email:
            raise ValueError("Google Workspace delegated_admin_email is required for legacy service-account sync")

        now = datetime.now(UTC)
        assertion = jwt.encode(
            {
                "iss": client_email,
                "sub": self.delegated_admin_email,
                "scope": scopes,
                "aud": token_uri,
                "iat": int(now.timestamp()),
                "exp": int((now + timedelta(minutes=55)).timestamp()),
            },
            private_key,
            algorithm="RS256",
        )

        async with httpx.AsyncClient(timeout=20, proxy=GOOGLE_HTTP_PROXY) as client:
            resp = await client.post(
                token_uri,
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": assertion,
                },
            )
            data = resp.json()
            if resp.status_code >= 400 or "access_token" not in data:
                raise RuntimeError(f"Google OAuth token error: {data}")
            self._access_token = str(data["access_token"])
            expires_in = int(data.get("expires_in") or 3600)
            self._token_expires_at = now + timedelta(seconds=max(expires_in - 60, 60))
            return self._access_token

    async def _refresh_user_access_token(self) -> str:
        if not self.client_id or not self.client_secret:
            raise ValueError("Google Workspace client_id and client_secret are required")
        if not self.admin_refresh_token:
            raise ValueError("Google Workspace admin authorization is required before directory sync")

        now = datetime.now(UTC)
        provider = GoogleWorkspaceAuthProvider(
            config={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            }
        )
        data = await provider.refresh_access_token(self.admin_refresh_token)
        if "access_token" not in data:
            raise RuntimeError(f"Google OAuth refresh error: {data}")
        self._access_token = str(data["access_token"])
        expires_value = data.get("expires_in")
        expires_in = int(expires_value) if isinstance(expires_value, str | int | float) else 3600
        self._token_expires_at = now + timedelta(seconds=max(expires_in - 60, 60))
        return self._access_token

    @override
    async def fetch_departments(self) -> list[ExternalDepartment]:
        token = await self.get_access_token()
        customer_id = self.customer_id or "my_customer"
        departments: list[ExternalDepartment] = []

        async with httpx.AsyncClient(timeout=20, proxy=GOOGLE_HTTP_PROXY) as client:
            resp = await client.get(
                f"{self.GOOGLE_DIRECTORY_BASE_URL}/customer/{customer_id}/orgunits",
                params={"type": "all"},
                headers={"Authorization": f"Bearer {token}"},
            )
            data = resp.json()
            if resp.status_code >= 400:
                raise RuntimeError(f"Google Workspace orgunits error: {data}")

            items: list[dict[str, Any]] = [
                dict[str, Any](item) for item in (data.get("organizationUnits", []) or []) if isinstance(item, dict)
            ]

            for item in items:
                org_unit_path = json_as_str_or(item.get("orgUnitPath"))
                external_id = json_as_str_or(item.get("orgUnitId")) or org_unit_path or json_as_str_or(item.get("name"))
                normalized_path = org_unit_path if org_unit_path.startswith("/") else f"/{org_unit_path}"
                self._org_unit_path_to_external_id[normalized_path] = external_id
                self._org_unit_path_to_display_path[normalized_path] = normalized_path.strip("/") or "Root"

            departments.append(
                ExternalDepartment(
                    external_id="root",
                    name="Root",
                    parent_external_id=None,
                    member_count=0,
                    raw_data={"orgUnitPath": "/", "name": "Root"},
                )
            )

            for item in items:
                org_unit_path = item.get("orgUnitPath") or ""
                parent_org_unit_path = json_as_str_or(item.get("parentOrgUnitPath"), "/")
                external_id = json_as_str_or(item.get("orgUnitId")) or org_unit_path or json_as_str_or(item.get("name"))
                normalized_path = org_unit_path if org_unit_path.startswith("/") else f"/{org_unit_path}"
                parent_path = (
                    parent_org_unit_path if parent_org_unit_path.startswith("/") else f"/{parent_org_unit_path}"
                )
                parent_external_id = (
                    "root" if parent_path == "/" else self._org_unit_path_to_external_id.get(parent_path)
                )

                departments.append(
                    ExternalDepartment(
                        external_id=external_id,
                        name=item.get("name", ""),
                        parent_external_id=parent_external_id,
                        member_count=0,
                        raw_data=item,
                    )
                )

        return departments

    @override
    async def fetch_users(self, department_external_id: str) -> list[ExternalUser]:
        # Google Directory users are listed tenant-wide rather than per-org-unit.
        # Only fetch them on the root iteration to avoid duplicates.
        if department_external_id != "root":
            return []

        token = await self.get_access_token()
        users: list[ExternalUser] = []
        page_token = ""
        customer = self.customer_id or "my_customer"

        async with httpx.AsyncClient(timeout=20, proxy=GOOGLE_HTTP_PROXY) as client:
            while True:
                params: dict[str, Any] = {
                    "customer": customer,
                    "maxResults": 500,
                    "projection": "full",
                    "orderBy": "email",
                    "showDeleted": "false",
                }
                if page_token:
                    params["pageToken"] = page_token

                resp = await client.get(
                    f"{self.GOOGLE_DIRECTORY_BASE_URL}/users",
                    params=params,
                    headers={"Authorization": f"Bearer {token}"},
                )
                data = resp.json()
                if resp.status_code >= 400:
                    raise RuntimeError(f"Google Workspace users error: {data}")

                for item_raw in data.get("users", []) or []:
                    item = dict[str, Any](item_raw) if isinstance(item_raw, dict) else {}
                    org_unit_path = item.get("orgUnitPath") or "/"
                    normalized_path = org_unit_path if org_unit_path.startswith("/") else f"/{org_unit_path}"
                    department_external = self._org_unit_path_to_external_id.get(normalized_path, "root")
                    orgs = item.get("organizations") or [None]
                    primary_org_raw = orgs[0] if orgs else None
                    primary_org: dict[str, Any] = (
                        dict[str, Any](primary_org_raw) if isinstance(primary_org_raw, dict) else {}
                    )
                    primary_phone = self._extract_primary_phone(item.get("phones") or [])

                    users.append(
                        ExternalUser(
                            external_id=item.get("id", "") or item.get("primaryEmail", ""),
                            open_id=item.get("primaryEmail", ""),
                            unionid="",
                            name=(item.get("name") or {}).get("fullName") or item.get("primaryEmail", ""),
                            email=item.get("primaryEmail", "") or "",
                            avatar_url=item.get("thumbnailPhotoUrl", "") or "",
                            title=primary_org.get("title", "") or "",
                            department_external_id=department_external,
                            department_path=self._org_unit_path_to_display_path.get(normalized_path, "Root"),
                            department_ids=[department_external],
                            mobile=primary_phone,
                            status="inactive" if item.get("suspended") or item.get("archived") else "active",
                            raw_data=item,
                        )
                    )

                page_token = data.get("nextPageToken") or ""
                if not page_token:
                    break

        return users

    def _extract_primary_phone(self, phones: list[JsonObject]) -> str:
        if not phones:
            return ""
        for phone in phones:
            if phone.get("primary"):
                value = phone.get("value")
                return value if isinstance(value, str) else ""
        fallback = phones[0].get("value")
        return fallback if isinstance(fallback, str) else ""
