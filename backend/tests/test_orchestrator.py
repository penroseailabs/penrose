"""The orchestrator's loop: plan, run each step with a fresh subagent, read its report, verify, ask, finish."""

import json
import re

from app.config import settings
from app.plan import new_step, plan_view, step_view
from fakes import (
    FakeDevices,
    Script,
    ask_user,
    click,
    finish,
    handover,
    kinds,
    log,
    make_state,
    orch,
    phone,
    report,
    run_step,
    runner_for,
    sections,
    set_plan,
    step_spec,
    verdict,
    verify,
)
from phone import FakePhone, screen_of

LOGIN, CART = step_spec("login", "Log in to Instacart"), step_spec("cart", "Add milk to the cart")


def two_step_orchestrator(context, n):
    """Plan two steps, run them in order, then finish with what the reports said."""
    plan = {s["id"]: s for s in context["plan"]}
    if not plan:
        return orch(set_plan(LOGIN, CART, context="Order milk on Instacart"), run_step("login"), reply="Logging in first.")
    if plan["login"]["status"] != "done":
        return orch(run_step("login", note="try again"))
    if plan["cart"]["status"] == "pending":
        return orch(run_step("cart"))
    return orch(finish(f"Done: {context['facts']}"))


def step_field(text, key):
    """A field of the step in a subagent's state text ("id", "goal")."""
    return re.search(rf"^{key}: (.*)$", sections(text)["step"], re.M).group(1)


def subagent_reports(text, n):
    return phone(report(summary=f"{step_field(text, 'goal')}: done", facts={step_field(text, "id"): "ok"}))


async def test_each_step_gets_a_fresh_subagent_and_only_its_report_comes_back(store):
    state = make_state(store, request="Order milk", route=None)
    model = Script(Orchestrator=two_step_orchestrator, Subagent=subagent_reports)
    runner = runner_for(store, model)
    await runner.run(state)

    assert state["status"] == "completed"
    assert [s["status"] for s in state["plan"]] == ["done", "done"]
    assert state["facts"] == {"login": "ok", "cart": "ok"}
    # The second subagent never sees the first one's turns: only the facts and the plan.
    second = sections(model.of("Subagent")[1]["context"])
    assert "id: cart" in second["step"] and second["facts"] == "login: ok"
    assert second["Recent actions, oldest first"] == "(none)" and "previous_attempt" not in second
    # The orchestrator reads reports and the journal, never a subagent's turns.
    last = model.of("Orchestrator")[-1]["context"]
    assert last["latest"]["report"]["summary"] == "Add milk to the cart: done"
    assert "turns" not in json.dumps(last["plan"])
    assert any("login done" in line for line in last["journal"])
    # One lease for the whole run, released at the end.
    assert runner.devices.acquired == ["run"] and runner.devices.released == ["run"]
    # The orchestrator's notes and its final answer reach the chat; subagent turns never do.
    chat = [m["content"] for m in store.rows("SELECT content FROM messages ORDER BY created")]
    assert chat[0] == "Logging in first." and chat[-1].startswith("Done:")
    assert "completed" in kinds(store)


async def test_the_frontend_sees_the_plan_as_a_chain_of_nodes(store):
    state = make_state(store)
    await runner_for(store, Script(Orchestrator=two_step_orchestrator, Subagent=subagent_reports)).run(state)
    saved = store.load("run")
    assert list(saved["nodes"]) == ["login", "cart"]
    assert saved["nodes"]["cart"]["spec"]["depends_on"] == ["login"]
    assert saved["nodes"]["login"]["status"] == "succeeded"
    assert saved["nodes"]["login"]["output"] == "Log in to Instacart: done"


async def test_a_failed_step_is_run_again_with_the_orchestrators_note_and_what_the_last_attempt_did(store):
    state = make_state(store)
    attempts = []

    def subagent(text, n):
        attempts.append(sections(text))
        if n == 0:
            return phone(log("App open on the login screen"), click(1))
        if n == 1:
            return phone(report("failed", "The OTP screen never came."))
        return phone(report(summary="Logged in."))

    model = Script(Orchestrator=two_step_orchestrator, Subagent=subagent)
    phone_ = FakePhone(screen_of("Welcome"), screen_of("Login"))
    await runner_for(store, model, phone_).run(state)
    retry = attempts[2]
    assert retry["note_from_orchestrator"] == "try again"
    previous = json.loads(retry["previous_attempt"])
    assert previous["report"]["summary"] == "The OTP screen never came."
    assert [line.split(" | ", 1)[1] for line in previous["log"]] == ["App open on the login screen"]
    assert "tapped [1] Continue" in previous["last_actions"][0]
    log_part = next(v for k, v in retry.items() if k.startswith("Your log"))
    assert log_part.endswith("(nothing yet)")  # a new attempt starts its own log
    assert state["plan"][0]["attempts"] == 2 and state["plan"][0]["status"] == "done"


async def test_an_unfinished_handover_is_evaluated_and_a_rewritten_step_keeps_what_it_found(store):
    state = make_state(store)
    seen, attempts = [], []
    website = step_spec("login", "Log in on the Instacart website")

    def orchestrator(context, n):
        seen.append(context)
        if n == 0:
            return orch(set_plan(LOGIN, context="Log in"), run_step("login"))
        if n == 1:  # another route, under the same id
            return orch(set_plan(website), run_step("login", note="continue from the handover"))
        return orch(finish("done"))

    def subagent(text, n):
        attempts.append(sections(text))
        if "Turn: handover" in text:
            return phone(handover("Login screen reached", "control_missing", facts={"screen": "login"}))
        if n < 4:
            return phone(log("The Login button has no number"), click(1))
        return phone(report(summary="Logged in."))

    await runner_for(store, Script(Orchestrator=orchestrator, Subagent=subagent),
                     FakePhone(lambda done: screen_of("Same"))).run(state)
    latest = seen[1]["latest"]["report"]
    assert latest["status"] == "unfinished" and latest["handover"]["cause"] == "control_missing"
    assert latest["summary"].startswith("The runtime ended the session: the same actions were done")
    assert seen[1]["facts"] == {"screen": "login"} and seen[1]["plan"][0]["status"] == "unfinished"
    assert any(line.startswith("login handed back unfinished") for line in seen[1]["journal"])
    assert "problem" not in seen[1]["plan"][0]
    retry = json.loads(attempts[5]["previous_attempt"])
    assert retry["goal"] == "Log in to Instacart" and retry["report"]["status"] == "unfinished"
    assert [line.split(" | ", 1)[1] for line in retry["log"]] == ["The Login button has no number"] * 4 and retry["screens"]
    assert retry["log"][0].startswith("T+00:00 @ com.example MainActivity | ")  # where it was, said once
    assert attempts[5]["note_from_orchestrator"] == "continue from the handover"
    assert state["plan"][0]["goal"] == "Log in on the Instacart website" and state["plan"][0]["status"] == "done"


def test_a_step_that_ended_without_a_report_shows_the_orchestrator_what_its_session_kept():
    step = {**new_step(LOGIN), "status": "stopped", "sessions": {"subagent": {
        "progress": "[now] read Sat shows", "notes": ["Fri: 7:50 PM"], "trail": ["T+00:10 com.fandango ShowtimesActivity"]}}}
    assert step_view(step)["unreported_session"] == {
        "progress": "[now] read Sat shows", "notes": ["Fri: 7:50 PM"], "screens": ["T+00:10 com.fandango ShowtimesActivity"]}
    assert "unreported_session" not in step_view({**step, "report": {"status": "failed", "summary": "x"}})


def test_the_plan_names_a_reports_facts_and_their_values_are_sent_once_in_facts():
    report_ = {"status": "completed", "summary": "Found it", "facts": {"price": "$12", "store": "Target"}}
    step = {**new_step(LOGIN), "status": "done", "report": report_}
    assert plan_view({"plan": [step]})[0]["report"] == {**report_, "facts": ["price", "store"]}
    assert step_view(step)["report"] == report_  # the activity panel still gets the values


async def test_a_step_cannot_be_started_more_than_the_attempt_limit(store, monkeypatch):
    monkeypatch.setattr(settings, "max_step_attempts", 1)
    state = make_state(store)
    seen = []

    def orchestrator(context, n):
        seen.append(context)
        if n == 0:
            return orch(set_plan(LOGIN), run_step("login"))
        if n == 1:
            return orch(run_step("login"))  # rejected: one attempt only
        return orch(finish("Could not log in."))

    model = Script(Orchestrator=orchestrator, Subagent=lambda c, n: phone(report("failed", "Blocked.")))
    await runner_for(store, model).run(state)
    assert "has had 1 attempts" in seen[2]["rejected_decision"]
    assert state["status"] == "completed" and state["plan"][0]["attempts"] == 1


async def test_steps_run_in_order(store):
    state = make_state(store)
    seen = []

    def orchestrator(context, n):
        seen.append(context)
        if n == 0:
            return orch(set_plan(LOGIN, CART), run_step("cart"))
        return orch(finish("Stopped."))

    await runner_for(store, Script(Orchestrator=orchestrator)).run(state)
    assert "the next one is login" in seen[1]["rejected_decision"]
    assert state["plan"] == []  # a rejected turn is applied not at all, its set_plan included


async def test_a_verifier_checks_a_done_step_without_its_history_and_a_failed_check_fails_the_step(store):
    state = make_state(store)

    def orchestrator(context, n):
        if n == 0:
            return orch(set_plan(CART), run_step("cart"))
        if n == 1:
            return orch(verify("cart", "Milk is in the cart", ["Open the cart"]))
        return orch(finish("Checked."))

    model = Script(Orchestrator=orchestrator, Subagent=lambda c, n: phone(report(summary="Added milk.")),
                   Verifier=lambda c, n: phone(verdict(False, "The cart is empty.")))
    runner = runner_for(store, model)
    await runner.run(state)
    text = model.of("Verifier")[0]["context"]
    check = sections(text)
    assert check["claim"] == "Milk is in the cart" and check["how"] == "- Open the cart"
    assert check["device_state"] == "wifi: on"
    assert "Added milk" not in text  # the verifier does not take the subagent's word
    step = state["plan"][0]
    assert step["status"] == "failed" and step["problem"] == "Verification failed: The cart is empty."
    assert model.of("Orchestrator")[2]["context"]["latest"]["verification"]["passed"] is False


async def test_only_a_done_step_can_be_verified(store):
    state = make_state(store, steps=[CART])
    seen = []

    def orchestrator(context, n):
        seen.append(context)
        return orch(verify("cart")) if n == 0 else orch(finish())

    await runner_for(store, Script(Orchestrator=orchestrator)).run(state)
    assert "Only a done step can be verified" in seen[1]["rejected_decision"]


async def test_a_step_that_failed_only_its_check_may_be_checked_once_more(store):
    state = make_state(store)
    seen = []

    def orchestrator(context, n):
        seen.append(context)
        if n == 0:
            return orch(set_plan(CART), run_step("cart"))
        if n in (1, 2, 3):  # check, check again (allowed), a third check (rejected)
            return orch(verify("cart", "Milk is in the cart", ["Open the cart"]))
        return orch(finish("Added milk; the cart could not be confirmed."))

    model = Script(Orchestrator=orchestrator, Subagent=lambda c, n: phone(report(summary="Added milk.")),
                   Verifier=lambda c, n: phone(verdict(False, "Could not tell.")))
    await runner_for(store, model).run(state)
    assert len(model.of("Verifier")) == 2 and "rejected_decision" not in seen[2]
    assert "checked 2 times" in seen[4]["rejected_decision"]
    assert state["status"] == "completed"


async def test_a_decision_rejected_twice_ends_the_turn_with_what_there_is_not_a_failed_run(store):
    state = make_state(store)
    seen = []

    def orchestrator(context, n):
        seen.append(context)
        return orch(run_step("nope")) if n < 2 else orch(finish("Here is where it stands."))

    await runner_for(store, Script(Orchestrator=orchestrator)).run(state)
    assert seen[2]["must_conclude"] is True and "Twice now" in seen[2]["rejected_decision"]
    assert state["status"] == "completed"


async def test_a_decision_rejected_three_times_fails_the_run(store):
    state = make_state(store)
    await runner_for(store, Script(Orchestrator=lambda c, n: orch(run_step("nope")))).run(state)
    assert state["status"] == "failed"


async def test_needs_input_goes_to_the_orchestrator_which_asks_the_user_and_the_answer_restarts_the_step(store):
    state = make_state(store)

    def orchestrator(context, n):
        if n == 0:
            return orch(set_plan(CART), run_step("cart"))
        if context["trigger"] == "user_message":
            return orch(run_step("cart", note=f"The user chose: {context['new_user_messages'][0]}"))
        if context["latest"] and context["latest"]["report"]["status"] == "needs_input":
            return orch(ask_user("Horizon or Organic Valley?"))
        return orch(finish("Added."))

    def subagent(text, n):
        if sections(text).get("note_from_orchestrator"):
            return phone(report(summary="Added Horizon."))
        return phone(report("needs_input", "Two brands match.", question="Horizon or Organic Valley?"))

    model = Script(Orchestrator=orchestrator, Subagent=subagent)
    runner = runner_for(store, model)
    await runner.run(state)
    assert state["status"] == "waiting_user" and state["plan"][0]["status"] == "needs_input"
    assert store.rows("SELECT content FROM messages")[-1]["content"] == "Horizon or Organic Valley?"
    # The user answers in the chat; the run continues with the orchestrator.
    state["inbox"] = ["Horizon"]
    await runner.run(state)
    assert state["status"] == "completed" and state["plan"][0]["status"] == "done"
    assert any(line == "user: Horizon" for line in (j["line"] for j in state["journal"]))


async def test_what_the_user_is_asked_for_is_kept_as_one_line_until_they_answer(store):
    state = make_state(store)

    def orchestrator(context, n):
        if context["trigger"] == "user_message":
            return orch(finish("Booked for the morning."))
        return orch({**ask_user("Morning or evening?"), "needs": "  Choose a   time for the technician "})

    runner = runner_for(store, Script(Orchestrator=orchestrator))
    await runner.run(state)
    assert state["status"] == "waiting_user" and state["needs"] == "Choose a time for the technician"
    state["inbox"] = ["Morning"]
    await runner.run(state)
    assert state["status"] == "completed" and "needs" not in state


async def test_a_resumed_run_leases_its_phone_again_instead_of_trusting_the_saved_slot(store):
    # Stopping releases the lease after the run's state was saved, so the slot in it can be stale.
    state = make_state(store, slot="device-1")
    model = Script(Orchestrator=lambda c, n: orch(set_plan(CART), run_step("cart")) if n == 0 else orch(finish("Added.")),
                   Subagent=lambda text, n: phone(report(summary="Added.")))
    devices = FakeDevices()
    await runner_for(store, model, devices=devices).run(state)
    assert state["status"] == "completed" and devices.acquired == [state["id"]]
    assert "node_status" in kinds(store)
    assert not any("waiting_device" in r["payload"] for r in store.rows("SELECT payload FROM events"))


async def test_a_private_code_parks_the_step_and_the_same_session_continues_after_it_is_typed(store):
    state = make_state(store)

    def subagent(text, n):
        if n == 0:
            return phone({"tool": "ask_for_password_or_code", "question": "The OTP Instacart sent"})
        assert "typed the user's private entry" in sections(text)["Notices"]
        assert sections(text)["Last result"] == "ask_for_password_or_code -> asked the user: The OTP Instacart sent"
        return phone(report(summary="Logged in."))

    def orchestrator(context, n):
        if n == 0:
            return orch(set_plan(LOGIN), run_step("login"))
        return orch(finish("Logged in."))

    model = Script(Orchestrator=orchestrator, Subagent=subagent)
    devices = FakeDevices()
    runner = runner_for(store, model, devices=devices)
    await runner.run(state)
    step = state["plan"][0]
    assert state["status"] == "waiting_user" and step["status"] == "waiting_user"
    assert step["question"] == {"question": "The OTP Instacart sent", "sensitive": True}
    assert state["questions"][0]["node_id"] == "login"
    assert devices.released == []  # the phone stays leased while the user types the code
    # The user enters the code privately (app/api/runs.py stores it and marks the step to resume).
    store.execute("INSERT INTO secrets VALUES (?,?,?)", ("run", "login", "123456"))
    step["status"] = "resume"
    await runner.run(state)
    assert state["status"] == "completed"
    assert {"kind": "type_text", "text": "123456", "sensitive": True} in devices.actions
    assert "123456" not in json.dumps(model.calls) and "123456" not in json.dumps(store.rows("SELECT * FROM events"))
    assert len(model.of("Orchestrator")) == 2  # resuming the step needed no orchestrator turn


async def test_a_plan_with_nothing_started_runs_its_first_step(store):
    state = make_state(store)
    model = Script(Orchestrator=lambda c, n: orch(set_plan(CART)) if n == 0 else orch(finish("Added.")),
                   Subagent=lambda text, n: phone(report(summary="Added.")))
    await runner_for(store, model).run(state)
    assert state["status"] == "completed" and state["plan"][0]["status"] == "done"
    assert len(model.of("Orchestrator")) == 2  # the plan-only turn was not rejected


async def test_a_plan_written_beside_kept_findings_runs_its_first_step(store):
    """Findings kept in the turn that writes the plan change nothing: its first step starts, in the same turn."""
    kept = {"tool": "remember", "findings": ["Milk comes from Instacart"]}
    turns = [orch(keep("The mail had nothing: the app is next"), kept, set_plan(CART), reply="Trying the app."), orch(finish("Added."))]
    state = make_state(store)
    model = Script(Orchestrator=lambda c, n: turns[n], Subagent=lambda text, n: phone(report(summary="Added.")),
                   Memory=lambda c, n: {"actions": [{"tool": "remember", "save": [], "forget": []}]})
    await runner_for(store, model).run(state)
    assert state["status"] == "completed" and state["plan"][0]["status"] == "done"
    assert len(model.of("Orchestrator")) == 2 and len(model.of("Subagent")) == 1


async def test_a_private_code_is_not_typed_after_another_task_used_the_phone(store):
    from app.device.client import Slot

    state = make_state(store)

    def subagent(text, n):
        if n == 0:
            return phone({"tool": "ask_for_password_or_code", "question": "The OTP Instacart sent"})
        notices = sections(text)["Notices"]
        assert "another task used the phone" in notices.lower() and "NOT typed" in notices
        return phone(report(summary="Asked again."))

    def orchestrator(context, n):
        if n == 0:
            return orch(set_plan(LOGIN), run_step("login"))
        return orch(finish("Done."))

    model = Script(Orchestrator=orchestrator, Subagent=subagent)
    devices = FakeDevices()
    runner = runner_for(store, model, devices=devices)
    await runner.run(state)
    assert state["status"] == "waiting_user"
    store.execute("INSERT INTO secrets VALUES (?,?,?)", ("run", "login", "123456"))
    state["plan"][0]["status"] = "resume"

    async def lost(owner):  # the lease was taken by another task while the run waited
        slot = Slot("device-1")
        slot.kept = False
        return slot

    devices.acquire = lost
    await runner.run(state)
    assert state["status"] == "completed"
    assert not any(a.get("text") == "123456" for a in devices.actions)
    assert store.consume_secret("run", "login") is None


async def test_a_sign_in_handed_to_the_user_parks_the_step_and_their_note_reaches_the_same_session(store):
    state = make_state(store)

    def subagent(text, n):
        if n == 0:
            return phone({"tool": "ask_login", "app": "Instagram", "why": "The verification screen cannot be read."})
        assert "From the user: I signed in on the phone myself. Continue from the screen." in sections(text)["Notices"]
        return phone(report(summary="Signed in."))

    def orchestrator(context, n):
        if n == 0:
            return orch(set_plan(LOGIN), run_step("login"))
        return orch(finish("Signed in."))

    model = Script(Orchestrator=orchestrator, Subagent=subagent)
    devices = FakeDevices()
    runner = runner_for(store, model, devices=devices)
    await runner.run(state)
    step = state["plan"][0]
    assert state["status"] == "waiting_user" and step["status"] == "waiting_user"
    assert step["question"] == {"question": "Sign in to Instagram on the phone. The verification screen cannot be read.",
                                "sensitive": False, "kind": "login"}
    assert state["questions"][0]["kind"] == "login" and devices.released == []
    # The user signs in on the phone panel and says so (app/api/runs.py keeps the note and marks the step to resume).
    step["answers"] = ["I signed in on the phone myself. Continue from the screen."]
    step["status"] = "resume"
    await runner.run(state)
    assert state["status"] == "completed" and "answers" not in step
    assert len(model.of("Orchestrator")) == 2


async def test_the_last_orchestrator_turn_must_ask_or_finish(store, monkeypatch):
    monkeypatch.setattr(settings, "max_orchestrator_turns", 2)
    state = make_state(store)
    seen = []

    def orchestrator(context, n):
        seen.append(context)
        if n == 0:
            return orch(set_plan(LOGIN), run_step("login"))
        if n == 1:
            return orch(run_step("login"))
        return orch(finish("Out of turns."))

    model = Script(Orchestrator=orchestrator, Subagent=lambda c, n: phone(report("failed", "Blocked.")))
    await runner_for(store, model).run(state)
    assert seen[1]["must_conclude"] is True and "last turn" in seen[2]["rejected_decision"]
    assert state["status"] == "completed"


async def test_set_plan_keeps_done_steps_and_replaces_the_rest(store):
    from app.tools.orchestrator import SetPlan

    state = make_state(store, steps=[LOGIN, CART])
    state["plan"][0]["status"] = "done"
    new = SetPlan(steps=[step_spec("pay", "Pay with cash")]).merged(state["plan"])
    assert [s["id"] for s in new] == ["login", "pay"]
    redo = SetPlan(steps=[step_spec("login", "Log in again"), step_spec("pay")]).merged(state["plan"])
    assert [(s["id"], s["status"]) for s in redo] == [("login", "pending"), ("pay", "pending")]


async def test_the_gate_answers_simple_messages_without_the_orchestrator(store):
    state = make_state(store, route=None)
    model = Script(**{"Fast response": lambda c, n: {"actions": [{"tool": "answer", "text": "Hello!"}]}})
    await runner_for(store, model).run(state)
    assert state["status"] == "completed" and not model.of("Orchestrator")
    assert store.rows("SELECT content, source FROM messages") == [{"content": "Hello!", "source": "Fast response"}]


async def test_a_follow_up_task_sees_the_previous_tasks_facts(store):
    state = make_state(store)
    await runner_for(store, Script(Orchestrator=two_step_orchestrator, Subagent=subagent_reports)).run(state)
    follow = {**state, "id": "run2", "plan": [], "facts": {}, "journal": [], "status": "running"}
    store.execute("INSERT INTO runs VALUES (?,?,?,?,?)", ("run2", "chat", "running", json.dumps(follow), 0))
    model = Script(Orchestrator=lambda c, n: orch(finish("ok")))
    await runner_for(store, model).run(follow)
    previous = model.of("Orchestrator")[0]["context"]["previous_task"]
    assert previous["facts"] == {"login": "ok", "cart": "ok"}
    assert previous["ended"].endswith("UTC")  # when its facts were true; no time zone is set in tests


async def test_stop_marks_the_working_step_stopped_and_unconfirmed_actions_uncertain(store):
    from app.store import halt

    state = make_state(store, steps=[LOGIN])
    state["plan"][0].update(status="running", turns=[{"summary": "Tap Pay", "action": "click", "status": "started"}])
    halt(state)
    assert state["plan"][0]["status"] == "stopped"
    assert state["plan"][0]["turns"][0]["status"] == "uncertain"
    assert state["nodes"]["login"]["status"] == "stopped"


async def test_a_question_step_gets_the_short_time_budget_and_every_brief_says_where_the_phone_is(store):
    state = make_state(store, request="pharmacy near me", route=None)
    devices = FakeDevices()
    devices.fix = {"lat": 53.443, "lng": -2.2746, "accuracy": 10}
    seen = []

    def orchestrator(context, n):
        seen.append(context)
        if n == 0:
            return orch(set_plan(step_spec("look", "Find a pharmacy", kind="question"), step_spec("buy", "Order it")), run_step("look"))
        if n == 1:
            return orch(run_step("buy"))
        return orch(finish("done"))

    def subagent(text, n):
        brief = sections(text)
        assert brief["phone_location"].startswith("GPS at lat 53.4430, lng -2.2746 (the user's computer's position")
        budget = "1. Clock: T+00:00 of 04:00." if n == 0 else "1. Clock: T+00:00 of 30:00."
        assert brief["Turn"] == budget, brief["Turn"]
        assert ("kind: question" in brief["step"]) is (n == 0)
        return phone(report(summary="ok"))

    await runner_for(store, Script(Orchestrator=orchestrator, Subagent=subagent), devices=devices).run(state)
    assert state["status"] == "completed"
    assert seen[0]["phone_location"] == {"lat": 53.443, "lng": -2.2746}  # before the phone was leased: the one phone there is
    assert seen[1]["plan"][0]["kind"] == "question" and seen[1]["plan"][1]["kind"] == "action"
    assert devices.refreshed == 2  # the fix is sent again as each step starts


async def test_without_a_fix_the_brief_says_so_and_nothing_is_asked_of_the_gateway_twice(store):
    state = make_state(store, request="weather", route=None)
    devices = FakeDevices()
    briefs = []

    def orchestrator(context, n):
        assert context["phone_location"] is None
        if n == 0:
            return orch(set_plan(step_spec("s1", "Look it up")), run_step("s1"))
        return orch(finish("done"))

    def subagent(text, n):
        briefs.append(sections(text)["phone_location"])
        return phone(log("x"), click(1)) if n == 0 else phone(report())

    await runner_for(store, Script(Orchestrator=orchestrator, Subagent=subagent), devices=devices).run(state)
    assert briefs == ["not set on this phone (no fix was given). Apps may show no position; ask nothing and move nothing."] * 2


async def test_a_task_that_used_its_phone_time_must_conclude_until_the_user_writes_again(store, monkeypatch):
    monkeypatch.setattr(settings, "task_phone_minutes", 1e-9)
    state = make_state(store, request="find things", route=None)
    seen = []

    def orchestrator(context, n):
        seen.append(context)
        if n == 0:
            assert context["must_conclude"] is False and context["phone_time"]["used_minutes"] == 0
            return orch(set_plan(step_spec("s1", "Look", kind="question"), step_spec("s2", "Look more", kind="question")), run_step("s1"))
        if n == 1:
            assert context["must_conclude"] is True and context["phone_time"]["budget_minutes"] == 1e-9
            return orch(run_step("s2"))  # rejected: the task must conclude
        if n == 2:
            return orch(ask_user("Shall I go on?"))
        if n == 3:  # the user answered: a fresh budget
            assert context["must_conclude"] is False and context["phone_time"]["used_minutes"] == 0
            return orch(run_step("s2"))
        return orch(finish("done"))

    runner = runner_for(store, Script(Orchestrator=orchestrator, Subagent=lambda text, n: phone(report())))
    await runner.run(state)
    assert state["status"] == "waiting_user" and "rejected_decision" in seen[2]
    assert "last turn" in seen[2]["rejected_decision"] and state.get("phone_seconds", 0) > 0
    state["inbox"] = ["yes"]
    await runner.run(state)
    assert state["status"] == "completed" and [s["status"] for s in state["plan"]] == ["done", "done"]


# --- notes to itself: what a turn worked out that the plan, the facts and the journal do not show ------------

RULED_OUT, LEAD = "Ruled out the camp stove: Sam already has one (Amazon order email)", "Not checked yet: Target order history"


def keep(*lines):
    return {"tool": "note", "notes": "\n".join(lines)}


class Thinks(Script):
    """A model that reasons before every turn."""

    async def ask(self, agent, context, on_thinking=None, **kw):
        if on_thinking:
            await on_thinking("Weighing the options.")
        return await super().ask(agent, context, on_thinking=on_thinking, **kw)


async def test_the_orchestrator_reads_its_own_notes_on_later_turns_and_in_the_task_that_follows(store):
    state = make_state(store)

    def orchestrator(context, n):
        if n == 0:
            return orch(keep(RULED_OUT), set_plan(CART), run_step("cart"))
        return orch(keep(LEAD, RULED_OUT), finish("Added."))

    model = Script(Orchestrator=orchestrator, Subagent=subagent_reports)
    await runner_for(store, model).run(state)
    first, second = (call["context"] for call in model.of("Orchestrator"))
    assert "notes" not in first and second["notes"] == [RULED_OUT]
    assert state["status"] == "completed" and state["notes"] == [RULED_OUT, LEAD]  # one kept twice is kept once
    assert "camp stove" not in json.dumps(state["journal"])  # the journal says what happened, not what was thought
    follow = {**state, "id": "run2", "plan": [], "facts": {}, "journal": [], "notes": [], "status": "running"}
    store.execute("INSERT INTO runs VALUES (?,?,?,?,?)", ("run2", "chat", "running", json.dumps(follow), 0))
    model = Script(Orchestrator=lambda c, n: orch(finish("ok")))
    await runner_for(store, model).run(follow)
    assert model.of("Orchestrator")[0]["context"]["previous_task"]["notes"] == [RULED_OUT, LEAD]


async def test_notes_join_any_turn_and_a_turn_of_notes_alone_is_followed_by_a_move(store):
    state = make_state(store)
    turns = [orch(keep(LEAD)), orch(keep(RULED_OUT), set_plan(CART)), orch(finish("Added."))]
    model = Script(Orchestrator=turns, Subagent=subagent_reports)
    await runner_for(store, model).run(state)
    # The notes-only turn was a turn, and the plan beside a note still started its first step.
    assert state["status"] == "completed" and state["plan"][0]["status"] == "done" and len(model.of("Orchestrator")) == 3
    assert state["notes"] == [LEAD, RULED_OUT]


async def test_the_chat_keeps_each_turns_notes_and_the_reasoning_of_a_turn_that_said_nothing(store):
    from app.api.chats import thoughts

    state = make_state(store)
    turns = [orch(keep(RULED_OUT), set_plan(CART), run_step("cart")), orch(run_step("cart")), orch(keep(LEAD), finish("Added."))]
    model = Thinks(Orchestrator=turns, Subagent=lambda text, n: phone(report("failed" if n == 0 else "completed")))
    await runner_for(store, model).run(state)
    with store.connect() as db:
        planned, again, ended = thoughts(db, "chat")
    # Said nothing: its reasoning is kept here, with its notes. No notes: the reasoning alone.
    assert (planned["thinking"], planned["notes"]) == ("Weighing the options.", [RULED_OUT])
    assert (again["thinking"], again["notes"]) == ("Weighing the options.", [])
    # Wrote to the user: the reasoning is on its message (the same id), the notes here.
    message = store.rows("SELECT id, thinking FROM messages WHERE role='assistant' AND content='Added.'")[0]
    assert ended == {"id": message["id"], "created": ended["created"], "thinking": "", "notes": [LEAD]}
    assert message["thinking"] == "Weighing the options."


# --- the phone's apps and the step's app's shortcuts in the brief (2026-11-22) -----------------------------------

PHONE_APPS = [
    {"name": "Amazon", "package": "com.amazon.mShop.android.shopping", "shortcuts": ["Search", "Orders", "Cart"]},
    {"name": "Maps", "package": "com.google.android.apps.maps", "shortcuts": []},
]


async def test_the_brief_names_every_app_and_the_shortcuts_of_the_steps_app(store):
    devices = FakeDevices()
    devices.apps_list = PHONE_APPS
    amazon, maps, absent = ({"name": name, "package": package} for name, package in (
        ("Amazon", "com.amazon.mShop.android.shopping"), ("Maps", "com.google.android.apps.maps"), ("Lyft", "me.lyft.android")))

    def orchestrator(context, n):
        if n == 0:
            return orch(set_plan(step_spec("orders", "Read the last order", app=amazon), step_spec("route", "Find the way", app=maps),
                                 step_spec("ride", "Book a ride", app=absent)), run_step("orders"))
        return orch(run_step(("route", "ride")[n - 1])) if n < 3 else orch(finish("done"))

    turns = [phone(log("x"), click(1)), phone(report()), phone(report()), phone(report())]
    model = Script(Orchestrator=orchestrator, Subagent=turns)
    await runner_for(store, model, devices=devices).run(make_state(store, request="orders", route=None))
    first, second, route, ride = (sections(call["context"]) for call in model.of("Subagent"))
    # Every app with an icon, by name, a system app (Maps) among them: on the session's first turn only.
    assert first["installed_apps"] == "- Amazon com.amazon.mShop.android.shopping\n- Maps com.google.android.apps.maps"
    assert "installed_apps" not in second and "installed_packages" not in first
    # Where the step's app can be opened straight to, on every turn of the step.
    assert first["app_entry_points"] == second["app_entry_points"] == "- Search\n- Orders\n- Cart"
    assert first["target_app_installed"] == "true"
    assert route["target_app_installed"] == "true" and "app_entry_points" not in route  # Maps offers none
    assert ride["target_app_installed"] == "false" and "app_entry_points" not in ride


async def test_the_verifier_reads_the_apps_too_and_may_not_open_a_shortcut(store):
    devices = FakeDevices()
    devices.apps_list = PHONE_APPS

    def orchestrator(context, n):
        if n == 0:
            return orch(set_plan(step_spec("orders", "Read the last order", app={"name": "Amazon", "package": PHONE_APPS[0]["package"]})),
                        run_step("orders"))
        return orch(verify("orders", "The order is listed", ["Open Orders"])) if n == 1 else orch(finish("done"))

    model = Script(Orchestrator=orchestrator, Subagent=lambda c, n: phone(report(summary="Read it.")),
                   Verifier=[phone({"tool": "open_app", "app": "Amazon", "shortcut": "Orders"}), phone(verdict(True, "Listed"))])
    await runner_for(store, model, devices=devices).run(make_state(store))
    first, second = (sections(call["context"]) for call in model.of("Verifier"))
    assert first["installed_apps"].splitlines()[0] == "- Amazon com.amazon.mShop.android.shopping"
    assert "app_entry_points" not in first and "installed_apps" not in second
    assert "a check opens the app and looks" in second["Last result"]
    assert not [op for op in devices.op_log if op.get("op") == "launch"]
