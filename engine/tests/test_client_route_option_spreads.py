import pytest

from test_client_route_resolution import ExtractionError, extract_calls


@pytest.mark.parametrize("field", ["owner", "signal", "token"])
@pytest.mark.parametrize("member", [True, False])
@pytest.mark.parametrize(("routing", "method"), [("", "GET"), ("body,", "POST"), ("method: 'DELETE',", "DELETE")])
def test_conditional_context_field_spread_preserves_routing(
    field: str, member: bool, routing: str, method: str
) -> None:
    reference = f"context.{field}" if member else field
    property_value = f"{field}: {reference}" if member else field
    source = f"apiRequest('/api/x', {{ {routing} ...({reference} === undefined ? {{}} : {{ {property_value} }}) }})"

    calls = extract_calls(source)

    assert [(call.method, call.path) for call in calls] == [(method, "/api/x")]


@pytest.mark.parametrize(
    "spread",
    [
        "context",
        "(context.owner === undefined ? {} : context)",
        "(context.owner === undefined ? {} : { method: context.owner })",
        "(context.owner === undefined ? { body } : { owner: context.owner })",
        "(context.owner === undefined ? {} : { owner: context.owner, method: 'DELETE' })",
        "(context.owner === undefined ? {} : { ...context })",
        "(context.owner === undefined ? {} : { [field]: context.owner })",
        "(context.owner === undefined ? {} : { owner: mutate() })",
        "(context.owner === undefined ? {} : { owner: context.other })",
        "(context.owner === undefined ? {} : { get owner() { return context.owner } })",
        "(context.owner === undefined ? {} : { __proto__: context.owner })",
        "(mutate() === undefined ? {} : { owner: context.owner })",
    ],
)
def test_conditional_spread_rejects_unknown_routing_or_effects(spread: str) -> None:
    source = f"apiRequest('/api/x', {{ method: 'GET', ...{spread} }})"

    with pytest.raises(ExtractionError, match="top-level spread"):
        extract_calls(source)
