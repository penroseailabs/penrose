"""Claude through the Anthropic SDK, against a mock transport that speaks the Messages streaming protocol."""

import json
from dataclasses import replace

import anthropic
import httpx2
import pytest
from app.agents.anthropic_llm import FALLBACK_BETA, OPTIONAL_LIMIT, Claude, json_text, output_format
from app.agents.llm import LLM, Parts, context_parts
from app.agents.registry import AGENTS, GATE, ORCHESTRATOR, SUBAGENT
from app.config import settings
from app.errors import ProviderError

ANSWER = {"actions": [{"tool": "answer", "text": "pong"}]}


# These are the JSON turn's tests (how memory, title, compaction and the like answer), on the gate's and the
# orchestrator's turn shapes without their `compose`; the two answering with tool calls, as they do, are in
# test_tool_call_turns.py.
GATE, ORCHESTRATOR = replace(GATE, compose=None), replace(ORCHESTRATOR, compose=None)


def event(kind, **data):
    return f"event: {kind}\ndata: {json.dumps({'type': kind, **data})}\n\n"


def stream_body(blocks, stop_reason="end_turn", model="claude-haiku-5-5"):
    """SSE for one message whose content is `blocks` ({"type": "text", "text"} / {"type": "tool_use", ...})."""
    out = [event("message_start", message={
        "id": "msg_1", "type": "message", "role": "assistant", "model": model, "content": [],
        "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 0},
    })]
    for i, block in enumerate(blocks):
        if block["type"] == "text":
            out.append(event("content_block_start", index=i, content_block={"type": "text", "text": ""}))
            out.append(event("content_block_delta", index=i, delta={"type": "text_delta", "text": block["text"]}))
        elif block["type"] == "thinking":
            out.append(event("content_block_start", index=i, content_block={"type": "thinking", "thinking": "", "signature": ""}))
            out.append(event("content_block_delta", index=i, delta={"type": "thinking_delta", "thinking": block["thinking"]}))
            out.append(event("content_block_delta", index=i, delta={"type": "signature_delta", "signature": "sig"}))
        else:
            out.append(event("content_block_start", index=i, content_block={
                "type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}))
            out.append(event("content_block_delta", index=i, delta={
                "type": "input_json_delta", "partial_json": json.dumps(block["input"])}))
        out.append(event("content_block_stop", index=i))
    out.append(event("message_delta", delta={"stop_reason": stop_reason, "stop_sequence": None},
                     usage={"output_tokens": 5}))
    out.append(event("message_stop"))
    return "".join(out).encode()


def claude(seen, *replies, status=200):
    """A Claude client whose requests land in `seen` and are answered by `replies` in order: a stream body, or
    (status, message) for an error."""
    replies = list(replies)

    def respond(request):
        seen.append({"url": str(request.url), "headers": dict(request.headers), "body": json.loads(request.content)})
        if status != 200:
            return httpx2.Response(status, json={"type": "error", "error": {"type": "x", "message": "no"}})
        reply = replies.pop(0)
        if isinstance(reply, tuple):
            return httpx2.Response(reply[0], json={"type": "error", "error": {"type": "invalid_request_error", "message": reply[1]}})
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=reply)

    client = anthropic.AsyncAnthropic(api_key="test", max_retries=0,
                                      http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(respond)))
    return Claude(client)


def llm_with(claude_client):
    llm = LLM()
    llm.claude = claude_client
    return llm


async def test_a_json_turn_is_parsed_and_streamed():
    seen, updates, thoughts = [], [], []
    llm = llm_with(claude(seen, stream_body([{"type": "thinking", "thinking": "hmm"}, {"type": "text", "text": json.dumps(ANSWER)}])))

    async def on_update(partial):
        updates.append(partial)

    async def on_thinking(text, reset=False):
        thoughts.append(text)

    turn = await llm.ask(GATE, {"x": 1}, on_update=on_update, on_thinking=on_thinking, provider="anthropic")
    assert turn.actions[0].text == "pong"
    assert updates[-1] == ANSWER and "hmm" in thoughts
    body = seen[0]["body"]
    assert body["model"] == settings.anthropic_fast_model
    assert body["output_config"]["effort"] == "low"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert GATE.instructions in body["system"][0]["text"]
    assert body["messages"][0]["content"][-1]["text"] == '{"x": 1}'
    assert "temperature" not in body


async def test_the_orchestrator_gets_opus_with_server_side_fallbacks():
    seen = []
    out = {"summary": "s", "actions": [{"tool": "finish", "answer": "done"}]}
    llm = llm_with(claude(seen, stream_body([{"type": "text", "text": "```json\n" + json.dumps(out) + "\n```"}], model="claude-opus-5-5")))
    turn = await llm.ask(ORCHESTRATOR, {}, provider="anthropic")
    assert turn.summary == "s"
    body, headers = seen[0]["body"], seen[0]["headers"]
    assert body["model"] == settings.anthropic_heavy_model == "claude-opus-5-5"
    assert body["fallbacks"] == "default" and FALLBACK_BETA in headers["anthropic-beta"]
    assert body["output_config"]["effort"] == "high"


async def test_a_context_in_parts_is_cached_up_to_each_part_and_reads_as_one_object():
    seen = []
    llm = llm_with(claude(seen, stream_body([{"type": "text", "text": json.dumps(ANSWER)}])))
    context = Parts({"user": "Ana", "skills": ["x"]})
    context.end()
    context["looked_up"] = [{"query": "fridge"}]
    context.end()
    context.update({"plan": [], "clock": "now"})
    await llm.ask(GATE, context, provider="anthropic")
    blocks = seen[0]["body"]["messages"][0]["content"]
    assert [("cache_control" in b) for b in blocks] == [True, True, False]
    assert "".join(b["text"] for b in blocks) == json.dumps(context)
    assert blocks[1]["text"].startswith(', "looked_up"')


def test_parts_that_end_nowhere_new_send_one_piece():
    context = Parts({"a": 1})
    context.end()
    context.end()  # nothing added since: no empty part
    assert context_parts(context) == ['{"a": 1}']
    assert context_parts({"a": 1, "b": 2}) == ['{"a": 1, "b": 2}']


def test_turn_schemas_become_structured_output_formats():
    schema = output_format(ORCHESTRATOR.schema)["schema"]
    assert schema["additionalProperties"] is False
    assert '"const"' not in json.dumps(schema) and '"enum": ["finish"]' in json.dumps(schema)
    # `facts` is a map with free keys, which a structured-output schema cannot say: that turn stays prompted.
    assert output_format(SUBAGENT.schema) is None


def test_a_format_stays_within_claudes_optional_fields():
    """Claude's grammar about doubles with each optional field, and refused the orchestrator's 44 as too large:
    a schema over the limit has every field required, its default in its description for the model to write out.
    A schema within the limit keeps its optional fields, so its replies are no longer than before."""
    def optional(node):
        if isinstance(node, list):
            return [name for item in node for name in optional(item)]
        if not isinstance(node, dict):
            return []
        here = [name for name in node["properties"] if name not in node.get("required", [])] if (
            node.get("type") == "object" and isinstance(node.get("properties"), dict)) else []
        return here + [name for value in node.values() for name in optional(value)]

    for agent in AGENTS:
        fmt = output_format(agent.schema)
        assert fmt is None or len(optional(fmt["schema"])) <= OPTIONAL_LIMIT, agent.name
    assert optional(output_format(ORCHESTRATOR.schema)["schema"]) == []
    assert "spoken" in optional(output_format(GATE.schema)["schema"])
    look_up = output_format(ORCHESTRATOR.schema)["schema"]["$defs"]["LookUp"]
    assert look_up["required"] == list(look_up["properties"]) and "default: False" in look_up["properties"]["apps"]["description"]
    # A turn with every default written out is the same turn.
    turn = ORCHESTRATOR.schema.model_validate({"summary": "s", "reply": "", "actions": [
        {"tool": "look_up", "queries": ["fridge"], "email": False, "apps": False}]})
    assert turn == ORCHESTRATOR.schema.model_validate({"summary": "s", "actions": [{"tool": "look_up", "queries": ["fridge"]}]})


async def test_a_refused_format_is_left_out_and_the_schema_stays_prompted():
    seen = []
    llm = llm_with(claude(seen, (400, "The compiled grammar is too large, which would cause performance issues."),
                          stream_body([{"type": "text", "text": json.dumps(ANSWER)}]),
                          stream_body([{"type": "text", "text": json.dumps(ANSWER)}])))
    assert (await llm.ask(GATE, {}, provider="anthropic")).actions[0].text == "pong"
    assert (await llm.ask(GATE, {}, provider="anthropic")).actions[0].text == "pong"
    assert ["format" in request["body"]["output_config"] for request in seen] == [True, False, False]


async def test_a_request_refused_for_another_reason_says_anthropics_reason():
    seen = []
    llm = llm_with(claude(seen, (400, "prompt is too long: 1200000 tokens"), (400, "prompt is too long: 1200000 tokens")))
    with pytest.raises(ProviderError, match="HTTP 400: prompt is too long"):
        await llm.ask(GATE, {}, provider="anthropic")
    assert GATE.schema in llm.claude.formats and llm.claude.formats[GATE.schema]  # the format was not the reason: kept


def test_the_json_object_is_found_inside_prose_or_a_fence():
    assert json_text('Here it is:\n```json\n{"a": {"b": 1}}\n```\nDone.') == '{"a": {"b": 1}}'
    assert json_text('I {think} so. {"a": 1}') == '{"a": 1}'
    assert json_text('Sure {"a": "x', partial=True) == '{"a": "x'
    assert json_text("no json here") == "no json here"


async def test_invalid_json_gets_one_corrected_retry():
    seen = []
    llm = llm_with(claude(seen, stream_body([{"type": "text", "text": "not json"}]),
                          stream_body([{"type": "text", "text": json.dumps(ANSWER)}])))
    turn = await llm.ask(GATE, {}, provider="anthropic")
    assert turn.actions[0].text == "pong"
    retry = seen[1]["body"]["messages"]
    assert [m["role"] for m in retry] == ["user", "assistant", "user"]


async def test_a_phone_turn_returns_native_tool_calls_with_images():
    seen = []
    llm = llm_with(claude(seen, stream_body([
        {"type": "text", "text": "Opening it."},
        {"type": "tool_use", "id": "t1", "name": "interact", "input": {"element": 4, "interaction": "tap"}},
    ])))
    content, calls, *_ = await llm.act(SUBAGENT, "Screen: ...", images=[b"\xff\xd8x\xff\xd9"], provider="anthropic")
    assert content == "Opening it."
    assert calls == [{"name": "interact", "arguments": {"element": 4, "interaction": "tap"}}]
    body = seen[0]["body"]
    assert body["model"] == settings.anthropic_phone_model
    assert body["messages"][0]["content"][0]["type"] == "image"
    names = {t["name"] for t in body["tools"]}
    assert {"interact", "report"} <= names
    assert all("input_schema" in t for t in body["tools"])


async def test_phone_turns_with_thinking_off_force_a_tool_on_haiku(monkeypatch):
    monkeypatch.setattr(settings, "phone_thinking", False)
    seen = []
    llm = llm_with(claude(seen, stream_body([{"type": "tool_use", "id": "t", "name": "report", "input": {}}])))
    await llm.act(SUBAGENT, "x", provider="anthropic")
    body = seen[0]["body"]
    assert body["thinking"] == {"type": "disabled"} and body["tool_choice"] == {"type": "any"}


async def test_a_refusal_is_a_provider_error():
    llm = llm_with(claude([], stream_body([], stop_reason="refusal")))
    with pytest.raises(ProviderError, match="declined"):
        await llm.ask(GATE, {}, provider="anthropic")


async def test_a_bad_key_is_reported_without_details():
    llm = llm_with(claude([], status=401))
    with pytest.raises(ProviderError, match="rejected the API key"):
        await llm.act(SUBAGENT, "x", provider="anthropic")


async def test_a_missing_key_says_where_to_add_it(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    with pytest.raises(ProviderError, match="Settings"):
        await LLM().ask(GATE, {}, provider="anthropic")


def test_a_list_that_grows_is_cut_after_each_item_and_cached_up_to_its_last():
    """The text up to the mark is exactly what the next call, with one more item, sends before that item."""
    def pieces(looked_up, youtube=None):
        context = Parts({"user": "Ana"})
        context.end()
        if youtube:
            context["youtube"] = youtube
            context.grows("youtube")
        context["looked_up"] = looked_up
        context.grows("looked_up")
        context.update({"plan": [], "clock": "now"})
        parts = context_parts(context)
        assert "".join(parts) == json.dumps(context)
        return parts

    one, two = pieces([[{"q": "a"}]]), pieces([[{"q": "a"}], [{"q": "b"}]])
    assert [p.cache for p in one] == [True, True, False]
    assert one[1].endswith('[{"q": "a"}]') and one[2].startswith('], "plan"')
    cached = "".join(one[:2])  # what the first call asked Claude to keep
    assert "".join(two).startswith(cached) and cached in ("".join(two[:n]) for n in range(len(two) + 1))
    assert [p.cache for p in two] == [True, False, True, False]
    with_video = pieces([[{"q": "a"}], [{"q": "b"}]], youtube=[{"read": "x"}])
    assert [p.cache for p in with_video] == [True, True, False, True, False]
    assert with_video[1] == ', "youtube": [{"read": "x"}'


async def test_at_most_three_pieces_are_marked_beside_the_system_prompt():
    seen = []
    llm = llm_with(claude(seen, stream_body([{"type": "text", "text": json.dumps(ANSWER)}])))
    context = Parts({"user": "Ana"})
    context.end()
    context["recipes"] = ["r"]
    context.end()
    for key in ("youtube", "looked_up"):
        context[key] = [{"n": 1}, {"n": 2}]
        context.grows(key)
    context["plan"] = []
    await llm.ask(GATE, context, provider="anthropic")
    blocks = seen[0]["body"]["messages"][0]["content"]
    assert "".join(b["text"] for b in blocks) == json.dumps(context)
    assert [("cache_control" in b) for b in blocks] == [False, True, False, True, False, True, False]
