import base64
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from app.api import skills
from app.core.json_types import JsonValue


class FakeResponse:
    def __init__(self, payload: JsonValue) -> None:
        self._payload = payload
        self.status_code = 200
        self.headers = {"content-type": "application/json"}

    def json(self) -> JsonValue:
        return self._payload


class FakeAsyncClient:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self._responses = responses

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback) -> bool:
        return False

    async def get(self, _url: str, **_kwargs) -> FakeResponse:
        return self._responses.pop(0)


def _client_factory(responses: list[FakeResponse]):
    def build_client(*_args, **_kwargs):
        return FakeAsyncClient(responses)

    return build_client


async def test_install_from_clawhub_defaults_null_moderation_to_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fetch_metadata(*_args, **_kwargs):
        return {
            "skill": {"displayName": "Research"},
            "owner": {"handle": "acme"},
            "moderation": None,
        }, "https://clawhub.test"

    async def fetch_archive(*_args, **_kwargs):
        return [{"path": "SKILL.md", "content": "# Research"}], "https://clawhub.test"

    async def save_skill(*_args, **_kwargs):
        return {"id": "skill-id", "name": "Research", "folder_name": "research"}

    monkeypatch.setattr(skills, "_fetch_clawhub_skill_meta", fetch_metadata)
    monkeypatch.setattr(skills, "_fetch_clawhub_skill_archive", fetch_archive)
    monkeypatch.setattr(skills, "_save_skill_to_db", save_skill)

    result = await skills.install_from_clawhub(
        skills.ClawhubInstallIn(slug="research"),
        SimpleNamespace(display_name="Test User", tenant_id=None, id=None, role="member"),
    )

    assert result["is_suspicious"] is False
    assert result["moderation_summary"] == ""


async def test_fetch_clawhub_json_parses_valid_fields_and_ignores_malformed_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = [
        FakeResponse(
            {
                "results": [
                    {
                        "slug": "research",
                        "displayName": "Research",
                        "summary": "A research skill",
                        "score": 0.9,
                        "version": "1.0.0",
                        "updatedAt": 1704110400000,
                    },
                    {"slug": "partial", "summary": ["invalid"], "score": True, "updatedAt": "invalid"},
                ],
                "skill": {"displayName": "Research", "summary": ["invalid"]},
                "owner": {"handle": "acme"},
                "moderation": None,
            }
        )
    ]
    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(responses))

    payload, base_url = await skills._fetch_clawhub_json(
        lambda base: f"{base}/search", preferred_base="https://clawhub.test"
    )

    assert base_url == "https://clawhub.test"
    assert payload == {
        "results": [
            {
                "slug": "research",
                "displayName": "Research",
                "summary": "A research skill",
                "score": 0.9,
                "version": "1.0.0",
                "updatedAt": 1704110400000,
            },
            {"slug": "partial"},
        ],
        "skill": {"displayName": "Research"},
        "owner": {"handle": "acme"},
        "moderation": None,
    }


async def test_fetch_clawhub_json_rejects_non_mapping_root(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = [FakeResponse([]), FakeResponse([]), FakeResponse([])]
    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(responses))

    with pytest.raises(HTTPException) as raised:
        await skills._fetch_clawhub_json(lambda base: f"{base}/search", preferred_base="https://clawhub.test")

    assert raised.value.status_code == 502


def test_parse_skill_md_frontmatter_keeps_only_string_fields() -> None:
    frontmatter = skills._parse_skill_md_frontmatter(
        "---\nname: Research\ndescription: Collect findings\nextra: ignored\n---\n# Research"
    )

    assert frontmatter == {"name": "Research", "description": "Collect findings"}


def test_parse_skill_md_frontmatter_defaults_malformed_values() -> None:
    frontmatter = skills._parse_skill_md_frontmatter("---\nname:\n  - Research\ndescription: 3\n---\n# Research")

    assert frontmatter == {}


async def test_fetch_github_directory_preserves_valid_file_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    encoded = base64.b64encode(b"# Research").decode()
    responses = [
        FakeResponse(
            [
                {
                    "name": "SKILL.md",
                    "type": "file",
                    "path": "SKILL.md",
                    "url": "https://github.test/SKILL.md",
                    "size": 10,
                }
            ]
        ),
        FakeResponse({"content": encoded}),
    ]
    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(responses))

    files = await skills._fetch_github_directory("acme", "skills", "", token="token")

    assert files == [{"path": "SKILL.md", "content": "# Research"}]


async def test_fetch_github_directory_rejects_malformed_file_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = [
        FakeResponse(
            [
                {
                    "name": 3,
                    "type": "file",
                    "path": "SKILL.md",
                    "url": "https://github.test/SKILL.md",
                }
            ]
        )
    ]
    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(responses))

    with pytest.raises(HTTPException) as raised:
        await skills._fetch_github_directory("acme", "skills", "", token="token")

    assert raised.value.status_code == 502
