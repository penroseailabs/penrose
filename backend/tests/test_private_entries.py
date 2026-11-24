"""Private entries: values the user gives a chat privately (a card, a password), asked for or not. The agents
see their number and label only; the runtime types a value where an agent writes {private:N}, and the gateway
hides it from every read after, also when the app shows it regrouped."""

from app import gateway
from app.gateway.core import Redactions, scrub
from app.models import PRIVATE_MARK
from fakes import make_state, phone
from fastapi.testclient import TestClient
from phone import FakePhone, field, snapshot
from test_phone_agent import last_result, run, then_report

CARD = "4111111111111111"


def api_client(store, monkeypatch):
    from app import api

    monkeypatch.setattr(api.common, "store", store)
    return TestClient(api.app)


def typing(value, element=1):
    return {"tool": "interact", "element": element, "interaction": "type", "value": value}


def test_entries_reach_the_task_as_labels_and_never_as_values(store, monkeypatch):
    state = make_state(store)
    state["status"] = "waiting_user"
    state["private_asked"] = {"kind": "card", "label": ""}
    store.save(state)
    with api_client(store, monkeypatch) as client:
        sent = client.post("/api/chats/chat/private", json={"entries": [
            {"label": "Card number", "value": CARD}, {"label": "CVV", "value": "123"}]})
        assert sent.status_code == 202
        # The waiting task continues with a message that names the entries, not what they hold.
        assert sent.json()["inbox"] == ["🔒 Sent privately: Card number (#1), CVV (#2)"]
        detail = client.get("/api/chats/chat").json()
        assert CARD not in str(detail) and "123\"" not in str(detail["messages"])
        # More can come later, unasked; they are numbered on.
        assert client.post("/api/chats/chat/private", json={"entries": [{"label": "Expiry", "value": "12/28"}]}).status_code == 202
        assert client.post("/api/chats/chat/private", json={"entries": [{"label": "Name", "value": "José"}]}).status_code == 422
    assert store.private_labels("chat") == [{"n": 1, "label": "Card number"}, {"n": 2, "label": "CVV"}, {"n": 3, "label": "Expiry"}]
    assert store.private_value("chat", 1) == CARD


def test_entries_are_forgotten_when_the_task_ends_or_the_chat_goes(store, monkeypatch):
    state = make_state(store)
    with store.connect() as db:
        store.add_private(db, "chat", [("Card number", CARD)])
    state["status"] = "waiting_user"
    store.save(state)
    assert store.private_labels("chat")
    state["status"] = "completed"
    store.save(state)
    assert store.private_labels("chat") == []
    with store.connect() as db:
        store.add_private(db, "chat", [("Card number", CARD)])
    with api_client(store, monkeypatch) as client:
        assert client.delete("/api/chats/chat").status_code == 200
    assert store.rows("SELECT * FROM private_entries") == []


async def test_a_private_entry_is_typed_where_the_agent_names_it(store):
    with store.connect() as db:
        store.add_private(db, "chat", [("Card number", CARD)])
    screen = snapshot(field(1, flags=("edit", "click")))
    end, s = await run(store, then_report(phone(typing("{private:1}"))), phone=FakePhone(screen))
    assert [c["cmd"] for c in s.phone.calls] == ["set_text"] and s.phone.calls[0]["text"] == CARD
    assert s.phone.calls[0].get("private") is True  # sent as private: the gateway hides it from then on
    # The agent read only the reference, never the value.
    assert all(CARD not in c["context"] for c in s.calls)


async def test_a_missing_or_partial_reference_is_refused(store):
    screen = snapshot(field(1, flags=("edit", "click")))
    end, s = await run(store, then_report(phone(typing("{private:4}"))), phone=FakePhone(screen))
    assert "there is no private entry #4" in last_result(s.calls[1]["context"]) and s.phone.calls == []
    with store.connect() as db:
        store.add_private(db, "chat", [("Card number", CARD)])
    end, s = await run(store, then_report(phone(typing("Card {private:1}"))), phone=FakePhone(screen))
    assert "must be the whole value" in last_result(s.calls[1]["context"]) and s.phone.calls == []


def test_a_value_is_hidden_also_when_the_app_regroups_it():
    hidden = Redactions()
    hidden.add(CARD)
    hidden.add("1228")
    hidden.add("123")
    shown = {"a": "4111 1111 1111 1111", "b": "4111-1111-1111-1111", "c": "Expires 12/28", "d": "CVV 123",
             "e": "1234 reviews", "f": "Card ending 1111"}
    out = scrub(shown, hidden)
    assert out["a"] == out["b"] == PRIVATE_MARK and out["c"] == f"Expires {PRIVATE_MARK}" and out["d"] == f"CVV {PRIVATE_MARK}"
    assert out["e"] == "1234 reviews" and out["f"] == "Card ending 1111"  # a short value only as a whole word


def test_a_short_value_is_hidden_only_for_a_while(monkeypatch):
    from app.gateway import core

    now = [1000.0]
    monkeypatch.setattr(core.time, "monotonic", lambda: now[0])
    hidden = Redactions()
    hidden.add("123")
    hidden.add(CARD)
    now[0] += core.SHORT_SECONDS + 1
    assert scrub("CVV 123, card 4111 1111 1111 1111", hidden) == f"CVV 123, card {PRIVATE_MARK}"


async def test_the_gateway_hides_a_privately_typed_text_from_then_on(monkeypatch):
    from test_driver import GatewayPhone

    phone = GatewayPhone({"ok": True, "windows": [{"nodes": [{"text": "4111 1111 1111 1111", "h": 1}]}]})
    monkeypatch.setitem(gateway.core.phones, "device-1", phone)
    monkeypatch.setitem(gateway.core.redactions, "device-1", Redactions())
    monkeypatch.setattr(gateway.core, "check_owner", lambda slot, owner: None)
    body = gateway.actions.BridgeRequest(owner="o", method="call", cmd="set_text", process="com.x",
                                         args={"h": 4, "text": CARD}, private=True)
    await gateway.actions.bridge("device-1", body)
    assert phone.seen == [("set_text", "com.x", {"h": 4, "text": CARD})]
    read = await gateway.actions.bridge("device-1", gateway.actions.BridgeRequest(owner="o", method="snapshot_display"))
    assert read["windows"][0]["nodes"][0]["text"] == PRIVATE_MARK


def test_the_orchestrator_asks_for_a_kind_and_names_an_other():
    import pytest
    from app.errors import Rejected
    from app.tools.orchestrator import AskUser

    AskUser(question="How would you like to pay?", private="card").check([], {})
    with pytest.raises(Rejected, match="private_label"):
        AskUser(question="Your PAN?", private="other").check([], {})

    class O:
        state = {}
        def ask_user(self, question, private=None):
            self.state["asked"] = private
        def watch(self, tool):
            pass

    import asyncio
    o = O()
    asyncio.run(AskUser(question="Card?", private="card", private_label="Card for Hulu").run(o))
    assert o.state["asked"] == {"kind": "card", "label": "Card for Hulu"}


async def test_after_a_private_value_is_typed_an_unreadable_screen_is_not_photographed(store):
    """A payment overlay the phone cannot read may show the card again: no picture leaves with nothing blacked out."""
    from fakes import report
    from phone import screen_of
    from test_phone_agent import SHOT, section, turn_no

    with store.connect() as db:
        store.add_private(db, "chat", [("Card number", CARD)])
    first = snapshot(field(1, flags=("edit", "click")))
    phone_ = FakePhone(first, screen_of("Overlay"), screen_of("Overlay"))

    def decide(text):
        n = turn_no(text)
        if n == 1:
            return phone(typing("{private:1}"))
        if n == 2:
            return phone(SHOT)
        if n == 3:
            assert section(text, "Screenshot").startswith("not taken: a private value was typed")
        return phone(report())

    from fakes import FakeDevices
    from test_phone_agent import JPEG
    end, s = await run(store, decide, devices=FakeDevices(phone_, jpeg=JPEG))
    assert all(not c["images"] for c in s.calls)


async def test_type_private_types_an_entry_into_the_focused_field(store):
    with store.connect() as db:
        store.add_private(db, "chat", [("Hulu OTP", "482913")])
    end, s = await run(store, then_report(phone({"tool": "type_private", "entry": 1})))
    typed = [a for a in s.devices.actions if a["kind"] == "type_text"]
    assert typed == [{"kind": "type_text", "text": "482913", "sensitive": True}]
    assert all("482913" not in c["context"] for c in s.calls)
    end, s = await run(store, then_report(phone({"tool": "type_private", "entry": 7})))
    assert "there is no private entry #7" in last_result(s.calls[1]["context"])
