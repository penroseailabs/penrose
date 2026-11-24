"""Saved sign-ins: which accounts Google Password Manager has a passkey or password for, by page or app."""

import asyncio

from app.gateway import saved_signins
from app.gateway.saved_signins import match, site_of

INDEX = {
    "passwords": [
        {"realm": "https://www.amazon.in/", "user": "a@b.com"},
        {"realm": "https://lwa.amazon.in/", "user": "work@b.com"},
        {"realm": "https://www.amazon.com/", "user": "us@b.com"},
        {"realm": "android://hash==@com.netflix.mediaclient/", "user": "tv@b.com"},
        {"realm": "https://www.netflix.com/", "user": "web@b.com"},
        {"realm": "federation://accounts.google.com/x", "user": "skip@b.com"},
    ],
    "passkeys": [
        {"rp": "amazon.in", "user": "a@b.com"},
        {"rp": "amazon.in", "user": "api@c.in"},
        {"rp": "amazon.com", "user": "us@b.com"},
        {"rp": "www.linkedin.com", "user": "li@b.com"},
    ],
    "apps": {"com.netflix.mediaclient": ["https://www.netflix.com"]},
    "app_groups": {"in.amazon.mShop.android.shopping": ["https://www.amazon.in", "https://amazon.com"]},
}


def test_site_of_keeps_country_second_levels():
    assert site_of("www.amazon.in") == "amazon.in"
    assert site_of("www.amazon.co.uk") == "amazon.co.uk"
    assert site_of("m.uber.com.au") == "uber.com.au"
    assert site_of("localhost") == "localhost"


def test_a_page_gets_its_sites_passkeys_and_passwords():
    found = match(INDEX, url="https://www.amazon.in/ap/signin?x=1")
    assert found == {"site": "amazon.in", "passkeys": ["a@b.com", "api@c.in"], "passwords": ["a@b.com", "work@b.com"]}


def test_a_passkey_for_a_subdomain_does_not_cover_its_parent():
    assert match(INDEX, url="https://linkedin.com/login")["passkeys"] == []
    assert match(INDEX, url="https://www.linkedin.com/login")["passkeys"] == ["li@b.com"]


def test_an_app_gets_its_own_and_its_linked_websites():
    found = match(INDEX, package="com.netflix.mediaclient")
    assert found == {"site": "com.netflix.mediaclient", "passkeys": [], "passwords": ["tv@b.com", "web@b.com"]}


def test_an_app_without_strict_links_falls_back_to_its_brand_group():
    found = match(INDEX, package="in.amazon.mShop.android.shopping")
    assert found["passkeys"] == ["a@b.com", "api@c.in", "us@b.com"]
    assert found["passwords"] == ["a@b.com", "work@b.com", "us@b.com"]


def test_nothing_on_screen_matches_nothing():
    assert match(INDEX) == {"site": "", "passkeys": [], "passwords": []}
    assert match(INDEX, package="com.unknown") == {"site": "com.unknown", "passkeys": [], "passwords": []}


def test_the_index_is_read_once_until_googles_store_or_the_bridge_changes(monkeypatch):
    calls = []
    stamps = iter(["1-10\npackage:/a.apk", "1-10\npackage:/a.apk", "2-11\npackage:/a.apk"])

    async def su(slot, script, timeout=25):
        calls.append(script)
        if script.startswith("stat"):
            return next(stamps)
        return 'noise\n{"ok":true,"passwords":[],"passkeys":[{"rp":"x.com","user":"u"}],"apps":{},"app_groups":{}}\n'

    monkeypatch.setattr(saved_signins, "su", su)
    saved_signins.cache.clear()
    first = asyncio.run(saved_signins.index("s1"))
    again = asyncio.run(saved_signins.index("s1"))
    changed = asyncio.run(saved_signins.index("s1"))
    assert first == again == changed and first["passkeys"][0]["user"] == "u"
    assert sum("app_process" in c for c in calls) == 2  # the first read and after the change


def test_a_phone_without_google_data_or_an_old_bridge_has_none(monkeypatch):
    async def no_store(slot, script, timeout=25):
        return "\npackage:/a.apk" if script.startswith("stat") else ""

    monkeypatch.setattr(saved_signins, "su", no_store)
    saved_signins.cache.clear()
    assert asyncio.run(saved_signins.index("s1")) is None

    async def old_bridge(slot, script, timeout=25):
        return "1-10\npackage:/a.apk" if script.startswith("stat") else "Error: Could not find class app.relay.bridge.SavedSignins"

    monkeypatch.setattr(saved_signins, "su", old_bridge)
    assert asyncio.run(saved_signins.index("s1")) is None
