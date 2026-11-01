"""Lock-held guest configuration write for the model chosen to run."""

from __future__ import annotations

from app.records.agent import AgentRecord
from app.records.llm import LLMModelRecord
from app.services.agent_manager import agent_manager
from app.services.llm.turn import ModelBundle


class GuestConfigWriteError(RuntimeError):
    """Turn preparation failed before the guest configuration was ready."""


def apply_guest_model(agent: AgentRecord, bundle: ModelBundle, selected: LLMModelRecord | None) -> None:
    """Rewrite ``openclaw.json`` with ``selected`` as the guest model.

    The caller must already be inside ``guest_write_section``: this never takes the workspace flock again
    (it is not recursive) and holds no database row lock of its own.
    """
    try:
        _ = agent_manager.write_guest_config(
            agent,
            primary=bundle.primary,
            secondary=bundle.secondary,
            fallback=bundle.fallback,
            selected=selected,
        )
    except Exception as exc:
        # Boundary: any file/token/OAuth sync failure means "not applied"; callers keep the turn pending.
        raise GuestConfigWriteError("Guest configuration could not be updated. Please try again.") from exc
