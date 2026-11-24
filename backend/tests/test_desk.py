"""The reception desk (runtime/desk.py, runtime/deskstate.py, api/desk.py): before each reply it reads penrose as
it is now (the state), its tools route the user's words to the sessions, and the sessions' news reaches the phone
as events."""

import asyncio
import base64
import json
import time

import httpx
import pytest
from app.api import common, desk as desk_api, voice
from app.config import settings
from app.plan import new_step
from app.runtime import desk, deskstate
from app.store import uid
from fakes import step_spec
from fastapi.testclient import TestClient


def put_chat(store, chat_id, title, created=1.0):
    store.execute("INSERT INTO chats VALUES (?,?,?)", (chat_id, title, created))


def put_run(store, chat_id, status, **extra):
    state = {"id": uid(), "chat_id": chat_id, "request": "test", "status": status, "created": time.time(),
             "plan": [], "nodes": {}, "questions": [], **extra}
    store.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?)",
                  (state["id"], chat_id, status, json.dumps(state), time.time()))
    return state


def waiting_for_code(store, chat_id):
    step = {**new_step(step_spec("s1")), "status": "waiting_user",
            "question": {"question": "Enter the code Target sent", "sensitive": True}}
    return put_run(store, chat_id, "waiting_user", plan=[step],
                   questions=[{"node_id": "s1", "question": "Enter the code Target sent", "sensitive": True}])


@pytest.fixture
def desk_on(monkeypatch):
    monkeypatch.setattr(settings, "voice_desk", True)
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")


@pytest.fixture
def client(store, monkeypatch, desk_on):
    monkeypatch.setattr(common, "store", store)
    from app.api import app

    with TestClient(app) as c:
        yield c


# --- the state and the memory --------------------------------------------------------------------------------------


def errand(text, session):
    """The lines of one errand in the state block."""
    lines, mine = [], False
    for line in text.splitlines():
        if line[:1].isdigit():
            mine = f"s:{session} " in line
        elif not line.startswith("   "):
            mine = False
        if mine:
            lines.append(line)
    return "\n".join(lines)


def test_the_state_lists_the_errands_numbered_with_ids_statuses_questions_and_rewind_points(store):
    put_chat(store, "aaaaaa1111", "DoorDash dinner", 10.0)
    put_chat(store, "bbbbbb2222", "Flight to Boston", 20.0)
    put_run(store, "aaaaaa1111", "running")  # a message belongs to the run that had begun when it was written
    store.message("aaaaaa1111", "user", "order me a pizza from doordash", message_id="cccccc3333")
    waiting_for_code(store, "bbbbbb2222")
    text = deskstate.state(store)["text"]
    assert text.startswith("[penrose now] ")
    assert "Last acted on: nothing in the last hour." in text
    flight, dinner = errand(text, "bbbbbb"), errand(text, "aaaaaa")
    assert flight.splitlines()[0][3:] == 's:bbbbbb "Flight to Boston"' and "status: waiting for the user" in flight
    assert ('asks (a few seconds ago): "Enter the code Target sent" (private: the user answers in a form on their '
            'screen, never aloud)') in flight
    assert "status: working" in dinner
    assert 'rewind points: m:cccccc "order me a pizza from doordash"' in dinner
    assert 'user, ' in dinner and '"order me a pizza from doordash"' in dinner
    assert text.rstrip().endswith("2 running or waiting on the user.")


def test_a_deleted_errand_is_not_in_the_state_whatever_the_desk_said_about_it(store):
    put_chat(store, "aaaaaa1111", "Pad thai")
    put_run(store, "aaaaaa1111", "running")
    desk.write(store, "user", {"text": "Order the usual pad thai"})
    desk.write(store, "call", {"call_id": "c1", "name": "start_errand", "arguments": "{}"})
    desk.write(store, "output", {"call_id": "c1", "output": '{"session": "s:aaaaaa", "status": "starting"}'})
    desk.write(store, "assistant", {"text": "Ordering your usual pad thai now."})
    assert "Last acted on: s:aaaaaa (started it" in deskstate.state(store)["text"]
    store.execute("DELETE FROM runs WHERE chat_id='aaaaaa1111'")
    store.execute("DELETE FROM chats WHERE id='aaaaaa1111'")  # the user deleted the chat
    now = deskstate.state(store)
    assert "aaaaaa" not in now["text"] and now["about"] == "" and "No errands yet." in now["text"]
    assert "Nothing is running or waiting." in now["text"]
    # And the session that connects gets the words said, never the old call or its result.
    assert [i["type"] for i in desk.items(store)] == ["message", "message"]


def test_the_state_says_what_the_desk_last_acted_on_as_a_hint_or_what_the_user_opened(store):
    put_chat(store, "aaaaaa1111", "One", 10.0)
    put_chat(store, "bbbbbb2222", "Two", 20.0)
    desk.write(store, "call", {"call_id": "c1", "name": "pass_on", "arguments": "{}"})
    desk.write(store, "output", {"call_id": "c1", "output": '{"session": "s:aaaaaa", "did": "steered"}'})
    desk.write(store, "call", {"call_id": "c2", "name": "stop_errand", "arguments": "{}"})
    desk.write(store, "output", {"call_id": "c2", "output": '{"error": "It is not working right now."}'})  # did nothing
    now = deskstate.state(store)
    assert "Last acted on: s:aaaaaa (passed the user's words on to it" in now["text"] and now["about"] == "aaaaaa1111"
    assert "A hint only: what the user says next may be about any errand, or a new one." in now["text"]
    opened = deskstate.state(store, about="bbbbbb2222", opened=True, at=time.time())
    assert "The user opened s:bbbbbb on their screen" in opened["text"] and opened["about"] == "bbbbbb2222"
    assert deskstate.state(store, about="zzzzzz9999")["about"] == "aaaaaa1111"  # an errand that is gone: back to the hint


def test_a_working_errand_shows_what_it_does_what_is_on_the_phone_and_its_notes(store):
    put_chat(store, "aaaaaa1111", "Pad thai")
    step = {**new_step(step_spec("order")), "status": "running", "app": {"name": "DoorDash", "package": "com.dd.doordash"},
            "sessions": {"subagent": {
                "progress": "[now] Add 1 x Pad Thai, large\n[next] Checkout",
                "log": ["T+00:06 @ app.relay.home HomeActivity | (runtime) open_app(com.dd.doordash) -> opened",
                        "T+00:34 @ com.dd.doordash CartActivity | Cart open: 1 x Pad Thai [48] (Large), total 21.40"]}}}
    put_run(store, "aaaaaa1111", "running", plan=[step],
            notes=["Request: the usual pad thai.", "DoorDash usual is Thai Basil Kitchen.", "Large costs 16.50.", "Next: place it."])
    mine = errand(deskstate.state(store)["text"], "aaaaaa")
    assert "doing: Add 1 x Pad Thai, large" in mine
    assert "on the phone now (DoorDash): Cart open: 1 x Pad Thai (Large), total 21.40" in mine  # no element numbers
    assert "(runtime)" not in mine and "com.dd." not in mine
    notes = [line.strip() for line in mine.splitlines() if line.startswith("     - ") and "user," not in line]
    assert notes == ["- DoorDash usual is Thai Basil Kitchen.", "- Large costs 16.50.", "- Next: place it."]  # the last three


def test_a_stopped_errand_never_reads_as_doing_something_now_and_a_quiet_one_reads_as_stalled(store):
    put_chat(store, "aaaaaa1111", "Stopped one", 10.0)
    put_chat(store, "bbbbbb2222", "Quiet one", 20.0)
    step = {**new_step(step_spec("s1")), "status": "running", "sessions": {"subagent": {"log": ["T+00:01 | Reading the cart"]}}}
    put_run(store, "aaaaaa1111", "stopped", plan=[step])
    put_run(store, "bbbbbb2222", "running", plan=[step])
    store.execute("UPDATE runs SET updated=? WHERE chat_id='bbbbbb2222'", (time.time() - 600,))
    text = deskstate.state(store)["text"]
    stopped, quiet = errand(text, "aaaaaa"), errand(text, "bbbbbb")
    assert "status: stopped" in stopped and "doing:" not in stopped and "on the phone" not in stopped
    assert "status: stalled (working, but nothing has happened for 10 min)" in quiet


def test_a_finished_errand_shows_its_result_and_what_the_user_has_not_been_told(store):
    put_chat(store, "aaaaaa1111", "DoorDash dinner", 10.0)
    put_chat(store, "aaaaab2222", "Flights", 20.0)
    put_run(store, "aaaaaa1111", "completed")
    store.message("aaaaaa1111", "assistant", "**Ordered.** A long written reply.", source="Orchestrator", spoken="Your pizza is ordered.")
    desk.write(store, "event", {"chat_id": "aaaaaa1111", "title": "DoorDash dinner", "kind": "finished", "text": "Your pizza is ordered."},
               announced=0)
    now = deskstate.state(store, about="aaaaaa1111")
    dinner = errand(now["text"], "aaaaaa")
    assert "status: finished a few seconds ago" in dinner and "not told yet: the result" in dinner
    assert 'result (a few seconds ago): "Your pizza is ordered."' in dinner
    assert "not told yet" not in errand(now["text"], "aaaaab")
    assert now["due"] == [desk.latest_event_id(store)]
    desk.announced(store, ids=now["due"])
    assert "not told yet" not in deskstate.state(store, about="aaaaaa1111")["text"]


def test_a_short_id_names_one_recent_session_or_nothing(store):
    put_chat(store, "aaaaaa1111", "One")
    put_chat(store, "aaaaab2222", "Two")
    assert desk.resolve(store, "s:aaaaaa") == "aaaaaa1111"
    assert desk.resolve(store, "AAAAAB") == "aaaaab2222"
    assert desk.resolve(store, "s:aaaaa") is None  # two match
    assert desk.resolve(store, "s:zzzzzz") is None and desk.resolve(store, "") is None


def test_a_connecting_session_gets_only_the_last_lines_said(store):
    put_chat(store, "aaaaaa1111", "DoorDash dinner")
    desk.write(store, "user", {"text": "order a pizza"})
    desk.write(store, "call", {"call_id": "c1", "name": "start_errand", "arguments": '{"request": "order a pizza"}'})
    desk.write(store, "output", {"call_id": "c1", "output": '{"session": "s:aaaaaa"}'})
    desk.write(store, "assistant", {"text": "On it."})
    desk.write(store, "event", {"chat_id": "aaaaaa1111", "title": "DoorDash dinner", "status": "completed",
                                "text": "Your pizza is ordered.", "private": False}, announced=0)
    items = desk.items(store)
    # No state, call, result or event: the state is added before each reply, and an event is a card until it is told.
    assert [(i["type"], i["role"], i["content"][0]["type"], i["content"][0]["text"]) for i in items] == [
        ("message", "user", "input_text", "order a pizza"), ("message", "assistant", "output_text", "On it.")]
    assert [e["payload"]["text"] for e in desk.unannounced(store)] == ["Your pizza is ordered."]
    # What was said a while ago is not remembered: the state says where things are.
    assert desk.items(store, now=time.time() + desk.DIALOGUE_SECONDS + 1) == []


def test_news_is_heard_per_session_when_the_user_opens_or_talks_about_it(store):
    for chat, text in (("aaaaaa1111", "One."), ("aaaaab2222", "Two.")):
        put_chat(store, chat, text)
        desk.write(store, "event", {"chat_id": chat, "title": text, "kind": "finished", "text": text}, announced=0)
    desk.announced(store, chat_id="aaaaaa1111")  # the user opened its card, or said "later" to it
    assert [e["payload"]["chat_id"] for e in desk.unannounced(store)] == ["aaaaab2222"]
    asyncio.run(desk.perform(store, "read_reply", json.dumps({"session": "s:aaaaab"})))  # and talking about the other
    assert desk.unannounced(store) == []


def test_the_memory_keeps_only_the_last_turns(store):
    for i in range(30):
        desk.write(store, "user", {"text": f"turn {i}"})
    first = 30 - desk.DIALOGUE_LINES
    assert [i["content"][0]["text"] for i in desk.items(store)] == [f"turn {i}" for i in range(first, 30)]


def test_the_log_prunes_itself_to_the_newest_entries_but_keeps_untold_news(store, monkeypatch):
    monkeypatch.setattr(desk, "KEEP", 10)
    desk.write(store, "event", {"chat_id": "aaaaaa1111", "title": "t", "status": "completed", "text": "untold"}, announced=0)
    for i in range(30):
        desk.write(store, "user", {"text": f"turn {i}"})
    kept = desk.entries(store, limit=100)
    assert kept[0]["kind"] == "event" and [e["payload"]["text"] for e in kept[1:]] == [f"turn {i}" for i in range(20, 30)]
    desk.announced(store, upto=desk.latest_event_id(store))
    desk.write(store, "user", {"text": "turn 30"})
    assert [e["payload"]["text"] for e in desk.entries(store, limit=100)] == [f"turn {i}" for i in range(21, 31)]
    assert len(desk.events_after(store, 0)) == 0


def test_the_log_drops_old_entries_even_when_short(store):
    with store.connect() as db:
        db.execute("INSERT INTO desk_log(kind,payload,created,announced) VALUES ('user',?,?,1)",
                   (json.dumps({"text": "last week"}), time.time() - 8 * 86400))
        db.execute("INSERT INTO desk_log(kind,payload,created,announced) VALUES ('event',?,?,0)",
                   (json.dumps({"chat_id": "aaaaaa1111", "title": "t", "status": "completed", "text": "old news"}), time.time() - 8 * 86400))
    desk.write(store, "user", {"text": "today"})
    assert [e["payload"]["text"] for e in desk.entries(store)] == ["old news", "today"]


def test_deleting_a_chat_takes_its_events_out_of_the_log(client, store):
    put_chat(store, "aaaaaa1111", "DoorDash dinner")
    put_chat(store, "aaaaab2222", "Flights")
    for chat_id in ("aaaaaa1111", "aaaaab2222"):
        desk.write(store, "event", {"chat_id": chat_id, "title": "t", "status": "completed", "text": "done"})
    desk.write(store, "user", {"text": "thanks"})
    assert client.delete("/api/chats/aaaaaa1111").status_code == 200
    assert [e["payload"]["chat_id"] for e in desk.events_after(store, 0)] == ["aaaaab2222"]
    assert [e["kind"] for e in desk.entries(store)] == ["event", "user"]


# --- the tools --------------------------------------------------------------------------------------------------


async def test_start_opens_a_session_and_tell_steers_or_continues_it(store):
    out = await desk.perform(store, "start_errand", json.dumps({"request": "order a pizza from DoorDash"}))
    chat_id = desk.resolve(store, out["session"])
    # A run that has only begun reads as "starting", with a word that nothing is done yet: the desk once read
    # "queued" as an outcome and told the user it was done.
    assert out["started"] and out["status"] == "starting" and "Never say it is done" in out["note"] and chat_id
    saved = store.rows("SELECT source, content FROM messages WHERE chat_id=?", (chat_id,))[0]
    assert saved == {"source": "voice", "content": "order a pizza from DoorDash"}
    assert store.rows("SELECT state FROM runs WHERE chat_id=?", (chat_id,)) and json.loads(
        store.rows("SELECT state FROM runs WHERE chat_id=?", (chat_id,))[0]["state"])["channel"] == "voice"

    told = await desk.perform(store, "pass_on", json.dumps({"session": out["session"], "message": "make it two"}))
    assert told["did"] == "steered"  # the task is queued: the words wait in its inbox
    assert store.rows("SELECT content FROM inbox")[0]["content"] == "make it two"


async def test_a_reply_that_only_tells_news_cannot_act(store):
    """The desk once passed a task's own progress back to it as the user's words: a call from a reply that only
    tells news may read and search, never start, steer, stop or rewind."""
    put_chat(store, "aaaaaa1111", "Sofa cleaning")
    put_run(store, "aaaaaa1111", "running", channel="voice")
    for name, args in (("pass_on", {"session": "s:aaaaaa", "message": "I'm going through search instead."}),
                       ("start_errand", {"request": "book it"}), ("stop_errand", {"session": "s:aaaaaa"})):
        out = await desk.perform(store, name, json.dumps(args), for_user=False)
        assert "never something to pass on" in out["error"]
    assert not store.rows("SELECT 1 FROM inbox")
    assert store.rows("SELECT status FROM runs WHERE chat_id='aaaaaa1111'")[0]["status"] == "running"
    assert "sessions" in await desk.perform(store, "find_errand", json.dumps({"query": "sofa"}), for_user=False)
    # The user's own words (or a page that does not say) still steer.
    told = await desk.perform(store, "pass_on", json.dumps({"session": "s:aaaaaa", "message": "make it two"}), for_user=True)
    assert told["did"] == "steered"


async def test_tell_sends_a_spoken_code_privately_to_the_waiting_step(store, monkeypatch):
    put_chat(store, "aaaaaa1111", "Target sign-in")
    state = waiting_for_code(store, "aaaaaa1111")
    monkeypatch.setattr(common, "store", store)
    out = await desk.perform(store, "pass_on", json.dumps({"session": "s:aaaaaa", "message": "two seven nine three"}))
    assert out["did"] == "sent the code privately"
    assert store.rows("SELECT value FROM secrets WHERE run_id=?", (state["id"],))[0]["value"] == "2793"
    assert store.rows("SELECT content FROM messages WHERE chat_id='aaaaaa1111'")[0]["content"] == "[Private code provided]"


async def test_stop_resume_and_an_unknown_session(store, monkeypatch):
    monkeypatch.setattr(common, "store", store)
    put_chat(store, "aaaaaa1111", "DoorDash dinner")
    put_run(store, "aaaaaa1111", "running", route={"orchestrate": True}, context="", facts={}, journal={})
    assert (await desk.perform(store, "stop_errand", json.dumps({"session": "s:aaaaaa"})))["did"] == "stopped"
    assert store.rows("SELECT status FROM runs WHERE chat_id='aaaaaa1111'")[0]["status"] == "stopped"
    assert "not working" in (await desk.perform(store, "stop_errand", json.dumps({"session": "s:aaaaaa"})))["error"]
    assert (await desk.perform(store, "resume_errand", json.dumps({"session": "s:aaaaaa"})))["did"] == "resumed"
    assert store.rows("SELECT status FROM runs WHERE chat_id='aaaaaa1111'")[0]["status"] == "queued"
    gone = (await desk.perform(store, "stop_errand", json.dumps({"session": "s:zzzzzz"})))["error"]
    assert gone.startswith("No errand has that id") and "nothing to act on" in gone  # final: nothing to go looking for
    assert "No errand" in (await desk.perform(store, "pass_on", json.dumps({"session": "s:zzzzzz", "message": "hi"})))["error"]
    assert "No tool" in (await desk.perform(store, "dance", "{}"))["error"]
    assert (await desk.perform(store, "pass_on", "not json"))["error"] == "Bad arguments."


async def test_rewind_goes_back_to_a_point_in_the_state(store, monkeypatch):
    monkeypatch.setattr(common, "store", store)
    out = await desk.perform(store, "start_errand", json.dumps({"request": "order a pizza"}))
    chat_id = desk.resolve(store, out["session"])
    state = store.load(store.rows("SELECT id FROM runs WHERE chat_id=?", (chat_id,))[0]["id"])
    state["status"] = "completed"  # the task ended: its first message can be rewritten
    store.execute("UPDATE runs SET status=?, state=? WHERE id=?", ("completed", json.dumps(state), state["id"]))
    store.execute("UPDATE jobs SET status='done' WHERE run_id=?", (state["id"],))

    async def release(owners):
        pass

    monkeypatch.setattr(common, "release", release)  # no gateway here
    point = store.rows("SELECT id FROM messages WHERE chat_id=?", (chat_id,))[0]["id"]
    assert f"m:{point[:6]}" in deskstate.state(store)["text"]
    back = await desk.perform(store, "rewind_errand", json.dumps({"session": out["session"], "to": f"m:{point[:6]}", "message": "order sushi instead"}))
    assert back["did"].startswith("rewound")
    assert [m["content"] for m in store.rows("SELECT content FROM messages WHERE chat_id=?", (chat_id,))] == ["order sushi instead"]
    assert "No rewind point" in (await desk.perform(store, "rewind_errand", json.dumps({"session": out["session"], "to": "m:zzzzzz", "message": "x"})))["error"]


async def test_read_reply_and_find(store):
    put_chat(store, "aaaaaa1111", "DoorDash dinner")
    store.message("aaaaaa1111", "user", "order a pizza from DoorDash")
    store.message("aaaaaa1111", "assistant", "Ordered from **Tony's**: 2 pizzas, $31.", source="Orchestrator")
    put_run(store, "aaaaaa1111", "completed")
    more = await desk.perform(store, "read_reply", json.dumps({"session": "s:aaaaaa"}))
    assert more["reply"] == "Ordered from Tony's: 2 pizzas, $31."
    found = await desk.perform(store, "find_errand", json.dumps({"query": "pizza"}))
    assert found["sessions"][0]["session"] == "s:aaaaaa" and found["sessions"][0]["title"] == "DoorDash dinner"


# --- events ---------------------------------------------------------------------------------------------------


def runner_said(store, run, content, spoken=None):
    message = store.message(run["chat_id"], "assistant", content, source="Orchestrator", spoken=spoken)
    store.event(run["id"], "agent_message", {"message": message})


def test_a_message_held_for_the_question_that_follows_makes_one_asks_event(store):
    put_chat(store, "bbbbbb2222", "Work sign-in")
    run = put_run(store, "bbbbbb2222", "running", channel="voice")
    runner_said(store, run, "Microsoft is asking for a **six-digit code** to sign in.",
                spoken="Microsoft wants a six-digit code from your Authenticator app.")
    store.event(run["id"], "questions", {"questions": [{"node_id": "s1", "question": "The code", "sensitive": True}]})
    held = {}
    cursor, logged = desk.report(store, 0, held, now=100.0)
    assert logged and held == {}
    events = desk.unannounced(store)
    assert [e["payload"] for e in events] == [{"chat_id": "bbbbbb2222", "title": "Work sign-in", "kind": "asks",
                                              "text": "Microsoft wants a six-digit code from your Authenticator app.",
                                              "private": True, "ask": {"kind": "code", "label": "The code"}}]
    assert desk.event_text(events[0]["payload"]).startswith('[event] s:bbbbbb "Work sign-in" asks: Microsoft wants')
    assert cursor == 2


def test_a_question_that_asks_for_a_card_carries_its_form(store):
    """The orchestrator asks for a card privately: the voice screen opens the card form as the desk tells it."""
    put_chat(store, "dddddd4444", "Reorder Bella's food")
    run = put_run(store, "dddddd4444", "running", channel="voice")
    runner_said(store, run, "Hulu needs a card.", spoken="Hulu needs a card. Fill it in on your screen.")
    store.event(run["id"], "questions", {"questions": [], "private": {"kind": "card", "label": "Card for Hulu"}})
    desk.report(store, 0, {}, now=100.0)
    got = desk.unannounced(store)[0]["payload"]
    assert got["kind"] == "asks" and got["private"] and got["ask"] == {"kind": "card", "label": "Card for Hulu"}
    assert "on the user's screen" in desk.event_text(got)


def test_a_question_asked_again_without_a_message_says_the_question(store):
    """A code sent privately and rejected: the step asks again, the orchestrator writes nothing new."""
    put_chat(store, "bbbbbb2222", "Work sign-in")
    run = put_run(store, "bbbbbb2222", "waiting_user", channel="voice")
    store.event(run["id"], "questions", {"questions": [
        {"node_id": "s1", "question": "A new code: the previous one was *rejected*.", "sensitive": True}]})
    desk.report(store, 0, {}, now=100.0)
    got = desk.unannounced(store)[0]["payload"]
    assert got["kind"] == "asks" and got["private"] and got["text"] == "A new code: the previous one was rejected."


def test_a_message_alone_becomes_progress_for_a_voice_conversation_only_after_the_grace(store):
    put_chat(store, "aaaaaa1111", "DoorDash dinner")
    put_chat(store, "cccccc3333", "Typed task")
    spoken = put_run(store, "aaaaaa1111", "running", channel="voice")
    typed = put_run(store, "cccccc3333", "running")
    runner_said(store, spoken, "Found your address; ordering now.", spoken="Found your address, ordering now.")
    runner_said(store, typed, "Opening DoorDash.")
    held = {}
    cursor, logged = desk.report(store, 0, held, now=100.0)
    assert not logged and set(held) == {spoken["id"], typed["id"]} and desk.entries(store) == []
    # A second line within the grace replaces the held one: only the latest is told.
    runner_said(store, spoken, "Paying.", spoken="Paying now.")
    cursor, logged = desk.report(store, cursor, held, now=100.5)
    assert not logged and held[spoken["id"]][1]["text"] == "Paying now."
    cursor, logged = desk.report(store, cursor, held, now=101.5)
    assert logged and held == {}
    entries = desk.entries(store)
    assert [e["payload"]["kind"] for e in entries] == ["progress"] and entries[0]["announced"] == 1
    assert desk.unannounced(store) == []  # progress never rings
    assert desk.event_text(entries[0]["payload"]) == '[event] s:aaaaaa "DoorDash dinner" working: Paying now.'


def test_a_result_a_failure_and_a_skipped_task(store):
    put_chat(store, "aaaaaa1111", "DoorDash dinner")
    put_chat(store, "cccccc3333", "Reservation")
    put_chat(store, "dddddd4444", "A notification")
    done = put_run(store, "aaaaaa1111", "completed")
    runner_said(store, done, "Ordered from **Tony's**: 2 pizzas, $31.", spoken="Two pizzas from Tony's, about thirty-one dollars.")
    store.event(done["id"], "completed", {"reply": "Ordered from **Tony's**: 2 pizzas, $31.", "spoken": ""})
    failed = put_run(store, "cccccc3333", "failed")
    store.event(failed["id"], "error", {"message": "The app crashed."})
    skipped = put_run(store, "dddddd4444", "completed")
    store.event(skipped["id"], "completed", {"reply": ""})
    desk.report(store, 0, {}, now=100.0)
    got = [e["payload"] for e in desk.unannounced(store)]
    assert [(g["kind"], g["text"]) for g in got] == [
        ("finished", "Two pizzas from Tony's, about thirty-one dollars."), ("failed", "The app crashed.")]
    # A reply without a spoken form or a message before it is read as plain text.
    store.event(done["id"], "completed", {"reply": "See [the receipt](https://x.example)."})
    desk.report(store, 3, {}, now=100.0)
    assert desk.unannounced(store)[-1]["payload"]["text"] == "See the receipt."


async def test_watching_logs_events_as_the_runner_writes_them(store):
    put_chat(store, "aaaaaa1111", "DoorDash dinner")
    run = put_run(store, "aaaaaa1111", "running", channel="voice")
    store.event(run["id"], "stage_started", {})  # before the watch began: not reported
    told, rung = [], []

    def listen():
        event = asyncio.Event()
        rung.append(event)
        return event

    task = asyncio.create_task(desk.watch(store, lambda: told.append(1), listen))
    await asyncio.sleep(0.05)
    store.event(run["id"], "completed", {"reply": "Done.", "spoken": "Your pizza is on its way."})
    rung[-1].set()
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    events = desk.unannounced(store)
    assert len(events) == 1 and told == [1]
    assert events[0]["payload"] == {"chat_id": "aaaaaa1111", "title": "DoorDash dinner", "kind": "finished",
                                    "text": "Your pizza is on its way.", "private": False}


# --- the API --------------------------------------------------------------------------------------------------


def test_open_mints_a_realtime_secret_and_hands_over_the_desks_memory(client, store, monkeypatch):
    put_chat(store, "aaaaaa1111", "DoorDash dinner")
    desk.write(store, "event", {"chat_id": "aaaaaa1111", "title": "DoorDash dinner", "status": "completed",
                                "text": "Done.", "private": False}, announced=0)
    asked = {}

    def handler(request):
        asked["url"] = str(request.url)
        asked["body"] = json.loads(request.content)
        return httpx.Response(200, json={"value": "ek_abc", "expires_at": 1700000000, "session": {}})

    real = httpx.AsyncClient
    monkeypatch.setattr(desk_api.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    out = client.post("/api/voice/desk/open").json()
    assert out["secret"] == "ek_abc" and out["url"] == f"wss://api.openai.com/v1/realtime?model={settings.voice_desk_model}"
    session = asked["body"]["session"]
    assert session["type"] == "realtime" and session["model"] == settings.voice_desk_model
    assert [t["name"] for t in session["tools"]] == ["start_errand", "pass_on", "stop_errand", "resume_errand", "rewind_errand", "read_reply", "find_errand"]
    assert session["instructions"].startswith("You are penrose")
    assert session["audio"]["input"]["turn_detection"]["interrupt_response"] is True
    assert out["latest_event"] == 1 and "announce" not in out  # opening is silent
    assert out["items"] == []  # nothing said lately; the event is a card (`unheard`), not something to read out
    now = client.post("/api/voice/desk/state", json={"about": "aaaaaa1111"}).json()
    assert now["text"].startswith("[penrose now]") and now["about"] == "aaaaaa1111" and now["due"] == [1]
    assert "not told yet: the result" in now["text"]
    assert [(e["session"], e["text"]) for e in out["unheard"]] == [("s:aaaaaa", '[event] s:aaaaaa "DoorDash dinner" finished: Done.')]
    assert out["unheard"][0]["news"].startswith('[news] s:aaaaaa "DoorDash dinner" is finished')


def test_a_tool_call_is_run_logged_and_answered(client, store):
    out = client.post("/api/voice/desk/tool", json={
        "call_id": "c1", "name": "start_errand", "arguments": json.dumps({"request": "order a pizza"}), "provider": "deepseek",
    }).json()
    result = json.loads(out["output"])
    assert result["started"] and out["focus"]["chat_id"] == desk.resolve(store, result["session"])
    assert [r["kind"] for r in desk.entries(store)] == ["call", "output"]
    client.post("/api/voice/desk/log", json={"role": "user", "text": "order a pizza"})
    client.post("/api/voice/desk/log", json={"role": "assistant", "text": "Ordering a pizza."})
    assert [r["kind"] for r in desk.entries(store)] == ["call", "output", "user", "assistant"]


def test_events_are_fetched_since_a_cursor_or_unannounced_and_marked_told(client, store):
    put_chat(store, "aaaaaa1111", "DoorDash dinner")
    for text in ("One.", "Two."):
        desk.write(store, "event", {"chat_id": "aaaaaa1111", "title": "DoorDash dinner", "status": "completed",
                                    "text": text, "private": False}, announced=0)
    got = client.get("/api/voice/desk/events?after=1").json()
    assert [e["id"] for e in got["events"]] == [2] and got["latest"] == 2
    assert got["events"][0]["text"] == '[event] s:aaaaaa "DoorDash dinner" finished: Two.'
    assert [e["id"] for e in client.get("/api/voice/desk/events?unannounced=1").json()["events"]] == [1, 2]
    client.post("/api/voice/desk/announced", json={"ids": [1]})
    assert [e["id"] for e in client.get("/api/voice/desk/events?unannounced=1").json()["events"]] == [2]
    client.post("/api/voice/desk/announced", json={"chat_id": "aaaaaa1111"})  # its card dismissed
    assert client.get("/api/voice/desk/events?unannounced=1").json()["events"] == []


def test_the_desk_is_off_unless_switched_on(client, monkeypatch):
    monkeypatch.setattr(settings, "voice_desk", False)
    assert client.post("/api/voice/desk/open").status_code == 404
    assert client.get("/api/voice/desk/events").status_code == 404


def test_the_desk_is_on_by_default_once_an_openai_key_is_set(monkeypatch):
    from app.api import updates
    from app.config import Settings

    assert Settings.model_fields["voice_desk"].default is True
    monkeypatch.setattr(settings, "voice_desk", True)
    monkeypatch.setattr(settings, "openai_api_key", "")
    assert updates.health()["voice_desk"] is False
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    assert updates.health()["voice_desk"] is True


def test_the_updates_stream_carries_the_desks_latest_event(store, monkeypatch, desk_on):
    from app.api import updates

    monkeypatch.setattr(common, "store", store)
    assert updates.snapshot()["desk"] == 0
    desk.write(store, "event", {"chat_id": "a", "title": "", "status": "completed", "text": "x", "private": False}, 0)
    assert updates.snapshot()["desk"] == 1


def test_the_phone_may_reach_the_desk_over_the_tunnel():
    from app.companion import tunnel

    assert tunnel.allowed("POST", "/api/voice/desk/open") and tunnel.allowed("GET", "/api/voice/desk/events")


def test_voice_needs_the_key_for_the_desk_too(client, monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "")
    assert client.post("/api/voice/desk/open").status_code == 503
    assert voice.require_key  # the same guard


def test_a_runner_event_written_while_the_api_runs_reaches_the_desks_events_route(client, store):
    """The whole wire: the store's write rings the API (`written`), the ring wakes `watch`, which logs the desk
    event the route then serves, with its kind."""
    put_chat(store, "aaaaaa1111", "DoorDash dinner")
    run = put_run(store, "aaaaaa1111", "completed", channel="voice")
    store.event(run["id"], "completed", {"reply": "Done.", "spoken": "Your pizza is on its way."})
    for _ in range(40):
        got = client.get("/api/voice/desk/events?unannounced=1").json()["events"]
        if got:
            break
        time.sleep(0.05)
    assert [(e["kind"], e["text"]) for e in got] == [
        ("finished", '[event] s:aaaaaa "DoorDash dinner" finished: Your pizza is on its way.')]


# --- pictures: what the voice screen shows while the desk speaks ------------------------------------------------

SHOE = "/api/runs/0123abcd/images/7"
RECEIPT = "/api/runs/0123abcd/images/8"


def test_what_was_written_says_how_long_ago_rounded_as_people_say_it(store):
    """A result that says "arriving in about 40 minutes" was read out four hours later as if just written."""
    assert [deskstate.ago(s) for s in (5, 70, 200, 3000, 4800, 9000, 4 * 3600 + 1300, 30 * 3600, 3 * 86400)] == [
        "a few seconds ago", "1 min ago", "3 min ago", "50 min ago", "1 h 20 min ago", "2 h 30 min ago", "4 h ago",
        "1 day ago", "3 days ago"]
    put_chat(store, "aaaaaa1111", "Order usual pad thai")
    put_run(store, "aaaaaa1111", "completed")
    store.message("aaaaaa1111", "user", "Order the usual pad thai")
    store.message("aaaaaa1111", "assistant", "Ordered, arriving in about 40 minutes.", source="Orchestrator")
    later = time.time() + 4 * 3600 + 600
    tea = errand(deskstate.state(store, now=later)["text"], "aaaaaa")
    assert "status: finished 4 h ago (" in tea
    assert 'result (4 h ago): "Ordered, arriving in about 40 minutes."' in tea
    assert '- user, 4 h ago: "Order the usual pad thai"' in tea


def test_news_with_pictures_does_not_make_the_state_say_there_are_no_errands(store):
    put_chat(store, "aaaaaa1111", "Running shoes")
    put_run(store, "aaaaaa1111", "completed")
    desk.write(store, "event", {"chat_id": "aaaaaa1111", "title": "Running shoes", "kind": "finished", "text": "Ordered.",
                                "pictures": [{"url": RECEIPT, "caption": "Order confirmation"}]}, announced=0)
    text = deskstate.state(store)["text"]
    assert "not told yet: the result [on screen:" in text and "No errands yet." not in text
    put_chat(store, "bbbbbb2222", "Flights", 5.0)  # and with an errand after it that has no news
    assert "No errands yet." not in deskstate.state(store)["text"]


def test_a_cut_reply_remembers_whether_it_was_a_tap_or_their_voice(client, store):
    client.post("/api/voice/desk/log", json={"role": "assistant", "text": "I found three sets."})
    client.post("/api/voice/desk/cut", json={"heard": "I found three", "by": "user", "how": "voice"})
    row = store.rows("SELECT payload FROM desk_log WHERE kind='assistant' ORDER BY id DESC LIMIT 1")[0]
    assert json.loads(row["payload"]) == {"text": "I found three sets.", "heard": "I found three", "cut": "user", "how": "voice"}


def test_a_reply_keeps_where_its_time_went(client, store):
    timing = {"total": 1840, "state": 12, "model": 910, "ahead": True, "round": 0}
    client.post("/api/voice/desk/log", json={"role": "assistant", "text": "Checking.", "timing": timing})
    row = store.rows("SELECT payload FROM desk_log WHERE kind='assistant' ORDER BY id DESC LIMIT 1")[0]
    assert json.loads(row["payload"]) == {"text": "Checking.", "timing": timing}


def test_a_result_carries_its_pictures_and_the_desk_reads_only_the_captions(store):
    put_chat(store, "aaaaaa1111", "Running shoes")
    run = put_run(store, "aaaaaa1111", "completed")
    reply = f"Ordered the **Pegasus 41**, $129.\n\n![Pegasus 41, $129]({SHOE})\n\n![Order confirmation]({RECEIPT})"
    store.event(run["id"], "completed", {"reply": reply, "spoken": "Ordered the Pegasus, a hundred twenty-nine dollars."})
    desk.report(store, 0, {}, now=100.0)
    got = desk.unannounced(store)[0]["payload"]
    assert got["pictures"] == [{"url": SHOE, "caption": "Pegasus 41, $129"}, {"url": RECEIPT, "caption": "Order confirmation"}]
    assert desk.event_text(got).endswith('[on screen: 2 pictures: "Pegasus 41, $129"; "Order confirmation"]')
    assert "Pegasus" not in desk.news_text(got)  # the headline carries neither the result nor its pictures


def test_a_question_shows_the_pictures_of_the_message_held_for_it(store):
    put_chat(store, "aaaaaa1111", "Running shoes")
    run = put_run(store, "aaaaaa1111", "running", channel="voice")
    runner_said(store, run, f"Which one?\n\n![Pegasus 41]({SHOE})", spoken="The Pegasus or the Vomero?")
    store.event(run["id"], "questions", {"questions": [{"node_id": "s1", "question": "Which one?"}]})
    desk.report(store, 0, {}, now=100.0)
    got = desk.unannounced(store)[0]["payload"]
    assert got["kind"] == "asks" and got["pictures"] == [{"url": SHOE, "caption": "Pegasus 41"}]


def test_an_event_without_pictures_says_nothing_about_the_screen(store):
    put_chat(store, "aaaaaa1111", "DoorDash dinner")
    run = put_run(store, "aaaaaa1111", "completed")
    store.event(run["id"], "completed", {"reply": "Done.", "spoken": "Done."})
    desk.report(store, 0, {}, now=100.0)
    got = desk.unannounced(store)[0]["payload"]
    assert "pictures" not in got and "on screen" not in desk.event_text(got)


def test_read_reply_hands_the_pictures_to_the_screen_and_captions_to_the_desk(client, store):
    put_chat(store, "aaaaaa1111", "Running shoes")
    store.message("aaaaaa1111", "assistant", f"Here it is.\n\n![Order confirmation]({RECEIPT})", source="Orchestrator")
    put_run(store, "aaaaaa1111", "completed")
    out = client.post("/api/voice/desk/tool", json={
        "call_id": "c1", "name": "read_reply", "arguments": json.dumps({"session": "s:aaaaaa"}), "provider": "deepseek",
    }).json()
    assert out["result"]["pictures"] == [{"url": RECEIPT, "caption": "Order confirmation"}]
    told = json.loads(out["output"])
    assert "pictures" not in told and told["on_screen"] == '[on screen: 1 picture: "Order confirmation"]'


async def test_a_session_gets_the_users_own_words_not_the_desks_retelling(store):
    desk.write(store, "output", {"call_id": "c0", "output": "{}"})  # the desk's last tool result
    desk.write(store, "user", {"text": "Log in to WhatsApp with 5125550198."})
    desk.write(store, "assistant", {"text": "Got it, setting that up."})  # this response's words, before its call
    out = await desk.perform(store, "start_errand", json.dumps({"request": "Log in to WhatsApp with 1 2 5 5 5 0 1 9 8"}))
    chat_id = desk.resolve(store, out["session"])
    assert [m["content"] for m in store.rows("SELECT content FROM messages WHERE chat_id=?", (chat_id,))] == [
        "Log in to WhatsApp with 5125550198."]

    # Nothing of theirs since the desk's last tool result: the desk's words go.
    desk.write(store, "output", {"call_id": "c1", "output": "{}"})
    told = await desk.perform(store, "pass_on", json.dumps({"session": out["session"], "message": "make it quick"}))
    assert told["did"] == "steered" and store.rows("SELECT content FROM inbox")[-1]["content"] == "make it quick"


async def test_a_sentence_started_over_counts_once_and_an_unlogged_turn_is_not_guessed(store):
    desk.write(store, "assistant", {"text": "Anything else?"})
    desk.write(store, "user", {"text": "Find us a good movie to watch on either Netflix."})
    desk.write(store, "user", {"text": "Find us a good movie to watch on either Netflix, Hulu or Prime Video."})
    assert desk.heard(store) == "Find us a good movie to watch on either Netflix, Hulu or Prime Video."
    # Their newest words are not logged yet (the desk already answered the earlier turn): nothing is guessed.
    desk.write(store, "assistant", {"text": "Here are a few."})
    desk.write(store, "assistant", {"text": "On it."})
    assert desk.heard(store) == ""


async def test_words_spoken_over_a_cut_reply_carry_what_the_user_heard(store):
    desk.write(store, "assistant", {"text": "The best value is Liquid Chal by Acme, and other options are Brand B and Brand C."})
    desk.cut(store, "The best value is Liquid Chal by Acme, and other", "user")
    desk.write(store, "user", {"text": "Add that to the cart."})
    note = '[interrupting your reply after hearing: "The best value is Liquid Chal by Acme, and other"]'
    assert desk.their_words(store, "add that") == "Add that to the cart.\n\n" + note
    # The desk's own word before its call does not hide it.
    desk.write(store, "assistant", {"text": "Okay."})
    desk.write(store, "call", {"call_id": "c1", "name": "pass_on", "arguments": "{}"})
    assert desk.their_words(store, "add that") == "Add that to the cart.\n\n" + note
    # Once the desk has acted, the cut is history.
    desk.write(store, "output", {"call_id": "c1", "output": "{}"})
    desk.write(store, "user", {"text": "And make it two."})
    assert desk.their_words(store, "make it two") == "And make it two."


async def test_a_cut_reply_counts_before_the_users_turn_is_logged(store):
    # A long turn transcribes late: the desk has already answered and called when its words are asked for.
    desk.write(store, "assistant", {"text": "Found three. The cheapest is the blue one, then the red one at twice that."})
    desk.cut(store, "Found three. The cheapest is the blue one,", "user")
    desk.write(store, "assistant", {"text": "Okay."})
    assert desk.their_words(store, "that one please") == (
        'that one please\n\n[interrupting your reply after hearing: "Found three. The cheapest is the blue one,"]'
    )


async def test_a_reply_cut_for_news_or_played_whole_adds_nothing(store):
    desk.write(store, "assistant", {"text": "Got it, looking into it now."})
    desk.cut(store, "Got it,", "news")
    desk.write(store, "user", {"text": "Yes."})
    assert desk.their_words(store, "yes") == "Yes."
    desk.write(store, "assistant", {"text": "Which one?"})
    desk.write(store, "user", {"text": "The blue one."})
    assert desk.their_words(store, "the blue one") == "The blue one."
    # Cut before a word was heard: nothing to tell.
    desk.write(store, "assistant", {"text": "Sure, the blue one.", "heard": "", "cut": "user"})
    desk.write(store, "user", {"text": "No, red."})
    assert desk.their_words(store, "no, red") == "No, red."


def test_the_memory_keeps_what_the_user_heard_of_a_cut_reply(store):
    desk.write(store, "assistant", {"text": "The best value is X, and other options are Y.", "heard": "The best value is X, and other", "cut": "user"})
    desk.write(store, "assistant", {"text": "Never heard at all.", "heard": "", "cut": "user"})
    desk.write(store, "assistant", {"text": "Played whole."})
    texts = [i["content"][0]["text"] for i in desk.items(store) if i.get("role") == "assistant"]
    assert texts == ["The best value is X, and other", "Played whole."]


def test_the_phone_logs_a_cut_with_the_reply_or_after_it(client, store):
    client.post("/api/voice/desk/log", json={"role": "assistant", "text": "One two three four.", "heard": "One two", "by": "user"})
    client.post("/api/voice/desk/log", json={"role": "assistant", "text": "Five six seven."})
    assert client.post("/api/voice/desk/cut", json={"heard": "Five six", "by": "news"}).status_code == 204
    logged = [e["payload"] for e in desk.entries(store) if e["kind"] == "assistant"]
    assert logged == [
        {"text": "One two three four.", "heard": "One two", "cut": "user"},
        {"text": "Five six seven.", "heard": "Five six", "cut": "news"},
    ]


def test_the_nods_are_made_once_in_the_desks_voice_and_kept(client, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    desk_api._nods.clear()
    asked = []

    def handler(request):
        asked.append(json.loads(request.content))
        return httpx.Response(200, content=b"\x00\x10" * 4, headers={"content-type": "audio/pcm"})

    real = httpx.AsyncClient
    monkeypatch.setattr(voice.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    got = client.get("/api/voice/desk/nods").json()
    assert got["rate"] == 24000 and len(got["clips"]) == len(desk_api.NODS)
    assert all(c == base64.b64encode(b"\x00\x10" * 4).decode() for c in got["clips"])  # the PCM as base64
    assert [a["input"] for a in asked] == list(desk_api.NODS)
    assert asked[0]["response_format"] == "pcm" and asked[0]["voice"] == settings.voice_tts_voice
    assert asked[0]["instructions"] == desk_api.NOD_INSTRUCTIONS
    assert sorted(f.name for f in (tmp_path / "voice").iterdir()) == [f"nod-{settings.voice_tts_voice}-{i}.pcm" for i in range(len(desk_api.NODS))]
    # Kept: the second ask makes nothing, and neither does a fresh server with the files on disk.
    client.get("/api/voice/desk/nods")
    desk_api._nods.clear()
    client.get("/api/voice/desk/nods")
    assert len(asked) == len(desk_api.NODS)


def test_without_the_speech_model_there_are_no_nods(client, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    desk_api._nods.clear()
    real = httpx.AsyncClient
    handler = lambda request: httpx.Response(503, json={"error": {"message": "down"}})
    monkeypatch.setattr(voice.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    assert client.get("/api/voice/desk/nods").json()["clips"] == []
    assert not (tmp_path / "voice").exists()


def test_the_desks_turns_end_on_the_words_not_a_timer():
    turns = desk.session_config()["audio"]["input"]["turn_detection"]
    assert turns["type"] == "semantic_vad" and turns["eagerness"] == "low"


async def test_a_reply_older_than_the_users_latest_message_says_so(store):
    put_chat(store, "aaaaaa1111", "WhatsApp login")
    store.message("aaaaaa1111", "user", "log in with my number")
    store.message("aaaaaa1111", "assistant", "That number has nine digits. Which is missing?", source="Orchestrator")
    put_run(store, "aaaaaa1111", "waiting_user")
    fresh = await desk.perform(store, "read_reply", json.dumps({"session": "s:aaaaaa"}))
    assert "note" not in fresh
    time.sleep(0.01)
    store.message("aaaaaa1111", "user", "No, it is 5125550198")
    stale = await desk.perform(store, "read_reply", json.dumps({"session": "s:aaaaaa"}))
    assert stale["reply"].startswith("That number has nine digits") and "before the user's latest message" in stale["note"]
