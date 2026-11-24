"""The gateway's reading of the user's own browsing for look_up: what they searched for and the pages they went to
on their other devices, as Chrome keeps it on the phone. Only what has a query's words, never penrose's own."""

import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from app.config import settings
from app.gateway import app_data, core, history

LAPTOP, TEST_PHONE = "laptop-sync-id-0123456789", "testphone-sync-id-012345"
NOON_20_NOV = datetime(2026, 11, 20, 12, tzinfo=timezone.utc).timestamp()


def at(days_ago, hours=0):
    """Chrome's time for `days_ago` days (and `hours`) before noon on 20 November 2026, UTC."""
    return int((NOON_20_NOV - days_ago * 86400 - hours * 3600 + history.CHROME_EPOCH) * 1_000_000)


class Chrome:
    """A History database as Chrome keeps it, and the gateway's read-only runner over it."""

    def __init__(self, tmp_path):
        self.copy = SimpleNamespace(path=lambda part: tmp_path / part, work_stamp=None)
        self.db = sqlite3.connect(tmp_path / "main.db")
        self.db.executescript("""
            CREATE TABLE urls(id INTEGER PRIMARY KEY, url TEXT, title TEXT);
            CREATE TABLE visits(id INTEGER PRIMARY KEY, url INTEGER, visit_time INTEGER, originator_cache_guid TEXT);
            CREATE TABLE keyword_search_terms(keyword_id INTEGER, url_id INTEGER, term TEXT, normalized_term TEXT);""")
        self.reads = 0

    def visit(self, url, title, *times, device=LAPTOP, searched=None):
        page = self.db.execute("INSERT INTO urls(url, title) VALUES (?,?)", (url, title)).lastrowid
        self.db.executemany("INSERT INTO visits(url, visit_time, originator_cache_guid) VALUES (?,?,?)",
                            [(page, when, device) for when in times])
        if searched:
            self.db.execute("INSERT INTO keyword_search_terms VALUES (2,?,?,?)", (page, searched, searched.lower()))
        self.db.commit()

    async def query(self, slot, body):
        assert (body.package, body.folder, body.file) == ("com.android.chrome", "app_chrome/Default", "History")
        self.reads += 1
        results = await asyncio.to_thread(app_data.run_queries, self.copy, str(self.reads), body.queries)
        return {"ok": True, "file": "History", "stamp": str(self.reads), "utc_offset": "-0600", "results": results}


@pytest.fixture
def chrome(tmp_path, monkeypatch):
    chrome = Chrome(tmp_path)
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(app_data, "query", chrome.query)

    async def preferences(slot, script, timeout=25):
        return '"cache_guid":"this-phone-sync-id-01234"\n'

    monkeypatch.setattr(app_data, "sh", preferences)
    monkeypatch.setattr(history, "recorded", set())
    return chrome


async def found(queries):
    return {r["query"]: r["found"].splitlines() for r in await history.history("device-1", queries)}


async def test_a_query_gets_the_searches_and_the_sites_that_have_its_words(chrome):
    chrome.visit("https://www.google.com/search?q=noise+cancelling+headphones", "noise cancelling headphones - Google Search",
                 at(24), at(24, 1), at(30), searched="Noise Cancelling Headphones")
    chrome.visit("https://www.amazon.com/dp/B0SONY?ref=sr_1_3", "Sony WH-1000XM6 Headphones", at(6), at(6, 2), at(9), at(12))
    chrome.visit("https://www.amazon.com/dp/B0BOSE", "Bose QuietComfort Headphones", at(6))
    chrome.visit("https://www.crutchfield.com/c/headphones", "Headphones | Crutchfield", at(40))
    chrome.visit("https://www.amazon.com/dp/B0KETTLE", "Fellow Stagg Kettle", at(2))  # another thing: not its words
    lines = (await found(["headphones"]))["headphones"]
    assert lines == [
        "searched 27 Oct · x3 · noise cancelling headphones",  # as typed, when last (the phone's own time zone), how often
        # The site returned to on most days first; its pages by the days they were opened on, each with its last.
        "amazon.com · 5 visits on 3 days: Sony WH-1000XM6 Headphones (14 Nov); Bose QuietComfort Headphones (14 Nov)",
        "crutchfield.com · 1 visit on 1 day: Headphones - Crutchfield (11 Oct)",  # a title's own "|" is not the line's
    ]
    assert (await found(["sony kettle"]))["sony kettle"] == []  # every word, on one page


async def test_penroses_own_browsing_is_left_out(chrome):
    chrome.visit("https://www.amazon.com/dp/B0A", "Sennheiser HD 560S headphones", at(3))
    chrome.visit("https://www.amazon.com/dp/B0B", "Sony WH-1000XM5 headphones", at(1), device="")  # browsed on this phone
    chrome.visit("https://www.amazon.com/dp/B0C", "Bose QC45 headphones", at(1), device=TEST_PHONE)
    chrome.visit("https://www.google.com/search?q=x", "site:amazon.com headphones under 200 - Google Search", at(1),
                 device=TEST_PHONE, searched="site:amazon.com headphones under 200")
    chrome.visit("https://www.amazon.com/dp/B0D", "Beats Studio headphones", at(1), device="this-phone-sync-id-01234")
    # Another penrose phone on the same account, known to this computer: added by hand, or by that phone itself.
    history.own_file().parent.mkdir(parents=True)
    history.own_file().write_text(json.dumps({TEST_PHONE: {"phone": "an old test phone", "added": 1}}))
    assert (await found(["headphones"]))["headphones"] == [
        "amazon.com · 1 visit on 1 day: Sennheiser HD 560S headphones (17 Nov)"]
    # This phone's own sync id is recorded the first time it is searched, for penrose's other phones to leave out.
    assert set(history.own_ids()) == {TEST_PHONE, "this-phone-sync-id-01234"}
    assert history.own_ids()["this-phone-sync-id-01234"]["phone"] == core.slots.get("device-1", "device-1")


async def test_pages_that_say_nothing_about_an_interest_are_left_out(chrome, monkeypatch):
    monkeypatch.setattr(settings, "companion_origin", "https://relay-companion.example.workers.dev")
    chrome.visit("https://accounts.google.com/v3/signin?x=1", "Sign in - Google Accounts camera", at(1))
    chrome.visit("https://www.bestbuy.com/login?next=/camera", "Sign In to Best Buy camera", at(1))
    chrome.visit("https://www.google.com/search?q=camera", "camera - Google Search", at(1))
    chrome.visit("http://192.168.1.1/cgi-bin/camera", "Router camera settings", at(1))
    chrome.visit("http://localhost:8000/camera", "camera dev page", at(1))
    chrome.visit("https://relay-companion.example.workers.dev/chat/1", "penrose camera chat", at(1))
    chrome.visit("https://www.bhphotovideo.com/c/product/1", "", at(1))  # a redirect: no title
    chrome.visit("https://example.com/list?sort=camera", "Wish list", at(1))  # the word is only in the address
    chrome.visit("https://www.bhphotovideo.com/c/product/2?utm=a", "(3) Fujifilm X100VI Camera", at(1))
    chrome.visit("https://www.bhphotovideo.com/c/product/2?utm=b", "Fujifilm X100VI Camera", at(2))  # the same page
    assert (await found(["camera"]))["camera"] == ["bhphotovideo.com · 2 visits on 2 days: Fujifilm X100VI Camera (19 Nov)"]
    assert (await found(["bhphotovideo"]))["bhphotovideo"][0].startswith("bhphotovideo.com · 2 visits")  # a site by its name


async def test_a_look_up_gets_thirty_lines_shared_by_its_queries_and_each_thing_once(chrome):
    for n in range(40):
        chrome.visit(f"https://www.google.com/search?q=keyboard+{n}", "Google Search", at(n), searched=f"keyboard switch {n}")
    for site in range(12):
        for page in range(4):
            chrome.visit(f"https://shop{site}.example.com/p/{page}", f"Keyboard {site}-{page}", *[at(d) for d in range(site + page + 1)])
    one = (await found(["keyboard"]))["keyboard"]
    assert len(one) == history.LINES == 30
    assert sum(line.startswith("searched") for line in one) == 10  # a third for the searches, the newest
    assert one[0] == "searched 20 Nov · keyboard switch 0"
    sites = [line for line in one if not line.startswith("searched")]
    # Every site's first line before any site's second: the most returned-to first, each with its three top pages.
    assert sites[0] == "shop11.example.com · 54 visits on 15 days: Keyboard 11-3 (20 Nov); Keyboard 11-2 (20 Nov); Keyboard 11-1 (20 Nov)"
    assert [line.split(" ")[0] for line in sites[:12]] == [f"shop{n}.example.com" for n in range(11, -1, -1)]
    assert sites[12] == "shop11.example.com, more: Keyboard 11-0 (20 Nov)"
    # Two queries share the thirty, and what the first listed is not listed again under the second.
    both = await found(["keyboard", "keyboard switch"])
    assert len(both["keyboard"]) == 15 and len(both["keyboard switch"]) == 15
    assert not set(both["keyboard"]) & set(both["keyboard switch"])
    assert all(line.startswith("searched") for line in both["keyboard switch"])  # no page is titled "switch"
    assert chrome.reads == 2  # one read of Chrome's database per look-up, whatever its queries


async def test_the_lines_a_query_does_not_use_go_to_one_that_has_more(chrome):
    for site in range(40):
        chrome.visit(f"https://shop{site}.example.com/p", f"Standing desk {site}", at(site))
    chrome.visit("https://www.ikea.com/p/1", "Desk lamp", at(1))
    both = await found(["lamp", "standing desk", "nothing of the kind"])
    assert both["lamp"] == ["ikea.com · 1 visit on 1 day: Desk lamp (19 Nov)"] and both["nothing of the kind"] == []
    assert len(both["standing desk"]) == 29  # its own ten, and the nineteen the other two left


async def test_one_product_a_store_titles_several_ways_is_one_page(chrome):
    chrome.visit("https://www.amazon.com/dp/B0A?th=1", "Amazon.com: Keychron Q1 Max Wireless Custom Mechanical Keyboard, QMK/VIA", at(1))
    chrome.visit("https://www.amazon.com/dp/B0A?th=2", "Amazon.com : Keychron Q1 Max Wireless Custom Mechanical Keyboard, QMK/VIA "
                 "Programmable, Hot-swappable Gateron Jupiter Switch", at(2))
    chrome.visit("https://www.amazon.com/s?k=keychron", "Amazon.com : keychron keyboard", at(2))
    assert (await found(["keychron"]))["keychron"] == [
        # The site's name is on the line already; a longer title is cut at a word and says so.
        "amazon.com · 3 visits on 2 days: Keychron Q1 Max Wireless Custom Mechanical Keyboard, QMK/VIA (19 Nov); keychron keyboard (18 Nov)"]
    assert history.shown("Keychron Q1 Max Wireless Custom Mechanical Keyboard, QMK/VIA Programmable, Hot-swappable") == (
        "Keychron Q1 Max Wireless Custom Mechanical Keyboard, QMK/VIA…")


async def test_a_history_chrome_cannot_answer_says_so_and_no_words_is_no_search(chrome):
    assert await history.history("device-1", ["?"]) == [{"query": "?", "found": ""}] and chrome.reads == 0
    chrome.db.execute("DROP TABLE keyword_search_terms")
    chrome.db.commit()
    [answer] = await history.history("device-1", ["camera"])
    assert answer["query"] == "camera" and "could not be read" in answer["error"]


def test_a_database_outside_the_databases_folder_and_one_with_a_journal(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    chrome = app_data.Copy("device-1", "com.android.chrome", "History", "app_chrome/Default")
    assert chrome.remote == "/data/data/com.android.chrome/app_chrome/Default/History"
    teams = app_data.Copy("device-1", "com.microsoft.teams", "SkypeTeams.db")
    assert teams.remote == "/data/data/com.microsoft.teams/databases/SkypeTeams.db" and chrome.dir != teams.dir
    assert chrome.dir.name != app_data.Copy("device-1", "com.android.chrome", "Web.db").dir.name  # their copies apart
    assert app_data.SAFE_FOLDER.fullmatch("app_chrome/Default") and not app_data.SAFE_FOLDER.fullmatch("../shared_prefs")
    assert not app_data.SAFE_FOLDER.fullmatch("/data/system") and not app_data.SAFE_FOLDER.fullmatch("a/../../b")
    # The stamp of a database with a write-ahead log reads as before; a journal's is added to it.
    assert app_data.stamp_of("10|a", {"-wal": "5|b", "-journal": ""}) == "10|a#5|b"
    assert app_data.stamp_of("10|a", {"-wal": "", "-journal": "8|c"}) == "10|a##8|c"
    # The journal goes into the working copy beside the main file, as the WAL does.
    sqlite3.connect(chrome.path("main.db")).executescript("CREATE TABLE t(x); INSERT INTO t VALUES (1);")
    chrome.path("main.db-journal").write_bytes(b"\0" * 512)  # as Chrome leaves it between writes: an emptied header
    assert app_data.run_queries(chrome, "1", {"t": ["SELECT x FROM t", []]})["t"]["rows"] == [[1]]
    assert chrome.path("work.db").exists()
