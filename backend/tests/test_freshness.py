"""Where the user came back to a chat: the conversation the agents read marks each new request with when it came, so
what an earlier reply saw (an order delivered, a price) is read as what was true then, not now."""

import json
import time

import pytest
from app.store import REQUEST_GAP, request_break

DAY = 24 * 60 * 60


@pytest.fixture
def clock(monkeypatch):
    now = [1_800_000_000.0]
    monkeypatch.setattr(time, "time", lambda: now[0])
    return now


def run_started(store, run_id, when):
    store.execute("INSERT INTO runs VALUES (?,?,?,?,?)",
                  (run_id, "chat", "completed", json.dumps({"id": run_id, "chat_id": "chat", "created": when}), when))


def roles(store):
    return [m["role"] for m in store.conversation_context("chat")["conversation"]]


def test_a_request_long_after_the_last_reply_is_marked_with_when_it_came(store, clock):
    store.message("chat", "user", "Order my usual from Sweetgreen")
    clock[0] += 60
    store.message("chat", "assistant", "Ordered. It was delivered at 7:40 pm.", source="Orchestrator")
    clock[0] += 2 * DAY
    store.message("chat", "user", "When will my order arrive?")

    conversation = store.conversation_context("chat")["conversation"]
    assert [m["role"] for m in conversation] == ["user", "assistant", "break", "user"]
    assert conversation[2]["content"].startswith("New request on ") and "2 days after the message before" in conversation[2]["content"]


def test_quick_back_and_forth_has_no_breaks(store, clock):
    for text in ("Book a ride home", "Uber or Lyft?", "Uber"):
        store.message("chat", "user" if text != "Uber or Lyft?" else "assistant", text, source=None if text != "Uber or Lyft?" else "Orchestrator")
        clock[0] += 30
    assert "break" not in roles(store)


def test_a_new_task_in_the_chat_starts_a_new_request_however_soon(store, clock):
    run_started(store, "run1", clock[0])
    store.message("chat", "user", "Order pad thai")
    clock[0] += 60
    store.message("chat", "assistant", "Ordered.", source="Orchestrator")
    clock[0] += 60
    run_started(store, "run2", clock[0])
    clock[0] += 0.001
    store.message("chat", "user", "And a drink")
    assert roles(store) == ["user", "assistant", "break", "user"]


def test_the_first_message_after_a_summary_is_measured_from_the_summarised_part(store, clock):
    store.message("chat", "user", "old")
    store.save_summary("chat", "The user ordered pad thai; it was delivered.", clock[0], 1)
    clock[0] += REQUEST_GAP + 1
    store.message("chat", "user", "Where is it?")
    assert roles(store) == ["break", "user"]


def test_the_line_is_fixed_once_written_and_names_the_time_zone_when_unknown():
    assert request_break(1_800_000_000, 90) == request_break(1_800_000_000, 90)
    assert request_break(1_800_000_000, 90).endswith("UTC, moments after the message before.")
    assert "15 minutes" in request_break(0, 15 * 60) and "5 hours" in request_break(0, 5 * 3600)


