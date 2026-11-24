"""The setup doctor: what it sends the user's model (trimmed, no secrets or home folder) and what it may answer."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from app.agents import llm as llm_module
from app.errors import ProviderError
from app.provision import Layout, doctor


def test_it_hides_keys_and_the_home_folder():
    text = f"{Path.home()}/Library/x key=sk-abcdefghijklmnop12345 ok"
    assert doctor.scrub(text) == "~/Library/x key=[hidden] ok"


def test_the_log_it_sends_is_the_setup_story_without_percentages(tmp_path):
    lines = [{"event": "step", "id": "golden", "title": "Building"}]
    lines += [{"event": "progress", "id": "golden", "percent": p} for p in range(100)]
    lines += [{"event": "progress", "id": "golden", "percent": 80, "doing": "chrome"},
              {"event": "log", "text": "Chrome started without the bridge"},
              {"event": "error", "message": "The phone's final check did not pass.", "hint": "Press Try again."}]
    (tmp_path / "provision.log").write_text("\n".join(json.dumps(e) for e in lines))
    tail = doctor.log_tail(tmp_path)
    assert tail == ["STEP golden: Building", "part golden/chrome (80%)", "Chrome started without the bridge",
                    "ERROR The phone's final check did not pass. | Press Try again."]


def test_only_the_last_few_messages_are_sent_each_shortened():
    messages = [{"role": "user", "text": "x" * 2000}] * 20 + [{"role": "system", "text": "hi"}, {"text": ""}]
    kept = doctor.trim(messages)
    assert len(kept) == doctor.MAX_MESSAGES
    assert kept[-1] == {"role": "doctor", "text": "hi"}  # nothing speaks as anyone but the user or the doctor
    assert all(len(m["text"]) <= doctor.MAX_MESSAGE_CHARS for m in kept)


def test_without_a_key_it_says_how_to_get_help(monkeypatch, tmp_path):
    for _, env in doctor.PROVIDERS:
        monkeypatch.delenv(env, raising=False)
    answer = asyncio.run(doctor.ask(Layout(tmp_path), None, {"trigger": "question"}))
    assert answer["action"] == "none" and "API key" in answer["reply"]


@pytest.mark.parametrize("action", ["try_again", "reboot_the_phone_with_su"])
def test_it_answers_from_the_model_and_only_with_a_known_action(monkeypatch, tmp_path, action):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(llm_module.settings, "deepseek_api_key", "test")
    monkeypatch.setattr(doctor, "gather", lambda layout: {"computer": {"free_gb": 50}})
    sent = []
    reply = {"summary": "bridge missing after restore", "reply": "Press Try again.", "state": "failed", "action": action}

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(reply)}}]})

    real = httpx.AsyncClient
    monkeypatch.setattr(llm_module.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler)))
    request = {"trigger": "failed", "messages": [{"role": "user", "text": "Is this stuck?"}], "state": {"step": "golden"}}
    if action == "try_again":
        answer = asyncio.run(doctor.ask(Layout(tmp_path), None, request))
        assert answer["action"] == "try_again" and answer["reply"] == "Press Try again."
        context = json.loads(sent[0]["messages"][-1]["content"])
        assert context["trigger"] == "failed" and context["conversation"][0]["text"] == "Is this stuck?"
        assert "setup doctor" in sent[0]["messages"][0]["content"]
    else:  # an action off the menu is refused, so the app never runs something it does not know
        with pytest.raises(ProviderError):
            asyncio.run(doctor.ask(Layout(tmp_path), None, request))
