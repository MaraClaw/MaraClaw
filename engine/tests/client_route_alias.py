from dataclasses import dataclass

from test_client_route_resolution import (
    ClientCall,
    Extractor,
    Token,
    group_end,
    split_tokens,
    statement,
    template_tokens,
    top_level,
)


@dataclass(frozen=True, slots=True)
class AliasCalls:
    calls: tuple[ClientCall, ...]
    paths: frozenset[int]


def receiver_independent(extractor: Extractor, value: list[Token]) -> bool:
    if len(value) != 1 or value[0].kind != "word":
        return False
    tokens = extractor.tokens
    name = value[0].text
    declarations = [i for i in top_level(tokens) if tokens[i].text == "function" and tokens[i + 1].text == name]
    if len(declarations) != 1:
        return False
    declaration = declarations[0]
    candidates = [fn for fn in extractor.functions if fn.name == name]
    if len(candidates) != 1:
        return False
    fn = candidates[0]
    body = tokens[declaration : fn.end + 1]
    if any(token.text in {"this", "arguments", "eval"} for token in [*body, *template_tokens(body)]):
        return False
    for i, token in enumerate(tokens):
        if token.text != name or i == declaration + 1 or token is value[0]:
            continue
        if i and tokens[i - 1].text == ".":
            continue
        if i and tokens[i - 1].text in {"{", ","} and tokens[i + 1].text == ":":
            continue
        return False
    return True


def websocket_alias(extractor: Extractor, reference: int) -> AliasCalls:
    """Prove member calls of a local literal fallback; injected transports remain outside this proof."""
    tokens = extractor.tokens
    error = extractor.error(tokens[reference], "unproven websocket transport alias")
    owner = extractor.enclosing(reference)
    if owner is None:
        raise error
    for i, token in enumerate(tokens):
        if token.text != "wsUrl" or i == reference or extractor.specifier(i):
            continue
        if (
            i + 1 >= len(tokens)
            or tokens[i + 1].text != "("
            or (i and tokens[i - 1].text in {"const", "let", "var", "function", "class"})
        ):
            raise error
    starts = [i for i in range(owner.body + 1, reference) if tokens[i].text == "{" and group_end(tokens, i) > reference]
    if not starts:
        raise error
    opening = starts[-1]
    closing = group_end(tokens, opening)
    properties: dict[str, list[Token]] = {}
    route_property = ""
    for prop in split_tokens(tokens[opening + 1 : closing], ","):
        if not prop:
            continue
        if len(prop) < 3 or prop[0].kind != "word" or prop[1].text != ":":
            raise error
        key = prop[0].text
        if key in properties or key == "__proto__":
            raise error
        properties[key] = prop[2:]
        if len(prop) == 3 and prop[2] is tokens[reference]:
            route_property = key
    if not route_property:
        raise error
    declarations = []
    for relative in top_level(tokens[owner.body + 1 : owner.end]):
        i = owner.body + 1 + relative
        if tokens[i].text == "const" and i + 2 < opening:
            cursor = i + 2
            if tokens[cursor].text == ":" and tokens[cursor + 1].kind == "word":
                cursor += 2
            if tokens[cursor].text == "=" and cursor < opening:
                initializer = statement(tokens, cursor + 1, owner.end)
                if initializer and initializer[-1] is tokens[closing]:
                    declarations.append((i, cursor))
    if len(declarations) != 1:
        raise error
    declaration, equals = declarations[0]
    binding = tokens[declaration + 1]
    if binding.kind != "word":
        raise error
    prefix = tokens[equals + 1 : opening]
    if prefix:
        if len(prefix) < 2 or prefix[-1].text != "??":
            raise error
        chain = prefix[:-1]
        if len(chain) % 2 != 1 or any(
            token.kind != "word" if i % 2 == 0 else token.text != "." for i, token in enumerate(chain)
        ):
            raise error
    scope = tokens[owner.body + 1 : owner.end]
    if any(token.text in {binding.text, "eval"} for token in template_tokens(scope)):
        raise error
    paths: set[int] = set()
    calls: list[ClientCall] = []
    for i in range(owner.body + 1, owner.end):
        token = tokens[i]
        if token.text in {"eval", "with"}:
            raise error
        if token.kind != "word" or token.text != binding.text or token is binding:
            continue
        if tokens[i - 1].text == ".":
            continue
        if i <= closing or tokens[i - 1].text in {"delete", "++", "--"}:
            raise error
        if tokens[i + 1].text != "." or tokens[i + 2].kind != "word":
            raise error
        key = tokens[i + 2].text
        following = tokens[i + 3].text
        if key not in properties or following not in {"(", ",", ";", "}", ")", "]", "?", ":", "||", "&&", "??"}:
            raise error
        if key != route_property:
            if following == "(" and not receiver_independent(extractor, properties[key]):
                raise error
            continue
        if following != "(":
            raise error
        end = group_end(tokens, i + 3)
        args = split_tokens(tokens[i + 4 : end], ",")
        if len(args) != 1 or len(args[0]) != 1 or args[0][0].kind != "literal":
            raise error
        path_index = i + 4
        paths.add(path_index)
        fn = extractor.enclosing(i)
        for path, reason in extractor.paths(path_index):
            calls.append(
                ClientCall(
                    "websocket",
                    "WEBSOCKET",
                    path,
                    extractor.file,
                    token.line,
                    tokens[path_index].text,
                    fn.name if fn else "",
                    reason,
                    resolution_reason="literal transport fallback branch only",
                )
            )
    if not calls:
        raise error
    return AliasCalls(tuple(calls), frozenset(paths))
