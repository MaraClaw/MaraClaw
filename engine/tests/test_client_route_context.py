import pytest

from test_client_route_resolution import ExtractionError, extract_calls

CONTEXT = """
function context(owner?: SessionOwner, signal?: AbortSignal) {
  return {
    ...(owner === undefined ? {} : { owner }),
    ...(signal === undefined ? {} : { signal }),
  }
}
"""


@pytest.mark.parametrize(
    ("expression", "method"),
    [
        ("apiRequest('/api/x', context(owner, signal))", "GET"),
        ("apiFormRequest('/api/x', form, context(owner))", "POST"),
        ("apiRequest('/api/x', { method: 'DELETE', body, ...context(owner, signal) })", "DELETE"),
        ("apiRequest('/api/x', { body, ...context(undefined, signal) })", "POST"),
        ("apiRequest('/api/x', { method: 'PUT', ...context(owner), method: 'PATCH' })", "PATCH"),
        ("apiRequest('/api/x', { body, ...context(owner), body: undefined })", "GET"),
        ("apiRequest('/api/x', { body: undefined, ...context(owner), body })", "POST"),
    ],
)
def test_context_preserves_routing_when_used_as_options_or_spread(expression: str, method: str) -> None:
    # Given a context helper with conditional, routing-neutral properties.
    source = CONTEXT + expression
    # When extracting the request's routing consequence.
    calls = extract_calls(source)
    # Then only explicit routing properties determine its method.
    assert [(call.method, call.path) for call in calls] == [(method, "/api/x")]


def test_context_is_proven_by_shape_not_helper_name() -> None:
    # Given an independently named helper with parameter-backed context properties.
    source = "function metadata(session: Owner) { return { owner: session } }"
    source += "apiRequest('/api/x', metadata(session))"
    # When extracting its request.
    calls = extract_calls(source)
    # Then its structurally neutral return is accepted.
    assert [call.method for call in calls] == ["GET"]


@pytest.mark.parametrize(
    "helper",
    [
        "function context(owner) { return { method: owner } }",
        "function context(owner) { return { method: 'DELETE' } }",
        "function context(owner) { return { body: owner } }",
        "function context(owner) { return { body: undefined } }",
        "function context(owner) { return { ...owner } }",
        "function context(owner) { return { [owner]: 'DELETE' } }",
        "function context(owner) { return { get owner() { return mutate() } } }",
        "function context(owner) { mutate(); return { owner } }",
        "function context(owner) { return { owner: mutate() } }",
        "function context(owner) { return { ...(owner ? {} : unknown) } }",
        "function context(owner) { return { ...(owner === undefined ? {} : { method: owner }) } }",
        "function context(owner = mutate()) { return { owner } }",
        "async function context(owner) { return { owner } }",
        "function context(owner) { return\n{ owner } }",
        "function context(owner) { return { __proto__: owner } }",
        "const other = function context(owner) { return { owner } }",
        "void function context(owner) { return { owner } }",
    ],
)
@pytest.mark.parametrize("options", ["context(owner)", "{ method: 'PUT', ...context(owner) }"])
def test_context_rejects_unknown_routing_or_effects(helper: str, options: str) -> None:
    # Given a helper whose return or evaluation is not provably neutral.
    source = helper + f"apiRequest('/api/x', {options})"
    # When extracting it, then reject rather than assume a routing method.
    with pytest.raises(ExtractionError):
        extract_calls(source)


@pytest.mark.parametrize(
    "use",
    [
        "function f(context) { apiRequest('/api/x', context(owner)) }",
        "function f() { const context = other; apiRequest('/api/x', context(owner)) }",
        "function context(owner) { return { method: 'DELETE' } } apiRequest('/api/x', context(owner))",
        "context = other; apiRequest('/api/x', context(owner))",
        "mutate(context); apiRequest('/api/x', context(owner))",
        "apiRequest('/api/x', context(mutate()))",
    ],
)
def test_context_rejects_shadowing_mutation_and_effectful_arguments(use: str) -> None:
    # Given a neutral declaration but unprovable binding/evaluation at the call.
    source = CONTEXT + use
    # When extracting it, then fail closed.
    with pytest.raises(ExtractionError):
        extract_calls(source)
