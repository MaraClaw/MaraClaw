from test_client_route_resolution import Token, group_end, split_tokens


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
