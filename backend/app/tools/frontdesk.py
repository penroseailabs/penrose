"""The front desk's tool: where the user's spoken words go, and the few words said back (runtime/frontdesk.py)."""

from typing import Literal

from pydantic import Field

from .base import Tool


class Route(Tool):
    """Say which conversation the user's words belong to, and acknowledge them in a few spoken words."""

    tool: Literal["route"] = "route"
    chat_id: str = Field(
        default="", max_length=64,
        description="The conversation to continue: an id from `focus`, `waiting`, `recent` or `related`. Empty to "
        "start a new one.",
    )
    code: bool = Field(
        default=False,
        description="The words are the one-time code (or password) a step in that conversation waits for: digits said "
        "aloud and nothing else of substance.",
    )
    acknowledgment: str = Field(
        description="One short sentence said back at once, in the user's language: where the request went, or what "
        "starts. No result, promise or question.",
    )
