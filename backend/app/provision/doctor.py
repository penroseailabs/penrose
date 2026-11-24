"""The setup doctor: the user's own model looks at a stuck or failed phone setup and answers in plain words.

`relay-server provision doctor --home ... --logs ...` reads one request on stdin, from the desktop app:
  {"trigger": "failed" | "question", "messages": [{"role": "user" | "doctor", "text": ...}], "state": {...}}
and prints one line: {"event": "doctor", "reply", "state", "action"}.

What it sends the model: the setup's state as the app sees it, the end of the setup log and facts read just now
from the computer and the phone, all read-only. What the model may do is pick one action from a fixed menu
(DoctorAnswer.action); the app carries it out with its own code. Nothing is kept: the app holds the
conversation while the window is open and sends its last few messages each time.
"""

import asyncio
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from . import GOLDEN_SERIAL, Layout, adbui
from .adbui import VECTOR_CLI

MAX_MESSAGES = 8  # the conversation sent each time: its last few turns
MAX_MESSAGE_CHARS = 600
LOG_LINES = 60
LOG_CHARS = 6000
PROVIDERS = (("anthropic", "ANTHROPIC_API_KEY"), ("openai", "OPENAI_API_KEY"), ("deepseek", "DEEPSEEK_API_KEY"))

FALLBACK = {
    "failed": "Setup stopped. Press Try again; it picks up where it stopped.",
    "question": "Add an AI model's API key in the step before this one, and I can look into the setup for you.",
}


class DoctorAnswer(BaseModel):
    summary: str = Field(description="One or two technical sentences for penrose's log: the diagnosis and why.")
    reply: str = Field(description="To the user: 1 to 3 short sentences in plain words, no jargon or commands.")
    state: Literal["working", "slow", "stuck", "failed", "needs_you", "done"]
    action: Literal["none", "try_again", "start_over", "user_step"]


@dataclass(frozen=True)
class Doctor:
    """What agents/llm.py needs of an agent."""

    name: str = "setup doctor"
    tier: str = "fast"

    @property
    def instructions(self):
        from ..agents.prompts import load

        return load("setup_doctor")

    @cached_property
    def schema(self):
        return DoctorAnswer


# --- what it sees --------------------------------------------------------------------------------------------

SECRET = re.compile(r"\b(sk-[A-Za-z0-9_\-]{8,}|[A-Za-z0-9_\-]{32,})\b")


def scrub(text: str) -> str:
    """No home folder (it holds the user's name) and nothing shaped like a key."""
    home = str(Path.home())
    return SECRET.sub("[hidden]", text.replace(home, "~"))


def log_tail(logs: Path | None) -> list[str]:
    """The end of the setup log as the window's events: steps, parts, errors and log lines, without the
    percentages that make up most of it."""
    if not logs or not (logs / "provision.log").exists():
        return []
    lines = []
    with open(logs / "provision.log", encoding="utf-8", errors="replace") as f:
        for raw in f.readlines()[-400:]:
            try:
                event = json.loads(raw)
            except ValueError:
                lines.append(raw.strip())
                continue
            kind = event.get("event")
            at = f"{event['at']} " if event.get("at") else ""  # on steps, parts and errors: how long each took
            if kind == "progress":
                if event.get("doing"):
                    lines.append(f"{at}part {event['id']}/{event['doing']} ({event.get('percent')}%)")
            elif kind == "log":
                lines.append(event.get("text", ""))
            elif kind == "step":
                lines.append(f"{at}STEP {event.get('id')}: {event.get('title')}")
            elif kind == "error":
                lines.append(f"{at}ERROR {event.get('message')} | {event.get('hint', '')}")
            elif kind in ("done", "status"):
                lines.append(kind.upper())
    lines = [scrub(line)[:300] for line in lines if line.strip()][-LOG_LINES:]
    while sum(len(line) for line in lines) > LOG_CHARS:
        lines.pop(0)
    return lines


def run_quiet(args, timeout=10) -> str:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
        return (result.stdout or result.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"(no answer: {type(exc).__name__})"


def host_facts(layout: Layout) -> dict:
    target = layout.home if layout.home.exists() else Path.home()
    facts = {
        "os": f"{platform.system()} {platform.release()} {platform.machine()}",
        "cpus": os.cpu_count(),
        "free_gb": round(shutil.disk_usage(target).free / 1e9, 1),
        "sdk_present": layout.emulator.exists(),
        "golden_built": (layout.golden_home / "relay-golden.avd" / "provisioned").exists(),
    }
    if sys.platform == "darwin":
        facts["memory_gb"] = round(int(run_quiet(["sysctl", "-n", "hw.memsize"]) or 0) / 1e9, 1)
        # "used = 8886.75M": swapping hard is why a phone turns slow and its apps stop responding.
        used = re.search(r"used = ([\d.]+)M", run_quiet(["sysctl", "-n", "vm.swapusage"]))
        facts["swap_used_gb"] = round(float(used.group(1)) / 1024, 1) if used else None
    ps = run_quiet(["ps", "-axo", "pid,etime,%cpu,command"]) if os.name != "nt" else ""
    facts["emulators"] = [scrub(" ".join(line.split()[:3]) + " " + ("golden" if "relay-golden" in line else "phone"))
                          for line in ps.splitlines() if "qemu-system" in line][:3]
    emulator_log = layout.logs / "golden-emulator.log"
    if emulator_log.exists():
        tail = emulator_log.read_text(encoding="utf-8", errors="replace").splitlines()[-8:]
        facts["golden_emulator_log"] = [scrub(line)[:200] for line in tail]
    return facts


def phone_facts(serial: str) -> dict:
    """Read-only checks on a phone that answers adb; {"answers_adb": False} when it does not."""
    if serial not in adbui.adb(serial, "devices", check=False, timeout=10):
        return {"answers_adb": False}

    def sh(command):
        try:
            return adbui.shell(serial, command, check=False, timeout=15).strip()
        except subprocess.TimeoutExpired:
            return "(no answer)"

    def su(command):
        try:
            return adbui.su(serial, command, check=False, timeout=20).strip()
        except (subprocess.TimeoutExpired, RuntimeError):
            return "(no answer)"

    uptime = sh("cat /proc/uptime").split()
    sockets = re.findall(r"@relaybridge\.([\w.:]+)", sh("cat /proc/net/unix"))
    scope = su(f"{VECTOR_CLI} --json scope ls app.relay.bridge")
    return {
        "answers_adb": True,
        "uptime_s": int(float(uptime[0])) if uptime and uptime[0].replace(".", "").isdigit() else None,
        "boot_completed": sh("getprop sys.boot_completed"),
        "locked": "isKeyguardShowing=true" in sh("dumpsys window | grep isKeyguardShowing"),
        "root": "uid=0" in su("id"),
        "magisk_modules": su("ls /data/adb/modules").split()[:8],
        "vector_running": "vectord" in sh("ps -A | grep vectord"),
        "bridge_module": su(f"{VECTOR_CLI} modules ls").splitlines()[-1:],
        "chrome_in_bridge_scope": "com.android.chrome" in scope,
        "chrome_running": bool(sh("pidof com.android.chrome")),
        "bridge_in": sorted(set(sockets))[:12],
    }


def gather(layout: Layout) -> dict:
    adbui.ADB = str(layout.adb)
    facts = {"computer": host_facts(layout)}
    if layout.adb.exists():
        facts["golden_phone"] = phone_facts(GOLDEN_SERIAL)
    return facts


# --- asking -----------------------------------------------------------------------------------------------


def provider():
    return next((name for name, env in PROVIDERS if os.environ.get(env)), None)


def trim(messages) -> list[dict]:
    kept = [m for m in messages or [] if isinstance(m, dict) and m.get("text")][-MAX_MESSAGES:]
    return [{"role": "user" if m.get("role") == "user" else "doctor", "text": str(m["text"])[:MAX_MESSAGE_CHARS]}
            for m in kept]


async def ask(layout: Layout, logs: Path | None, request: dict) -> dict:
    trigger = "failed" if request.get("trigger") == "failed" else "question"
    name = provider()
    if not name:
        return {"reply": FALLBACK[trigger], "state": "failed" if trigger == "failed" else "working",
                "action": "none", "summary": "no model key"}
    context = {
        "trigger": trigger,
        "setup": request.get("state") or {},
        "log_tail": log_tail(logs),
        "facts": await asyncio.to_thread(gather, layout),
        "conversation": trim(request.get("messages")),
    }
    from ..agents.llm import LLM

    llm = LLM()
    try:
        answer = await llm.ask(Doctor(), context, provider=name)
    finally:
        await llm.close()
    return answer.model_dump()


def main(layout: Layout, logs: Path | None) -> int:
    try:
        request = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        request = {}
    try:
        answer = asyncio.run(ask(layout, logs, request))
    except Exception as exc:  # the doctor is a helper: when it cannot answer, setup carries on as before
        print(json.dumps({"event": "log", "text": f"setup doctor: {type(exc).__name__}: {scrub(str(exc))[:300]}"}))
        trigger = "failed" if request.get("trigger") == "failed" else "question"
        answer = {"reply": "I couldn't look into it just now." if trigger == "question" else FALLBACK["failed"],
                  "state": "failed" if trigger == "failed" else "working", "action": "none", "summary": "doctor error"}
    print(json.dumps({"event": "log", "text": f"setup doctor: {answer.get('summary', '')}"}), flush=True)
    print(json.dumps({"event": "doctor", **{k: answer[k] for k in ("reply", "state", "action")}}), flush=True)
    return 0
