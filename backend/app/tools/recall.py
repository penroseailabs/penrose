"""Recall: a search of earlier conversations, for the gate.

The gate reads the conversation and the memories; what was said in another conversation ("the hotel I booked
last month", "what did we order last time", "what did I get done last week") is in neither. `recall` searches
every earlier conversation for it, by meaning (the agent writes the thing several ways; words match by their
stem) or by time. The runtime runs the search, shows it in the chat with links to the conversations it found,
and calls the agent again with `recalled`; the agent then answers or plans from what came back.
"""

from typing import Literal

from pydantic import Field, model_validator

from .base import Tool

DAY = r"^(\d{4}-\d{2}-\d{2})?$"


class Recall(Tool):
    """Search earlier conversations for something the user refers to that this conversation and the
    memories do not hold: an earlier order, booking, search or answer ("the hotel I booked last month",
    "what did we order last time", "that restaurant you found"), or what happened in a stretch of time
    ("what did I do last week", "yesterday's order"). Search by meaning: write the thing two to four ways,
    the user's words and the words the earlier conversation would have used ("hotel", "stay", "Airbnb",
    "Marriott booking"). For a time, set `after` and/or `before`; with no queries you get every conversation
    of that time, summed up. Once per message: you are called again with `recalled` and decide from it.
    Never for what is on the phone now, and never when the conversation or a memory already says it."""

    tool: Literal["recall"] = "recall"
    queries: list[str] = Field(
        default_factory=list, max_length=4,
        description="One to four short wordings of the thing: names, places, products, the words a reply about it "
        "would use. Empty only when searching a stretch of time.",
    )
    after: str = Field(
        default="", pattern=DAY,
        description="YYYY-MM-DD: the first day to search, in the user's time zone (work it out from "
        "current_datetime_utc and locale: 'last week', 'in September'). Empty for no start.",
    )
    before: str = Field(default="", pattern=DAY, description="YYYY-MM-DD: the last day to search, included. Empty for up to now.")

    @model_validator(mode="before")
    @classmethod
    def one_query_is_a_list_of_one(cls, data):
        if isinstance(data, dict) and isinstance(data.get("query"), str) and not data.get("queries"):
            data = {**data, "queries": [data["query"]]}
        return data

    @model_validator(mode="after")
    def something_to_search(self):
        self.queries = [q.strip()[:100] for q in self.queries if q.strip()]
        if not self.queries and not (self.after or self.before):
            raise ValueError("recall needs queries, or a stretch of time (after and/or before)")
        return self


def recalled(store, recall, chat_id=None):
    """What a `recall` brings back, as the agent reads it: {queries, after, before, found}, `found` being the
    matching conversations (each with its matches or its summary), or a line saying there are none."""
    after, before = store.day_bounds(recall.after or None, recall.before or None)
    if recall.queries:
        found = store.search_history(recall.queries, exclude_chat=chat_id, after=after, before=before)
    else:
        found = store.conversations_between(after, before, exclude_chat=chat_id)
    span = " ".join(p for p in (recall.after and f"from {recall.after}", recall.before and f"to {recall.before}") if p)
    what = ", ".join(repr(q) for q in recall.queries) or "anything"
    return {
        "queries": recall.queries, "after": recall.after, "before": recall.before,
        "found": found or f"No earlier conversation mentions {what}{f' {span}' if span else ''}.",
    }


def searched(queries, found, after="", before=""):
    """The `chats_searched` event the chat shows as a tool call: what was searched and the conversations
    found, each a link."""
    return {
        "queries": list(queries), "after": after, "before": before,
        "found": [{"chat_id": c["chat_id"], "title": c["title"], "date": c["date"]} for c in found if isinstance(c, dict)]
        if isinstance(found, list) else [],
    }
