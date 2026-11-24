"""The title agent's tool: the name of a conversation."""

from typing import Literal

from pydantic import Field

from .base import Tool


class SetTitle(Tool):
    """Name the conversation, so the user can find it in the task list later."""

    tool: Literal["set_title"] = "set_title"
    title: str = Field(
        min_length=1, description="A short name for what the user asked for: two to six words, in the language they wrote in, "
        "no quotes and no trailing punctuation ('Groceries for Friday', 'Cancel the gym membership').",
    )
