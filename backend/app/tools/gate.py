"""The gate's tools: answer the message directly, or hand it to the orchestrator."""

from typing import Literal

from pydantic import Field

from .base import Tool


class Answer(Tool):
    """Answer the user yourself: the message needs nothing done on the phone and nothing live."""

    tool: Literal["answer"] = "answer"
    text: str = Field(description="The complete answer, in natural markdown.")
    spoken: str = Field(
        default="", description="With `voice` in what you read: the same, as you would say it aloud. One to three plain "
        "sentences, no markdown. Otherwise empty.",
    )


class Delegate(Tool):
    """Hand the message to the orchestrator, which plans the work and answers the user."""

    tool: Literal["delegate"] = "delegate"
    # First, so it streams to the chat at once: the orchestrator's first words can take half a minute or more.
    note: str = Field(default="", description="One short line to the user saying what is starting "
                      "now, in their language ('Comparing three long-weekend trips from Chicago.').")
    reason: str = Field(default="", description="Why, briefly, for the activity trace.")
