import hashlib
from datetime import timedelta

import pytest
from fastapi import HTTPException

from app.core import security
from file_preview_fixtures import preview_context

__all__ = ["preview_context"]


async def test_metadata_returns_exact_local_version_when_file_exists(preview_context):
    # Given
    ctx = preview_context
    content = b"%PDF-preview"
    stat = (ctx.root / str(ctx.agent_id) / "report.pdf").stat()
    digest = hashlib.sha256(content).hexdigest()
    # When
    response = await ctx.client.get(f"{ctx.url}/preview-metadata", params={"path": "./report.pdf"}, headers=ctx.headers)
    # Then
    assert response.status_code == 200
    assert response.json() == {
        "path": "report.pdf",
        "name": "report.pdf",
        "size": len(content),
        "version_token": f"{stat.st_mtime_ns}:{len(content)}:{digest}",
        "content_type": "application/pdf",
    }
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    ctx.access.assert_awaited_once_with(ctx.user, ctx.agent_id, fresh=True)


async def test_bytes_return_same_origin_content_when_version_matches(preview_context):
    # Given
    ctx = preview_context
    version = await ctx.storage.get_version(f"{ctx.agent_id}/report.pdf")
    # When
    response = await ctx.client.get(
        f"{ctx.url}/preview-bytes",
        params={"path": "report.pdf", "version_token": version.token},
        headers=ctx.headers,
    )
    # Then
    assert response.status_code == 200
    assert response.content == b"%PDF-preview"
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-disposition"].startswith("attachment;")
    assert "location" not in response.headers
    ctx.access.assert_awaited_once_with(ctx.user, ctx.agent_id, fresh=True)


@pytest.mark.parametrize("endpoint", ["preview-metadata", "preview-bytes"])
@pytest.mark.parametrize(
    ("state", "status"),
    [
        ("missing", 401),
        ("invalid", 401),
        ("expired", 401),
        ("revoked", 401),
        ("inactive_user", 401),
        ("inactive_identity", 401),
        ("force_change", 403),
    ],
)
async def test_preview_rejects_current_auth_failure_when_snapshot_was_valid(preview_context, endpoint, state, status):
    # Given
    ctx = preview_context
    headers = ctx.headers
    match state:
        case "missing":
            headers = {}
        case "invalid":
            headers = {"Authorization": "Bearer invalid"}
        case "expired":
            token = await security.create_access_token(
                str(ctx.user.id), "member", timedelta(seconds=-1), identity_id=ctx.user.identity_id
            )
            headers = {"Authorization": f"Bearer {token}"}
        case "revoked":
            ctx.generation.return_value = 2
        case "inactive_user":
            ctx.user.is_active = False
        case "inactive_identity":
            ctx.user.identity.is_active = False
        case "force_change":
            ctx.user.identity.must_change_password = True
    # When
    response = await ctx.client.get(
        f"{ctx.url}/{endpoint}",
        params={"path": "report.pdf", "version_token": "stale", "ticket": "unused"},
        headers=headers,
    )
    # Then
    assert response.status_code == status
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    if state == "force_change":
        assert response.json()["detail"]["must_change_password"] is True
    ctx.access.assert_not_awaited()


@pytest.mark.parametrize("endpoint", ["preview-metadata", "preview-bytes"])
@pytest.mark.parametrize("status", [403, 404])
async def test_preview_honors_current_agent_access_when_authorization_fails(preview_context, endpoint, status):
    # Given
    ctx = preview_context
    ctx.access.side_effect = HTTPException(status, "Agent unavailable")
    # When
    response = await ctx.client.get(
        f"{ctx.url}/{endpoint}", params={"path": "report.pdf", "version_token": "stale"}, headers=ctx.headers
    )
    # Then
    assert response.status_code == status
    ctx.access.assert_awaited_once_with(ctx.user, ctx.agent_id, fresh=True)


@pytest.mark.parametrize("endpoint", ["preview-metadata", "preview-bytes"])
@pytest.mark.parametrize(
    "path",
    [
        "../report.pdf",
        " ../report.pdf",
        "folder/../../report.pdf",
        "folder/.. ",
        "/report.pdf",
        " /report.pdf",
        "C:/report.pdf",
        "C:report.pdf",
        " C:report.pdf",
        "C:\\report.pdf",
        "a\\b",
        "a\x00b",
        "a\x1fb",
        "a\x7fb",
        "a\x85b",
        "report.pdf\n",
        "report.pdf\t",
        "\nreport.pdf",
        "enterprise_info/../report.pdf",
    ],
)
async def test_preview_rejects_unsafe_paths_when_authenticated(preview_context, endpoint, path):
    # Given
    ctx = preview_context
    # When
    response = await ctx.client.get(
        f"{ctx.url}/{endpoint}", params={"path": path, "version_token": "stale"}, headers=ctx.headers
    )
    # Then
    assert response.status_code == 403


@pytest.mark.parametrize("endpoint", ["preview-metadata", "preview-bytes"])
@pytest.mark.parametrize("path", ["missing.pdf", "folder", "."])
async def test_preview_returns_not_found_when_target_is_absent_or_directory(preview_context, endpoint, path):
    # Given
    ctx = preview_context
    (ctx.root / str(ctx.agent_id) / "folder").mkdir()
    # When
    response = await ctx.client.get(
        f"{ctx.url}/{endpoint}", params={"path": path, "version_token": "stale"}, headers=ctx.headers
    )
    # Then
    assert response.status_code == 404


@pytest.mark.parametrize("endpoint", ["preview-metadata", "preview-bytes"])
@pytest.mark.parametrize("same_agent", [False, True])
async def test_preview_rejects_symlink_even_when_target_is_in_same_agent(preview_context, endpoint, same_agent):
    # Given
    ctx = preview_context
    secret = ctx.root / str(ctx.agent_id) / "same-agent.txt" if same_agent else ctx.root / "other-agent.txt"
    secret.write_bytes(b"private")
    (ctx.root / str(ctx.agent_id) / "alias.txt").symlink_to(secret)
    # When
    response = await ctx.client.get(
        f"{ctx.url}/{endpoint}", params={"path": "alias.txt", "version_token": "stale"}, headers=ctx.headers
    )
    # Then
    assert response.status_code == 403
    assert b"private" not in response.content


async def test_enterprise_alias_is_tenant_scoped_when_existing_download_policy_allows_it(preview_context):
    # Given
    ctx = preview_context
    key = f"enterprise_info_{ctx.user.tenant_id}/knowledge_base/company.txt"
    await ctx.storage.write_bytes(key, b"current company")
    version = await ctx.storage.get_version(key)
    # When
    response = await ctx.client.get(
        f"{ctx.url}/preview-bytes",
        headers=ctx.headers,
        params={"path": "enterprise_info/knowledge_base/company.txt", "version_token": version.token},
    )
    # Then
    assert response.status_code == 200
    assert response.content == b"current company"
