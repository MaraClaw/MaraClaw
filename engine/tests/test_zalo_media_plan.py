from uuid import uuid4, uuid5

import pytest
from pydantic import ValidationError

from app.records.channel_media import StoredFile
from app.records.outbound_plan import OutboundChunk, OutboundFileChunk, OutboundPlan, OutboundProvider, OutboundTarget
from app.services.channels.outbound_plan import build_plan


def stored_file() -> StoredFile:
    path = "workspace/uploads/zalo/replies/digest/report.pdf"
    return StoredFile(
        storage_key=f"{uuid4()}/{path}", path=path, name="report.pdf", size=7, sha256="a" * 64, kind="file"
    )


def target() -> OutboundTarget:
    return OutboundTarget(
        provider=OutboundProvider.ZALO, mode="personal_qr", config_id=uuid4(), account=("1",), destination="route"
    )


def test_file_only_and_mixed_plans_round_trip_with_stable_text_first_steps() -> None:
    delivery, file = uuid4(), stored_file()
    only_file = build_plan(target(), "", delivery, files=(file,))
    assert only_file.version == 2
    assert only_file.chunks == (OutboundFileChunk(key=uuid5(delivery, "v2:0"), file=file),)
    mixed = build_plan(only_file.target, "read this", delivery, files=(file, file))
    assert mixed.chunks == (
        OutboundChunk(text="read this", key=uuid5(delivery, "v2:0")),
        OutboundFileChunk(key=uuid5(delivery, "v2:1"), file=file),
        OutboundFileChunk(key=uuid5(delivery, "v2:2"), file=file),
    )
    assert OutboundPlan.model_validate_json(mixed.model_dump_json()) == mixed
    legacy = build_plan(mixed.target, "read this", delivery)
    assert legacy.model_dump(mode="json")["chunks"] == [{"text": "read this", "key": str(uuid5(delivery, "v1:0"))}]
    with pytest.raises(ValidationError):
        OutboundPlan.model_validate({**mixed.model_dump(), "version": 1})


@pytest.mark.parametrize("provider", [p for p in OutboundProvider if p != OutboundProvider.ZALO])
def test_other_providers_cannot_parse_file_plans(provider: OutboundProvider) -> None:
    outbound = target().model_copy(update={"provider": provider})
    with pytest.raises(ValidationError):
        build_plan(outbound, "", uuid4(), files=(stored_file(),))


def test_cloud_zalo_cannot_parse_file_plan() -> None:
    with pytest.raises(ValidationError):
        build_plan(target().model_copy(update={"mode": "cloud"}), "", uuid4(), files=(stored_file(),))
