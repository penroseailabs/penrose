"""Highways: for a few large apps, a faster and more exact way to read them than their screen, beside it.

The agent works in the app as always: it opens the app, taps, types and scrolls through Screen. A Highway changes
how it reads: while one of its screens is in front, the turn's state also carries "App data", the content of that
screen read from the app's own data on the phone (gateway/app_data.py), complete and exact where the screen is cut,
scrolled and partly drawn. A Highway may also offer actions (app_action) that are still done on the app's own
interface, the way a person would, but in one step and with a known outcome: open a chat by its link and wait until
the app has synced it, scroll a chat up until the app has loaded the next page of its history.

The rules every Highway keeps:
- It only adds. Screen, its numbered elements and every tool stay as they are, so when a Highway cannot read
  (the app changed its data, the phone said no, what it read does not match the screen) the agent has the screen
  it always had, and is told so in one line.
- What it shows is guaranteed: it says how fresh and how complete it is (synced or not, how much is on the phone),
  and it checks itself against the screen in front before it is shown.
- It does not rebuild what the app does better: a search runs on the app's own server, so it stays the app's search.

A Highway is a class with `package`, `name`, `file` (its database, for app_data), `block(screen)` -> the App data
text for the screen in front, or None for a screen it does not cover, and `act(action, item, text, items)` -> the
line for the agent. One instance per phone session, so it remembers what it opened and what it showed.
"""

import time
from datetime import UTC, timedelta, timezone

import httpx

from ..errors import DeviceError, safe_error


class HighwayError(Exception):
    """The Highway cannot read what it needs (an unexpected database, a failed query): the screen alone serves."""


class Highway:
    package = ""
    name = ""
    file = ""

    def __init__(self, session):
        self.s = session
        self.tz = UTC
        self.stamp = None  # changes whenever the app wrote
        self.file_name = None

    async def query(self, **queries):
        """{name: [row dicts]} for {name: (sql, params)}, from a copy of the app's database as of now."""
        body = await self.s.devices.app_db(self.s.slot, self.package, self.file,
                                           {name: [sql, list(params)] for name, (sql, params) in queries.items()})
        self.stamp, self.file_name = body.get("stamp"), body.get("file")
        self.tz = utc_offset(body.get("utc_offset"))
        out = {}
        for name, result in (body.get("results") or {}).items():
            if "error" in result:
                raise HighwayError(f"query {name}: {result['error']}")
            columns = result.get("columns") or []
            out[name] = [dict(zip(columns, row, strict=False)) for row in result.get("rows") or []]
        return out

    async def block(self, screen):
        return None

    async def act(self, action, item, text, items=()):
        return f"NOT RUN: {self.name} offers no action {action!r}"


def utc_offset(text):
    """The phone's "+0530" as a timezone."""
    text = str(text or "").strip()
    if len(text) == 5 and text[0] in "+-" and text[1:].isdigit():
        sign = -1 if text[0] == "-" else 1
        return timezone(sign * timedelta(hours=int(text[1:3]), minutes=int(text[3:5])))
    return UTC


def registry():
    from .teams import Teams

    return {cls.package: cls for cls in (Teams,)}


def highway(session, package):
    """The session's Highway for `package`, made the first time it is needed; None for an app without one."""
    cls = registry().get(package or "")
    if cls is None:
        return None
    kept = session.highways.get(package)
    if kept is None:
        kept = session.highways[package] = cls(session)
    return kept


# Shown only while the app is in front. Everything an agent needs to know about an app's Highway is here and in the
# Highway's own `guide`, never in the shared prompt or tool descriptions, which stay the same for every app.
HEADER = ("App data ({name}): this screen's content read from the app's own data on this phone, complete and exact "
          "where Screen is cut off or scrolled. Read from it, never by scrolling the screen or opening items one by "
          "one, whatever your brief says about scrolling or opening (it was written without App data). Act through "
          "Screen's numbered elements{actions}")


async def block(session):
    """The App data part of the turn's state for the screen in front, or "" when no Highway covers it. A Highway
    that cannot read says so in one line, and the agent goes on from Screen."""
    screen = session.driver.screen
    if not screen:
        return ""
    found = highway(session, screen.header.get("app"))
    if found is None:
        return ""
    started = time.monotonic()
    try:
        text = await found.block(screen)
    except (HighwayError, DeviceError, httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
        why = reason(exc)
        session.done(f"runtime: {found.name} App data unavailable: {why}"[:300])
        return f"App data ({found.name}): not available this turn ({clip(why, 160)}). Read Screen as usual."
    if not text:
        return ""
    found.last_ms = (time.monotonic() - started) * 1000
    guide = getattr(found, "guide", "")
    return HEADER.format(name=found.name, actions=f", and app_action: {guide}" if guide else ".") + "\n" + text


async def act(session, action, item=0, text="", items=()):
    """app_action: one of the actions of the Highway of the app in front (or of `text`'s app when none is)."""
    screen = session.driver.screen
    package = screen.header.get("app") if screen else None
    found = highway(session, package)
    if found is None:
        # The action may be how the agent gets into the app (open a Teams chat from anywhere).
        found = next((highway(session, p) for p in registry() if action in getattr(registry()[p], "anywhere", ())), None)
    if found is None:
        return "NOT RUN: app_action works only in an app with App data (it is shown in your state when it does)."
    try:
        return await found.act(action, item, text, list(items or ()))
    except (HighwayError, DeviceError, httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
        return f"FAILED: {reason(exc)}"


def reason(exc):
    """Why a Highway could not read, in words for the agent: its own reasons and the gateway's as they are,
    anything else as the safe generic line."""
    return str(exc) if isinstance(exc, (HighwayError, DeviceError)) and str(exc) else safe_error(exc)


def clip(text, length):
    text = " ".join(str(text or "").split())
    return text if len(text) <= length else text[: length - 1].rstrip() + "…"
