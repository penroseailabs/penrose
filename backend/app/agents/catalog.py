"""The models a run can be put on, and how a run's choice reaches the model client.

The composer picks two things for a task, each with an effort:
- the agent (left): the orchestrator and the agents that work beside it (memory, compaction, reflection), the
  "heavy" tier of agents/registry.py;
- the subagent (right): the agents that operate the phone (subagent, verifier), the "phone" tier.
The gate and the title agent stay on the cheap "fast" model of the agent's provider. A pick is {provider, model,
effort}; the two may come from different providers. The window sends its picks with every message, the run keeps
them (`state["models"]`), and `route` turns them into the keyword arguments of LLM.ask and LLM.act.

Without picks (a run from before them, a notification's task) a run is on its provider's tiers
(`llm.model_for`, `openai_model_for`, `anthropic_llm.model_for`).
"""

import typing
from dataclasses import dataclass

from pydantic import BaseModel, model_validator

from ..config import settings

Provider = typing.Literal["deepseek", "openai", "anthropic"]
SIDES = ("agent", "subagent")
TIER_SIDE = {"heavy": "agent", "phone": "subagent"}

# Efforts by provider, lowest first. DeepSeek's "off" runs the model without thinking.
EFFORTS = {
    "anthropic": ("low", "medium", "high"),
    "openai": ("low", "medium", "high"),
    "deepseek": ("off", "low", "high", "max"),
}


@dataclass(frozen=True)
class Entry:
    id: str  # the provider's model name
    provider: str
    label: str
    short: str  # for the composer's button
    sides: tuple


CATALOG = (
    Entry("claude-opus-5-5", "anthropic", "Claude Opus 5.5", "Opus 5.5", ("agent",)),
    Entry("claude-sonnet-5-5", "anthropic", "Claude Sonnet 5.5", "Sonnet 5.5", ("agent", "subagent")),
    Entry("claude-haiku-5-5", "anthropic", "Claude Haiku 5.5", "Haiku 5.5", ("subagent",)),
    Entry("deepseek-v4-pro", "deepseek", "DeepSeek V4 Pro", "V4 Pro", ("agent",)),
    Entry("deepseek-flash", "deepseek", "DeepSeek Flash", "Flash", ("agent", "subagent")),
    Entry("gpt-6-astra", "openai", "GPT-6 Astra", "Astra", ("agent",)),
    Entry("gpt-6.1-sol", "openai", "GPT-6.1 Sol", "GPT-6.1 Sol", ("agent", "subagent")),
    Entry("gpt-6-luna", "openai", "GPT-6 Luna", "Luna", ("agent", "subagent")),
)


def defaults(provider):
    """The picks a provider's run has today without any: {side: {provider, model, effort}}."""
    if provider == "anthropic":
        agent, subagent = (settings.anthropic_heavy_model, "high"), (settings.anthropic_phone_model, settings.subagent_reasoning_effort)
    elif provider == "openai":
        agent, subagent = (settings.openai_heavy_model, "high"), (settings.openai_phone_model, settings.subagent_reasoning_effort)
    else:
        agent = (settings.heavy_model, "max")
        subagent = (settings.subagent_model, settings.subagent_reasoning_effort if settings.phone_thinking else "off")
    return {
        side: {"provider": provider, "model": model, "effort": effort}
        for side, (model, effort) in zip(SIDES, (agent, subagent), strict=True)
    }


def known(provider):
    """The model names a pick may use: the catalog's, and the ones the settings put on this provider's tiers."""
    names = {e.id for e in CATALOG if e.provider == provider}
    return names | {pick["model"] for pick in defaults(provider).values()}


def listing():
    """What the window shows: the entries (each with its efforts), and every provider's default picks."""
    return {
        "models": [
            {"id": e.id, "provider": e.provider, "label": e.label, "short": e.short, "sides": list(e.sides),
             "efforts": list(EFFORTS[e.provider])}
            for e in CATALOG
        ],
        "defaults": {provider: defaults(provider) for provider in EFFORTS},
    }


class Pick(BaseModel):
    provider: Provider
    model: str
    effort: str

    @model_validator(mode="after")
    def in_the_catalog(self):
        if self.model not in known(self.provider):
            raise ValueError(f"{self.model} is not a {self.provider} model penrose offers.")
        if self.effort not in EFFORTS[self.provider]:
            raise ValueError(f"{self.effort} is not an effort for {self.provider}.")
        return self


class Models(BaseModel):
    """A task's picks, as the window sends them."""

    agent: Pick
    subagent: Pick

    @model_validator(mode="after")
    def on_its_side(self):
        for side in SIDES:
            pick = getattr(self, side)
            entry = next((e for e in CATALOG if e.id == pick.model and e.provider == pick.provider), None)
            if entry and side not in entry.sides:
                raise ValueError(f"{entry.label} is not offered for the {side}.")
        return self


def route(state, agent):
    """The keyword arguments for LLM.ask and LLM.act that put `agent`'s turn on the run's models: {} for a run on
    DeepSeek without picks; the provider alone for an OpenAI or Claude one."""
    models = state.get("models")
    if not models:
        return {"provider": state["provider"]} if state.get("provider") in ("openai", "anthropic") else {}
    side = TIER_SIDE.get(agent.tier)
    if side:
        pick = models[side]
        if agent.support and pick["provider"] == "anthropic":
            pick = {**pick, "model": settings.anthropic_support_model}
        return {"provider": pick["provider"], "model": pick["model"], "effort": pick["effort"]}
    return {"provider": models["agent"]["provider"]}  # the fast tier: the agent's provider, its own cheap model
