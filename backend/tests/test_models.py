"""The user's picks for the agent and the subagent: model and effort each, from any provider (agents/catalog.py)."""

import json

import pytest
from app import delivery
from app.agents import anthropic_llm, catalog
from app.agents.llm import LLM
from app.agents.registry import (
    COMPACTOR,
    GATE,
    MEMORY,
    ORCHESTRATOR,
    REFLECTION,
    SUBAGENT,
    VERIFIER,
)
from app.config import settings
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
from test_api import api_client

OPUS_AND_HAIKU = {
    "agent": {"provider": "anthropic", "model": "claude-opus-5-5", "effort": "high"},
    "subagent": {"provider": "anthropic", "model": "claude-haiku-5-5", "effort": "low"},
}
SONNET_AND_FLASH = {
    "agent": {"provider": "anthropic", "model": "claude-sonnet-5-5", "effort": "medium"},
    "subagent": {"provider": "deepseek", "model": "deepseek-flash", "effort": "off"},
}


def test_every_default_is_a_pick_the_catalog_accepts():
    for provider in catalog.EFFORTS:
        picks = catalog.defaults(provider)
        assert catalog.Models(**picks)


def test_the_picker_offers_two_models_on_each_side_of_claude_and_deepseek_and_the_latest_gpt():
    sides = {side: [m["id"] for m in catalog.listing()["models"] if side in m["sides"]] for side in catalog.SIDES}
    assert sides["agent"] == ["claude-opus-5-5", "claude-sonnet-5-5", "deepseek-v4-pro", "deepseek-flash",
                              "gpt-6-astra", "gpt-6.1-sol", "gpt-6-luna"]
    assert sides["subagent"] == ["claude-sonnet-5-5", "claude-haiku-5-5", "deepseek-flash", "gpt-6.1-sol", "gpt-6-luna"]
    assert all(m["efforts"] for m in catalog.listing()["models"])


@pytest.mark.parametrize("pick, why", [
    ({"provider": "anthropic", "model": "gpt-6-luna", "effort": "low"}, "not a anthropic model"),
    ({"provider": "anthropic", "model": "claude-opus-5-5", "effort": "max"}, "not an effort"),
    ({"provider": "openai", "model": "gpt-6-luna", "effort": "off"}, "not an effort"),
])
def test_a_pick_outside_the_catalog_is_refused(pick, why):
    with pytest.raises(ValueError, match=why):
        catalog.Pick(**pick)


def test_a_model_is_only_accepted_on_the_side_it_is_offered_for():
    with pytest.raises(ValueError, match="not offered for the subagent"):
        catalog.Models(agent=OPUS_AND_HAIKU["agent"], subagent=OPUS_AND_HAIKU["agent"])
    with pytest.raises(ValueError, match="not offered for the agent"):
        catalog.Models(agent=OPUS_AND_HAIKU["subagent"], subagent=OPUS_AND_HAIKU["subagent"])


def test_a_model_the_settings_put_on_a_tier_is_accepted_too(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_heavy_model", "claude-opus-5-6")
    assert catalog.Pick(provider="anthropic", model="claude-opus-5-6", effort="high")


def test_route_sends_each_agent_to_its_side_and_the_cheap_ones_to_the_agents_provider():
    state = {"provider": "anthropic", "models": SONNET_AND_FLASH}
    assert catalog.route(state, ORCHESTRATOR) == {"provider": "anthropic", "model": "claude-sonnet-5-5", "effort": "medium"}
    assert catalog.route(state, SUBAGENT) == catalog.route(state, VERIFIER) == {
        "provider": "deepseek", "model": "deepseek-flash", "effort": "off"}
    assert catalog.route(state, GATE) == {"provider": "anthropic"}


def test_memory_and_reflection_run_on_the_support_model_whatever_the_agent_is():
    opus = {"provider": "anthropic", "models": OPUS_AND_HAIKU}
    for agent in (MEMORY, REFLECTION):
        assert catalog.route(opus, agent) == {"provider": "anthropic", "model": "claude-sonnet-5-5", "effort": "high"}
        assert anthropic_llm.model_for(agent.tier, agent.support) == ("claude-sonnet-5-5", "high")
    assert catalog.route(opus, ORCHESTRATOR)["model"] == "claude-opus-5-5"
    assert catalog.route(opus, COMPACTOR)["model"] == "claude-opus-5-5"
    assert anthropic_llm.model_for(ORCHESTRATOR.tier, ORCHESTRATOR.support) == ("claude-opus-5-5", "high")
    deepseek = {"provider": "deepseek", "models": {
        "agent": {"provider": "deepseek", "model": "deepseek-v4-pro", "effort": "max"}, "subagent": SONNET_AND_FLASH["subagent"]}}
    assert catalog.route(deepseek, MEMORY)["model"] == "deepseek-v4-pro"


def test_without_picks_a_run_is_on_its_providers_tiers():
    assert catalog.route({"provider": "deepseek"}, ORCHESTRATOR) == {}
    assert catalog.route({"provider": "openai"}, SUBAGENT) == {"provider": "openai"}


# --- the model client ------------------------------------------------------------------------------


def test_deepseek_effort_off_runs_without_thinking_and_a_level_turns_it_on():
    off = LLM.payload(ORCHESTRATOR, [], "deepseek", "deepseek-flash", "off")
    assert off["model"] == "deepseek-flash" and off["thinking"] == {"type": "disabled"} and "reasoning_effort" not in off
    high = LLM.payload(GATE, [], "deepseek", "deepseek-v4-pro", "high")
    assert high["model"] == "deepseek-v4-pro" and high["thinking"] == {"type": "enabled"} and high["reasoning_effort"] == "high"
    default = LLM.payload(ORCHESTRATOR, [], "deepseek")
    assert default["model"] == settings.heavy_model and default["reasoning_effort"] == "max"


def test_deepseek_phone_turns_use_the_picked_model_and_effort():
    off = LLM.tool_payload(SUBAGENT, "s", None, "deepseek", "deepseek-flash", "off")
    assert off["thinking"] == {"type": "disabled"} and off["tool_choice"] == "required" and "reasoning_effort" not in off
    on = LLM.tool_payload(SUBAGENT, "s", None, "deepseek", "deepseek-v4-pro", "high")
    assert on["model"] == "deepseek-v4-pro" and on["reasoning_effort"] == "high" and on["tool_choice"] == "auto"
    default = LLM.tool_payload(SUBAGENT, "s", None, "deepseek")
    assert default["model"] == settings.subagent_model and default["reasoning_effort"] == settings.subagent_reasoning_effort


def test_openai_turns_use_the_picked_model_and_effort():
    json_turn = LLM.payload(ORCHESTRATOR, [{"role": "system", "content": "x"}], "openai", "gpt-6-astra", "medium")
    assert json_turn["model"] == "gpt-6-astra" and json_turn["reasoning_effort"] == "medium"
    phone_turn = LLM.responses_payload(SUBAGENT, "s", None, "gpt-6.1-sol", "high")
    assert phone_turn["model"] == "gpt-6.1-sol" and phone_turn["reasoning"]["effort"] == "high"
    default = LLM.responses_payload(SUBAGENT, "s", None)
    assert default["model"] == settings.openai_phone_model


async def test_claude_turns_use_the_picked_model_and_effort():
    from app.agents.anthropic_llm import Claude

    seen = []

    class Message:
        stop_reason, usage, model = "end_turn", None, "m"
        content = [type("Block", (), {"type": "tool_use", "name": "report", "input": {}})()]

    claude = Claude(client=object())

    async def run(model, **request):
        seen.append((model, request["output_config"]))
        return Message()

    claude.run = run
    await claude.act(SUBAGENT, "system", "state", model="claude-sonnet-5-5", effort="high")
    await claude.act(SUBAGENT, "system", "state")
    assert seen[0] == ("claude-sonnet-5-5", {"effort": "high"})
    assert seen[1][0] == settings.anthropic_phone_model


# --- from the message to the model call ----------------------------------------------------------


def test_a_message_carries_its_picks_into_the_new_task(store, monkeypatch):
    with api_client(store, monkeypatch) as client:
        chat = client.post("/api/chats").json()
        sent = client.post(f"/api/chats/{chat['id']}/messages", json={
            "content": "Find me a fridge", "provider": "deepseek", "models": SONNET_AND_FLASH})
    assert sent.status_code == 202
    state = sent.json()
    assert state["models"] == SONNET_AND_FLASH
    assert state["provider"] == "anthropic"  # the agent's provider stands for the task
    assert store.load(state["id"])["models"] == SONNET_AND_FLASH


def test_a_message_with_a_pick_outside_the_catalog_is_refused(store, monkeypatch):
    bad = {**SONNET_AND_FLASH, "agent": {"provider": "anthropic", "model": "claude-haiku-5-5", "effort": "low"}}
    with api_client(store, monkeypatch) as client:
        chat = client.post("/api/chats").json()
        response = client.post(f"/api/chats/{chat['id']}/messages", json={"content": "Hi", "models": bad})
    assert response.status_code == 422


def test_a_message_without_picks_leaves_the_task_on_its_providers_models(store, monkeypatch):
    with api_client(store, monkeypatch) as client:
        chat = client.post("/api/chats").json()
        state = client.post(f"/api/chats/{chat['id']}/messages", json={"content": "Hi", "provider": "openai"}).json()
    assert "models" not in state and state["provider"] == "openai"


def test_the_picks_of_a_message_sent_while_the_task_works_reach_the_task_and_a_queued_one_starts_the_next(store):
    state = make_state(store, steps=[step_spec("a")])
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        delivery.deliver(db, "chat", "Use Sonnet", "anthropic", "steer", SONNET_AND_FLASH)
        delivery.deliver(db, "chat", "Then do more", "anthropic", "next", OPUS_AND_HAIKU)
    heard = store.take_messages("run")
    assert json.loads(heard[0]["models"]) == SONNET_AND_FLASH
    state["status"] = "completed"
    store.save(state)
    next_run = store.settle("run")
    assert next_run["models"] == OPUS_AND_HAIKU and next_run["provider"] == "anthropic"


async def test_a_run_calls_each_agent_on_the_model_picked_for_its_side(store):
    spec = step_spec("a")
    state = make_state(store, route=None, models=SONNET_AND_FLASH, provider="anthropic")
    model = Script(
        Orchestrator=[orch(set_plan(spec)), orch(run_step("a")), orch(finish("Done."))],
        Subagent=lambda text, n: phone(report("completed", "Found it.")),
    )
    await runner_for(store, model).run(state)

    assert model.of("Fast response")[0]["provider"] == "anthropic"
    assert model.of("Fast response")[0]["model"] is None  # the gate stays on the agent's provider's cheap model
    assert {(c["provider"], c["model"], c["effort"]) for c in model.of("Orchestrator")} == {
        ("anthropic", "claude-sonnet-5-5", "medium")}
    assert {(c["provider"], c["model"], c["effort"]) for c in model.of("Subagent")} == {
        ("deepseek", "deepseek-flash", "off")}
