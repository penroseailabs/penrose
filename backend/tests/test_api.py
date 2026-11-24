"""The store and the API around a run: leases and jobs, replies, private codes, stop and continue."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from app.plan import new_step
from fakes import make_state, step_spec
from fastapi.testclient import TestClient


def api_client(store, monkeypatch):
    from app import api

    monkeypatch.setattr(api.common, "store", store)
    return TestClient(api.app)


def waiting_state(store, question):
    state = make_state(store, steps=[step_spec("a"), step_spec("otp")])
    state["status"] = "waiting_user"
    state["plan"][0].update(status="done", report={"status": "completed", "summary": "preserved"})
    state["plan"][1].update(status="waiting_user", question=question)
    store.save(state)
    return state


def test_lease_and_job_claim_are_exclusive(store):
    with ThreadPoolExecutor(max_workers=8) as pool:
        acquired = list(pool.map(lambda i: store.lease(["only-device"], f"owner{i}"), range(8)))
    assert acquired.count("only-device") == 1
    with store.connect() as db:
        store.enqueue("run", db)
    with ThreadPoolExecutor(max_workers=8) as pool:
        jobs = list(pool.map(lambda i: store.claim(str(i)), range(8)))
    assert sum(job is not None for job in jobs) == 1


def test_a_stale_job_requires_inspection_instead_of_replay(store):
    state = make_state(store, steps=[step_spec("a")])
    state["plan"][0].update(status="running", turns=[{"summary": "Tap Pay", "action": "click", "status": "started"}])
    store.save(state)
    store.execute("INSERT INTO jobs VALUES (?,?,?,?,?)", ("job", "run", "running", "dead-worker", time.time() - 200))
    assert store.recover_stale() == ["run"]  # the worker frees the phone this run held
    saved = store.load("run")
    assert saved["status"] == "interrupted" and saved["plan"][0]["status"] == "stopped"
    assert saved["plan"][0]["turns"][0]["status"] == "uncertain"
    assert store.claim("new-worker") is None


def test_cancel_cannot_be_overwritten_by_a_late_worker_save(store):
    state = make_state(store)
    store.execute("UPDATE runs SET status='cancelled' WHERE id='run'")
    store.save(state)
    assert store.rows("SELECT status FROM runs")[0]["status"] == "cancelled"


def test_a_code_written_in_plain_text_reaches_the_waiting_step(store, monkeypatch):
    waiting_state(store, {"question": "OTP?", "sensitive": True})
    with api_client(store, monkeypatch) as client:
        response = client.post("/api/runs/run/reply", json={"content": "OTP is 2793", "node_id": "otp"})
        assert response.status_code == 202
        step = response.json()["plan"][1]
        assert step["answers"] == ["OTP is 2793"] and step["status"] == "resume"


def test_replies_and_codes_never_enter_history(store, monkeypatch):
    waiting_state(store, {"question": "OTP?", "sensitive": True})
    with api_client(store, monkeypatch) as client:
        response = client.post("/api/runs/run/reply", json={"content": "123456", "node_id": "otp", "sensitive": True})
        assert response.status_code == 202
        body = response.json()
        assert body["plan"][0]["status"] == "done" and body["plan"][1]["status"] == "resume"
        assert body["nodes"]["a"]["status"] == "succeeded"
        assert "123456" not in client.get("/api/chats/chat").text
        assert "123456" not in client.get("/api/runs/run/events").text
    assert store.consume_secret("run", "otp") == "123456"
    assert store.consume_secret("run", "otp") is None


def test_text_instead_of_a_private_code_reaches_the_step_not_the_phone(store, monkeypatch):
    waiting_state(store, {"question": "OTP?", "sensitive": True})
    with api_client(store, monkeypatch) as client:
        response = client.post("/api/runs/run/reply", json={"content": "Use email instead", "node_id": "otp", "instead": True})
        assert response.status_code == 202
        step = response.json()["plan"][1]
        assert step["answers"] == ["Use email instead"] and step["status"] == "resume"
        assert response.json()["nodes"]["otp"]["guidance"] == ["Use email instead"]
    assert store.consume_secret("run", "otp") is None


def test_a_plain_reply_goes_to_the_orchestrator(store, monkeypatch):
    state = make_state(store, steps=[step_spec("a")])
    state["status"] = "waiting_user"
    store.save(state)
    with api_client(store, monkeypatch) as client:
        response = client.post("/api/runs/run/reply", json={"content": "Horizon, please"})
        assert response.status_code == 202 and response.json()["inbox"] == ["Horizon, please"]


def test_cross_origin_writes_are_refused_and_a_second_message_joins_the_working_run(store, monkeypatch):
    with api_client(store, monkeypatch) as client:
        assert client.post("/api/chats", headers={"Origin": "https://untrusted.example"}).status_code == 403
        chat = client.post("/api/chats").json()
        assert client.post(f"/api/chats/{chat['id']}/messages", json={"content": "hello"}).status_code == 202
        created = store.load(store.rows("SELECT id FROM runs")[0]["id"])
        assert created["plan"] == [] and created["status"] == "queued"
        again = client.post(f"/api/chats/{chat['id']}/messages", json={"content": "and another"})
        assert again.status_code == 202 and again.json()["id"] == created["id"]
        assert len(store.rows("SELECT id FROM runs")) == 1
        assert [r["content"] for r in store.rows("SELECT content FROM inbox")] == ["and another"]


def test_stop_pauses_the_task_and_the_next_message_continues_it(store, monkeypatch):
    state = make_state(store, steps=[step_spec("a"), step_spec("b")])
    state["plan"][0].update(status="done")
    state["plan"][1].update(status="running", turns=[
        {"step": True, "kind": "act", "summary": "Pick the address"},
        {"summary": "Tap Home", "action": "click", "status": "started"},
    ])
    store.save(state)
    with api_client(store, monkeypatch) as client:
        stopped = client.post("/api/runs/run/stop")
        assert stopped.status_code == 200 and stopped.json()["status"] == "stopped"
        step = stopped.json()["plan"][1]
        assert step["status"] == "stopped" and step["turns"][1]["status"] == "uncertain"
        assert stopped.json()["nodes"]["b"]["status"] == "stopped"
        assert client.post("/api/runs/run/stop").status_code == 409
        resumed = client.post("/api/chats/chat/messages", json={"content": "Use the Work address"})
        assert resumed.status_code == 202
        assert resumed.json()["id"] == "run" and resumed.json()["status"] == "queued"
        assert resumed.json()["inbox"] == ["Use the Work address"]
    assert len(store.rows("SELECT * FROM runs")) == 1


def test_a_message_after_a_completed_task_starts_a_new_run(store, monkeypatch):
    state = make_state(store)
    state["status"] = "completed"
    store.save(state)
    with api_client(store, monkeypatch) as client:
        response = client.post("/api/chats/chat/messages", json={"content": "Now the next one"})
        assert response.status_code == 202 and response.json()["id"] != "run"


def test_cancel_releases_the_runs_lease(store, monkeypatch):
    state = make_state(store, steps=[step_spec("a")])
    state["status"] = "waiting_user"
    store.save(state)
    deleted = []

    class Gateway:
        async def delete(self, path):
            deleted.append(path)

        async def aclose(self):
            pass

    with api_client(store, monkeypatch) as client:
        from app.api import common

        monkeypatch.setattr(common, "gateway", Gateway())
        assert client.post("/api/runs/run/cancel").status_code == 200
    assert "/leases/run" in deleted


async def test_gateway_quotes_android_shell_arguments(monkeypatch):
    import shlex

    from app import gateway

    calls = []

    async def adb(*args, **kwargs):
        calls.append(args)
        return b""

    monkeypatch.setattr(gateway.core, "adb", adb)
    text = "hello'; am force-stop com.android.chrome; echo '"
    await gateway.core.shell("device-1", "input", "text", text)
    assert shlex.split(calls[0][2]) == ["input", "text", text]


def test_new_steps_start_pending_with_no_attempts():
    step = new_step(step_spec("a"))
    assert step["status"] == "pending" and step["attempts"] == 0 and step["turns"] == []


def test_a_reply_sent_while_the_worker_still_finishes_the_run_waits_for_it(store, monkeypatch):
    state = make_state(store, steps=[step_spec("a")])
    state["status"] = "waiting_user"
    store.save(state)
    # The worker's job is still open: it writes the memory after showing the run as waiting.
    store.execute("INSERT INTO jobs VALUES (?,?,?,?,?)", ("job", "run", "running", "worker", time.time()))
    threading.Timer(0.5, lambda: store.execute("UPDATE jobs SET status='done' WHERE id='job'")).start()
    with api_client(store, monkeypatch) as client:
        started = time.monotonic()
        response = client.post("/api/runs/run/reply", json={"content": "Horizon, please"})
        assert response.status_code == 202 and 0.4 < time.monotonic() - started < 5
        assert response.json()["inbox"] == ["Horizon, please"]


def test_a_reply_to_a_working_run_is_refused_at_once(store, monkeypatch):
    make_state(store, steps=[step_spec("a")])
    store.execute("INSERT INTO jobs VALUES (?,?,?,?,?)", ("job", "run", "running", "worker", time.time()))
    with api_client(store, monkeypatch) as client:
        started = time.monotonic()
        assert client.post("/api/runs/run/reply", json={"content": "hi"}).status_code == 409
        assert time.monotonic() - started < 2


# --- rewinding to a message ---------------------------------------------------------------------------

def settle(store, status="waiting_user", journal=()):
    """The worker has finished with the chat's latest run: it is `status` now, having journaled `journal`."""
    run = store.rows("SELECT id FROM runs ORDER BY updated DESC")[0]["id"]
    state = store.load(run)
    state["status"] = status
    state.setdefault("journal", []).extend({"at": time.time(), "line": line} for line in journal)
    store.save(state)
    store.execute("UPDATE jobs SET status='done' WHERE run_id=?", (run,))
    return run


def test_rewriting_the_message_that_began_a_task_starts_the_task_again(store, monkeypatch):
    with api_client(store, monkeypatch) as client:
        chat = client.post("/api/chats").json()["id"]
        client.post(f"/api/chats/{chat}/messages", json={"content": "Order a pizza"})
        first = settle(store, "completed")
        client.post(f"/api/chats/{chat}/messages", json={"content": "Thanks"})
        second = settle(store, "completed")
        assert second != first
        detail = client.get(f"/api/chats/{chat}").json()
        began = detail["messages"][0]["id"]
        assert began in detail["rewindable"]
        response = client.post(f"/api/chats/{chat}/messages/{began}/rewind", json={"content": "Order two pizzas"})
        assert response.status_code == 202
        after = client.get(f"/api/chats/{chat}").json()
        assert [m["content"] for m in after["messages"]] == ["Order two pizzas"]
        assert [r["request"] for r in after["runs"]] == ["Order two pizzas"]
        assert store.load(first) is None
        assert store.rows("SELECT title FROM chats WHERE id=?", (chat,))[0]["title"] == "Order two pizzas"


def test_rewriting_a_follow_up_puts_the_task_back_as_it_was(store, monkeypatch):
    with api_client(store, monkeypatch) as client:
        chat = client.post("/api/chats").json()["id"]
        client.post(f"/api/chats/{chat}/messages", json={"content": "Order a pizza"})
        run = settle(store, journal=["s1 done: chose a margherita"])
        client.post(f"/api/chats/{chat}/messages", json={"content": "Add garlic bread"})
        settle(store, journal=["s2 done: added garlic bread"])
        client.post(f"/api/chats/{chat}/messages", json={"content": "And a cola"})
        settle(store)
        store.event(run, "stage_output", {})
        messages = client.get(f"/api/chats/{chat}").json()
        garlic = next(m["id"] for m in messages["messages"] if m["content"] == "Add garlic bread")
        cola = next(m["id"] for m in messages["messages"] if m["content"] == "And a cola")
        assert garlic in messages["rewindable"] and cola in messages["rewindable"]

        response = client.post(f"/api/chats/{chat}/messages/{garlic}/rewind", json={"content": "Add a brownie"})
        assert response.status_code == 202
        state = response.json()
        assert state["id"] == run and state["status"] == "queued" and state["inbox"] == ["Add a brownie"]
        lines = [j["line"] for j in state["journal"]]
        assert lines[0] == "s1 done: chose a margherita"
        assert "added garlic bread" in lines[-1] and "already happened on the phone" in lines[-1]
        assert [m["content"] for m in client.get(f"/api/chats/{chat}").json()["messages"]] == ["Order a pizza", "Add a brownie"]
        assert store.rows("SELECT COUNT(*) AS n FROM checkpoints")[0]["n"] == 1  # only the new message's


def test_a_message_cannot_be_rewritten_while_the_task_works_or_without_a_checkpoint(store, monkeypatch):
    with api_client(store, monkeypatch) as client:
        chat = client.post("/api/chats").json()["id"]
        client.post(f"/api/chats/{chat}/messages", json={"content": "Order a pizza"})
        began = client.get(f"/api/chats/{chat}").json()["messages"][0]["id"]
        working = client.post(f"/api/chats/{chat}/messages/{began}/rewind", json={"content": "Order a pasta"})
        assert working.status_code == 409
        settle(store)
        client.post(f"/api/chats/{chat}/messages", json={"content": "Add garlic bread"})
        settle(store)
        garlic = client.get(f"/api/chats/{chat}").json()["messages"][1]["id"]
        store.execute("DELETE FROM checkpoints")  # a message from before checkpoints were kept
        assert garlic not in client.get(f"/api/chats/{chat}").json()["rewindable"]
        refused = client.post(f"/api/chats/{chat}/messages/{garlic}/rewind", json={"content": "Add a salad"})
        assert refused.status_code == 422
        assert client.post(f"/api/chats/{chat}/messages/nope/rewind", json={"content": "x"}).status_code == 404
