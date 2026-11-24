"""The user's choices about briefings, kept in the profile: the apps whose notifications penrose looks at, the
conversations of those apps the user muted, and the models what notifications start runs on. Nothing here is
decided by a model."""

import json

from ..agents.catalog import Models

# Profile key: {"apps": [package ids], "muted": [{"package", "key", "name", "label"}], "models": picks or None}.
KEY = "briefing_policy"
# The most apps and muted conversations kept: the lists are the user's own, and never grow without them.
MAX_APPS, MAX_MUTED = 60, 200


def read(db):
    """The policy as saved, inside the caller's transaction: {"apps": [...], "muted": [...]}."""
    row = db.execute("SELECT value FROM profile WHERE key=?", (KEY,)).fetchone()
    saved = json.loads(row["value"]) if row else {}
    return {"apps": list(saved.get("apps") or []), "muted": list(saved.get("muted") or []), "models": saved.get("models")}


def write(db, value):
    db.execute("INSERT OR REPLACE INTO profile VALUES (?,?)", (KEY, json.dumps(value)))


def load(store):
    with store.connect() as db:
        return read(db)


def models(value):
    """The agent and subagent picks (agents/catalog.py) for what notifications start on their own: sorting them and
    preparing a briefing. None: the same as the user's latest task, and also when a pick is one penrose no longer
    offers."""
    try:
        return Models.model_validate(value["models"]).model_dump() if value.get("models") else None
    except ValueError:
        return None


def with_models(value, picks):
    return {**value, "models": Models.model_validate(picks).model_dump() if picks else None}


def muted(value, package, key):
    """Whether the user muted this conversation (`key`: groups.key_of) of the app."""
    return any((m.get("package"), m.get("key")) == (package, key) for m in value["muted"])


def briefs_on(value, package, key):
    """Whether a notification from this conversation of the app joins a briefing."""
    return package in value["apps"] and not muted(value, package, key)


def with_app(value, package, on):
    """The policy with the app turned on (the newest last) or off."""
    apps = [a for a in value["apps"] if a != package]
    return {**value, "apps": ([*apps, package] if on else apps)[-MAX_APPS:]}


def with_muted(value, package, key, name, label):
    if muted(value, package, key):
        return value
    entry = {"package": package, "key": key, "name": name, "label": label}
    return {**value, "muted": [*value["muted"], entry][-MAX_MUTED:]}


def without_muted(value, package, key):
    return {**value, "muted": [m for m in value["muted"] if (m.get("package"), m.get("key")) != (package, key)]}
