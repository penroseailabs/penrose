"""Messages sent while a task works: steering ones (Enter) reach it at its next step boundary, queued ones
(Ctrl+Enter) go out as the next message once it ends."""

import json

from app import delivery
from fakes import (
    Script,
    finish,
    make_state,
    note,
    orch,
    phone,
    report,
    run_step,
    runner_for,
    sections,
    set_plan,
    step_spec,
)
from test_api import api_client


def chat_messages(store):
    return [m["content"] for m in store.rows("SELECT content FROM messages WHERE role='user' ORDER BY created")]


def post(store, content, mode="steer"):
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        return delivery.deliver(db, "chat", content, "deepseek", mode)


def test_a_message_sent_while_the_task_works_waits_in_its_inbox(store, monkeypatch):
    make_state(store, steps=[step_spec("a")])
    with api_client(store, monkeypatch) as client:
        steer = client.post("/api/chats/chat/messages", json={"content": "Make it the blue one"})
        queued = client.post("/api/chats/chat/messages", json={"content": "Then book a cab", "mode": "next"})
        assert steer.status_code == queued.status_code == 202
        assert steer.json()["id"] == queued.json()["id"] == "run"
        # A steering message is in the conversation at once; a queued one is not, until it goes out.
        assert chat_messages(store) == ["Make it the blue one"]
        pending = client.get("/api/chats/chat").json()["pending"]
        assert [(p["mode"], p["content"]) for p in pending] == [("steer", "Make it the blue one"), ("next", "Then book a cab")]
        assert pending[0]["message_id"] and pending[1]["message_id"] is None
        # A queued message can be taken back; a steering one cannot.
        assert client.delete(f"/api/chats/chat/queued/{pending[0]['id']}").status_code == 404
        taken = client.delete(f"/api/chats/chat/queued/{pending[1]['id']}")
        assert taken.status_code == 200 and taken.json()["content"] == "Then book a cab"
        assert [p["mode"] for p in client.get("/api/chats/chat").json()["pending"]] == ["steer"]
    assert len(store.rows("SELECT * FROM runs")) == 1


def test_stop_keeps_steering_messages_for_the_task_and_hands_queued_ones_back(store, monkeypatch):
    make_state(store, steps=[step_spec("a")])
    post(store, "Use the Work address")
    post(store, "And then order water", mode="next")
    with api_client(store, monkeypatch) as client:
        stopped = client.post("/api/runs/run/stop").json()
    assert stopped["inbox"] == ["Use the Work address"] and stopped["unsent"] == ["And then order water"]
    assert store.load("run")["inbox"] == ["Use the Work address"] and "unsent" not in store.load("run")
    assert store.rows("SELECT * FROM inbox") == []


def test_what_a_finished_task_did_not_read_starts_the_next_task(store):
    state = make_state(store)
    post(store, "Also check the price")
    post(store, "Then book a cab", mode="next")
    state["status"] = "completed"
    store.save(state)
    after = store.settle("run")
    assert after["id"] != "run" and after["request"] == "Also check the price\n\nThen book a cab"
    assert chat_messages(store) == ["Also check the price", "Then book a cab"]
    # Both messages belong to the new task (the one that had begun when each was written).
    created = [m["created"] for m in store.rows("SELECT created FROM messages WHERE role='user'")]
    assert after["created"] <= min(created)
    assert store.rows("SELECT run_id FROM jobs WHERE status='queued'") == [{"run_id": after["id"]}]
    assert store.rows("SELECT * FROM inbox") == [] and store.settle(after["id"]) is None


def test_what_a_waiting_task_did_not_read_continues_it(store):
    state = make_state(store, steps=[step_spec("a")])
    post(store, "Pick the cheaper one", mode="next")
    state["status"] = "waiting_user"
    store.save(state)
    after = store.settle("run")
    assert after["id"] == "run" and after["status"] == "queued" and after["inbox"] == ["Pick the cheaper one"]
    assert chat_messages(store) == ["Pick the cheaper one"]
    # It joined the task like any reply, so it can be rewritten later.
    assert store.rows("SELECT run_id FROM checkpoints") == [{"run_id": "run"}]


def test_a_stopped_task_sends_nothing_on(store):
    state = make_state(store)
    post(store, "Then book a cab", mode="next")
    store.execute("UPDATE runs SET status='stopped' WHERE id='run'")
    assert store.settle("run") is None and store.rows("SELECT run_id FROM jobs") == []
    assert state["id"] == "run"


async def test_the_user_steers_the_working_step_and_the_orchestrator_hears_it(store):
    state = make_state(store, request="Order a shirt", route=None)
    shirt = step_spec("shirt", "Add a shirt to the cart")

    def orchestrator(context, n):
        if n == 0:
            return orch(set_plan(shirt, context="Order a shirt"), run_step("shirt"))
        assert context["trigger"] == "user_message" and context["new_user_messages"] == ["Make it the blue one"]
        assert context["latest"]["report"]["summary"] == "Added the blue shirt"
        return orch(finish("Added the blue shirt."))

    def subagent(text, n):
        if n == 0:
            post(store, "Make it the blue one")  # the user writes while the step works
            return phone(note("Looking at shirts"))
        brief = sections(text)
        assert "Make it the blue one" in brief["user_messages"]  # in the brief from now on
        if n == 1:
            assert "The user just wrote" in brief["Notices"]
            return phone(note("Switching to the blue one"))
        assert "The user just wrote" not in brief.get("Notices", "")  # the notice comes once
        return phone(report(summary="Added the blue shirt"))

    model = Script(Orchestrator=orchestrator, Subagent=subagent)
    await runner_for(store, model).run(state)
    assert state["status"] == "completed" and state["inbox"] == []
    assert any(line["line"] == "user: Make it the blue one" for line in state["journal"])
    assert store.rows("SELECT * FROM inbox") == []
    heard = [json.loads(r["payload"]) for r in store.rows("SELECT payload FROM events WHERE kind='message_heard'")]
    assert heard and heard[0]["messages"] == ["Make it the blue one"]


async def test_a_message_sent_before_the_first_decision_is_read_with_the_request(store):
    state = make_state(store, request="Order a shirt", route=None)
    post(store, "Size M please")

    def orchestrator(context, n):
        assert context["new_user_messages"] == ["Size M please"]
        return orch(finish("Noted."))

    await runner_for(store, Script(Orchestrator=orchestrator)).run(state)
    assert state["status"] == "completed" and store.rows("SELECT * FROM inbox") == []


class Thinking(Script):
    """The orchestrator thinks out loud (on_thinking) before it answers; `meanwhile(n)` runs as it starts thinking
    for the n-th time (the user writes then)."""

    def __init__(self, meanwhile, **handlers):
        super().__init__(**handlers)
        self.meanwhile, self.thoughts = meanwhile, 0

    async def ask(self, agent, context, images=None, on_update=None, on_thinking=None, **kw):
        if agent.name == "Orchestrator":
            self.thoughts += 1
            self.meanwhile(self.thoughts)
            await on_thinking("Planning the shirt...")
        return await super().ask(agent, context, images, on_update, on_thinking, **kw)


async def test_a_message_sent_while_the_orchestrator_thinks_starts_its_decision_over(store, monkeypatch):
    from app.runtime import runner as runner_module

    monkeypatch.setattr(runner_module, "HEED_SECONDS", 0)
    state = make_state(store, request="Order a shirt")  # handed over: the orchestrator alone (test_head_start.py)

    def orchestrator(context, n):
        assert context["new_user_messages"] == ["Make it the blue one"]  # the decision it made is the one with it
        return orch(finish("Noted: the blue one."))

    model = Thinking(lambda n: n == 1 and post(store, "Make it the blue one"), Orchestrator=orchestrator)
    await runner_for(store, model).run(state)
    assert state["status"] == "completed" and model.thoughts == 2 and len(model.of("Orchestrator")) == 1
    errors = [json.loads(r["payload"]) for r in store.rows("SELECT payload FROM events WHERE kind='stage_error'")]
    assert [e["description"] for e in errors] == ["Started over for the user's new message"]


async def test_a_stream_of_messages_still_ends_in_a_decision(store, monkeypatch):
    from app.runtime import runner as runner_module

    monkeypatch.setattr(runner_module, "HEED_SECONDS", 0)
    state = make_state(store, request="Order a shirt")  # handed over: the orchestrator alone (test_head_start.py)

    def orchestrator(context, n):
        return orch(finish("Noted."))

    model = Thinking(lambda n: post(store, f"message {n}"), Orchestrator=orchestrator)
    await runner_for(store, model).run(state)
    assert state["status"] == "completed" and model.thoughts == 3  # started over twice, then decided
    assert model.of("Orchestrator")[0]["context"]["new_user_messages"] == ["message 1", "message 2"]
