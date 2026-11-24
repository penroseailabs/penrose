"""For you: what penrose has for the user when they open it (app/briefings): the briefings from the apps they
chose, the chats that wait for their answer, and what else came in. The list itself reaches every screen with
the other updates (api/updates.py `for_you`); these are what the user does with it (open a briefing, look into
one, dismiss one, take a waiting chat off the list), and their choice of apps."""

import typing

from fastapi import APIRouter, HTTPException
from fastapi import Path as PathParam
from pydantic import BaseModel, Field

from ..agents.catalog import Models
from ..briefings import actions, feed, groups, policy
from . import chats, common, updates

router = APIRouter(prefix="/api/for-you")

# An Android package id, as the notification log holds it.
PACKAGE = r"^[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)+$"
Package = typing.Annotated[str, PathParam(pattern=PACKAGE, max_length=200)]
# A chat's id (store.uid).
ChatId = typing.Annotated[str, PathParam(pattern=r"^[0-9a-f]{8,64}$")]


class OpenInput(BaseModel):
    provider: typing.Literal["deepseek", "openai", "anthropic"] = "deepseek"
    # The user's picks for the agent and the subagent, as a message carries them (api/chats.py MessageInput).
    models: Models | None = None


class DismissInput(BaseModel):
    # Also stop briefing on this conversation of the app.
    mute: bool = False


class AppInput(BaseModel):
    on: bool


class ModelsInput(BaseModel):
    # The agent and subagent for sorting and preparing; None: the same as the user's latest task.
    models: Models | None = None


class MutedInput(BaseModel):
    package: str = Field(pattern=PACKAGE, max_length=200)
    key: str = Field(max_length=groups.KEY_CHARS)


def gone():
    return HTTPException(404, "That is no longer in the list")


def choices():
    """The apps the phone has had notifications from, the busiest first, the conversations the user muted, and the
    models sorting and preparing run on (None: the same as the user's tasks)."""
    value = policy.load(common.store)
    return {"apps": feed.apps(common.store), "muted": value["muted"], "models": policy.models(value)}


@router.get("")
def for_you():
    return feed.feed(common.store)


@router.get("/apps")
def apps():
    return choices()


@router.put("/apps/{package}")
def choose_app(package: Package, body: AppInput):
    actions.choose_app(common.store, package, body.on)
    # What the app showed today now waits to be sorted: the worker hears through its doorbell (app/bell.py).
    common.worker_pending = True
    common.worker_news.notify()
    updates.changed()
    return choices()


@router.put("/models")
def choose_models(body: ModelsInput):
    actions.choose_models(common.store, body.models.model_dump() if body.models else None)
    return choices()


@router.post("/unmute")
def unmute(body: MutedInput):
    actions.unmute(common.store, body.package, body.key)
    return choices()


@router.post("/{briefing_id}/open", status_code=202)
def open_briefing(briefing_id: int, body: OpenInput):
    """The user's tap on a briefing: its chat, begun with what arrived and a task on what can be done about it. A
    briefing already opened answers with its chat. The answer holds the chat as GET /api/chats/{id} gives it, so the
    window shows it without asking again: on the phone app each request crosses the tunnel."""
    opened = actions.start(common.store, briefing_id, body.models.agent.provider if body.models else body.provider,
                           body.models.model_dump() if body.models else None)
    if not opened:
        raise gone()
    chat_id, state = opened
    if state:
        common.store.event(state["id"], "queued", {})  # the worker hears, and the lists are pushed again
    else:
        updates.changed()
    return {"chat_id": chat_id, "run_id": state["id"] if state else None, "chat": chats.chat_detail(chat_id)}


@router.post("/{briefing_id}/dismiss")
def dismiss(briefing_id: int, body: DismissInput):
    if not actions.dismiss(common.store, briefing_id, mute=body.mute):
        raise gone()
    updates.changed()
    return feed.feed(common.store)


@router.post("/{briefing_id}/look")
def look(briefing_id: int):
    """The user looks into a briefing: it is seen (the badge counts it no more) and stays in the list as it was.
    Answers with its details (feed.detail)."""
    if not actions.look(common.store, briefing_id):
        raise gone()
    updates.changed()
    return feed.detail(common.store, briefing_id)


@router.post("/waiting/{chat_id}/dismiss")
def dismiss_waiting(chat_id: ChatId):
    """Take a chat that waits for the user off the list; its task is untouched (actions.hide_waiting)."""
    if not actions.hide_waiting(common.store, chat_id):
        raise gone()
    updates.changed()
    return feed.feed(common.store)
