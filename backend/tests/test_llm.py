"""The model client (one call is one agent turn) and how the runner journals and publishes turns."""

import asyncio
import json
import sqlite3
from dataclasses import replace

import httpx
import pytest
from app.agents.llm import LLM, schema_text
from app.agents.registry import AGENTS, GATE, ORCHESTRATOR, SUBAGENT, VERIFIER
from app.config import settings
from app.errors import ProviderError
from app.store import Store
from fakes import Script, finish, make_state, orch, runner_for

JPEG = b"\xff\xd8fake-jpeg\xff\xd9"
ANSWER = {"actions": [{"tool": "answer", "text": "pong"}]}


# These are the JSON turn's tests (how memory, title, compaction and the like answer), on the gate's and the
# orchestrator's turn shapes without their `compose`; the two answering with tool calls, as they do, are in
# test_tool_call_turns.py.
GATE, ORCHESTRATOR = replace(GATE, compose=None), replace(ORCHESTRATOR, compose=None)


def sse(content="", finish=None, **delta):
    return ("data: " + json.dumps({"choices": [{"delta": {"content": content, **delta}, "finish_reason": finish}]}) + "\n\n").encode()


def replying(seen, content):
    def respond(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(content)}}]})

    return LLM(httpx.AsyncClient(transport=httpx.MockTransport(respond)))


@pytest.mark.parametrize("agent,model,thinking,effort", [
    (GATE, "fast_model", "disabled", None),
    (ORCHESTRATOR, "heavy_model", "enabled", "max"),
])
async def test_each_tier_gets_its_model_thinking_and_effort(monkeypatch, agent, model, thinking, effort):
    monkeypatch.setattr(settings, "deepseek_api_key", "test")
    seen = []
    content = ANSWER if agent is GATE else {"summary": "s", "actions": [finish()]}
    llm = replying(seen, content)
    try:
        await llm.ask(agent, {})
    finally:
        await llm.close()
    payload = seen[0]
    assert payload["model"] == getattr(settings, model)
    assert payload["thinking"]["type"] == thinking
    assert payload.get("reasoning_effort") == effort


async def test_the_prompt_is_the_agents_markdown_file_and_its_schema(monkeypatch):
    monkeypatch.setattr(settings, "deepseek_api_key", "test")
    seen = []
    await replying(seen, ANSWER).ask(GATE, {"x": 1})
    system = seen[0]["messages"][0]["content"]
    assert GATE.instructions in system and schema_text(GATE.schema) in system
    assert '"title"' not in schema_text(GATE.schema)
    assert seen[0]["messages"][1]["content"] == '{"x": 1}'


def calling(seen, *calls, content="", reasoning=None):
    """A model that answers with native tool calls: (name, arguments as sent, JSON text or not), with its
    reasoning when it thinks."""
    def respond(request):
        seen.append(json.loads(request.content))
        tool_calls = [{"id": str(i), "type": "function", "function": {"name": name, "arguments": arguments}}
                      for i, (name, arguments) in enumerate(calls)]
        message = {"content": content, "tool_calls": tool_calls}
        if reasoning is not None:
            message["reasoning_content"] = reasoning
        return httpx.Response(200, json={"choices": [{"finish_reason": "tool_calls", "message": message}]})

    return LLM(httpx.AsyncClient(transport=httpx.MockTransport(respond)))


async def test_a_phone_agent_answers_with_native_tool_calls_and_no_thinking(monkeypatch):
    monkeypatch.setattr(settings, "deepseek_api_key", "test")
    monkeypatch.setattr(settings, "phone_thinking", False)
    seen = []
    llm = calling(seen, ("interact", '{"element": 4, "interaction": "tap"}'), ("note", "{not json"))
    content, calls, *_ = await llm.act(SUBAGENT, "Screen:\n[4] Button", images=[JPEG])
    payload = seen[0]
    assert payload["model"] == settings.subagent_model and payload["thinking"] == {"type": "disabled"}
    assert payload["tool_choice"] == "required" and payload["temperature"] == 0
    assert payload["max_tokens"] == settings.phone_max_tokens and "reasoning_effort" not in payload
    assert payload["messages"][0]["content"].startswith(SUBAGENT.instructions)
    user = payload["messages"][1]["content"]
    assert user[0] == {"type": "text", "text": "Screen:\n[4] Button"}
    assert user[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    tools = {t["function"]["name"]: t["function"] for t in payload["tools"]}
    assert {"interact", "open_app", "request_screenshot", "log", "read_page", "report", "checkpoint"} <= set(tools) and "tap_at" not in tools
    assert "finish" not in tools  # the harness's own ending: the subagent ends with report
    assert "tool" not in tools["report"]["parameters"]["properties"] and tools["report"]["description"].startswith("End your work")
    assert calls == [{"name": "interact", "arguments": {"element": 4, "interaction": "tap"}},
                     {"name": "note", "arguments": {}, "invalid_json": "{not json"}]
    assert content == ""


async def test_phone_agents_think_at_low_effort_by_default(monkeypatch):
    monkeypatch.setattr(settings, "deepseek_api_key", "test")
    seen = []
    _, _, thought, _ = await calling(seen, ("report", '{"status": "completed", "summary": "ok"}'),
                                  reasoning="  The cart shows it. Report. ").act(SUBAGENT, "state")
    assert thought == "The cart shows it. Report."
    payload = seen[0]
    assert payload["thinking"] == {"type": "enabled"} and payload["tool_choice"] == "auto"
    assert payload["reasoning_effort"] == settings.subagent_reasoning_effort and "temperature" not in payload


async def test_openai_phone_agents_reason_with_tools_through_the_responses_api(monkeypatch):
    # OpenAI's chat completions refuse function tools with a reasoning effort (HTTP 400).
    monkeypatch.setattr(settings, "openai_api_key", "test")
    seen = []

    def respond(request):
        seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"output": [
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "Settings is on the home screen."}]},
            {"type": "function_call", "name": "open_app", "arguments": '{"app": "Settings"}', "call_id": "c1"},
            {"type": "function_call", "name": "note", "arguments": "not json", "call_id": "c2"},
        ], "usage": {"input_tokens": 120, "output_tokens": 9, "input_tokens_details": {"cached_tokens": 100}}})

    llm = LLM(httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    content, calls, thought, usage = await llm.act(SUBAGENT, "state", images=[JPEG], provider="openai")
    path, payload = seen[0]
    assert path.endswith("/responses") and payload["tool_choice"] == "required"
    assert payload["model"] == settings.openai_phone_model == "gpt-6-luna"
    assert payload["reasoning"]["effort"] == settings.subagent_reasoning_effort
    assert payload["tools"][0]["type"] == "function" and "function" not in payload["tools"][0]
    # Only the instructions are written to OpenAI's cache: the screen state after them changes every turn.
    developer, user = payload["input"]
    assert payload["prompt_cache_options"] == {"mode": "explicit"} and "instructions" not in payload
    assert developer["role"] == "developer" and developer["content"][0]["text"].startswith(SUBAGENT.instructions)
    assert developer["content"][0]["prompt_cache_breakpoint"] == {"mode": "explicit"}
    assert "prompt_cache_breakpoint" not in json.dumps(user)
    assert user["content"][1]["image_url"].startswith("data:image/jpeg;base64,")
    assert content == "" and thought == "Settings is on the home screen."
    assert calls == [{"name": "open_app", "arguments": {"app": "Settings"}},
                     {"name": "note", "arguments": {}, "invalid_json": "not json"}]
    assert usage == {"prompt_tokens": 120, "completion_tokens": 9, "prompt_cache_hit_tokens": 100}


def test_openai_caches_only_the_instructions_and_deepseek_is_unchanged():
    messages = LLM.messages(GATE, {"request": "hi"})
    openai = LLM.payload(GATE, messages, "openai")
    system, user = openai["messages"]
    assert openai["prompt_cache_options"] == {"mode": "explicit"}
    assert system["content"] == [{"type": "text", "text": messages[0]["content"],
                                  "prompt_cache_breakpoint": {"mode": "explicit"}}]
    assert user == messages[1]
    deepseek = LLM.payload(GATE, messages, "deepseek")
    assert deepseek["messages"] == messages and "prompt_cache_options" not in deepseek


async def test_a_provider_error_says_the_providers_reason(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "test")
    llm = LLM(httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(400, json={"error": {"message": "Unknown model."}}))))
    with pytest.raises(ProviderError, match="HTTP 400: Unknown model."):
        await llm.act(SUBAGENT, "state", provider="openai")


def test_a_read_only_agent_is_offered_only_looking_interactions():
    interact = next(t for t in VERIFIER.functions if t["function"]["name"] == "interact")
    assert interact["function"]["parameters"]["properties"]["interaction"]["enum"][0] == "tap"
    # It may type to search; the runtime takes typing only into a search box (runtime.phone.searchable).
    assert not {"long_press", "set_value", "choose"} & set(interact["function"]["parameters"]["properties"]["interaction"]["enum"])
    assert not {"type_and_submit", "set_setting", "tap_at", "drag"} & {t["function"]["name"] for t in VERIFIER.functions}


async def test_a_picture_goes_to_whichever_model_is_picked_and_its_api_says_if_it_cannot_read_it(monkeypatch):
    monkeypatch.setattr(settings, "deepseek_api_key", "test")
    seen = []
    await replying(seen, orch(finish())).ask(ORCHESTRATOR, {}, images=[JPEG])
    assert "image_url" in json.dumps(seen[0])  # no list of which models see pictures is kept here
    refusing = LLM(httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(400, json={"error": {"message": "Image input is not supported."}}))))
    with pytest.raises(ProviderError, match="HTTP 400: Image input is not supported."):
        await refusing.ask(ORCHESTRATOR, {}, images=[JPEG])


async def test_invalid_output_is_retried_once_with_the_problem(monkeypatch):
    monkeypatch.setattr(settings, "deepseek_api_key", "test")
    replies = iter([{"actions": [{"tool": "answer"}]}, ANSWER])
    seen = []

    def respond(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(next(replies))}}]})

    turn = await LLM(httpx.AsyncClient(transport=httpx.MockTransport(respond))).ask(GATE, {})
    assert turn.actions[0].text == "pong"
    assert "did not match the JSON schema" in seen[1]["messages"][-1]["content"]
    assert "text" in seen[1]["messages"][-1]["content"]


@pytest.mark.parametrize("reply", [
    '{"actions": [{"tool": "answer", "text": "line one\nline two\tend"}]}',  # raw newline and tab inside a string
    'Here it is:\n```json\n{"actions": [{"tool": "answer", "text": "pong"}]}\n```',  # fenced
    '{"actions": [{"tool": "answer", "text": "pong"}]} Done.',  # a word after it
])
async def test_a_reply_that_holds_the_whole_object_is_read_without_a_retry(monkeypatch, reply):
    monkeypatch.setattr(settings, "deepseek_api_key", "test")
    seen = []

    def respond(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": reply}}]})

    turn = await LLM(httpx.AsyncClient(transport=httpx.MockTransport(respond))).ask(GATE, {})
    assert turn.actions[0].text.startswith(("pong", "line one"))
    assert len(seen) == 1


async def test_a_reply_in_tool_call_markup_is_retried_with_the_reply_and_logged_by_tag_only(monkeypatch, caplog):
    monkeypatch.setattr(settings, "deepseek_api_key", "test")
    markup = '[{"x": 1}]\n<｜DSML｜tool_calls><｜DSML｜invoke name="answer">secret words</｜DSML｜invoke>'
    replies = iter([markup, json.dumps(ANSWER)])
    seen = []

    def respond(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": next(replies)}}]})

    with caplog.at_level("WARNING"):
        turn = await LLM(httpx.AsyncClient(transport=httpx.MockTransport(respond))).ask(GATE, {})
    assert turn.actions[0].text == "pong"
    retry = seen[1]["messages"]
    assert retry[-2] == {"role": "assistant", "content": markup}  # it sees what it wrote
    assert "never as tool-call markup" in retry[-1]["content"]
    assert "markup <｜DSML｜tool_calls" in caplog.text and "secret words" not in caplog.text


class GatedStream(httpx.AsyncByteStream):
    def __init__(self, gate):
        self.gate = gate

    async def __aiter__(self):
        yield sse(reasoning_content="Private deliberation must not reach events")
        yield sse('{"actions":[{"tool":"answer","text":"I will check')
        await self.gate.wait()
        yield sse(' availability."}]}')
        yield sse(finish="stop")
        yield b"data: [DONE]\n\n"


async def test_a_partial_answer_streams_before_completion_and_reasoning_stays_apart(monkeypatch):
    monkeypatch.setattr(settings, "deepseek_api_key", "test")
    gate, ready = asyncio.Event(), asyncio.Event()
    updates, thinking = [], []

    async def update(partial):
        updates.append(partial)
        if partial.get("actions"):
            ready.set()

    async def think(text, reset=False):
        thinking.append((text, reset))

    llm = LLM(httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=GatedStream(gate)))))
    task = asyncio.create_task(llm.ask(GATE, {}, on_update=update, on_thinking=think))
    try:
        await asyncio.wait_for(ready.wait(), 2)
        assert not task.done() and updates[-1]["actions"][0]["text"] == "I will check"
        gate.set()
        turn = await task
        assert turn.actions[0].text == "I will check availability."
        assert "".join(t for t, _ in thinking) == "Private deliberation must not reach events"
        assert "Private deliberation" not in json.dumps(updates)
    finally:
        gate.set()
        await asyncio.gather(task, return_exceptions=True)
        await llm.close()


@pytest.mark.parametrize("end", [b"data: [DONE]\n\n", sse(finish="length")])
async def test_an_incomplete_or_truncated_stream_never_passes(monkeypatch, end):
    monkeypatch.setattr(settings, "deepseek_api_key", "test")

    async def update(partial):
        pass

    llm = LLM(httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=sse('{"actions":[') + end))))
    with pytest.raises(ProviderError):
        await llm.ask(GATE, {}, on_update=update)


def test_every_agent_has_a_prompt_and_its_tools():
    for agent in AGENTS:
        assert agent.instructions and "{{" not in agent.instructions
        for tool in agent.tools:
            assert tool.__doc__ and tool.tool_name(), tool
        if agent.acts:  # phone agents: native function definitions, each described
            names = [t["function"]["name"] for t in agent.functions]
            assert len(names) == len(set(names)) and all(t["function"]["description"] for t in agent.functions)
        else:
            assert agent.schema.model_json_schema()["properties"]["actions"]


# --- how the runner publishes turns ------------------------------------------------------------------

async def test_the_gate_hands_off_silently_and_the_orchestrator_speaks(store):
    state = make_state(store, route=None)
    model = Script(Orchestrator=lambda c, n: orch({"tool": "ask_user", "question": "Which day should I check?"}))
    await runner_for(store, model).run(state)
    assert state["status"] == "waiting_user"
    messages = store.rows("SELECT id, content, source FROM messages")
    assert [(m["content"], m["source"]) for m in messages] == [("Which day should I check?", "Orchestrator")]
    event = store.rows("SELECT payload FROM events WHERE kind='agent_message'")[0]
    assert json.loads(event["payload"])["message"]["id"] == messages[0]["id"]


async def test_subagent_turns_never_become_chat_messages(store):
    state = make_state(store)

    class Model:
        async def act(self, agent, text, **kwargs):
            return "", [{"name": "report", "arguments": {"status": "completed", "summary": text}}]

    runner = runner_for(store, Model())
    await asyncio.gather(*(runner.act(state, SUBAGENT, name, node_id=name) for name in ("one", "two")))
    outputs = [json.loads(r["payload"]) for r in store.rows("SELECT payload FROM events WHERE kind='stage_output'")]
    assert len({o["call_id"] for o in outputs}) == 2
    # The trace shows a phone turn's calls as its actions, in the shape the other agents' turns have.
    assert outputs[0]["output"]["actions"][0]["tool"] == "report"
    assert outputs[0]["output"]["summary"].startswith("report(status=completed")
    assert store.rows("SELECT * FROM messages") == []


async def test_a_cancelled_stream_does_not_keep_a_partial_reply(store):
    state = make_state(store)
    ready = asyncio.Event()

    class Model:
        async def ask(self, agent, context, on_update=None, **kwargs):
            await on_update({"actions": [{"tool": "answer", "text": "Unfinished"}]})
            ready.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(runner_for(store, Model()).call(state, GATE, {}))
    await asyncio.wait_for(ready.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.rows("SELECT * FROM messages") == []
    assert store.rows("SELECT * FROM events WHERE kind='stage_error'")


async def test_progress_messages_do_not_displace_the_users_words(store):
    state = make_state(store, route=None)
    store.message("chat", "user", "I need vegetarian options.")
    for i in range(40):
        store.message("chat", "assistant", f"Progress {i}", source="Subagent")

    def gate(context, n):
        assert context["conversation"] == [{"role": "user", "content": "I need vegetarian options."}]
        return {"actions": [{"tool": "answer", "text": "Vegetarian it is."}]}

    await runner_for(store, Script(**{"Fast response": gate})).run(state)
    assert state["status"] == "completed"


def test_an_existing_message_database_migrates_idempotently(tmp_path):
    with sqlite3.connect(tmp_path / "agent.sqlite3") as db:
        db.execute("CREATE TABLE messages(id TEXT PRIMARY KEY,chat_id TEXT,role TEXT,content TEXT,created REAL)")
        db.execute("INSERT INTO messages VALUES ('old','chat','assistant','Previous answer',1)")
    store = Store(tmp_path)
    Store(tmp_path)
    assert store.rows("SELECT * FROM messages")[0]["content"] == "Previous answer"
    assert store.message("chat", "assistant", "New", source="Fast response")["source"] == "Fast response"
