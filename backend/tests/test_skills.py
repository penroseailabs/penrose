"""Skills: authored app and task know-how, indexed for the orchestrator, read in full where it matters."""

from app import skills
from app.runtime.orchestrator import previous_attempt
from fakes import (
    FakeDevices,
    Script,
    finish,
    make_state,
    orch,
    phone,
    report,
    run_step,
    runner_for,
    sections,
    set_plan,
    step_spec,
)
from phone import FakePhone, screen_of

CHROME = "com.android.chrome"


def skill_tree(tmp_path):
    (tmp_path / "apps").mkdir(parents=True)
    (tmp_path / "tasks").mkdir(parents=True)
    (tmp_path / "apps" / "google-search.md").write_text(
        "---\nname: google-search\ndescription: Web search by URL.\npackages: com.android.chrome, com.google.android.googlequicksearchbox\n"
        "apps: Chrome, Google\n---\n\nSearch with open_link.\n", encoding="utf-8")
    (tmp_path / "tasks" / "whats-the-story.md").write_text(
        "---\nname: whats-the-story\ndescription: What is X.\n---\n\nOne step in Chrome.\n", encoding="utf-8")
    return tmp_path


def test_a_skill_file_is_its_front_matter_and_its_body(tmp_path):
    root = skill_tree(tmp_path)
    assert skills.index(root) == {
        "apps": [{"name": "google-search", "description": "Web search by URL.",
                  "packages": [CHROME, "com.google.android.googlequicksearchbox"], "apps": ["Chrome", "Google"]}],
        "tasks": [{"name": "whats-the-story", "description": "What is X."}],
    }
    assert skills.recipes(root) == [{"name": "whats-the-story", "description": "What is X.", "content": "One step in Chrome."}]


def test_an_app_skill_is_found_by_package_or_by_name(tmp_path):
    root = skill_tree(tmp_path)
    found = "(google-search)\nSearch with open_link."
    assert skills.for_app({"name": "Browser", "package": CHROME}, root) == found
    assert skills.for_app({"name": "chrome"}, root) == found
    assert skills.for_app({"name": "Instacart", "package": "com.instacart.client"}, root) is None
    assert skills.for_app(None, root) is None


def test_a_file_without_front_matter_is_a_skill_named_after_its_file(tmp_path):
    (tmp_path / "apps").mkdir()
    (tmp_path / "apps" / "plain.md").write_text("Just text.", encoding="utf-8")
    assert skills.load("apps", tmp_path) == [{"name": "plain", "description": "", "packages": [], "apps": [], "tools": [], "content": "Just text."}]
    assert skills.load("tasks", tmp_path) == []


def test_the_shipped_skills_parse_and_name_their_packages():
    assert skills.for_app({"name": "Chrome"}).startswith("(google-search)")
    assert skills.for_app({"package": "com.twitter.android"}).startswith("(x)")
    assert {s["name"] for s in skills.recipes()} >= {"whats-the-story", "compare-and-buy"}
    for skill in skills.load("apps"):
        assert skill["packages"] and skill["description"], skill["name"]


def test_a_tool_skill_is_read_in_full_by_the_agents_with_that_tool(tmp_path):
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "look-up.md").write_text(
        "---\nname: look-up\ndescription: Search well.\ntools: look_up, home_search\n---\n\nShort words.\n", encoding="utf-8")
    assert skills.for_tools(["finish", "look_up"], tmp_path) == "## How to use look_up, home_search (look-up skill)\n\nShort words."
    assert skills.for_tools(["finish"], tmp_path) == ""
    assert "tools" not in skills.index(tmp_path)


def test_the_shipped_look_up_skill_reaches_the_orchestrator_only():
    from app.agents.registry import GATE, ORCHESTRATOR, SUBAGENT

    assert "How to use look_up (look-up skill)" in ORCHESTRATOR.instructions
    assert "How to use look_up" not in GATE.instructions and "How to use look_up" not in SUBAGENT.instructions


def test_the_shipped_sign_in_playbook_reaches_the_subagent_and_the_orchestrator():
    from app.agents.registry import GATE, ORCHESTRATOR, SUBAGENT, VERIFIER

    for agent in (SUBAGENT, ORCHESTRATOR):
        assert "(sign-in skill)" in agent.instructions
        assert "Approve sign in request" in agent.instructions  # Microsoft's number-matching page
    assert "(sign-in skill)" not in GATE.instructions and "(sign-in skill)" not in VERIFIER.instructions


async def test_the_orchestrator_sees_the_index_and_the_recipes_on_every_turn(store, tmp_path, monkeypatch):
    monkeypatch.setattr(skills, "SKILLS", skill_tree(tmp_path / "skills"))

    def orchestrator(context, n):
        if not context["plan"]:
            return orch(set_plan(step_spec("s1", "Search", app={"name": "Chrome"})), run_step("s1"))
        return orch(finish("done"))

    model = Script(Orchestrator=orchestrator, Subagent=lambda text, n: phone(report()))
    await runner_for(store, model).run(make_state(store, request="What is the emudra issue?", route=None))
    first, second = model.of("Orchestrator")[:2]
    assert first["context"]["skills"]["tasks"] == [{"name": "whats-the-story", "description": "What is X."}]
    assert [r["name"] for r in first["context"]["recipes"]] == ["whats-the-story"]
    # Also once there is a plan: leaving them out moved everything after them, which Claude then wrote to its
    # cache again, at many times the price of reading the recipes.
    assert second["context"]["plan"] and second["context"]["recipes"] == first["context"]["recipes"]
    assert list(second["context"])[:2] == ["skills", "recipes"]


async def test_a_subagent_in_an_app_with_a_skill_reads_it_in_full(store):
    def orchestrator(context, n):
        if not context["plan"]:
            return orch(set_plan(step_spec("s1", "Search", app={"name": "Chrome", "package": CHROME})), run_step("s1"))
        return orch(finish("done"))

    model = Script(Orchestrator=orchestrator, Subagent=lambda text, n: phone(report()))
    await runner_for(store, model).run(make_state(store, request="What is the emudra issue?", route=None))
    brief = sections(model.of("Subagent")[0]["context"])
    assert brief["app_skill"].startswith("(google-search)") and "open_link" in brief["app_skill"]
    assert "\n\n" not in brief["app_skill"]  # one section of the state text, not several


async def test_the_progress_list_stays_in_front_of_the_subagent_and_reaches_the_next_attempt(store):
    plan = "[done] Opened Chrome\n[now] Search"
    turns = [phone({"tool": "progress", "text": plan}, {"tool": "interact", "element": 1, "interaction": "tap"}),
             phone(report(status="failed", summary="Blocked."))]
    devices = FakeDevices(FakePhone(screen_of("First"), screen_of("Second")))

    def orchestrator(context, n):
        if not context["plan"]:
            return orch(set_plan(step_spec("s1", "Search")), run_step("s1"))
        return orch(finish("done"))

    model = Script(Orchestrator=orchestrator, Subagent=turns)
    state = make_state(store, request="Search", route=None)
    await runner_for(store, model, devices=devices).run(state)
    first, second = model.of("Subagent")[:2]
    assert "Your progress" not in first["context"]
    assert sections(second["context"])["Your progress"] == plan
    assert previous_attempt(state["plan"][0])["progress"] == plan
