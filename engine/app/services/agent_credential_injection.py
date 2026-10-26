"""Per-revision cookie insertion receipts, not website authentication evidence."""

import base64
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, ClassVar
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, ValidationError

from app.config import get_settings
from app.core.logging import logger
from app.core.security import decrypt_data
from app.dao.agent_credential_dao import agent_credential_dao
from app.records.agent_credential import CredentialRevision

if TYPE_CHECKING:
    from app.services.agentbay_client import AgentBayClient


class InjectionReceipt(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, strict=True, extra="forbid")
    credential_id: UUID
    revision: int = Field(gt=0)
    accepted: int = Field(ge=0)
    failed: int = Field(ge=0)


_INJECT_SCRIPT = r"""
const { chromium } = require('/usr/local/lib/node_modules/playwright');
(async () => {
    let browser;
    try {
        browser = await chromium.connectOverCDP('http://localhost:9222');
        const context = browser.contexts()[0];
        const sameSiteMap = { none: 'None', lax: 'Lax', strict: 'Strict' };
        let accepted = 0;
        let failed = 0;
        for (const raw of batch.cookies) {
            try {
                const cookie = { ...raw };
                if (cookie.sameSite != null) {
                    cookie.sameSite = sameSiteMap[String(cookie.sameSite).toLowerCase()] || 'Lax';
                }
                if (cookie.expires != null && cookie.expires <= 0) delete cookie.expires;
                if (cookie.domain && !cookie.domain.startsWith('.')) cookie.domain = '.' + cookie.domain;
                await context.addCookies([cookie]);
                accepted++;
            } catch {
                failed++;
            }
        }
        console.log('INJECT_RESULT:' + JSON.stringify({
            credential_id: batch.credential_id, revision: batch.revision, accepted, failed
        }));
    } catch {
        process.exitCode = 1;
    } finally {
        if (browser) await browser.close().catch(() => { process.exitCode = 1; });
    }
})();
"""


async def inject_credentials(client: AgentBayClient, agent_id: UUID) -> None:
    try:
        credentials = await agent_credential_dao.list_active_with_cookies(agent_id)
        if not credentials:
            return
        _ = await client._ensure_browser_initialized()
    except Exception as error:
        logger.warning("[AgentBay] Credential injection unavailable: error_type={}", type(error).__name__)
        return
    for credential in credentials:
        try:
            if credential.cookies_json is None:
                continue
            raw = decrypt_data(credential.cookies_json, get_settings().SECRET_KEY)
            cookies = TypeAdapter(list[dict[str, JsonValue]]).validate_json(raw)
            if not cookies:
                continue
            batch = {
                "credential_id": str(credential.id),
                "revision": credential.revision,
                "cookies": cookies,
            }
            script = "const batch = " + json.dumps(batch) + ";\n" + _INJECT_SCRIPT
            encoded = base64.b64encode(script.encode()).decode("ascii")
            path = f"tc_inject_cookies_{uuid4().hex}.js"
            written = await client.command_exec(f"echo '{encoded}' | /usr/bin/base64 -d > {path}")
            if written.get("success") is not True:
                continue
            result = await client.command_exec(f"node {path}; result=$?; rm -f {path}; exit $result", timeout_ms=15000)
            stdout = result.get("stdout")
            if result.get("success") is not True or not isinstance(stdout, str):
                continue
            lines = [
                line.removeprefix("INJECT_RESULT:") for line in stdout.splitlines() if line.startswith("INJECT_RESULT:")
            ]
            if len(lines) != 1:
                continue
            receipt = InjectionReceipt.model_validate_json(lines[0])
            if (
                receipt.credential_id == credential.id
                and receipt.revision == credential.revision
                and receipt.accepted == len(cookies)
                and receipt.failed == 0
            ):
                _ = await agent_credential_dao.publish_injection(
                    CredentialRevision(credential.id, agent_id, credential.revision), datetime.now(UTC)
                )
        except ValidationError, ValueError:
            logger.warning("[AgentBay] Invalid stored cookie batch: credential={}", credential.id)
        except Exception as error:
            logger.warning(
                "[AgentBay] Credential injection failed: credential={}, error_type={}",
                credential.id,
                type(error).__name__,
            )
