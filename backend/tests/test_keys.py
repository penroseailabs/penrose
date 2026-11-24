"""The API keys: only their last four characters leave the API, and a new key goes to the desktop app."""

import httpx
import pytest
from app import api
from app.api import keys
from app.config import settings
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "deepseek_api_key", "")
    monkeypatch.setattr(settings, "openai_api_key", "sk-openai-1234")
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant-abcd")
    monkeypatch.setattr(settings, "desktop_control_url", "http://control")
    monkeypatch.setattr(settings, "desktop_control_token", "secret")
    return TestClient(api.app)


def control(monkeypatch, handler):
    """Answer the API's calls to the desktop app's control server with `handler`."""
    real = httpx.AsyncClient
    monkeypatch.setattr(
        keys.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
    )


def test_lists_only_the_last_four_characters(client):
    assert client.get("/api/keys").json() == {"deepseek": "", "openai": "…1234", "anthropic": "…abcd"}


def test_saving_hands_the_key_to_the_app(client, monkeypatch):
    seen = []

    def handler(request):
        seen.append((request.url.path, request.headers["authorization"], request.read()))
        return httpx.Response(200, json={"ok": True, "keys": {"deepseek": "…9999", "openai": "", "anthropic": ""}})

    control(monkeypatch, handler)
    response = client.put("/api/keys/deepseek", json={"key": "sk-new-9999"})
    assert response.status_code == 200
    assert response.json()["keys"]["deepseek"] == "…9999"
    assert "sk-new-9999" not in response.text
    path, auth, body = seen[0]
    assert path == "/keys" and auth == "Bearer secret"
    assert b'"provider":"deepseek"' in body.replace(b" ", b"") and b"sk-new-9999" in body


def test_a_key_the_provider_refuses_is_a_400_with_the_reason(client, monkeypatch):
    control(monkeypatch, lambda request: httpx.Response(200, json={"ok": False, "error": "This key was not accepted."}))
    response = client.put("/api/keys/openai", json={"key": "nope"})
    assert response.status_code == 400
    assert response.json()["detail"] == "This key was not accepted."


def test_an_unreachable_app_is_a_502(client, monkeypatch):
    def handler(request):
        raise httpx.ConnectError("down")

    control(monkeypatch, handler)
    assert client.put("/api/keys/openai", json={"key": "sk-x"}).status_code == 502


def test_without_the_desktop_app_keys_cannot_be_saved(client, monkeypatch):
    monkeypatch.setattr(settings, "desktop_control_url", "")
    assert client.put("/api/keys/openai", json={"key": "sk-x"}).status_code == 501


def test_unknown_provider(client):
    assert client.put("/api/keys/gemini", json={"key": "x"}).status_code == 404
