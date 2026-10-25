import pytest

from test_client_route_resolution import ExtractionError, extract_calls

WRAPPER = """
async function memberSessionRequest<T>(path: string, options: Options<T>): Promise<T> {
  const owner = options.owner ?? capture();
  try { return await apiRequest(path, { ...options, owner }); }
  catch (error) { expire(owner); throw error; }
}
"""


@pytest.mark.parametrize(("options", "method"), [("{}", "GET"), ("{ body }", "POST"), ("{ method: 'PATCH' }", "PATCH")])
def test_forwards_literal_routes_through_owner_only_wrapper(options: str, method: str) -> None:
    source = WRAPPER + f"memberSessionRequest('/api/x', {options})"

    calls = extract_calls(source)

    assert [(call.method, call.path, call.function) for call in calls] == [(method, "/api/x", "")]


@pytest.mark.parametrize(
    "mutation",
    [
        "path = '/api/other';",
        "options.method = 'DELETE';",
        "options.body = payload;",
        "mutate(options);",
        "const alias = options;",
        "const other = path;",
        "`${mutate(options)}`;",
    ],
)
def test_rejects_unproven_forwarding_parameter_use(mutation: str) -> None:
    source = WRAPPER.replace("  try", f"  {mutation}\n  try") + "memberSessionRequest('/api/x', {})"

    with pytest.raises(ExtractionError):
        extract_calls(source)


@pytest.mark.parametrize(
    "reference",
    [
        "const alias = memberSessionRequest",
        "load().then(memberSessionRequest)",
        "memberSessionRequest = other; memberSessionRequest('/api/x', {})",
        "function f(memberSessionRequest) { memberSessionRequest('/api/x', {}) }",
        "function f() { const memberSessionRequest = other; memberSessionRequest('/api/x', {}) }",
        "holder.memberSessionRequest('/api/x', {})",
    ],
)
def test_rejects_arbitrary_forwarder_references(reference: str) -> None:
    with pytest.raises(ExtractionError):
        extract_calls(WRAPPER + reference)


@pytest.mark.parametrize("modifier", ["", "async "])
def test_rejects_named_function_expression_bound_to_unclassified_alias(modifier: str) -> None:
    source = f"""
    const send = {modifier}function memberSessionRequest(path: string, options: Options) {{
      const owner = options.owner ?? capture();
      return {"await " if modifier else ""}apiRequest(path, {{...options, owner}});
    }};
    send(runtimePath, {{method: 'DELETE'}});
    """

    with pytest.raises(ExtractionError):
        extract_calls(source)
