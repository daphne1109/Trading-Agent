"""Pydantic models for the Deriv messages the agent relies on."""

from __future__ import annotations

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


class Tick(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    symbol: str = Field(validation_alias=AliasChoices("symbol", "underlying_symbol"))
    epoch: int
    quote: float
    pip_size: int | None = None

    @classmethod
    def from_message(cls, msg: dict[str, object]) -> Tick:
        """Parse a `msg_type == "tick"` message."""
        return cls.model_validate(msg["tick"])
