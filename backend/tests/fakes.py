"""Fakes for the runtime: a scripted model, the device gateway, and run states with a plan."""

import json
import time

from app.agents import registry
from app.plan import StepSpec, new_step
from app.runtime.runner import Runner
from phone import FakePhone, screen_of


def step_spec(id, goal=None, **extra):
    return StepSpec(id=id, goal=goal or f"Do {id}", done_when=f"{id} is done", **extra)


def make_state(store, steps=(), **extra):
    """A running run in chat "chat", with `steps` (StepSpec or dicts of step state) as its plan."""
    plan = [new_step(s) if isinstance(s, StepSpec) else s for s in steps]
    state = {
        "id": "run", "chat_id": "chat", "request": "test", "status": "running", "route": {"orchestrate": True},
        "context": "test context", "plan": plan, "facts": {}, "journal": [], "nodes": {}, "questions": [], **extra,
    }
    if not store.rows("SELECT id FROM chats WHERE id='chat'"):
        store.execute("INSERT INTO chats VALUES (?,?,?)", ("chat", "test", time.time()))
    store.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?)", ("run", "chat", "running", json.dumps(state), time.time()))
    return state


# --- turns, written as the model would write them ----------------------------------------------------

def orch(*actions, reply="", summary="Deciding."):
    return {"summary": summary, "reply": reply, "actions": list(actions)}


def run_step(step, note=""):
    return {"tool": "run_step", "step": step, "note": note}


def set_plan(*specs, context=""):
    return {"tool": "set_plan", "context": context, "steps": [s.model_dump() if isinstance(s, StepSpec) else s for s in specs]}


def finish(answer="All done."):
    return {"tool": "finish", "answer": answer}


def ask_user(question="Which one?"):
    return {"tool": "ask_user", "question": question}


def verify(step, claim="It is done", how=()):
    return {"tool": "verify_step", "step": step, "claim": claim, "how": list(how)}


def as_call(action):
    """A tool call as the model sends it: {"name", "arguments"}. Written in tests as {"tool": name, **arguments}."""
    if "name" in action and "arguments" in action:
        return action
    args = dict(action)
    return {"name": args.pop("tool"), "arguments": args}


def phone(*actions, content="", thought=""):
    """A phone agent's turn: its tool calls, in order, and the reasoning the model gave with them."""
    return {"content": content, "calls": [as_call(a) for a in actions], "thought": thought}


def sections(text):
    """A phone agent's state text as {label: content}: "step", "facts", "Last result", "Screen"..."""
    out = {}
    for part in text.split("\n\n"):
        label, _, rest = part.partition(":")
        out.setdefault(label, rest.strip())
    return out


def value(content):
    """A brief item's text back as data: JSON, `key: value` lines as a dict, or the text itself."""
    try:
        return json.loads(content)
    except ValueError:
        pass
    lines = content.splitlines()
    if lines and all(": " in line for line in lines):
        return dict(line.split(": ", 1) for line in lines)
    return content


def click(n=1):
    return {"tool": "interact", "element": n, "interaction": "tap"}


def note(text):
    return {"tool": "note", "text": text}


def log(text):
    return {"tool": "log", "text": text}


def report(status="completed", summary="Done.", **extra):
    return {"tool": "report", "status": status, "summary": summary, **extra}


def handover(found="nothing yet", cause="on_track", **extra):
    return {"tool": "handover", "found": found, "where": "the app's list screen", "cause": cause,
            "detail": "the same tap did nothing", "next": "open the item's own page", **extra}


def checkpoint(found=("Found one thing",), tried=(), where="the app's list screen", plan="[now] Read the list"):
    return {"tool": "checkpoint", "found": list(found), "tried": list(tried), "where": where, "plan": plan}


def verdict(passed=True, reason="The screen shows it."):
    return {"tool": "verdict", "passed": passed, "reason": reason}


class Script:
    """The model. `handlers` maps an agent name (as in the trace: "Orchestrator", "Subagent", "Verifier",
    "Fast response", "Memory"...) to a function of (context, n) -> a turn dict, or a
    list of turn dicts answered in order. Every turn is validated against the agent's schema, like the real
    model's. Unscripted: the gate delegates and memory keeps nothing."""

    def __init__(self, **handlers):
        self.handlers = {name.replace("_", " "): h for name, h in handlers.items()}
        self.calls = []
        # (provider, model, usage, created) every request reports, when a test checks the cost accounting.
        self.usage = None

    async def ask(self, agent, context, images=None, on_update=None, on_thinking=None, provider="deepseek",
                  on_usage=None, model=None, effort=None):
        n = sum(1 for c in self.calls if c["agent"] == agent.name)
        if on_usage and self.usage:
            on_usage(*self.usage)
        self.calls.append({"agent": agent.name, "context": json.loads(json.dumps(context, default=str)), "images": images or [],
                           "provider": provider, "model": model, "effort": effort})
        handler = self.handlers.get(agent.name)
        if handler is None:
            if agent is registry.GATE:
                return agent.schema.model_validate({"actions": [{"tool": "delegate"}]})
            if agent is registry.MEMORY:
                return agent.schema.model_validate({"actions": [{"tool": "remember"}]})
            if agent is registry.TITLER:
                return agent.schema.model_validate({"actions": [{"tool": "set_title", "title": "A test chat"}]})
            raise AssertionError(f"unscripted {agent.name}")
        turn = handler[n] if isinstance(handler, list) else handler(context, n)
        return agent.schema.model_validate(turn)

    async def act(self, agent, text, images=None, provider="deepseek", on_usage=None, model=None, effort=None):
        """A phone agent's turn. Its handler returns phone(...) (or a list of them, answered in order); the
        context it gets is the turn's state text. The runtime's checkpoint turn (its state says "Turn:
        checkpoint.") is scripted as "Checkpoint"; unscripted, it answers with no call, which leaves the session as
        it was."""
        name = "Checkpoint" if "\nTurn: checkpoint." in text else agent.name
        n = sum(1 for c in self.calls if c["agent"] == name)
        if on_usage and self.usage:
            on_usage(*self.usage)
        self.calls.append({"agent": name, "context": text, "images": images or [],
                           "provider": provider, "model": model, "effort": effort})
        handler = self.handlers.get(name)
        if handler is None and name == "Checkpoint":
            return "", [], ""
        if handler is None:
            raise AssertionError(f"unscripted {name}")
        turn = handler[n] if isinstance(handler, list) else handler(text, n)
        if isinstance(turn, list):
            turn = phone(*turn)
        return turn.get("content", ""), [as_call(c) for c in turn["calls"]], turn.get("thought", "")

    def of(self, name):
        return [c for c in self.calls if c["agent"] == name]

    async def close(self):
        pass


class FakeDevices:
    """The gateway client with one phone (`phone.FakePhone`). Records actions, ops and releases."""

    def __init__(self, phone=None, accounts=(), jpeg=None):
        self.phone = phone or FakePhone(screen_of("Home"))
        self.released, self.actions, self.op_log, self.acquired = [], [], [], []
        self.limits = []  # the apps each lease was limited to (None: any)
        self.account_list = list(accounts)
        self.jpeg = jpeg
        self.packages_list = []
        self.apps_list = []  # the apps with an icon: [{"name", "package", "shortcuts"}]
        self.fix = None  # {"lat", "lng", "accuracy"} once the phone was given a location
        self.refreshed = 0
        self.zones = []  # (slot, zone) each time the phone's clock was put on a time zone
        self.rescoped = []  # each app the bridge's scope was brought up to date for (it ran without the bridge)
        self.scoped = True  # what that says: the app is in the scope (None: the gateway could not say)
        self.found = {}  # the home app's search: query -> its result lines
        self.search_error = None  # a DeviceError the home app's search raises instead
        self.searches = []
        self.search_email = []  # the `email` flag of each home search
        self.mail = {}  # query -> how it stands with Gmail when asked with `email` (answered unless said)
        self.wakes = []  # (slot, for_seconds) the worker woke the phone for
        self.web_found = {}  # Google in the phone's Chrome: query -> its result lines
        self.web_error = None  # a DeviceError the Google search raises instead
        self.web_searches = []
        self.play_found = {}  # Google Play in the phone's Chrome: query -> its app lines
        self.play_error = None  # a DeviceError the Play search raises instead
        self.play_searches = []
        self.history_found = {}  # the user's browsing on their other devices: query -> its lines
        self.history_error = None  # a DeviceError reading Chrome's history raises instead
        self.history_searches = []
        self.saved = {}  # url or package -> the gateway's saved sign-ins answer
        self.signin_lookups = []

    async def acquire(self, owner, wait=0, apps=None):
        self.acquired.append(owner)
        self.limits.append(apps)
        return "device-1"

    async def holders(self):
        return {}

    async def phones(self):
        return [{"id": "device-1", "connected": True, "owner": next(iter((await self.holders()).values()), None)}]

    def device(self, slot, owner):
        return self.phone

    async def bridge(self, slot, owner, method, **body):
        if method == "call":
            return await self.phone.call(body["cmd"], process=body.get("process"), **body.get("args", {}))
        return await getattr(self.phone, method)()

    async def action(self, slot, owner, action):
        self.actions.append(action)
        if action["kind"] in ("back", "home", "recents", "enter"):
            await self.phone.key(action["kind"])
        elif action["kind"] == "tap":
            await self.phone.tap(action["x"], action["y"])
        elif action["kind"] == "swipe":
            await self.phone.swipe(action["x"], action["y"], action["x2"], action["y2"], action["duration_ms"])
        elif action["kind"] == "type_text" and not action.get("sensitive"):
            await self.phone.type_text(action["text"])
        if action["kind"] == "adb_command":
            return {"ok": True, "exit_code": 0, "output": f"ran {action['command']}", "stderr": ""}
        if action["kind"] == "install_app":
            return {"ok": True, "status": "installed", "message": f"{action['package']} installed"}
        return {"ok": True}

    async def packages(self, slot, owner):
        return self.packages_list

    async def apps(self, slot, owner):
        return self.apps_list

    async def screenshot(self, slot, width=None):
        return self.jpeg

    async def ops(self, slot, owner, ops):
        self.op_log += list(ops)
        return [{"op": op["op"], "ok": True, "summary": f"{op['op']} done", **({"state": {"wifi": "on"}} if op["op"] == "state" else {})} for op in ops]

    async def wake(self, slot, for_seconds):
        self.wakes.append((slot, for_seconds))

    async def home_search(self, slot, queries, email=True):
        self.searches.append((slot, list(queries)))
        self.search_email.append(email)
        if self.search_error:
            raise self.search_error
        return [{"query": q, "found": self.found.get(q, ""), **({"mail": self.mail.get(q, "answered")} if email else {})}
                for q in queries]

    async def web_search(self, slot, queries):
        self.web_searches.append((slot, list(queries)))
        if self.web_error:
            raise self.web_error
        return [{"query": q, "found": self.web_found.get(q, "")} for q in queries]

    async def play_search(self, slot, queries):
        self.play_searches.append((slot, list(queries)))
        if self.play_error:
            raise self.play_error
        return [{"query": q, "found": self.play_found.get(q, "")} for q in queries]

    async def history_search(self, slot, queries):
        self.history_searches.append((slot, list(queries)))
        if self.history_error:
            raise self.history_error
        return [{"query": q, "found": self.history_found.get(q, "")} for q in queries]

    async def accounts(self, slot, owner):
        return list(self.account_list)

    async def saved_signins(self, slot, url="", package=""):
        self.signin_lookups.append(url or package)
        return self.saved.get(url or package)

    async def location(self, slot, refresh=False):
        self.refreshed += refresh
        return self.fix

    async def locations(self):
        return {"device-1": self.fix or {}}

    async def set_timezone(self, slot, zone):
        self.zones.append((slot, zone))
        return True

    async def bridge_scope(self, slot, package):
        self.rescoped.append(package)
        return self.scoped

    async def release(self, owner):
        self.released.append(owner)

    async def close(self):
        pass


def runner_for(store, model, phone=None, devices=None, **kw):
    return Runner(store, model=model, devices=devices or FakeDevices(phone, **kw))


def kinds(store):
    return [r["kind"] for r in store.rows("SELECT kind FROM events ORDER BY id")]
