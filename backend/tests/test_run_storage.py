"""How a run is saved: its turns one row each, written as they come, and the rest of its state only when it
matters, so a long run costs the same to save at its thousandth event as at its first."""

import json

from app.runtime.runner import Runner
from app.store import dump_state, put_state, read_state
from fakes import make_state, step_spec


def turn(n, **extra):
    return {"step": True, "summary": f"turn {n}", "kind": "act", "at": 1000 + n, **extra}


def saved_turns(store, step_id="a"):
    return [json.loads(r["turn"]) for r in store.rows(
        "SELECT turn FROM turns WHERE run_id='run' AND step_id=? ORDER BY idx", (step_id,))]


def head(store):
    return json.loads(store.rows("SELECT state FROM runs WHERE id='run'")[0]["state"])


def test_turns_are_kept_apart_and_the_state_reads_back_whole(store):
    state = make_state(store, steps=[step_spec("a"), step_spec("b")])
    state["plan"][0]["turns"] = [turn(i) for i in range(30)]
    store.save(state)
    saved = head(store)
    assert "nodes" not in saved and all("turns" not in step for step in saved["plan"])
    assert saved_turns(store) == state["plan"][0]["turns"]
    loaded = store.load("run")
    assert loaded["plan"][0]["turns"] == state["plan"][0]["turns"] and loaded["plan"][1]["turns"] == []
    assert loaded["nodes"]["a"]["turns"] == state["plan"][0]["turns"]  # the window's view, made on reading


def test_a_save_writes_only_new_and_recent_turns(store):
    state = make_state(store, steps=[step_spec("a")])
    turns = state["plan"][0]["turns"]
    turns.extend(turn(i) for i in range(40))
    store.save(state)
    # An action's turn gets its result after it was first saved: a recent turn is written again.
    turns[-1]["result"] = {"ok": True}
    turns.append(turn(40))
    statements = []
    real_connect = store.connect

    def spying():
        context = real_connect()
        db = context.__enter__()
        db.set_trace_callback(statements.append)
        return _Wrap(context, db)

    store.connect = spying
    store.save(state)
    store.connect = real_connect
    writes = [s for s in statements if s.startswith("INSERT OR REPLACE INTO turns")]
    assert len(writes) == 2  # the changed turn and the new one, not all 41
    assert saved_turns(store) == turns


class _Wrap:
    def __init__(self, context, db):
        self.context, self.db = context, db

    def __enter__(self):
        return self.db

    def __exit__(self, *exc):
        return self.context.__exit__(*exc)


def test_a_step_started_over_drops_its_old_turns(store):
    state = make_state(store, steps=[step_spec("a")])
    state["plan"][0]["turns"] = [turn(i) for i in range(12)]
    store.save(state)
    state["plan"][0]["turns"] = [turn(100)]
    store.save(state)
    assert saved_turns(store) == [turn(100)]


def test_a_partial_save_keeps_the_turns_but_not_the_rest(store):
    state = make_state(store, steps=[step_spec("a")])
    store.save(state)
    state["facts"] = {"price": "42"}
    state["plan"][0]["turns"].append(turn(1, action="click", status="started"))
    store.save(state, whole=False)
    assert head(store)["facts"] == {} and saved_turns(store) == [turn(1, action="click", status="started")]
    store.save(state)
    assert head(store)["facts"] == {"price": "42"}


def test_a_stopped_run_keeps_its_turns_against_a_late_worker_save(store):
    state = make_state(store, steps=[step_spec("a")])
    state["plan"][0]["turns"] = [turn(1, action="click", status="uncertain")]
    store.save(state)
    store.execute("UPDATE runs SET status='stopped' WHERE id='run'")
    state["plan"][0]["turns"] = [turn(1, action="click", status="completed"), turn(2)]
    store.save(state, whole=False, event=("action_completed", {}))
    assert saved_turns(store) == [turn(1, action="click", status="uncertain")]
    assert store.rows("SELECT kind FROM events")[0]["kind"] == "action_completed"  # the trace still has it


def test_a_state_saved_whole_before_the_split_reads_and_saves_as_before(store):
    state = make_state(store, steps=[step_spec("a")])
    state["plan"][0]["turns"] = [turn(1), turn(2)]
    store.execute("UPDATE runs SET state=? WHERE id='run'", (json.dumps(state),))  # turns inside, the old way
    loaded = store.load("run")
    assert loaded["plan"][0]["turns"] == [turn(1), turn(2)]
    loaded["plan"][0]["turns"].append(turn(3))
    store.save(loaded)
    assert saved_turns(store) == [turn(1), turn(2), turn(3)] and "turns" not in head(store)["plan"][0]


def test_a_checkpoint_holds_the_turns_and_puts_them_back(store):
    state = make_state(store, steps=[step_spec("a")])
    state["plan"][0]["turns"] = [turn(1)]
    store.save(state)
    with store.connect() as db:
        before = dump_state(read_state(db, "run"))
    state["plan"][0]["turns"].append(turn(2))
    store.save(state)
    with store.connect() as db:
        put_state(db, json.loads(before))
    assert saved_turns(store) == [turn(1)] and store.load("run")["plan"][0]["turns"] == [turn(1)]


def test_the_runner_saves_the_whole_state_on_a_status_change_and_before_a_model_call(store):
    state = make_state(store, steps=[step_spec("a")])
    runner = Runner(store, model=object(), devices=object())
    runner.emit(state, "run_started", {})  # the first save is whole
    state["facts"] = {"seen": "x"}
    state["plan"][0]["turns"].append(turn(1))
    runner.emit(state, "ui_started", {})  # nothing changed status: turns only
    assert head(store)["facts"] == {} and saved_turns(store) == [turn(1)]
    runner.flush(state)  # what Runner.call and Runner.act do first
    assert head(store)["facts"] == {"seen": "x"}
    state["facts"] = {"seen": "y"}
    state["plan"][0]["status"] = "done"
    runner.emit(state, "node_status", {})  # a step's status changed: whole
    assert head(store)["facts"] == {"seen": "y"} and head(store)["plan"][0]["status"] == "done"
    assert [r["kind"] for r in store.rows("SELECT kind FROM events ORDER BY id")] == ["run_started", "ui_started", "node_status"]



def test_a_reused_connection_reads_what_another_one_wrote_after_an_unfinished_query(store, tmp_path):
    from app.store import Store

    other = Store(tmp_path)  # another process, as far as the database goes
    make_state(store)
    with store.connect() as db:
        kept = db.execute("SELECT id FROM runs")
        kept.fetchone()  # left unfinished, and still referenced
    other.execute("UPDATE runs SET status='stopped' WHERE id='run'")
    assert store.status("run") == "stopped"
