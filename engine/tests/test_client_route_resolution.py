"""Fail-closed extraction of frontend engine calls and dispatch-order resolution."""

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


@dataclass(frozen=True, slots=True)
class Token:
    text: str
    kind: Literal["word", "punct", "literal", "regex"]
    line: int
    start: int
    end: int
    parts: tuple[Part, ...] = ()


@dataclass(frozen=True, slots=True)
class ExtractionError(ValueError):
    file: str
    line: int
    reason: str

    @override
    def __str__(self) -> str:
        return f"{self.file}:{self.line}: {self.reason}"


class Lexer:
    """A cursor tokenizer; recursive scans keep template expressions opaque to outer text."""

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
                self.scan(expression=True)
                parts.append(Part(self.source[start : self.pos - 1], True))
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
        if tokens[index].text == ";":
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

    def query_suffix(self, expression: str, index: int) -> bool:
        expr = Lexer(expression, self.file).scan()
        value: list[list[Token]] | None = None
        fn = self.enclosing(index)
        if len(expr) == 1 and expr[0].kind == "word" and fn is not None:
            for cursor in range(fn.body + 1, index):
                if self.tokens[cursor].text not in {"const", "let"}:
                    continue
                if [t.text for t in self.tokens[cursor + 1 : cursor + 3]] != [expr[0].text, "="]:
                    continue
                if self.enclosing(cursor) != fn:
                    continue
                scopes = [
                    (opening, group_end(self.tokens, opening))
                    for opening in range(fn.body, cursor)
                    if self.tokens[opening].text == "{"
                ]
                if any(opening < cursor < end and not opening < index < end for opening, end in scopes):
                    continue
                value = [statement(self.tokens, cursor + 3, index)]
        elif len(expr) >= 3 and expr[0].kind == "word" and expr[1].text == "(" and group_end(expr, 1) == len(expr) - 1:
            declared = next((item for item in self.functions if item.name == expr[0].text), None)
            if declared is not None:
                body = self.tokens[declared.body + 1 : declared.end]
                value = [
                    statement(body, cursor + 1, len(body))
                    for cursor in top_level(body)
                    if body[cursor].text == "return"
                ]
        if value is None:
            return False
        alternatives = [literal_alternatives(item) for item in value]
        if not value or any(item is None for item in alternatives):
            raise self.error(self.tokens[index], f"query suffix {expression!r} is not literal/conditional")
        for items in alternatives:
            assert items is not None
            for token in items:
                text = "".join(part.text for part in token.parts if not part.expression)
                if text != "" and not text.startswith("?"):
                    raise self.error(
                        self.tokens[index], f"query suffix {expression!r} has a non-query alternative {token.text}"
                    )
                if text == "" and any(part.expression for part in token.parts):
                    raise self.error(self.tokens[index], f"query suffix {expression!r} is not provably empty")
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
        while parts and not parts[-1].expression and parts[-1].text == "":
            parts.pop()
        if parts and parts[-1].expression and self.query_suffix(parts[-1].text.strip(), index):
            parts.pop()
        fn = self.enclosing(index)
        paths = [""]
        reason = ""
        for part in parts:
            values = [part.text]
            if part.expression:
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
        option_index = 2 if helper == "apiFormRequest" else 1
        default = "POST" if helper == "apiFormRequest" else "GET"
        if len(args) <= option_index or not args[option_index]:
            return [default]
        options = args[option_index]
        if options[0].text != "{" or group_end(options, 0) != len(options) - 1:
            raise self.error(token, "request options must be an object literal")
        method = None
        body = False
        for prop in split_tokens(options[1:-1], ","):
            if not prop:
                continue
            if prop[0].text == "...":
                raise self.error(token, "top-level spread in request options")
            key = "".join(p.text for p in prop[0].parts) if prop[0].kind == "literal" else prop[0].text
            value = prop[2:] if len(prop) > 1 and prop[1].text == ":" else prop
            if key == "method":
                alternatives = literal_alternatives(value)
                if alternatives is None or any(t.text.startswith("`") for t in alternatives):
                    raise self.error(token, "method must be a string literal or literal conditional")
                method = ["".join(p.text for p in item.parts).upper() for item in alternatives]
            if key == "body":
                body = [t.text for t in value] != ["undefined"]
        return method if method is not None else ["POST" if body else default]

    def calls(self) -> list[ClientCall]:
        tokens = self.tokens
        consumed = set()
        calls = []
        for index, token in enumerate(tokens):
            if token.text not in HELPERS:
                continue
            if index > 0 and tokens[index - 1].text == "function":
                continue
            cursor = index + 1
            if cursor < len(tokens) and tokens[cursor].text == "<":
                cursor = group_end(tokens, cursor) + 1
            if cursor >= len(tokens) or tokens[cursor].text != "(":
                continue
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
            text = "".join(p.text for p in token.parts if not p.expression)
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
  apiRequest(`/api/function${querySuffix(id)}`)
  apiRequest('/api/method', { method: 'put', body: undefined })
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
            ("http", "POST", "/api/generic"),
            ("http", "GET", "/api/literal"),
            ("http", "GET", "/api/suffix"),
            ("http", "GET", "/api/function"),
            ("http", "PUT", "/api/method"),
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
        ("function f() { const suffix = compute(); apiRequest(`/api/x${suffix}`) }", "not literal/conditional"),
        ("const url = '/api/unclassified'", "unclassified engine path literal"),
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
