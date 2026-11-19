"""The ILIKE candidate patterns behind Clawbook @mention resolution.

SQL only has to return a superset of the agents and users a token names: Python's ``name.lower() in tokens`` is the
deciding rule. These tests model PostgreSQL's ``ILIKE`` under the foldings a database can use (``C`` collation, libc
Unicode, ICU, Turkish and Lithuanian ICU) and prove that every name Python lowercases to a token matches the token's
pattern.
"""

import random
import sys
import unicodedata
from collections.abc import Callable

import pytest

from app.dao.mention_patterns import like_patterns

type Fold = Callable[[str], str]

DOTTED_CAPITAL_I = "\u0130"
DOTLESS_SMALL_I = "\u0131"
COMBINING_DOT_ABOVE = "\u0307"
KELVIN_SIGN = "\u212a"
ANGSTROM_SIGN = "\u212b"
OHM_SIGN = "\u2126"
CAPITAL_SIGMA = "\u03a3"
FINAL_SMALL_SIGMA = "\u03c2"

_ASCII_FOLD = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
_LIKE_METACHARACTERS = frozenset("%_\\")
_SURROGATES = range(0xD800, 0xE000)


def _fold_c_collation(text: str) -> str:
    """``C`` collation: lower() folds ASCII letters only."""
    return text.translate(_ASCII_FOLD)


def _simple_lower(char: str) -> str:
    """One-to-one lowercase like libc ``towlower``: U+0130 becomes a plain ``i``."""
    lowered = char.lower()
    return lowered[0] if len(lowered) > 1 else lowered


def _fold_libc_unicode(text: str) -> str:
    return "".join(_simple_lower(char) for char in text)


def _fold_icu(text: str) -> str:
    """Whole-string lowering as ICU and Python do it: final sigma, and U+0130 becomes ``i`` + U+0307."""
    return text.lower()


def _next_mark_above(text: str, index: int) -> str | None:
    """The first following character of combining class 230 (above) with no base or other above mark before it."""
    for char in text[index + 1 :]:
        combining_class = unicodedata.combining(char)
        if combining_class == 230:
            return char
        if combining_class == 0:
            return None
    return None


def _fold_turkish(text: str) -> str:
    """SpecialCasing ``tr``/``az``: I lowers to a dotless i unless a dot above follows, which is then dropped."""
    folded: list[str] = []
    after_capital_i = False
    for index, char in enumerate(text):
        if char == COMBINING_DOT_ABOVE and after_capital_i:
            after_capital_i = False
            continue
        if char == "I":
            folded.append("i" if _next_mark_above(text, index) == COMBINING_DOT_ABOVE else DOTLESS_SMALL_I)
            after_capital_i = True
            continue
        if unicodedata.combining(char) in (0, 230):
            after_capital_i = False
        folded.append("i" if char == DOTTED_CAPITAL_I else _simple_lower(char))
    return "".join(folded)


_LITHUANIAN_ACCENTED_I = {"\u00cc": "i\u0307\u0300", "\u00cd": "i\u0307\u0301", "\u0128": "i\u0307\u0303"}


def _fold_lithuanian(text: str) -> str:
    """SpecialCasing ``lt``: I, J and Į keep their dot before an accent above, and Ì, Í and Ĩ expand to it."""
    folded: list[str] = []
    for index, char in enumerate(text):
        if char in _LITHUANIAN_ACCENTED_I:
            folded.append(_LITHUANIAN_ACCENTED_I[char])
        elif char in "IJ\u012e" and _next_mark_above(text, index) is not None:
            folded.append(char.lower() + COMBINING_DOT_ABOVE)
        else:
            folded.append(char.lower())
    return "".join(folded)


_FOLDS: dict[str, Fold] = {
    "c": _fold_c_collation,
    "libc": _fold_libc_unicode,
    "icu": _fold_icu,
    "turkish": _fold_turkish,
    "lithuanian": _fold_lithuanian,
}


def _like(text: str, pattern: str) -> bool:
    """PostgreSQL LIKE with its default backslash escape: ``%`` is any run, ``_`` any single character."""
    parts: list[tuple[str, str]] = []
    escaped = False
    for char in pattern:
        if escaped:
            parts.append(("char", char))
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "%":
            parts.append(("any", ""))
        elif char == "_":
            parts.append(("one", ""))
        else:
            parts.append(("char", char))
    assert not escaped, f"dangling escape in {pattern!r}"
    positions = {0}
    for kind, char in parts:
        reached: set[int] = set()
        for position in positions:
            if kind == "any":
                reached.update(range(position, len(text) + 1))
            elif position < len(text) and (kind == "one" or text[position] == char):
                reached.add(position + 1)
        positions = reached
        if not positions:
            return False
    return len(text) in positions


def _ilike(name: str, pattern: str, fold: Fold) -> bool:
    """ILIKE folds the name and the pattern with the same lower(), then applies LIKE."""
    return _like(fold(name), fold(pattern))


def _pattern(token: str) -> str:
    patterns = like_patterns([token])
    assert len(patterns) == 1
    return patterns[0]


def _unmatched(name: str) -> list[str]:
    """Foldings under which the pattern built from ``name.lower()`` fails to match ``name``."""
    pattern = _pattern(name.lower())
    if pattern == name and _LIKE_METACHARACTERS.isdisjoint(pattern):
        return []  # identical text folds identically under any folding, so a literal pattern equal to the name matches
    return [label for label, fold in _FOLDS.items() if not _ilike(name, pattern, fold)]


def test_like_metacharacters_are_escaped() -> None:
    assert like_patterns(["a%b_c\\d"]) == ["a\\%b\\_c\\\\d"]


def test_dotted_capital_i_lowercase_is_a_wildcard() -> None:
    assert DOTTED_CAPITAL_I.lower() == "i" + COMBINING_DOT_ABOVE
    assert like_patterns(["i" + COMBINING_DOT_ABOVE + "stanbul"]) == ["%stanbul"]
    assert like_patterns(["xi" + COMBINING_DOT_ABOVE + "y"]) == ["x%y"]
    assert like_patterns(["e" + COMBINING_DOT_ABOVE]) == ["e%"]


def test_the_i_family_and_j_are_runs_and_k_a_single_character_wildcard() -> None:
    # Turkish drops a dot after I and Lithuanian adds one after i, j and į, so their lowered length can differ.
    assert like_patterns(["kim"]) == ["_%m"]
    assert like_patterns(["jik"]) == ["%_"]
    assert like_patterns(["trí", "ìgnas", "įj"]) == ["%", "%gnas", "tr%"]


def test_other_ascii_characters_stay_literal() -> None:
    assert like_patterns(["bob-2.0"]) == ["bob-2.0"]


def test_caseless_non_ascii_text_stays_literal() -> None:
    assert like_patterns(["田中太郎"]) == ["田中太郎"]
    assert like_patterns(["한국"]) == ["한국"]


def test_cased_non_ascii_letters_become_single_character_wildcards() -> None:
    assert like_patterns(["đức"]) == ["__c"]
    assert like_patterns(["\u03b4" + FINAL_SMALL_SIGMA]) == ["__"]


def test_one_sorted_pattern_per_distinct_token() -> None:
    assert like_patterns(["bob", "ana", "bob"]) == ["ana", "bob"]
    assert like_patterns(["đa", "ứa"]) == ["_a"]
    assert like_patterns([]) == []


def test_patterns_still_discriminate_between_names() -> None:
    for fold in _FOLDS.values():
        assert not _ilike("alice", _pattern("bob"), fold)
        assert not _ilike("鈴木", _pattern("田中"), fold)
        assert not _ilike("bobby", _pattern("bob"), fold)


@pytest.mark.parametrize(
    ("name", "fold_label"),
    [
        (KELVIN_SIGN + "elvin", "c"),
        (DOTTED_CAPITAL_I, "libc"),
        (DOTTED_CAPITAL_I, "c"),
        ("I", "turkish"),
        ("\u0394" + CAPITAL_SIGMA, "libc"),
        ("Đ", "c"),
        ("I\u0323" + COMBINING_DOT_ABOVE, "turkish"),
        ("TR\u00cd", "lithuanian"),
        ("J\u0301", "lithuanian"),
    ],
)
def test_a_literal_pattern_misses_names_the_wildcards_catch(name: str, fold_label: str) -> None:
    literal_token = name.lower()
    assert not _ilike(name, literal_token, _FOLDS[fold_label])
    assert _unmatched(name) == []


def test_every_code_point_lowercase_matches_the_code_point_under_every_ilike_model() -> None:
    failures: list[tuple[str, list[str]]] = []
    for code_point in range(sys.maxunicode + 1):
        if code_point in _SURROGATES:
            continue
        char = chr(code_point)
        if missed := _unmatched(char):
            failures.append((f"U+{code_point:04X}", missed))
        # A caseless code point cannot lowercase differently in any context. The cased ones are rechecked after a
        # cased letter, which exposes Python's one contextual rule, final sigma.
        if (char.lower() != char or char.upper() != char) and (missed := _unmatched("a" + char)):
            failures.append((f"U+{code_point:04X} after a letter", missed))
    assert failures == []


_ALPHABET = (
    "abAB09kKiIsSnN%_\\.,-"
    + DOTTED_CAPITAL_I
    + DOTLESS_SMALL_I
    + "ßẞ\u03a3\u03c3\u03c2\u0394\u03b4"
    + KELVIN_SIGN
    + ANGSTROM_SIGN
    + OHM_SIGN
    + "ĐđỨứỪừỄễ"
    + "JjĮįÌìÍíĨĩ"
    + COMBINING_DOT_ABOVE
    + "\u0301\u0323"
    + "田中한😀"
)


def test_random_names_match_the_pattern_of_their_lowercase() -> None:
    rng = random.Random(20261119)
    failures: list[tuple[str, list[str]]] = []
    for _ in range(4000):
        name = "".join(rng.choice(_ALPHABET) for _ in range(rng.randint(1, 8)))
        if missed := _unmatched(name):
            failures.append((name, missed))
    assert failures == []


@pytest.mark.parametrize(
    "name",
    [
        "Bob",
        "Đức",
        "Nguyễn",
        "NGUYỄN",
        DOTTED_CAPITAL_I + "stanbul",
        "Istanbul",
        KELVIN_SIGN + "elvin",
        "\u039b\u0394" + CAPITAL_SIGMA,
        "Straße",
        "田中",
        "50%_off",
        "a\\b",
        "i" + COMBINING_DOT_ABOVE,
        "TRÍ",
        "Ìgnas",
    ],
)
def test_every_name_lowercasing_to_the_token_matches_the_pattern(name: str) -> None:
    variants = {name, name.lower(), name.upper(), name.title(), name.swapcase()}
    same_token = {variant for variant in variants if variant.lower() == name.lower()}
    assert {name, name.lower()} <= same_token
    assert [variant for variant in same_token if _unmatched(variant)] == []
