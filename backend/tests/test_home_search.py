"""The gateway's side of look_up: the Relay home app's search over its socket, trimmed for the orchestrator."""

import asyncio
import json

from app.gateway import home


def test_found_lines_keep_what_the_phone_has_and_drop_offers_and_actions():
    results = [
        {"n": 1, "kind": "Mail", "title": "Your order of LG LRMVS3006S", "subtitle": "from Amazon · Mar 12", "actions": ["open"]},
        {"n": 2, "kind": "Contact", "title": "Jordan Lee", "subtitle": "+1 512-555-0142", "actions": ["call", "sms"]},
        {"n": 3, "kind": "Play", "title": "fridge", "subtitle": "not installed", "actions": ["install"]},
        {"n": 4, "kind": "Web", "title": "fridge", "subtitle": "", "actions": ["google"]},
        {"n": 5, "kind": "App", "title": "Amazon", "subtitle": "", "actions": ["open"]},
    ]
    assert home.found_lines(results) == (
        'Mail "Your order of LG LRMVS3006S" from Amazon · Mar 12\nContact "Jordan Lee" +1 512-555-0142\nApp "Amazon"'
    )


async def test_ask_searches_every_query_at_once_and_answers_in_order():
    seen, connections = [], []

    async def serve(reader, writer):
        connections.append(writer)
        while line := await reader.readline():
            request = json.loads(line)
            seen.append(request)
            if request["q"] == "slow":
                await asyncio.sleep(0.3)  # Gmail's servers: the others do not wait for it
            if request["q"] == "broken":
                reply = {"ok": False, "error": "IllegalStateException: no index"}
            else:
                reply = {"ok": True, "results": [{"kind": "App", "title": request["q"].title(), "subtitle": ""},
                                                 {"kind": "Mail", "title": request["q"], "subtitle": "search Gmail · 3 emails"}]}
            writer.write((json.dumps({**reply, "id": request["id"]}) + "\n").encode())
            await writer.drain()
        writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        started = asyncio.get_running_loop().time()
        answers = await home.ask(port, ["slow", "maps", "broken"], timeout=5)
        took = asyncio.get_running_loop().time() - started
        without_mail = await home.ask(port, ["maps"], timeout=5, email=False)
    assert len(connections) == 4 and {r["cmd"] for r in seen} == {"search"}
    assert took < 0.6  # side by side, not one after another
    assert answers == [
        {"query": "slow", "found": 'App "Slow"\nMail "slow" search Gmail · 3 emails'},
        {"query": "maps", "found": 'App "Maps"\nMail "maps" search Gmail · 3 emails'},
        {"query": "broken", "error": "IllegalStateException: no index"},
    ]
    assert [r["mail"] for r in seen] == [True, True, True, False]
    assert without_mail == [{"query": "maps", "found": 'App "Maps"'}]  # no Gmail row when Gmail was not asked


async def test_a_query_the_home_app_does_not_answer_fails_alone_and_each_is_told_how_long_gmail_may_take(monkeypatch):
    monkeypatch.setattr(home, "GRACE", 0.1)
    seen = []

    async def serve(reader, writer):
        while line := await reader.readline():
            request = json.loads(line)
            seen.append(request)
            if request["q"] == "stuck":
                await asyncio.sleep(5)  # never within the time it was given
            # What the phone has comes back at the deadline, with Gmail's row saying its emails are still to come.
            mail = "NOT SEARCHED: Gmail has not answered yet" if request["q"] == "late" else "2 emails"
            reply = {"ok": True, "mail": "pending" if request["q"] == "late" else "answered", "results": [
                {"kind": "App", "title": request["q"].title(), "subtitle": ""},
                {"kind": "Mail", "title": request["q"], "subtitle": f"search Gmail · {mail}"}]}
            writer.write((json.dumps({**reply, "id": request["id"]}) + "\n").encode())
            await writer.drain()
        writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        started = asyncio.get_running_loop().time()
        answers = await home.ask(port, ["maps", "stuck", "late"], timeout=0.3)
        took = asyncio.get_running_loop().time() - started
    assert took < 1  # the time given and a little for the answer to be written, never the stuck query's
    assert {r["wait_ms"] for r in seen} == {300}
    assert answers == [
        {"query": "maps", "found": 'App "Maps"\nMail "maps" search Gmail · 2 emails', "mail": "answered"},
        {"query": "stuck", "error": "the penrose home app did not answer (TimeoutError)", "unanswered": True},
        {"query": "late", "found": 'App "Late"\nMail "late" search Gmail · NOT SEARCHED: Gmail has not answered yet', "mail": "pending"},
    ]


async def test_gmail_s_servers_are_asked_once_more_for_the_queries_they_did_not_answer(monkeypatch):
    monkeypatch.setattr(home, "GRACE", 0.1)
    monkeypatch.setattr(home, "RETRY_LEAST", 0.2)
    seen = []

    async def serve(reader, writer):
        while line := await reader.readline():
            request = json.loads(line)
            seen.append(request)
            # Gmail's servers are out of reach the first time for two of them, and back the second for one.
            if request["q"] == "back" and not request.get("retry") or request["q"] == "still down":
                reply = {"mail": "phone_only", "results": [{"kind": "Mail", "title": request["q"], "subtitle": (
                    "search Gmail · NOT SEARCHED: Gmail's servers did not answer; only the 1 emails stored on the phone are listed")}]}
            else:
                reply = {"mail": "answered", "results": [{"kind": "Mail", "title": request["q"], "subtitle": "search Gmail · 7 emails"}]}
            writer.write((json.dumps({"ok": True, **reply, "id": request["id"]}) + "\n").encode())
            await writer.drain()
        writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        answers = await home.searched(port, ["fine", "back", "still down"], email=True, budget=2)
        without_mail = await home.searched(port, ["fine"], email=False)
    # How it stands with Gmail goes back with every query, so "none" and "could not look" are told apart.
    assert [(a["query"], a["mail"]) for a in answers] == [("fine", "answered"), ("back", "answered"), ("still down", "phone_only")]
    assert "7 emails" in answers[1]["found"] and "NOT SEARCHED" in answers[2]["found"]
    asked = [(r["q"], r.get("retry", False)) for r in seen[:5]]
    assert sorted(asked[:3]) == [("back", False), ("fine", False), ("still down", False)]
    assert sorted(asked[3:]) == [("back", True), ("still down", True)]  # only the two Gmail did not answer, and said as a retry
    assert "mail" not in without_mail[0] and len(seen) == 6  # Gmail not asked: nothing to say about it, nothing to ask again
