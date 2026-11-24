"""Long-term memory: named notes learned after each run, recalled by type and app, kept free of duplicates."""

import json
import sqlite3

import pytest
from app.errors import DeviceError
from app.plan import AppTarget
from app.runtime.memory import learn
from app.store import Store
from app.tools.memory import Note
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
    sections,
    set_plan,
    step_spec,
    value,
)
from fastapi.testclient import TestClient
from pydantic import ValidationError

REQUEST = "I moved to Denver, order my usual veggie burrito bowl"
ANSWER = {"actions": [{"tool": "answer", "text": "Sure."}]}
INSTACART = "com.instacart.client"


def note(name, content, type="user", description=None, app=""):
    return {"name": name, "type": type, "description": description or content[:150], "content": content, "app": app}


def memory(save=(), forget=()):
    return lambda context, n: {"actions": [{"tool": "remember", "save": list(save), "forget": list(forget)}]}


def chat_state(store, request=REQUEST):
    state = make_state(store, request=request, route=None)
    store.message("chat", "user", request)
    return state


async def test_a_run_learns_from_the_users_words_and_replaces_a_memory_by_name(store):
    store.save_memory("home-city", "user", "Lives in Austin", "I live in Austin.")
    store.save_memory("old-diet", "user", "Vegetarian", "I prefer vegetarian food.")
    state = chat_state(store)
    model = Script(**{"Fast response": lambda c, n: ANSWER, "Memory": memory(
        [note("home-city", "I live in Denver (moved in October 2026).", description="Lives in Denver"),
         note("diet", "Vegetarian.\nWhy: says 'my usual veggie burrito bowl'.\nHow to apply: choose veg dishes.", "feedback")],
        ["old-diet", "unknown"],
    )})
    await runner_for(store, model).run(state)

    assert state["status"] == "completed"
    seen = model.of("Memory")[0]["context"]
    assert seen["new_user_messages"] == [REQUEST] and "task" not in seen
    assert seen["memory_index"] == {"user": ["home-city: Lives in Austin", "old-diet: Vegetarian"]}
    rows = {m["name"]: m for m in store.memories()}
    assert set(rows) == {"home-city", "diet"}
    assert rows["home-city"]["content"].startswith("I live in Denver") and rows["home-city"]["source"] == "chat"
    assert rows["home-city"]["created"] < rows["home-city"]["updated"]  # the same memory, updated
    payload = json.loads(store.rows("SELECT payload FROM events WHERE kind='memories_updated'")[0]["payload"])
    assert [m["name"] for m in payload["added"]] == ["home-city", "diet"]
    assert payload["removed"] == ["I prefer vegetarian food."]


async def test_the_gate_and_orchestrator_see_an_index_of_the_memories_and_never_their_text(store):
    store.save_memory("home-city", "user", "Lives in Denver", "I live in Denver.\nWhy: said so on 3 Oct.")
    store.save_memory("instacart-login", "app", "Instacart logs in with phone + OTP", "Phone number, then OTP.", app=INSTACART)
    model = Script(**{"Fast response": lambda c, n: ANSWER})
    await runner_for(store, model).run(chat_state(store))
    context = model.of("Fast response")[0]["context"]
    assert context["memory_index"] == {"user": ["home-city: Lives in Denver"],
                                       "app": [f"instacart-login ({INSTACART}): Instacart logs in with phone + OTP"]}
    assert "said so on 3 Oct" not in json.dumps(context) and "memories" not in context


async def test_the_gate_reads_a_memory_in_full_by_name_and_answers_from_it(store):
    store.save_memory("dog-swimming", "user", "Dog swimming options near home", "DogNation, Bellandur: 769 per session.")

    def gate(context, n):
        if n == 0:
            return {"actions": [{"tool": "read_memory", "names": ["dog-swimming (user):", "no-such"]}]}  # as its line has it
        return {"actions": [{"tool": "answer", "text": "DogNation, 769 a session."}]}

    model = Script(**{"Fast response": gate})
    state = chat_state(store, "How much is dog swimming?")
    await runner_for(store, model).run(state)
    read = model.of("Fast response")[1]["context"]["memories_read"]
    assert read["dog-swimming"]["content"] == "DogNation, Bellandur: 769 per session." and read["dog-swimming"]["updated"]
    assert read["no-such"].startswith("There is no memory with this name")
    assert state["status"] == "completed"


async def test_the_orchestrator_reads_memories_by_name_and_names_the_ones_a_step_turns_on(store):
    doordash = AppTarget(name="DoorDash", package="com.dd.doordash")
    store.save_memory("doordash-coupons", "feedback", "Always check the coupons on the DoorDash cart", "Open View all coupons first.")
    store.save_memory("workwear", "feedback", "Workwear: cotton, size M", "Cotton smart-casual, size M.")
    store.save_memory("home-address", "user", "Home is 4500 Mueller Blvd, Austin", "4500 Mueller Blvd, Austin, TX 78723.")
    store.save_memory("doordash-login", "app", "DoorDash logs in with a text code", "Phone, then OTP.", app="com.dd.doordash")

    def orchestrator(context, n):
        if n == 0:
            return orch({"tool": "read_memory", "names": ["home-address"]})
        if n == 1:  # a name the index does not have is refused, and it decides again
            return orch(set_plan(step_spec("order", "Order lunch on DoorDash", app=doordash, memories=["doordash-coupon"])))
        if n == 2:
            return orch(set_plan({**step_spec("order", "Order lunch on DoorDash", app=doordash).model_dump(),
                                  "memories": ["doordash-coupons (feedback)"]}))  # copied with its type: still that name
        return orch(finish("Ordered."))

    model = Script(Orchestrator=orchestrator, Subagent=lambda c, n: phone(report()))
    state = make_state(store)
    await runner_for(store, model).run(state)
    calls = model.of("Orchestrator")
    assert "memories_read" not in calls[0]["context"]
    assert calls[1]["context"]["memories_read"]["home-address"]["content"].startswith("4500 Mueller")
    assert calls[1]["context"]["trigger"] == "memories_read"
    assert "No memory is named doordash-coupon" in calls[2]["context"]["rejected_decision"]
    assert calls[3]["context"]["memories_read"]["home-address"]  # kept before it for the rest of the task
    # The subagent reads the rule its step names and the notes on its app: not the other rules, and no index.
    brief = sections(model.of("Subagent")[0]["context"])
    assert [m["name"] for m in value(brief["memories"])] == ["doordash-coupons", "doordash-login"]
    assert "memory_index" not in brief and "workwear" not in model.of("Subagent")[0]["context"]


async def test_the_index_is_held_to_its_size_and_the_memory_agent_is_asked_to_prune(store, monkeypatch):
    from app.config import settings
    from app.runtime.memory import about_user, context_for

    monkeypatch.setattr(settings, "memory_index_lines", 10)
    for i in range(9):
        store.save_memory(f"fact-{i}", "user", f"Fact {i}", f"Fact {i}.")
    assert len(about_user(store)["memory_index"]["user"]) == 9 and "consolidate" not in context_for(store)[0]
    for i in range(9, 13):
        store.save_memory(f"fact-{i}", "user", f"Fact {i}", f"Fact {i}.")
    listed = about_user(store)["memory_index"]
    # The ten most recently updated, oldest first, and a line saying the rest are out of sight.
    assert listed["user"][0] == "fact-3: Fact 3" and listed["user"][9] == "fact-12: Fact 12" and len(listed["user"]) == 10
    assert listed["not_listed"] == "3 older memories are not listed: the index is full"
    context, _ = context_for(store)
    assert len(context["memory_index"]["user"]) == 13 and context["consolidate"] is True  # the agent that prunes sees them all
    monkeypatch.setattr(settings, "memory_index_lines", 200)
    monkeypatch.setattr(settings, "memory_index_chars", 60)
    assert len(about_user(store)["memory_index"]["user"]) == 3  # three lines fit in 60 characters


async def test_the_memory_agent_reads_a_memory_before_it_replaces_it(store):
    store.save_memory("home-city", "user", "Lives in Austin", "I live in Austin, Texas.\nWhy: said so in September.")
    asked = []

    def agent(context, n):
        asked.append(context.get("memories_read"))
        if n == 0:
            return {"actions": [{"tool": "read_memory", "names": ["home-city"]}]}
        return {"actions": [{"tool": "remember", "save": [note("home-city", "I live in Denver.", description="Lives in Denver")]}]}

    model = Script(**{"Fast response": lambda c, n: ANSWER, "Memory": agent})
    await runner_for(store, model).run(chat_state(store))
    assert asked[0] is None and asked[1]["home-city"]["content"].startswith("I live in Austin, Texas.")
    assert {m["name"]: m["content"] for m in store.memories()} == {"home-city": "I live in Denver."}


async def test_a_failed_steps_lessons_are_not_kept(store):
    # A failed step explains its failure as the app's limit ("the keypad takes only minutes and seconds").
    clock = AppTarget(name="Clock", package="com.google.android.deskclock")

    def orchestrator(context, n):
        if n == 0:
            return orch(set_plan(step_spec("timer", "Set a 17h 51m 18s timer", app=clock)), run_step("timer"))
        return orch(finish("I could not set it."))

    model = Script(Orchestrator=orchestrator, Memory=memory([]), Subagent=lambda c, n: phone(report(
        status="failed", summary="The keypad took four digits.", lessons=["The timer keypad takes only minutes and seconds"])))
    state = make_state(store)
    await runner_for(store, model).run(state)
    assert not state.get("lessons")


async def test_a_subagents_lessons_become_app_memories_the_next_visit_reads(store):
    instacart = AppTarget(name="Instacart", package=INSTACART)
    store.save_memory("ask-before-paying", "feedback", "Ask before paying", "Ask me before any payment.")

    def orchestrator(context, n):
        if n == 0:
            return orch(set_plan(step_spec("login", "Log in to Instacart", app=instacart)), run_step("login"))
        return orch(finish("Logged in."))

    lessons = ["Login is email + a texted code; no password", "A promo sheet opens on launch; Close is top right"]
    model = Script(Orchestrator=orchestrator,
                   Subagent=lambda c, n: phone(report(summary="Logged in.", lessons=lessons)),
                   Memory=memory([note("instacart-login", "Email + a texted code; dismiss the promo sheet first.", "app",
                                       "Instacart login: phone + OTP, promo sheet in the way", INSTACART)]))
    state = make_state(store)
    await runner_for(store, model).run(state)

    assert "lessons" not in state["plan"][0]["report"]  # the orchestrator reads the report without them
    task = model.of("Memory")[0]["context"]["task"]
    assert task["lessons"] == [{"step": "login", "app": "Instacart", "package": INSTACART, "status": "completed",
                                "summary": "Logged in.", "lessons": lessons}]
    assert "memories" not in sections(model.of("Subagent")[0]["context"])  # the step named none, the app has no notes yet

    # The next task in Instacart starts from what this one learned; the lessons are not learned twice.
    await learn(runner_for(store, model), state)
    assert len(model.of("Memory")) == 1
    model2 = Script(Orchestrator=orchestrator, Subagent=lambda c, n: phone(report()))
    state2 = make_state(store)
    await runner_for(store, model2).run(state2)
    memories = value(sections(model2.of("Subagent")[0]["context"])["memories"])
    assert [m["name"] for m in memories] == ["instacart-login"] and memories[0]["app"] == INSTACART


async def test_messages_are_read_once_and_private_answers_skipped(store):
    state = chat_state(store)
    model = Script(**{"Fast response": lambda c, n: ANSWER, "Memory": memory()})
    runner = runner_for(store, model)
    await runner.run(state)
    state.pop("route")
    await runner.run(state)
    assert len(model.of("Memory")) == 1

    store.message("chat", "user", "[Private code provided]")
    await learn(runner, state)
    assert len(model.of("Memory")) == 1
    assert not store.rows("SELECT * FROM events WHERE kind='memories_updated'")


async def test_too_many_memories_ask_the_memory_agent_to_consolidate(store, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "memory_index_lines", 10)
    for i in range(11):
        store.save_memory(f"fact-{i}", "user", f"Fact {i}", f"Fact {i}.")
    model = Script(**{"Fast response": lambda c, n: ANSWER, "Memory": memory()})
    await runner_for(store, model).run(chat_state(store))
    assert model.of("Memory")[0]["context"]["consolidate"] is True


async def test_memory_failures_and_the_disabled_setting_never_affect_the_run(store):
    state = chat_state(store)

    def failing(context, n):
        raise RuntimeError("provider down")

    await runner_for(store, Script(**{"Fast response": lambda c, n: ANSWER, "Memory": failing})).run(state)
    assert state["status"] == "completed"
    assert not store.rows("SELECT * FROM memory_scans")  # the failed pass is retried after the next run

    store.profile_set("auto_memory", False)
    model = Script(Memory=memory([note("home-city", "I live in Denver.")]))
    await learn(runner_for(store, model), state)
    assert model.of("Memory") == [] and not store.memories()


def test_an_app_memory_must_name_its_app_and_names_are_kebab_case():
    with pytest.raises(ValidationError, match="needs app"):
        Note(**note("instacart-login", "OTP", "app"))
    with pytest.raises(ValidationError):
        Note(**note("Home Address", "12th Main"))


async def test_an_update_past_the_asked_lengths_is_kept_whole(store):
    # Lengths are guidance: one long note used to fail the whole update, and with it every other note.
    long_note = note("stockgro-account", "Has a StockGro account. " * 30, description="StockGro account " * 12)
    others = [note(f"fact-{i}", f"Fact {i}.") for i in range(9)]
    state = chat_state(store)
    model = Script(Memory=lambda c, n: {"actions": [{"tool": "remember", "save": [long_note, *others]}]})
    await learn(runner_for(store, model), state)
    assert len(store.memories()) == 10 and not store.rows("SELECT 1 FROM events WHERE kind='memory_failed'")
    assert len(next(m for m in store.memories() if m["name"] == "stockgro-account")["content"]) > 600


def test_the_panel_routes_a_note_through_the_memory_agent_and_keeps_it_as_written_when_it_cannot(store, monkeypatch):
    from app import api

    monkeypatch.setattr(api.common, "store", store)
    store.save_memory("home-city", "user", "Lives in Seattle", "I live in Seattle.")
    placed = Script(Memory=lambda c, n: {"actions": [{"tool": "remember", "save": [
        note("home-city", "I live in Austin (moved October 2026).", description="Lives in Austin")]}]})
    monkeypatch.setattr(api.memory, "model", placed)
    with TestClient(api.app) as client:
        reply = client.post("/api/memories", json={"content": "I live in Austin.", "provider": "anthropic"}).json()
        assert reply["placed"] and [m["name"] for m in reply["saved"]] == ["home-city"]
        assert "profile" not in reply  # nothing sums the memories up: the list is what the user reads
        seen = placed.of("Memory")[0]["context"]
        assert seen["user_added"] == "I live in Austin." and seen["memory_index"] == {"user": ["home-city: Lives in Seattle"]}
        rows = client.get("/api/memories").json()
        assert list(rows) == ["memories"] and [m["name"] for m in rows["memories"]] == ["home-city"]
        assert rows["memories"][0]["source"] == "user" and rows["memories"][0]["content"].startswith("I live in Austin")

        def failing(context, n):
            raise RuntimeError("provider down")

        monkeypatch.setattr(api.memory, "model", Script(Memory=failing))
        reply = client.post("/api/memories", json={"content": "I am vegetarian!"}).json()
        assert not reply["placed"] and reply["saved"][0]["name"] == "i-am-vegetarian"
        # Kept as written: a note that fits the line is the line, with nothing more to it.
        assert reply["saved"][0]["type"] == "user" and reply["saved"][0]["description"] == "I am vegetarian!"
        assert reply["saved"][0]["content"] == ""

        # The agent keeping nothing still keeps the user's words; an edit keeps them word for word.
        monkeypatch.setattr(api.memory, "model", Script(Memory=memory()))
        kept = client.post("/api/memories", json={"content": "Call me Al."}).json()["saved"][0]
        edited = client.put(f"/api/memories/{kept['id']}", json={"content": "Call me Al, never Alex.", "description": "Name"}).json()
        assert (edited["name"], edited["content"], edited["description"]) == ("call-me-al", "Call me Al, never Alex.", "Name")
        assert client.put("/api/memories/nope", json={"content": "x"}).status_code == 404
        client.delete(f"/api/memories/{kept['id']}")
        assert [m["name"] for m in client.get("/api/memories").json()["memories"]] == ["i-am-vegetarian", "home-city"]


async def test_a_memory_the_agent_saves_is_a_line_the_gate_reads_next_and_a_failure_is_shown(store):
    state = chat_state(store)
    model = Script(**{"Fast response": lambda c, n: ANSWER,
                      "Memory": memory([note("home-city", "I live in Denver.", description="Lives in Denver")])})
    await runner_for(store, model).run(state)
    model2 = Script(**{"Fast response": lambda c, n: ANSWER})
    await runner_for(store, model2).run(chat_state(store, "hello"))
    context = model2.of("Fast response")[0]["context"]
    assert context["memory_index"] == {"user": ["home-city: Lives in Denver"]} and "user_profile" not in context

    def failing(context, n):
        raise RuntimeError("provider down")

    state = chat_state(store, "something new")
    await runner_for(store, Script(**{"Fast response": lambda c, n: ANSWER, "Memory": failing})).run(state)
    failures = store.rows("SELECT payload FROM events WHERE kind='memory_failed'")
    assert len(failures) == 1 and "provider down" in json.loads(failures[0]["payload"])["description"]


def test_earlier_conversations_are_searched_by_their_words(store):
    store.execute("INSERT INTO chats VALUES (?,?,?)", ("old", "Hotel in Austin", 1))
    store.execute("INSERT INTO chats VALUES (?,?,?)", ("other", "Groceries", 2))
    store.message("old", "user", "Book the Fairmont in Austin for 12 November")
    store.message("old", "assistant", "Booked the Fairmont, 12 to 14 November, $1,840.", source="Orchestrator")
    store.message("old", "assistant", "internal note about Austin", source="Subagent")
    store.message("other", "user", "Order milk from Instacart")
    found = store.search_history("hotel I booked in Austin")
    assert [f["chat_id"] for f in found] == ["old"] and found[0]["title"] == "Hotel in Austin"
    assert len(found[0]["matches"]) == 2 and found[0]["date"].count("-") == 2
    assert any("Fairmont" in m["excerpt"] and m["role"] == "assistant" for m in found[0]["matches"])
    assert all("internal note" not in m["excerpt"] for m in found[0]["matches"])  # a subagent's words are not the chat
    assert store.search_history("zzz") == [] and store.search_history("a") == []
    assert store.search_history("Austin", exclude_chat="old") == []


def test_the_search_matches_by_stem_takes_several_wordings_and_ranks_rare_words_first(store):
    store.execute("INSERT INTO chats VALUES (?,?,?)", ("stay", "Trip", 1))
    store.execute("INSERT INTO chats VALUES (?,?,?)", ("milk", "Groceries", 2))
    store.message("stay", "assistant", "Your booking at the Marriott Downtown is confirmed for two nights.", source="Orchestrator")
    store.message("milk", "user", "Order the usual milk, it is for the week")
    # "booked" finds "booking" (stems); the other wordings bring in what the user never said.
    found = store.search_history(["hotel I booked", "Marriott stay"])
    assert [f["chat_id"] for f in found] == ["stay"] and "Marriott" in found[0]["matches"][0]["excerpt"]
    # A rare word outranks a common one: the Marriott chat comes first though both mention "the".
    assert store.search_history("the Marriott")[0]["chat_id"] == "stay"


def test_the_search_keeps_to_a_stretch_of_time_and_lists_its_conversations(store):
    import time

    day = 86400
    now = time.time()
    store.execute("INSERT INTO chats VALUES (?,?,?)", ("old", "Shoes", 1))
    store.execute("INSERT INTO chats VALUES (?,?,?)", ("new", "Dinner", 2))
    store.execute("INSERT INTO chats VALUES (?,?,?)", ("long", "Flights", 3))
    store.execute("INSERT INTO messages(id,chat_id,role,content,created) VALUES ('a','old','user','Find running shoes',?)", (now - 30 * day,))
    store.execute("INSERT INTO messages(id,chat_id,role,content,created) VALUES ('b','new','user','Book dinner for two',?)", (now - 2 * day,))
    store.execute("INSERT INTO messages(id,chat_id,role,content,created,source) VALUES ('c','new','assistant','Booked Uchi at 8.',?,"
                  "'Orchestrator')", (now - 2 * day + 60,))
    store.execute("INSERT INTO messages(id,chat_id,role,content,created) VALUES ('d','long','user','Flights to Lisbon',?)", (now - 3 * day,))
    store.save_summary("long", "You booked TAP flights to Lisbon for 3 to 10 December.", now - 3 * day, 4)
    after, before = store.day_bounds(store.local_day(now - 7 * day), None)
    assert [f["chat_id"] for f in store.search_history(["shoes", "dinner"], after=after)] == ["new"]
    listed = store.conversations_between(after, before)
    assert [c["chat_id"] for c in listed] == ["new", "long"]
    assert [m["excerpt"] for m in listed[0]["matches"]] == ["Book dinner for two", "Booked Uchi at 8."]
    assert listed[1]["summary"].startswith("You booked TAP flights") and "matches" not in listed[1]
    assert [c["chat_id"] for c in store.conversations_between(None, after)] == ["old"]


def test_an_index_from_before_stems_is_rebuilt_with_them(tmp_path):
    db = sqlite3.connect(tmp_path / "agent.sqlite3")
    db.executescript("""
        CREATE TABLE chats(id TEXT PRIMARY KEY,title TEXT,created REAL);
        CREATE TABLE messages(id TEXT PRIMARY KEY,chat_id TEXT,role TEXT,content TEXT,created REAL);
        CREATE VIRTUAL TABLE messages_fts USING fts5(content, content='messages', content_rowid='rowid');
        INSERT INTO chats VALUES ('old','Hotel',1);
        INSERT INTO messages VALUES ('m','old','user','Booking the Fairmont',1);
        INSERT INTO messages_fts(messages_fts) VALUES ('rebuild');
    """)
    db.commit()
    db.close()
    store = Store(tmp_path)
    assert "porter" in store.rows("SELECT sql FROM sqlite_master WHERE name='messages_fts'")[0]["sql"]
    assert [f["chat_id"] for f in store.search_history("booked")] == ["old"]
    store.message("old", "assistant", "Hotels nearby are full.", source="Orchestrator")  # new messages are indexed too
    assert len(store.search_history("hotel")[0]["matches"]) == 1


async def test_the_gate_recalls_earlier_conversations_once_and_then_answers(store):
    store.execute("INSERT INTO chats VALUES (?,?,?)", ("old", "Hotel", 1))
    store.message("old", "assistant", "Booked the Fairmont in Austin.", source="Orchestrator")
    state = chat_state(store, "Which hotel did I book in Austin?")
    recall = {"actions": [{"tool": "recall", "queries": ["hotel Austin", "Fairmont stay"]}]}
    model = Script(**{"Fast response": lambda c, n: recall if n == 0 else ANSWER})
    await runner_for(store, model).run(state)
    calls = model.of("Fast response")
    assert len(calls) == 2 and "recalled" not in calls[0]["context"]
    recalled = calls[1]["context"]["recalled"]
    assert recalled["queries"] == ["hotel Austin", "Fairmont stay"]
    assert recalled["found"][0]["chat_id"] == "old" and "Fairmont" in recalled["found"][0]["matches"][0]["excerpt"]
    assert state["status"] == "completed"
    # The search is shown in the chat as a tool call, with the conversations it found to open.
    shown = [json.loads(r["payload"]) for r in store.rows("SELECT payload FROM events WHERE kind='chats_searched'")]
    assert shown == [{"queries": ["hotel Austin", "Fairmont stay"], "after": "", "before": "",
                      "found": [{"chat_id": "old", "title": "Hotel", "date": recalled["found"][0]["date"]}]}]

    # Recalling twice is not allowed: the orchestrator takes the message.
    state = chat_state(store, "And the other one?")
    model = Script(**{"Fast response": lambda c, n: recall, "Orchestrator": lambda c, n: orch(finish("None."))})
    await runner_for(store, model).run(state)
    assert len(model.of("Fast response")) == 2 and state["route"] == {"orchestrate": True}
    assert model.of("Fast response")[1]["context"]["recalled"]["queries"] == ["hotel Austin", "Fairmont stay"]


async def test_the_gate_recalls_a_stretch_of_time_and_a_single_query_still_works(store):
    from app.tools.recall import Recall

    store.execute("INSERT INTO chats VALUES (?,?,?)", ("old", "Dinner", 1))
    store.message("old", "user", "Book dinner for two at Uchi")
    today = store.local_day(__import__("time").time())
    model = Script(**{"Fast response": lambda c, n: {"actions": [{"tool": "recall", "after": today}]} if n == 0 else ANSWER})
    await runner_for(store, model).run(chat_state(store, "What did I do today?"))
    found = model.of("Fast response")[1]["context"]["recalled"]["found"]
    assert [c["chat_id"] for c in found] == ["old"] and found[0]["matches"][0]["excerpt"] == "Book dinner for two at Uchi"
    assert Recall(tool="recall", query="hotel").queries == ["hotel"]  # the old single query
    with pytest.raises(ValidationError, match="needs queries"):
        Recall(tool="recall", queries=[" "])
    empty = model.of("Fast response")  # nothing earlier: a line saying so
    from app.tools.recall import recalled

    assert recalled(store, Recall(tool="recall", queries=["zebra"]))["found"] == "No earlier conversation mentions 'zebra'."
    assert empty


async def test_the_gate_updates_the_memory_before_it_answers_even_with_learning_off(store):
    store.profile_set("auto_memory", False)
    state = chat_state(store, "Remember that I'm vegetarian, and my card is 4111 1111 1111 1111")
    saved = note("diet", "Vegetarian.\nWhy: the user asked.\nHow to apply: food orders.", "feedback", "Vegetarian")
    model = Script(**{
        "Fast response": lambda c, n: {"actions": [{"tool": "update_memory", "request": "I'm vegetarian; my card is 4111..."}]}
        if n == 0 else {"actions": [{"tool": "answer", "text": "Saved that you're vegetarian. Card numbers are never kept."}]},
        "Memory": lambda c, n: {"actions": [{"tool": "remember", "save": [saved], "declined": ["Card number: never kept"]}]},
    })
    await runner_for(store, model).run(state)
    gate = model.of("Fast response")
    assert gate[1]["context"]["memory_updated"] == {"saved": ["Vegetarian"], "forgotten": [], "declined": ["Card number: never kept"]}
    assert model.of("Memory")[0]["context"]["user_asked"] == "I'm vegetarian; my card is 4111..."
    assert len(model.of("Memory")) == 1  # the pass after the run does not read the message again
    assert [m["name"] for m in store.memories()] == ["diet"] and store.memories()[0]["chat_id"] == "chat"
    event = json.loads(store.rows("SELECT payload FROM events WHERE kind='memories_updated'")[0]["payload"])
    assert event["declined"] == ["Card number: never kept"] and event["added"][0]["name"] == "diet"

    # A failure is said, not hidden behind a confirmation.
    def failing(context, n):
        raise RuntimeError("provider down")

    model = Script(**{"Fast response": lambda c, n: {"actions": [{"tool": "update_memory", "request": "forget my diet"}]}
                      if n == 0 else ANSWER, "Memory": failing})
    await runner_for(store, model).run(chat_state(store, "Forget my diet"))
    assert "failed" in model.of("Fast response")[1]["context"]["memory_updated"]


async def test_with_learning_off_only_what_the_user_asks_is_kept_and_nothing_is_picked_up_later(store):
    store.profile_set("auto_memory", False)
    state = chat_state(store, "I live in Denver now. Order a burrito bowl")
    model = Script(Memory=memory([note("home-city", "I live in Denver.")]))
    await learn(runner_for(store, model), state)
    assert model.of("Memory") == [] and not store.memories()
    # Said while learning was off: not read when it is turned back on.
    store.profile_set("auto_memory", True)
    store.message("chat", "user", "thanks")
    await learn(runner_for(store, model), state)
    assert model.of("Memory")[0]["context"]["new_user_messages"] == ["thanks"]

    store.profile_set("auto_memory", False)
    store.message("chat", "user", "Please remember my gate code is 1234 and order dinner")
    state["lessons"] = [{"app": INSTACART, "lesson": "The cart is under the bag icon."}]
    model = Script(Memory=memory())
    await learn(runner_for(store, model), state)
    seen = model.of("Memory")[0]["context"]
    assert seen["explicit_only"] is True and "task" not in seen  # no app lessons while learning is off


def test_deleting_or_editing_a_memory_in_the_panel_is_done_at_once_with_no_agent(store, monkeypatch):
    from app import api
    from app.runtime.memory import about_user

    monkeypatch.setattr(api.common, "store", store)
    home = store.save_memory("home-address", "user", "Home is 4500 Mueller Blvd", "Home is 4500 Mueller Blvd, Austin.")
    diet = store.save_memory("diet", "feedback", "Vegetarian", "Vegetarian.")
    model = Script(Memory=memory())
    monkeypatch.setattr(api.memory, "model", model)
    with TestClient(api.app) as client:
        assert client.delete(f"/api/memories/{home['id']}").json() == {"deleted": True}
        edited = client.put(f"/api/memories/{diet['id']}", json={"content": "Vegan.", "description": "Vegan"}).json()
        assert edited["content"] == "Vegan." and "profile" not in edited
    # Nothing was summed up, so nothing is left saying what the user took out: the index is the memories as they stand.
    assert model.calls == [] and about_user(store) == {"memory_index": {"feedback": ["diet: Vegan"]}}


def test_the_panel_runs_the_memory_agent_on_the_agent_picked_in_the_composer(store, monkeypatch):
    from app import api

    monkeypatch.setattr(api.common, "store", store)
    model = Script(Memory=memory())
    monkeypatch.setattr(api.memory, "model", model)
    sonnet = {"provider": "anthropic", "model": "claude-sonnet-5-5", "effort": "medium"}
    on = lambda call: (call["provider"], call["model"], call["effort"])  # noqa: E731
    with TestClient(api.app) as client:
        client.post("/api/memories", json={"content": "I am vegetarian.", "provider": "anthropic", "agent": sonnet})
        assert [on(c) for c in model.calls] == [("anthropic", "claude-sonnet-5-5", "medium")]

        # Without a pick (an older window), the provider's own model, as before.
        client.post("/api/memories", json={"content": "Call me Al.", "provider": "anthropic"})
        assert on(model.calls[-1]) == ("anthropic", None, None)
        # A pick outside the catalog is refused.
        bad = {**sonnet, "effort": "max"}
        assert client.post("/api/memories", json={"content": "x", "provider": "anthropic", "agent": bad}).status_code == 422


def test_the_panel_does_not_keep_what_the_memory_agent_declines(store, monkeypatch):
    from app import api

    monkeypatch.setattr(api.common, "store", store)
    monkeypatch.setattr(api.memory, "model", Script(Memory=lambda c, n: {"actions": [
        {"tool": "remember", "declined": ["Passport number: never kept"]}]}))
    with TestClient(api.app) as client:
        reply = client.post("/api/memories", json={"content": "My passport number is X1234567"}).json()
    assert reply["declined"] == ["Passport number: never kept"] and reply["saved"] == [] and not store.memories()


async def test_the_orchestrator_looks_up_the_phone_and_earlier_chats_before_planning(store):
    store.execute("INSERT INTO chats VALUES (?,?,?)", ("old", "Fridge", 1))
    store.message("old", "user", "Our fridge water tastes off again")
    devices = FakeDevices()
    devices.found["fridge"] = 'Mail "Your order of LG LRMVS3006S fridge" from Amazon · Mar 12'
    devices.web_found["fridge"] = 'Overview French-door fridges keep produce fresher…'

    def orchestrator(context, n):
        if n == 0:
            assert "looked_up" not in context
            return orch({"tool": "look_up", "queries": ["fridge", "water filter"], "email": True})
        assert context["trigger"] == "looked_up"
        first, second = context["looked_up"][0]
        assert "LG LRMVS3006S" in first["phone"] and "tastes off" in first["earlier_conversations"][0]["matches"][0]["excerpt"]
        assert first["web"].startswith("Overview French-door fridges")
        assert second == {"query": "water filter", "phone": "nothing found", "web": "nothing found"}
        return orch(finish("It is an LG LRMVS3006S."))

    model = Script(Orchestrator=orchestrator)
    state = make_state(store)
    await runner_for(store, model, devices=devices).run(state)
    assert state["status"] == "completed" and devices.searches == [("device-1", ["fridge", "water filter"])]
    assert devices.web_searches == devices.searches and devices.search_email == [True]
    assert any("looked up on the phone, in Gmail, on Google" in j["line"] for j in state["journal"])
    # Woken as the task started, before anything asked for the phone, and kept awake while each decision is made.
    assert devices.wakes[0] == ("device-1", 60) and set(devices.wakes[1:]) == {("device-1", 90)}
    shown = json.loads(store.rows("SELECT payload FROM events WHERE kind='chats_searched'")[0]["payload"])
    assert shown["queries"] == ["fridge", "water filter"] and [c["chat_id"] for c in shown["found"]] == ["old"]


async def test_a_gmail_search_is_asked_of_gmail_alone_and_keeps_its_operators(store):
    store.execute("INSERT INTO chats VALUES (?,?,?)", ("old", "Orders", 1))
    store.message("old", "user", "subject ordered your order from amazon")
    mail = 'from:amazon subject:(ordered OR "your order" OR "order confirmation" OR "order placed") -subject:(shipped OR delivered) -category:promotions'
    devices = FakeDevices()
    devices.found[mail] = ('App "Amazon" com.amazon.mShop.android.shopping\n'
                           + "\n".join(f'Mail "Ordered: item {i}" from Amazon · Oct {i} · {"x" * 150}' for i in range(1, 26))
                           + f'\nMail "{mail}" search Gmail · 25 emails')

    def orchestrator(context, n):
        if n == 0:
            return orch({"tool": "look_up", "queries": [mail, "headphones"], "apps": True})
        found, other = context["looked_up"][0]
        assert set(found) == {"query", "phone"} and found["query"] == mail  # no Google, browsing, chats or notifications
        assert "App " not in found["phone"] and "item 25" in found["phone"] and "search Gmail · 25 emails" in found["phone"]
        assert {"web", "play"} <= set(other)
        return orch(finish("Done."))

    state = make_state(store)
    await runner_for(store, Script(Orchestrator=orchestrator), devices=devices).run(state)
    assert state["status"] == "completed"
    assert devices.searches == [("device-1", [mail, "headphones"])] and devices.search_email == [True]
    assert devices.web_searches == devices.play_searches == devices.history_searches == [("device-1", ["headphones"])]


async def test_google_that_cannot_be_searched_says_why_and_the_phone_still_counts(store):
    devices = FakeDevices()
    devices.found["fridge"] = 'Mail "Your order of LG LRMVS3006S fridge" from Amazon · Mar 12'
    devices.web_error = DeviceError("Google could not be searched: Chrome is not running.")

    def orchestrator(context, n):
        if n == 0:
            return orch({"tool": "look_up", "queries": ["fridge"]})
        [found] = context["looked_up"][0]
        assert "LG LRMVS3006S" in found["phone"] and found["web"] == "not searched: Google could not be searched: Chrome is not running."
        return orch(finish("Done."))

    state = make_state(store)
    await runner_for(store, Script(Orchestrator=orchestrator), devices=devices).run(state)
    assert state["status"] == "completed"
    assert any("Google not searched: Google could not be searched" in j["line"] for j in state["journal"])
    assert devices.search_email == [False]  # a look-up leaves Gmail out unless it asks for it


async def test_a_look_up_brings_the_users_own_browsing_that_has_its_words(store, monkeypatch):
    from app.config import settings

    devices = FakeDevices()
    devices.history_found["headphones"] = (
        "searched 27 Oct · x2 · sennheiser hd 560s\namazon.com · 6 visits on 3 days: Sennheiser HD 560S (14 Nov)")

    def orchestrator(context, n):
        if n == 0:
            return orch({"tool": "look_up", "queries": ["headphones", "amplifier"]})
        if n == 1:
            first, second = context["looked_up"][0]
            assert first["history"].splitlines()[0] == "searched 27 Oct · x2 · sennheiser hd 560s"
            assert "history" not in second  # nothing they looked at has that word: no line
            devices.history_error = DeviceError("Chrome keeps no history on this phone.")
            return orch({"tool": "look_up", "queries": ["headphones"]})
        if n == 2:
            [again] = context["looked_up"][1]
            assert "history" not in again and again["phone"] == "nothing found"  # the others still count
            monkeypatch.setattr(settings, "history_lookup", False)
            return orch({"tool": "look_up", "queries": ["headphones"]})
        return orch(finish("Done."))

    state = make_state(store)
    await runner_for(store, Script(Orchestrator=orchestrator), devices=devices).run(state)
    assert state["status"] == "completed"
    # Read with every look-up while it is on; switched off, Chrome's history is not read at all.
    assert devices.history_searches == [("device-1", ["headphones", "amplifier"]), ("device-1", ["headphones"])]
    lines = [j["line"] for j in state["journal"]]
    assert sum("the user's Chrome history not read: Chrome keeps no history on this phone." in line for line in lines) == 1


async def test_a_look_up_searches_google_play_only_when_asked_and_says_why_when_it_cannot(store):
    devices = FakeDevices()
    devices.play_found["electricity bill"] = (
        'Play app "Duke Energy" Duke Energy Corporation · com.dukeenergy.customerapp · 4.6★ · '
        "in the user's Play library since Mar 2024, not on this phone")

    def orchestrator(context, n):
        if n == 0:
            return orch({"tool": "look_up", "queries": ["electricity bill", "power bill"], "apps": True})
        if n == 1:
            first, second = context["looked_up"][0]
            assert "in the user's Play library since Mar 2024" in first["play"] and second["play"] == "nothing found"
            devices.play_error = DeviceError("Google Play could not be searched: Chrome is not running.")
            return orch({"tool": "look_up", "queries": ["com.dukeenergy.customerapp"], "apps": True})
        if n == 2:
            [app] = context["looked_up"][1]
            assert app["play"] == "not searched: Google Play could not be searched: Chrome is not running."
            assert app["phone"] == "nothing found" and app["web"] == "nothing found"  # the others still count
            return orch({"tool": "look_up", "queries": ["Duke Energy"]})
        assert "play" not in context["looked_up"][2][0]  # not asked for: no line, and Play was not searched
        return orch(finish("Done."))

    state = make_state(store)
    await runner_for(store, Script(Orchestrator=orchestrator), devices=devices).run(state)
    assert state["status"] == "completed"
    assert devices.play_searches == [("device-1", ["electricity bill", "power bill"]), ("device-1", ["com.dukeenergy.customerapp"])]
    assert len(devices.searches) == len(devices.web_searches) == 3
    lines = [j["line"] for j in state["journal"]]
    assert any("on Google, on Google Play, in earlier conversations" in line for line in lines)
    assert any("Google Play not searched: Google Play could not be searched" in line for line in lines)


async def test_look_ups_are_capped_per_message_and_a_phone_that_cannot_search_says_why(store, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "max_lookups", 3)
    devices = FakeDevices()
    devices.search_error = DeviceError("The penrose home app did not answer its search (TimeoutError).")

    def orchestrator(context, n):
        if n < 4:
            if n == 3:
                assert "rejected_decision" not in context
            return orch({"tool": "look_up", "queries": [f"thing {n}"]})  # the fourth is rejected
        assert "rejected_decision" in context and len(context["looked_up"]) == 3
        assert context["looked_up"][0][0]["phone"].startswith("not searched: The penrose home app did not answer")
        assert context["looked_up"][0][0]["web"] == "nothing found"  # Google still searched
        return orch(finish("Done."))

    model = Script(Orchestrator=orchestrator)
    state = make_state(store)
    await runner_for(store, model, devices=devices).run(state)
    assert state["status"] == "completed" and len(model.of("Orchestrator")) == 5


def test_old_flat_memories_are_dropped_for_the_new_shape(tmp_path):
    db = sqlite3.connect(tmp_path / "agent.sqlite3")
    db.execute("CREATE TABLE memories(id TEXT PRIMARY KEY,content TEXT,created REAL,source TEXT,chat_id TEXT)")
    db.execute("INSERT INTO memories VALUES ('m','I live in Seattle.',1,'user',NULL)")
    db.commit()
    db.close()
    store = Store(tmp_path)
    assert store.memories() == []
    assert store.save_memory("home-city", "user", "Lives in Seattle", "I live in Seattle.")["name"] == "home-city"


async def test_a_memory_with_a_validity_is_recalled_until_its_day_and_kept_with_the_exact_wording(store):
    from app.runtime.memory import about_user, for_step

    store.save_memory("home-city", "user", "Lives in Austin", "I live in Austin, Texas.")
    store.save_memory("trip-cdmx", "user", "In Mexico City until 25 Oct", "In Mexico City until 25 Oct 2099.", expires="2099-10-25")
    store.save_memory("old-trip", "user", "Was in Berlin", "In Berlin until 3 March 2026.", expires="2026-03-03")
    store.save_memory("box", "feedback", "Ships 12x10x6 boxes", "Boxes are 12x10x6 inches.\nWhy: said so.\nHow to apply: shipping quotes.")
    from app.runtime.memory import read

    assert about_user(store)["memory_index"] == {
        "user": ["home-city: Lives in Austin", "trip-cdmx: In Mexico City until 25 Oct (until 2099-10-25)"],
        "feedback": ["box: Ships 12x10x6 boxes"]}
    found = read(store, ["trip-cdmx", "box", "old-trip"])
    assert found["trip-cdmx"]["valid_until"] == "2099-10-25" and "valid_until" not in found["box"]
    assert isinstance(found["old-trip"], str) and read(store, ["old-trip"], every=True)["old-trip"]["content"]
    # A step reads the memories it names, in the order named, and none it does not.
    assert for_step(store, None) == [] and [m["name"] for m in for_step(store, None, ["box", "old-trip", "home-city"])] == ["box", "home-city"]
    # The memory agent sets the validity itself.
    state = chat_state(store, "I'm in Mexico City until Sunday")
    model = Script(**{"Fast response": lambda c, n: ANSWER, "Memory": memory(
        [{**note("trip-cdmx", "In Mexico City until 25 Oct 2099."), "expires": "2099-10-25"}])})
    await runner_for(store, model).run(state)
    assert store.rows("SELECT expires FROM memories WHERE name='trip-cdmx'")[0]["expires"] == "2099-10-25"
    assert not store.rows("SELECT 1 FROM memories WHERE name='old-trip'")  # past its day: deleted at the next memory pass
    with pytest.raises(ValidationError):
        Note(name="x", type="user", description="d", content="c", expires="next week")


def test_a_database_from_before_validity_gains_the_column(tmp_path):
    first = Store(tmp_path)
    with first.connect() as db:
        db.execute("ALTER TABLE memories DROP COLUMN expires")
    again = Store(tmp_path)
    again.save_memory("a", "user", "d", "c")
    assert again.memories()[0]["expires"] == ""


async def test_the_orchestrator_commits_what_a_task_found_about_the_users_world_as_it_goes(store):
    """`remember` goes to the memory agent as task_findings, before the turn's last tool; the journal says what stuck."""
    findings = ["Morgan Blake (Engineering Manager, Northwind) works with the user on the Q3 launch; Teams chat 'Launch prep', 27 Oct 2026"]

    def orchestrator(context, n):
        if n == 0:
            return orch({"tool": "remember", "findings": findings}, finish("Profile built."))
        return orch(finish("Done."))

    def memory_agent(context, n):
        if "task_findings" not in context:  # the pass after the run: nothing new
            return {"actions": [{"tool": "remember", "save": [], "forget": []}]}
        assert context["task_findings"] == findings and context["task"]["request"] == state["request"]
        return {"actions": [{"tool": "remember", "save": [note("morgan-blake", "Morgan Blake, Engineering Manager at Northwind, works with you on the Q3 launch (as of 27 Oct 2026).")],
                             "forget": [], "declined": ["Morgan's phone number: a third party's detail"]}]}

    model = Script(Orchestrator=orchestrator, Memory=memory_agent)
    state = make_state(store)
    await runner_for(store, model).run(state)
    assert state["status"] == "completed"
    assert [m["name"] for m in store.memories()] == ["morgan-blake"]
    line = next(j["line"] for j in state["journal"] if j["line"].startswith("memory:"))
    assert line == "memory: kept morgan-blake; declined Morgan's phone number: a third party's detail"
    shown = json.loads(store.rows("SELECT payload FROM events WHERE kind='memories_updated'")[0]["payload"])
    assert [m["name"] for m in shown["added"]] == ["morgan-blake"] and shown["declined"]


async def test_remember_alone_is_a_turn_once_and_a_memory_failure_is_a_journal_line(store):
    """A model that calls tools keeps its findings and then reads what came of it: that turn is done, with the
    user's message still new for the next one. A second such turn in a row is told to move."""
    def orchestrator(context, n):
        if n < 2:
            assert "rejected_decision" not in context
            assert context["new_user_messages"] and context["trigger"] == "user_message"
            assert (n == 1) == ("memory:" in json.dumps(context["journal"]))
            return orch({"tool": "remember", "findings": ["Something"]})  # no run_step, ask_user or finish after it
        assert "End the turn with exactly one of" in context["rejected_decision"]
        return orch(finish("Done."))

    def memory_agent(context, n):
        if "task_findings" not in context:
            return {"actions": [{"tool": "remember", "save": [], "forget": []}]}
        raise RuntimeError("Provider returned HTTP 520")

    model = Script(Orchestrator=orchestrator, Memory=memory_agent)
    state = make_state(store, inbox=["Keep that in mind."])
    await runner_for(store, model).run(state)
    assert state["status"] == "completed" and store.memories() == []
    assert any(j["line"].startswith("memory: could not be updated now") for j in state["journal"])
    assert store.rows("SELECT 1 FROM events WHERE kind='memory_failed'")


def test_a_match_on_common_words_alone_is_no_match(store):
    """"temperature in New York now" found chats that only said "in", "new" and "now" (2026-11-04): a conversation
    counts only when its words carry half the query's weight, rare words weighing most (bm25's IDF)."""
    store.execute("INSERT INTO chats VALUES (?,?,?)", ("here", "Weather", 1))
    store.execute("INSERT INTO chats VALUES (?,?,?)", ("code", "Claude", 2))
    store.execute("INSERT INTO chats VALUES (?,?,?)", ("trip", "Trip", 3))
    store.execute("INSERT INTO chats VALUES (?,?,?)", ("nyc", "New York", 4))
    store.message("here", "user", "What is the temperature in New York now?")
    store.message("code", "user", "Trying the new code in Claude now, with no reinstall.")
    store.message("trip", "assistant", "New Zealand is researched, now pulling together Kazakhstan.", source="Orchestrator")
    store.message("nyc", "user", "Book a table in New York for Friday")
    for n in range(12):  # an ordinary history, where "in" and "now" are everywhere
        store.execute("INSERT INTO chats VALUES (?,?,?)", (f"c{n}", "Other", 5 + n))
        store.message(f"c{n}", "user", f"Order item {n} in the app now")
    found = store.search_history("temperature in New York now", exclude_chat="here")
    assert [f["chat_id"] for f in found] == ["nyc"]
    assert store.search_history("in now", exclude_chat="here")  # a query of only common words still finds them


def test_a_memory_may_be_its_line_alone_and_is_read_with_its_line(store):
    from app.runtime.memory import for_step, read

    store.save_memory("ubereats-coupons", "feedback", "Always apply coupons when ordering on Uber Eats", "")
    store.save_memory("home-address", "user", "Home is 4500 Mueller Blvd, Austin", "Gate code 4411; leave parcels with the concierge.")
    found = read(store, ["ubereats-coupons", "home-address"])
    assert found["ubereats-coupons"]["description"] == "Always apply coupons when ordering on Uber Eats" and "content" not in found["ubereats-coupons"]
    assert found["home-address"]["description"].startswith("Home is 4500") and found["home-address"]["content"].startswith("Gate code")
    assert [m["description"] for m in for_step(store, None, ["ubereats-coupons"])] == ["Always apply coupons when ordering on Uber Eats"]


def test_the_notes_on_an_app_are_read_in_full_only_while_they_fit(store, monkeypatch):
    from app.config import settings
    from app.runtime.memory import for_step

    monkeypatch.setattr(settings, "memory_step_chars", 250)
    store.save_memory("pay-with-paypal", "feedback", "Pay with PayPal when offered", "Why: said so on 3 Nov 2026.")
    for i, size in enumerate((900, 100, 100)):  # oldest first: the long one is the oldest
        store.save_memory(f"note-{i}", "app", f"Instacart note {i}", "x" * size, app=INSTACART)
    views = for_step(store, {"package": INSTACART}, ["pay-with-paypal"])
    assert [m["name"] for m in views] == ["pay-with-paypal", "note-2", "note-1", "note-0"]
    # What the step names is always whole; the app's notes are whole newest first, and the one that no longer fits is its line.
    assert views[0]["content"].startswith("Why:") and "content" in views[1] and "content" in views[2]
    assert "content" not in views[3] and views[3]["description"] == "Instacart note 0"


async def test_a_look_up_lists_an_earlier_conversation_once_with_its_two_best_matches(store):
    store.execute("INSERT INTO chats VALUES (?,?,?)", ("old", "Fridge", 1))
    for line in ("Our fridge water tastes off again", "The fridge is an LG", "Book a fridge repair"):
        store.message("old", "user", line)
    assert len(store.search_history("fridge")[0]["matches"]) == 2

    def orchestrator(context, n):
        if n == 0:
            return orch({"tool": "look_up", "queries": ["fridge", "fridge repair", "LG fridge"]})
        first, second, third = context["looked_up"][0]
        assert [c["chat_id"] for c in first["earlier_conversations"]] == ["old"]
        assert "earlier_conversations" not in second and "earlier_conversations" not in third
        return orch(finish("Found."))

    model = Script(Orchestrator=orchestrator)
    await runner_for(store, model, devices=FakeDevices()).run(make_state(store))
    assert len(model.of("Orchestrator")) == 2


def test_the_best_matching_conversation_is_quoted_at_length_and_the_others_briefly(store):
    reply = "Three names for the sourdough starter: " + ", ".join(f"name number {i} with its pun" for i in range(30))
    for chat, said in (("names", "sourdough starter names please"), ("other", "I fed the sourdough once")):
        store.execute("INSERT INTO chats VALUES (?,?,?)", (chat, chat, 1))
        store.message(chat, "user", said)
        store.message(chat, "assistant", reply + ("" if chat == "names" else " and bread"), source="Orchestrator")
    best, other = store.search_history("sourdough starter names")
    assert best["chat_id"] == "names"
    assert max(len(m["excerpt"]) for m in best["matches"]) > 600 >= 250 > max(len(m["excerpt"]) for m in other["matches"])


def test_the_index_is_in_sections_by_kind_with_what_is_pending_last_and_first_to_go(store, monkeypatch):
    from app.config import settings
    from app.runtime.memory import about_user

    store.save_memory("garage-repair", "open", "Scooter is at the garage; pick-up owed by Friday", "", expires="2099-01-09")
    store.save_memory("doordash-login", "app", "DoorDash logs in with a text code", "", app="com.dd.doordash")
    store.save_memory("coupons", "feedback", "Always check coupons", "")
    store.save_memory("orders-live-in", "reference", "Order status is in Amazon's Your Orders, not Gmail", "")
    store.save_memory("bicycle", "thing", "Bicycle is a Trek FX 3, size L", "")
    store.save_memory("ellen", "person", "Ellen is your sister; she lives in Denver", "")
    store.save_memory("home-city", "user", "Lives in Austin", "")
    listed = about_user(store)["memory_index"]
    assert list(listed) == ["user", "person", "thing", "reference", "feedback", "app", "open"]
    assert listed["app"] == ["doordash-login (com.dd.doordash): DoorDash logs in with a text code"]
    assert listed["open"] == ["garage-repair: Scooter is at the garage; pick-up owed by Friday (note kept until 2099-01-09)"]
    # When the index is full, what is pending goes first, however recently it was written.
    store.save_memory("garage-repair", "open", "Scooter is at the garage; pick-up owed by Saturday", "", expires="2099-01-10")
    monkeypatch.setattr(settings, "memory_index_lines", 6)
    listed = about_user(store)["memory_index"]
    assert "open" not in listed and listed["not_listed"].startswith("1 older") and len(listed) == 7


async def test_an_open_matter_gets_a_last_day_and_a_memory_past_its_day_is_deleted(store):
    from datetime import date, timedelta

    store.save_memory("old-order", "open", "Charger ordered, due 3 March", "", expires="2026-03-03")
    state = chat_state(store, "The plumber still owes me a quote for the bathroom")
    model = Script(**{"Fast response": lambda c, n: ANSWER, "Memory": memory(
        [note("plumber-quote", "", "open", "Plumber owes a quote for the bathroom")])})
    await runner_for(store, model).run(state)
    rows = {m["name"]: m for m in store.memories()}
    assert set(rows) == {"plumber-quote"}  # the lapsed one is gone
    lapses = date.fromisoformat(rows["plumber-quote"]["expires"]) - date.today()
    assert timedelta(days=28) <= lapses <= timedelta(days=31)  # no day was given: thirty days on
    assert "old-order" not in json.dumps(model.of("Memory")[0]["context"]["memory_index"])


async def test_the_gathering_tools_end_a_turn_together_each_once(store):
    """A model that calls tools asks for a search and a memory at once: both run, and the next decision reads both.
    Two look-ups in one turn are one look-up with more queries."""
    store.save_memory("home-city", "user", "Lives in Seattle", "Moved from Austin in 2024.")

    def orchestrator(context, n):
        if n == 0:
            return orch({"tool": "look_up", "queries": ["one"]}, {"tool": "look_up", "queries": ["two"]})
        if n == 1:
            assert "One look_up, one youtube and one read_memory at most" in context["rejected_decision"]
            return orch({"tool": "look_up", "queries": ["one", "two"]}, {"tool": "read_memory", "names": ["home-city"]})
        assert "rejected_decision" not in context
        assert len(context["looked_up"]) == 1 and [r["query"] for r in context["looked_up"][0]] == ["one", "two"]
        assert context["memories_read"]["home-city"]["content"] == "Moved from Austin in 2024."
        assert list(context).index("looked_up") < list(context).index("memories_read") < list(context).index("plan")
        return orch(finish("Done."))

    model = Script(Orchestrator=orchestrator)
    state = make_state(store)
    await runner_for(store, model, devices=FakeDevices()).run(state)
    assert state["status"] == "completed" and len(model.of("Orchestrator")) == 3


def cached_texts(parts):
    """The texts Claude is asked to keep for a context's parts: the prompt up to each cached piece's end."""
    return ["".join(parts[:i + 1]) for i, part in enumerate(parts) if part.cache]


async def test_the_recipes_open_the_context_and_the_lists_that_grow_leave_what_is_before_them(store, monkeypatch):
    """The recipes are sent on every turn in the first part, cached; the look-ups and then the conversation are
    cut item by item, so a second look-up or a new reply leaves everything before it as the last call sent it."""
    from app.agents.llm import context_parts
    from app.runtime import orchestrator as module

    monkeypatch.setattr(module, "recipes", lambda: [{"name": "order-food", "content": "steps"}])
    monkeypatch.setattr(module, "index", lambda: {})
    sent = []

    def orchestrator(context, n):
        sent.append(context_parts(context))
        if n < 2:
            return orch({"tool": "look_up", "queries": [f"thing {n}"]}, reply=f"Looking for thing {n}.")
        if n == 2:
            return orch(set_plan(step_spec("s1", "Search", app={"name": "Chrome"})), run_step("s1"), reply="Searching.")
        return orch(finish("Done."))

    def kept(earlier, later):
        """How many of the texts the earlier call asked Claude to keep open the later call, each ending at the
        end of one of its pieces: those are read from the cache, the rest is written again."""
        ends = {"".join(later[:n]) for n in range(len(later) + 1)}
        return [text in ends for text in cached_texts(earlier)]

    model = Script(Orchestrator=orchestrator, Subagent=lambda text, n: phone(report()))
    state = make_state(store)
    store.message("chat", "user", "Find the thing")
    await runner_for(store, model, devices=FakeDevices()).run(state)
    first, second, third, fourth = sent
    recipes = '{"recipes": [{"name": "order-food", "content": "steps"}]'
    for parts in sent:  # the same opening on every turn, with a plan and without
        assert parts[0] == recipes and parts[0].cache
    # The first look-up stands before the conversation, which is written again once: only the recipes are read.
    assert kept(first, second) == [True, False]
    # A second look-up: the recipes and the first look-up are read; the conversation after them, a few lines,
    # is written again.
    assert kept(second, third) == [True, True, False]
    # A reply and a plan, no new look-up: everything kept is read, the look-ups and the conversation so far.
    assert kept(third, fourth) == [True, True, True]
    # The look-ups, then the conversation, each kept up to its last item with its list still open.
    looked, said = cached_texts(third)[-2:]
    assert looked.endswith("]") and '"looked_up"' in looked and '"conversation"' not in looked
    assert said.endswith("}") and '"conversation": [' in said[len(looked):] and "Looking for thing 1." in said
    order = list(json.loads("".join(third)))
    assert order.index("looked_up") < order.index("conversation") < order.index("request") < order.index("plan")


async def test_gmail_that_could_not_be_asked_is_never_read_as_no_emails(store):
    devices = FakeDevices()
    devices.found["gravastar"] = 'Mail "gravastar" search Gmail · NOT SEARCHED: Gmail\'s servers did not answer'
    devices.found["loop gear"] = 'Mail "loop gear" search Gmail · no emails found'
    devices.mail["gravastar"] = "failed"

    def orchestrator(context, n):
        if n == 0:
            return orch({"tool": "look_up", "queries": ["gravastar", "loop gear"], "email": True})
        if n == 1:
            unseen, seen = context["looked_up"][0]
            # In a field of its own, in words that cannot be taken for an empty result.
            assert unseen["gmail"].startswith("NOT SEARCHED: Gmail's servers did not answer.")
            assert "does not mean there is no such email" in unseen["gmail"] and "could not see their email" in unseen["gmail"]
            assert "gmail" not in seen  # Gmail's servers answered this one: "no emails found" is an answer
            return orch({"tool": "look_up", "queries": ["gravastar"]})
        assert "gmail" not in context["looked_up"][1][0]  # Gmail was not asked: nothing is said about it
        return orch(finish("Done."))

    state = make_state(store)
    await runner_for(store, Script(Orchestrator=orchestrator), devices=devices).run(state)
    assert state["status"] == "completed"
    lines = [j["line"] for j in state["journal"]]
    assert sum("Gmail NOT searched for: gravastar: whether such emails exist is unknown" in line for line in lines) == 1
