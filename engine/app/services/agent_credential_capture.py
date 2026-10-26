"""Revision-bound browser cookie capture without vault enumeration."""

import base64
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, assert_never
from uuid import UUID, uuid4

from pydantic import JsonValue, TypeAdapter, ValidationError

from app.config import get_settings
from app.core.security import encrypt_data
from app.dao.agent_credential_dao import CredentialRevisionConflictError, agent_credential_dao
from app.records.agent_credential import CredentialRevision
from app.schemas.agent_credential import CredentialCapture, CredentialCaptureCreate, CredentialCaptureReplace

if TYPE_CHECKING:
    from app.services.agentbay_client import AgentBayClient


class CaptureTargetMissingError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class CapturePlan:
    agent_id: UUID
    platform: str
    target: CredentialRevision | None


async def bind_capture(agent_id: UUID, capture: CredentialCapture) -> CredentialRevision | None:
    match capture:
        case CredentialCaptureCreate():
            return None
        case CredentialCaptureReplace(credential_id=credential_id, expected_revision=expected):
            credential = await agent_credential_dao.get_for_agent(credential_id, agent_id)
            if credential is None:
                raise CaptureTargetMissingError
            if credential.revision != expected:
                raise CredentialRevisionConflictError
            return CredentialRevision(credential_id, agent_id, expected)
        case unreachable:
            assert_never(unreachable)


_EXPORT_SCRIPT = r"""
const { chromium } = require('/usr/local/lib/node_modules/playwright');
(async () => {
    let browser;
    try {
        browser = await chromium.connectOverCDP('http://localhost:9222');
        const rawCookies = await browser.contexts()[0].cookies();
        const sameSiteMap = { none: 'None', lax: 'Lax', strict: 'Strict' };
        const cookies = rawCookies.map(c => {
            const out = { ...c };
            if (out.sameSite != null) {
                out.sameSite = sameSiteMap[String(out.sameSite).toLowerCase()] || 'Lax';
            }
            if (out.expires != null && out.expires <= 0) delete out.expires;
            if (out.domain && !out.domain.startsWith('.')) out.domain = '.' + out.domain;
            return out;
        });
        console.log('COOKIES_EXPORT:' + JSON.stringify(cookies));
    } catch {
        process.exitCode = 1;
    } finally {
        if (browser) await browser.close().catch(() => { process.exitCode = 1; });
    }
})();
"""


async def capture_cookies(client: AgentBayClient, plan: CapturePlan) -> int:
    script_path = f"tc_export_cookies_{uuid4().hex}.js"
    encoded = base64.b64encode(_EXPORT_SCRIPT.encode()).decode("ascii")
    written = await client.command_exec(f"echo '{encoded}' | /usr/bin/base64 -d > {script_path}")
    if written.get("success") is not True:
        return 0
    result = await client.command_exec(
        f"node {script_path}; result=$?; rm -f {script_path}; exit $result", timeout_ms=15000
    )
    if result.get("success") is not True:
        return 0
    stdout = result.get("stdout")
    if not isinstance(stdout, str):
        return 0
    lines = [line.removeprefix("COOKIES_EXPORT:") for line in stdout.splitlines() if line.startswith("COOKIES_EXPORT:")]
    if len(lines) != 1:
        return 0
    try:
        cookies = TypeAdapter(list[dict[str, JsonValue]]).validate_json(lines[0])
    except ValidationError:
        return 0
    if not cookies:
        return 0
    encrypted = encrypt_data(json.dumps(cookies), get_settings().SECRET_KEY)
    now = datetime.now(UTC)
    if plan.target is None:
        _ = await agent_credential_dao.create(
            obj_in={
                "agent_id": plan.agent_id,
                "credential_type": "website",
                "platform": plan.platform,
                "display_name": plan.platform,
                "cookies_json": encrypted,
                "cookies_updated_at": now,
                "last_login_at": now,
                "status": "active",
            }
        )
    else:
        _ = await agent_credential_dao.edit(
            plan.target,
            {"cookies_json": encrypted, "cookies_updated_at": now, "last_login_at": now, "status": "active"},
        )
    return len(cookies)
