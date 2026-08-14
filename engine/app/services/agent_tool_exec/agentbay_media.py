from __future__ import annotations

import uuid
from pathlib import Path

from app.core.logging import logger


def _agentbay_normalize_image_bytes(data: object) -> bytes | None:
    """Normalize AgentBay image payloads to raw bytes."""
    import base64 as _base64

    if isinstance(data, str):
        if data.startswith("data:image"):
            data = data.split(",", 1)[1]
        return _base64.b64decode(data)
    if isinstance(data, bytes):
        return data
    return None


def _agentbay_save_image_to_workspace(
    *,
    agent_id: uuid.UUID,
    ws: Path,
    raw_bytes: bytes,
    prefix: str,
    label: str,
) -> str:
    """Save an explicitly requested screenshot under workspace/screenshots/."""
    import time as _time

    rel_path = f"workspace/screenshots/{prefix}-{int(_time.time())}.png"
    screenshot_path = ws / rel_path
    screenshot_path.parent.mkdir(parents=True, exist_ok=True)
    _ = screenshot_path.write_bytes(raw_bytes)
    logger.info(f"[AgentBay] Explicit screenshot saved to workspace: {rel_path}")
    return f"Screenshot saved to `{rel_path}`.\n![{label}](/api/agents/{agent_id}/files/download?path={rel_path})"
