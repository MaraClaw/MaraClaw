from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import anyio
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api import dingtalk, feishu, wecom
from app.core.json_types import int_from_row
from app.dao.identity_provider_dao import identity_provider_dao
from app.dao.sso_scan_session_dao import sso_scan_session_dao
from app.db.connection import DbConnection
from app.db.session import get_connection
from app.records.identity import IdentityProviderRecord
from app.records.sso_scan_session import SSOScanSessionRecord
from app.services.auth_provider import PROVIDER_CLASSES, AuthProviderPayload, BaseAuthProvider, ExternalUserInfo
from company_admission_fixtures import AdmissionScenario, admission_scenario

__all__ = ["admission_scenario", "scan_race"]


@dataclass
class ScanRace:
    admission: AdmissionScenario
    client: AsyncClient
    provider: IdentityProviderRecord
    scan: SSOScanSessionRecord
    info: ExternalUserInfo
    entered: anyio.Event = field(default_factory=anyio.Event)
    release: anyio.Event = field(default_factory=anyio.Event)
    responses: list[str] = field(default_factory=list)
    expected_exchanges: int = 1
    exchanges: int = 0
    pause_stage: str = "exchange"
    configs: list[str] = field(default_factory=list)
    provider_ids: list[UUID] = field(default_factory=list)

    async def callback(self) -> None:
        response = await self.client.get(
            f"/api/auth/{self.provider.provider_type}/callback",
            params={
                "authCode" if self.provider.provider_type == "dingtalk" else "code": "one-use-provider-code",
                "state": str(self.scan.id),
            },
        )
        assert response.status_code == 200
        assert "access_token" not in response.text
        assert "provider-access-secret" not in response.text
        self.responses.append(response.text)

    async def counts(self) -> dict[str, int]:
        row = await self.admission.db.fetchone(
            "SELECT (SELECT count(*) FROM identities) AS identities, (SELECT count(*) FROM users) AS users, "
            "(SELECT count(*) FROM org_members) AS members, "
            "(SELECT count(*) FROM company_admission_requests) AS admissions, "
            "(SELECT count(*) FROM notifications) AS notifications"
        )
        assert row is not None
        return {key: int_from_row(value) for key, value in row.items()}

    async def scan_status(self) -> object:
        return await self.admission.db.fetchval(
            "SELECT status FROM sso_scan_sessions WHERE id=%(id)s", {"id": self.scan.id}
        )


async def wait_blocked(db: DbConnection, backend_pid: object) -> None:
    with anyio.fail_after(10):
        while not await db.fetchval("SELECT cardinality(pg_blocking_pids(%(pid)s)) > 0", {"pid": backend_pid}):
            await anyio.lowlevel.checkpoint()


@pytest.fixture
async def scan_race(
    admission_scenario: AdmissionScenario, monkeypatch: pytest.MonkeyPatch, provider_type: str
) -> AsyncIterator[ScanRace]:
    provider = await identity_provider_dao.create(
        obj_in={
            "id": uuid4(),
            "provider_type": provider_type,
            "name": "Captured authority",
            "is_active": True,
            "sso_login_enabled": True,
            "config": {"app_id": "captured-app", "app_secret": "captured-secret"},
        }
    )
    scan = await sso_scan_session_dao.create(
        obj_in={"id": uuid4(), "expires_at": datetime.now(UTC) + timedelta(minutes=5)}
    )
    app = FastAPI()
    for router in (feishu.router, dingtalk.router, wecom.router):
        app.include_router(router, prefix="/api")
    async with AsyncClient(transport=ASGITransport(app), base_url="http://scan.test") as client:
        scenario = ScanRace(
            admission_scenario,
            client,
            provider,
            scan,
            ExternalUserInfo(
                provider_type=provider_type,
                provider_user_id="exact-external-id",
                provider_union_id="exact-union-id",
                name="Scan applicant",
                raw_data={"user_id": "exact-external-id", "userid": "exact-external-id"},
            ),
        )

        async def exchange(
            adapter: BaseAuthProvider, code: str, redirect_uri: str | None = None
        ) -> AuthProviderPayload:
            assert get_connection() is None
            assert adapter.provider is not None
            scenario.provider_ids.append(adapter.provider.id)
            scenario.configs.append(adapter._config_string("app_secret"))
            scenario.exchanges += 1
            if scenario.pause_stage == "exchange":
                if scenario.exchanges == scenario.expected_exchanges:
                    scenario.entered.set()
                await scenario.release.wait()
            return {"access_token": "provider-access-secret"}

        async def profile(adapter: BaseAuthProvider, token: str) -> ExternalUserInfo:
            assert get_connection() is None
            if scenario.pause_stage == "profile":
                scenario.entered.set()
                await scenario.release.wait()
            return scenario.info

        cls = PROVIDER_CLASSES[provider_type]
        monkeypatch.setattr(cls, "exchange_code_for_token", exchange)
        monkeypatch.setattr(cls, "get_user_info", profile)
        yield scenario
