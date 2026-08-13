import sys
import uuid
import warnings
from datetime import UTC, datetime
from types import SimpleNamespace

from pydantic.warnings import PydanticDeprecatedSince20


def test_pydantic_models_do_not_use_deprecated_class_config() -> None:
    for module_name in (
        "app.api.chat_sessions",
        "app.api.plaza",
        "app.services.sandbox.config",
    ):
        sys.modules.pop(module_name, None)

    with warnings.catch_warnings():
        warnings.simplefilter("error", PydanticDeprecatedSince20)
        from app.api.chat_sessions import SessionOut
        from app.api.plaza import CommentOut, PostOut
        from app.services.sandbox.config import SandboxConfig, SandboxType

    now = datetime.now(UTC)

    explicit_sandbox = SandboxConfig(type=SandboxType.DOCKER)
    assert SandboxConfig.__doc__ == "Configuration for sandbox backend."
    assert explicit_sandbox.model_dump()["type"] == "docker"

    fallback_sandbox = SandboxConfig.from_dict({"sandbox_type": "unknown"})
    assert fallback_sandbox.type == SandboxType.SUBPROCESS.value

    session = SessionOut.model_validate(
        SimpleNamespace(
            id="session-1",
            agent_id="agent-1",
            user_id="user-1",
            title="Support",
            created_at=now.isoformat(),
        )
    )
    assert session.source_channel == "web"
    assert session.participant_type == "user"

    post = PostOut.model_validate(
        SimpleNamespace(
            id=uuid.uuid4(),
            author_id=uuid.uuid4(),
            author_type="human",
            author_name="Alice",
            content="hello",
            likes_count=1,
            comments_count=0,
            created_at=now,
        )
    )
    assert post.content == "hello"

    comment = CommentOut.model_validate(
        SimpleNamespace(
            id=uuid.uuid4(),
            post_id=uuid.uuid4(),
            author_id=uuid.uuid4(),
            author_type="human",
            author_name="Alice",
            content="reply",
            created_at=now,
        )
    )
    assert comment.content == "reply"
