"""The gate and the orchestrator answering with native tool calls: their turn object, put together from what the
model wrote and what it called."""

import asyncio
import json

import httpx
import pytest
from app.agents.llm import LLM, schema_text
from app.agents.registry import GATE, ORCHESTRATOR, gate_turn, orchestrator_notes, orchestrator_turn
from app.config import settings
from app.errors import ProviderError
from app.tools.gate import Answer, Delegate
from app.tools.orchestrator import AskUser, Finish, RunStep, SetPlan
from test_anthropic import claude, llm_with, stream_body

PLAN = {"steps": [{"id": "s1", "goal": "Find boxing gloves", "done_when": "Three pairs are listed"}]}


@pytest.fixture(autouse=True)
def deepseek_key(monkeypatch):
    monkeypatch.setattr(settings, "deepseek_api_key", "test")


def chat(seen, *replies):
    """A chat-completions model whose requests land in `seen`: each reply is (text, [(tool, arguments), ...])."""
    replies = list(replies)

    def respond(request):
        seen.append(json.loads(request.content))
        text, calls = replies.pop(0)
        message = {"content": text, "tool_calls": [
            {"id": str(i), "type": "function", "function": {"name": name, "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments)}}
            for i, (name, arguments) in enumerate(calls)]}
        return httpx.Response(200, json={"choices": [{"finish_reason": "tool_calls" if calls else "stop", "message": message}]})

    return LLM(httpx.AsyncClient(transport=httpx.MockTransport(respond)))


# --- what the model wrote and called, as a turn -----------------------------------------------------------

def test_the_gates_plain_text_is_its_answer_and_words_beside_a_delegate_are_its_note():
    assert gate_turn("Paris.", []) == {"actions": [{"tool": "answer", "text": "Paris."}]}
    assert gate_turn("Comparing the two.", [{"tool": "delegate"}])["actions"] == [{"tool": "delegate", "note": "Comparing the two."}]
    assert gate_turn("x", [{"tool": "delegate", "note": "Its own note."}])["actions"][0]["note"] == "Its own note."
    assert gate_turn("", []) == {"actions": []}  # nothing written, nothing called: not a turn


def test_the_orchestrators_text_is_a_reply_beside_calls_and_the_final_message_without_one():
    run = {"tool": "run_step", "step": "s1"}
    assert orchestrator_turn("Looking now.", [run]) == {"summary": "Looking now.", "reply": "Looking now.", "actions": [run]}
    assert orchestrator_turn("", [run]) == {"summary": "run step: s1", "actions": [run]}
    assert orchestrator_turn("", [{"tool": "look_up", "queries": ["gloves", "wraps"]}])["summary"] == "look up: gloves; wraps"
    # No call at all, or only ones that prepare: what it wrote ends the task.
    assert orchestrator_turn("Here they are.", [])["actions"] == [{"tool": "finish", "answer": "Here they are."}]
    kept = {"tool": "remember", "findings": ["Trains at Title Boxing"]}
    assert [a["tool"] for a in orchestrator_turn("Done.", [kept])["actions"]] == ["remember", "finish"]
    # A file goes on the phone for a step that uses it: the words are a reply and the next turn moves.
    put = {"tool": "put_on_phone", "files": ["file:2"]}
    said = "I'll put your photo on the phone and set it as your WhatsApp profile picture."
    assert orchestrator_turn(said, [put]) == {"summary": said, "reply": said, "actions": [put]}
    # A plan alone is left for the runtime to start, with the words as the note.
    planned = orchestrator_turn("Starting.", [{"tool": "set_plan", **PLAN}])
    assert [a["tool"] for a in planned["actions"]] == ["set_plan"] and planned["reply"] == "Starting."
    assert orchestrator_turn("", [{"tool": "set_plan", **PLAN}])["summary"] == "set plan: Find boxing gloves"
    assert orchestrator_turn("", [{"tool": "youtube", "videos": ["abc", "def"]}])["summary"] == "youtube: abc, def"


def test_words_beside_a_plan_never_end_the_task_whatever_else_the_turn_kept():
    """A plan is work still to do: findings kept in the same turn do not make its words the final message."""
    kept = {"tool": "remember", "findings": ["Benefits run on the employer's portal"]}
    plan = {"tool": "set_plan", **PLAN}
    note = {"tool": "note", "notes": "The mailbox has no policy mail: the portal is next"}
    said = "The mail has no policy document: I'm opening the benefits portal."
    for calls in ([kept, plan], [plan, kept], [note, kept, plan]):
        turn = orchestrator_turn(said, calls)
        assert "finish" not in [a["tool"] for a in turn["actions"]]
        assert turn["reply"] == said and turn["summary"] == said
    assert [a["tool"] for a in orchestrator_turn(said, [note, kept, plan])["actions"]] == ["note", "remember", "set_plan"]


def test_notes_to_itself_go_first_and_change_nothing_else_about_the_turn():
    kept = {"tool": "note", "notes": "Ruled out the camp stove: Sam already has one (Amazon order email)"}
    run = {"tool": "run_step", "step": "s1"}
    # Wherever among the calls the model wrote it.
    assert orchestrator_turn("", [run, kept]) == {"summary": "run step: s1", "actions": [kept, run]}
    planned = orchestrator_turn("Starting.", [kept, {"tool": "set_plan", **PLAN}])
    assert [a["tool"] for a in planned["actions"]] == ["note", "set_plan"] and planned["reply"] == "Starting."
    assert [a["tool"] for a in orchestrator_turn("Here they are.", [kept])["actions"]] == ["note", "finish"]
    assert orchestrator_turn("", [kept]) == {"summary": "note", "actions": [kept]}
    assert orchestrator_notes({"actions": [kept, run]}) == [kept["notes"]] and orchestrator_notes({"actions": [run]}) == []


def test_a_note_is_plain_text_however_the_model_lays_it_out():
    """`notes` is one text, which every provider quotes, so it cannot arrive broken: as a list, Sonnet 5.5 wrote a
    bare sentence where the list belonged and the turn was lost (2026-11-22). Its lines are what is kept and shown."""
    schema = next(f["function"] for f in ORCHESTRATOR.functions if f["function"]["name"] == "note")["parameters"]
    assert schema["properties"]["notes"]["type"] == "string" and schema["required"] == ["notes"]
    one = "Diagnostic answered from look_up only, no steps."
    run = {"tool": "run_step", "step": "s1"}
    for written, lines in (
        (one, [one]),  # the sentence that broke the list
        ("- Pick: the hiking daypack\n- After s2: ask which to order", ["Pick: the hiking daypack", "After s2: ask which to order"]),
        ("1. First\n\n2) Second   point\n", ["First", "Second point"]),
        ("3.5 stars on Amazon\n-5 C tonight", ["3.5 stars on Amazon", "-5 C tonight"]),  # not bullets
        ([one, "Not checked: Target"], [one, "Not checked: Target"]),  # the earlier list shape still fits
        ("", []),  # nothing kept, nothing refused
        ("x" * 5000, ["x" * 1800]),  # cut, not refused
    ):
        turn = ORCHESTRATOR.schema.model_validate(orchestrator_turn("", [{"tool": "note", "notes": written}, run]))
        assert orchestrator_notes(turn.model_dump()) == lines
    # A note still being written (the live preview) reads the same way.
    assert orchestrator_notes(orchestrator_turn("", [{"tool": "note", "notes": "- Pick: the bu"}])) == ["Pick: the bu"]
    assert orchestrator_notes(orchestrator_turn("", [{"tool": "note"}])) == []


def test_a_message_in_the_call_is_not_said_twice():
    twice = orchestrator_turn("Which size?", [{"tool": "ask_user", "question": "Which size? 12 oz or 16 oz."}])
    assert "reply" not in twice and twice["summary"] == "Asking the user"
    other = orchestrator_turn("Found three pairs.", [{"tool": "finish", "answer": "The Hayabusa T3 is the pick."}])
    assert other["reply"] == "Found three pairs." and other["summary"] == "Answering the user"
    # A call still being written has no arguments yet: the preview is the turn so far, never an error.
    assert orchestrator_turn("Which", [{"tool": "ask_user"}])["actions"] == [{"tool": "ask_user"}]


# --- DeepSeek (chat completions) ----------------------------------------------------------------------------

async def test_the_orchestrator_is_sent_its_tools_and_its_calls_become_the_turn():
    seen = []
    llm = chat(seen, ("Looking for gloves now.", [("set_plan", PLAN), ("run_step", {"step": "s1"})]))
    turn = await llm.ask(ORCHESTRATOR, {"x": 1})
    assert [type(a) for a in turn.actions] == [SetPlan, RunStep] and turn.reply == "Looking for gloves now."
    payload = seen[0]
    assert payload["tool_choice"] == "auto" and "response_format" not in payload
    assert payload["thinking"] == {"type": "enabled"} and payload["reasoning_effort"] == "max"
    tools = {t["function"]["name"]: t["function"]["parameters"] for t in payload["tools"]}
    assert set(tools) == {"set_plan", "remember", "put_on_phone", "note", "run_step", "verify_step", "ask_user", "finish",
                          "look_up", "read_memory", "youtube"}
    assert "$ref" not in json.dumps(payload["tools"]) and "tool" not in tools["finish"]["properties"]
    assert tools["look_up"]["required"] == ["queries"]  # optional stays optional: there is no grammar to shrink
    system = payload["messages"][0]["content"]
    assert ORCHESTRATOR.instructions in system and ORCHESTRATOR.calling in system
    assert schema_text(ORCHESTRATOR.schema) not in system and "Return JSON" not in system
    assert payload["messages"][1]["content"] == '{"x": 1}'


async def test_a_turn_without_a_call_is_the_final_message_not_a_fault():
    turn = await chat([], ("The Hayabusa T3 16 oz is the pick.", [])).ask(ORCHESTRATOR, {})
    assert [type(a) for a in turn.actions] == [Finish] and turn.actions[0].answer == "The Hayabusa T3 16 oz is the pick."
    answer = await chat([], ("Paris.", [])).ask(GATE, {})
    assert isinstance(answer.actions[0], Answer) and answer.actions[0].text == "Paris."
    handed = await chat([], ("Comparing the two now.", [("delegate", {"reason": "needs the phone"})])).ask(GATE, {})
    assert isinstance(handed.actions[0], Delegate) and handed.actions[0].note == "Comparing the two now."


async def test_a_call_that_does_not_fit_its_tool_is_asked_for_again_once(caplog):
    seen = []
    llm = chat(seen, ("", [("run_step", {"stage": "s1", "secret": "the user's words"})]), ("", [("run_step", {"step": "s1"})]))
    turn = await llm.ask(ORCHESTRATOR, {"x": 1})
    assert turn.actions[0].step == "s1"
    again = seen[1]["messages"][1]["content"]
    assert again.startswith('{"x": 1}') and "could not be applied (actions.0.run_step.step: Field required)" in again
    assert "run_step.step: missing" in caplog.text and "the user's words" not in caplog.text

    for bad in (("", [("run_step", "{not json")]), ("", [])):  # arguments that are not JSON; an empty turn
        with pytest.raises(ProviderError, match="invalid structured output"):
            await chat([], bad, bad).ask(ORCHESTRATOR, {})


def piece(finish=None, **delta):
    return ("data: " + json.dumps({"choices": [{"delta": delta, "finish_reason": finish}]}) + "\n\n").encode()


def call_piece(arguments, name=None):
    return piece(tool_calls=[{"index": 0, "function": {**({"name": name} if name else {}), "arguments": arguments}}])


class GatedCalls(httpx.AsyncByteStream):
    def __init__(self, gate):
        self.gate = gate

    async def __aiter__(self):
        yield piece(reasoning_content="Private deliberation")
        yield call_piece('{"question": "Which size', name="ask_user")
        await self.gate.wait()
        yield call_piece(' do you wear?"}')
        yield piece(finish="tool_calls")
        yield b"data: [DONE]\n\n"


async def test_a_call_shows_while_its_arguments_are_still_being_written():
    gate, ready, updates = asyncio.Event(), asyncio.Event(), []

    async def update(partial):
        updates.append(partial)
        if partial.get("actions"):
            ready.set()

    async def think(text, reset=False):
        pass

    llm = LLM(httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=GatedCalls(gate)))))
    task = asyncio.create_task(llm.ask(ORCHESTRATOR, {}, on_update=update, on_thinking=think))
    try:
        await asyncio.wait_for(ready.wait(), 2)
        assert not task.done() and updates[-1]["actions"] == [{"tool": "ask_user", "question": "Which size"}]
        assert ORCHESTRATOR.voice(updates[-1]) == "Which size"  # what the chat shows meanwhile
        gate.set()
        turn = await task
        assert isinstance(turn.actions[0], AskUser) and turn.actions[0].question == "Which size do you wear?"
        assert "Private deliberation" not in json.dumps(updates)
    finally:
        gate.set()
        await asyncio.gather(task, return_exceptions=True)
        await llm.close()


# --- OpenAI (the Responses API) -----------------------------------------------------------------------------

def response(text="", calls=(), status="completed"):
    """A finished Responses API reply: a message with `text` and a function call per (name, arguments)."""
    output = ([{"type": "message", "content": [{"type": "output_text", "text": text}]}] if text else []) + [
        {"type": "function_call", "name": name, "arguments": json.dumps(arguments)} for name, arguments in calls]
    return {"status": status, "output": output, "usage": {"input_tokens": 10, "output_tokens": 5}, "created_at": 1}


def openai(seen, *replies):
    replies = list(replies)

    def respond(request):
        seen.append({"url": str(request.url), "body": json.loads(request.content)})
        reply = replies.pop(0)
        if isinstance(reply, bytes):
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=reply)
        return httpx.Response(200, json=reply)

    return LLM(httpx.AsyncClient(transport=httpx.MockTransport(respond)))


def events(*items):
    return "".join(f"event: {item['type']}\ndata: {json.dumps(item)}\n\n" for item in items).encode()


async def test_openai_takes_the_turn_through_the_responses_api_with_tools_that_are_not_strict(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "test")
    seen, used = [], []
    llm = openai(seen, response("Looking for gloves now.", [("set_plan", PLAN), ("run_step", {"step": "s1"})]),
                 response("Lace-ups suit sparring.", []))
    turn = await llm.ask(ORCHESTRATOR, {"x": 1}, provider="openai", model="gpt-6-luna", effort="low",
                         on_usage=lambda *usage: used.append(usage))
    assert [type(a) for a in turn.actions] == [SetPlan, RunStep] and turn.reply == "Looking for gloves now."
    body = seen[0]["body"]
    assert seen[0]["url"].endswith("/responses") and body["model"] == "gpt-6-luna" and "stream" not in body
    assert body["reasoning"] == {"effort": "low", "summary": "auto"} and body["tool_choice"] == "auto" and body["store"] is False
    assert len(body["tools"]) == 11 and all(t["type"] == "function" and t["strict"] is False for t in body["tools"])
    developer, user = body["input"]
    assert ORCHESTRATOR.calling in developer["content"][0]["text"] and "prompt_cache_breakpoint" in developer["content"][0]
    assert user["content"] == [{"type": "input_text", "text": '{"x": 1}'}]
    assert used == [("openai", "gpt-6-luna", {"input_tokens": 10, "output_tokens": 5}, 1)]
    # Text alone ends the task here too; the gate's tier model and effort apply without picks.
    final = await llm.ask(ORCHESTRATOR, {}, provider="openai")
    assert isinstance(final.actions[0], Finish) and seen[1]["body"]["model"] == settings.openai_heavy_model
    with pytest.raises(ProviderError, match="token limit"):
        await openai([], response("Cut off", [], status="incomplete")).ask(ORCHESTRATOR, {}, provider="openai")


async def test_openais_turn_streams_its_words_its_calls_and_its_reasoning_summary(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "test")
    updates, thoughts, seen = [], [], []

    async def on_update(partial):
        updates.append(partial)

    async def on_thinking(text, reset=False):
        thoughts.append(text)

    done = response("", [("ask_user", {"question": "Which size do you wear?"})])
    stream = events(
        {"type": "response.created", "response": {}},
        {"type": "response.reasoning_summary_text.delta", "delta": "Sizes differ."},
        {"type": "response.output_item.added", "output_index": 1, "item": {"type": "function_call", "name": "ask_user"}},
        {"type": "response.function_call_arguments.delta", "output_index": 1, "delta": '{"question": "Which size'},
        {"type": "response.some_event_from_the_future", "anything": True},
        {"type": "response.completed", "response": done},
    )
    turn = await openai(seen, stream).ask(ORCHESTRATOR, {}, on_update=on_update, on_thinking=on_thinking, provider="openai")
    assert isinstance(turn.actions[0], AskUser) and turn.actions[0].question == "Which size do you wear?"
    assert seen[0]["body"]["stream"] is True and "Sizes differ." in "".join(thoughts)
    assert any(a.get("tool") == "ask_user" for u in updates for a in u.get("actions", []))  # shown while it is written

    for ending in ({"type": "response.failed", "response": {"error": {"message": "The model is overloaded."}}}, None):
        broken = events({"type": "response.output_text.delta", "delta": "Half"}, *([ending] if ending else []))
        with pytest.raises(ProviderError, match="overloaded" if ending else "ended before completion"):
            await openai([], broken).ask(ORCHESTRATOR, {}, on_update=on_update, provider="openai")


# --- Claude -------------------------------------------------------------------------------------------------

async def test_claude_is_sent_tools_and_no_output_format():
    seen, updates = [], []

    async def on_update(partial):
        updates.append(partial)

    llm = llm_with(claude(seen, stream_body([
        {"type": "thinking", "thinking": "hmm"},
        {"type": "text", "text": "Looking for gloves now."},
        {"type": "tool_use", "id": "t1", "name": "set_plan", "input": PLAN},
        {"type": "tool_use", "id": "t2", "name": "run_step", "input": {"step": "s1"}},
    ], stop_reason="tool_use", model="claude-opus-5-5")))
    turn = await llm.ask(ORCHESTRATOR, {"x": 1}, on_update=on_update, provider="anthropic")
    assert [type(a) for a in turn.actions] == [SetPlan, RunStep] and turn.reply == "Looking for gloves now."
    assert ORCHESTRATOR.voice(updates[-1]) == "Looking for gloves now."  # the chat shows its words while it writes
    body = seen[0]["body"]
    assert body["output_config"] == {"effort": "high"}  # no format: nothing is compiled to a grammar
    assert body["tool_choice"] == {"type": "auto"} and body["fallbacks"] == "default"
    tools = {t["name"]: t for t in body["tools"]}
    assert len(tools) == 11 and all(t["eager_input_streaming"] and "strict" not in t for t in tools.values())
    assert tools["look_up"]["input_schema"]["required"] == ["queries"]
    assert ORCHESTRATOR.calling in body["system"][0]["text"] and "cache_control" in body["system"][0]
    assert body["messages"][0]["content"][-1]["text"] == '{"x": 1}'


async def test_claudes_turn_of_text_alone_is_the_final_message():
    llm = llm_with(claude([], stream_body([{"type": "text", "text": "The Hayabusa T3 is the pick."}], model="claude-opus-5-5")))
    turn = await llm.ask(ORCHESTRATOR, {}, provider="anthropic")
    assert isinstance(turn.actions[0], Finish) and turn.actions[0].answer == "The Hayabusa T3 is the pick."
    assert turn.summary == "Answering the user"


async def test_claudes_misfit_call_is_asked_for_again_after_the_cached_parts():
    seen = []
    llm = llm_with(claude(seen, stream_body([{"type": "tool_use", "id": "t", "name": "run_step", "input": {}}], stop_reason="tool_use"),
                          stream_body([{"type": "tool_use", "id": "t", "name": "run_step", "input": {"step": "s1"}}], stop_reason="tool_use")))
    turn = await llm.ask(ORCHESTRATOR, {"x": 1}, provider="anthropic")
    assert turn.actions[0].step == "s1"
    first, again = (request["body"]["messages"][0]["content"] for request in seen)
    assert [block["text"] for block in again[:len(first)]] == [block["text"] for block in first]
    assert "could not be applied" in again[-1]["text"] and "cache_control" not in again[-1]


async def test_claudes_call_that_is_not_json_is_asked_for_again():
    # Eager input streaming passes arguments on unchecked: Opus 5.5 once wrote `{"notes": s` and the SDK raised mid-stream.
    broken = stream_body([{"type": "tool_use", "id": "t", "name": "run_step", "input": {"notes": "X"}}], stop_reason="tool_use")
    broken = broken.replace(b'\\"X\\"', b"s")
    seen = []
    llm = llm_with(claude(seen, broken, stream_body([{"type": "tool_use", "id": "t", "name": "run_step", "input": {"step": "s1"}}],
                                                     stop_reason="tool_use")))
    turn = await llm.ask(ORCHESTRATOR, {"x": 1}, provider="anthropic")
    assert turn.actions[0].step == "s1" and len(seen) == 2
    assert "run_step were not valid JSON" in seen[1]["body"]["messages"][0]["content"][-1]["text"]
