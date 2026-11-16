from uuid import UUID, uuid5

import pytest
from pydantic import ValidationError

from app.records.outbound_plan import OutboundChunk, OutboundPlan, OutboundProvider, OutboundTarget
from app.services.channels.outbound_plan import build_plan, split_chunks


@pytest.mark.parametrize(
    "expected",
    [
        pytest.param(("a" * 1200 + "\n", "b" * 500 + " " + "c" * 500), id="newline-before-later-space"),
        pytest.param(("a" * 1200 + " ", "b" * 900), id="wordspace"),
        pytest.param(("head\n" + "a" * 1494 + " ", "b" * 900), id="ignore-early-newline"),
        pytest.param(("a" * 1999 + "\n", "b" * 2), id="newline-at-limit"),
        pytest.param(("a" * 1999 + " ", "b" * 2), id="space-at-limit"),
        pytest.param((" \t" + "a" * 1196 + "\r\n", "b" * 900 + " \t\n"), id="retain-crlf-and-padding"),
        pytest.param(("\U0001f642" * 2000, "\u754c" * 20), id="hard-codepoint-limit"),
    ],
)
def test_zalo_prefers_readable_boundaries_without_changing_content(expected: tuple[str, ...]) -> None:
    text = "".join(expected)

    chunks = split_chunks(text, OutboundProvider.ZALO)

    assert chunks == expected
    assert "".join(chunks) == text
    assert all(chunk.strip() and len(chunk) <= 2000 for chunk in chunks)


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("a" * 2000 + " \n", id="short-whitespace-tail"),
        pytest.param(" " * 1999 + "ab" + " " * 1999, id="leading-and-trailing-limit"),
        pytest.param("aa" + " " * 3998 + "b", id="interior-needs-three-nonblank-chunks"),
        pytest.param("a" * 1500 + "\n" + " " * 2000 + "b", id="skip-infeasible-readable-cut"),
        pytest.param((" \n\t\u754c\U0001f642e\u0301\r\n" * 1000) + " \t", id="mixed-unicode-and-whitespace"),
    ],
)
def test_zalo_preserves_feasible_whitespace_without_blank_chunks(text: str) -> None:
    chunks = split_chunks(text, OutboundProvider.ZALO)

    assert "".join(chunks) == text
    assert all(chunk.strip() and len(chunk) <= 2000 for chunk in chunks)


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(" " * 2000 + "a", id="leading-whitespace-exceeds-capacity"),
        pytest.param("a" + " " * 2000, id="trailing-whitespace-exceeds-capacity"),
        pytest.param("a" + " " * 3999 + "b", id="interior-whitespace-exceeds-capacity"),
        pytest.param(" " * 1000 + "a" + " " * 1000, id="one-anchor-cannot-fill-two-chunks"),
        pytest.param("", id="empty"),
        pytest.param(" \n\t" * 1000, id="only-whitespace"),
    ],
)
def test_zalo_rejects_content_that_cannot_form_lossless_nonblank_chunks(text: str) -> None:
    with pytest.raises(ValueError):
        split_chunks(text, OutboundProvider.ZALO)


@pytest.mark.parametrize(
    ("provider", "limit"),
    [(OutboundProvider.TELEGRAM, 4096), (OutboundProvider.DISCORD, 2000)],
)
def test_other_providers_keep_hard_cuts_despite_readable_boundaries(provider: OutboundProvider, limit: int) -> None:
    text = "a" * 1200 + "\n" + "b" * 500 + " " + "c" * 3000

    chunks = split_chunks(text, provider)

    assert chunks == tuple(text[start : start + limit] for start in range(0, len(text), limit))


def test_zalo_plan_keeps_stable_chunk_keys_and_frozen_persisted_text() -> None:
    delivery_id = UUID("00000000-0000-0000-0000-000000000001")
    target = OutboundTarget(
        provider=OutboundProvider.ZALO,
        mode="personal_qr",
        config_id=UUID("00000000-0000-0000-0000-000000000002"),
        account=("account", "epoch", "generation"),
        destination="zalo-destination",
    )
    expected = ("a" * 1200 + "\n", "b" * 900)
    text = "".join(expected)

    plan = build_plan(target, text, delivery_id)

    assert plan.chunks == tuple(
        OutboundChunk(text=text, key=uuid5(delivery_id, f"v1:{index}")) for index, text in enumerate(expected)
    )
    assert build_plan(target, text, delivery_id) == plan
    persisted = OutboundPlan.model_validate_json(plan.model_dump_json())
    assert persisted == plan
    with pytest.raises(ValidationError, match="frozen"):
        persisted.chunks[0].__setattr__("text", "changed")
