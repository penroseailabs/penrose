"""What each model request cost, in credits (1 credit = 1 US cent), kept per request and summed per chat."""

import json
from datetime import datetime, timezone

import httpx
from app.agents.llm import LLM
from app.agents.registry import GATE, SUBAGENT
from app.config import settings
from app.pricing import add_up, cost, is_peak
from app.store import upgraded
from test_anthropic import claude, stream_body
from fakes import Script, make_state, orch, runner_for
from test_api import api_client

USAGE = {"prompt_tokens": 1000, "completion_tokens": 200, "total_tokens": 1200,
         "prompt_cache_hit_tokens": 600, "prompt_cache_miss_tokens": 400,
         "prompt_tokens_details": {"cached_tokens": 600}, "completion_tokens_details": {"reasoning_tokens": 50}}


def at(text):
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()


def test_peak_is_weekday_utc_windows_only(monkeypatch):
    monkeypatch.setattr(settings, "deepseek_holidays", "")
    assert is_peak(at("2026-10-21T02:30"))  # Wednesday, 01-04 UTC
    assert is_peak(at("2026-10-21T09:59"))
    assert not is_peak(at("2026-10-21T04:00"))  # between the windows
    assert not is_peak(at("2026-10-21T12:00"))
    assert not is_peak(at("2026-10-24T02:30"))  # Saturday
    monkeypatch.setattr(settings, "deepseek_holidays", "2026-10-21")
    assert not is_peak(at("2026-10-21T02:30"))  # a Chinese public holiday, by Beijing date


def test_cost_uses_cache_hit_miss_and_output_prices_halved_off_peak():
    peak, is_p = cost("deepseek", "deepseek-flash", USAGE, at("2026-10-21T02:30"))
    # 600 x 0.006 + 400 x 0.30 + 200 x 1.20 USD per 1M tokens, in cents
    assert is_p and abs(peak - (600 * 0.006 + 400 * 0.30 + 200 * 1.20) / 1e4) < 1e-12
    off, is_p = cost("deepseek", "deepseek-flash", USAGE, at("2026-10-24T02:30"))
    assert not is_p and abs(off - peak / 2) < 1e-12
    assert cost("deepseek", "some-new-model", USAGE, at("2026-10-24T02:30"))[0] is None


def test_usage_json_sums_every_field_nested_too():
    total = add_up(add_up({}, USAGE), USAGE)
    assert total["prompt_tokens"] == 2000 and total["completion_tokens_details"] == {"reasoning_tokens": 100}


async def test_a_streamed_answer_reports_its_usage(monkeypatch):
    monkeypatch.setattr(settings, "deepseek_api_key", "test")
    seen, heard = [], []

    def respond(request):
        seen.append(json.loads(request.content))
        last = {"created": 1792800000, "usage": USAGE,
                "choices": [{"delta": {"content": json.dumps({"actions": [{"tool": "delegate"}]})}, "finish_reason": "stop"}]}
        return httpx.Response(200, content=("data: " + json.dumps(last) + "\n\ndata: [DONE]\n\n").encode())

    llm = LLM(httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    async def on_update(partial):
        pass
    await llm.ask(GATE, {}, on_update=on_update, on_usage=lambda *a: heard.append(a))
    assert seen[0]["stream_options"] == {"include_usage": True}
    assert heard == [("deepseek", settings.fast_model, USAGE, 1792800000)]


async def test_a_phone_turn_reports_its_usage(monkeypatch):
    monkeypatch.setattr(settings, "deepseek_api_key", "test")
    heard = []

    def respond(request):
        message = {"content": "", "tool_calls": [{"id": "0", "type": "function",
                                                   "function": {"name": "report", "arguments": "{}"}}]}
        return httpx.Response(200, json={"created": 5, "usage": USAGE,
                                         "choices": [{"finish_reason": "tool_calls", "message": message}]})

    llm = LLM(httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    await llm.act(SUBAGENT, "Screen", on_usage=lambda *a: heard.append(a))
    assert heard == [("deepseek", settings.subagent_model, USAGE, 5)]


async def test_each_request_is_kept_and_the_chat_shows_the_total(store, monkeypatch):
    state = make_state(store, route=None)
    model = Script(Orchestrator=lambda c, n: orch({"tool": "ask_user", "question": "Which day?"}))
    model.usage = ("deepseek", "deepseek-flash", USAGE, at("2026-10-21T02:30"))
    await runner_for(store, model).run(state)
    one, _ = cost("deepseek", "deepseek-flash", USAGE, at("2026-10-21T02:30"))
    with api_client(store, monkeypatch) as client:
        detail = client.get("/api/chats/chat").json()
    requests = detail["requests"]
    assert requests and all(r["peak"] and r["model"] == "deepseek-flash" for r in requests)
    assert "Orchestrator" in {r["stage"] for r in requests}
    total = detail["usage"]
    assert total["requests"] == len(requests) and total["peak_requests"] == len(requests)
    assert abs(total["credits"] - one * len(requests)) < 1e-9
    assert total["usage"]["deepseek"]["prompt_tokens"] == 1000 * len(requests)
    assert total["tokens"]["input"] == 1000 * len(requests) and requests[0]["tokens"]["cached"] == 600
    assert any(r["kind"] == "usage" for r in store.rows("SELECT kind FROM events"))


def close(a, b):
    return abs(a - b) < 1e-9


def test_openai_prices_reads_writes_and_long_context():
    usage = {"input_tokens": 10_000, "output_tokens": 500,
             "input_tokens_details": {"cached_tokens": 4_000, "cache_write_tokens": 3_000,
                                      "cache_write_tokens_details": {"ttl_30m_tokens": 2_000, "ttl_12h_tokens": 1_000}},
             "output_tokens_details": {"reasoning_tokens": 200}}
    credits, peak = cost("openai", "gpt-6-luna", usage, at("2026-10-21T02:30"))
    # 3,000 plain x $0.10 + 4,000 read x $0.01 + 2,000 x $0.125 + 1,000 x $0.25 + 500 out x $0.50, per 1M, in cents
    assert not peak and close(credits, (3000 * 0.10 + 4000 * 0.01 + 2000 * 0.125 + 1000 * 0.25 + 500 * 0.50) / 1e4)
    long = {"input_tokens": 300_000, "output_tokens": 0}
    assert close(cost("openai", "gpt-6.1-sol", long, 0)[0], 300_000 * 4.00 / 1e4)
    # chat completions names, and a dated snapshot priced as its model
    chat = {"prompt_tokens": 1_000, "completion_tokens": 100, "prompt_tokens_details": {"cached_tokens": 400}}
    assert close(cost("openai", "gpt-6-sol-2026-09-01", chat, 0)[0], (600 * 2.00 + 400 * 0.20 + 100 * 10.00) / 1e4)


def test_claude_prices_reads_writes_and_haikus_long_prompts():
    usage = {"input_tokens": 1_000, "cache_read_input_tokens": 8_000, "cache_creation_input_tokens": 2_000,
             "cache_creation": {"ephemeral_5m_input_tokens": 2_000, "ephemeral_1h_input_tokens": 0},
             "output_tokens": 300}
    credits, _ = cost("anthropic", "claude-opus-5-5", usage, 0)
    assert close(credits, (1000 * 4.00 + 2000 * 5.00 + 8000 * 0.20 + 300 * 20.00) / 1e4)
    # Haiku 5.5: a prompt over 100K (cache reads included) pays the higher row
    big = {"input_tokens": 1_000, "cache_read_input_tokens": 120_000, "output_tokens": 100}
    assert close(cost("anthropic", "claude-haiku-5-5", big, 0)[0], (1000 * 0.50 + 120_000 * 0.05 + 100 * 2.50) / 1e4)
    assert cost("anthropic", "claude-unknown-9", usage, 0)[0] is None


async def test_an_openai_phone_turn_reports_its_usage(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "test")
    heard, raw = [], {"input_tokens": 50, "output_tokens": 5}

    def respond(request):
        return httpx.Response(200, json={"created_at": 7, "usage": raw, "output": [
            {"type": "function_call", "name": "report", "arguments": "{}"}]})

    llm = LLM(httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    await llm.act(SUBAGENT, "Screen", provider="openai", on_usage=lambda *a: heard.append(a))
    assert heard == [("openai", settings.openai_phone_model, raw, 7)]


async def test_a_claude_turn_reports_its_usage_with_the_answering_model(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "test")
    heard = []
    llm = LLM()
    llm.claude = claude([], stream_body([{"type": "text", "text": json.dumps({"actions": [{"tool": "delegate"}]})}],
                                        model="claude-haiku-5-5"))
    await llm.ask(GATE, {}, provider="anthropic", on_usage=lambda *a: heard.append(a))
    provider, model, usage, created = heard[0]
    assert (provider, model, created) == ("anthropic", "claude-haiku-5-5", None)
    assert usage["input_tokens"] == 10 and usage["output_tokens"] == 5


def test_a_chat_priced_before_other_providers_keeps_its_deepseek_total():
    old = {"requests": 1, "credits": 0.1, "usage": {"prompt_tokens": 5}}
    assert upgraded(old)["usage"] == {"deepseek": {"prompt_tokens": 5}}
    assert upgraded({"usage": {"openai": {"input_tokens": 1}}})["usage"] == {"openai": {"input_tokens": 1}}
