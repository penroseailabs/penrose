"""The watch check agent's tool: whether a notification is what a watch waits for (app/watches.py)."""

from typing import Literal

from pydantic import Field

from .base import Tool


class Judge(Tool):
    """Say whether the notification is what the watch waits for."""

    tool: Literal["judge"] = "judge"
    arrived: bool = Field(
        description="True only when the notification itself shows that what the watch waits for has happened. "
        "False when it is about something else, is only related (the same sender, another order), or leaves it open.",
    )
    reason: str = Field(description="One sentence: what in the notification says so.")
