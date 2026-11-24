"""Briefings and the "For you" list: a notification no chat waits for, from an app the user chose, joins its
conversation's briefing; a briefing is sorted once, as a whole; opening one starts a chat that reads every
earlier conversation on the matter; and a chat whose task is over no longer collects notifications."""

import json
import time

import pytest
from app import delivery, notifications
from app.briefings import actions, dossier, groups, policy, triage
from app.briefings import feed as for_you
from app.runtime.orchestrator import Orchestrator
from fakes import Script, ask_user, make_state, orch, runner_for
from test_api import api_client

TEAMS = "com.microsoft.teams"
WHATSAPP = "com.whatsapp"


def event(app, label, title, text, **extra):
    return {"type": "notification", "app": app, "label": label, "title": title, "text": text,
            "key": f"0|{app}|1|{title}|{text}", **extra}


def release_sync(sender, text):
    return event(TEAMS, "Teams", f"Release sync: {sender}", text, conversation="Release sync", sender=sender)


def sorted_as(verdict, headline="New build posted, release sync at 4", why="", search=(), **said):
    return {"actions": [{"tool": "sort", "verdict": verdict, "headline": headline, "why": why, "search": list(search),
                         **said}]}


def briefed(store):
    return store.rows("SELECT * FROM briefings ORDER BY id")


def test_only_an_app_the_user_chose_is_briefed_on_and_a_conversation_is_one_briefing(store):
    assert for_you.feed(store)["apps_on"] == 0
    kept = notifications.keep(store, "device-1", release_sync("Theo", "Build 3.2.3 is up"))
    assert "briefing_id" not in kept and briefed(store) == []  # nothing chosen: it stays in the log, as before

    actions.choose_app(store, TEAMS, True)
    first = notifications.keep(store, "device-1", release_sync("Theo", "Build 3.2.4 is up"))
    second = notifications.keep(store, "device-1", release_sync("Maria", "Release sync at 4, bring your open items"))
    other = notifications.keep(store, "device-1", event(TEAMS, "Teams", "Billing v3: Lena", "fixed", conversation="Billing v3", sender="Lena"))
    elsewhere = notifications.keep(store, "device-1", event(WHATSAPP, "WhatsApp", "Dad", "Saturday?"))
    assert first["briefing_id"] == second["briefing_id"] != other["briefing_id"] and "briefing_id" not in elsewhere
    rows = briefed(store)
    # The one from before the app was chosen is taken in with it: the list starts from today.
    assert [(r["name"], r["count"], r["verdict"]) for r in rows] == [("Release sync", 3, None), ("Billing v3", 1, None)]
    # Not shown until it has been sorted.
    assert for_you.feed(store)["ready"] == [] and for_you.feed(store)["came_in"] == []
    assert [a["package"] for a in for_you.apps(store) if a["on"]] == [TEAMS]


def test_a_briefing_is_due_once_it_goes_quiet_or_has_waited_long_enough(store):
    actions.choose_app(store, TEAMS, True)
    now = time.time()
    notifications.keep(store, "device-1", release_sync("Theo", "one"))
    due, wait = triage.due(store, now)
    assert due == [] and 0 < wait <= triage.QUIET_SECONDS + 1
    due, wait = triage.due(store, now + triage.QUIET_SECONDS + 2)
    assert [r["name"] for r in due] == ["Release sync"] and wait is None


async def test_a_sort_reads_the_burst_beside_what_is_known_and_decides_where_it_shows(store):
    actions.choose_app(store, TEAMS, True)
    store.save_memory("v7-launch", "user", "The user owns QA sign-off for Billing v3", "Owns QA sign-off for Billing v3.")
    notifications.keep(store, "device-1", release_sync("Theo", "Build 3.2.3 is up"))
    notifications.keep(store, "device-1", release_sync("Maria", "Release sync at 4, bring your open items"))
    notifications.keep(store, "device-1", event(TEAMS, "Teams", "Billing v3: Lena", "thanks!", conversation="Billing v3", sender="Lena"))
    read = []

    def agent(context, n):
        read.append(context)
        if context["from"]["conversation"] == "Release sync":
            return sorted_as("brief", why="You own QA sign-off for Billing v3.", search=["Billing v3", "release sync"])
        return sorted_as("list", headline="Lena said thanks")

    runner = runner_for(store, Script(Triage=agent))
    for row in briefed(store):
        await triage.sort(runner, row["id"])
    assert [n["from"] for n in read[0]["notifications"]] == ["Theo", "Maria"]
    assert "QA sign-off" in json.dumps(read[0]["memory_index"])
    feed = for_you.feed(store)
    [ready], [listed] = feed["ready"], feed["came_in"]
    assert (ready["headline"], ready["name"], ready["count"], ready["seen"]) == ("New build posted, release sync at 4", "Release sync", 2, False)
    assert ready["why"].startswith("You own") and listed["headline"] == "Lena said thanks" and feed["unseen"] == 1
    # More arrives: the same briefing, due again, and the sorter is told what it said before.
    notifications.keep(store, "device-1", release_sync("Theo", "Moved to 4:30"))
    [again] = [r for r in briefed(store) if r["count"] > r["sorted_count"]]
    # A conversation that keeps talking is not read again for a while, then it is.
    soon, later = time.time() + triage.MAX_WAIT_SECONDS + 5, time.time() + triage.RESORT_SECONDS + 5
    assert triage.due(store, soon)[0] == [] and [r["id"] for r in triage.due(store, later)[0]] == [again["id"]]
    await triage.sort(runner, again["id"])
    assert read[-1]["sorted_before_as"]["verdict"] == "brief" and len(briefed(store)) == 2
    # Noise is kept in the log and shown nowhere.
    notifications.keep(store, "device-1", event(TEAMS, "Teams", "Teams", "Try the new calendar"))
    [noise] = [r for r in briefed(store) if not r["key"]]
    await triage.sort(runner_for(store, Script(Triage=[sorted_as("ignore", headline="An advert")])), noise["id"])
    feed = for_you.feed(store)
    assert len(feed["ready"]) == 1 and len(feed["came_in"]) == 1


async def test_a_briefing_that_cannot_be_sorted_is_listed_as_it_came(store):
    actions.choose_app(store, WHATSAPP, True)
    notifications.keep(store, "device-1", event(WHATSAPP, "WhatsApp", "Dad", "Are you coming on Saturday?", conversation="Dad", sender="Dad"))

    def broken(context, n):
        raise RuntimeError("no key")

    [row] = briefed(store)
    runner = runner_for(store, Script(Triage=broken))
    with pytest.raises(triage.NotAsked):
        await triage.sort(runner, row["id"])
    [listed] = for_you.feed(store)["came_in"]
    assert listed["headline"] == "Dad: Are you coming on Saturday?"
    # It is still due: the agent is asked again later, and what it then says replaces the stand-in.
    [still] = briefed(store)
    assert still["sorted_count"] == 0 and still["sorted"] is None
    assert await triage.sort(runner_for(store, Script(Triage=[sorted_as("brief", headline="Dad asks about Saturday")])), row["id"]) == "brief"
    assert [b["headline"] for b in for_you.feed(store)["ready"]] == ["Dad asks about Saturday"]
    # On its last try a sort that cannot ask leaves the briefing as it is listed, settled until more arrives.
    notifications.keep(store, "device-1", event(WHATSAPP, "WhatsApp", "Dad", "Hello?", conversation="Dad", sender="Dad"))
    assert await triage.sort(runner, row["id"], last=True) == "brief"
    [settled] = briefed(store)
    assert settled["sorted_count"] == settled["count"] == 2 and settled["headline"] == "Dad asks about Saturday"


async def test_opening_a_briefing_starts_a_chat_that_reads_every_earlier_conversation_on_the_matter(store):
    actions.choose_app(store, TEAMS, True)
    # Two earlier chats hold what matters here: one whose step read the group, one that only speaks of the launch.
    make_state(store)
    store.execute("UPDATE runs SET status='completed' WHERE id='run'")
    store.message("chat", "user", "Check my open items for the launch")
    store.message("chat", "assistant", "You own the QA sign-off and the PDP icons.", source="Orchestrator")
    notifications.worked_in(store, "chat", TEAMS, ["Open the Release sync group and list what is asked of the user"])
    store.execute("INSERT INTO chats VALUES (?,?,?)", ("qa", "V7 build QA", time.time()))
    store.message("qa", "user", "Test the Billing v3 build for the checkout events")
    store.message("qa", "assistant", "Build 3.2.2: two of five checkout events missing.", source="Orchestrator")

    notifications.keep(store, "device-1", release_sync("Theo", "Build 3.2.3 is up"))
    notifications.keep(store, "device-1", release_sync("Maria", "Release sync at 4, bring your open items"))
    [row] = briefed(store)
    await triage.sort(runner_for(store, Script(Triage=[sorted_as("brief", search=["Billing v3 build"])])), row["id"])

    chat_id, state = actions.start(store, row["id"], "openai")
    assert state["status"] == "queued" and state["trigger"] == "briefing" and state["preparing"] is True
    assert state["route"] == {"orchestrate": True} and state["provider"] == "openai"
    said = store.rows("SELECT role,content FROM messages WHERE chat_id=? ORDER BY created", (chat_id,))
    assert [m["role"] for m in said] == ["notification", "notification", "user"] and said[-1]["content"] == actions.ASK
    assert "Build 3.2.3 is up" in said[0]["content"]
    assert store.rows("SELECT title FROM chats WHERE id=?", (chat_id,))[0]["title"] == "New build posted, release sync at 4"
    found = state["briefing"]
    assert found["from"] == {"app": "Teams", "package": TEAMS, "conversation": "Release sync"}
    assert [n["from"] for n in found["arrived"]] == ["Theo", "Maria"]
    titles = {c["title"]: c for c in found["related_conversations"]}
    assert set(titles) == {"test", "V7 build QA"}  # both, not the latest one
    assert "QA sign-off" in titles["test"]["ended_with"] and titles["V7 build QA"]["asked"] == [
        "Test the Billing v3 build for the checkout events"]
    # Opening it again answers with the same chat and starts nothing.
    assert actions.start(store, row["id"]) == (chat_id, None)
    assert len(store.rows("SELECT id FROM runs WHERE chat_id=?", (chat_id,))) == 1

    # The orchestrator reads it as a briefing, offers what it can do, and the offers reach the list.
    seen = {}

    def orchestrator(context, n):
        seen.update(context)
        return orch({**ask_user("A new build is out and the release sync is at 4. Two open items are yours."),
                     "options": [{"label": "Test the new build", "message": "Install build 3.2.3 and test my two events"}]})

    await runner_for(store, Script(Orchestrator=orchestrator)).run(state)
    assert seen["trigger"] == "briefing" and seen["briefing"]["preparing"] is True and seen["request"] == actions.ASK
    [ready] = for_you.feed(store)["ready"]
    assert ready["chat_id"] == chat_id and ready["status"] == "waiting_user" and ready["seen"] is True
    assert ready["options"] == [{"label": "Test the new build", "message": "Install build 3.2.3 and test my two events"}]
    assert ready["found"].startswith("A new build is out")
    assert for_you.feed(store)["waiting"] == []  # it is under the briefings, not listed twice

    # What the group says next goes to the open briefing, not to a new one.
    more = notifications.keep(store, "device-1", release_sync("Theo", "Moved to 4:30"))
    assert more["chat_id"] == chat_id and "briefing_id" not in more

    # The user's answer makes it an ordinary task: the offers are gone and it is no longer only preparing.
    store.execute("UPDATE jobs SET status='done'")  # the worker's part, which this test plays itself
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        answered = delivery.deliver(db, chat_id, "Install build 3.2.3 and test my two events", "openai")
    again = {}

    def then(context, n):
        again.update(context)
        return orch({"tool": "finish", "answer": "Tested: both events fire."})

    await runner_for(store, Script(Orchestrator=then)).run(answered)
    assert again["briefing"]["preparing"] is False and "options" not in store.load(answered["id"])
    assert for_you.feed(store)["ready"] == []  # dealt with


def test_a_chat_whose_task_is_over_no_longer_collects_what_its_conversation_says(store):
    actions.choose_app(store, WHATSAPP, True)
    make_state(store)
    store.message("chat", "user", "Tell Jordan I will be late")
    notifications.worked_in(store, "chat", WHATSAPP, ["Open the chat with Jordan Lee and send: I will be late"])
    reply = event(WHATSAPP, "WhatsApp", "Jordan Lee", "ok", conversation="Jordan Lee", sender="Jordan Lee")
    assert notifications.keep(store, "device-1", reply)["chat_id"] == "chat"  # its task is open
    store.execute("UPDATE runs SET status='waiting_user' WHERE id='run'")
    assert notifications.keep(store, "device-1", {**reply, "text": "where are you?"})["chat_id"] == "chat"
    store.execute("UPDATE runs SET status='completed' WHERE id='run'")
    later = notifications.keep(store, "device-1", {**reply, "text": "call me"})
    assert later["chat_id"] is None and later["briefing_id"]  # the finished chat is read by the briefing instead
    [row] = briefed(store)
    assert "chat" in dossier.related(store, row)


def test_a_dismissed_briefing_leaves_the_list_and_a_muted_conversation_is_not_briefed_on_again(store, monkeypatch):
    actions.choose_app(store, TEAMS, True)
    notifications.keep(store, "device-1", release_sync("Theo", "one"))
    store.execute("UPDATE briefings SET verdict='brief',headline='One',sorted=?,sorted_count=count", (time.time(),))
    with api_client(store, monkeypatch) as client:
        [ready] = client.get("/api/for-you").json()["ready"]
        assert client.post(f"/api/for-you/{ready['id']}/dismiss", json={"mute": True}).json()["ready"] == []
        listed = client.get("/api/for-you/apps").json()
        assert listed["muted"][0]["name"] == "Release sync" and listed["apps"][0] == {
            "package": TEAMS, "label": "Teams", "count": 1, "people": True, "on": True}
        assert "briefing_id" not in notifications.keep(store, "device-1", release_sync("Theo", "two"))
        client.post("/api/for-you/unmute", json={"package": TEAMS, "key": "release sync"})
        assert notifications.keep(store, "device-1", release_sync("Theo", "three"))["briefing_id"]
        # Turning the app off takes what is listed from it away.
        assert client.put(f"/api/for-you/apps/{TEAMS}", json={"on": False}).json()["apps"][0]["on"] is False
        assert json.loads(store.profile_get(policy.KEY) and json.dumps(store.profile_get(policy.KEY)))["apps"] == []
        assert "briefing_id" not in notifications.keep(store, "device-1", release_sync("Theo", "four"))


def test_the_list_holds_the_chats_that_wait_for_the_user(store, monkeypatch):
    make_state(store)
    store.message("chat", "assistant", "**Morning or evening** for the technician?", source="Orchestrator")
    store.execute("UPDATE runs SET status='waiting_user' WHERE id='run'")
    with api_client(store, monkeypatch) as client:
        feed = client.get("/api/for-you").json()
        assert [(w["chat_id"], w["asks"]) for w in feed["waiting"]] == [("chat", "Morning or evening for the technician?")]
        store.execute("UPDATE briefings SET dismissed=1")
        assert client.post("/api/for-you/999/open", json={}).status_code == 404


async def test_turning_an_app_on_takes_in_what_it_showed_today_and_the_sorter_hears_of_it(store):
    import asyncio

    now = time.time()
    # In the log from before the user chose the app: this morning's burst, a later one, and one a chat already took.
    notifications.keep(store, "device-1", release_sync("Theo", "Build 3.2.2 is up"), now=now - 5 * 3600)
    notifications.keep(store, "device-1", release_sync("Theo", "Build 3.2.3 is up"), now=now - 600)
    notifications.keep(store, "device-1", release_sync("Maria", "Release sync at 4"), now=now - 540)
    notifications.keep(store, "device-1", event(TEAMS, "Teams", "Billing v3: Lena", "old", conversation="Billing v3", sender="Lena"),
                       now=now - 3 * 86400)
    assert briefed(store) == []
    actions.choose_app(store, TEAMS, True)
    rows = briefed(store)
    # One conversation, two bursts hours apart: the later one stands for it, and last week's is left in the log.
    assert [(r["name"], r["count"], bool(r["dismissed"])) for r in rows] == [("Release sync", 1, True), ("Release sync", 2, False)]
    due, _ = triage.due(store, now)
    assert [r["count"] for r in due] == [2]
    actions.choose_app(store, TEAMS, True)  # chosen again: nothing is taken in twice
    assert len(briefed(store)) == 2

    # The sorter rests until a notification joins a briefing or the API has news, whichever is first.
    rung = asyncio.Event()
    sorter = triage.Sorter(runner_for(store, Script(Triage=[sorted_as("list", headline="Builds and a meeting")])), lambda: rung)
    task = asyncio.create_task(sorter.run())
    for _ in range(50):
        await asyncio.sleep(0)
        if for_you.feed(store)["came_in"]:
            break
    assert [i["headline"] for i in for_you.feed(store)["came_in"]] == ["Builds and a meeting"]
    task.cancel()


async def test_the_sorter_keeps_to_its_hourly_allowance_and_retries_a_failed_sort_later(store, monkeypatch):
    actions.choose_app(store, TEAMS, True)
    old = time.time() - triage.MAX_WAIT_SECONDS - 5
    notifications.keep(store, "device-1", release_sync("Theo", "one"), now=old)
    notifications.keep(store, "device-1", event(TEAMS, "Teams", "Billing v3: Lena", "two", conversation="Billing v3", sender="Lena"), now=old)
    calls = []

    async def failing(runner, briefing_id, last=False):
        calls.append(store.rows("SELECT name FROM briefings WHERE id=?", (briefing_id,))[0]["name"])
        raise RuntimeError("the database is locked")

    monkeypatch.setattr(triage, "sort", failing)
    sorter = triage.Sorter(runner_for(store, Script()))
    # Both fail outright: each is left for later, and the next turn does not try them again at once.
    assert 0 < await sorter.turn() <= triage.RETRY_SECONDS and sorted(calls) == ["Billing v3", "Release sync"]
    assert 0 < await sorter.turn() <= triage.RETRY_SECONDS and len(calls) == 2

    # The allowance: with room for one sort an hour, the second briefing waits for the hour to turn.
    monkeypatch.undo()
    monkeypatch.setattr(triage, "SORTS_PER_HOUR", 1)
    sorter = triage.Sorter(runner_for(store, Script(Triage=[sorted_as("list", headline="One")])))
    wait = await sorter.turn()
    assert 3500 < wait <= 3600
    assert [r["verdict"] for r in briefed(store)].count("list") == 1


def test_an_app_is_named_as_its_notifications_name_it_and_a_bad_package_is_refused(store, monkeypatch):
    notifications.keep(store, "device-1", {**release_sync("Theo", "one"), "label": TEAMS})  # shown before its name was known
    notifications.keep(store, "device-1", release_sync("Theo", "two"))
    assert for_you.apps(store) == [{"package": TEAMS, "label": "Teams", "count": 2, "people": True, "on": False}]
    with api_client(store, monkeypatch) as client:
        assert client.put("/api/for-you/apps/not a package", json={"on": True}).status_code == 422
        assert client.put(f"/api/for-you/apps/{TEAMS}", json={"on": True}).json()["apps"][0]["on"] is True
        assert policy.load(store)["apps"] == [TEAMS]


def test_apps_where_people_message_the_user_are_offered_before_busier_ones_that_only_advertise(store):
    shop = "com.example.shop"
    for i in range(5):
        notifications.keep(store, "device-1", event(shop, "Shop", "Deals", f"{i}0% off today"))
    notifications.keep(store, "device-1", event(WHATSAPP, "WhatsApp", "Chris", "In a meeting", sender="Chris"))
    assert [(a["package"], a["people"]) for a in for_you.apps(store)] == [(WHATSAPP, True), (shop, False)]


def test_an_opened_briefing_shows_what_arrived_as_of_when_it_came(store):
    actions.choose_app(store, TEAMS, True)
    came = time.time() - 900
    notifications.keep(store, "device-1", release_sync("Theo", "Build 3.2.3 is up"), now=came)
    [row] = briefed(store)
    chat_id, state = actions.start(store, row["id"])
    shown = store.rows("SELECT role,created FROM messages WHERE chat_id=? ORDER BY created", (chat_id,))
    assert [m["role"] for m in shown] == ["notification", "user"] and abs(shown[0]["created"] - came) < 1
    assert state["created"] > shown[0]["created"]


async def test_a_sort_never_writes_over_a_briefing_the_user_opened_or_dismissed_meanwhile(store):
    actions.choose_app(store, TEAMS, True)
    notifications.keep(store, "device-1", release_sync("Theo", "Build 3.2.3 is up"))
    [row] = briefed(store)
    opened = {}

    def slow(context, n):  # the user opens it while the agent is still reading
        opened["chat"], _ = actions.start(store, row["id"])
        return sorted_as("ignore", headline="Nothing much")

    await triage.sort(runner_for(store, Script(Triage=slow)), row["id"])
    [now] = briefed(store)
    assert now["chat_id"] == opened["chat"] and now["verdict"] == "brief" and now["headline"] is None
    # And one already opened or dismissed is not asked about at all.
    assert await triage.sort(runner_for(store, Script()), row["id"]) is None


def test_a_briefing_worth_reading_is_not_lost_to_what_the_conversation_says_hours_later(store):
    actions.choose_app(store, "com.amazon.mShop.android.shopping", True)
    amazon = lambda title, text, at: notifications.keep(  # noqa: E731 - an app that names no conversation
        store, "device-1", event("com.amazon.mShop.android.shopping", "Amazon", title, text), now=at)
    now = time.time()
    amazon("Delivered", "Your package was left at the front door", now - 4 * 3600)
    store.execute("UPDATE briefings SET verdict='brief',headline='Your package was delivered',sorted=?,sorted_count=count", (now,))
    amazon("Deals for you", "Up to 40% off", now - 60)
    old, new = briefed(store)
    assert old["dismissed"] is None and new["id"] != old["id"]  # the delivery stays; the advert is a briefing of its own
    assert [b["headline"] for b in for_you.feed(store)["ready"]] == ["Your package was delivered"]
    # One that was only listed does give way.
    store.execute("UPDATE briefings SET verdict='list',sorted=?,sorted_count=count WHERE id=?", (now, new["id"]))
    store.execute("UPDATE briefings SET latest=? WHERE id=?", (now - 3 * 3600, new["id"]))
    amazon("More deals", "Ends tonight", now)
    assert [bool(b["dismissed"]) for b in briefed(store)] == [False, True, False]


def test_a_conversation_is_known_by_its_name_whatever_its_letters_and_a_website_is_one_too(store):
    actions.choose_app(store, WHATSAPP, True)
    actions.choose_app(store, "com.android.chrome", True)
    said = lambda text, at: notifications.keep(  # noqa: E731
        store, "device-1", event(WHATSAPP, "WhatsApp", "Élodie Мама", text, conversation="Élodie Мама", sender="Élodie"), now=at)
    now = time.time()
    said("first", now - 5 * 3600)
    said("second", now)
    latest = briefed(store)[-1]
    assert latest["count"] == 1 and [n["text"] for n in groups.earlier(store, latest, 5)] == ["first"]
    # Two websites in one browser are two conversations, each under its site.
    site = lambda host, text: notifications.keep(  # noqa: E731
        store, "device-1", {**event("com.android.chrome", "Chrome", text, "tap to read"), "sub_text": host})
    assert site("news.example.com", "Election result")["briefing_id"] != site("shop.example.org", "Order shipped")["briefing_id"]


def test_a_dismissed_or_muted_briefing_no_longer_takes_what_its_conversation_says(store):
    actions.choose_app(store, TEAMS, True)
    notifications.keep(store, "device-1", release_sync("Theo", "Build 3.2.3 is up"))
    [row] = briefed(store)
    chat_id, _ = actions.start(store, row["id"])
    assert notifications.keep(store, "device-1", release_sync("Theo", "one more"))["chat_id"] == chat_id
    actions.dismiss(store, row["id"], mute=True)
    after = notifications.keep(store, "device-1", release_sync("Theo", "and another"))
    assert after["chat_id"] is None and "briefing_id" not in after


def test_a_card_that_gave_way_opens_the_briefing_that_took_its_place_and_holds_what_just_arrived(store):
    actions.choose_app(store, TEAMS, True)
    now = time.time()
    notifications.keep(store, "device-1", release_sync("Theo", "this morning"), now=now - 5 * 3600)
    store.execute("UPDATE briefings SET verdict='list',headline='Morning chatter',sorted=?,sorted_count=count", (now,))
    [stale] = briefed(store)
    notifications.keep(store, "device-1", release_sync("Maria", "Release sync at 4"), now=now)
    chat_id, state = actions.start(store, stale["id"])  # the user tapped the card that was still on their screen
    _, current = briefed(store)
    assert current["chat_id"] == chat_id and current["verdict"] == "brief"
    assert [n["text"] for n in state["briefing"]["arrived"]] == ["Release sync at 4"]
    # A briefing's dossier is the orchestrator's reading: the window is not sent it with every reload.
    from app.api.chats import run_view

    assert "briefing" not in run_view(state) and run_view(state)["status"] == "queued"


async def test_sorting_and_preparing_run_on_the_models_chosen_for_notifications(store, monkeypatch):
    chosen = {"agent": {"provider": "anthropic", "model": "claude-sonnet-5-5", "effort": "medium"},
              "subagent": {"provider": "anthropic", "model": "claude-haiku-5-5", "effort": "low"}}
    actions.choose_app(store, TEAMS, True)
    notifications.keep(store, "device-1", release_sync("Theo", "Build 3.2.3 is up"))
    [row] = briefed(store)
    with api_client(store, monkeypatch) as client:
        assert client.get("/api/for-you/apps").json()["models"] is None  # the same as the user's tasks
        wrong = {"agent": {**chosen["agent"], "model": "gpt-9"}, "subagent": chosen["subagent"]}
        assert client.put("/api/for-you/models", json={"models": wrong}).status_code == 422
        assert client.put("/api/for-you/models", json={"models": chosen}).json()["models"] == chosen
    model = Script(Triage=[sorted_as("brief")])
    await triage.sort(runner_for(store, model), row["id"])
    assert model.calls[-1]["provider"] == "anthropic"  # a sort is on the fast tier of the agent's provider
    # Preparing runs on them whatever the window sends; the user's answer then runs on the window's.
    _, state = actions.start(store, row["id"], "deepseek", None)
    assert state["provider"] == "anthropic" and state["models"] == chosen
    # A pick penrose no longer offers is the same as none; and the user can go back to their tasks' models.
    store.profile_set(policy.KEY, {**policy.load(store), "models": {**chosen, "agent": {**chosen["agent"], "model": "gone"}}})
    assert policy.models(policy.load(store)) is None
    with api_client(store, monkeypatch) as client:
        assert client.put("/api/for-you/models", json={"models": None}).json()["models"] is None



def test_a_briefing_task_sent_again_begins_as_it_did_and_still_only_prepares_when_unchanged(store, monkeypatch):
    actions.choose_app(store, TEAMS, True)
    notifications.keep(store, "device-1", release_sync("Theo", "Build 3.2.3 is up"))
    [row] = briefed(store)
    chat_id, first = actions.start(store, row["id"])

    def failed(run_id):  # the task could not finish
        store.save({**store.load(run_id), "status": "failed"})
        store.execute("DELETE FROM jobs")
        [asked] = store.rows("SELECT id FROM messages WHERE chat_id=? AND role='user'", (chat_id,))
        return f"/api/chats/{chat_id}/messages/{asked['id']}/rewind"

    with api_client(store, monkeypatch) as client:
        again = client.post(failed(first["id"]), json={"content": actions.ASK}).json()
        state = store.load(again["id"])
        assert state["briefing"] == first["briefing"] and state["route"] == {"orchestrate": True}
        assert state["trigger"] == "briefing" and state["preparing"] is True
        # Rewritten, the words are the user's own instruction: the dossier stays, the holding back does not.
        mine = client.post(failed(again["id"]), json={"content": "Draft my release sync notes"}).json()
        state = store.load(mine["id"])
        assert state["briefing"] == first["briefing"] and "preparing" not in state


def test_opening_a_briefing_answers_with_its_chat_so_the_window_needs_no_second_request(store, monkeypatch):
    actions.choose_app(store, TEAMS, True)
    notifications.keep(store, "device-1", release_sync("Theo", "Build 3.2.3 is up"))
    [row] = briefed(store)
    with api_client(store, monkeypatch) as client:
        opened = client.post(f"/api/for-you/{row['id']}/open", json={}).json()
        assert opened["chat"] == client.get(f"/api/chats/{opened['chat_id']}").json()
        assert [m["content"] for m in opened["chat"]["messages"]][-1] == actions.ASK
        # Tapped again (its notification, a second window): the same chat, no second task.
        again = client.post(f"/api/for-you/{row['id']}/open", json={}).json()
        assert again["chat_id"] == opened["chat_id"] and again["run_id"] is None


def worth_it(store):
    """A briefing the sort found worth the user's attention: what penrose prepares on its own."""
    actions.choose_app(store, TEAMS, True)
    notifications.keep(store, "device-1", release_sync("Theo", "Build 3.2.3 is up"))
    store.execute("UPDATE briefings SET verdict='brief',headline='New build, release sync at 8',sorted=?,sorted_count=count",
                  (time.time(),))
    [row] = briefed(store)
    return row


def test_penrose_prepares_a_briefing_on_its_own_one_at_a_time_and_the_user_still_has_it_unseen(store):
    from app.briefings import prepare

    row = worth_it(store)
    state = prepare.begin_next(store)
    assert state["background"] and state["preparing"] and state["trigger"] == "briefing"
    [now] = briefed(store)
    assert now["chat_id"] == state["chat_id"] and now["seen"] is None
    [card] = for_you.feed(store)["ready"]
    assert card["chat_id"] == state["chat_id"] and card["status"] == "queued" and not card["seen"]
    # One at a time: another worth it waits until this one is done.
    notifications.keep(store, "device-1", event(TEAMS, "Teams", "Ops: Dana", "Pager is quiet", conversation="Ops", sender="Dana"))
    store.execute("UPDATE briefings SET verdict='brief',sorted=?,sorted_count=count WHERE chat_id IS NULL", (time.time(),))
    assert prepare.begin_next(store) is None
    # The user opening it is their first look: seen, and no second task.
    assert actions.start(store, row["id"]) == (state["chat_id"], None)
    assert briefed(store)[0]["seen"] is not None


def test_the_users_own_task_comes_first_and_a_background_one_pauses_for_it_then_carries_on(store):
    from app.briefings import prepare

    worth_it(store)
    background = prepare.begin_next(store)
    with store.connect() as db:
        db.execute("INSERT INTO chats VALUES ('mine','Order lunch',?)", (time.time(),))
        mine = delivery.new_run(db, "mine", "Order lunch", "deepseek")
    # Queued after it, the user's own task is taken first.
    assert store.claim("worker")["run_id"] == mine["id"]
    store.execute("UPDATE jobs SET status='done'")
    store.execute("UPDATE jobs SET status='queued' WHERE run_id=?", (background["id"],))
    assert store.claim("worker")["run_id"] == background["id"] and store.background(background["id"])
    store.execute("UPDATE runs SET status='running' WHERE id=?", (background["id"],))
    store.save({**store.load(background["id"]), "status": "running"})
    # The user starts something while it works: it is stopped, its card still at work, and queued again after.
    with store.connect() as db:
        db.execute("INSERT INTO chats VALUES ('mine2','Book a cab',?)", (time.time(),))
        delivery.new_run(db, "mine2", "Book a cab", "deepseek")
    assert store.users_waiting() and prepare.pause(store, background["id"])
    assert store.status(background["id"]) == "stopped"
    assert for_you.feed(store)["ready"][0]["status"] == "queued"
    store.execute("UPDATE jobs SET status='done' WHERE run_id=?", (background["id"],))
    again = prepare.resume(store, background["id"])
    assert again["status"] == "queued" and again["trigger"] == "resumed" and "yielded" not in again
    assert prepare.resume(store, background["id"]) is None  # once
    assert store.claim("worker")["run_id"] != background["id"]  # the user's cab first


def test_a_prepared_briefing_the_user_answers_is_theirs_and_one_dismissed_stops_being_prepared(store):
    from app.briefings import prepare

    worth_it(store)
    state = prepare.begin_next(store)
    store.execute("UPDATE jobs SET status='done'")
    store.save({**store.load(state["id"]), "status": "waiting_user"})
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        answered = delivery.deliver(db, state["chat_id"], "Draft the release sync notes", "deepseek")
    assert "background" not in answered and not store.background(state["id"])
    # Another, dismissed while penrose works on it: its task stops, and is not taken up again.
    notifications.keep(store, "device-1", event(TEAMS, "Teams", "Ops: Dana", "Pager is quiet", conversation="Ops", sender="Dana"))
    store.execute("UPDATE briefings SET verdict='brief',sorted=?,sorted_count=count WHERE chat_id IS NULL", (time.time(),))
    store.execute("UPDATE jobs SET status='done'")
    other = prepare.begin_next(store)
    [ops] = [b for b in briefed(store) if b["chat_id"] == other["chat_id"]]
    actions.dismiss(store, ops["id"])
    assert store.status(other["id"]) == "stopped" and prepare.resume(store, other["id"]) is None
    assert not store.queued() or all(j["run_id"] != other["id"] for j in store.rows("SELECT run_id FROM jobs WHERE status='queued'"))


async def test_the_orchestrator_knows_a_briefing_it_prepares_on_its_own(store):
    from app.briefings import prepare

    worth_it(store)
    state = prepare.begin_next(store)
    model = Script(Orchestrator=[orch(ask_user("Here is what I found"))])
    await Orchestrator(runner_for(store, model), store.load(state["id"])).drive()
    seen = model.of("Orchestrator")[0]["context"]
    assert seen["briefing"]["on_your_own"] is True and seen["briefing"]["preparing"] is True and seen["trigger"] == "briefing"


def test_penrose_never_takes_the_phone_from_a_task_waiting_for_the_user_on_it(store):
    runner = runner_for(store, Script())
    with store.connect() as db:
        db.execute("INSERT INTO chats VALUES ('signin','Sign in to Uber',?)", (time.time(),))
        waiting = delivery.new_run(db, "signin", "Sign in to Uber", "deepseek")
    store.save({**store.load(waiting["id"]), "status": "waiting_user",
                "questions": [{"node_id": "n1", "question": "Sign in on the phone", "kind": "login"}]})
    assert runner.kept_for_user({"background": True}, waiting["id"])
    assert not runner.kept_for_user({}, waiting["id"])  # the user's own task may take it
    store.save({**store.load(waiting["id"]), "questions": [{"node_id": "n1", "question": "Which one?"}]})
    assert not runner.kept_for_user({"background": True}, waiting["id"])  # a plain question leaves the phone free


def test_a_preparing_task_the_worker_lost_carries_on_but_a_task_of_the_users_does_not(store):
    from app.briefings import prepare

    worth_it(store)
    state = prepare.begin_next(store)
    store.save({**store.load(state["id"]), "status": "interrupted"})  # the worker restarted while it worked
    store.execute("UPDATE jobs SET status='interrupted'")
    again = prepare.begin_next(store)
    assert again["id"] == state["id"] and again["status"] == "queued" and again["trigger"] == "resumed"
    # Answered by the user, it is theirs: an interrupted one waits for them, as any task of theirs does.
    store.execute("UPDATE jobs SET status='done'")
    store.save({**store.load(state["id"]), "status": "interrupted", "preparing": False})
    assert prepare.carry_on(store) is None


def test_a_briefing_penrose_prepared_and_ended_stays_until_the_user_answers_it(store):
    from app.briefings import prepare

    row = worth_it(store)
    state = prepare.begin_next(store)
    store.save({**store.load(state["id"]), "status": "completed"})
    store.message(state["chat_id"], "assistant", "Owen sent the full scoring prompt.\n\nDetails follow.")
    [card] = for_you.feed(store)["ready"]
    assert card["status"] == "completed" and card["found"] == "Owen sent the full scoring prompt." and not card["seen"]
    actions.start(store, row["id"])  # the user opens it: seen, and still theirs to answer
    feed = for_you.feed(store)
    assert [c["id"] for c in feed["ready"]] == [row["id"]] and feed["ready"][0]["seen"] and feed["unseen"] == 0
    # Ended without waiting on the user (answered, or never penrose's own preparing): dealt with.
    ended = store.load(state["id"])
    ended.pop("preparing")
    store.save(ended)
    assert for_you.feed(store)["ready"] == []


async def test_a_sort_keeps_what_is_asked_of_the_user_and_the_things_said_that_matter(store):
    actions.choose_app(store, TEAMS, True)
    notifications.keep(store, "device-1", release_sync("Nina", "Can you bring chairs on Saturday?"))
    [row] = briefed(store)
    said = sorted_as("brief", asks="  Sam asks if you\n can bring chairs. ",
                     gist=["Build  3.2.3 is up", " ", "x" * 200, "Release sync at 4"])
    await triage.sort(runner_for(store, Script(Triage=[said])), row["id"])
    [ready] = for_you.feed(store)["ready"]
    assert ready["asks"] == "Sam asks if you can bring chairs."
    assert ready["gist"] == ["Build 3.2.3 is up", "x" * 160, "Release sync at 4"]
    # A last try that cannot ask the agent keeps what it said before.
    notifications.keep(store, "device-1", release_sync("Nina", "Hello?"))

    def broken(context, n):
        raise RuntimeError("no key")

    await triage.sort(runner_for(store, Script(Triage=broken)), row["id"], last=True)
    [kept] = briefed(store)
    assert kept["asks"] == ready["asks"] and json.loads(kept["gist"]) == ready["gist"]


def test_a_briefing_looked_into_shows_what_arrived_and_what_penrose_prepared(store, monkeypatch):
    from app.briefings import prepare

    monkeypatch.setattr(for_you, "ARRIVED_SHOWN", 2)
    actions.choose_app(store, TEAMS, True)
    now = time.time()
    notifications.keep(store, "device-1", release_sync("Theo", "Build 3.2.3 is up"), now=now - 30)
    notifications.keep(store, "device-1", release_sync("Maria", "Release sync at 4"), now=now - 20)
    notifications.keep(store, "device-1", release_sync("Theo", "x" * 700), now=now - 10)
    store.execute("UPDATE briefings SET verdict='brief',headline='New build',gist=?,sorted=?,sorted_count=count",
                  (json.dumps(["Build 3.2.3 is up"]), now))
    [row] = briefed(store)
    shown = for_you.detail(store, row["id"])
    assert [a["from"] for a in shown["arrived"]] == ["Maria", "Theo"] and shown["earlier"] == 1  # oldest first
    assert shown["arrived"][0] == {"from": "Maria", "text": "Release sync at 4", "at": pytest.approx(now - 20)}
    assert len(shown["arrived"][1]["text"]) == 600 and shown["arrived"][1]["text"].endswith("…")
    assert shown["gist"] == ["Build 3.2.3 is up"] and "chat_id" not in shown and "prepared" not in shown
    # Prepared by penrose: its task, and the whole of what it wrote.
    state = prepare.begin_next(store)
    store.save({**store.load(state["id"]), "status": "completed"})
    store.message(state["chat_id"], "assistant", "**Two** events are yours.\n\n- checkout\n- cart")
    shown = for_you.detail(store, row["id"])
    assert (shown["chat_id"], shown["status"], shown["found"]) == (state["chat_id"], "completed", "Two events are yours.")
    assert shown["prepared"]["content"] == "**Two** events are yours.\n\n- checkout\n- cart" and shown["prepared"]["at"]
    # Dismissed, it leaves the list and can still be looked into.
    actions.dismiss(store, row["id"])
    assert for_you.feed(store)["ready"] == [] and for_you.detail(store, row["id"])["prepared"]
    assert for_you.detail(store, 999) is None


def test_looking_into_a_briefing_marks_it_seen_and_it_stays_listed(store, monkeypatch):
    from app.companion.tunnel import allowed

    row = worth_it(store)
    with api_client(store, monkeypatch) as client:
        assert client.get("/api/for-you").json()["unseen"] == 1
        shown = client.post(f"/api/for-you/{row['id']}/look").json()
        assert shown["id"] == row["id"] and shown["seen"] is True and shown["arrived"][0]["text"] == "Build 3.2.3 is up"
        feed = client.get("/api/for-you").json()
        assert feed["unseen"] == 0 and [b["id"] for b in feed["ready"]] == [row["id"]]
        assert client.post("/api/for-you/999/look").status_code == 404
    # The phone app reaches both new routes through its tunnel.
    assert allowed("POST", f"/api/for-you/{row['id']}/look") and allowed("POST", f"/api/for-you/waiting/{'a' * 32}/dismiss")


def test_a_waiting_chat_taken_off_the_list_returns_once_its_task_moves(store, monkeypatch):
    with store.connect() as db:
        db.execute("INSERT INTO chats VALUES ('0123abcd','Fix the fridge',?)", (time.time(),))
        run = delivery.new_run(db, "0123abcd", "Fix the fridge", "deepseek")
    store.save({**run, "status": "waiting_user", "needs": "Choose a time for the technician"})
    store.message("0123abcd", "assistant", "![slots](a.png)\n**Morning or evening** for the technician?\nBoth are free.\n\n"
                  "I checked the slots.", source="Orchestrator")
    with api_client(store, monkeypatch) as client:
        [waits] = client.get("/api/for-you").json()["waiting"]
        assert waits["needs"] == "Choose a time for the technician"
        assert waits["asks"] == "Morning or evening for the technician? Both are free."  # the first paragraph
        assert client.post("/api/for-you/waiting/0123abcd/dismiss").json()["waiting"] == []
        assert client.post("/api/for-you/waiting/ffffffff/dismiss").status_code == 404
        assert client.post("/api/for-you/waiting/not-a-chat/dismiss").status_code == 422
    # Anything that moves its task afterwards brings it back.
    store.execute("UPDATE runs SET updated=? WHERE id=?", (time.time() + 5, run["id"]))
    assert [w["chat_id"] for w in for_you.feed(store)["waiting"]] == ["0123abcd"]
    # A long question is cut, and a deleted chat leaves nothing behind.
    assert for_you.first_paragraph("word " * 100).endswith("…") and len(for_you.first_paragraph("word " * 100)) == 320
    with store.connect() as db:
        actions.forget_chat(db, "0123abcd")
    assert store.rows("SELECT * FROM for_you_hidden") == []
