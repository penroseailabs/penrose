"""What a tool is.

Every tool an agent can call is one class in this package. The class is the whole definition:
- the docstring is the description the model reads;
- each field is a parameter, and its `description` is what the model reads about it;
- the method below the fields is what the tool does.
Pydantic turns the classes into the JSON schema (or, for phone agents, the function definitions) the model
is sent, and checks every call against them. Which agent may call which tools is set in
app/agents/registry.py. The phone agents' screen and device calls are the harness's (app/bridge/tools.py).

Two kinds, by how they take part in a phone agent's turn (screen actions are the harness's tools):
- TurnTool: device commands, ADB, asking privately. It is the whole turn.
- FinalTool: ends the agent's work, and is what the agent hands back (a report, a verdict).
Orchestrator, gate and memory tools are plain Tools: the runtime applies them (`run`).
"""

from typing import Annotated, Union

from pydantic import BaseModel, Field, create_model


class Tool(BaseModel):
    tool: str  # every tool narrows this to Literal["its_name"] with that default

    @classmethod
    def tool_name(cls):
        return cls.model_fields["tool"].default

    async def run(self, ctx):
        raise NotImplementedError(f"{self.tool} is applied by the runtime")


class TurnTool(Tool):
    async def run(self, session) -> str:
        """Do it; return what happened, one line, for the agent's next turn."""
        raise NotImplementedError


class FinalTool(Tool):
    async def accept(self, session) -> str | None:
        """Why this ending cannot be accepted yet (the agent is told and goes on), or None."""
        return None


def toolset(tools):
    """The type of one call to any of `tools`: the `tool` field says which."""
    tools = tuple(tools)
    if len(tools) == 1:
        return tools[0]
    return Annotated[Union[tools], Field(discriminator="tool")]  # noqa: UP007


def turn_model(name, base, tools, max_actions):
    """`base` with an `actions` field that takes calls to `tools`."""
    return create_model(
        name, __base__=base,
        actions=(list[toolset(tools)], Field(
            min_length=1, max_length=max_actions,
            description="Your tool calls, run in order." if max_actions > 1 else "Exactly one tool call.",
        )),
    )
