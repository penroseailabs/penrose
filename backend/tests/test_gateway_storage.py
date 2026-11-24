import httpx
import pytest
from app.store import Store
from fakes import make_state, step_spec
from fastapi.testclient import TestClient


def test_gateway_database_is_separate():
    from app import api, gateway

    assert gateway.core.store.path != api.common.store.path
    assert gateway.core.store.directory == api.common.store.directory / "gateway"


@pytest.mark.parametrize("run_status", [None, "running", "waiting_user", "interrupted"])
def test_manual_controls_use_gateway_leases_with_separate_databases(
    store,
    tmp_path,
    monkeypatch,
    run_status,
):
    from app import api, gateway

    lease_store = Store(tmp_path / "gateway")
    monkeypatch.setattr(api.common, "store", store)
    monkeypatch.setattr(gateway.core, "store", lease_store)
    monkeypatch.setattr(gateway.core, "slots", {"device-1": "emulator-5554"})
    actions = []

    async def shell(slot, *args):
        actions.append(
            lease_store.rows("SELECT owner FROM leases WHERE slot=?", (slot,))[0]["owner"]
        )
        return b""

    monkeypatch.setattr(gateway.watch, "ready", lambda slot: True)
    monkeypatch.setattr(gateway.core, "shell", shell)
    if run_status:
        state = make_state(store, steps=[step_spec("node")])
        state["status"] = run_status
        store.save(state)
        lease_store.lease(["device-1"], "run")  # one lease per run

    with TestClient(api.app) as client:
        # No native SQLite access: the API talks to a separate gateway over HTTP.
        original = api.common.gateway
        with client.portal.wrap_async_context_manager(
            httpx.AsyncClient(
                base_url="http://gateway", transport=httpx.ASGITransport(app=gateway.app)
            )
        ) as gateway_client:
            api.common.gateway = gateway_client
            response = client.post("/api/devices/device-1/actions", json={"kind": "home"})
            api.common.gateway = original

    assert response.status_code == (409 if run_status == "running" else 200)
    assert store.rows("SELECT * FROM leases") == []
    if run_status:
        assert lease_store.rows("SELECT owner FROM leases") == [{"owner": "run"}]
        assert actions == ([] if run_status == "running" else ["run"])
    else:
        assert len(actions) == 1
        assert lease_store.rows("SELECT * FROM leases") == []


async def test_specific_device_lease_is_exclusive(tmp_path, monkeypatch):
    from app import gateway

    monkeypatch.setattr(gateway.core, "store", Store(tmp_path / "gateway"))
    monkeypatch.setattr(gateway.core, "slots", {"one": "first", "two": "second"})

    monkeypatch.setattr(gateway.watch, "ready", lambda slot: True)
    async with httpx.AsyncClient(
        base_url="http://gateway", transport=httpx.ASGITransport(app=gateway.app)
    ) as client:
        assert (await client.post("/leases", json={"owner": "agent", "slot": "one"})).json() == {
            "slot": "one", "kept": False,
        }
        # The same owner again keeps its phone: nobody else used it in between.
        assert (await client.post("/leases", json={"owner": "agent"})).json() == {"slot": "one", "kept": True}
        assert (
            await client.post("/leases", json={"owner": "manual", "slot": "one"})
        ).status_code == 409
        assert (
            await client.post("/leases", json={"owner": "manual", "slot": "missing"})
        ).status_code == 404
        assert (await client.get("/leases/one")).json() == {"owner": "agent"}
        assert (await client.post("/leases", json={"owner": "other"})).json() == {"slot": "two", "kept": False}
