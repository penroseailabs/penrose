"""Watches: what a chat's task said will come back reaches the chat from whichever app brings it; one that says
what to do then wakes the chat, once, from its own apps, into a task that can use only those apps."""

import asyncio
import json
import time

import pytest
from app import notifications, watches
from app.plan import AppTarget
from fakes import (
    FakeDevices,
    Script,
    finish,
    make_state,
    orch,
    phone,
    report,
    run_step,
    runner_for,
    set_plan,
    step_spec,
)
from fastapi import HTTPException
from test_api import api_client

WHATSAPP, UBER, GMAIL, AMAZON, CHROME = ("com.whatsapp", "com.ubercab", "com.google.android.gm",
                                         "com.amazon.mShop.android.shopping", "com.android.chrome")
ORDER = {"what": "Updates on the Amazon order of the Anker charger", "match": ["Anker charger", "112-5550142-0042137"]}
ADDRESS = {
    "what": "Jordan's reply with his address", "match": ["Jordan Lee"],
    "then": "Jordan has sent his address: book an UberX from Home to it for 6 pm today and tell the user the fare",
    "apps": [{"name": "WhatsApp", "package": WHATSAPP}, {"name": "Uber", "package": UBER}],
}


def event(app, label, title, text, **extra):
    return {"type": "notification", "app": app, "label": label, "title": title, "text": text,
            "key": f"0|{app}|1|{title}|{len(text)}", **extra}


def entries(store, chat_id="chat"):
    return [r["content"] for r in store.rows(
        "SELECT content FROM messages WHERE chat_id=? AND role='notification' ORDER BY created", (chat_id,))]


def watching(store, *specs, **extra):
    """A chat whose finished task left `specs` as watches."""
    state = make_state(store, **extra)
    state["status"] = "completed"
    store.save(state)
    store.message("chat", "user", "Order the Anker charger")
    watches.keep(store, state, [{"days": 14, **spec} for spec in specs])
    return state


def judged(arrived, reason="It says so."):
    return {"actions": [{"tool": "judge", "arrived": arrived, "reason": reason}]}


# --- what the orchestrator leaves open ---------------------------------------------------------------------


async def test_a_finished_task_leaves_a_watch_and_what_comes_back_reaches_the_chat_from_any_app(store, monkeypatch):
    state = make_state(store, request="Order the Anker charger from amazon.com", route=None)
    store.message("chat", "user", "Order the Anker charger from amazon.com")
    order = step_spec("order", "Order it in Chrome", app=AppTarget(name="Chrome", package=CHROME, website="amazon.com"))
    model = Script(
        Orchestrator=[orch(set_plan(order), run_step("order")),
                      orch({**finish("Ordered: it arrives Friday."), "watch": [ORDER]})],
        Subagent=[phone(report(summary="Placed order 112-5550142-0042137"))],
    )
    await runner_for(store, model).run(state)
    assert state["status"] == "completed"
    [kept] = watches.open_for(store, "chat")
    assert kept["what"] == ORDER["what"] and kept["match"] == ORDER["match"] and "then" not in kept
    assert any(line["line"] == f"watching for: {ORDER['what']}" for line in state["journal"])
    [told] = store.rows("SELECT payload FROM events WHERE kind='watching'")
    assert json.loads(told["payload"])["watches"][0]["id"] == kept["id"]
    # The reply went out before the watch was kept: it never waits for it.
    kinds = [r["kind"] for r in store.rows("SELECT kind FROM events ORDER BY id")]
    assert kinds.index("completed") < kinds.index("watching")

    # The store's email, its app and its website each bring an update: none from an app the task worked in.
    mail = notifications.keep(store, "device-1", event(GMAIL, "Gmail", "Amazon.com", "Shipped",
                                                       big_text="Shipped: your Anker USB-C charger is on its way"))
    alert = notifications.keep(store, "device-1", event(AMAZON, "Amazon Shopping", "Out for delivery",
                                                        "Order 112-5550142-0042137 arrives today"))
    web = notifications.keep(store, "device-1", event(CHROME, "Chrome", "Delivered", "Your Anker 30W charger was delivered",
                                                      sub_text="www.amazon.com"))
    assert [n["chat_id"] for n in (mail, alert, web)] == ["chat"] * 3 and web["source"] == "amazon.com"
    assert entries(store) == [
        "Gmail · Amazon.com: Shipped: your Anker USB-C charger is on its way",
        "Amazon Shopping · Out for delivery: Order 112-5550142-0042137 arrives today",
        "Chrome (amazon.com) · Delivered: Your Anker 30W charger was delivered",
    ]
    # What only shares the store, or one of the words, stays in the log.
    advert = notifications.keep(store, "device-1", event(AMAZON, "Amazon Shopping", "Deals for you", "Save on Anker power banks"))
    other = notifications.keep(store, "device-1", event(AMAZON, "Amazon Shopping", "Shipped", "Order 114-0000000-1111111"))
    assert advert["chat_id"] is None and other["chat_id"] is None
    # Nothing was woken: such a watch only passes on.
    assert len(store.rows("SELECT id FROM runs")) == 1

    # The next task in the chat reads what it waits for, and the window shows it.
    seen = {}

    def orchestrator(context, n):
        seen.update(context)
        return orch({**finish("It was delivered."), "stop_watching": [kept["id"]]})

    again = make_state(store, request="Has it come?", inbox=["Has it come?"])
    assert api_client(store, monkeypatch).get("/api/chats/chat").json()["watches"][0]["what"] == ORDER["what"]
    await runner_for(store, Script(Orchestrator=orchestrator)).run(again)
    assert seen["watching"] == [{"id": kept["id"], "what": ORDER["what"], "match": ORDER["match"], "until": kept["until"]}]
    assert watches.open_for(store, "chat") == []
    assert api_client(store, monkeypatch).get("/api/chats/chat").json()["watches"] == []


async def test_a_watch_needs_words_to_match_and_apps_for_what_it_does(store):
    asked = []

    def deciding(*tries):
        def orchestrator(context, n):
            asked.append(context.get("rejected_decision"))
            return orch({**finish("Sent."), "watch": [tries[n]]})
        return Script(Orchestrator=orchestrator)

    # The orchestrator is told why, and decides again, as for any tool.
    state = make_state(store, request="Tell Jordan", route=None)
    await runner_for(store, deciding({"what": "Jordan's reply", "match": ["a b"]}, {"what": "Jordan's reply", "match": ["Jordan"]})).run(state)
    assert "three letters" in asked[1] and [w["match"] for w in watches.open_for(store, "chat")] == [["Jordan"]]
    asked.clear()
    state = make_state(store, request="Tell Jordan", route=None)
    await runner_for(store, deciding({**ADDRESS, "apps": []}, ADDRESS)).run(state)
    assert "needs `apps`" in asked[1]
    kept = watches.open_for(store, "chat")[-1]
    assert kept["then"] == ADDRESS["then"] and kept["apps"] == ["WhatsApp", "Uber"]


def test_a_watch_ends_when_the_user_says_so_when_it_is_set_again_and_when_its_time_is_up(store, monkeypatch):
    state = watching(store, ORDER)
    client = api_client(store, monkeypatch)
    [kept] = watches.open_for(store, "chat")
    # Set again under the same name: one watch, the newer.
    [again] = watches.keep(store, state, [{**ORDER, "match": ["Anker"], "days": 2}])
    assert again["id"] != kept["id"] and again["match"] == ["Anker"]
    assert client.delete(f"/api/chats/chat/watches/{kept['id']}").status_code == 404
    assert client.delete(f"/api/chats/chat/watches/{again['id']}").json() == {"watches": []}
    assert notifications.keep(store, "device-1", event(AMAZON, "Amazon", "Shipped", "Anker charger"))["chat_id"] is None
    # Past its days it is over by itself.
    watches.keep(store, state, [{**ORDER, "days": 1}])
    later = time.time() + 2 * 86400
    with store.connect() as db:
        assert watches.open_rows(db, "chat", now=later) == []
    assert store.rows("SELECT why FROM watches ORDER BY id")[-1]["why"] == watches.EXPIRED
    # A chat keeps a few at most, and a deleted chat none.
    watches.keep(store, state, [{"what": f"Thing {n}", "match": [f"thing{n} word"]} for n in range(watches.MAX_OPEN + 2)])
    assert len(watches.open_for(store, "chat")) == watches.MAX_OPEN
    with client:
        assert client.delete("/api/chats/chat").status_code == 200
    assert store.rows("SELECT id FROM watches") == []


def test_a_watch_that_only_passes_on_gives_way_in_a_chat_nobody_reads(store, monkeypatch):
    watching(store, ORDER)
    monkeypatch.setattr(notifications, "NOTE_CAP", 2)
    passed = [notifications.keep(store, "device-1", event(AMAZON, "Amazon", f"Update {n}", "Anker charger"))["chat_id"]
              for n in range(3)]
    assert passed == ["chat", "chat", None]


# --- the conversation an app declares ----------------------------------------------------------------------


def test_a_group_is_known_by_its_own_title_and_then_by_the_apps_id_for_it(store):
    make_state(store)
    store.message("chat", "user", "Ask the Home group who is cooking")
    notifications.worked_in(store, "chat", WHATSAPP, ["Open the Home group and ask who is cooking"])
    # Android titles it "<group>: <sender>"; the app also says which is which.
    first = notifications.keep(store, "device-1", event(WHATSAPP, "WhatsApp", "Home: Dad", "I am", conversation="Home",
                                                        sender="Dad", thread="grp-1"))
    assert first["chat_id"] == "chat" and (first["conversation"], first["sender"], first["thread"]) == ("Home", "Dad", "grp-1")
    # Renamed, it is still that conversation; another with the old name's sender is not.
    renamed = notifications.keep(store, "device-1", event(WHATSAPP, "WhatsApp", "Family: Mum", "me too", conversation="Family",
                                                          sender="Mum", thread="grp-1"))
    dad = notifications.keep(store, "device-1", event(WHATSAPP, "WhatsApp", "Dad", "call me", sender="Dad", thread="dm-9"))
    assert renamed["chat_id"] == "chat" and dad["chat_id"] is None


def test_a_notification_is_dated_when_the_phone_posted_it_and_keeps_its_fuller_text(store):
    make_state(store)
    posted = time.time() - 600
    kept = notifications.keep(store, "device-1", event(GMAIL, "Gmail", "Uber", "Your receipt", when=posted * 1000,
                                                       big_text="Your receipt\nThanks for riding, total $18.40. Code 482913"))
    [row] = store.rows("SELECT posted,text FROM notifications")
    assert abs(row["posted"] - posted) < 0.01 and "482913" in row["text"] and kept["text"].startswith("Your receipt\nThanks")
    # A clock that is off (ahead, or days behind) is not believed.
    for when in (time.time() + 3600, time.time() - 3 * 86400):
        notifications.keep(store, "device-1", event(GMAIL, "Gmail", "Uber", f"later {when}", when=when * 1000))
    assert all(abs(r["posted"] - time.time()) < 5 for r in store.rows("SELECT posted FROM notifications ORDER BY id")[1:])


# --- waking ------------------------------------------------------------------------------------------------


async def test_a_watch_with_then_wakes_its_chat_once_from_its_own_apps_into_a_task_held_to_them(store):
    watching(store, ADDRESS, provider="openai")
    [kept] = watches.open_for(store, "chat")
    checks = []

    def check(context, n):
        checks.append(context)
        return judged("address" in context["notification"]["text"])

    devices = FakeDevices()
    runner, rung = runner_for(store, Script(Watch_check=check), devices=devices), []
    intake = notifications.Intake(store, None, runner, ring=lambda: rung.append(1))

    async def arrives(note_event):
        note = notifications.keep(store, "device-1", note_event)
        if note.get("watch"):
            await intake.wake(note)
        return note

    # The words in another app: passed to the chat, but it wakes nothing and no model is asked.
    elsewhere = await arrives(event("org.telegram.messenger", "Telegram", "Jordan Lee", "my address is 1 Elm St"))
    assert elsewhere["chat_id"] == "chat" and checks == [] and len(store.rows("SELECT id FROM runs")) == 1
    # From the app, but not what it waits for: the check says so, and the watch stays.
    await arrives(event(WHATSAPP, "WhatsApp", "Jordan Lee", "give me a minute"))
    assert len(checks) == 1 and len(store.rows("SELECT id FROM runs")) == 1 and rung == []
    assert checks[0]["watch"]["waits_for"] == ADDRESS["what"] and checks[0]["notification"]["app"] == "WhatsApp"
    # What it waits for: a task starts on the instruction written then.
    await arrives(event(WHATSAPP, "WhatsApp", "Jordan Lee", "my address is 12 Oak Lane"))
    assert rung == [1] and watches.open_for(store, "chat") == []
    assert store.rows("SELECT why FROM watches WHERE id=?", (kept["id"],))[0]["why"] == watches.WOKE
    [row] = store.rows("SELECT id,status FROM runs WHERE id!='run'")
    woken = store.load(row["id"])
    assert row["status"] == "queued" and store.rows("SELECT run_id FROM jobs WHERE status='queued'")[0]["run_id"] == row["id"]
    assert woken["request"] == ADDRESS["then"] and woken["scope"] == [WHATSAPP, UBER] and woken["provider"] == "openai"
    assert woken["route"] == {"orchestrate": True}  # the gate is for the user's messages
    # It woke once: his next message is only passed on.
    await arrives(event(WHATSAPP, "WhatsApp", "Jordan Lee", "the address again: 12 Oak Lane"))
    assert len(checks) == 2 and len(store.rows("SELECT id FROM runs")) == 2

    # The woken task: told why it runs, its phone limited to the watch's apps, its subagent told so.
    seen = {}

    def orchestrator(context, n):
        if n == 0:
            seen.update(context)
            return orch(set_plan(step_spec("book", "Book the Uber", app=AppTarget(name="Uber", package=UBER))), run_step("book"))
        return orch(finish("Jordan sent 12 Oak Lane. UberX booked for 6 pm, $14."))

    def subagent(text, n):
        seen["brief"] = text
        return phone(report(summary="Booked"))

    await runner_for(store, Script(Orchestrator=orchestrator, Subagent=subagent), devices=devices).run(woken)
    assert seen["trigger"] == "watch" and seen["request"] == ADDRESS["then"]
    assert seen["woken_by"]["waited_for"] == ADDRESS["what"] and seen["woken_by"]["limited_to_apps"] is True
    assert seen["woken_by"]["notification"]["text"] == "my address is 12 Oak Lane"
    assert seen["woken_by"]["only_apps"] == ["WhatsApp", "Uber"]
    assert devices.limits and all(apps == [WHATSAPP, UBER] for apps in devices.limits)
    assert "only_these_apps" in seen["brief"] and "WhatsApp, Uber" in seen["brief"]
    # The conversation shows what arrived as someone else's words, never as the user's.
    roles = [m["role"] for m in store.conversation_context("chat")["conversation"]]
    assert roles.count("user") == 1 and "notification" in roles and woken["status"] == "completed"


async def test_a_working_task_is_not_woken_and_a_waiting_one_goes_on(store):
    working = watching(store, ADDRESS)
    working["status"] = "running"
    store.save(working)
    runner = runner_for(store, Script(Watch_check=lambda context, n: judged(True)))
    note = notifications.keep(store, "device-1", event(WHATSAPP, "WhatsApp", "Jordan Lee", "my address is 12 Oak Lane"))
    # The chat's task is working: it reads the notification at its next decision.
    assert await watches.wake(runner, note["watch"], note) is None
    assert len(watches.open_for(store, "chat")) == 1 and store.rows("SELECT content FROM inbox WHERE mode='notice'")
    # It waits for the user: the same task goes on, on what the watch says, held to the watch's apps.
    waiting = store.load("run")
    waiting["status"] = "waiting_user"
    store.save(waiting)
    state = await watches.wake(runner, note["watch"], note)
    assert state["id"] == "run" and state["status"] == "queued" and state["trigger"] == "watch"
    assert state["scope"] == [WHATSAPP, UBER] and state["woken"]["then"] == ADDRESS["then"]
    assert watches.open_for(store, "chat") == []


async def test_the_users_own_message_lifts_the_limit_of_a_woken_task(store):
    state = make_state(store, request=ADDRESS["then"], scope=[WHATSAPP, UBER], slot="device-1",
                       woken={"waited_for": ADDRESS["what"], "only_apps": ["WhatsApp", "Uber"]}, inbox=["Also order pizza"])
    devices, seen = FakeDevices(), {}

    def orchestrator(context, n):
        seen.update(context)
        return orch(finish("On it."))

    runner = runner_for(store, Script(Orchestrator=orchestrator), devices=devices)
    from app.runtime.orchestrator import Orchestrator

    await Orchestrator(runner, state).drive()
    assert "scope" not in state and devices.limits == [None] and seen["woken_by"]["limited_to_apps"] is False


async def test_a_watch_that_matches_too_much_to_be_what_it_waits_for_is_closed(store, monkeypatch):
    watching(store, ADDRESS)
    monkeypatch.setattr(watches, "MAX_CHECKS", 2)
    notes = [notifications.keep(store, "device-1", event(WHATSAPP, "WhatsApp", "Jordan Lee", f"message {n}")) for n in range(4)]
    assert [bool(n.get("watch")) for n in notes] == [True, True, False, False]
    assert store.rows("SELECT why FROM watches")[0]["why"] == watches.NOISY


# --- the gateway: a lease held to some apps -----------------------------------------------------------------


@pytest.fixture
def held(tmp_path, monkeypatch):
    """A phone leased to "run", limited to WhatsApp and Uber, with `front` the app in front."""
    from app.gateway import core, scope
    from app.store import Store

    class Phone:
        front = WHATSAPP

        async def foreground(self):
            return self.front

    gateway_store = Store(tmp_path / "gateway")
    monkeypatch.setattr(core, "store", gateway_store)
    monkeypatch.setattr(core, "slots", {"device-1": "emulator-5554"})
    monkeypatch.setattr(core, "phones", {"device-1": Phone()})
    assert gateway_store.lease(["device-1"], "run") == "device-1"
    scope.keep("run", [WHATSAPP, UBER])
    return core.phones["device-1"]


async def test_a_limited_lease_acts_only_in_its_apps_and_androids_own(held):
    from app.bridge.opscatalog import NOTIFIER
    from app.gateway import scope
    from app.models import Action

    assert scope.of("device-1", "run") == {WHATSAPP, UBER} and scope.of("device-1", "someone-else") is None

    async def refused(kind, **fields):
        try:
            await scope.action("device-1", "run", Action(kind=kind, **fields))
        except HTTPException as exc:
            return exc.status_code == 422 and exc.detail
        return False

    # In one of its apps, and on the home screen or a permission dialog: taps and text go ahead.
    for front in (WHATSAPP, UBER, "app.relay.home", "com.google.android.permissioncontroller"):
        held.front = front
        assert not await refused("tap", x=5, y=5) and not await refused("type_text", text="hi")
        assert await scope.op("device-1", "run", {"op": "set_value", "target": {"text": "Tip"}, "value": "on"}) is None
    # Another app in front (a link opened it, a notification was tapped): nothing but leaving.
    held.front = "com.bank.app"
    assert "com.bank.app is in front" in await refused("tap", x=5, y=5)
    assert await refused("type_text", text="hi") and await refused("swipe", x=1, y=1, x2=2, y2=2)
    assert not await refused("back") and not await refused("home")
    assert "com.bank.app is in front" in await scope.op("device-1", "run", {"op": "scroll_to", "target": {}, "pos": "top"})
    # Whatever is in front: no raw adb, no installs, no sign-ins, no device settings; reads always.
    held.front = WHATSAPP
    assert await refused("adb_command", command="adb shell pm clear com.bank.app")
    assert await refused("install_app", package="com.bank.app") and await refused("google_signin")
    for op in ({"op": "wifi", "value": "off"}, {"op": "notifications_clear"}, {"op": "geo", "lat": 1, "lng": 2},
               {"op": "launch", "package": "com.bank.app"}, {"op": "stop", "package": "com.bank.app"},
               {"op": "open", "uri": "https://bank.example"}, {"op": "prefs", "action": "set", "package": "com.bank.app"}):
        assert await scope.op("device-1", "run", op), op
    for op in ({"op": "launch", "package": UBER}, {"op": "open", "uri": "uber://", "package": UBER}, {"op": "state"},
               {"op": "notifications"}, {"op": "wait_for", "text": "Fare"}, {"op": "prefs", "action": "get", "package": "com.bank.app"},
               {"op": "animations", "value": "off"}, {"op": "shade", "value": "collapse"}):
        assert await scope.op("device-1", "run", op) is None, op
    # Through the bridge: a click in another app's process is refused the way the bridge refuses one; reads are not.
    assert await scope.bridge_call("device-1", "run", "click", UBER, {"h": 3}) is None
    assert await scope.bridge_call("device-1", "run", "snapshot", "com.bank.app", {}) is None
    answer = await scope.bridge_call("device-1", "run", "click", "com.bank.app:ui", {"h": 3})
    assert answer["ok"] is False and answer["refused"] and "com.bank.app is not one of them" in answer["error"]
    held.front = "com.bank.app"
    assert (await scope.bridge_call("device-1", "run", "click", None, {"h": "ua:10,20"}))["refused"]
    tap = {"key": "0|com.bank.app|7|null|10200"}
    assert (await scope.bridge_call("device-1", "run", "notification_click", NOTIFIER, tap))["refused"]
    assert await scope.bridge_call("device-1", "run", "notification_click", NOTIFIER, {"key": f"0|{UBER}|7|null|10200"}) is None
    # The app in front could not be told: nothing is done blind.
    held.front = None
    assert "could not be told" in await refused("tap", x=5, y=5)


async def test_a_lease_without_a_limit_is_not_held_to_anything_and_leasing_again_lifts_one(held):
    from app.gateway import scope
    from app.models import Action

    held.front = "com.bank.app"
    scope.keep("run", None)
    assert scope.of("device-1", "run") is None
    await scope.action("device-1", "run", Action(kind="adb_command", command="adb shell ls"))
    assert await scope.op("device-1", "run", {"op": "wifi", "value": "off"}) is None
    assert await scope.bridge_call("device-1", "run", "click", "com.bank.app", {"h": 3}) is None


def test_the_ops_of_a_limited_lease_stop_at_the_first_one_it_may_not_run(held, monkeypatch):
    from app import gateway
    from app.gateway import core, sleep
    from fastapi.testclient import TestClient

    ran = []

    class Lane:
        async def run(self, op):
            ran.append(op["op"])
            return {"op": op["op"], "ok": True, "summary": "done"}

    async def wake(slot):
        return False

    monkeypatch.setattr(core, "ops_lane", {"device-1": Lane()})
    monkeypatch.setattr(core, "redactions", {"device-1": set()})
    monkeypatch.setattr(gateway.settings, "gateway_token", "")
    monkeypatch.setattr(sleep, "wake", wake)
    monkeypatch.setattr(sleep, "idle", lambda slot, delay=None: None)
    body = {"owner": "run", "ops": [{"op": "launch", "package": UBER}, {"op": "wifi", "value": "off"}, {"op": "state"}]}
    results = TestClient(gateway.app).post("/devices/device-1/ops", json=body).json()["results"]
    assert ran == ["launch"] and [r["ok"] for r in results] == [True, False] and results[1]["refused"]
    assert "may only use com.ubercab, com.whatsapp" in results[1]["summary"]


# --- the gateway hears the phone's listener ------------------------------------------------------------------


async def test_the_gateway_holds_a_read_on_the_listener_and_tells_whoever_waits(monkeypatch):
    from app.gateway import core, notices

    asked, release = [], asyncio.Event()

    async def listener(reader, writer):
        """The module's notifier: answers the first read at once, holds the next until a notification is posted."""
        while line := await reader.readline():
            ask = json.loads(line)
            asked.append((ask["since"], ask["wait"]))
            if ask["wait"]:
                await release.wait()
            seq = 8 if release.is_set() else 7
            writer.write((json.dumps({"ok": True, "id": ask["id"], "seq": seq, "events": [], "held": True}) + "\n").encode())
            await writer.drain()
            if release.is_set():
                break
        writer.close()

    server = await asyncio.start_server(listener, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    async def adb(slot, *args, timeout=25):
        return str(port).encode() if args[0] == "forward" and args[1] == "tcp:0" else b""

    monkeypatch.setattr(core, "adb", adb)
    monkeypatch.setattr(notices, "seqs", {})
    monkeypatch.setattr(notices, "_news", {})
    hearing = asyncio.create_task(notices._hear("device-1"))
    try:
        assert await notices.changed("device-1", notices.NOWHERE, 2) and notices.seq("device-1") == 7
        # Nothing new: whoever waits is not answered, and the listener is asked once, with a wait, not over and over.
        assert not await notices.changed("device-1", 7, 0.2)
        assert asked == [(notices.NOWHERE, 0), (7, notices.HOLD_SECONDS * 1000)]
        waiter = asyncio.create_task(notices.changed("device-1", 7, 5))
        await asyncio.sleep(0.05)
        release.set()  # a notification is posted
        assert await waiter and notices.seq("device-1") == 8
    finally:
        hearing.cancel()
        server.close()


def test_a_held_read_asks_the_phone_only_when_its_listener_has_news(monkeypatch):
    from app import gateway
    from app.gateway import core, notices, sleep
    from fastapi.testclient import TestClient

    calls = []

    class Phone:
        async def call(self, cmd, process=None, **args):
            calls.append((cmd, args))
            return {"ok": True, "seq": 9, "events": [{"type": "notification", "app": WHATSAPP, "label": "WhatsApp",
                                                      "title": "Home", "text": "milk", "seq": 9}]}

    monkeypatch.setattr(core, "slots", {"device-1": "emulator-5554"})
    monkeypatch.setattr(core, "phones", {"device-1": Phone()})
    monkeypatch.setattr(core, "redactions", {"device-1": set()})
    monkeypatch.setattr(gateway.settings, "gateway_token", "")
    monkeypatch.setattr(sleep, "idle", lambda slot, delay=None: calls.append("idle"))
    monkeypatch.setattr(notices, "_news", {})
    client = TestClient(gateway.app)
    path = "/devices/device-1/notifications/events"
    # The phone is not up: the wait is held here, and says so.
    monkeypatch.setattr(notices, "seqs", {})
    assert client.get(path, params={"since": 8, "wait": 0.05}).json() == {"seq": None, "events": []}
    # Up, nothing after what the caller has: no call on the phone.
    monkeypatch.setattr(notices, "seqs", {"device-1": 8})
    monkeypatch.setattr(notices, "_news", {})  # the test client runs each request on a loop of its own
    assert client.get(path, params={"since": 8, "wait": 0.05}).json() == {"seq": 8, "events": []}
    assert calls == []
    # The listener moved on: read at once, with the app's name as the listener gave it (no second call for names).
    monkeypatch.setattr(notices, "seqs", {"device-1": 9})
    monkeypatch.setattr(notices, "_news", {})
    found = client.get(path, params={"since": 8, "wait": 5}).json()
    assert found["seq"] == 9 and found["events"][0]["label"] == "WhatsApp" and calls == [("events", {"since": 8})]


async def test_the_intake_is_told_of_a_notification_instead_of_asking(store):
    class Gateway:
        def __init__(self):
            self.reads, self.events, self.news = [], [], asyncio.Event()

        async def notification_events(self, slot, since, wait=0):
            self.reads.append((since, wait))
            if wait and since == len(self.events):  # as the gateway: held while the listener has nothing after it
                await asyncio.wait_for(self.news.wait(), wait)
                self.news.clear()
            return {"seq": len(self.events), "events": [e for e in self.events if e["seq"] > since]}

    gateway = Gateway()
    intake = notifications.Intake(store, gateway)
    following = asyncio.create_task(intake.follow("device-1"))
    await asyncio.sleep(0.05)
    assert gateway.reads == [(notifications.NOWHERE, notifications.HOLD_SECONDS), (0, notifications.HOLD_SECONDS)]
    gateway.events.append({**event(WHATSAPP, "WhatsApp", "Home", "milk"), "seq": 1})
    gateway.news.set()
    await asyncio.sleep(0.05)
    following.cancel()
    assert [r["text"] for r in store.rows("SELECT text FROM notifications")] == ["milk"]
    assert len(gateway.reads) == 3  # one read per notification, then held again


# --- the phone keeps hearing its apps ------------------------------------------------------------------------


def test_doze_opens_its_window_at_penroses_pace_and_apps_are_not_left_stopped(monkeypatch):
    from app.gateway import grants, sleep

    monkeypatch.setattr(sleep.settings, "phone_doze_window_seconds", 900)
    assert sleep.doze_pace().count("900000") == 2 and "idle_to" in sleep.doze_pace() and "max_idle_to" in sleep.doze_pace()
    monkeypatch.setattr(sleep.settings, "phone_doze_window_seconds", 0)
    assert sleep.doze_pace() == ""
    assert "cmd package unstop $pkg" in grants.grant_script(["com.example"])
