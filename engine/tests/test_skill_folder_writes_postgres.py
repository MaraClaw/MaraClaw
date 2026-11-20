"""Skill folder writes against managed S3 quota (opt-in ``MARACLAW_TEST_POSTGRES_DSN``)."""

import pytest

from agent_deletion_fixtures import deletion_scenario
from app.services.storage_runtime.quota_errors import (
    StorageFileTooLargeError,
    StorageQuotaError,
    StorageQuotaExceededError,
)
from app.services.storage_runtime.skill_install import write_skill_files
from managed_storage_fixtures import StorageWorld, quota_world

__all__ = ["deletion_scenario", "quota_world"]

LIMIT, CEILING = 1000, 400


@pytest.mark.parametrize(
    ("sizes", "error"),
    [((400, CEILING + 1), StorageFileTooLargeError), ((400, 400, 400), StorageQuotaExceededError)],
    ids=["file-too-large", "quota-exceeded"],
)
async def test_a_quota_refusal_mid_import_keeps_its_type_and_restores_storage(
    quota_world: StorageWorld, sizes: tuple[int, ...], error: type[StorageQuotaError]
) -> None:
    # Given an agent with a metered skill file and an import that shrinks it, then outgrows the agent's limits.
    world = quota_world
    world.scale(LIMIT, CEILING)
    agent = await world.agent()
    skill_key = world.key(agent, "skills/report/SKILL.md")
    await world.backend.write_bytes(skill_key, b"o" * 100)
    before = dict(world.s3.state.objects)
    usage = await world.usage(agent)
    payload = [
        (skill_key, b"n" * 50),
        *((world.key(agent, f"skills/report/file-{index}.bin"), b"x" * size) for index, size in enumerate(sizes)),
    ]

    # When the import is refused partway through.
    with pytest.raises(error) as raised:
        _ = await write_skill_files(world.backend, payload, folder=world.key(agent, "skills/report"))

    # Then the quota error reaches the caller as itself, and storage and the charge are as they were.
    assert type(raised.value) is error
    assert world.s3.state.objects == before
    assert await world.usage(agent) == usage
