"""A tenant's primary and fallback models behind scripted clients, for callers that run through the provider circuit.

`install_model_pair` serves the agent's model rows from `llm_model_dao.get_many` and builds a scripted client for
whichever model a caller picks, so a test reads which model answered from `created` and the clients' requests.
"""

from collections.abc import Sequence
from dataclasses import dataclass, replace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from tests.llm_retry_fakes import health_model
from tests.test_background_llm import BackgroundClient

from app.dao.llm_dao import llm_model_dao
from app.records.agent import AgentRecord
from app.records.llm import LLMModelRecord
from app.services.llm.types import LLMResponse

type Script = Sequence[LLMResponse | BaseException]


@dataclass(frozen=True, slots=True)
class ModelPair:
    agent: AgentRecord
    primary: LLMModelRecord
    fallback: LLMModelRecord | None
    primary_client: BackgroundClient
    fallback_client: BackgroundClient
    created: list[str]


def install_model_pair(
    monkeypatch: pytest.MonkeyPatch, primary_script: Script, fallback_script: Script | None = None
) -> ModelPair:
    """An agent with a primary model and, when ``fallback_script`` is given, a fallback; each answers from its script."""
    tenant_id = uuid4()
    primary = replace(health_model("primary"), tenant_id=tenant_id)
    fallback = replace(health_model("fallback"), tenant_id=tenant_id) if fallback_script is not None else None
    rows = [row for row in (primary, fallback) if row is not None]
    clients = {"primary": BackgroundClient(primary_script), "fallback": BackgroundClient(fallback_script or [])}
    created: list[str] = []

    def rows_for(ids: Sequence[UUID]) -> list[LLMModelRecord]:
        return [row for row in rows if row.id in ids]

    async def fresh_client(model: LLMModelRecord, **_kwargs: object) -> tuple[LLMModelRecord, BackgroundClient]:
        created.append(model.model)
        return model, clients[model.model]

    monkeypatch.setattr(llm_model_dao, "get_many", AsyncMock(side_effect=rows_for))
    monkeypatch.setattr("app.services.llm.create_fresh_llm_client_from_model", fresh_client)
    agent = AgentRecord(
        id=uuid4(),
        name="Worker",
        creator_id=uuid4(),
        tenant_id=tenant_id,
        primary_model_id=primary.id,
        fallback_model_id=fallback.id if fallback is not None else None,
    )
    return ModelPair(agent, primary, fallback, clients["primary"], clients["fallback"], created)
