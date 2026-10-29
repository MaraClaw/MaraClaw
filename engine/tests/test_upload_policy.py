import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from app.api import files as files_api
from app.core import security
from app.main import app
from app.records.agent import AgentRecord
from app.records.user import UserRecord
from app.services.openclaw_update_exclusion import guest_write_exclusion
from app.services.storage_runtime.local import LocalStorageBackend


@pytest.fixture
async def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    user = UserRecord(id=uuid.uuid4())

    async def authenticate(token: str, *, require_active: bool, enforce_password_change: bool) -> UserRecord:
        assert token == "test-token"
        assert require_active
        assert enforce_password_change
        return user

    async def allow_access(current_user: UserRecord, agent_id: uuid.UUID) -> tuple[AgentRecord, str]:
        return AgentRecord(id=agent_id, name="test", creator_id=current_user.id), "manage"

    monkeypatch.setattr(security, "load_user_from_access_token", authenticate)
    monkeypatch.setattr(files_api, "check_agent_access", allow_access)
    monkeypatch.setattr(files_api, "get_storage_backend", lambda: LocalStorageBackend(str(tmp_path)))
    app.dependency_overrides[guest_write_exclusion] = lambda: None
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as session:
            yield session
    finally:
        app.dependency_overrides.pop(guest_write_exclusion)


async def test_policy_requires_authentication(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/chat/upload-policy")
    assert response.status_code == 401


async def test_policy_exposes_limits_and_sorted_extensions(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/chat/upload-policy", headers={"Authorization": "Bearer test-token"})
    assert response.status_code == 200
    assert response.json() == {
        "chat_image_max_bytes": 10 * 1024 * 1024,
        "chat_file_max_bytes": 50 * 1024 * 1024,
        "workspace_file_max_bytes": 50 * 1024 * 1024,
        "image_extensions": [".bmp", ".gif", ".jpeg", ".jpg", ".png", ".webp"],
    }


async def test_policy_response_is_typed_in_application_openapi(client: httpx.AsyncClient) -> None:
    response = await client.get("/openapi.json")
    document = response.json()
    schema = document["paths"]["/api/chat/upload-policy"]["get"]["responses"]["200"]["content"]["application/json"][
        "schema"
    ]
    assert schema["$ref"] == "#/components/schemas/UploadPolicy"
    fields = document["components"]["schemas"]["UploadPolicy"]
    assert set(fields["required"]) == {
        "chat_image_max_bytes",
        "chat_file_max_bytes",
        "workspace_file_max_bytes",
        "image_extensions",
    }
    assert fields["properties"]["chat_image_max_bytes"]["type"] == "integer"
    assert fields["properties"]["chat_file_max_bytes"]["type"] == "integer"
    assert fields["properties"]["workspace_file_max_bytes"]["type"] == "integer"
    assert fields["properties"]["image_extensions"]["items"]["type"] == "string"


@pytest.mark.parametrize(
    ("filename", "content_type", "size", "expected_status", "is_image"),
    [
        ("picture.PNG", "application/octet-stream", 10 * 1024 * 1024, 200, True),
        ("picture.PNG", "application/octet-stream", 10 * 1024 * 1024 + 1, 400, True),
        ("archive.bin", "image/png", 50 * 1024 * 1024, 200, False),
        ("archive.bin", "image/png", 50 * 1024 * 1024 + 1, 400, False),
        (".png", "image/png", 10 * 1024 * 1024 + 1, 200, False),
    ],
)
async def test_chat_upload_applies_filename_limit(
    client: httpx.AsyncClient, filename: str, content_type: str, size: int, expected_status: int, is_image: bool
) -> None:
    response = await client.post(
        "/api/chat/upload",
        headers={"Authorization": "Bearer test-token"},
        files={"file": (filename, b"x" * size, content_type)},
    )
    assert response.status_code == expected_status
    if expected_status == 200:
        assert response.json()["size"] == size
        assert response.json()["is_image"] is is_image


@pytest.mark.parametrize(("size", "expected_status"), [(50 * 1024 * 1024, 200), (50 * 1024 * 1024 + 1, 400)])
async def test_workspace_image_upload_uses_file_limit(
    client: httpx.AsyncClient, size: int, expected_status: int
) -> None:
    response = await client.post(
        f"/api/agents/{uuid.uuid4()}/files/upload",
        headers={"Authorization": "Bearer test-token"},
        files={"file": ("picture.png", b"x" * size, "image/png")},
    )
    assert response.status_code == expected_status
    if expected_status == 200:
        assert response.json()["size"] == size
