"""The user's own browsing, for the orchestrator's `look_up`: what they searched for and the pages they went to on
their other devices, as Chrome keeps it on this phone.

Chrome signed in with history sync on keeps about 90 days of the account's visits in its History database on every
device, each visit marked with the device it was made on. That database is read as root through app_data.py (a
copy, taken again when Chrome wrote to it; nothing is synced for the look-up, only what is already there is read).

What a look-up gets, for each query, is only what has every word of it: never the history itself.
- the searches the user typed (Chrome's keyword_search_terms), one line per search with when and how often;
- the pages they went to, one line per site with its visits, days and the pages they returned to most, each
  with the day it was last opened.

Left out:
- penrose's own browsing. A visit made on this phone carries no device, and those are never read. A visit made on
  another penrose phone signed in to the same account (a test phone, an earlier phone) carries that phone's sync
  id: every phone records its own id in own-chrome-devices.json when it is first searched, and the ids in that
  file are left out on all of them. Without this penrose would read its own searching as the user's interests.
- penrose's own app and pages that say nothing about an interest: search result pages (their words are the
  searches above), sign-in and verification pages, addresses on this computer or the home network, pages with no
  title.

Measured on a test phone: thousands of visits, most of them synced from the account's other devices; after the
above, a few thousand visits of some hundreds of pages and searches. A two-word match takes 4 ms; the copy takes a second when
Chrome wrote since the last one (6.6 MB).
"""

import json
import logging
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..config import settings
from . import app_data, core

router = APIRouter()
log = logging.getLogger("gateway.history")

CHROME = "com.android.chrome"
FOLDER = "app_chrome/Default"
LINES = 30  # of one look-up, shared between its queries
TITLES = 3  # pages named on one line of a site
TITLE_CHARS = 70
CHROME_EPOCH = 11644473600  # Chrome counts microseconds from 1601

# Visits made on other devices (this phone's own have no originator) whose page has every word, newest first; a
# search result page is not a page here. The words are matched again on the title and the site alone (below): an
# address's query string matches anything.
PAGES = """SELECT u.url, u.title, v.visit_time, v.originator_cache_guid FROM visits v JOIN urls u ON u.id = v.url
WHERE v.originator_cache_guid != '' AND NOT EXISTS (SELECT 1 FROM keyword_search_terms k WHERE k.url_id = u.id)
AND {every} ORDER BY v.visit_time DESC"""
SEARCHES = """SELECT k.normalized_term, v.visit_time, v.originator_cache_guid FROM keyword_search_terms k
JOIN visits v ON v.url = k.url_id WHERE v.originator_cache_guid != '' AND {every} ORDER BY v.visit_time DESC"""

SIGN_IN = re.compile(r"(^|\.)(accounts|login|signin|auth|sso|id|oauth|secure)\.|/(login|signin|sign-in|oauth|sso|auth|checkpoint|challenge)\b", re.I)
LOCAL = re.compile(r"localhost|[\d.]+|\[[\da-f:]+\]|.*\.(local|lan|home|internal)", re.I)
OWN_ID = re.compile(r'"cache_guid":"([^"]{8,64})"')

recorded = set()  # slots whose own sync id is in the file


class SearchRequest(BaseModel):
    queries: list[str] = Field(min_length=1, max_length=6)


def words(query):
    """The words of a search: all of them must be there (the rule the notification log is searched by)."""
    return list(dict.fromkeys(w.lower() for w in re.findall(r"[^\W_]+", query or "", re.UNICODE) if len(w) > 1))[:8]


def own_file():
    return settings.user_dir / "gateway" / "own-chrome-devices.json"


def own_ids():
    """The sync ids of penrose's own phones: {id: {"phone", "added"}}. An id may be added by hand for a phone this
    computer never ran."""
    try:
        return json.loads(own_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


async def own_devices(slot):
    """The ids to leave out, with this phone's own recorded the first time it is searched (Chrome keeps it in its
    Preferences, one per account that ever synced here)."""
    known = own_ids()
    if slot not in recorded:
        found = OWN_ID.findall(await app_data.sh(slot, f"grep -o '\"cache_guid\":\"[^\"]*\"' /data/data/{CHROME}/{FOLDER}/Preferences 2>/dev/null"))
        fresh = [guid for guid in dict.fromkeys(found) if guid not in known]
        if fresh:
            known.update({guid: {"phone": core.slots.get(slot, slot), "added": int(time.time())} for guid in fresh})
            own_file().parent.mkdir(parents=True, exist_ok=True)
            own_file().write_text(json.dumps(known, indent=1), encoding="utf-8")
        if found:
            recorded.add(slot)
    return set(known)


def page_of(url, title, found):
    """(site, title) of a visit that says something about what the user looked at and has every word, else None."""
    parts = urlsplit(url or "")
    host = (parts.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    name = re.sub(r"^\(\d+\+?\)\s*", "", " ".join((title or "").split()))  # "(3) Inbox": the counter is not the page
    name = re.sub(rf"^{re.escape(host)}\s*:\s*", "", name, flags=re.I)  # "Amazon.com: ...": the line names the site
    if parts.scheme not in ("http", "https") or not host or LOCAL.fullmatch(host):
        return None
    own = {urlsplit(origin).hostname for origin in (settings.public_origin, settings.companion_origin) if origin}
    if host in own or (host.endswith(".workers.dev") and host.startswith("relay-companion")):
        return None  # penrose's own window and phone app
    if host.startswith("google.") and parts.path.startswith(("/search", "/url")):
        return None
    if SIGN_IN.search(host + parts.path) or not name or name.lower() in (host, (url or "").lower()):
        return None
    text = f"{name} {host}".lower()
    return (host, name) if all(word in text for word in found) else None


def local_day(visit_time, zone):
    return datetime.fromtimestamp(visit_time / 1_000_000 - CHROME_EPOCH, zone)


def zone_of(offset):
    """The phone's time zone from `date +%z` ("+0530")."""
    match = re.fullmatch(r"([+-])(\d\d)(\d\d)", (offset or "").strip())
    minutes = (int(match[2]) * 60 + int(match[3])) * (-1 if match[1] == "-" else 1) if match else 0
    return timezone(timedelta(minutes=minutes))


def plural(n, word):
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def shown(title):
    """A page's title as a line shows it: without the marks the line itself uses, and cut at a word, with an
    ellipsis, when it is longer than a line has room for."""
    title = title.replace("|", "-").replace(";", ",")
    if len(title) <= TITLE_CHARS:
        return title
    return title[:TITLE_CHARS].rsplit(" ", 1)[0].rstrip(" ,-:(") + "…"


def same_page(name):
    """What makes two titles one page: the words as they are shown (a store writes one product several ways, and
    what differs past the part a line shows makes no difference to its reader)."""
    return re.sub(r"[\W_]+", " ", name.lower()).strip()[:TITLE_CHARS - 10].strip()


def candidates(found, pages, searches, own, zone, seen):
    """What a query could list, best first, as ([(line, keys)] of searches, [(line, keys)] of sites): the searches
    the user typed that have its words, newest first, and the sites, the ones returned to on most days first. A
    site's line names its three most visited pages; a site with more gets further lines, every site's first line
    before any site's second. `keys` are what a line lists; `seen` holds what an earlier query of the same look-up
    listed, which is not listed again."""
    typed = {}
    for term, at, guid in searches:
        if guid in own or ("search", term) in seen:
            continue
        entry = typed.setdefault(term, {"last": at, "times": 0})
        entry["times"] += 1
    sites = defaultdict(dict)
    for url, title, at, guid in pages:
        page = None if guid in own else page_of(url, title, found)
        if not page or (page[0], same_page(page[1])) in seen:
            continue
        host, name = page
        entry = sites[host].setdefault(same_page(name), {"title": name, "visits": 0, "days": set(), "last": at})
        entry["visits"] += 1
        entry["days"].add(local_day(at, zone).date())

    def rank(pages):
        return (-len(set().union(*(p["days"] for p in pages))), -sum(p["visits"] for p in pages), -max(p["last"] for p in pages))

    chunks = []  # (round, the site's rank, line, keys)
    for order, (host, by_name) in enumerate(sorted(sites.items(), key=lambda site: rank(site[1].values()))):
        listed = sorted(by_name.values(), key=lambda p: (-len(p["days"]), -p["visits"], -p["last"]))
        for start in range(0, len(listed), TITLES):
            # Each page with the day it was last opened: a date on the site's line was read as every page's.
            titles = "; ".join(f"{shown(p['title'])} ({local_day(p['last'], zone):%d %b})" for p in listed[start:start + TITLES])
            if start:
                line = f"{host}, more: {titles}"
            else:
                days = set().union(*(p["days"] for p in listed))
                line = f"{host} · {plural(sum(p['visits'] for p in listed), 'visit')} on {plural(len(days), 'day')}: {titles}"
            chunks.append((start // TITLES, order, line, [(host, same_page(p["title"])) for p in listed[start:start + TITLES]]))
    chunks.sort(key=lambda chunk: chunk[:2])
    return ([(f"searched {local_day(entry['last'], zone):%d %b}" + (f" · x{entry['times']}" if entry["times"] > 1 else "")
              + f" · {term}", [("search", term)]) for term, entry in typed.items()],
            [(line, keys) for _, _, line, keys in chunks])


def found_lines(found, pages, searches, own, zone, share, seen):
    """A query's lines, at most `share` (a third of them for the searches, the rest for the sites; what one does
    not use, the other may), and what it could list beyond them."""
    typed, sites = candidates(found, pages, searches, own, zone, seen)
    for_searches = min(len(typed), max(share // 3, share - len(sites)))
    for_sites = min(len(sites), share - for_searches)
    taken, more = typed[:for_searches] + sites[:for_sites], sites[for_sites:] + typed[for_searches:]
    for _, keys in taken:
        seen.update(keys)
    return [line for line, _ in taken], more


async def history(slot, queries):
    """[{"query", "found"} or {"query", "error"}] for each query: the lines of the user's own browsing that have
    its words ("" when none). One read of Chrome's database for all of them."""
    own = await own_devices(slot)
    specs, asked = {}, {}
    for index, query in enumerate(queries):
        if found := words(query):
            asked[index] = found
            likes = [f"%{word}%" for word in found]
            specs[f"pages{index}"] = [PAGES.format(every=" AND ".join("(u.title LIKE ? OR u.url LIKE ?)" for _ in found)),
                                      [like for like in likes for _ in (0, 1)]]
            specs[f"searches{index}"] = [SEARCHES.format(every=" AND ".join("k.normalized_term LIKE ?" for _ in found)), likes]
    if not specs:
        return [{"query": query, "found": ""} for query in queries]
    answer = await app_data.query(slot, app_data.Query(package=CHROME, file="History", folder=FOLDER, queries=specs))
    if not answer.get("ok"):
        raise HTTPException(404, "Chrome keeps no history on this phone.")
    zone, share, seen = zone_of(answer.get("utc_offset")), max(LINES // len(asked), 1), set()
    lines, more, errors = {}, {}, {}
    for index, found in asked.items():
        pages, searches = answer["results"][f"pages{index}"], answer["results"][f"searches{index}"]
        if problem := pages.get("error") or searches.get("error"):
            errors[index] = f"Chrome's history could not be read ({problem[:120]})"
        else:
            lines[index], more[index] = found_lines(found, pages["rows"], searches["rows"], own, zone, share, seen)
    # The lines a query did not use go to the ones that have more to list, in the order asked.
    spare = LINES - sum(len(found) for found in lines.values())
    for index, rest in more.items():
        for line, keys in rest:
            if spare <= 0:
                break
            if not seen.issuperset(keys):  # an earlier query's extra lines may have listed it meanwhile
                seen.update(keys)
                lines[index].append(line)
                spare -= 1
    return [{"query": query, "error": errors[index]} if index in errors else {"query": query, "found": "\n".join(lines.get(index, []))}
            for index, query in enumerate(queries)]


@router.post("/devices/{slot}/history/search")
async def search(slot: str, body: SearchRequest):
    """What the user searched for and the pages they went to on their other devices, for each query's words:
    {"results": [{"query", "found"} or {"query", "error"}]}. 404 when Chrome keeps no history here, 503 when it
    could not be copied. Read-only: no lease, nothing on screen, nothing synced."""
    core.require_slot(slot)
    queries = [q.strip()[:200] for q in body.queries if q.strip()]
    if not queries:
        raise HTTPException(422, "Give at least one query.")
    return {"results": core.scrub(await history(slot, queries), core.redactions.get(slot, set()))}
