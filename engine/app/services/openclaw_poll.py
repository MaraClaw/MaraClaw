import json
from pathlib import Path

from app.services import guest_bootstrap


def write_poll_dispatcher(
    agent_dir: Path, base_url: str, *, hooks_url: str = "http://127.0.0.1:18789/hooks/agent"
) -> Path:
    destination = agent_dir / ".maraclaw-poll.mjs"
    template = Path(__file__).resolve().parents[2] / "connectors" / "openclaw-dispatcher" / "dispatcher.mjs"
    source = (
        template.read_text(encoding="utf-8")
        .replace("GUARD_PATH", json.dumps(guest_bootstrap.BOOTSTRAP_HELPER))
        .replace("BASE_URL", json.dumps(base_url))
        .replace("HOOKS_URL", json.dumps(hooks_url))
    )
    destination.write_text(source, encoding="utf-8")
    return destination
