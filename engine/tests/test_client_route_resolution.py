"""Fail-closed extraction of frontend engine calls and dispatch-order resolution."""

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Final, Literal, override

import pytest
from fastapi import FastAPI
from starlette.routing import Match

from app.main import app
from route_dispatch import EffectiveRoute, Protocol, effective_routes, resolve

PLACEHOLDER: Final = "00000000-0000-4000-8000-000000000000"
HELPERS: Final = {"apiRequest", "apiFormRequest", "wsUrl"}


@dataclass(frozen=True, slots=True)
class Part:
    text: str
    expression: bool = False
    tokens: tuple[Token, ...] = ()


@dataclass(frozen=True, slots=True)
class Token:
    text: str
    kind: Literal["word", "punct", "literal", "regex"]
    line: int
    start: int
    end: int
    parts: tuple[Part, ...] = ()


@dataclass(slots=True)
class ExtractionError(ValueError):
    file: str
    line: int
    reason: str

    @override
    def __str__(self) -> str:
        return f"{self.file}:{self.line}: {self.reason}"


class Lexer:
    """A cursor tokenizer; template-expression tokens stay on their Part, outside the outer stream."""

    def __init__(self, source: str, file: str) -> None:
        self.source = source
        self.file = file
        self.pos = 0

    def error(self, reason: str) -> ExtractionError:
        return ExtractionError(self.file, self.source.count("\n", 0, self.pos) + 1, reason)

    def literal(self) -> tuple[Part, ...]:
        quote = self.source[self.pos]
        self.pos += 1
        parts = []
        text = ""
        while self.pos < len(self.source):
            char = self.source[self.pos]
            if char == quote:
                self.pos += 1
                return (*parts, Part(text))
            if char == "\\":
                self.pos += 1
                if self.pos == len(self.source):
                    break
                escaped = self.source[self.pos]
                text += {"n": "\n", "r": "\r", "t": "\t"}.get(escaped, escaped)
                self.pos += 1
                continue
            if quote == "`" and self.source.startswith("${", self.pos):
                parts.append(Part(text))
                text = ""
                self.pos += 2
                start = self.pos
                tokens = self.scan(expression=True)
                parts.append(Part(self.source[start : self.pos - 1], True, tuple(tokens)))
                continue
            text += char
            self.pos += 1
        raise self.error("unterminated string/template literal")

    def scan(self, *, expression: bool = False) -> list[Token]:
        tokens = []
        depth = 0
        while self.pos < len(self.source):
            start = self.pos
            char = self.source[start]
            if char.isspace():
                self.pos += 1
                continue
            if self.source.startswith("//", start):
                end = self.source.find("\n", start)
                self.pos = len(self.source) if end < 0 else end
                continue
            if self.source.startswith("/*", start):
                end = self.source.find("*/", start + 2)
                if end < 0:
                    raise self.error("unterminated block comment")
                self.pos = end + 2
                continue
            if expression and char == "}" and depth == 0:
                self.pos += 1
                return tokens
            line = self.source.count("\n", 0, start) + 1
            parts: tuple[Part, ...] = ()
            kind: Literal["word", "punct", "literal", "regex"] = "punct"
            if char in "'\"`":
                parts = self.literal()
                kind = "literal"
            elif char == "/" and (
                not tokens
                or tokens[-1].text in {"=", "(", "[", "{", ",", ":", ";", "!", "?", "return", "=>", "&&", "||"}
            ):
                kind = "regex"
                self.pos += 1
                in_class = False
                while self.pos < len(self.source):
                    current = self.source[self.pos]
                    self.pos += 1
                    if current == "\\":
                        self.pos += 1
                    elif current == "[":
                        in_class = True
                    elif current == "]":
                        in_class = False
                    elif current == "/" and not in_class:
                        break
                else:
                    raise self.error("unterminated regex literal")
                while self.pos < len(self.source) and self.source[self.pos].isalpha():
                    self.pos += 1
            elif char.isalnum() or char in "_$":
                kind = "word"
                self.pos += 1
                while self.pos < len(self.source) and (
                    self.source[self.pos].isalnum() or self.source[self.pos] in "_$"
                ):
                    self.pos += 1
            else:
                operator = next(
                    (
                        op
                        for op in ("...", "=>", "?.", "??", "&&", "||", "===", "!==", "==", "!=")
                        if self.source.startswith(op, start)
                    ),
                    char,
                )
                self.pos += len(operator)
                depth += (operator == "{") - (operator == "}")
            tokens.append(Token(self.source[start : self.pos], kind, line, start, self.pos, parts))
        if expression:
            raise self.error("unterminated template expression")
        return tokens


def group_end(tokens: list[Token], start: int) -> int:
    pairs = {"(": ")", "[": "]", "{": "}", "<": ">"}
    closing = pairs[tokens[start].text]
    depth = 1
    for index in range(start + 1, len(tokens)):
        depth += (tokens[index].text == tokens[start].text) - (tokens[index].text == closing)
        if depth == 0:
            return index
    raise ExtractionError("<tokens>", tokens[start].line, f"unclosed {tokens[start].text}")


def top_level(tokens: list[Token]) -> list[int]:
    result = []
    index = 0
    while index < len(tokens):
        result.append(index)
        if tokens[index].text in {"(", "[", "{"}:
            index = group_end(tokens, index)
        index += 1
    return result


def split_tokens(tokens: list[Token], delimiter: str) -> list[list[Token]]:
    cuts = [-1, *(i for i in top_level(tokens) if tokens[i].text == delimiter), len(tokens)]
    return [tokens[left + 1 : right] for left, right in pairwise(cuts)]


def statement(tokens: list[Token], start: int, end: int) -> list[Token]:
    index = start
    while index < end:
        if tokens[index].text in {";", ")", "]", "}"}:
            break
        if (
            index > start
            and tokens[index].line > tokens[index - 1].line
            and tokens[index - 1].text not in {"?", ":", "=", ",", "&&", "||"}
            and tokens[index].text not in {"?", ":"}
        ):
            break
        if tokens[index].text in {"(", "[", "{"}:
            index = group_end(tokens, index)
        index += 1
    return tokens[start:index]


def static_text(token: Token) -> str:
    return "".join(part.text for part in token.parts if not part.expression)


def trim_empty_tail(parts: list[Part]) -> None:
    while parts and not parts[-1].expression and parts[-1].text == "":
        parts.pop()


def template_tokens(tokens: Iterable[Token]) -> Iterator[Token]:
    """Every token inside template expressions, at any nesting depth."""
    for token in tokens:
        for part in token.parts:
            yield from part.tokens
            yield from template_tokens(part.tokens)


def literal_alternatives(tokens: list[Token]) -> list[Token] | None:
    if len(tokens) == 1 and tokens[0].kind == "literal":
        return tokens
    indices = top_level(tokens)
    questions = [i for i in indices if tokens[i].text == "?"]
    colons = [i for i in indices if tokens[i].text == ":"]
    if len(questions) == len(colons) == 1 and questions[0] < colons[0]:
        left = literal_alternatives(tokens[questions[0] + 1 : colons[0]])
        right = literal_alternatives(tokens[colons[0] + 1 :])
        if left is not None and right is not None:
            return left + right
    return None


@dataclass(frozen=True, slots=True)
class Function:
    name: str
    body: int
    end: int


def functions(tokens: list[Token]) -> list[Function]:
    result = []
    for index, token in enumerate(tokens):
        if token.text != "function" or index + 2 >= len(tokens):
            continue
        name = tokens[index + 1].text
        cursor = index + 2
        if tokens[cursor].text == "<":
            cursor = group_end(tokens, cursor) + 1
        if tokens[cursor].text != "(":
            continue
        cursor = group_end(tokens, cursor) + 1
        while cursor < len(tokens) and tokens[cursor].text != "{":
            if tokens[cursor].text in {"<", "[", "("}:
                cursor = group_end(tokens, cursor)
            cursor += 1
        if cursor < len(tokens):
            end = group_end(tokens, cursor)
            if end + 1 < len(tokens) and tokens[end + 1].text == "{":
                cursor = end + 1
                end = group_end(tokens, cursor)
            result.append(Function(name, cursor, end))
    for index, token in enumerate(tokens[:-1]):
        if token.text == "=>" and tokens[index + 1].text == "{":
            result.append(Function("<arrow>", index + 1, group_end(tokens, index + 1)))
    return result


@dataclass(frozen=True, slots=True)
class ClientCall:
    protocol: Protocol
    method: str
    path: str
    file: str
    line: int
    raw: str
    function: str
    expansion_reason: str = ""
    resolution_reason: str = ""


@dataclass(frozen=True, slots=True)
class Expansion:
    file: str
    functions: frozenset[str]
    webhook_functions: frozenset[str]
    expression: str
    reason: str


CHANNEL_EXPANSION: Final = Expansion(
    "web-e/src/lib/workspace-api.ts",
    frozenset({"getChannel", "saveChannel", "deleteChannel", "getChannelWebhook"}),
    frozenset({"getChannelWebhook"}),
    "path",
    "Channel helpers receive a route segment from CHANNELS[].path; probe every declared segment, and probe "
    "getChannelWebhook only for entries with webhook: true because agent-channels.tsx gates that query on it",
)


class Extractor:
    """Parse only statically provable request shapes; unsupported shapes are errors, not omissions."""

    def __init__(self, source: str, file: str) -> None:
        self.file = file
        self.tokens = Lexer(source, file).scan()
        self.functions = functions(self.tokens)

    def error(self, token: Token, reason: str) -> ExtractionError:
        return ExtractionError(self.file, token.line, reason)

    def enclosing(self, index: int) -> Function | None:
        candidates = [fn for fn in self.functions if fn.body < index < fn.end]
        return max(candidates, key=lambda fn: fn.body) if candidates else None

    def const_value(self, name: str, fn: Function, index: int) -> list[Token] | None:
        """Initializer of the const binding of name visible at index; None when there is none."""
        tokens = self.tokens
        scopes = [
            (opening, group_end(tokens, opening)) for opening in range(fn.body, index) if tokens[opening].text == "{"
        ]
        declarations = [
            cursor
            for cursor in range(fn.body + 1, index)
            if tokens[cursor].text == "const"
            and [t.text for t in tokens[cursor + 1 : cursor + 3]] == [name, "="]
            and self.enclosing(cursor) == fn
            and not any(opening < cursor < end and not opening < index < end for opening, end in scopes)
        ]
        if not declarations:
            return None
        references = [
            cursor
            for cursor in range(fn.body + 1, fn.end)
            if tokens[cursor].kind == "word"
            and tokens[cursor].text == name
            and tokens[cursor - 1].text not in {".", "?."}
        ]
        if references != [declarations[-1] + 1]:
            raise self.error(tokens[index], f"query suffix {name!r} must be one const binding read only in templates")
        return statement(tokens, declarations[-1] + 3, index)

    def returned_values(self, name: str, index: int) -> list[list[Token]] | None:
        """Every value a local function can return; None when name is not declared in this file."""
        declared = [fn for fn in self.functions if fn.name == name]
        if not declared:
            return None
        if len(declared) > 1:
            raise self.error(self.tokens[index], f"query suffix function {name!r} is declared more than once")
        fn = declared[0]
        body = self.tokens[fn.body + 1 : fn.end]
        if not any(body[cursor].text == "return" for cursor in top_level(body)):
            raise self.error(self.tokens[index], f"query suffix function {name!r} may fall through without returning")
        return [
            statement(self.tokens, cursor + 1, fn.end)
            for cursor in range(fn.body + 1, fn.end)
            if self.tokens[cursor].text == "return" and self.enclosing(cursor) == fn
        ]

    def query_suffix(self, part: Part, index: int) -> bool:
        """Whether a trailing expression provably adds only a query; False when its value is not literal."""
        expr = list(part.tokens)
        fn = self.enclosing(index)
        value: list[list[Token]] | None = None
        if len(expr) == 1 and expr[0].kind == "word" and fn is not None:
            initializer = self.const_value(expr[0].text, fn, index)
            value = None if initializer is None else [initializer]
        elif len(expr) >= 3 and expr[0].kind == "word" and expr[1].text == "(" and group_end(expr, 1) == len(expr) - 1:
            value = self.returned_values(expr[0].text, index)
        alternatives = [literal_alternatives(item) for item in value or []]
        if not alternatives or any(item is None for item in alternatives):
            return False
        for items in alternatives:
            assert items is not None
            for token in items:
                # parts[0] is always the static text before any interpolation.
                if not token.parts[0].text.startswith("?") and token.parts != (Part(""),):
                    raise self.error(
                        self.tokens[index],
                        f"query suffix {part.text.strip()!r} has a non-query alternative {token.text}",
                    )
        return True

    def channels(self) -> list[tuple[str, bool]]:
        """Return (path, webhook) for each CHANNELS entry; both must be literals."""
        tokens = self.tokens
        for index in range(len(tokens) - 4):
            if [t.text for t in tokens[index : index + 5]] == ["export", "const", "CHANNELS", "=", "["]:
                end = group_end(tokens, index + 4)
                assert [t.text for t in tokens[end + 1 : end + 3]] == ["as", "const"], "CHANNELS must be as const"
                values = []
                for entry in split_tokens(tokens[index + 5 : end], ","):
                    if not entry:
                        continue
                    if entry[0].text != "{" or group_end(entry, 0) != len(entry) - 1:
                        raise self.error(entry[0], "CHANNELS entry must be an object literal")
                    props = {prop[0].text: prop for prop in split_tokens(entry[1:-1], ",") if prop}
                    path = props.get("path")
                    if path is None or len(path) != 3 or path[1].text != ":" or path[2].kind != "literal":
                        raise self.error(entry[0], "CHANNELS.path must be literal")
                    if any(part.expression for part in path[2].parts):
                        raise self.error(path[2], "CHANNELS.path must be static")
                    webhook = props.get("webhook")
                    if webhook is None or [t.text for t in webhook[1:]] not in ([":", "true"], [":", "false"]):
                        raise self.error(entry[0], "CHANNELS.webhook must be a literal true or false")
                    values.append(("".join(part.text for part in path[2].parts), webhook[2].text == "true"))
                assert values, "CHANNELS has no literal paths"
                return values
        raise self.error(tokens[0], "channel expansion requires export const CHANNELS")

    def paths(self, index: int) -> list[tuple[str, str]]:
        token = self.tokens[index]
        parts = []
        for part in token.parts:
            if not part.expression and "?" in part.text:
                parts.append(Part(part.text.split("?", 1)[0]))
                break
            parts.append(part)
        trim_empty_tail(parts)
        if parts and parts[-1].expression and self.query_suffix(parts[-1], index):
            parts.pop()
            trim_empty_tail(parts)
        fn = self.enclosing(index)
        paths = [""]
        reason = ""
        for position, part in enumerate(parts):
            values = [part.text]
            if part.expression:
                following = parts[position + 1].text if position + 1 < len(parts) else "/"
                if not parts[position - 1].text.endswith("/") or not following.startswith("/"):
                    raise self.error(
                        token, f"${{{part.text.strip()}}} is neither a whole path segment nor a provable query suffix"
                    )
                values = [PLACEHOLDER]
                if (
                    self.file == CHANNEL_EXPANSION.file
                    and fn is not None
                    and fn.name in CHANNEL_EXPANSION.functions
                    and part.text.strip() == CHANNEL_EXPANSION.expression
                ):
                    gated = fn.name in CHANNEL_EXPANSION.webhook_functions
                    values = [path for path, webhook in self.channels() if webhook or not gated]
                    reason = CHANNEL_EXPANSION.reason
            paths = [prefix + value for prefix in paths for value in values]
        return [(path, reason) for path in paths]

    def methods(self, helper: str, args: list[list[Token]], token: Token) -> list[str]:
        from client_route_context import routing_neutral_context

        option_index = 2 if helper == "apiFormRequest" else 1
        default = "POST" if helper == "apiFormRequest" else "GET"
        if len(args) <= option_index or not args[option_index]:
            return [default]
        options = args[option_index]
        if options[0].text != "{" or group_end(options, 0) != len(options) - 1:
            if routing_neutral_context(self, options):
                return [default]
            raise self.error(token, "request options must be an object literal")
        method = None
        body = False
        for prop in split_tokens(options[1:-1], ","):
            if not prop:
                continue
            if prop[0].text == "...":
                if routing_neutral_context(self, prop[1:]):
                    continue
                raise self.error(token, "top-level spread in request options")
            if prop[0].text == "[":
                close = group_end(prop, 0)
                computed = prop[1:close]
                if len(computed) != 1 or computed[0].kind != "literal" or len(computed[0].parts) != 1:
                    raise self.error(token, "computed request option keys must be static string literals")
                key, rest = static_text(computed[0]), prop[close + 1 :]
            elif prop[0].kind in {"word", "literal"}:
                key, rest = static_text(prop[0]) if prop[0].kind == "literal" else prop[0].text, prop[1:]
            else:
                raise self.error(token, f"unsupported request option key {prop[0].text}")
            if rest and rest[0].text == ":":
                value = rest[1:]
            elif not rest and prop[0].kind == "word":
                value = prop
            else:
                raise self.error(token, f"unsupported request option property {key!r}")
            if key == "method":
                alternatives = literal_alternatives(value)
                if alternatives is None or any(t.text.startswith("`") for t in alternatives):
                    raise self.error(token, "method must be a string literal or literal conditional")
                method = ["".join(p.text for p in item.parts).upper() for item in alternatives]
            if key == "body":
                body = [t.text for t in value] != ["undefined"]
        return method if method is not None else ["POST" if body else default]

    def specifier(self, index: int) -> bool:
        """Whether tokens[index] is a plain name in an import/export specifier list."""
        tokens = self.tokens
        if not 0 < index < len(tokens) - 1:
            return False
        if tokens[index - 1].text not in {"{", ","} or tokens[index + 1].text not in {",", "}", "as"}:
            return False
        cursor = index - 1
        while cursor > 0 and tokens[cursor].text != "{":
            if tokens[cursor].kind != "word" and tokens[cursor].text != ",":
                return False
            cursor -= 1
        if cursor == 0 or tokens[cursor - 1].text not in {"import", "export", "type", ","}:
            return False
        if next(token for token in tokens if token.line == tokens[cursor].line).text not in {"import", "export"}:
            return False
        if tokens[index + 1].text == "as":
            raise self.error(tokens[index], f"aliasing {tokens[index].text} hides its calls")
        return True

    def calls(self) -> list[ClientCall]:
        from client_route_declarations import helper_declaration

        tokens = self.tokens
        for inner in template_tokens(tokens):
            if inner.text in HELPERS or (inner.kind == "literal" and static_text(inner).startswith(("/api/", "/ws/"))):
                raise self.error(inner, "engine call inside a template expression is unsupported")
        consumed = set()
        calls = []
        for index, token in enumerate(tokens):
            if token.kind != "word" or token.text not in HELPERS:
                continue
            if index > 0 and tokens[index - 1].text in {"function", "const", "let", "var"}:
                continue
            if self.specifier(index):
                continue
            if helper_declaration(tokens, index):
                continue
            cursor = index + 1
            if cursor < len(tokens) and tokens[cursor].text == "<":
                cursor = group_end(tokens, cursor) + 1
            if cursor >= len(tokens) or tokens[cursor].text != "(":
                if token.text == "wsUrl":
                    from client_route_alias import websocket_alias

                    alias = websocket_alias(self, index)
                    calls.extend(alias.calls)
                    consumed.update(alias.paths)
                    continue
                raise self.error(token, f"{token.text} is referenced without a direct call")
            end = group_end(tokens, cursor)
            args = split_tokens(tokens[cursor + 1 : end], ",")
            if not args or len(args[0]) != 1 or args[0][0].kind != "literal":
                raise self.error(token, "first argument must be a single string/template literal")
            path_index = cursor + 1
            consumed.add(path_index)
            protocol: Protocol = "websocket" if token.text == "wsUrl" else "http"
            methods = ["WEBSOCKET"] if protocol == "websocket" else self.methods(token.text, args, token)
            fn = self.enclosing(index)
            for path, reason in self.paths(path_index):
                calls.extend(
                    ClientCall(
                        protocol,
                        method,
                        path,
                        self.file,
                        token.line,
                        tokens[path_index].text,
                        fn.name if fn else "",
                        reason,
                    )
                    for method in methods
                )
        for index, token in enumerate(tokens):
            if token.kind != "literal" or index in consumed:
                continue
            text = static_text(token)
            if not text.startswith(("/api/", "/ws/")):
                continue
            if index == 0 or tokens[index - 1].text != "return" or tokens[index - 1].line != token.line:
                raise self.error(token, "unclassified engine path literal")
            following = tokens[index + 1] if index + 1 < len(tokens) else None
            if following is not None and following.line == token.line and following.text not in {";", "}"}:
                raise self.error(token, "unclassified engine path literal (return is not a direct literal)")
            protocol = "websocket" if text.startswith("/ws/") else "http"
            fn = self.enclosing(index)
            for path, reason in self.paths(index):
                calls.append(
                    ClientCall(
                        protocol,
                        "WEBSOCKET" if protocol == "websocket" else "GET",
                        path,
                        self.file,
                        token.line,
                        token.text,
                        fn.name if fn else "",
                        reason,
                    )
                )
        return calls


def extract_calls(source: str, file: str = "snippet.ts") -> list[ClientCall]:
    return Extractor(source, file).calls()


def load_client_calls(root: Path) -> list[ClientCall]:
    directories = [root / name / "src/lib" for name in ("web-a", "web-e")]
    socket = root / "web-e/src/lib/chat/ws-client.ts"
    for path in [*directories, socket]:
        if not path.exists():
            pytest.fail(f"Missing {path}: this guard needs the monorepo checkout with web-a/ and web-e/ beside engine/")
    calls = []
    for directory in directories:
        for path in sorted(directory.rglob("*")):
            if path.suffix in {".ts", ".tsx"} and ".test." not in path.name and ".spec." not in path.name:
                calls.extend(extract_calls(path.read_text(), path.relative_to(root).as_posix()))
    return calls


def client_failures(calls: list[ClientCall], routes: list[EffectiveRoute], *, redirect_slashes: bool) -> list[str]:
    failures = []
    for call in calls:
        result = resolve(routes, call.protocol, call.method, call.path, redirect_slashes=redirect_slashes)
        reason = ""
        if result.outcome == "none":
            reason = "no route matches"
        elif result.outcome == "partial":
            allowed = sorted(
                {
                    method
                    for route in routes
                    if route.match(call.protocol, call.method, call.path) == Match.PARTIAL
                    for method in route.methods
                }
            )
            reason = f"path exists but not for {call.method} (allowed: {', '.join(allowed)})"
        elif PLACEHOLDER not in call.path and result.route is not None and "{" in result.route.path:
            exact = next(
                (
                    route
                    for route in routes
                    if route.index > result.route.index
                    and route.path == result.path
                    and route.protocol == call.protocol
                    and call.method in route.methods
                ),
                None,
            )
            if exact is not None:
                reason = f"reaches {result.route.handler} but the exact route {exact.handler} is shadowed"
        if reason:
            failures.append(f"{call.method} {call.path} ({call.file}:{call.line}): {reason}")
    return failures


def test_frontend_calls_reach_mounted_routes() -> None:
    calls = load_client_calls(Path(__file__).resolve().parents[2])
    assert all(any(call.file.startswith(name + "/") for call in calls) for name in ("web-a", "web-e"))
    assert any(
        call.file == "web-e/src/lib/chat/ws-client.ts"
        and call.protocol == "websocket"
        and call.path == f"/ws/chat/{PLACEHOLDER}"
        for call in calls
    )
    expanded = [call for call in calls if call.expansion_reason]
    assert {call.function for call in expanded} == CHANNEL_EXPANSION.functions
    channels = Extractor(
        (Path(__file__).resolve().parents[2] / CHANNEL_EXPANSION.file).read_text(), CHANNEL_EXPANSION.file
    ).channels()
    for name in CHANNEL_EXPANSION.functions:
        probed = [path for path, webhook in channels if webhook or name not in CHANNEL_EXPANSION.webhook_functions]
        assert sum(call.function == name for call in expanded) == len(probed), name
    failures = client_failures(calls, effective_routes(app), redirect_slashes=app.router.redirect_slashes)
    assert failures == [], "Frontend calls do not reach mounted routes:\n" + "\n".join(failures)


def test_extractor_supported_shapes() -> None:
    source = r"""
export async function apiRequest<T>(path: string, options = {}) { return null }
export const apiFormRequest = createApiRequest()
import {
  apiFormRequest, apiRequest,
} from '@/lib/http'
export async function load() { return apiRequest<{ id: string }>('/api/exported') }
// apiRequest('/api/commented')
/* apiRequest('/api/block-commented') */
function querySuffix(id?: string): string { return id ? `?id=${id}` : '' }
function requests() {
  const pattern = /^request failed \(\d+\)['"]$/i
  const suffix = query ? `?q=${encodeURIComponent(query)}` : ''
  apiRequest<{
    users?: Array<{ id: string }>
  }>('/api/generic', { body })
  apiRequest(`/api/literal?${nested({value: `a${"?"}`})}`)
  apiRequest(`/api/suffix${suffix}`)
  apiRequest(`/api/aligned/${suffix}`)
  apiRequest(`/api/segment/${id}${suffix}`)
  apiRequest(`/api/function${querySuffix(id)}`)
  apiRequest('/api/method', { method: 'put', body: undefined })
  apiRequest('/api/computed', { ['method']: 'patch' })
  apiRequest('/api/conditional', { method: enabled ? 'POST' : 'DELETE' })
  apiFormRequest('/api/form', form)
  apiFormRequest('/api/form-options', form, { method: 'PATCH' })
  apiRequest('/api/undefined', { body: undefined })
  wsUrl(`/ws/chat/${id}?${params.toString()}`)
  return `/api/download/${id}?${query.toString()}`
}
"""
    assert sorted((call.protocol, call.method, call.path) for call in extract_calls(source)) == sorted(
        [
            ("http", "GET", "/api/exported"),
            ("http", "POST", "/api/generic"),
            ("http", "GET", "/api/literal"),
            ("http", "GET", "/api/suffix"),
            ("http", "GET", "/api/aligned/"),
            ("http", "GET", f"/api/segment/{PLACEHOLDER}"),
            ("http", "GET", "/api/function"),
            ("http", "PUT", "/api/method"),
            ("http", "PATCH", "/api/computed"),
            ("http", "POST", "/api/conditional"),
            ("http", "DELETE", "/api/conditional"),
            ("http", "POST", "/api/form"),
            ("http", "PATCH", "/api/form-options"),
            ("http", "GET", "/api/undefined"),
            ("websocket", "WEBSOCKET", f"/ws/chat/{PLACEHOLDER}"),
            ("http", "GET", f"/api/download/{PLACEHOLDER}"),
        ]
    )


@pytest.mark.parametrize(
    ("source", "reason"),
    [
        ("apiRequest(path)", "first argument"),
        ("apiRequest('/api/x', { ...options })", "spread"),
        ("apiRequest('/api/x', options)", "object literal"),
        ("apiRequest('/api/x', { method: method })", "method must"),
        (
            "function f() { const suffix = ok ? '?x=1' : '/bad'; apiRequest(`/api/x${suffix}`) }",
            "non-query alternative",
        ),
        ("function f() { const suffix = compute(); apiRequest(`/api/x${suffix}`) }", "neither a whole path segment"),
        ("const url = '/api/unclassified'", "unclassified engine path literal"),
        (
            'function f() { const suffix = `${"/bad"}?q=1`; apiRequest(`/api/health${suffix}`) }',
            "non-query alternative",
        ),
        ('const suffix = `${"/bad"}?q=1`; apiRequest(`/api/health${suffix}`)', "neither a whole path segment"),
        (
            "function f() { let suffix = '?q=1'; suffix = '/bad'; apiRequest(`/api/health${suffix}`) }",
            "neither a whole path segment",
        ),
        (
            "function f() { const suffix = '?q=1'; for (const suffix of ['/bad']) apiRequest(`/api/x${suffix}`) }",
            "one const binding",
        ),
        (
            "function q() { if (x) { return '/bad' } return '' } function f() { apiRequest(`/api/x${q()}`) }",
            "non-query alternative",
        ),
        (
            "function q() { if (x) { return '?a=1' } } function f() { apiRequest(`/api/x${q()}`) }",
            "may fall through",
        ),
        ("function f(suffix: string) { apiRequest(`/api/health${suffix}`) }", "neither a whole path segment"),
        ("apiRequest(`/api/agents/x-${id}`)", "neither a whole path segment"),
        ("apiRequest('/api/x', { [key]: 'POST' })", "computed request option keys"),
        ("apiRequest('/api/x', { get method() { return 'POST' } })", "unsupported request option property"),
        ('function f() { return `value=${apiRequest("/api/not-a-route")}` }', "inside a template expression"),
        ("const url = `${'/api/hidden'}`", "inside a template expression"),
        ("load().then(apiRequest)", "without a direct call"),
        ("export function f() { return { apiRequest } }", "without a direct call"),
        ("import { apiRequest as request } from '@/lib/http'", "aliasing apiRequest"),
    ],
)
def test_extractor_rejects_unprovable_shapes(source: str, reason: str) -> None:
    with pytest.raises(ExtractionError, match=reason) as error:
        extract_calls(source, "fixture.ts")
    assert "fixture.ts:1:" in str(error.value)


@pytest.mark.parametrize("missing", ["web-a/src/lib", "web-e/src/lib", "web-e/src/lib/chat/ws-client.ts"])
def test_loader_requires_monorepo(tmp_path: Path, missing: str) -> None:
    for directory in ("web-a/src/lib", "web-e/src/lib/chat"):
        (tmp_path / directory).mkdir(parents=True)
    socket = tmp_path / "web-e/src/lib/chat/ws-client.ts"
    socket.write_text("")
    target = tmp_path / missing
    if target.is_file():
        target.unlink()
    else:
        for path in sorted(target.rglob("*"), reverse=True):
            path.rmdir() if path.is_dir() else path.unlink()
        target.rmdir()
    with pytest.raises(pytest.fail.Exception) as error:
        load_client_calls(tmp_path)
    assert str(target) in str(error.value)
    assert "monorepo checkout with web-a/ and web-e/ beside engine/" in str(error.value)


def test_upload_dead_url_is_reported_against_real_app() -> None:
    calls = extract_calls("apiFormRequest('/api/upload', form)")
    assert client_failures(calls, effective_routes(app), redirect_slashes=app.router.redirect_slashes) == [
        "POST /api/upload (snippet.ts:1): no route matches"
    ]


@pytest.mark.parametrize(
    ("protocol", "method", "path", "redirect_slashes", "outcome", "redirected", "matched"),
    [
        ("http", "GET", "/exact", True, "full", False, "/exact"),
        ("http", "POST", "/exact", True, "partial", False, "/exact"),
        ("http", "GET", "/slash", True, "full", True, "/slash/"),
        ("http", "POST", "/slash", True, "none", False, "/slash"),
        ("http", "GET", "/slash", False, "none", False, "/slash"),
        ("websocket", "WEBSOCKET", "/socket", True, "none", False, "/socket"),
        ("websocket", "WEBSOCKET", "/socket/", True, "full", False, "/socket/"),
    ],
)
def test_resolver_dispatch_semantics(
    protocol: Protocol, method: str, path: str, redirect_slashes: bool, outcome: str, redirected: bool, matched: str
) -> None:
    synthetic = FastAPI()

    async def endpoint() -> None:
        pass

    synthetic.add_api_route("/exact", endpoint, methods=["GET"])
    synthetic.add_api_route("/exact/", endpoint, methods=["POST"])
    synthetic.add_api_route("/slash/", endpoint, methods=["GET"])
    synthetic.add_api_websocket_route("/socket/", endpoint)
    result = resolve(effective_routes(synthetic), protocol, method, path, redirect_slashes=redirect_slashes)
    assert (result.outcome, result.redirected, result.path) == (outcome, redirected, matched)
    assert (result.route is not None) == (outcome != "none")
