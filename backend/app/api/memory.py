"""What Relay remembers across tasks, and the user's profile (the Google account on the phone, their time zone
and language)."""

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..agents.catalog import Pick
from ..agents.llm import LLM
from ..android import LANGUAGE, TIME_ZONE
from ..runtime import memory as memory_runtime
from . import common
from .chats import MessageInput

router = APIRouter(prefix="/api")

# The memory agent's model, opened on the first note the user adds.
model: LLM | None = None


def the_model():
    global model
    if model is None:
        model = LLM()
    return model


class MemorySettings(BaseModel):
    auto: bool


class MemoryEdit(BaseModel):
    # Generous: the memory agent's notes may run past the lengths it is asked for, and must stay editable.
    content: str = Field(default="", max_length=4000)  # a memory may be its line alone
    description: str | None = Field(default=None, max_length=1000)
    expires: str | None = Field(default=None, pattern=r"^(\d{4}-\d{2}-\d{2})?$")


class MemoryNote(MessageInput):
    agent: Pick | None = None  # the agent picked in the composer: the memory agent runs on it


def picked(agent):
    return agent.model_dump() if agent else None


class LocaleInput(BaseModel):
    timezone: str = Field(pattern=TIME_ZONE, max_length=64)
    language: str = Field(pattern=LANGUAGE, max_length=35)
    # The phone the window or companion app is showing: its clock goes on this time zone now, not at the next task.
    device: str | None = Field(default=None, max_length=64)


@router.get("/profile")
async def profile():
    return {"google_account": common.store.google_account(), "locale": common.store.user_locale()}


@router.post("/profile/locale")
async def set_locale(body: LocaleInput):
    """The user's time zone and language, from the browser or phone they are using Relay on: what the agents read
    (`locale`) and the phone's clock. Never the server's own."""
    common.store.set_user_locale(body.timezone, body.language)
    if body.device:
        try:
            await common.gateway.post(f"/devices/{body.device}/timezone", json={"zone": body.timezone}, timeout=30)
        except httpx.HTTPError:
            pass  # the runner sets it before the next task
    return common.store.user_locale()


@router.get("/memories")
async def memories():
    """Every memory, newest first."""
    return {"memories": common.store.memories()}


@router.post("/memories", status_code=201)
async def add_memory(body: MemoryNote):
    """A memory the user writes in the panel. The memory agent names and types it and merges it with what
    is there; when it cannot run, the note is kept as written. Returns what was saved, removed and declined
    (never kept, with why)."""
    content = body.content.strip()
    if not content:
        raise HTTPException(422, "Write something to remember.")
    saved, removed, placed, declined = await memory_runtime.add(
        the_model(), common.store, content, provider=body.provider, agent=picked(body.agent))
    return {"saved": saved, "removed": removed, "placed": placed, "declined": declined}


@router.put("/memories/{memory_id}")
async def edit_memory(memory_id: str, body: MemoryEdit):
    """The user's own edit: kept word for word, under the same name. Returns the row."""
    row = common.store.update_memory(memory_id, body.content.strip(), body.description, body.expires)
    if not row:
        raise HTTPException(404, "Memory not found")
    return row


@router.delete("/memories/{memory_id}")
async def delete_memory(memory_id: str):
    """Forget a memory: its line leaves the index with it."""
    common.store.execute("DELETE FROM memories WHERE id=?", (memory_id,))
    return {"deleted": True}


@router.get("/memory-settings")
async def memory_settings():
    return {"auto": common.store.profile_get("auto_memory", True)}


@router.put("/memory-settings")
async def update_memory_settings(body: MemorySettings):
    common.store.profile_set("auto_memory", body.auto)
    return {"auto": body.auto}
