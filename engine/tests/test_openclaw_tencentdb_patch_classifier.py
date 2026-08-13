import subprocess
from pathlib import Path

import pytest

from openclaw_tencentdb_patch_verifier_fixtures import (
    EXACT_MESSAGES_PROPERTY,
    require_command,
    run_classifier,
    selected_hook_source,
)


def _write_target(tmp_path: Path, name: str, source: str) -> Path:
    target = tmp_path / name
    target.write_text(source, encoding="utf-8")
    return target


def _assert_silent_classifier_failure(target: Path, *, expose_internals: bool = True) -> None:
    result = run_classifier(target, expose_internals=expose_internals)
    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_pinned_node_26_5_0_exposes_expected_acorn_ast() -> None:
    # Given
    probe = """
const acorn = require('internal/deps/acorn/acorn/dist/acorn');
const ast = acorn.parse('const value = ctx.params.session?.messages;', {
  ecmaVersion: 'latest', sourceType: 'module', allowHashBang: true
});
process.stdout.write(`${process.version}\\n${acorn.version}:${ast.type}:${ast.body[0].declarations[0].init.type}\\n`);
"""

    # When
    result = subprocess.run(  # noqa: S603 - pinned host runtime availability probe.
        [require_command("node"), "--expose-internals", "-e", probe],
        capture_output=True,
        text=True,
        check=False,
    )

    # Then
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert len(lines) == 2, result.stdout
    node_ver, ast_line = lines
    if not node_ver.startswith("v26.5.0"):
        pytest.skip(f"pinned Node 26.5.0 not available ({node_ver})")
    assert ast_line.endswith(":Program:ChainExpression")
    assert result.stderr == ""


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (selected_hook_source(), "unpatched"),
        (selected_hook_source("messages    : ctx.params.session?.messages,\n  durationMs"), "prepatched"),
        (selected_hook_source(f"durationMs,\n  {EXACT_MESSAGES_PROPERTY}"), "prepatched"),
        (
            "const after_tool_call = true;\nhookEvent = { durationMs };\nhookRunnerAfter();\n",
            "unpatched",
        ),
        (
            "const after_tool_call = true;\nconst hookEvent = { durationMs };\nawait hookRunner.runAfterToolCall();\n",
            "unpatched",
        ),
    ],
)
def test_classifier_classifies_selected_hook_regardless_of_property_order(
    tmp_path: Path, source: str, expected: str
) -> None:
    # Given
    target = _write_target(tmp_path, "arbitrary-selected-hook.js", source)

    # When
    result = run_classifier(target)

    # Then
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{expected}\n"
    assert result.stderr == ""


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            selected_hook_source() + f"const differentMarker = {{ {EXACT_MESSAGES_PROPERTY} }};\n",
            "unpatched",
        ),
        (
            selected_hook_source()
            + f"const nested = () => {{ const hookEvent = {{ {EXACT_MESSAGES_PROPERTY}, durationMs }}; }};\n",
            "unpatched",
        ),
        (
            "const hookEvent = { durationMs };\n"
            "const after_tool_call = true;\n"
            f"const hook_event = {{ durationMs, {EXACT_MESSAGES_PROPERTY} }};\n"
            "hookRunnerAfter();\n",
            "prepatched",
        ),
    ],
)
def test_classifier_uses_only_the_runner_associated_hook(tmp_path: Path, source: str, expected: str) -> None:
    # Given
    target = _write_target(tmp_path, "arbitrary-association.js", source)

    # When
    result = run_classifier(target)

    # Then
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{expected}\n"
    assert result.stderr == ""


def test_classifier_rejects_prepatched_hook_without_an_immediate_runner(tmp_path: Path) -> None:
    # Given
    target = _write_target(
        tmp_path,
        "arbitrary-runner-gap.js",
        """const after_tool_call = true;
const hookEvent = {
  messages: ctx.params.session?.messages,
  durationMs
};
const unrelated = true;
hookRunnerAfter();
""",
    )

    # When
    result = run_classifier(target)

    # Then
    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr == ""


@pytest.mark.parametrize(
    "messages_property",
    [
        '"messages": ctx.params.session?.messages',
        '["messages"]: ctx.params.session?.messages',
        "messages",
        "messages() {}",
        "get messages() { return ctx.params.session?.messages; }",
        f"{EXACT_MESSAGES_PROPERTY}, {EXACT_MESSAGES_PROPERTY}",
        f"...other, {EXACT_MESSAGES_PROPERTY}",
        f"{EXACT_MESSAGES_PROPERTY}, ...other",
        "messages: capturedMessages",
        "messages: ctx.params.session.messages",
        "messages: ctx?.params.session.messages",
        "messages: ctx.params?.session.messages",
        'messages: ctx.params.session?.["messages"]',
        "messages: ctx.params.session?.messages.extra",
        "messages: getContext().params.session?.messages",
    ],
)
def test_classifier_rejects_invalid_messages_property_shapes(tmp_path: Path, messages_property: str) -> None:
    # Given
    target = _write_target(
        tmp_path, "arbitrary-invalid-property.js", selected_hook_source(f"durationMs,\n  {messages_property}")
    )

    # When
    result = run_classifier(target)

    # Then
    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr == ""


@pytest.mark.parametrize(
    "source",
    [
        "const unrelated = { durationMs };\n",
        """const after_tool_call = true;
const hookEvent = buildHook();
hookRunnerAfter();
""",
        """const after_tool_call = true;
const hookEvent = { durationMs };
hookRunnerAfter();
const hook_event = { durationMs };
hookRunnerAfter();
""",
        "const = invalid;\n",
    ],
)
def test_classifier_fails_closed_for_association_and_syntax_errors(tmp_path: Path, source: str) -> None:
    # Given
    target = _write_target(tmp_path, "arbitrary-invalid-association.js", source)

    # When
    _assert_silent_classifier_failure(target)


def test_classifier_fails_closed_when_acorn_is_not_exposed(tmp_path: Path) -> None:
    # Given
    target = _write_target(tmp_path, "arbitrary-unpatched.js", selected_hook_source())

    # When
    _assert_silent_classifier_failure(target, expose_internals=False)


def test_classifier_fails_closed_when_target_cannot_be_read(tmp_path: Path) -> None:
    # Given
    target = tmp_path / "missing-target.js"

    # When
    _assert_silent_classifier_failure(target)


def test_classifier_parses_but_does_not_execute_the_target(tmp_path: Path) -> None:
    # Given
    target = _write_target(tmp_path, "arbitrary-side-effect.js", f"process.exit(91);\n{selected_hook_source()}")

    # When
    result = run_classifier(target)

    # Then
    assert result.returncode == 0, result.stderr
    assert result.stdout == "unpatched\n"
    assert result.stderr == ""
