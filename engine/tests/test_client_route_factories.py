import pytest

from test_client_route_resolution import ExtractionError, extract_calls


@pytest.mark.parametrize("terminator", [";", ""])
@pytest.mark.parametrize("returned", ["apiRequest", "{ apiRequest }"])
def test_extracts_calls_when_factory_returns_declared_overloaded_helper(terminator: str, returned: str) -> None:
    binding = "apiRequest" if returned == "apiRequest" else "{ apiRequest }"
    source = f"""
    function createApiRequest() {{
      function apiRequest<T>(path: string, options: Options<T>): Promise<T>{terminator}
      function apiRequest<T = unknown>(path: string): Promise<T>{terminator}
      async function apiRequest<T>(path: string): Promise<T> {{
        apiRequest('/api/health');
        return transport(path);
      }}
      return {returned}{terminator}
    }}
    export const {binding} = createApiRequest();
    apiRequest('/api/users');
    """

    calls = extract_calls(source)

    assert [(call.method, call.path) for call in calls] == [("GET", "/api/health"), ("GET", "/api/users")]


@pytest.mark.parametrize(
    "source",
    [
        "function factory() { return apiRequest; }",
        "function factory(apiRequest) { return apiRequest; }",
        "function factory() { const apiRequest = other; return apiRequest; }",
        "function factory() { const value = function apiRequest() {}; return apiRequest; }",
        "function factory() { function apiRequest(): Promise<void>; return apiRequest; }",
        "function factory() { { function apiRequest() {} } return apiRequest; }",
        "function factory() { function apiRequest() {} { return apiRequest; } }",
        "function factory() { function apiRequest() {} function nested(apiRequest) { return apiRequest; } }",
        "function factory() { return { apiRequest }; }",
        "function factory() { { function apiRequest() {} } return { apiRequest }; }",
        "function factory() { function apiRequest() {} load().then(apiRequest); }",
        "function factory() { function apiRequest() {} const alias = apiRequest; return alias; }",
        "function factory() { function apiRequest() {} return apiRequest || other; }",
        "function factory() { function apiRequest() {} return\napiRequest; }",
    ],
)
def test_rejects_unproven_factory_helper_reference(source: str) -> None:
    with pytest.raises(ExtractionError, match="without a direct call"):
        extract_calls(source)


@pytest.mark.parametrize("canonical", ["", "const apiRequest = factory();"])
@pytest.mark.parametrize(
    "consumer", ["const send = factory(); send(runtimePath, {method: 'DELETE'});", "const send = factory"]
)
def test_rejects_factory_result_bound_to_unclassified_alias(canonical: str, consumer: str) -> None:
    source = f"""
    function factory() {{
      function apiRequest(path: string, options: Options) {{ return transport(path, options); }}
      return apiRequest;
    }}
    {canonical}
    {consumer}
    """

    with pytest.raises(ExtractionError):
        extract_calls(source)
