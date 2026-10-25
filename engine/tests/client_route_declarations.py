from test_client_route_resolution import HELPERS, Function, Token, group_end, split_tokens, top_level


def factory_return_helper(tokens: list[Token], index: int, factory: Function | None) -> bool:
    if factory is None:
        return False
    returning = index - 1
    if tokens[returning].text == "{" and tokens[index + 1].text in {",", "}"}:
        closing = group_end(tokens, returning)
        if closing + 1 != factory.end and tokens[closing + 1].text != ";":
            return False
        returning -= 1
    elif tokens[index + 1].text != ";" and index + 1 != factory.end:
        return False
    if tokens[returning].text != "return" or tokens[returning].line != tokens[returning + 1].line:
        return False
    declared = False
    cursor = factory.body + 1
    while cursor < returning:
        token = tokens[cursor]
        if token.text == "function" and tokens[cursor + 1].text == tokens[index].text:
            prefix = cursor - 1
            if tokens[prefix].text == "async":
                prefix -= 1
            statement = tokens[prefix].text in {"{", "}", ";"} or (
                tokens[prefix].text == ">" and tokens[prefix].line < token.line
            )
            body = named_function_body(tokens, cursor)
            if statement and body is not None:
                declared = True
        if token.text in {"{", "(", "[", "<"}:
            cursor = group_end(tokens, cursor)
        cursor += 1
    return declared and cursor == returning and canonical_factory_consumers(tokens, index, factory)


def function_statement(tokens: list[Token], declaration: int) -> bool:
    prefix = declaration - 1
    for modifier in ("async", "default", "export"):
        if prefix >= 0 and tokens[prefix].text == modifier:
            prefix -= 1
    return (
        prefix < 0
        or tokens[prefix].text in {"{", "}", ";"}
        or (tokens[prefix].line < tokens[declaration].line and tokens[prefix].kind == "literal")
    )


def canonical_factory_consumers(tokens: list[Token], index: int, factory: Function) -> bool:
    scope = top_level(tokens)
    declarations = [
        cursor
        for cursor in scope
        if tokens[cursor].text == "function"
        and tokens[cursor + 1].text == factory.name
        and function_statement(tokens, cursor)
        and named_function_body(tokens, cursor) == factory.body
    ]
    if len(declarations) != 1:
        return False
    consumed = False
    for cursor, token in enumerate(tokens):
        if token.text != factory.name or cursor == declarations[0] + 1:
            continue
        if (
            cursor < 3
            or cursor + 1 == len(tokens)
            or cursor not in scope
            or tokens[cursor + 1].text != "("
            or tokens[cursor - 1].text != "="
        ):
            return False
        end = group_end(tokens, cursor + 1)
        if end + 1 < len(tokens) and tokens[end + 1].text != ";":
            return False
        if tokens[index - 1].text == "{":
            openings = [
                opening for opening in scope if tokens[opening].text == "{" and group_end(tokens, opening) == cursor - 2
            ]
            if len(openings) != 1 or tokens[openings[0] - 1].text != "const":
                return False
            fields = split_tokens(tokens[openings[0] + 1 : cursor - 2], ",")
            if not all(len(field) == 1 and field[0].text in HELPERS for field in fields):
                return False
            if not any(field[0].text == tokens[index].text for field in fields):
                return False
        elif [part.text for part in tokens[cursor - 3 : cursor]] != ["const", tokens[index].text, "="]:
            return False
        consumed = True
    return consumed


def named_function_body(tokens: list[Token], declaration: int) -> int | None:
    cursor = declaration + 2
    if tokens[cursor].text == "<":
        cursor = group_end(tokens, cursor) + 1
    if tokens[cursor].text != "(":
        return None
    cursor = group_end(tokens, cursor) + 1
    if tokens[cursor].text == ":":
        cursor += 1
        if tokens[cursor].kind != "word":
            return None
        cursor += 1
        if tokens[cursor].text == "<":
            cursor = group_end(tokens, cursor) + 1
    return cursor if tokens[cursor].text == "{" else None


def helper_declaration(tokens: list[Token], index: int) -> bool:
    if object_method(tokens, index):
        return True
    for opening in range(index - 1, 0, -1):
        if tokens[opening].text != "{" or tokens[opening - 1].text != "const":
            continue
        closing = group_end(tokens, opening)
        if closing <= index or closing + 1 >= len(tokens) or tokens[closing + 1].text != "=":
            continue
        return all(
            len(part) == 1 and part[0].kind == "word" for part in split_tokens(tokens[opening + 1 : closing], ",")
        )
    return False


def object_method(tokens: list[Token], index: int) -> bool:
    prefix = index - 1
    if prefix >= 0 and tokens[prefix].text == "async":
        prefix -= 1
    if prefix < 0 or tokens[prefix].text not in {"{", ","}:
        return False
    cursor = index + 1
    if tokens[cursor].text == "<":
        cursor = group_end(tokens, cursor) + 1
    if tokens[cursor].text != "(":
        return False
    cursor = group_end(tokens, cursor) + 1
    if tokens[cursor].text == ":":
        cursor += 1
        if tokens[cursor].kind != "word":
            return False
        cursor += 1
        if tokens[cursor].text == "<":
            cursor = group_end(tokens, cursor) + 1
    return tokens[cursor].text == "{"
