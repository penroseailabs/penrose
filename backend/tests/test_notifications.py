"""Notifications: every one the phone shows is kept in a log, the ones from a conversation a chat's steps worked
in are passed to that chat as someone else's words, and look_up reads the log as leads."""

import json
import time

from app import delivery, notifications
from app.plan import AppTarget
from app.runtime.memory import learn
from fakes import (
    Script,
    ask_user,
    finish,
    make_state,
    note,
    orch,
    phone,
    report,
    run_step,
    runner_for,
    set_plan,
    step_spec,
)
from test_api import api_client

WHATSAPP = "com.whatsapp"


class NotifyingDevices:
    """The gateway as the intake reads it: one phone whose listener holds `events`."""

    def __init__(self):
        self.events = []
        self.reads = []
        self.restarted = 0  # how many events the listener had dropped when it restarted

    async def phones(self):
        return [{"id": "device-1", "connected": True}]

    async def notification_events(self, slot, since, wait=0):
        self.reads.append(since)
        return {"seq": len(self.events), "events": [e for e in self.events if e["seq"] > since]}

    def post(self, app, title, text, label=None, key=None):
        self.events.append({"type": "notification", "app": app, "label": label or app, "title": title, "text": text,
                            "key": key or f"0|{app}|1|{title}|10123", "seq": len(self.events) + 1})


def logged(store):
    return store.rows("SELECT package,label,title,text,chat_id FROM notifications ORDER BY id")


def entries(store, chat_id="chat"):
    return store.rows("SELECT role,source,content FROM messages WHERE chat_id=? AND role='notification' ORDER BY created", (chat_id,))


def messaged_jordan(store, **extra):
    """A chat whose step sent Jordan Lee a WhatsApp message."""
    state = make_state(store, **extra)
    store.message("chat", "user", "Tell Jordan I will be 10 minutes late")
    notifications.worked_in(store, "chat", WHATSAPP, ["Open the chat with Jordan Lee and send: I will be 10 minutes late"])
    return state


def jordan(text="ok, no worries"):
    return {"type": "notification", "app": WHATSAPP, "label": "WhatsApp", "title": "Jordan Lee", "text": text,
            "key": f"0|{WHATSAPP}|1|jordan|10123"}


async def test_every_new_notification_is_logged_and_what_was_there_before_is_not(store):
    devices = NotifyingDevices()
    devices.post(WHATSAPP, "Home", "old news")  # in the listener before penrose began keeping them
    intake = notifications.Intake(store, devices)
    await intake.read("device-1")
    assert logged(store) == [] and devices.reads == [notifications.NOWHERE]
    assert notifications.began(store)

    devices.post(WHATSAPP, "Home", "Mom: get 2 litres of milk", label="WhatsApp")
    devices.post("com.ubercab", "Your ride", "Arriving in 3 minutes", label="Uber")
    devices.post("com.android.systemui", "USB debugging", "connected")  # the system's: not kept
    devices.post("app.relay.home", "penrose", "ready")  # penrose's own: not kept
    devices.events.append({"type": "notification_removed", "app": WHATSAPP, "seq": len(devices.events) + 1})
    await intake.read("device-1")
    assert [(n["label"], n["title"], n["text"], n["chat_id"]) for n in logged(store)] == [
        ("WhatsApp", "Home", "Mom: get 2 litres of milk", None), ("Uber", "Your ride", "Arriving in 3 minutes", None)]
    # Nothing was started and no chat was written to: a notification is not a request.
    assert not store.rows("SELECT id FROM runs") and not store.rows("SELECT id FROM messages")
    await intake.read("device-1")
    assert len(logged(store)) == 2


async def test_the_place_in_the_listener_survives_a_restart_of_the_worker_and_of_the_phone(store):
    devices = NotifyingDevices()
    await notifications.Intake(store, devices).read("device-1")
    devices.post(WHATSAPP, "Home", "one")
    # A new worker reads on from where the last one stopped.
    await notifications.Intake(store, devices).read("device-1")
    assert [n["text"] for n in logged(store)] == ["one"] and devices.reads[-1] == 0
    devices.post(WHATSAPP, "Home", "two")
    devices.post(WHATSAPP, "Home", "three")
    await notifications.Intake(store, devices).read("device-1")
    assert [n["text"] for n in logged(store)] == ["one", "two", "three"]
    # The phone restarted: its listener counts from the start again, and all it holds is new.
    devices.events = []
    devices.post(WHATSAPP, "Home", "after the restart")
    await notifications.Intake(store, devices).read("device-1")
    assert [n["text"] for n in logged(store)][-1] == "after the restart" and devices.reads[-2:] == [3, 0]


def test_a_notification_said_again_under_its_key_is_kept_once_and_an_app_keeps_its_name(store):
    first = notifications.keep(store, "device-1", jordan("ok"))
    assert first["label"] == "WhatsApp"
    assert notifications.keep(store, "device-1", jordan("ok")) is None
    # The shade no longer shows it, so the gateway could only give the package: the name comes from the log.
    later = notifications.keep(store, "device-1", {**jordan("on my way"), "label": WHATSAPP})
    assert later["label"] == "WhatsApp" and len(logged(store)) == 2
    assert notifications.keep(store, "device-1", {**jordan(), "ongoing": True}) is None
    assert notifications.keep(store, "device-1", {**jordan(), "title": "", "text": ""}) is None


def test_a_reply_reaches_the_chat_whose_open_task_wrote_to_that_person_as_a_notification_entry(store):
    messaged_jordan(store, status="waiting_user")
    store.execute("UPDATE runs SET status='waiting_user' WHERE id='run'")
    kept = notifications.keep(store, "device-1", jordan())
    assert kept["chat_id"] == "chat"
    # Someone else's words: an entry of its own role, never the user's, and it starts nothing.
    assert entries(store) == [{"role": "notification", "source": "Notification", "content": "WhatsApp · Jordan Lee: ok, no worries"}]
    assert store.rows("SELECT id FROM jobs") == [] and store.rows("SELECT id FROM inbox") == []
    assert len(store.rows("SELECT id FROM runs")) == 1
    # The window follows the chat's latest run: that is how it hears of the entry.
    assert [json.loads(e["payload"])["app"] for e in store.rows("SELECT payload FROM events WHERE kind='notification'")] == ["WhatsApp"]
    # The agents read it in the conversation with when it came; the user's own messages stay the user's.
    conversation = store.conversation_context("chat")["conversation"]
    assert [m["role"] for m in conversation] == ["user", "notification"] and conversation[1]["at"]
    assert conversation[1]["content"] == "WhatsApp · Jordan Lee: ok, no worries"


def test_only_the_named_conversation_of_an_app_the_chat_worked_in_recently_reaches_it(store):
    messaged_jordan(store)
    # Another person in the same app, the same person in another app, and the app's own name as a title.
    assert notifications.keep(store, "device-1", {**jordan(), "title": "Dana Kim", "key": "k2"})["chat_id"] is None
    assert notifications.keep(store, "device-1", {**jordan(), "app": "org.telegram.messenger", "key": "k3"})["chat_id"] is None
    assert notifications.keep(store, "device-1", {**jordan(), "title": "WhatsApp", "key": "k4"})["chat_id"] is None
    # A part of a name is not the name; a count the app adds to the name is.
    assert not notifications.named("send it to Jordan Leeson", "Jordan Lee")
    assert notifications.named("the chat with Jordan Lee.", "Jordan Lee (2 messages)")
    # Android's title for a message in a named conversation: the conversation counts, the sender alone does not.
    assert notifications.named("post the list in the Home group", "Home: Dad")
    assert not notifications.named("the chat with Dad", "Home: Dad")
    assert notifications.keep(store, "device-1", {**jordan("there?"), "title": "Jordan Lee (2 messages)"})["chat_id"] == "chat"
    # Long after the chat last worked in the app, its notifications are only logged.
    store.execute("UPDATE chat_apps SET used=?", (time.time() - (notifications.AFFINITY_DAYS + 1) * 86400,))
    assert notifications.keep(store, "device-1", jordan("still there?"))["chat_id"] is None
    assert len(entries(store)) == 1 and len(logged(store)) == 5


def test_the_latest_chat_that_named_the_conversation_gets_it(store):
    messaged_jordan(store)
    store.execute("INSERT INTO chats VALUES ('newer','later',?)", (time.time(),))
    store.message("newer", "user", "Ask Jordan about Friday")
    store.execute("INSERT INTO runs VALUES ('newer-run','newer','waiting_user','{}',?)", (time.time(),))
    store.execute("UPDATE chat_apps SET used=used-60")
    notifications.worked_in(store, "newer", WHATSAPP, ["Message Jordan Lee: are we on for Friday?"])
    assert notifications.keep(store, "device-1", jordan())["chat_id"] == "newer"


def test_a_busy_conversation_stops_filling_a_chat_nobody_writes_in(store):
    messaged_jordan(store)
    for n in range(notifications.NOTE_CAP + 3):
        notifications.keep(store, "device-1", jordan(f"message {n}"))
    assert len(entries(store)) == notifications.NOTE_CAP and len(logged(store)) == notifications.NOTE_CAP + 3
    # The user writes in the chat again: it is being read, so it is passed what comes.
    store.message("chat", "user", "What did he say?")
    assert notifications.keep(store, "device-1", jordan("one more"))["chat_id"] == "chat"


async def test_a_step_that_works_in_an_app_makes_its_chat_the_home_of_that_conversation_while_its_task_is_open(store):
    state = make_state(store, request="Tell Jordan I will be late", route=None)
    store.message("chat", "user", "Tell Jordan I will be late")
    send = step_spec("send", "Send Jordan Lee a WhatsApp message", app=AppTarget(name="WhatsApp", package=WHATSAPP),
                     instructions="Open the chat with Jordan Lee and send: I will be 10 minutes late")
    model = Script(
        Orchestrator=[orch(set_plan(send, context="Jordan Lee, +1 512-555-0142"), run_step("send")),
                      orch(ask_user("Sent. Should I wait for his answer?")), orch(finish("Done."))],
        Subagent=[phone(report(summary="Sent the message to Jordan Lee"))],
    )
    runner = runner_for(store, model)
    await runner.run(state)
    [app] = store.rows("SELECT package,about FROM chat_apps WHERE chat_id='chat'")
    assert app["package"] == WHATSAPP and "Jordan Lee" in app["about"]
    assert notifications.keep(store, "device-1", jordan())["chat_id"] == "chat"
    # Once the task is over the chat receives no more: a reply that matters then is what a watch is for.
    state["inbox"] = ["No"]
    await runner.run(state)
    assert state["status"] == "completed"
    assert notifications.keep(store, "device-1", jordan("see you then"))["chat_id"] is None


async def test_a_working_task_reads_a_notification_at_its_next_decision_never_as_the_users_message(store):
    state = messaged_jordan(store, request="Tell Jordan I will be late and book a table", route=None)
    wait = step_spec("wait", "Wait for the confirmation")

    def orchestrator(context, n):
        if n == 0:
            return orch(set_plan(wait, context="Jordan Lee"), run_step("wait"))
        assert context["new_notifications"] == ["WhatsApp · Jordan Lee: ok, no worries"]
        assert context["new_user_messages"] == [] and context["trigger"] == "step_reported"
        return orch(finish("Jordan says it is fine."))

    def subagent(text, n):
        if n == 0:
            notifications.keep(store, "device-1", jordan())  # Jordan replies while the step works
            return phone(note("Still waiting"))
        assert "ok, no worries" not in text  # the step is not steered by it
        return phone(report(summary="Confirmed"))

    model = Script(Orchestrator=orchestrator, Subagent=subagent)
    await runner_for(store, model).run(state)
    assert state["status"] == "completed" and "notifications" not in state and state["inbox"] == []
    assert any(line["line"] == "notification: WhatsApp · Jordan Lee: ok, no worries" for line in state["journal"])
    assert store.rows("SELECT * FROM inbox") == [] and len(entries(store)) == 1
    # It is in the conversation as a notification; the user's messages are the user's only.
    assert [m["content"] for m in store.rows("SELECT content FROM messages WHERE role='user'")] == ["Tell Jordan I will be 10 minutes late"]


def test_a_notification_the_task_never_read_is_not_sent_on_as_a_message(store, monkeypatch):
    messaged_jordan(store)
    notifications.keep(store, "device-1", jordan())
    assert [r["mode"] for r in store.rows("SELECT mode FROM inbox")] == ["notice"]
    with api_client(store, monkeypatch) as client:
        assert client.get("/api/chats/chat").json()["pending"] == []  # not a message waiting to go out
    store.execute("UPDATE runs SET status='completed' WHERE id='run'")
    assert store.settle("run") is None
    assert store.rows("SELECT * FROM inbox") == [] and len(store.rows("SELECT id FROM runs")) == 1
    # The user's own waiting message still goes out, alone.
    make_state(store)
    notifications.keep(store, "device-1", jordan("and bring the charger"))
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        delivery.deliver(db, "chat", "Then book a cab", "deepseek", "next")
    store.execute("UPDATE runs SET status='completed' WHERE id='run'")
    assert store.settle("run")["request"] == "Then book a cab"


async def test_memory_never_learns_from_a_notification(store):
    state = messaged_jordan(store, status="completed")
    store.execute("UPDATE memory_scans SET scanned=0")
    notifications.keep(store, "device-1", jordan("Remember that my new number is 555-0100"))
    model = Script(Memory=lambda c, n: {"actions": [{"tool": "remember"}]})
    await learn(runner_for(store, model), state)
    [seen] = model.of("Memory")
    assert seen["context"]["new_user_messages"] == ["Tell Jordan I will be 10 minutes late"]


async def test_look_up_reads_the_log_as_leads_and_says_how_far_back_it_reaches(store):
    store.profile_set(notifications.SINCE, time.time() - 5 * 86400)
    notifications.keep(store, "device-1", {"type": "notification", "app": "com.microsoft.teams", "label": "Teams",
                                           "title": "Sam Rivera", "text": "can you send the budget deck for Atlas?", "key": "t1"})
    notifications.keep(store, "device-1", {"type": "notification", "app": "com.google.android.gm", "label": "Gmail",
                                           "title": "Sam Rivera", "text": "Atlas budget deck", "key": "g1"})
    found = notifications.search(store, "atlas budget")
    lines = found.splitlines()
    assert len(lines) == 2 and lines[0].startswith("Notification Teams · Sam Rivera · ")
    assert lines[0].endswith("can you send the budget deck for Atlas?")
    assert lines[1].startswith('Notifications "atlas budget" · 1 · kept since ') and "in its app" in lines[1]
    # Every word must be there; Gmail's are left to Gmail's own search.
    assert notifications.search(store, "atlas roadmap") == "" and "Gmail" not in notifications.search(store, "Sam Rivera")

    state = make_state(store, request="Should we send John the Atlas doc?")

    def orchestrator(context, n):
        if n == 0:
            return orch({"tool": "look_up", "queries": ["Atlas", "fridge"]})
        atlas, fridge = context["looked_up"][0]
        assert atlas["notifications"].startswith("Notification Teams · Sam Rivera") and "notifications" not in fridge
        return orch(finish("Sam Rivera asked for it in Teams."))

    await runner_for(store, Script(Orchestrator=orchestrator)).run(state)
    assert state["status"] == "completed"
    # What was passed to this chat is in its conversation already, so its look_up leaves it out.
    messaged_jordan(store)
    notifications.keep(store, "device-1", jordan("the Atlas thing can wait"))
    assert "Jordan" in notifications.search(store, "atlas wait") and notifications.search(store, "atlas wait", exclude_chat="chat") == ""


def test_the_log_forgets_old_notifications_and_a_deleted_chat_keeps_none(store, monkeypatch):
    messaged_jordan(store, status="completed")
    store.execute("UPDATE runs SET status='completed' WHERE id='run'")
    notifications.keep(store, "device-1", jordan())
    notifications.keep(store, "device-1", {**jordan("long ago"), "key": "old"}, now=time.time() - (notifications.KEEP_DAYS + 1) * 86400)
    notifications.prune(store)
    assert [n["text"] for n in logged(store)] == ["ok, no worries"]
    with api_client(store, monkeypatch) as client:
        assert client.delete("/api/chats/chat").status_code == 200
    assert store.rows("SELECT * FROM chat_apps") == [] and logged(store)[0]["chat_id"] is None
    assert notifications.keep(store, "device-1", jordan("hello?"))["chat_id"] is None


def test_the_gateway_reads_new_notifications_without_a_lease_and_names_their_apps(monkeypatch):
    from app import gateway
    from app.bridge.opscatalog import NOTIFIER
    from app.gateway import core, sleep
    from fastapi.testclient import TestClient

    class Phone:
        async def call(self, cmd, process=None, **args):
            assert process == NOTIFIER
            if cmd == "events":
                return {"ok": True, "seq": 7, "events": [
                    {"type": "notification", "app": "com.whatsapp", "title": "Home", "text": "milk", "seq": 6},
                    {"type": "notification_removed", "app": "com.whatsapp", "seq": 7}]}
            return {"ok": True, "notifications": [{"app": "com.whatsapp", "label": "WhatsApp"}]}

    monkeypatch.setattr(core, "slots", {"device-1": "emulator-5554"})
    monkeypatch.setattr(core, "phones", {"device-1": Phone()})
    monkeypatch.setattr(core, "redactions", {"device-1": set()})
    monkeypatch.setattr(gateway.settings, "gateway_token", "")
    # Read every few seconds: it must neither wake the phone nor start its countdown to sleep again.
    used = []
    monkeypatch.setattr(sleep, "idle", lambda slot, delay=None: used.append(("idle", slot)))

    async def wake(slot):
        used.append(("wake", slot))

    monkeypatch.setattr(sleep, "wake", wake)
    found = TestClient(gateway.app).get("/devices/device-1/notifications/events", params={"since": 5}).json()
    assert found == {"seq": 7, "events": [
        {"type": "notification", "app": "com.whatsapp", "title": "Home", "text": "milk", "seq": 6, "label": "WhatsApp"}]}
    assert used == []
