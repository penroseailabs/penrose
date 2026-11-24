"""The sorting agent's tool: what a group of notifications is to the user (app/briefings/triage.py)."""

from typing import Literal

from pydantic import Field

from .base import Tool


class Sort(Tool):
    """Say what these notifications are to the user."""

    tool: Literal["sort"] = "sort"
    verdict: Literal["ignore", "list", "brief"] = Field(
        description="ignore: noise. list: about the user's world, with nothing to prepare. brief: the user would "
        "want to know, and there is something penrose could usefully read up on or do.",
    )
    headline: str = Field(
        max_length=120,
        description="The one thing to know, in your own words, in the user's language: at most ten words, no full "
        "stop ('Landlord moved the inspection to Friday', 'Dad asks if Saturday still works'). Never a copy of the "
        "notification, never the app's name.",
    )
    asks: str = Field(
        default="", max_length=200,
        description="What is asked of the user or waits on them, with who asks, in a sentence ('Sam asks if you can "
        "bring chairs on Saturday.'). Empty when nothing is.",
    )
    gist: list[str] = Field(
        default_factory=list, max_length=4,
        description="For brief and list: the one to four things said that matter, each a short line with its "
        "names, numbers and times ('Coach moved the Saturday game to 10 am at Field 4').",
    )
    why: str = Field(
        default="", max_length=240,
        description="For brief and list: one sentence on what makes it this user's, from what you know about them "
        "('You are hosting this dinner.'). Empty when nothing you know says.",
    )
    search: list[str] = Field(
        default_factory=list, max_length=4,
        description="For brief and list: two to four short wordings the same matter goes by in the user's earlier "
        "conversations and memories: the project, the people, an alias ('kitchen remodel', 'Dana', 'spring offsite').",
    )
    urgent: bool = Field(
        default=False, description="True only when it names a time within the next few hours or says it cannot wait.",
    )
