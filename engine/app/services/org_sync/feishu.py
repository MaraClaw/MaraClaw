"""Feishu organization sync adapter."""

import asyncio
import uuid
from typing import Any, ClassVar, override

import httpx

from app.core.json_types import JsonObject, json_as_bool, json_as_str, json_as_str_or
from app.core.logging import logger
from app.records.identity import IdentityProviderRecord

from .base import BaseOrgSyncAdapter
from .types import ExternalDepartment, ExternalUser


class FeishuOrgSyncAdapter(BaseOrgSyncAdapter):
    """Feishu organization sync adapter."""

    provider_type: ClassVar[str] = "feishu"

    FEISHU_APP_TOKEN_URL: ClassVar[str] = "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal"  # noqa: S105
    FEISHU_DEPT_URL: ClassVar[str] = "https://open.feishu.cn/open-apis/contact/v3/departments"
    FEISHU_USERS_URL: ClassVar[str] = "https://open.feishu.cn/open-apis/contact/v3/users/find_by_department"

    def __init__(
        self,
        provider: IdentityProviderRecord | None = None,
        config: JsonObject | None = None,
        tenant_id: uuid.UUID | None = None,
    ):
        super().__init__(provider, config, tenant_id)
        self.app_id: str = self._config_string("app_id")
        self.app_secret: str = self._config_string("app_secret")

    @property
    @override
    def api_base_url(self) -> str:
        return "https://open.feishu.cn/open-apis"

    @override
    async def get_access_token(self) -> str:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                self.FEISHU_APP_TOKEN_URL,
                json={"app_id": self.app_id, "app_secret": self.app_secret},
            )
            data = resp.json()
            return data.get("tenant_access_token") or data.get("app_access_token") or ""

    @override
    async def fetch_departments(self) -> list[ExternalDepartment]:
        """Fetch all departments from Feishu using concurrent recursive calls to get parent-child relationships."""
        token = await self.get_access_token()
        all_depts: list[ExternalDepartment] = []
        # Add a virtual root for the tenant, consistent with DingTalk root behavior
        all_depts.append(
            ExternalDepartment(
                external_id="0",
                name="Root",
                parent_external_id=None,
                member_count=0,
                raw_data={"department_id": "0", "name": "Root"},
            )
        )

        async with httpx.AsyncClient() as client:
            sem = asyncio.Semaphore(15)  # Limit concurrent requests to avoid rate limits

            async def fetch_children(parent_id: str):
                page_token = ""
                tasks = []
                while True:
                    params = {
                        "department_id_type": "open_department_id",
                        "fetch_child": "false",
                        "page_size": "50",
                    }
                    if page_token:
                        params["page_token"] = page_token

                    async with sem:
                        resp = await client.get(
                            f"{self.FEISHU_DEPT_URL}/{parent_id}/children",
                            params=params,
                            headers={"Authorization": f"Bearer {token}"},
                        )
                    data = resp.json()

                    if data.get("code") != 0:
                        logger.error(f"Feishu fetch departments list error for parent {parent_id}: {data}")
                        break

                    res_data_raw = data.get("data", {})
                    res_data: dict[str, Any] = dict[str, Any](res_data_raw) if isinstance(res_data_raw, dict) else {}
                    items: list[dict[str, Any]] = [
                        dict[str, Any](item) for item in (res_data.get("items", []) or []) if isinstance(item, dict)
                    ]
                    for item in items:
                        dept_id = json_as_str(item.get("open_department_id"))
                        if not dept_id:
                            continue

                        # Since we fetched using parent_id, we intrinsically know the parent!
                        parent_external = parent_id if parent_id and parent_id != "0" else "0"

                        dept = ExternalDepartment(
                            external_id=dept_id,
                            name=item.get("name", ""),
                            parent_external_id=parent_external,
                            member_count=item.get("member_count", 0),
                            raw_data=item,
                        )
                        all_depts.append(dept)

                        # Recursively fetch children for this department
                        tasks.append(fetch_children(dept_id))

                    page_token = res_data.get("page_token", "")
                    if not page_token:
                        break

                if tasks:
                    _gathered: list[Any] = list(await asyncio.gather(*tasks))
                    del _gathered

            await fetch_children("0")

        logger.info(f"Feishu fetched {len(all_depts)} departments total.")
        return all_depts

    @override
    async def fetch_users(self, department_external_id: str) -> list[ExternalUser]:
        """Fetch users in a department.

        IMPORTANT: Uses user_id_type=user_id (employee_id), which requires the
        'contact:user.employee_id:readonly' permission in the Feishu app.

        WHY user_id (not open_id or union_id):
        - open_id is app-specific: the same user has a different open_id in each Feishu app.
          Using open_id would break matching between org-sync users and Feishu bot channel users,
          since they use different apps.
        - union_id is ISV-scoped (same across apps from the same ISV), but not universal.
        - user_id (employee_id) is the only enterprise-wide stable identifier that works
          consistently across org sync, SSO, and bot channel user resolution.

        This permission requires app re-publishing in Feishu console (not instant like DingTalk).
        """
        token = await self.get_access_token()
        users: list[ExternalUser] = []
        page_token = ""

        async with httpx.AsyncClient() as client:
            while True:
                params = {
                    "department_id": department_external_id,
                    "department_id_type": "open_department_id",
                    # user_id (employee_id) is the enterprise-wide stable identifier.
                    # Requires 'contact:user.employee_id:readonly' permission + app re-publish.
                    "user_id_type": "user_id",
                    "page_size": "50",
                }
                if page_token:
                    params["page_token"] = page_token

                resp = await client.get(
                    self.FEISHU_USERS_URL,
                    params=params,
                    headers={"Authorization": f"Bearer {token}"},
                )
                data = resp.json()

                if data.get("code") != 0:
                    error_code = data.get("code")
                    error_msg = data.get("msg", "")
                    logger.error(
                        f"Feishu fetch users error for dept {department_external_id}: "
                        + f"code={error_code}, msg={error_msg}"
                    )
                    # Provide targeted guidance based on error code
                    if error_code == 40060:
                        # 40060 = "no dept authority": the app has correct API scopes
                        # but lacks DATA-level access to this department.
                        guidance = (
                            f"Feishu API error (code {error_code}): {error_msg}. "
                            + f"The app does not have data access to this department. "
                            + f"Please go to Feishu Open Platform -> App -> Permissions -> "
                            + f"Data Permissions (数据权限) -> Contact Scope (通讯录权限范围) -> "
                            + f"set to 'All Employees' (全部员工) or add the required departments. "
                            + f"After changing, you must publish a new app version for it to take effect."
                        )
                    else:
                        guidance = (
                            f"Feishu API error (code {error_code}): {error_msg}. "
                            + f"One of the following scopes may be required: "
                            + f"[contact:user.employee_id:readonly]. "
                            + f"Please enable this permission in Feishu Open Platform -> App -> "
                            + f"Permissions -> search 'employee_id' -> enable and publish a new version. "
                            + f"Note: unlike DingTalk, Feishu permissions require app re-publishing to take effect."
                        )
                    raise RuntimeError(guidance)

                res_data_raw = data.get("data", {})
                res_data: dict[str, Any] = dict[str, Any](res_data_raw) if isinstance(res_data_raw, dict) else {}
                items: list[dict[str, Any]] = [
                    dict[str, Any](item) for item in (res_data.get("items", []) or []) if isinstance(item, dict)
                ]
                for item in items:
                    # Collect all departments the user belongs to
                    raw_dept_ids = item.get("department_ids", [])
                    department_ids = (
                        [str(did) for did in raw_dept_ids]
                        if isinstance(raw_dept_ids, list) and raw_dept_ids
                        else [department_external_id]
                    )

                    # When user_id_type=open_id, Feishu returns the open_id value in the
                    # "user_id" field of the response. So external_id == open_id == open_id field.
                    # The open_id field is also present for consistency.
                    external_id = json_as_str_or(item.get("user_id")) or json_as_str_or(item.get("open_id"))

                    # For Feishu, a user is considered inactive if they are explicitly frozen or resigned.
                    # Merely not being activated (is_activated=False) shouldn't hide them from the org chart.
                    status_raw = item.get("status", {})
                    feishu_status: dict[str, Any] = dict[str, Any](status_raw) if isinstance(status_raw, dict) else {}
                    is_frozen = json_as_bool(feishu_status.get("is_frozen", False))
                    is_resigned = json_as_bool(feishu_status.get("is_resigned", False))
                    member_status = "inactive" if (is_frozen or is_resigned) else "active"

                    user = ExternalUser(
                        external_id=external_id,
                        open_id=item.get("open_id", ""),
                        unionid=item.get("union_id", ""),
                        name=item.get("name", ""),
                        email=item.get("email", ""),
                        avatar_url=item.get("avatar_url", ""),
                        title=item.get("title", ""),
                        department_external_id=department_external_id,
                        department_ids=department_ids,
                        mobile=item.get("mobile", ""),
                        status=member_status,
                        raw_data=item,
                    )
                    users.append(user)

                page_token = res_data.get("page_token", "")
                if not page_token:
                    break

        return users
