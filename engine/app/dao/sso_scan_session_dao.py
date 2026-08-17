"""DAO for sso_scan_sessions"""

from __future__ import annotations

from typing import ClassVar, final

from app.dao.base import BaseDAO
from app.records.sso_scan_session import SSOScanSessionRecord

_COLUMNS = (
    "id",
    "status",
    "provider_type",
    "error_msg",
    "tenant_id",
    "user_id",
    "access_token",
    "expires_at",
    "created_at",
)


@final
class SSOScanSessionDAO(BaseDAO[SSOScanSessionRecord]):
    table: ClassVar[str] = "sso_scan_sessions"
    columns: ClassVar[tuple[str, ...]] = _COLUMNS
    record_factory = staticmethod(SSOScanSessionRecord.from_row)


sso_scan_session_dao = SSOScanSessionDAO()
