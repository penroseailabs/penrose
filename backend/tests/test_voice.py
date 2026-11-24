"""Voice mode: the front desk places what the user said, the agents write what to say aloud, and the voice API
hands out a transcription secret and speaks replies."""

import json
import time

import httpx
import pytest
from app import delivery
from app.agents import registry
from app.api import chats as chats_api
from app.api import common, voice
from app.companion import tunnel
from app.config import settings
from app.plan import new_step
from app.runtime import frontdesk
from app.store import uid
from fakes import Script, finish, make_state, orch, runner_for, step_spec
from fastapi.testclient import TestClient


def put_run(store, chat_id, status, **extra):
    """A saved run of `chat_id` in `status`."""
    state = {
        "id": uid(),
        "chat_id": chat_id,
        "request": "test",
        "status": status,
        "created": time.time(),
        "plan": [],
        "nodes": {},
        "questions": [],
        **extra,
    }
    store.execute(
        "INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?)",
        (state["id"], chat_id, status, json.dumps(state), time.time()),
    )
    return state


def waiting_for_code(store, chat_id="c1"):
    """A conversation whose task waits for a private code at step s1."""
    store.execute("INSERT INTO chats VALUES (?,?,?)", (chat_id, "Target sign-in", 1.0))
    step = {
        **new_step(step_spec("s1")),
        "status": "waiting_user",
        "question": {"question": "Enter the code Target sent", "sensitive": True},
    }
    return put_run(
        store,
        chat_id,
        "waiting_user",
        plan=[step],
        questions=[{"node_id": "s1", "question": "Enter the code Target sent", "sensitive": True}],
    )


def two_chats(store):
    store.execute("INSERT INTO chats VALUES (?,?,?)", ("trip", "New Zealand trip", 10.0))
    store.execute("INSERT INTO chats VALUES (?,?,?)", ("milk", "Instacart order", 20.0))
    store.message("trip", "user", "Plan two weeks in New Zealand in March")
    store.message(
        "trip",
        "assistant",
        "Here is a draft itinerary: Auckland, Rotorua, Queenstown.",
        source="Orchestrator",
    )
    store.message("milk", "user", "Order my usual milk on Instacart")
    put_run(store, "trip", "completed")
    put_run(store, "milk", "completed")


# --- digits as people say them ----------------------------------------------------------------------------------


def test_digits_reads_a_spoken_code_and_nothing_else():
    rows = [
        ("2793", "2793"),
        ("two seven nine three", "2793"),
        ("the code is 27 93", "2793"),
        ("it's two, seven, nine, three", "2793"),
        ("oh four one two seven", "04127"),
        ("my pin is 1 2 3 4", "1234"),
        ("order two pizzas", ""),
        ("279", ""),  # too short to be a code
        ("call 2793 and ask", ""),
    ]
    for said, code in rows:
        assert frontdesk.digits(said) == code


# --- the rules: a code for the waiting step; nothing to choose from ------------------------------------------


def test_a_spoken_code_goes_to_the_focus_conversations_waiting_step_by_rule(store):
    waiting_for_code(store)
    decision = frontdesk.by_rule(store, "two seven nine three", "c1")
    assert decision.chat_id == "c1" and decision.code and decision.decided_by == "rule"


def test_words_that_are_not_a_code_are_left_to_the_front_desk(store):
    waiting_for_code(store)
    assert frontdesk.by_rule(store, "use my email instead", "c1") is None


def test_with_no_conversations_a_new_one_starts_without_asking_a_model(store):
    decision = frontdesk.by_rule(store, "order milk", None)
    assert decision.chat_id == "" and not decision.code


# --- the front desk agent -------------------------------------------------------------------------------------


async def test_the_front_desk_reads_the_focus_the_recent_and_the_related_conversations(store):
    two_chats(store)
    seen = {}

    def route(context, n):
        seen.update(context)
        return {
            "actions": [
                {
                    "tool": "route",
                    "chat_id": "trip",
                    "acknowledgment": "Adding that to your New Zealand trip.",
                }
            ]
        }

    model = Script(Front_desk=route)
    decision = await frontdesk.decide(
        model, store, "add Milford Sound to the New Zealand plan", "milk", "deepseek", None
    )
    assert decision.chat_id == "trip" and not decision.code and decision.decided_by == "front desk"
    assert decision.acknowledgment == "Adding that to your New Zealand trip."
    assert seen["utterance"] == "add Milford Sound to the New Zealand plan"
    assert seen["focus"]["id"] == "milk" and seen["focus"]["conversation"][0]["content"].startswith(
        "Order my usual"
    )
    assert seen["focus"]["status"] == "completed"
    assert [c["id"] for c in seen["recent"]] == ["trip"]
    assert seen["related"] and seen["related"][0]["id"] == "trip"
    assert model.calls[0]["provider"] == "deepseek"


async def test_the_front_desk_runs_on_the_agents_picked_provider(store):
    two_chats(store)
    model = Script(
        Front_desk=lambda c, n: {"actions": [{"tool": "route", "acknowledgment": "On it."}]}
    )
    picks = {
        "agent": {"provider": "anthropic", "model": "claude-opus-5-5", "effort": "high"},
        "subagent": {"provider": "anthropic", "model": "claude-haiku-5-5", "effort": "low"},
    }
    decision = await frontdesk.decide(
        model, store, "book a table for two tonight", None, "anthropic", picks
    )
    assert decision.chat_id == "" and model.calls[0]["provider"] == "anthropic"


async def test_an_unknown_conversation_id_starts_a_new_one(store):
    two_chats(store)
    model = Script(
        Front_desk=lambda c, n: {
            "actions": [{"tool": "route", "chat_id": "nope", "acknowledgment": "Sure."}]
        }
    )
    decision = await frontdesk.decide(model, store, "something", None)
    assert decision.chat_id == ""


async def test_a_waiting_question_is_shown_to_the_front_desk(store):
    waiting_for_code(store)
    two_chats(store)
    seen = {}

    def route(context, n):
        seen.update(context)
        return {
            "actions": [
                {
                    "tool": "route",
                    "chat_id": "c1",
                    "code": True,
                    "acknowledgment": "Sending the code.",
                }
            ]
        }

    decision = await frontdesk.decide(
        Script(Front_desk=route), store, "the code is 2 7 9 3", "trip"
    )
    assert [w["id"] for w in seen["waiting"]] == ["c1"]
    assert seen["waiting"][0]["question"]["sensitive"] is True
    assert decision.chat_id == "c1" and decision.code


async def test_when_the_front_desk_fails_the_focus_conversation_continues(store):
    two_chats(store)

    def broken(context, n):
        raise RuntimeError("provider down")

    decision = await frontdesk.decide(Script(Front_desk=broken), store, "and make it two", "milk")
    assert (
        decision.chat_id == "milk" and decision.decided_by == "fallback" and decision.acknowledgment
    )


# --- what the agents say aloud ----------------------------------------------------------------------------------


def test_the_spoken_form_comes_from_the_tool_or_the_short_note():
    assert (
        registry.gate_spoken(
            {"actions": [{"tool": "answer", "text": "**Hi**", "spoken": "Hi there."}]}
        )
        == "Hi there."
    )
    assert (
        registry.gate_spoken({"actions": [{"tool": "delegate", "note": "Checking Instacart."}]})
        == "Checking Instacart."
    )
    assert (
        registry.orchestrator_spoken(
            {"reply": "Logging in.", "actions": [{"tool": "run_step", "step": "s1"}]}
        )
        == "Logging in."
    )
    assert (
        registry.orchestrator_spoken(
            {
                "reply": "",
                "actions": [
                    {"tool": "finish", "answer": "| a | b |", "spoken": "Done: two items."}
                ],
            }
        )
        == "Done: two items."
    )


async def test_a_voice_run_tells_the_agents_and_keeps_what_they_say_aloud(store):
    store.execute("INSERT INTO chats VALUES (?,?,?)", ("c1", "New conversation", 1.0))
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        state = delivery.deliver(db, "c1", "order milk", "deepseek", channel="voice")
    assert state["channel"] == "voice"
    assert store.rows("SELECT source FROM messages WHERE role='user'")[0]["source"] == "voice"
    seen = {}

    def gate(context, n):
        seen.update(context)
        return {
            "actions": [
                {
                    "tool": "answer",
                    "text": "Milk is **in the cart**.",
                    "spoken": "Your milk is in the cart.",
                }
            ]
        }

    runner = runner_for(store, Script(Fast_response=gate))
    await runner.run(store.load(state["id"]))
    assert seen["voice"] is True
    message = store.rows("SELECT content, spoken FROM messages WHERE role='assistant'")[0]
    assert (
        message["content"] == "Milk is **in the cart**."
        and message["spoken"] == "Your milk is in the cart."
    )
    done = [
        json.loads(r["payload"])
        for r in store.rows("SELECT payload FROM events WHERE kind='completed'")
    ][0]
    assert done["spoken"] == "Your milk is in the cart."


async def test_a_written_run_is_not_told_about_voice(store):
    store.execute("INSERT INTO chats VALUES (?,?,?)", ("c1", "New conversation", 1.0))
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        state = delivery.deliver(db, "c1", "order milk", "deepseek")
    seen = {}

    def gate(context, n):
        seen.update(context)
        return {"actions": [{"tool": "answer", "text": "Done."}]}

    await runner_for(store, Script(Fast_response=gate)).run(store.load(state["id"]))
    assert "voice" not in seen and "channel" not in state
    assert store.rows("SELECT spoken FROM messages WHERE role='assistant'")[0]["spoken"] is None


async def test_finish_carries_its_spoken_form(store):
    state = make_state(store, request="order milk", channel="voice", trigger="new_request")
    model = Script(Orchestrator=[orch({**finish("All **done**."), "spoken": "All done."})])
    await runner_for(store, model).run(state)
    done = [
        json.loads(r["payload"])
        for r in store.rows("SELECT payload FROM events WHERE kind='completed'")
    ][0]
    assert done == {"reply": "All **done**.", "spoken": "All done."}
    assert (
        store.rows("SELECT spoken FROM messages WHERE role='assistant'")[0]["spoken"] == "All done."
    )


# --- markdown as speech -----------------------------------------------------------------------------------------


def test_plain_takes_the_markdown_out_of_a_reply():
    text = (
        "## Found it\n\n"
        "The **cheapest** is [Horizon Organic](https://x.example/a) at $4.29.\n\n"
        "| Item | Price |\n|---|---|\n| Milk | $4.29 |\n| Eggs | $3.10 |\n\n"
        "- added to cart\n- *not* paid\n\n"
        "```\ncode\n```\n\nSee image:2."
    )
    spoken = voice.plain(text)
    assert spoken.splitlines() == [
        "Found it",
        "The cheapest is Horizon Organic at $4.29.",
        "Item, Price.",
        "Milk, $4.29.",
        "Eggs, $3.10.",
        "added to cart",
        "not paid",
        "See .",
    ]


# --- the API ---------------------------------------------------------------------------------------------------


@pytest.fixture
def client(store, monkeypatch):
    monkeypatch.setattr(common, "store", store)
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    monkeypatch.setattr(voice, "model", None)
    from app.api import app

    with TestClient(app) as c:
        yield c


def mock_openai(monkeypatch, handler):
    real = httpx.AsyncClient
    monkeypatch.setattr(
        voice.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
    )


def test_listen_mints_a_transcription_secret_for_the_browser(client, store, monkeypatch):
    store.set_user_locale("Europe/Amsterdam", "nl-NL")
    asked = {}

    def handler(request):
        asked["url"] = str(request.url)
        asked["auth"] = request.headers["authorization"]
        asked["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"value": "ek_abc", "expires_at": 1700000000, "session": {}}
        )

    mock_openai(monkeypatch, handler)
    out = client.post("/api/voice/listen").json()
    assert (
        out["secret"] == "ek_abc"
        and out["url"] == "wss://api.openai.com/v1/realtime?intent=transcription"
    )
    assert (
        asked["url"] == "https://api.openai.com/v1/realtime/client_secrets"
        and asked["auth"] == "Bearer sk-test"
    )
    session = asked["body"]["session"]
    assert session["type"] == "transcription"
    assert session["audio"]["input"]["transcription"] == {
        "model": settings.voice_transcribe_model,
        "language": "nl",
    }
    assert session["audio"]["input"]["turn_detection"]["type"] == "server_vad"
    assert asked["body"]["expires_after"]["seconds"] == voice.SECRET_SECONDS


def test_voice_needs_the_openai_key(client, monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "")
    assert client.post("/api/voice/listen").status_code == 503
    assert client.post("/api/voice/speak", json={"text": "hello"}).status_code == 503


def test_an_utterance_starts_a_conversation_and_comes_back_with_the_acknowledgment(
    client, store, monkeypatch
):
    two_chats(store)
    monkeypatch.setattr(
        voice,
        "model",
        Script(
            Front_desk=lambda c, n: {
                "actions": [{"tool": "route", "acknowledgment": "Looking for a table."}]
            }
        ),
    )
    out = client.post(
        "/api/voice/utterance",
        json={"text": "book a table for two tonight", "focus_chat_id": "milk"},
    ).json()
    assert (
        out["kind"] == "started"
        and out["acknowledgment"] == "Looking for a table."
        and out["decided_by"] == "front desk"
    )
    assert out["chat_id"] not in ("trip", "milk") and out["status"] == "queued"
    saved = store.rows(
        "SELECT chat_id, source, content FROM messages WHERE role='user' ORDER BY created DESC"
    )[0]
    assert (
        saved["chat_id"] == out["chat_id"]
        and saved["source"] == "voice"
        and saved["content"] == "book a table for two tonight"
    )
    assert store.load(out["run_id"])["channel"] == "voice"
    assert (
        store.rows("SELECT title FROM chats WHERE id=?", (out["chat_id"],))[0]["title"]
        == "book a table for two tonight"
    )


def test_an_utterance_continues_the_conversation_the_front_desk_picks(client, store, monkeypatch):
    two_chats(store)
    monkeypatch.setattr(
        voice,
        "model",
        Script(
            Front_desk=lambda c, n: {
                "actions": [
                    {
                        "tool": "route",
                        "chat_id": "trip",
                        "acknowledgment": "Adding that to your trip.",
                    }
                ]
            }
        ),
    )
    out = client.post(
        "/api/voice/utterance", json={"text": "add Milford Sound", "focus_chat_id": "milk"}
    ).json()
    assert (
        out["kind"] == "continued"
        and out["chat_id"] == "trip"
        and out["title"] == "New Zealand trip"
    )
    # The finished task is over: a new one begins in the same conversation, which the orchestrator reads whole.
    runs = store.rows("SELECT id, status FROM runs WHERE chat_id='trip' ORDER BY updated DESC")
    assert [r["status"] for r in runs] == ["queued", "completed"] and runs[0]["id"] == out["run_id"]


def test_a_spoken_code_answers_the_waiting_step_privately(client, store):
    waiting_for_code(store)
    out = client.post(
        "/api/voice/utterance", json={"text": "two seven nine three", "focus_chat_id": "c1"}
    ).json()
    assert (
        out["kind"] == "code"
        and out["chat_id"] == "c1"
        and out["acknowledgment"] == "Sending the code."
    )
    assert store.rows("SELECT value FROM secrets")[0]["value"] == "2793"
    assert (
        store.rows("SELECT content FROM messages WHERE role='user' ORDER BY created DESC")[0][
            "content"
        ]
        == "[Private code provided]"
    )
    assert store.rows("SELECT status FROM runs")[0]["status"] == "queued"


def test_speak_streams_openai_speech_as_mp3(client, monkeypatch):
    asked = {}

    def handler(request):
        asked["body"] = json.loads(request.content)
        return httpx.Response(200, content=b"ID3mp3bytes", headers={"content-type": "audio/mpeg"})

    mock_openai(monkeypatch, handler)
    response = client.post("/api/voice/speak", json={"text": "Your **milk** is in the cart."})
    assert response.status_code == 200 and response.headers["content-type"].startswith("audio/mpeg")
    assert response.content == b"ID3mp3bytes"
    assert (
        asked["body"]["input"] == "Your milk is in the cart."
        and asked["body"]["response_format"] == "mp3"
    )
    assert (
        asked["body"]["voice"] == settings.voice_tts_voice
        and asked["body"]["model"] == settings.voice_tts_model
    )


def test_speak_reports_a_provider_refusal(client, monkeypatch):
    mock_openai(
        monkeypatch, lambda request: httpx.Response(401, json={"error": {"message": "bad key"}})
    )
    response = client.post("/api/voice/speak", json={"text": "hello"})
    assert response.status_code == 502 and "bad key" in response.json()["detail"]


def test_the_phone_may_use_voice_over_the_tunnel():
    assert tunnel.allowed("POST", "/api/voice/listen")
    assert tunnel.allowed("POST", "/api/voice/utterance")
    assert tunnel.allowed("POST", "/api/voice/speak")


def test_chats_deliver_passes_the_channel_on(store):
    store.execute("INSERT INTO chats VALUES (?,?,?)", ("c1", "New conversation", 1.0))
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        state = chats_api.deliver(db, "c1", "hi", "deepseek", channel="voice")
    assert state["channel"] == "voice"


def test_shorten_cuts_at_a_sentence_end():
    from app.api.voice import SPEAK_CHARS, shorten

    short = "One sentence. Two sentences."
    assert shorten(short) == short
    long = " ".join(f"Sentence number {i} has a few words in it." for i in range(200))
    cut = shorten(long)
    assert len(cut) <= SPEAK_CHARS
    assert cut.endswith(".")
    assert cut == long[: len(cut)]
    assert len(shorten("x" * 5000)) == SPEAK_CHARS  # no sentence end at all: a plain cut
