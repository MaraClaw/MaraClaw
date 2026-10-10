import pytest

from app.api import feishu


@pytest.mark.parametrize(
    "helper_name",
    [
        "DEFAULT_CONTEXT_WINDOW_SIZE",
        "_call_agent_llm",
        "_call_llm_with_config",
        "_get_llm_timeout",
        "_LLM_TIMEOUT_SECONDS_DEFAULT",
    ],
)
def test_shared_llm_export_is_retired_from_feishu_router(helper_name: str) -> None:
    # Given: the loaded router, whose own model loader remains an ordinary import.
    # When
    exported = hasattr(feishu, helper_name)

    # Then
    assert not exported
