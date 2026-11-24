"""The desktop app's backend pieces: the API serving the web UI, the gateway token, the one-program entry."""

import httpx
import pytest
from app.config import settings
from fastapi.testclient import TestClient


@pytest.fixture
def ui(tmp_path, monkeypatch):
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<html>relay</html>")
    (tmp_path / "assets" / "app.js").write_text("console.log(1)")
    (tmp_path.parent / "secret.txt").write_text("outside")
    monkeypatch.setattr(settings, "frontend_dir", tmp_path)
    return tmp_path


def api_client(store, monkeypatch):
    from app import api

    monkeypatch.setattr(api.common, "store", store)
    return TestClient(api.app)


def test_the_api_serves_the_built_ui_and_its_routes(store, monkeypatch, ui):
    client = api_client(store, monkeypatch)
    assert client.get("/assets/app.js").text == "console.log(1)"
    assert client.get("/").text == "<html>relay</html>"
    assert client.get("/chats/abc").text == "<html>relay</html>"  # the app's own routes get index.html
    assert client.get("/../secret.txt").text != "outside"
    assert client.get("/api/nope").status_code == 404


def test_without_a_ui_folder_nothing_but_the_api_is_served(store, monkeypatch):
    monkeypatch.setattr(settings, "frontend_dir", None)
    assert api_client(store, monkeypatch).get("/").status_code == 404


def test_signing_in_guards_the_api_but_not_the_ui_files(store, monkeypatch, ui):
    monkeypatch.setattr(settings, "login_username", "u")
    monkeypatch.setattr(settings, "login_password", "p")
    with api_client(store, monkeypatch) as client:  # startup creates the sessions table
        assert client.get("/").status_code == 200
        assert client.get("/api/chats").status_code == 401
        assert client.post("/api/auth/login", json={"username": "u", "password": "p"}).status_code == 200
        assert client.get("/api/chats").status_code == 200


async def test_the_gateway_requires_its_token_when_one_is_set(monkeypatch):
    from app import gateway

    monkeypatch.setattr(settings, "gateway_token", "secret")
    transport = httpx.ASGITransport(app=gateway.app)
    async with httpx.AsyncClient(base_url="http://gateway", transport=transport) as client:
        assert (await client.get("/health")).status_code == 401
        assert (await client.get("/health", headers={"X-Gateway-Token": "wrong"})).status_code == 401
        assert (await client.get("/health", headers=settings.gateway_headers)).status_code == 200


async def test_the_gateway_is_open_without_a_token(monkeypatch):
    from app import gateway

    monkeypatch.setattr(settings, "gateway_token", "")
    async with httpx.AsyncClient(base_url="http://gateway", transport=httpx.ASGITransport(app=gateway.app)) as client:
        assert (await client.get("/health")).status_code == 200


def test_provision_status_reports_an_empty_home(tmp_path, capsys):
    import json

    from app.desktop import main

    assert main(["provision", "status", "--home", str(tmp_path), "--sdk", str(tmp_path / "sdk")]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["event"] == "status"
    assert not status["sdk_ready"] and not status["golden_ready"] and not status["phone_ready"]
