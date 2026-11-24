"""The orchestrator starts beside the fast response agent (the gate): handed the message, it has a head start of
the gate's whole decision; otherwise it is cancelled having shown, said and done nothing."""

import asyncio
import json

from app import delivery
from fakes import Script, finish, make_state, orch, runner_for


def post(store, content):
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        return delivery.deliver(db, "chat", content, "deepseek", "steer")


def events(store, kind):
    return [json.loads(r["payload"]) for r in store.rows("SELECT payload FROM events WHERE kind=? ORDER BY id", (kind,))]


class Together(Script):
    """The gate answers only once the orchestrator's call has begun: run one after the other, it never would."""

    def __init__(self, **handlers):
        super().__init__(**handlers)
        self.begun = asyncio.Event()

    async def ask(self, agent, context, images=None, on_update=None, on_thinking=None, **kw):
        if agent.name == "Orchestrator":
            self.begun.set()
            await on_thinking("Planning...")
        elif agent.name == "Fast response":
            await asyncio.wait_for(self.begun.wait(), 2)
        return await super().ask(agent, context, images, on_update, on_thinking, **kw)


async def test_the_orchestrator_starts_while_the_gate_decides_and_shows_once_handed_over(store):
    state = make_state(store, request="Order a shirt", route=None)
    model = Together(Orchestrator=[orch(finish("Ordered."))])
    await runner_for(store, model).run(state)
    assert state["status"] == "completed" and state["route"] == {"orchestrate": True}
    # One orchestrator call, shown in the trace after the gate's decision, its thinking with it.
    stages = [(e["stage"]) for e in events(store, "stage_started")]
    assert stages[:2] == ["Fast response", "Orchestrator"]
    gate_done = store.rows("SELECT id FROM events WHERE kind='stage_output' ORDER BY id")[0]["id"]
    shown = store.rows("SELECT id FROM events WHERE kind='stage_started' AND payload LIKE '%Orchestrator%'")[0]["id"]
    assert shown > gate_done
    assert [e["text"] for e in events(store, "stage_thinking") if e["stage"] == "Orchestrator"] == ["Planning..."]
    assert len(model.of("Orchestrator")) == 1 and model.of("Orchestrator")[0]["context"]["trigger"] == "new_request"


async def test_a_message_the_gate_answers_leaves_no_trace_of_the_orchestrator(store):
    state = make_state(store, request="Thanks!", route=None)
    model = Together(**{"Fast response": [{"actions": [{"tool": "answer", "text": "You're welcome."}]}]},
                     Orchestrator=[orch(finish("Should never be said."))])
    await runner_for(store, model).run(state)
    assert state["status"] == "completed" and state["route"] == {"orchestrate": False}
    assert "trigger" not in state
    assert not [e for e in events(store, "stage_started") if e["stage"] == "Orchestrator"]
    assert not [e for e in events(store, "stage_error") if e["stage"] == "Orchestrator"]
    assert not [e for e in events(store, "stage_thinking") if e["stage"] == "Orchestrator"]
    said = [r["content"] for r in store.rows("SELECT content FROM messages WHERE role='assistant'")]
    assert said == ["You're welcome."]
    assert events(store, "completed")[-1]["reply"] == "You're welcome."


class Overtaken(Together):
    """The user writes while the orchestrator is still unseen."""

    def __init__(self, store, **handlers):
        super().__init__(**handlers)
        self.store = store

    async def ask(self, agent, context, images=None, on_update=None, on_thinking=None, **kw):
        if agent.name == "Orchestrator" and not self.of("Orchestrator"):
            post(self.store, "Make it the blue one")
        return await super().ask(agent, context, images, on_update, on_thinking, **kw)


async def test_a_message_written_before_the_hand_over_is_left_for_the_next_message_when_the_gate_answers(store):
    state = make_state(store, request="Thanks!", route=None)
    model = Overtaken(store, **{"Fast response": [{"actions": [{"tool": "answer", "text": "You're welcome."}]}]},
                      Orchestrator=[orch(finish("Never."))])
    await runner_for(store, model).run(state)
    assert state["status"] == "completed"
    # Untaken: delivery.settle sends it out as the next message once the job ends.
    assert store.has_messages("run") and "inbox" not in state


async def test_a_message_written_before_the_hand_over_starts_the_decision_over(store):
    state = make_state(store, request="Order a shirt", route=None)

    def orchestrator(context, n):
        return orch(finish("Noted: the blue one." if context["new_user_messages"] else "Ordered."))

    model = Overtaken(store, Orchestrator=orchestrator)
    await runner_for(store, model).run(state)
    assert state["status"] == "completed" and len(model.of("Orchestrator")) == 2
    assert model.of("Orchestrator")[1]["context"]["new_user_messages"] == ["Make it the blue one"]
    assert [e["description"] for e in events(store, "stage_error")] == ["Started over for the user's new message"]
    assert events(store, "completed")[-1]["reply"] == "Noted: the blue one."


