"""Conversation compaction: a long chat's older messages are folded into one summary the agents read in their place."""

import json
import time

import pytest
from app.config import settings
from app.runtime.compaction import compact, due
from fakes import Script, runner_for
from fastapi.testclient import TestClient

ANSWER = {"actions": [{"tool": "answer", "text": "Sure."}]}


@pytest.fixture(autouse=True)
def ticking_clock(monkeypatch):
    """Every message gets its own time: on Windows the clock ticks every few ms, and the cut between folded and
    kept messages moves with a tie (tested on its own below)."""
    real, last = time.time, [time.time()]

    def tick():
        last[0] = max(real(), last[0] + 0.001)
        return last[0]

    monkeypatch.setattr(time, "time", tick)


def summarize(text="Summary so far."):
    return lambda context, n: {"actions": [{"tool": "summarize", "summary": text}]}


def chat(store, exchanges, request="And now?"):
    """A chat of `exchanges` long user messages and short replies, ending with `request`: the run's state."""
    for i in range(exchanges):
        store.message("chat", "user", f"Message {i}: " + "x" * 400)
        store.message("chat", "assistant", f"Reply {i}.", source="Orchestrator")
    store.message("chat", "user", request)
    from fakes import make_state

    return make_state(store, request=request, route=None)


async def test_a_chat_over_budget_is_folded_into_a_summary_the_next_run_reads(store, monkeypatch):
    monkeypatch.setattr(settings, "conversation_budget_tokens", 1000)  # about ten of the 400-character messages
    monkeypatch.setattr(settings, "conversation_keep_messages", 4)
    store.message("chat", "user", "[Private code provided]")
    state = chat(store, 12)
    model = Script(**{"Fast response": lambda c, n: ANSWER, "Compaction": summarize("The user sent 12 messages.")})
    await runner_for(store, model).run(state)

    assert state["status"] == "completed"
    # The gate read every message word for word: the summary did not exist yet.
    gate = model.of("Fast response")[0]["context"]
    assert "conversation_summary" not in gate and len(gate["conversation"]) == 26
    seen = model.of("Compaction")[0]["context"]
    assert seen["summary_so_far"] == "" and seen["today"].count("-") == 2
    folded = seen["messages"]
    assert len(folded) == 26 - 4 and folded[0]["content"].startswith("Message 0") and folded[-1]["role"] == "assistant"
    assert all(m["content"] != "[Private code provided]" for m in folded)
    assert folded[0]["date"].count("-") == 2
    row = store.summary("chat")
    assert row["summary"] == "The user sent 12 messages." and row["folded"] == 23  # the private marker is covered too
    event = json.loads(store.rows("SELECT payload FROM events WHERE kind='compacted'")[0]["payload"])
    assert event["messages"] == 23 and event["folded"] == 23 and "23 older messages" in event["description"]

    # The next run reads the summary, then the four messages after it, then what the user wrote since.
    store.message("chat", "user", "One more")
    state2 = chat(store, 0, request="Is it done?")
    state2["id"] = "run2"
    store.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?)", ("run2", "chat", "running", json.dumps(state2), time.time()))
    model2 = Script(**{"Fast response": lambda c, n: ANSWER})
    await runner_for(store, model2).run(state2)
    gate = model2.of("Fast response")[0]["context"]
    assert gate["conversation_summary"] == "The user sent 12 messages."
    assert [m["content"][:9] for m in gate["conversation"]] == ["Message 1", "Reply 11.", "And now?", "Sure.", "One more", "Is it don"]
    assert model2.of("Compaction") == []  # well under budget now


async def test_a_second_compaction_folds_the_summary_before_it(store, monkeypatch):
    monkeypatch.setattr(settings, "conversation_budget_tokens", 1000)
    monkeypatch.setattr(settings, "conversation_keep_messages", 2)
    state = chat(store, 12)
    first = Script(**{"Fast response": lambda c, n: ANSWER, "Compaction": summarize("First summary.")})
    await runner_for(store, first).run(state)
    covered = store.summary("chat")["covers_until"]

    for i in range(12):
        store.message("chat", "user", f"Later {i}: " + "y" * 400)
    second = Script(**{"Fast response": lambda c, n: ANSWER, "Compaction": summarize("Both summaries.")})
    await runner_for(store, second).run(chat(store, 0, request="Again?"))
    seen = second.of("Compaction")[0]["context"]
    assert seen["summary_so_far"] == "First summary."
    # The first fold kept "And now?" and the reply; this one folds them with the new messages, keeping the last two.
    assert seen["messages"][0]["content"] == "And now?" and seen["messages"][-1]["content"].startswith("Later 11")
    row = store.summary("chat")
    assert row["summary"] == "Both summaries." and row["covers_until"] > covered and row["folded"] == 24 + 14


def test_the_cut_never_falls_between_messages_written_in_the_same_clock_tick(store, monkeypatch):
    monkeypatch.setattr(settings, "conversation_budget_tokens", 10)
    monkeypatch.setattr(settings, "conversation_keep_messages", 2)
    for i, at in enumerate((1.0, 2.0, 3.0, 3.0, 3.0, 4.0)):
        store.message("chat", "user", f"m{i} " + "z" * 100)
        store.execute("UPDATE messages SET created=? WHERE content LIKE ?", (at, f"m{i} %"))
    _, fold = due(store, "chat")
    assert [m["content"][:2] for m in fold] == ["m0", "m1", "m2", "m3", "m4"]  # m3 and m4 share m2's tick
    store.save_summary("chat", "s", fold[-1]["created"], len(fold))
    assert [m["content"][:2] for m in store.conversation_context("chat")["conversation"]] == ["m5"]


async def test_a_chat_within_budget_or_with_compaction_off_is_left_alone(store, monkeypatch):
    model = Script(**{"Fast response": lambda c, n: ANSWER})
    await runner_for(store, model).run(chat(store, 12))
    assert model.of("Compaction") == [] and store.summary("chat") is None

    monkeypatch.setattr(settings, "conversation_budget_tokens", 0)
    await runner_for(store, model).run(chat(store, 30))
    assert model.of("Compaction") == [] and store.summary("chat") is None


async def test_a_failed_compaction_never_affects_the_run_and_is_tried_again(store, monkeypatch):
    monkeypatch.setattr(settings, "conversation_budget_tokens", 1000)
    state = chat(store, 12)

    def failing(context, n):
        raise RuntimeError("provider down")

    await runner_for(store, Script(**{"Fast response": lambda c, n: ANSWER, "Compaction": failing})).run(state)
    assert state["status"] == "completed" and store.summary("chat") is None
    failures = store.rows("SELECT payload FROM events WHERE kind='compaction_failed'")
    assert len(failures) == 1 and "provider down" in json.loads(failures[0]["payload"])["description"]

    model = Script(Compaction=summarize("Now it worked."))
    await compact(runner_for(store, model), state)
    assert store.summary("chat")["summary"] == "Now it worked."


def test_the_conversation_an_agent_reads_is_capped_when_compaction_keeps_failing(store):
    from app.store import CONVERSATION_CAP

    for i in range(CONVERSATION_CAP + 20):
        store.message("chat", "user", f"m{i}")
    context = store.conversation_context("chat")
    assert len(context["conversation"]) == CONVERSATION_CAP and context["conversation"][-1]["content"] == f"m{CONVERSATION_CAP + 19}"
    assert "conversation_summary" not in context


def test_a_rewind_into_the_summarised_part_drops_the_summary_and_deleting_the_chat_drops_it_too(store, monkeypatch):
    from app import api
    from test_api import settle

    monkeypatch.setattr(api.common, "store", store)
    with TestClient(api.app) as client:
        chat_id = client.post("/api/chats").json()["id"]
        client.post(f"/api/chats/{chat_id}/messages", json={"content": "Order a pizza"})
        settle(store, journal=["s1 done: chose a margherita"])
        client.post(f"/api/chats/{chat_id}/messages", json={"content": "Add garlic bread"})
        settle(store, journal=["s2 done: added garlic bread"])
        client.post(f"/api/chats/{chat_id}/messages", json={"content": "And a cola"})
        settle(store)
        messages = {m["content"]: m for m in client.get(f"/api/chats/{chat_id}").json()["messages"]}
        store.save_summary(chat_id, "Pizza with garlic bread.", messages["Add garlic bread"]["created"], 2)

        # Rewriting a message the summary does not cover keeps it.
        cola = messages["And a cola"]["id"]
        assert client.post(f"/api/chats/{chat_id}/messages/{cola}/rewind", json={"content": "And a lemonade"}).status_code == 202
        assert store.summary(chat_id)["summary"] == "Pizza with garlic bread."
        settle(store)
        # Rewriting one it folded drops it: the messages that stay are summarised again later.
        garlic = messages["Add garlic bread"]["id"]
        assert client.post(f"/api/chats/{chat_id}/messages/{garlic}/rewind", json={"content": "Add a brownie"}).status_code == 202
        assert store.summary(chat_id) is None

        store.save_summary(chat_id, "Pizza.", time.time(), 1)
        settle(store, "completed")
        assert client.delete(f"/api/chats/{chat_id}").json() == {"deleted": True}
        assert store.summary(chat_id) is None
