from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


class TransferPolicy(BaseModel):
    """Full replacement payload and response; amounts are integer minor units."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    fee_amount_minor: int = Field(strict=True, ge=0, le=9007199254740991)
    currency: Literal["USD", "EUR", "GBP", "VND", "JPY", "KWD"]
    instructions: Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=4000)]
