import pytest

from app.config import get_settings
from app.services.response_identity import ResponseGuard, identity_prompt

REF = "openrouter/nvidia/nemotron-3.5-super-vl:free"
BRAND = get_settings().APP_NAME


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (REF, BRAND),
        (f"I am running on {REF}.", f"I am running on {BRAND}."),
        (f"Model: `{REF.upper()}`, thanks", f"Model: `{BRAND}`, thanks"),
        ("it uses nvidia/nemotron-3.5-super-vl:free", f"it uses {BRAND}"),
        ("it is nemotron-3.5-super-vl (free)", f"it is {BRAND} (free)"),
        (f"first {REF}\nsecond {REF}", f"first {BRAND}\nsecond {BRAND}"),
        ("OpenRouter and NVIDIA are providers; Nemotron is a model family.", None),
        ("GPT-4o, Claude and Gemini are popular models; see https://openrouter.ai/models", None),
        ("nemotron-3.5-super-vl-pro and xnemotron-3.5-super-vl are different names", None),
        ("", None),
    ],
)
def test_configured_ids_are_concealed_and_ordinary_text_is_not(text, expected):
    assert ResponseGuard.build([REF]).redact(text) == (text if expected is None else expected)


def test_only_identifier_shaped_aliases_are_derived():
    guard = ResponseGuard.build(["openai/o3", "meta/llama", None, ""])
    assert guard.redact("o3 and llama and meta are words; openai/o3 is a ref") == (
        f"o3 and llama and meta are words; {BRAND} is a ref"
    )
    assert ResponseGuard.build([None, "ab"]).pattern is None


@pytest.mark.parametrize("brand", ["Acme Assist", ""])
def test_replacement_is_the_configured_app_name(monkeypatch, brand):
    monkeypatch.setattr(get_settings(), "APP_NAME", brand)
    guard = ResponseGuard.build([REF])
    assert guard.redact(f"on {REF}") == f"on {brand or 'the assistant'}"
    assert (brand or "the assistant") in identity_prompt()


def _stream(guard, pieces):
    redactor = guard.stream()
    return [redactor.feed(seq, piece, False) for seq, piece in enumerate(pieces)]


def test_any_split_of_the_stream_matches_one_shot_redaction_without_leaking_a_prefix():
    guard = ResponseGuard.build([REF])
    text = f"Hi. I run on {REF}; also nvidia/nemotron-3.5-super-vl:free, nemotron-3.5-super-vl. Done  "
    expected = guard.redact(text)
    for first in range(1, len(text)):
        for second in range(first, len(text) + 1):
            out = _stream(guard, [text[:first], text[first:second], text[second:]])
            assert "".join(part or "" for part in out) == expected, (first, second)


def test_hold_back_is_bounded_and_ordinary_text_flows_immediately():
    guard = ResponseGuard.build([REF])
    redactor = guard.stream()
    plain = "ordinary words, nothing to hide "
    assert redactor.feed(0, plain, False) == plain
    junk = "nemotron-3.5-super-v"
    emitted = ""
    for seq in range(1, 201):
        emitted += redactor.feed(seq, junk, False) or ""
    assert 0 <= 200 * len(junk) - len(emitted) <= guard.longest + 1
    assert redactor.feed(201, " ", False) is not None
    assert redactor.feed(202, "n", False) is None
    assert redactor.feed(203, "emotron-3.5-super-vl:free ", False) == f"{BRAND} "


def test_dropped_delta_withholds_appends_until_a_replace_snapshot_resynchronizes():
    guard = ResponseGuard.build([REF])
    redactor = guard.stream()
    assert redactor.feed(0, "start ", False) == "start "
    assert redactor.feed(2, f"uper-vl:free {REF}", False) is None
    assert redactor.feed(3, "more", False) is None
    assert redactor.feed(4, f"snapshot {REF} tail ", True) == f"snapshot {BRAND} tail "
    assert redactor.feed(5, " after", False) == " after"
    assert redactor.feed(6, "nemotron", True) == ""
