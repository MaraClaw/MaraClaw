from pathlib import Path

import pytest

from test_client_route_resolution import ExtractionError, extract_calls


@pytest.mark.parametrize(("binding", "property_name"), [("transport", "url"), ("wire", "resolve")])
@pytest.mark.parametrize("fallback", ["", "handlers.transport ?? "])
def test_extracts_websocket_alias_in_nested_function(binding: str, property_name: str, fallback: str) -> None:
    # Given a literal fallback, not a promise about an injected implementation.
    source = f"""
    function connect(url: string) {{ return new WebSocket(url); }}
    function chat(handlers: Handlers) {{
      const {binding}: Transport = {fallback}{{connect: connect, {property_name}: wsUrl, language: 'en'}};
      function open() {{ {binding}.connect({binding}.{property_name}(`/ws/chat/${{agentId}}`)); }}
      open();
    }}
    """
    # When extracting through the alias.
    calls = extract_calls(source, "client.ts")
    # Then the call site retains its routing identity and source provenance.
    assert [(call.protocol, call.method, call.path, call.file, call.function) for call in calls] == [
        ("websocket", "WEBSOCKET", "/ws/chat/00000000-0000-4000-8000-000000000000", "client.ts", "open")
    ]
    assert "fallback" in calls[0].resolution_reason


@pytest.mark.parametrize(
    ("declaration", "extra"),
    [
        ("let transport = {url: wsUrl}", ""),
        ("const transport = {url: wsUrl, url: unknown}", ""),
        ("const transport = {url: unknown, url: wsUrl}", ""),
        ("const transport = {...unknown, url: wsUrl}", ""),
        ("const transport = {url: wsUrl, ...unknown}", ""),
        ("const transport = {url: wsUrl, [key]: unknown}", ""),
        ("const transport = {url: wsUrl}", "transport.url = unknown;"),
        ("const transport = {url: wsUrl}", "transport.url ||= unknown;"),
        ("const transport = {url: wsUrl}", "const wsUrl = unknown;"),
        ("const transport = {url: wsUrl}", "const {wsUrl} = unknown;"),
        ("const transport = {url: wsUrl}", "transport = other;"),
        ("const transport = {url: wsUrl}", "mutate(transport);"),
        ("const transport = {url: wsUrl}", "const escape = transport;"),
        ("const transport = {url: wsUrl}", "const escape = transport.url;"),
        ("const transport = {url: wsUrl}", "function shadow(transport) { transport.url('other'); }"),
        ("const transport = {url: wsUrl}", "{ const transport = other; }"),
        ("const transport = {url: wsUrl}", "delete transport.url;"),
        ("const transport = {url: wsUrl}", "transport['url'] = unknown;"),
        ("const transport = {url: wsUrl}", "`${mutate(transport)}`;"),
        ("const transport = {url: wsUrl, connect: unknown}", "transport.connect();"),
        ("const transport = {url: wsUrl}", "transport.url.call(other, 'anything');"),
        ("const transport = {url: wsUrl}", "transport.url(dynamicPath);"),
        ("const transport = {url: wsUrl}", "const path = '/api/unclassified';"),
        ("const transport = {url: wsUrl}", "const hidden = apiRequest;"),
        ("const transport = {url: wsUrl}", "with (other) { transport.url('anything'); }"),
        (
            "const transport = {url: wsUrl, connect: connect}",
            "function connect() { this.url = unknown; } transport.connect();",
        ),
    ],
)
def test_rejects_unproven_websocket_alias(declaration: str, extra: str) -> None:
    # Given mutation, ambiguous bindings, or unknown routing effects.
    source = f"function chat() {{ {declaration}; {extra} transport.url('/ws/chat/a'); }}"
    # When/Then the guard fails closed rather than consuming the path.
    with pytest.raises(ExtractionError):
        extract_calls(source)


def test_real_socket_alias_keeps_real_call_site() -> None:
    # Given the current production source, including its injectable transport.
    root = Path(__file__).resolve().parents[2]
    file = "web-e/src/lib/chat/ws-client.ts"
    source = (root / file).read_text()
    # When extracting the literal fallback branch.
    calls = extract_calls(source, file)
    # Then the nested real call is present, not skipped or replaced by a fixture.
    assert [(call.protocol, call.path, call.file, call.function) for call in calls] == [
        ("websocket", "/ws/chat/00000000-0000-4000-8000-000000000000", file, "open")
    ]


def test_object_method_declaration_does_not_hide_body_calls() -> None:
    # Given a transport method declaration containing a real call.
    source = "const client = { async apiRequest<T>(path: string): Promise<T> { apiRequest('/api/health'); } };"
    # When extracting declarations and calls together.
    calls = extract_calls(source)
    # Then only the declaration is excluded, not its body.
    assert [(call.method, call.path) for call in calls] == [("GET", "/api/health")]


def test_destructured_helper_declarations_do_not_hide_calls() -> None:
    source = "export const { apiRequest, apiFormRequest } = makeClient(); apiRequest('/api/health');"
    calls = extract_calls(source)
    assert [(call.method, call.path) for call in calls] == [("GET", "/api/health")]
