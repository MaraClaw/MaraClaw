"""DingTalk organization sync adapter."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar, override

import httpx

from app.core.json_types import JsonObject, json_as_str_or
from app.records.identity import IdentityProviderRecord

from .base import BaseOrgSyncAdapter
from .types import ExternalDepartment, ExternalUser


class DingTalkOrgSyncAdapter(BaseOrgSyncAdapter):
    """DingTalk organization sync adapter."""

    provider_type: ClassVar[str] = "dingtalk"

    DINGTALK_API_URL: ClassVar[str] = "https://oapi.dingtalk.com"
    DINGTALK_TOKEN_URL: ClassVar[str] = "https://oapi.dingtalk.com/gettoken"  # noqa: S105
    DINGTALK_DEPT_LIST_URL: ClassVar[str] = "https://oapi.dingtalk.com/topapi/v2/department/listsub"
    DINGTALK_USER_LIST_URL: ClassVar[str] = "https://oapi.dingtalk.com/topapi/v2/user/list"

    def __init__(
        self,
        provider: IdentityProviderRecord | None = None,
        config: JsonObject | None = None,
        tenant_id: uuid.UUID | None = None,
    ):
        super().__init__(provider, config, tenant_id)
        self.app_key: str = self._config_string("app_key", "appkey", "app_id")
        self.app_secret: str = self._config_string("app_secret", "appsecret", "app_secret_key")
        self._access_token: str | None = None
        self._token_expires_at: datetime | None = None
        self._dept_path_map: dict[str, str] = {}

    @property
    @override
    def api_base_url(self) -> str:
        return self.DINGTALK_API_URL

    @override
    async def get_access_token(self) -> str:
        if self._access_token and self._token_expires_at and datetime.now(UTC) < self._token_expires_at:
            return self._access_token

        if not self.app_key or not self.app_secret:
            raise ValueError("DingTalk app_key/app_secret missing in provider config")

        async with httpx.AsyncClient() as client:
            resp = await client.get(
                self.DINGTALK_TOKEN_URL,
                params={"appkey": self.app_key, "appsecret": self.app_secret},
            )
            data = resp.json()
            if data.get("errcode") != 0:
                raise RuntimeError(f"DingTalk token error: {data.get('errmsg') or data}")
            token = data.get("access_token") or ""
            expires_in = int(data.get("expires_in") or 7200)
            self._access_token = token
            # refresh a bit earlier
            self._token_expires_at = datetime.now(UTC) + timedelta(seconds=max(expires_in - 60, 60))
            return token

    @override
    async def fetch_departments(self) -> list[ExternalDepartment]:
        token = await self.get_access_token()
        all_depts: list[ExternalDepartment] = []
        # dept_index: external_id -> (name, parent_external_id_str | None)
        dept_index: dict[str, tuple[str, str | None]] = {}

        seen: set[int] = set()
        queue: list[int] = [1]  # DingTalk root dept id
        _request_count = 0

        async with httpx.AsyncClient() as client:
            while queue:
                parent_id = queue.pop(0)
                if parent_id in seen:
                    continue
                seen.add(parent_id)

                # DingTalk rate limit: ~20 QPS per app per interface.
                # Sleep 60ms between requests to stay under the limit.
                if _request_count > 0:
                    await asyncio.sleep(0.06)
                _request_count += 1

                resp = await client.post(
                    self.DINGTALK_DEPT_LIST_URL,
                    params={"access_token": token},
                    json={"dept_id": parent_id},
                )
                data = resp.json()
                if data.get("errcode") != 0:
                    raise RuntimeError(f"DingTalk department list error: {data.get('errmsg') or data}")

                result = data.get("result")
                items: list[dict[str, Any]] = []
                if isinstance(result, list):
                    items = [dict[str, Any](item) for item in result if isinstance(item, dict)]
                elif isinstance(result, dict):
                    raw_items = result.get("department", []) or []
                    items = [dict[str, Any](item) for item in raw_items if isinstance(item, dict)]

                for item in items:
                    raw_dept_id = item.get("dept_id")
                    if raw_dept_id is None:
                        continue
                    dept_id = int(raw_dept_id)
                    dept_name = json_as_str_or(item.get("name"))
                    # Use actual parent_id from API response to preserve real hierarchy
                    raw_parent_id = item.get("parent_id")
                    if dept_id == 1 or not raw_parent_id or int(raw_parent_id) == dept_id:
                        parent_external = None  # Root has no parent
                    else:
                        parent_external = str(int(raw_parent_id))
                    external_id = str(dept_id)
                    dept_index[external_id] = (dept_name, parent_external)
                    all_depts.append(
                        ExternalDepartment(
                            external_id=external_id,
                            name=dept_name,
                            parent_external_id=parent_external,
                            member_count=item.get("member_count", 0) or 0,
                            raw_data=item,
                        )
                    )
                    if dept_id not in seen:
                        queue.append(dept_id)

        # Ensure root exists in index (for path building and possible member sync)
        if "1" not in dept_index:
            dept_index["1"] = ("Root", None)
            all_depts.append(
                ExternalDepartment(
                    external_id="1",
                    name="Root",
                    parent_external_id=None,
                    member_count=0,
                    raw_data={"dept_id": 1, "name": "Root"},
                )
            )

        self._dept_path_map = self._build_dept_paths(dept_index)
        return all_depts

    @override
    async def fetch_users(self, department_external_id: str) -> list[ExternalUser]:
        token = await self.get_access_token()
        users: list[ExternalUser] = []
        cursor = 0
        dept_id = int(department_external_id)
        async with httpx.AsyncClient() as client:
            while True:
                # DingTalk rate limit: ~20 QPS per app per interface.
                # Sleep 60ms between requests to stay under the limit.
                await asyncio.sleep(0.06)

                resp = await client.post(
                    self.DINGTALK_USER_LIST_URL,
                    params={"access_token": token},
                    json={"dept_id": dept_id, "cursor": cursor, "size": 100},
                )
                data = resp.json()
                if data.get("errcode") != 0:
                    raise RuntimeError(f"DingTalk user list error: {data.get('errmsg') or data}")

                result_raw = data.get("result", {}) or {}
                result: dict[str, Any] = dict[str, Any](result_raw) if isinstance(result_raw, dict) else {}
                items: list[dict[str, Any]] = [
                    dict[str, Any](item) for item in (result.get("list", []) or []) if isinstance(item, dict)
                ]
                for item in items:
                    external_id = json_as_str_or(item.get("userid")) or json_as_str_or(item.get("user_id"))
                    # Get user's actual department list from DingTalk data
                    dept_id_list = item.get("dept_id_list", [])
                    department_ids = (
                        [str(did) for did in dept_id_list]
                        if isinstance(dept_id_list, list) and dept_id_list
                        else [department_external_id]
                    )
                    # Use last level department (last item in list is most specific)
                    last_dept_id = department_ids[-1] if department_ids else department_external_id
                    last_dept_path = self._dept_path_map.get(last_dept_id, "")
                    user = ExternalUser(
                        external_id=external_id,
                        unionid=item.get("unionid", "") or "",
                        open_id=item.get("openid", "") or "",
                        name=item.get("name", ""),
                        email=item.get("email", "") or "",
                        avatar_url=item.get("avatar", "") or "",
                        title=item.get("title", "") or "",
                        department_external_id=last_dept_id,
                        department_path=last_dept_path,
                        department_ids=department_ids,
                        mobile=item.get("mobile", "") or "",
                        status="active" if item.get("active", True) else "inactive",
                        raw_data=item,
                    )
                    users.append(user)

                if not result.get("has_more"):
                    break
                cursor = int(result.get("next_cursor") or 0)

        return users

    def _build_dept_paths(self, dept_index: dict[str, tuple[str, str | None]]) -> dict[str, str]:
        paths: dict[str, str] = {}

        def compute_path(dept_id: str, visited: set[str] | None = None) -> str:
            if dept_id in paths:
                return paths[dept_id]
            if visited is None:
                visited = set()
            if dept_id in visited:
                # Cycle guard
                paths[dept_id] = dept_id
                return dept_id
            visited.add(dept_id)
            name, parent_id = dept_index.get(dept_id, ("", None))
            if not parent_id or parent_id not in dept_index:
                paths[dept_id] = name
                return name
            parent_path = compute_path(parent_id, visited)
            full = f"{parent_path}/{name}" if parent_path else name
            paths[dept_id] = full
            return full

        for did in list(dept_index.keys()):
            _ = compute_path(did)
        return paths
