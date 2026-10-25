from client_route_declarations import function_statement
from test_client_route_resolution import Extractor, group_end, split_tokens, template_tokens, top_level


def forwarding_helpers(extractor: Extractor) -> dict[str, int]:
    tokens = extractor.tokens
    result = {}
    for index, token in enumerate(tokens[:-1]):
        if token.text != "apiRequest" or tokens[index + 1].text != "(":
            continue
        arguments = split_tokens(tokens[index + 2 : group_end(tokens, index + 1)], ",")
        if len(arguments) != 2 or len(arguments[0]) != 1 or arguments[0][0].kind != "word":
            continue
        function = extractor.enclosing(index)
        if function is None or function.name == "<arrow>":
            continue
        declarations = [
            cursor
            for cursor in top_level(tokens)
            if tokens[cursor].text == "function"
            and tokens[cursor + 1].text == function.name
            and function_statement(tokens, cursor)
        ]
        if len(declarations) != 1 or sum(item.name == function.name for item in extractor.functions) != 1:
            continue
        opening = declarations[0] + 2
        if tokens[opening].text == "<":
            opening = group_end(tokens, opening) + 1
        if tokens[opening].text != "(":
            continue
        parameters = split_tokens(tokens[opening + 1 : group_end(tokens, opening)], ",")
        if len(parameters) != 2 or any(not part or part[0].kind != "word" for part in parameters):
            continue
        path, options = (part[0].text for part in parameters)
        if arguments[0][0].text != path or [item.text for item in arguments[1]] != [
            "{",
            "...",
            options,
            ",",
            "owner",
            "}",
        ]:
            continue
        body = tokens[function.body + 1 : function.end]
        if sum(item.text == "apiRequest" for item in body) != 1:
            continue
        if any(item.text in {"eval", "with", "arguments"} for item in body):
            continue
        if any(item.text in {path, options} for item in template_tokens(body)):
            continue
        proven = True
        for cursor, item in enumerate(tokens):
            if item.text != function.name or cursor == declarations[0] + 1:
                continue
            following = cursor + 1
            if following < len(tokens) and tokens[following].text == "<":
                following = group_end(tokens, following) + 1
            if (
                following == len(tokens)
                or tokens[following].text != "("
                or (cursor and tokens[cursor - 1].text in {".", "?.", "new", "const", "let", "var", "function"})
            ):
                proven = False
        for cursor, item in enumerate(body):
            if item.text == path and item.start != arguments[0][0].start:
                proven = False
            if (
                item.text == options
                and item.start != arguments[1][2].start
                and [part.text for part in body[cursor + 1 : cursor + 4]] != [".", "owner", "??"]
            ):
                proven = False
        if proven:
            result[function.name] = index
    return result
