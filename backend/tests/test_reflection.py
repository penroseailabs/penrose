"""Reflection: every few tasks, the reflection agent reads the recent tasks, the chat summaries and every memory together."""

import json
import time

from app.config import settings
from app.plan import AppTarget
from app.runtime.reflection import REFLECTION_KEY
from fakes import (
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

INSTACART = AppTarget(name="Instacart", package="com.instacart.client")


def orchestrator(context, n):
    if not context["plan"]:
        return orch(set_plan(step_spec("shop", "Order groceries", app=INSTACART)), run_step("shop"))
    return orch(finish("Ordered."))


def remember(save=(), forget=()):
    return lambda context, n: {"actions": [{"tool": "remember", "save": list(save), "forget": list(forget)}]}


ROUTINE = {"name": "grocery-routine", "type": "user", "description": "Orders groceries on Sundays",
           "content": "Orders groceries most Sunday evenings.\nWhy: 2 of the last 2 tasks."}


async def planned_run(store, model, run_id, request):
    state = make_state(store, request=request)
    state["id"] = run_id
    store.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?)", (run_id, "chat", "running", json.dumps(state), time.time()))
    await runner_for(store, model).run(state)
    return state


async def test_every_k_tasks_the_reflection_agent_reads_them_together_and_writes_memories(store, monkeypatch):
    monkeypatch.setattr(settings, "reflect_every_tasks", 2)
    store.save_memory("old-store", "feedback", "Shops at Safeway", "Groceries from Safeway.")
    store.save_summary("chat", "The user ordered groceries twice.", time.time(), 30)
    model = Script(Orchestrator=orchestrator, Subagent=lambda c, n: phone(report(summary="Ordered.")),
                   Reflection=remember([ROUTINE], ["old-store"]))

    await planned_run(store, model, "run1", "Order my groceries")
    assert model.of("Reflection") == []
    assert store.profile_get(REFLECTION_KEY) == {"tasks": 1}

    await planned_run(store, model, "run2", "Groceries again please")
    assert len(model.of("Reflection")) == 1
    seen = model.of("Reflection")[0]["context"]
    assert [t["request"] for t in seen["recent_tasks"]] == ["Groceries again please", "Order my groceries"]
    assert seen["recent_tasks"][0]["status"] == "completed" and seen["recent_tasks"][0]["journal"]
    assert seen["recent_conversations"] == [{"title": "test", "date": seen["today"], "summary": "The user ordered groceries twice."}]
    assert list(seen["memory_index"]) == ["feedback"] and seen["memory_index"]["feedback"][0].startswith("old-store: ")
    rows = {m["name"]: m for m in store.memories()}
    assert set(rows) == {"grocery-routine"} and rows["grocery-routine"]["source"] == "reflection"
    counter = store.profile_get(REFLECTION_KEY)
    assert counter["tasks"] == 0 and counter["last"] > 0
    events = [json.loads(r["payload"]) for r in store.rows("SELECT payload FROM events WHERE kind='memories_updated' AND run_id='run2'")]
    reflected = next(e for e in events if e.get("reflection"))
    assert [m["name"] for m in reflected["added"]] == ["grocery-routine"] and reflected["removed"] == ["Groceries from Safeway."]

    # The next reflection reads only what came after this one.
    model3 = Script(Orchestrator=orchestrator, Subagent=lambda c, n: phone(report()), Reflection=remember())
    await planned_run(store, model3, "run3", "Milk")
    await planned_run(store, model3, "run4", "Eggs")
    seen = model3.of("Reflection")[0]["context"]
    assert [t["request"] for t in seen["recent_tasks"]] == ["Eggs", "Milk"] and seen["recent_conversations"] == []


async def test_a_failed_reflection_is_shown_and_tried_again_after_the_next_task(store, monkeypatch):
    monkeypatch.setattr(settings, "reflect_every_tasks", 1)

    def failing(context, n):
        raise RuntimeError("provider down")

    model = Script(Orchestrator=orchestrator, Subagent=lambda c, n: phone(report()), Reflection=failing)
    state = await planned_run(store, model, "run1", "Order my groceries")
    assert state["status"] == "completed"
    failures = [json.loads(r["payload"]) for r in store.rows("SELECT payload FROM events WHERE kind='memory_failed'")]
    assert len(failures) == 1 and failures[0]["reflection"] and "provider down" in failures[0]["description"]
    assert store.profile_get(REFLECTION_KEY) is None  # the count stands: the next task brings reflection again

    model = Script(Orchestrator=orchestrator, Subagent=lambda c, n: phone(report()), Reflection=remember([ROUTINE]))
    await planned_run(store, model, "run2", "Again")
    assert [m["name"] for m in store.memories()] == ["grocery-routine"]


async def test_reflection_is_off_with_auto_memory_or_at_zero(store, monkeypatch):
    monkeypatch.setattr(settings, "reflect_every_tasks", 1)
    store.profile_set("auto_memory", False)
    model = Script(Orchestrator=orchestrator, Subagent=lambda c, n: phone(report()))
    await planned_run(store, model, "run1", "Order my groceries")
    assert model.of("Reflection") == [] and store.profile_get(REFLECTION_KEY) is None

    store.profile_set("auto_memory", True)
    monkeypatch.setattr(settings, "reflect_every_tasks", 0)
    await planned_run(store, model, "run2", "Again")
    assert model.of("Reflection") == [] and store.profile_get(REFLECTION_KEY) is None
