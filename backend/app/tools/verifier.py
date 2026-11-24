"""The verifier's own tool: its verdict."""

from typing import Literal

from pydantic import Field

from .base import FinalTool


class Verdict(FinalTool):
    """End the check. Alone in its turn, after you have looked for yourself."""

    tool: Literal["verdict"] = "verdict"
    passed: bool = Field(description="true only when what you saw on the phone shows the claim is true.")
    reason: str = Field(
        description="What you saw, in a sentence or two. When it fails: what is missing or different, and "
        "whether it looks like a mistake to correct or a blocker.",
    )
    evidence: list[str] = Field(
        default_factory=list, max_length=6,
        description="The screen text or event that settles it, quoted briefly ('Cart: Horizon Organic whole milk (half gallon) × 2').",
    )
