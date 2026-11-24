"""When it is for the user: the clock fields of an agent's context, which change on every call and so go last."""

from datetime import datetime, timedelta, timezone

CLOCK_KEYS = ("current_datetime_utc", "local_time", "coming_days")


def local_clock(store, now=None):
    """The user's own date, weekday and time, and the next two weeks' days, when their time zone is known. From
    UTC and a zone name alone, the agents worked out the day themselves: "tomorrow" or "this Friday" lands on the
    wrong day when UTC has already passed midnight, and "the Monday after next" from a Sunday came out a week late
    (2026-11-15)."""
    if not store.user_locale().get("timezone") or (zone := store.zone()) is None:
        return {}
    local = (now or datetime.now(timezone.utc)).astimezone(zone)
    return {
        "local_time": f"{local:%A %d %B %Y, %H:%M} ({zone.key})",
        "coming_days": ", ".join(f"{local + timedelta(days=i):%a %d %b}" for i in range(1, 15)),
    }
