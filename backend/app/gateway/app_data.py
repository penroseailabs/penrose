"""An app's own SQLite database on the phone, read as root and queried read-only on this computer: the primitive
Highways stand on (app/highways). The app is never touched: its files are copied, never opened in place.

The phone holds a database as a main file plus a side file. For most apps that is a write-ahead log (WAL) holding
what the app wrote since its last checkpoint: the main file is large (Teams': 33 MB) and changes only at a
checkpoint, so it is copied again only when its size or time changes; the WAL is small and is copied whenever it
changed. Some apps (Chrome's History) keep a rollback journal instead: the main file changes with every write, and
the journal undoes a write that was under way when the copy was made. Either way the main file and its side file are
queried together, in a working copy, so every answer is the app's data as of that moment.

Most apps keep their databases in /data/data/<package>/databases; `folder` names another place under the app's
own data (Chrome: app_chrome/Default).

A copy is made on the phone as root into /data/local/tmp, pulled, and deleted at once. A checkpoint that lands during
the copy is seen by the size and time of the main file before and after it: the copy is made again."""

import asyncio
import re
import shutil
import sqlite3
from collections import defaultdict
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..config import settings
from . import core

router = APIRouter(prefix="/devices/{slot}/app-db")

PACKAGE = re.compile(r"[A-Za-z]\w*(\.[A-Za-z]\w*)+")
SAFE_NAME = re.compile(r"[\w.\-]+")
SAFE_FOLDER = re.compile(r"\w[\w.\-]*(/\w[\w.\-]*)*")  # under the app's own data: no "..", nothing absolute
DATABASES = "databases"
# What SQLite keeps beside a database: its write-ahead log, or the journal that undoes a write under way.
SIDES = ("-wal", "-journal")
STAGING = "/data/local/tmp/relay-app-db"
COPY_TRIES = 3
QUERY_ROWS = 10000  # rows one query may return
# A query is a single SELECT (or WITH ... SELECT): the working copy is also opened with query_only.
READ_ONLY_SQL = re.compile(r"^\s*(select|with)\b", re.I)

copies = {}  # (slot, package, file) -> Copy
names = {}  # (slot, package, pattern) -> the database's file name, found once
locks = defaultdict(asyncio.Lock)


class Query(BaseModel):
    package: str = Field(description="The app's package ID, e.g. com.microsoft.teams.")
    file: str = Field(description="A regular expression for the database's file name in the app's databases folder.")
    folder: str = Field(
        default=DATABASES, description="Where the database is under the app's own data, when not in `databases`: "
        "'app_chrome/Default' for Chrome's.",
    )
    queries: dict[str, list] = Field(
        default_factory=dict,
        description='{name: [sql, [params]]}: read-only SELECTs, each answered as {"columns", "rows"} or {"error"}.',
    )


class Copy:
    """The local copy of one database: the main file and its side file as last pulled, and the working copy queried."""

    def __init__(self, slot, package, name, folder=DATABASES):
        self.slot, self.package, self.name, self.folder = slot, package, name, folder
        elsewhere = "" if folder == DATABASES else "-" + folder.replace("/", "_") + "-" + name
        self.dir = settings.user_dir / "gateway" / "app-db" / f"{slot}-{package}{elsewhere}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.main_stamp = self.side_stamps = self.work_stamp = None

    @property
    def remote(self):
        return f"/data/data/{self.package}/{self.folder}/{self.name}"

    def path(self, part):
        return self.dir / part


async def sh(slot, script, timeout=25):
    """A root shell script's output; a non-zero exit is an answer here (a missing WAL), not a failure."""
    quoted = script.replace("'", "'\\''")
    stdout, _, _ = await core.command_result("-s", core.slots[slot], "shell", f"su -c '{quoted}'", timeout=timeout)
    return stdout.decode(errors="replace")


async def pull(slot, remote, local: Path):
    _, stderr, code = await core.command_result("-s", core.slots[slot], "pull", remote, str(local), timeout=60)
    if code:
        raise HTTPException(503, f"Could not copy the app's data from the phone: {stderr.decode(errors='replace')[:200]}")


async def database_name(slot, package, pattern, folder=DATABASES):
    """The file in the app's databases folder whose name matches `pattern` (the largest if several)."""
    listing = await sh(slot, f"ls -S /data/data/{package}/{folder} 2>/dev/null")
    names = [n for n in listing.split() if pattern.fullmatch(n)]
    return names[0] if names else None


async def stamps(slot, remote):
    """(main, sides, phone's UTC offset): the main file's size and modification time, and each side file's by its
    suffix ("" for one that is not there)."""
    out = await sh(slot, f"stat -c '%n|%s|%y' {remote} {' '.join(remote + side for side in SIDES)} 2>/dev/null; date +%z")
    found = {}
    lines = out.strip().splitlines()
    for line in lines[:-1]:
        name, _, rest = line.partition("|")
        found[name] = rest
    return found.get(remote, ""), {side: found.get(remote + side, "") for side in SIDES}, (lines[-1].strip() if lines else "+0000")


def stamp_of(main, sides):
    """One stamp for the database as copied: it changes whenever the app wrote."""
    return f"{main}#{sides['-wal']}" + (f"#{sides['-journal']}" if sides["-journal"] else "")


async def fetch(copy, part, remote):
    """Copy one of the app's files to this computer through the root staging folder, deleted again at once."""
    staged = f"{STAGING}/{copy.slot}-{copy.package}-{part}"
    await sh(copy.slot, f"mkdir -p {STAGING} && cp {remote} {staged} && chmod 644 {staged}")
    try:
        await pull(copy.slot, staged, copy.path(part))
    finally:
        await sh(copy.slot, f"rm -f {staged}")


async def refresh(copy):
    """Bring the local main file and its side files up to the phone's; returns (stamp, UTC offset)."""
    main, sides, offset = await stamps(copy.slot, copy.remote)
    if not main:
        raise HTTPException(404, "The app's database is not on the phone (the app may have been reset).")
    if main == copy.main_stamp and sides == copy.side_stamps:  # the app wrote nothing since the last read
        return stamp_of(main, sides), offset
    for _ in range(COPY_TRIES):
        if main != copy.main_stamp:
            await fetch(copy, "main.db", copy.remote)
            copy.main_stamp, copy.side_stamps = main, None  # a new main file needs its own side files
        for side in SIDES:
            if sides[side] and sides[side] != (copy.side_stamps or {}).get(side):
                await fetch(copy, f"main.db{side}", copy.remote + side)
            elif not sides[side]:
                copy.path(f"main.db{side}").unlink(missing_ok=True)
        now_main, now_sides, offset = await stamps(copy.slot, copy.remote)
        if now_main == main:  # the main file was not rewritten during the copies: they belong together
            copy.side_stamps = sides
            return stamp_of(main, sides), offset
        main, sides = now_main, now_sides
    raise HTTPException(503, "The app kept rewriting its database while it was copied; try again.")


def run_queries(copy, stamp, queries):
    """The queries on the working copy (the main file and its side file together), rebuilt when either changed.
    SQLite folds the WAL into the working copy when it closes, or undoes with the journal the write that was under
    way; the pulled files stay as they came."""
    if copy.work_stamp != stamp:
        for suffix in ("", *SIDES, "-shm"):
            copy.path(f"work.db{suffix}").unlink(missing_ok=True)
        shutil.copyfile(copy.path("main.db"), copy.path("work.db"))
        for side in SIDES:
            if copy.path(f"main.db{side}").exists():
                shutil.copyfile(copy.path(f"main.db{side}"), copy.path(f"work.db{side}"))
        copy.work_stamp = stamp
    results = {}
    db = sqlite3.connect(copy.path("work.db"))
    try:
        db.execute("PRAGMA query_only = ON")
        for name, spec in queries.items():
            sql, params = (spec + [[]])[:2] if isinstance(spec, list) else (spec, [])
            # One SELECT: sqlite3 runs a single statement per execute (a second is an error), and query_only refuses
            # any write that would get through.
            if not isinstance(sql, str) or not READ_ONLY_SQL.match(sql):
                results[name] = {"error": "only one SELECT per query"}
                continue
            try:
                cursor = db.execute(sql, params or [])
                rows = cursor.fetchmany(QUERY_ROWS)
                results[name] = {"columns": [d[0] for d in cursor.description or []], "rows": [list(r) for r in rows]}
            except sqlite3.Error as exc:
                results[name] = {"error": str(exc)[:300]}
    finally:
        db.close()
    return results


@router.post("")
async def query(slot: str, body: Query):
    """Read-only queries on an app's own database on the phone (a copy of it, as of now). No lease: it reads, and
    the phone is not touched. {"ok", "file", "stamp", "utc_offset", "results"}; "stamp" changes whenever the app
    wrote."""
    core.require_slot(slot)
    if not PACKAGE.fullmatch(body.package):
        raise HTTPException(422, "package must be a package ID")
    try:
        pattern = re.compile(body.file)
    except re.error:
        raise HTTPException(422, "file must be a regular expression") from None
    if not SAFE_FOLDER.fullmatch(body.folder):
        raise HTTPException(422, "folder must be a folder under the app's own data")
    found = (slot, body.package, body.folder, body.file)
    name = names.get(found) or await database_name(slot, body.package, pattern, body.folder)
    if not name or not SAFE_NAME.fullmatch(name):
        return {"ok": False, "error": f"no database matching {body.file!r} in {body.package}"}
    key = (slot, body.package, body.folder, name)
    async with locks[key]:
        copy = copies.get(key) or copies.setdefault(key, Copy(slot, body.package, name, body.folder))
        try:
            stamp, offset = await refresh(copy)
        except HTTPException:
            names.pop(found, None)  # the app may have been reset: look for its database again next time
            raise
        names[found] = name
        results = await asyncio.to_thread(run_queries, copy, stamp, body.queries)
    return {"ok": True, "file": name, "stamp": stamp, "utc_offset": offset, "results": results}
