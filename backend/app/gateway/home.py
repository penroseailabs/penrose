"""The Relay home app's search, for the agents that plan (the orchestrator's `look_up`).

The home app (`app.relay.home`, android/home) answers on the abstract socket `relayhome`, one
JSON line each way: `{"cmd":"search","q":...}` returns every match on the phone (apps, their screens and
shortcuts, contacts, settings) and Gmail's server search (`"mail": false` leaves it out and answers at once),
as numbered lines. Nothing comes on screen and nothing changes, so a search needs no lease and does not wait
for the phone's lock: it can run while a step works on the phone.

What the phone itself has (apps, screens, contacts, settings) is there at once. Gmail is the slow part, and the
part that varies: the home app searches it one query at a time, about a second each on a phone in use, and
several seconds for the first ones after the phone has slept (measured after a wake from Doze, 2026-11-20: 9 s,
5 s, then 2 s each), or 6 s each when Gmail's servers do not answer at all. So a search never depends on it:
- every query is asked with the time it may wait for Gmail (`wait_ms`: what is left of BUDGET). When that runs
  out the home app answers with what the phone has, and the Gmail row says its emails have not come yet;
- each query answers or fails by itself: one that brought no answer is an error for that query, and the others
  keep what they found. Only when none answered is the search itself reported as failed.
- how it stands with Gmail goes back with every query (`mail`: answered, phone_only, failed, pending), so "Gmail
  has no such email" and "Gmail could not be asked" are never the same answer. Queries Gmail's servers did not
  answer are asked once more with what is left of the budget (the home app's `retry`): the network is often back
  a few seconds later, and a look-up that says so costs the orchestrator a whole turn.
"""

import asyncio
import contextlib
import json

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import core

router = APIRouter()

SOCKET = "relayhome"
# Rows that only offer to search elsewhere (the web, the Play Store): nothing found on the phone.
OFFERS = {"Web", "Play"}
MAX_REPLY_BYTES = 4 * 1024 * 1024
# Seconds a search waits for Gmail in all, however many queries: enough for four on a phone that has just slept.
BUDGET = 25
# After its deadline the home app still has to rank and write its answer.
GRACE = 4
# How it stands with Gmail when its servers gave no answer for a query (the home app's `mail`), and the seconds
# that must be left of the budget to ask them again.
UNANSWERED = ("phone_only", "failed", "pending")
RETRY_LEAST = 5


class SearchRequest(BaseModel):
    queries: list[str] = Field(min_length=1, max_length=6)
    # Gmail's server search takes a second or two per query; without it the home app answers at once.
    email: bool = True


def found_lines(results):
    """One line per thing found (`Mail "Your order has shipped" from Amazon · Oct 22 · Your package is…`),
    without the rows that only offer a search elsewhere and without the actions, which only the screen has."""
    lines = []
    for r in results:
        if r.get("kind") in OFFERS:
            continue
        line = f'{r.get("kind")} "{r.get("title", "")}"'
        if subtitle := r.get("subtitle"):
            line += f" {subtitle}"
        lines.append(line)
    return "\n".join(lines)


async def ask_one(port, query, timeout, email, retry=False):
    """One query on its own connection: the home app serves each connection on its own thread, and answers after
    `timeout` seconds at the latest, with Gmail's emails when they came by then. `retry`: Gmail's servers are
    asked again when they did not answer this query the last time."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port, limit=MAX_REPLY_BYTES)
    try:
        request = {"cmd": "search", "q": query, "mail": email, "wait_ms": int(timeout * 1000), "id": 1}
        writer.write((json.dumps({**request, **({"retry": True} if retry else {})}) + "\n").encode())
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout + GRACE)
        if not line:
            raise ConnectionError("the home app closed the connection")
        reply = json.loads(line)
        if not reply.get("ok"):
            return {"query": query, "error": str(reply.get("error") or "the search failed")[:300]}
        results = reply.get("results") or []
        if not email:  # the Gmail row would only say it was not searched
            results = [r for r in results if r.get("kind") != "Mail"]
        return {"query": query, "found": found_lines(results), **({"mail": reply["mail"]} if email and reply.get("mail") else {})}
    finally:
        with contextlib.suppress(Exception):
            writer.close()


async def ask(port, queries, timeout, email=True, retry=False):
    """Every query at the same time, the answers in the queries' order: what the phone has comes at once, and
    Gmail's emails as the home app gets to each query, within `timeout` seconds for all of them. A query the home
    app did not answer is that query's error; the others keep what they found."""
    answers = await asyncio.gather(*(ask_one(port, q, timeout, email, retry) for q in queries), return_exceptions=True)
    out = []
    for query, answer in zip(queries, answers, strict=True):
        if isinstance(answer, (OSError, TimeoutError, ValueError)):
            out.append({"query": query, "error": f"the penrose home app did not answer ({type(answer).__name__})", "unanswered": True})
        elif isinstance(answer, BaseException):
            raise answer
        else:
            out.append(answer)
    return out


async def searched(port, queries, email, budget=BUDGET):
    """`ask`, within `budget` seconds in all, and Gmail's servers asked a second time for the queries they did not
    answer when enough of it is left. The second answer counts when it is Gmail's own; otherwise the first stands
    (the emails stored on the phone are more than "not yet")."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget
    answers = await ask(port, queries, timeout=budget if email else GRACE, email=email)
    again = [i for i, answer in enumerate(answers) if answer.get("mail") in UNANSWERED]
    left = deadline - loop.time()
    if again and left >= RETRY_LEAST:
        second = await ask(port, [queries[i] for i in again], timeout=left, email=True, retry=True)
        for i, answer in zip(again, second, strict=True):
            if answer.get("mail") == "answered" or (answers[i].get("mail") == "pending" and "found" in answer):
                answers[i] = answer
    return answers


@router.post("/devices/{slot}/home/search")
async def search(slot: str, body: SearchRequest):
    """Each query searched by the home app, all at once, answered in order: {"results": [{"query", "found"} or
    {"query", "error"}]}. Gmail's emails only with `email`. 503 when the home app does not answer (not installed,
    not running, an image built without it)."""
    core.require_slot(slot)
    queries = [q.strip()[:300] for q in body.queries if q.strip()]  # a Gmail search with operators runs long
    if not queries:
        raise HTTPException(422, "Give at least one query.")
    port = (await core.adb(slot, "forward", "tcp:0", f"localabstract:{SOCKET}", timeout=8)).decode().strip()
    if not port.isdigit():
        raise HTTPException(503, f"adb forward returned {port!r}")
    try:
        answers = await searched(int(port), queries, body.email)
    finally:
        with contextlib.suppress(Exception):
            await core.command_result("-s", core.slots[slot], "forward", "--remove", f"tcp:{port}", timeout=5)
    unanswered = [answer.pop("unanswered", False) for answer in answers]
    if all(unanswered):  # not one answer: the home app is not there
        raise HTTPException(503, "The penrose home app did not answer its search (" + answers[0]["error"].rpartition("(")[2] + ".")
    return {"results": core.scrub(answers, core.redactions.get(slot, set()))}
