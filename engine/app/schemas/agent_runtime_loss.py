from datetime import datetime
from typing import ClassVar

from pydantic import BaseModel, ConfigDict

from app.records.agent_runtime_loss import AgentRuntimeLoss, RuntimeLossReason


class AgentRuntimeLossOut(BaseModel):
    """A recorded observation that the agent's guest container is gone and must be started anew."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    reason: RuntimeLossReason
    detected_at: datetime


def runtime_loss_out(loss: AgentRuntimeLoss | None) -> AgentRuntimeLossOut | None:
    return AgentRuntimeLossOut(reason=loss.reason, detected_at=loss.detected_at) if loss is not None else None
