from typing import Literal

from typing_extensions import TypedDict

from app.dao.analytics_projection_contracts import PlazaContributorProjection, PlazaStatsProjection
from app.services.skill_provider import SkillFilePayload


class OkAcknowledgement(TypedDict):
    ok: Literal[True]


class DeletedAcknowledgement(TypedDict):
    deleted: Literal[True]


class SkillIdentityResponse(TypedDict):
    id: str
    name: str


class SavedSkillResult(SkillIdentityResponse):
    folder_name: str


class UrlSkillInstallResponse(SavedSkillResult):
    tier: int
    file_count: int
    source: Literal["url"]


class ClawhubSkillInstallResponse(SavedSkillResult):
    tier: int
    file_count: int
    source: Literal["clawhub"]
    is_suspicious: bool
    moderation_summary: str
    has_scripts: bool
    archive_source: str


class ClawhubSearchProjection(TypedDict):
    slug: str | None
    displayName: str | None
    summary: str | None
    score: int | float | None
    version: str | None
    updatedAt: int | float | None


class SkillPreviewFile(TypedDict):
    path: str
    size: int


class SkillImportPreview(TypedDict):
    name: str
    description: str
    tier: int
    files: list[SkillPreviewFile]
    total_size: int
    has_scripts: bool


class SkillMetadataResponse(SavedSkillResult):
    description: str
    category: str
    icon: str
    is_builtin: bool


class SkillCatalogEntry(SkillMetadataResponse):
    is_default: bool
    created_at: str | None


class SkillDetailResponse(SkillMetadataResponse):
    files: list[SkillFilePayload]


class SkillTokenStatus(TypedDict):
    configured: bool
    source: Literal["tenant", "env"]
    masked: str
    clawhub_configured: bool
    clawhub_masked: str


class SkillBrowserEntry(TypedDict):
    name: str
    path: str
    is_dir: bool
    size: int


class SkillBrowserContent(TypedDict):
    content: str


class PublishedPageResponse(TypedDict):
    id: str
    short_id: str
    source_path: str
    title: str
    view_count: int
    created_at: str | None
    url: str


type PlazaContributor = PlazaContributorProjection


class PlazaStatsResponse(PlazaStatsProjection):
    pass


class PlazaLikeResponse(TypedDict):
    liked: bool
