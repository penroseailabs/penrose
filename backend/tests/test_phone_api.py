"""The phone API behind the `relay` command (Claude Code, Cowork): one lease per session, looking and acting
through the screen driver, the bearer token, the discovery file; and the command's own text."""

import json
import re

import pytest
from app import cli
from app.api import auth, phone
from app.config import settings
from app.device.client import Slot
from fakes import FakeDevices, make_state
from fastapi.testclient import TestClient
from phone import FakePhone, element, field, label, snapshot

SEARCH = snapshot(field(1, hint="Search"), element(2, "Go"), element(3, "Cart"))
RESULTS = snapshot(label(100, "Results for oat milk"), element(4, "Add to cart"), activity="com.example.Results")


class LeasingDevices(FakeDevices):
    """One phone whose lease is real: the first owner keeps it until it is released."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.holder = None

    async def acquire(self, owner, wait=0):
        if self.holder not in (None, owner):
            return None
        slot = Slot("device-1")
        slot.kept = self.holder == owner
        self.holder = owner
        self.acquired.append(owner)
        return slot

    async def holders(self):
        return {"device-1": self.holder} if self.holder else {}

    async def release(self, owner):
        self.released.append(owner)
        if self.holder == owner:
            self.holder = None


@pytest.fixture
def api(store, monkeypatch, tmp_path):
    from app import api

    monkeypatch.setattr(api.common, "store", store)
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(auth, "_integration", None)
    monkeypatch.setenv("CLAUDE_USER_DATA_DIR", str(tmp_path / "Claude"))  # never the real Claude app
    devices = LeasingDevices(FakePhone(SEARCH, RESULTS))
    monkeypatch.setattr(phone, "devices", devices)
    monkeypatch.setattr(phone, "sessions", {})
    monkeypatch.setattr(phone, "taken_back", set())
    with TestClient(api.app) as client:
        yield client, devices


def field_number(interactions):
    return int(re.search(r"\[(\d+)\][^\n]*type\(any text\)", interactions).group(1))


def test_a_command_takes_the_phone_looks_and_acts(api):
    client, devices = api
    body = client.post("/api/phone/screen", json={"client": "abc"}).json()
    assert devices.acquired == ["cli:abc"] and devices.holder == "cli:abc"
    assert "Search" in body["screen"] and body["ok"]
    n = field_number(body["interactions"])
    body = client.post("/api/phone/act", json={
        "client": "abc", "calls": [{"name": "type_and_submit", "arguments": {"element": n, "text": "oat milk"}}],
    }).json()
    assert body["ok"] and [c["cmd"] for c in devices.phone.calls] == ["set_text", "ime_action"]
    assert devices.phone.calls[0]["text"] == "oat milk"
    assert body["results"][0].startswith("typed into") and "Results for oat milk" in body["screen"]
    assert body["activity"] == "com.example.Results"
    # The lease is kept between commands: one acquire per command, the same owner.
    assert set(devices.acquired) == {"cli:abc"} and devices.released == []


def test_a_call_the_screen_does_not_offer_is_refused_with_the_reason(api):
    client, devices = api
    client.post("/api/phone/screen", json={"client": "abc"})
    response = client.post("/api/phone/act", json={
        "client": "abc", "calls": [{"name": "interact", "arguments": {"element": 2, "interaction": "scroll_down"}}],
    })
    assert response.status_code == 400 and "does not accept scroll_down" in response.json()["detail"]
    assert devices.phone.calls == []
    response = client.post("/api/phone/act", json={"client": "abc", "calls": [{"name": "finish", "arguments": {"status": "done", "reason": "x"}}]})
    assert response.status_code == 400 and "just stop" in response.json()["detail"]


def test_sessions_do_not_share_the_phone(api):
    client, devices = api
    assert client.post("/api/phone/screen", json={"client": "abc"}).status_code == 200
    response = client.post("/api/phone/screen", json={"client": "xyz"})
    assert response.status_code == 409 and "another Claude session (abc)" in response.json()["detail"]
    assert "xyz" not in phone.sessions  # a session that could not have the phone holds nothing
    assert client.post("/api/phone/release", json={"client": "xyz"}).json()["released"] is False
    assert client.post("/api/phone/release", json={"client": "abc"}).json()["released"] is True
    assert devices.released == ["cli:abc"]
    assert client.post("/api/phone/screen", json={"client": "xyz"}).status_code == 200


def test_a_relay_task_keeps_the_phone_unless_it_waits_for_the_user(api, store):
    client, devices = api
    state = make_state(store)
    devices.holder = "run"
    response = client.post("/api/phone/screen", json={"client": "abc"})
    assert response.status_code == 409 and "busy with a penrose task" in response.json()["detail"]
    assert devices.released == []
    state["status"] = "waiting_user"
    store.save(state)
    assert client.post("/api/phone/screen", json={"client": "abc"}).status_code == 200
    assert devices.released == ["run"] and devices.holder == "cli:abc"


def test_release_puts_back_what_the_session_changed(api):
    client, devices = api
    body = client.post("/api/phone/act", json={
        "client": "abc", "calls": [{"name": "set_setting", "arguments": {"name": "wifi", "value": "off"}}],
    }).json()
    assert body["ok"] and body["results"] == ["set_setting(wifi=off) -> wifi done"]
    assert [op["op"] for op in devices.op_log] == ["state", "wifi"]
    client.post("/api/phone/act", json={"client": "abc", "calls": [{"name": "set_location", "arguments": {"lat": 1, "lng": 2}}]})
    client.post("/api/phone/release", json={"client": "abc"})
    assert devices.op_log[-1] == {"op": "wifi", "value": "on"}  # as the state op read it before
    assert devices.refreshed == 1 and devices.released == ["cli:abc"]


def test_the_user_taking_the_phone_back_is_told_once(api):
    client, devices = api
    client.post("/api/phone/screen", json={"client": "abc"})
    assert client.delete("/api/phone/sessions").json()["released"] is True
    assert devices.released == ["cli:abc"]
    response = client.post("/api/phone/screen", json={"client": "abc"})
    assert response.status_code == 409 and "took the phone back" in response.json()["detail"]
    assert client.post("/api/phone/screen", json={"client": "abc"}).status_code == 200


def test_status_says_who_has_the_phone_without_taking_it(api):
    client, devices = api
    devices.account_list = [{"email": "me@example.com"}]
    body = client.get("/api/phone/status?client=abc").json()
    assert body["phone"] == {"connected": True, "holder": None, "yours": False, "busy": None}
    assert body["google_account"] == {"email": "me@example.com"} and devices.acquired == []
    client.post("/api/phone/screen", json={"client": "abc"})
    assert client.get("/api/phone/status?client=abc").json()["phone"]["yours"] is True
    assert "another Claude session" in client.get("/api/phone/status?client=xyz").json()["phone"]["busy"]


def test_the_integration_token_opens_the_phone_api_and_nothing_else(store, monkeypatch, tmp_path):
    from app import api

    monkeypatch.setattr(api.common, "store", store)
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "login_username", "user")
    monkeypatch.setattr(settings, "login_password", "secret")
    monkeypatch.setattr(auth, "_integration", None)
    monkeypatch.setenv("CLAUDE_USER_DATA_DIR", str(tmp_path / "Claude"))  # never the real Claude app
    monkeypatch.setattr(phone, "devices", LeasingDevices(FakePhone(SEARCH)))
    monkeypatch.setattr(phone, "sessions", {})
    token = auth.integration_token()
    assert (tmp_path / "integration_token").read_text() == token and auth.integration_token() == token
    with TestClient(api.app) as client:
        bearer = {"Authorization": f"Bearer {token}"}
        assert client.get("/api/phone/status", headers=bearer).status_code == 200
        assert client.get("/api/phone/status").status_code == 401
        assert client.get("/api/phone/status", headers={"Authorization": "Bearer nope"}).status_code == 401
        assert client.get("/api/chats", headers=bearer).status_code == 401


def test_the_discovery_file_names_the_api_and_the_token(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(auth, "_integration", None)
    monkeypatch.setenv("CLAUDE_USER_DATA_DIR", str(tmp_path / "Claude"))  # never the real Claude app
    path = phone.publish(8123)
    info = json.loads(path.read_text())
    assert path == tmp_path / "cli.json" and info["url"] == "http://127.0.0.1:8123"
    assert info["token"] == (tmp_path / "integration_token").read_text()
    monkeypatch.setenv("RELAY_CLI_FILE", str(path))
    monkeypatch.setattr(cli, "alive", lambda url: url == "http://127.0.0.1:8123")
    assert cli.discover()["token"] == info["token"]
    phone.unpublish()
    assert not path.exists()
    with pytest.raises(cli.Stop) as stop:
        cli.discover()
    assert stop.value.code == cli.EXIT_NO_RELAY


# --- the command ---------------------------------------------------------------------------------------

@pytest.fixture
def relay(monkeypatch):
    """A `relay` whose API answers from `answers`, recording every call."""
    seen = []

    def call(self, method, path, body=None, timeout=150):
        seen.append((method, path, body))
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    answers = []
    monkeypatch.setattr(cli, "discover", lambda: {"url": "http://127.0.0.1:1", "token": "t"})
    monkeypatch.setattr(cli.Relay, "call", call)
    monkeypatch.setenv("RELAY_CLIENT", "abc")
    return seen, answers


def test_the_command_prints_what_a_model_reads(relay, capsys):
    seen, answers = relay
    answers.append({"ok": True, "results": ["typed into [3] search via touch -> now on Results"], "notes": [],
                    "events": ["toast: Searching"], "screen": '[1] Button "Add"', "interactions": "[1] tap"})
    assert cli.main(["phone", "submit", "3", "oat", "milk"]) == 0
    assert seen == [("POST", "/api/phone/act", {"client": "abc", "calls": [
        {"name": "type_and_submit", "arguments": {"element": 3, "text": "oat milk"}}]})]
    out = capsys.readouterr().out
    assert out.startswith("typed into [3]") and "Events:\n- toast: Searching\n" in out
    assert 'Screen:\n[1] Button "Add"\nInteractions:\n[1] tap' in out


def test_every_phone_verb_is_one_of_the_subagents_tools(relay):
    seen, answers = relay
    verbs = {
        ("tap", "4"): ("interact", {"element": 4, "interaction": "tap"}),
        ("long-press", "4"): ("interact", {"element": 4, "interaction": "long_press"}),
        ("type", "4", "hi", "there"): ("interact", {"element": 4, "interaction": "type", "value": "hi there"}),
        ("scroll", "4", "down"): ("interact", {"element": 4, "interaction": "scroll_down"}),
        ("choose", "4", "Large"): ("interact", {"element": 4, "interaction": "choose", "value": "Large"}),
        ("set", "4", "max"): ("interact", {"element": 4, "interaction": "set_value", "value": "max"}),
        ("key", "hide-keyboard"): ("press_key", {"key": "hide_keyboard"}),
        ("swipe", "left"): ("swipe", {"direction": "left"}),
        ("open", "Uber", "Eats"): ("open_app", {"app": "Uber Eats"}),
        ("open", "Amazon", "--shortcut", "Orders"): ("open_app", {"app": "Amazon", "shortcut": "Orders"}),
        ("link", "market://details?id=x", "--package", "com.android.vending"): ("open_link", {"uri": "market://details?id=x", "package": "com.android.vending"}),
        ("apps", "doordash"): ("list_apps", {"query": "doordash"}),
        ("wait", "Add", "to", "cart", "--seconds", "3"): ("wait_for", {"text": "Add to cart", "timeout": 3.0}),
        ("wait", "--gone", "Loading"): ("wait_for", {"gone": "Loading", "timeout": 5}),
        ("setting", "dnd", "on"): ("set_setting", {"name": "dnd", "value": "on"}),
        ("location", "12.9", "77.6"): ("set_location", {"lat": 12.9, "lng": 77.6}),
        ("grant", "com.x", "location", "camera"): ("grant", {"package": "com.x", "permissions": ["location", "camera"]}),
    }
    for words, (name, arguments) in verbs.items():
        answers.append({"ok": True, "screen": "", "interactions": ""})
        assert cli.main(["phone", *words]) == 0, words
        assert seen[-1][2]["calls"] == [{"name": name, "arguments": arguments}], words


def test_exit_codes_tell_the_model_what_happened(relay, capsys, monkeypatch):
    seen, answers = relay
    answers.append({"ok": False, "results": ["tapped [2] Pay via touch -> FAILED: stale handle"], "screen": "", "interactions": ""})
    assert cli.main(["phone", "tap", "2"]) == cli.EXIT_REFUSED
    answers.append(cli.Stop(cli.EXIT_BUSY, "The phone is busy with a penrose task: Groceries."))
    assert cli.main(["phone", "tap", "2"]) == cli.EXIT_BUSY
    assert "error: The phone is busy with a penrose task: Groceries." in capsys.readouterr().out
    monkeypatch.setattr(cli, "discover", lambda: (_ for _ in ()).throw(cli.Stop(cli.EXIT_NO_RELAY, "penrose is not running.")))
    assert cli.main(["--json", "phone", "status"]) == cli.EXIT_NO_RELAY
    assert json.loads(capsys.readouterr().out) == {"error": "penrose is not running.", "exit": 2}


def test_the_setup_page_sees_the_installation_and_installs_the_skill(api, monkeypatch, tmp_path):
    client, _ = api
    monkeypatch.setattr(cli.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(cli.shutil, "which", lambda name: r"C:\tools\claude.cmd" if name == "claude" else None)
    monkeypatch.setattr(cli, "user_path", lambda: [str(tmp_path / "elsewhere")])
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    cli.launcher_file(bin_dir).write_text("launcher")
    monkeypatch.setattr(phone, "bin_dir", lambda: bin_dir)
    state = client.get("/api/phone/setup").json()
    assert state["claude_code"] == r"C:\tools\claude.cmd" and state["skill"] == "missing" and state["launcher_present"]
    assert state["on_path"] is False and state["sessions"] == []
    noted = []
    monkeypatch.setattr(cli, "put_on_path", lambda b: noted.append(b) or "Restart Claude Code.")
    state = client.post("/api/phone/setup", json={"skill": True, "path": True}).json()
    assert state["skill"] == "installed" and state["skill_written"] == str(tmp_path / ".claude" / "skills" / "android-phone" / "SKILL.md")
    assert noted == [bin_dir] and state["path_note"] == "Restart Claude Code."
    (tmp_path / ".claude" / "skills" / "android-phone" / "SKILL.md").write_text("older", encoding="utf-8")
    assert client.get("/api/phone/setup").json()["skill"] == "outdated"
    monkeypatch.setattr(cli, "user_path", lambda: [str(bin_dir)])
    assert client.get("/api/phone/setup").json()["on_path"] is True


def test_setup_installs_the_skill_for_claude_code(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    assert cli.main(["setup", "claude-code", "--memory"]) == 0
    skill = (tmp_path / ".claude" / "skills" / "android-phone" / "SKILL.md").read_text(encoding="utf-8")
    assert skill.startswith("---\nname: android-phone\n") and "allowed-tools: Bash(relay *)" in skill
    assert "relay phone status" in skill and "never go through you" in skill.lower() or "Codes and passwords" in skill
    memory = (tmp_path / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")
    assert memory.count(cli.MEMORY_MARK) == 2 and "relay phone status" in memory
    assert cli.main(["setup", "claude-code", "--memory"]) == 0  # again: nothing doubled
    assert (tmp_path / ".claude" / "CLAUDE.md").read_text(encoding="utf-8") == memory
    assert "not on PATH" in capsys.readouterr().out


# --- `relay mcp`: the same phone for Cowork, as the Claude app's local MCP server -----------------------------------

def mcp_session(*messages):
    """Run the MCP server over these messages (dicts, or raw lines); its replies by id."""
    import io

    from app import mcp

    out = io.StringIO()
    lines = [m if isinstance(m, str) else json.dumps(m) for m in messages]
    assert mcp.serve(io.StringIO("\n".join(lines) + "\n"), out) == 0
    return {reply["id"]: reply for reply in map(json.loads, out.getvalue().splitlines())}


def rpc(ident, method, **params):
    return {"jsonrpc": "2.0", "id": ident, "method": method, "params": params}


def test_the_mcp_server_introduces_itself_with_the_cowork_skill():
    from app import mcp

    replies = mcp_session(rpc(1, "initialize", protocolVersion="2025-03-26", capabilities={}),
                          {"jsonrpc": "2.0", "method": "notifications/initialized"},
                          rpc(2, "tools/list"), rpc(3, "ping"), rpc(4, "resources/list"), "{not json")
    assert set(replies) == {1, 2, 3, 4, None}  # the notification gets no answer
    init = replies[1]["result"]
    assert init["protocolVersion"] == "2025-03-26" and init["capabilities"] == {"tools": {}}
    assert init["serverInfo"]["name"] == "relay"
    assert not init["instructions"].startswith("---") and "phone_status" in init["instructions"]
    assert "Codes and passwords never go through you" in init["instructions"]
    tools = replies[2]["result"]["tools"]
    names = [t["name"] for t in tools]
    assert names[0] == "phone_status" and len(names) == len(set(names)) == 15
    assert set(names) == set(mcp.ACTIONS) | {"phone_status", "phone_screen", "phone_knowhow", "phone_release"}
    for t in tools:
        assert set(t["inputSchema"]["required"]) <= set(t["inputSchema"]["properties"]), t["name"]
    assert set(mcp.ACTIONS.values()) == {"interact", "type_and_submit", "press_key", "swipe", "open_app", "list_apps",
                                         "open_link", "wait_for", "set_setting", "set_location", "grant"}
    assert replies[3]["result"] == {} and replies[4]["error"]["code"] == -32601 and replies[None]["error"]["code"] == -32700


def test_mcp_tool_calls_go_through_the_phone_api(relay):
    seen, answers = relay
    answers += [
        {"screen": '[1] Button "Add"', "interactions": "[1] tap", "shot": "QUJD"},
        {"ok": False, "results": ["tapped [9] -> FAILED: not on the screen"], "screen": "", "interactions": ""},
        {"released": True},
        cli.Stop(cli.EXIT_BUSY, "The phone is busy with a penrose task: Groceries."),
    ]
    replies = mcp_session(
        rpc(1, "tools/call", name="phone_screen", arguments={"screenshot": True}),
        rpc(2, "tools/call", name="phone_interact", arguments={"element": 9, "interaction": "tap", "value": ""}),
        rpc(3, "tools/call", name="phone_release", arguments={}),
        rpc(4, "tools/call", name="phone_press_key", arguments={"key": "home"}),
    )
    assert seen == [
        ("POST", "/api/phone/screen", {"client": "abc", "shot": True}),
        ("POST", "/api/phone/act", {"client": "abc", "calls": [{"name": "interact", "arguments": {"element": 9, "interaction": "tap"}}]}),
        ("POST", "/api/phone/release", {"client": "abc"}),
        ("POST", "/api/phone/act", {"client": "abc", "calls": [{"name": "press_key", "arguments": {"key": "home"}}]}),
    ]
    screen = replies[1]["result"]
    assert screen["isError"] is False and 'Screen:\n[1] Button "Add"' in screen["content"][0]["text"]
    assert screen["content"][1] == {"type": "image", "data": "QUJD", "mimeType": "image/jpeg"}
    assert replies[2]["result"]["isError"] is True and "FAILED" in replies[2]["result"]["content"][0]["text"]
    assert replies[3]["result"] == {"content": [{"type": "text", "text": "Released."}], "isError": False}
    assert replies[4]["result"]["isError"] is True
    assert replies[4]["result"]["content"][0]["text"] == "error: The phone is busy with a penrose task: Groceries."


def test_relay_joins_the_claude_app_config_without_disturbing_it(monkeypatch, tmp_path, capsys):
    import zipfile

    monkeypatch.setenv("CLAUDE_USER_DATA_DIR", str(tmp_path / "Claude"))
    monkeypatch.delenv("RELAY_CLI_FILE", raising=False)
    monkeypatch.setattr(cli, "data_dirs", lambda: [tmp_path / "relay" / "data"])
    config = tmp_path / "Claude" / "claude_desktop_config.json"
    config.parent.mkdir()
    original = {"mcpServers": {"other": {"command": "x"}}, "preferences": {"keep": True}}
    config.write_text(json.dumps(original), encoding="utf-8")
    assert cli.claude_app_config() == config and not cli.claude_app_registered()

    assert cli.main(["setup", "claude-app"]) == 0
    saved = json.loads(config.read_text(encoding="utf-8"))
    assert saved["preferences"] == {"keep": True} and saved["mcpServers"]["other"] == {"command": "x"}
    entry = saved["mcpServers"]["relay"]
    assert entry["args"][-1] == "mcp" and entry["env"]["PYTHONPATH"].endswith("backend")
    assert json.loads(config.with_name(config.name + ".before-relay").read_text(encoding="utf-8")) == original
    with zipfile.ZipFile(tmp_path / "relay" / "claude" / "android-phone.zip") as z:
        assert z.namelist() == ["android-phone/SKILL.md"]
        assert z.read("android-phone/SKILL.md").decode().startswith("---\nname: android-phone\n")
    assert "Quit and reopen the Claude app" in capsys.readouterr().out
    assert cli.claude_app_registered()

    assert cli.main(["setup", "claude-app", "--remove"]) == 0
    assert json.loads(config.read_text(encoding="utf-8")) == original
    config.write_text("{broken", encoding="utf-8")  # never overwrite a config it cannot read
    assert cli.main(["setup", "claude-app"]) == cli.EXIT_REFUSED and config.read_text(encoding="utf-8") == "{broken"


def test_the_setup_page_adds_relay_to_the_claude_app(api, monkeypatch, tmp_path):
    client, _ = api
    (tmp_path / "Claude").mkdir()
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    state = client.get("/api/phone/setup").json()
    assert state["claude_app_config"] == str(tmp_path / "Claude" / "claude_desktop_config.json")
    assert state["claude_app_registered"] is False
    state = client.post("/api/phone/setup", json={"app": True, "skill_zip": True}).json()
    assert state["claude_app_registered"] is True
    assert state["skill_zip"] == str(tmp_path / "claude" / "android-phone.zip")
    entry = json.loads((tmp_path / "Claude" / "claude_desktop_config.json").read_text(encoding="utf-8"))["mcpServers"]["relay"]
    assert entry["env"]["RELAY_CLI_FILE"] == str(tmp_path / "data" / "cli.json")
    assert client.post("/api/phone/setup", json={"remove_app": True}).json()["claude_app_registered"] is False


def test_release_gives_back_a_lease_the_gateway_still_holds_for_a_client_this_api_no_longer_knows(api):
    """The API restarted (a dev edit) while the client held the phone: the gateway kept the lease, the session is gone."""
    client, devices = api
    devices.holder = "cli:abc"
    assert client.post("/api/phone/release", json={"client": "abc"}).json() == {"released": True, "why": "stale lease"}
    assert devices.released == ["cli:abc"] and devices.holder is None
    assert client.post("/api/phone/release", json={"client": "abc"}).json()["released"] is False


def test_the_startup_sweep_waits_for_the_gateway_and_drops_a_lease_of_a_client_it_no_longer_knows(api, monkeypatch):
    """Started together with the gateway, the API asks before the gateway answers, then while the phone is still
    being checked: it keeps asking, and a CLI lease nobody here holds goes even while the phone shows disconnected."""
    import asyncio

    client, devices = api
    devices.holder = "cli:gone"
    listed = []

    async def phones():
        listed.append(1)
        if len(listed) == 1:
            return []  # the gateway is not up yet
        return [{"id": "device-1", "connected": False, "owner": devices.holder}]

    monkeypatch.setattr(devices, "phones", phones)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(phone.asyncio, "sleep", lambda s: real_sleep(0))
    phone.sessions["kept"] = phone.Session("kept")  # a client this API does know keeps its phone
    devices.holder = "cli:gone"
    asyncio.run(phone.forget_stale(patience=5))
    assert len(listed) == 2 and devices.released == ["cli:gone"] and devices.holder is None
    devices.holder, devices.released[:] = "cli:kept", []
    asyncio.run(phone.forget_stale(patience=5))
    assert devices.released == [] and devices.holder == "cli:kept"
