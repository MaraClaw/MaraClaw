from test_client_route_resolution import (
    PLACEHOLDER,
    Extractor,
    Part,
    Token,
    group_end,
    literal_alternatives,
    split_tokens,
    static_text,
    template_tokens,
    top_level,
)


def literal_segments(expression: list[Token]) -> list[str] | None:
    alternatives = literal_alternatives(expression)
    if alternatives is None:
        return None
    values = []
    for token in alternatives:
        if "\\" in token.text or any(part.expression for part in token.parts):
            return None
        value = static_text(token)
        if not value or value in {".", ".."} or any(char not in "-._~" and not char.isalnum() for char in value):
            return None
        values.append(value)
    return values


def resource_reference(expression: list[Token]) -> bool:
    encoded = (
        len(expression) >= 4
        and [token.text for token in expression[:2]] == ["encodeURIComponent", "("]
        and group_end(expression, 1) == len(expression) - 1
    )
    if encoded:
        expression = expression[2:-1]
    if not expression or len(expression) % 2 != 1:
        return False
    if any(token.kind != "word" if index % 2 == 0 else token.text != "." for index, token in enumerate(expression)):
        return False
    name = expression[-1].text
    return name == "id" or name.endswith(("Id", "_id")) or (encoded and name in {"key", "provider"})


def segment_values(extractor: Extractor, part: Part, index: int) -> list[str]:
    expression = list(part.tokens)
    literals = literal_segments(expression)
    if literals is not None:
        return list(dict.fromkeys(literals))
    if resource_reference(expression):
        return [PLACEHOLDER]
    error = extractor.error(extractor.tokens[index], f"route segment {part.text.strip()!r} is not finitely provable")
    fn = extractor.enclosing(index)
    if len(expression) != 1 or expression[0].kind != "word" or fn is None or fn.name == "<arrow>":
        raise error
    tokens = extractor.tokens
    declarations = [i for i in top_level(tokens) if tokens[i].text == "function" and tokens[i + 1].text == fn.name]
    if len(declarations) != 1 or sum(item.name == fn.name for item in extractor.functions) != 1:
        raise error
    declaration = declarations[0]
    prefix = declaration - 1
    if prefix >= 0 and tokens[prefix].text == "async":
        prefix -= 1
    if prefix >= 0:
        previous = tokens[prefix]
        follows_import = previous.kind == "literal" and prefix > 0 and tokens[prefix - 1].text in {"from", "import"}
        if previous.text not in {";", "}"} and not follows_import:
            raise error
    if tokens[declaration + 2].text != "(":
        raise error
    end = group_end(tokens, declaration + 2)
    parameters = [parameter for parameter in split_tokens(tokens[declaration + 3 : end], ",") if parameter]
    if any(parameter[0].kind != "word" for parameter in parameters):
        raise error
    name = expression[0].text
    matches = [position for position, parameter in enumerate(parameters) if parameter[0].text == name]
    if len(matches) != 1:
        raise error
    position = matches[0]
    parameter = parameters[position]
    if [token.text for token in parameter] not in ([name], [name, ":", "string"]):
        raise error
    if any(
        token.kind == "word" and token.text == name and token.start != parameter[0].start
        for token in tokens[declaration + 3 : fn.end]
    ):
        raise error
    for token in tokens[declaration + 3 : fn.end]:
        for nested in token.parts:
            references = [*nested.tokens, *template_tokens(list(nested.tokens))]
            if any(inner.kind == "word" and inner.text == name for inner in references) and (
                len(nested.tokens) != 1 or nested.tokens[0].text != name
            ):
                raise error
    if any(
        token.kind == "word" and token.text in {"eval", "with", "arguments"}
        for token in [*tokens, *template_tokens(tokens)]
    ):
        raise error
    if any(token.kind == "word" and token.text == fn.name for token in template_tokens(tokens)):
        raise error
    values: list[str] = []
    for cursor, token in enumerate(tokens):
        if token.kind != "word" or token.text != fn.name or cursor == declaration + 1:
            continue
        if (
            cursor + 1 == len(tokens)
            or tokens[cursor + 1].text != "("
            or (cursor and tokens[cursor - 1].text in {".", "?.", "new"})
        ):
            raise error
        arguments = split_tokens(tokens[cursor + 2 : group_end(tokens, cursor + 1)], ",")
        if len(arguments) <= position or any(arg and arg[0].text == "..." for arg in arguments[: position + 1]):
            raise error
        alternatives = literal_segments(arguments[position])
        if alternatives is None:
            raise error
        values.extend(alternatives)
    if not values:
        raise error
    return list(dict.fromkeys(values))
