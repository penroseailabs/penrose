"""The compaction agent's tool: the older part of a conversation, summed up."""

from typing import Literal

from pydantic import Field

from .base import Tool


class Summarize(Tool):
    """Write the summary that stands in for the messages you were given. Every agent that works in this chat
    reads it in place of them, before the messages that follow, so it must carry everything a later turn could
    need from them."""

    tool: Literal["summarize"] = "summarize"
    summary: str = Field(
        min_length=1, description="The summary, in plain prose and short lists, in the order things happened. It folds "
        "`summary_so_far` and `messages` into one: what the user asked for and what came of each request, the "
        "exact values they gave, decisions and corrections, what is still open. Nothing invented, nothing from "
        "outside the messages.",
    )
