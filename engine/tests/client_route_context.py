from test_client_route_resolution import Extractor, Token, group_end, split_tokens, template_tokens, top_level


def routing_neutral_field_spread(expression: list[Token]) -> bool:
    if len(expression) == 12 and expression[1].kind == "word" and expression[1].text in {"owner", "signal", "token"}:
        field = expression[1].text
        return [token.text for token in expression] == f"( {field} === undefined ? {{ }} : {{ {field} }} )".split()
    if len(expression) != 18 or expression[1].kind != "word" or expression[3].text not in {"owner", "signal", "token"}:
        return False
    reference, field = expression[1].text, expression[3].text
    expected = f"( {reference} . {field} === undefined ? {{ }} : {{ {field} : {reference} . {field} }} )".split()
    return [token.text for token in expression] == expected


def neutral_object(tokens: list[Token], parameters: set[str]) -> bool:
    if not tokens or tokens[0].text != "{" or group_end(tokens, 0) != len(tokens) - 1:
        return False
    for prop in split_tokens(tokens[1:-1], ","):
        if not prop:
            continue
        if prop[0].text == "...":
            value = prop[1:]
            if not value or value[0].text != "(" or group_end(value, 0) != len(value) - 1:
                return False
            value = value[1:-1]
            if len(value) < 6 or [t.text for t in value[1:4]] != ["===", "undefined", "?"]:
                return False
            if value[0].text not in parameters:
                return False
            colons = [i for i in top_level(value) if value[i].text == ":"]
            if len(colons) != 1:
                return False
            colon = colons[0]
            if not neutral_object(value[4:colon], parameters) or not neutral_object(value[colon + 1 :], parameters):
                return False
        else:
            # These RequestOptions fields cannot select a method or supply a body.
            if prop[0].kind != "word" or prop[0].text not in {"owner", "signal", "token"}:
                return False
            value = prop if len(prop) == 1 else prop[2:] if prop[1].text == ":" else []
            if len(value) != 1 or value[0].kind != "word" or value[0].text not in parameters:
                return False
    return True


def routing_neutral_context(extractor: Extractor, expression: list[Token]) -> bool:
    if (
        len(expression) < 3
        or expression[0].kind != "word"
        or expression[1].text != "("
        or group_end(expression, 1) != len(expression) - 1
    ):
        return False
    arguments = split_tokens(expression[2:-1], ",")
    if any(arg and (len(arg) != 1 or arg[0].kind != "word") for arg in arguments):
        return False
    name = expression[0].text
    tokens = extractor.tokens
    declarations = [i for i in top_level(tokens) if tokens[i].text == "function" and tokens[i + 1].text == name]
    matches = [fn for fn in extractor.functions if fn.name == name]
    if len(declarations) != 1 or len(matches) != 1:
        return False
    declaration = declarations[0]
    if declaration:
        previous = tokens[declaration - 1]
        follows_import = previous.kind == "literal" and tokens[declaration - 2].text in {"from", "import"}
        if previous.text not in {";", "}", "export", "default"} and not follows_import:
            return False
    if tokens[declaration + 2].text != "(":
        return False
    end = group_end(tokens, declaration + 2)
    parameters: set[str] = set()
    for parameter in split_tokens(tokens[declaration + 3 : end], ","):
        if not parameter:
            continue
        tail = parameter[1:]
        if tail and tail[0].text == "?":
            tail = tail[1:]
        if parameter[0].kind != "word" or (tail and (len(tail) != 2 or tail[0].text != ":" or tail[1].kind != "word")):
            return False
        if parameter[0].text in parameters or parameter[0].text == "undefined":
            return False
        parameters.add(parameter[0].text)
    # Every other occurrence must be a direct call: bindings, aliases and writes fail closed.
    if any(inner.kind == "word" and inner.text == name for inner in template_tokens(tokens)):
        return False
    for i, token in enumerate(tokens):
        if token.kind != "word" or token.text != name or i == declaration + 1:
            continue
        if i + 1 == len(tokens) or tokens[i + 1].text != "(" or (i and tokens[i - 1].text in {".", "?."}):
            return False
    fn = matches[0]
    body = tokens[fn.body + 1 : fn.end]
    if body and body[-1].text == ";":
        body = body[:-1]
    return (
        len(body) >= 3
        and body[0].text == "return"
        and body[0].line == body[1].line
        and neutral_object(body[1:], parameters)
    )
