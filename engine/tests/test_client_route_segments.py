from pathlib import Path

import pytest

from app.main import app
from route_dispatch import effective_routes
from test_client_route_resolution import PLACEHOLDER, ExtractionError, client_failures, extract_calls

WRAPPER = """
function requestOwned(lease: ControlLease, action: string, body: object = {}) {
  return apiRequest(`/api/agents/${lease.agentId}/control/${action}`, {method: 'POST', body});
}
"""


@pytest.mark.parametrize(
    ("argument", "endpoints"),
    [
        ("'click'", ("click",)),
        ('"click"', ("click",)),
        ("`click`", ("click",)),
        ("enabled ? 'click' : `drag`", ("click", "drag")),
    ],
)
def test_resolves_finite_forwarded_segments(argument: str, endpoints: tuple[str, ...]) -> None:
    source = WRAPPER + f"function click() {{ return requestOwned(lease, {argument}, {{x: 1}}); }}"
    source += "function screenshot() { return requestOwned(lease, 'screenshot'); }"
    calls = extract_calls(source, "client.ts")
    assert [(call.method, call.path, call.file, call.line, call.function) for call in calls] == [
        ("POST", f"/api/agents/{PLACEHOLDER}/control/{endpoint}", "client.ts", 3, "requestOwned")
        for endpoint in (*endpoints, "screenshot")
    ]


def test_resolves_real_control_owner_requests() -> None:
    root = Path(__file__).resolve().parents[2]
    file = "web-e/src/lib/control-api.ts"
    calls = [call for call in extract_calls((root / file).read_text(), file) if call.function == "ownerRequest"]
    assert [(call.method, call.path) for call in calls] == [
        ("POST", f"/api/agents/{PLACEHOLDER}/control/{endpoint}")
        for endpoint in ("unlock", "screenshot", "current-url", "click", "type", "press_keys", "drag")
    ]
    assert client_failures(calls, effective_routes(app), redirect_slashes=app.router.redirect_slashes) == []


def test_reports_unknown_concrete_forwarded_endpoint() -> None:
    source = WRAPPER + "requestOwned(lease, 'click'); requestOwned(lease, 'not-a-control-endpoint');"
    failures = client_failures(
        extract_calls(source, "client.ts"), effective_routes(app), redirect_slashes=app.router.redirect_slashes
    )
    assert failures == [
        f"POST /api/agents/{PLACEHOLDER}/control/not-a-control-endpoint (client.ts:3): no route matches"
    ]


@pytest.mark.parametrize(
    "extra",
    [
        "",
        "requestOwned(lease, action);",
        "requestOwned(lease, `click-${dynamic}`);",
        "requestOwned(lease);",
        "requestOwned(...args, 'click');",
        "requestOwned(lease, enabled ? 'click' : dynamic);",
        "requestOwned(lease, 'click'); requestOwned(lease, dynamic);",
        "const alias = requestOwned; alias(lease, 'click');",
        "requestOwned.call(null, lease, 'click');",
        "requestOwned(lease, '../click');",
        "requestOwned(lease, 'click?query=1');",
        r"requestOwned(lease, '\x63lick');",
        r"requestOwned(lease, '\u0063lick');",
        "requestOwned = other; requestOwned(lease, 'click');",
        "function shadow(requestOwned) { requestOwned(lease, 'click'); }",
        "`${requestOwned(lease, 'click')}`;",
    ],
)
def test_rejects_unresolved_forwarded_segments(extra: str) -> None:
    source = WRAPPER + extra
    with pytest.raises(ExtractionError, match="route segment") as error:
        extract_calls(source, "client.ts")
    assert "client.ts:3:" in str(error.value)


@pytest.mark.parametrize(
    "change",
    [
        "action = 'not-mounted';",
        "action ||= 'not-mounted';",
        "{ const action = 'not-mounted'; }",
        "function nested(action) {}",
        "`${action = 'not-mounted'}`;",
        "`${`${action = 'not-mounted'}`}`;",
        "arguments[1] = 'not-mounted';",
        "eval('action = unknown');",
    ],
)
def test_rejects_mutated_or_shadowed_segment_parameter(change: str) -> None:
    source = WRAPPER.replace("  return apiRequest", f"  {change}\n  return apiRequest")
    source += "requestOwned(lease, 'click');"
    with pytest.raises(ExtractionError, match="route segment"):
        extract_calls(source)


@pytest.mark.parametrize(
    "source",
    [
        "export " + WRAPPER.lstrip() + "requestOwned(lease, 'click');",
        WRAPPER.replace("action: string", "action: string = 'click'") + "requestOwned(lease);",
        WRAPPER.replace("body: object = {}", "body = (action = 'not-mounted')") + "requestOwned(lease, 'click');",
        WRAPPER.replace("body: object = {}", "body = `${action = 'not-mounted'}`") + "requestOwned(lease, 'click');",
        WRAPPER.replace("action: string", "{action}: Options") + "requestOwned(lease, {action: 'click'});",
        "function load(action: string) { return apiRequest(`/api/agents/${agentId}/control/${action}`); }",
        "apiRequest(`/api/agents/${agentId}/control/${operation}`);",
    ],
)
def test_rejects_open_ended_segment_bindings(source: str) -> None:
    with pytest.raises(ExtractionError, match="route segment"):
        extract_calls(source)


@pytest.mark.parametrize("expression", ["'click'", "`click`", "enabled ? 'click' : 'screenshot'"])
def test_resolves_finite_inline_segment_literals(expression: str) -> None:
    source = f"apiRequest(`/api/agents/${{agentId}}/control/${{{expression}}}`, {{method: 'POST'}});"
    calls = extract_calls(source)
    endpoints = ("click", "screenshot") if expression.startswith("enabled") else ("click",)
    assert [(call.method, call.path) for call in calls] == [
        ("POST", f"/api/agents/{PLACEHOLDER}/control/{endpoint}") for endpoint in endpoints
    ]


def test_resolves_forwarded_first_parameter() -> None:
    source = """
    function dispatch(action: string, agentId: string) {
      return apiRequest(`/api/agents/${agentId}/control/${action}`, {method: 'POST'});
    }
    dispatch('click', agentId);
    """
    calls = extract_calls(source)
    assert [(call.method, call.path) for call in calls] == [("POST", f"/api/agents/{PLACEHOLDER}/control/click")]
