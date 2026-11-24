"""Authored knowledge the agents start from, on the pattern of Claude Code's skills: a folder of short
Markdown docs, each with a front matter that says what it is for; an index (name and description) the
planning agents always see; the full text only where it matters.

    backend/skills/apps/<slug>.md    how one app works: its deep links, login, where things are, what gets in
                                     the way. `packages` names the Android packages it is about and `apps` the
                                     names the orchestrator may use for it. The subagent (and the verifier)
                                     working in that app reads it in full, ahead of the memories learned on
                                     earlier visits.
    backend/skills/tasks/<slug>.md   a recipe for a kind of request that comes up again and again ("what are
                                     people saying about X"): where to look and the steps that work. The
                                     orchestrator reads the recipes in full beside their index, on every
                                     turn (they are the same on each, so the provider's cache holds them).
    backend/skills/tools/<slug>.md   how to use one of the agents' own tools well: what it searches, how to
                                     word a call, examples. `tools` names the tools it is about; every agent
                                     that has one of them reads it in full, in its instructions.

Memories (app/runtime/memory.py) are what the agents learn by themselves, capped and consolidated; skills
are what a person wrote and keeps in the repository. The same shape, the other source. A lesson that
keeps coming back for one app belongs in that app's skill.

The files are read on every call, like the prompts, so an edited skill applies to the next model call.
"""

import re
from pathlib import Path

SKILLS = Path(__file__).resolve().parents[1] / "skills"
FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.S)
LIST_KEYS = ("packages", "apps", "tools")


def parse(text, slug):
    """A skill file as {"name", "description", "packages", "apps", "tools", "content"}. The front matter is a
    flat `key: value` block; `packages`, `apps` and `tools` are comma-separated lists."""
    skill = {"name": slug, "description": "", "packages": [], "apps": [], "tools": [], "content": text.strip()}
    if match := FRONT_MATTER.match(text):
        for line in match.group(1).splitlines():
            key, sep, value = line.partition(":")
            key, value = key.strip().lower(), value.strip()
            if not sep or not key:
                continue
            if key in LIST_KEYS:
                skill[key] = [v.strip() for v in value.strip("[]").split(",") if v.strip()]
            elif key in ("name", "description"):
                skill[key] = value
        skill["content"] = text[match.end():].strip()
    return skill


def load(kind, root=None):
    """Every skill of one kind ("apps", "tasks" or "tools"), by file name."""
    folder = Path(root or SKILLS) / kind
    if not folder.is_dir():
        return []
    return [parse(path.read_text(encoding="utf-8"), path.stem) for path in sorted(folder.glob("*.md"))]


# --- what the agents read ---------------------------------------------------------------------------

def index(root=None):
    """What the orchestrator always sees: each skill's name and description, and for app skills which
    packages and names it covers. Empty when there are no skills."""
    view = {}
    if apps := load("apps", root):
        view["apps"] = [{"name": s["name"], "description": s["description"], "packages": s["packages"], "apps": s["apps"]}
                        for s in apps]
    if tasks := load("tasks", root):
        view["tasks"] = [{"name": s["name"], "description": s["description"]} for s in tasks]
    return view


def recipes(root=None):
    """The task recipes in full, for the orchestrator."""
    return [{"name": s["name"], "description": s["description"], "content": s["content"]} for s in load("tasks", root)]


def for_app(app, root=None):
    """The skill for a step's app (by package, else by name) as one block of text for a phone agent's
    brief, or None. Blank lines are folded: the brief's sections are separated by blank lines."""
    app = app or {}
    package = (app.get("package") or "").strip().lower()
    name = (app.get("name") or "").strip().lower()
    for skill in load("apps", root):
        packages = [p.lower() for p in skill["packages"]]
        names = [a.lower() for a in skill["apps"]] + [skill["name"].lower()]
        if (package and package in packages) or (name and name in names):
            body = re.sub(r"\n\s*\n+", "\n", skill["content"])
            return f"({skill['name']})\n{body}"
    return None


def for_tools(names, root=None):
    """The tool skills for an agent with these tools, in full, as one section of its instructions; "" when
    none of its tools has one."""
    names = set(names)
    found = [s for s in load("tools", root) if names & set(s["tools"])]
    return "\n\n".join(f"## How to use {', '.join(s['tools'])} ({s['name']} skill)\n\n{s['content']}" for s in found)
