"""The agents get the user's own day and weekday, worked out by the runtime, not from UTC by the model."""

from datetime import datetime, timezone

from app.runtime.clock import local_clock


class Store:
    def __init__(self, zone):
        self.locale = {"timezone": zone} if zone else {}

    def user_locale(self):
        return self.locale

    def zone(self):
        from zoneinfo import ZoneInfo
        return ZoneInfo(self.locale.get("timezone") or "UTC")


def test_the_local_day_when_utc_has_passed_midnight():
    # 02:30 UTC on a Saturday is still Friday evening in California: "tomorrow" is Saturday there.
    clock = local_clock(Store("America/Los_Angeles"), datetime(2026, 11, 14, 2, 30, tzinfo=timezone.utc))
    assert clock["local_time"] == "Friday 13 November 2026, 18:30 (America/Los_Angeles)"
    assert clock["coming_days"].startswith("Sat 14 Nov, Sun 15 Nov, Mon 16 Nov")
    assert clock["coming_days"].endswith("Fri 27 Nov")


def test_no_zone_no_guess():
    assert local_clock(Store(None)) == {}
