"""Structured output models for the DeepSeek classifier."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, StrictBool

Category = Literal["informational", "reminder", "financial", "personal", "other"]
Importance = Literal["low", "normal", "high"]


class Classification(BaseModel):
    """Semantic classification of an incoming message.

    The LLM only performs semantic interpretation. These values are validated by
    Pydantic and consumed by Python application code; the LLM never writes them
    to the database directly.

    ``action_required`` uses :class:`StrictBool` so a malformed value such as
    ``"true"``, ``"yes"`` or ``1`` is rejected instead of being silently coerced
    into a boolean (which would hide an LLM formatting error).
    """

    category: Category
    importance: Importance
    summary: str = Field(min_length=1)
    action_required: StrictBool
