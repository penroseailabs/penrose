# Developing penrose

The product page is the [README](../README.md); this is how the pieces fit and how to work on them.

penrose is internally still `relay` in code, packages and paths.

A desktop app with an Android phone inside it, run by AI agents. You ask in plain words ("a table for two on
Saturday", "reorder last week's groceries"); penrose plans the task and its agents do it in the apps on the phone,
checking with you before anything you did not ask for.

- **One window** (Electron + React): the conversation, the task's progress, and the live phone beside it, which
  you can also use yourself.
- **The phone** is a real Android emulator on your computer, rooted and fitted with an Xposed module
  (relay-bridge) so agents read each app from inside instead of guessing from pixels. It is built once on first
  run and is where your computer is (location follows the computer).
- **Your own API keys** (Claude, GPT or DeepSeek), stored encrypted on the computer.

Install, first run, the window, the phone's start-up, and how the pieces fit: [docs/DESKTOP.md](DESKTOP.md).

## How a task runs

```
message ─▶ gate ──(simple)──▶ answer
             └──(needs work)─▶ orchestrator ─▶ plan of steps
                                   │  ▲
                     run a step    ▼  │ report (and facts for later steps)
                               subagent on the phone ─▶ verifier checks the claim on the phone
```

- **Gate** (fast model): answers greetings and simple questions itself; delegates everything else.
- **Orchestrator** (heavy model): the only agent that talks to you. It plans ordered steps, starts each one,
  reads its report, has it verified, asks you when it must, and finishes. It never sees a subagent's turns,
  only reports, facts and a one-line journal, which keeps its context on the goal.
- **Subagent** (phone model): starts fresh on every step and works the phone turn by turn (look, think, act
  with tool calls) until it reports done, blocked, or asks.
- **Verifier**: checks on the phone, independently, that what the subagent claims is really there.
- **Memory agent**: after each run, keeps what is worth remembering (your preferences, notes about apps) as
  named notes the other agents recall.

Private codes (OTPs) go straight from you to the phone through a private reply and never reach a model, the
chat or the logs. Google sign-in is yours to do once, on the phone; agents then use "Continue with Google".

Code: `backend/app/runtime` (the loops), `backend/app/agents` (each agent's prompt, model and tools;
prompts in `backend/agent_prompts`; written app and task know-how in `backend/skills`), `backend/app/tools`, `backend/app/device` and `backend/app/bridge` (the
phone), `backend/app/api` (the window's API), `backend/app/gateway` (the only service with adb),
`backend/app/provision` (building the phone), `frontend/src`, `desktop/src`.

## Claude Code and Cowork

Claude Code (the CLI, and the Code tab of the Claude desktop app) and Cowork can use penrose's phone instead of
penrose's own agents, through the `relay` command: `relay phone screen` prints the screen as numbered text,
`relay phone tap 12` acts on an element and prints the new screen, and `open`, `link`, `wait`, `notifications`
and the rest are the phone subagent's tools, one verb each (`relay --help`). The app writes the launcher to
`<data>/bin` and the API's address and token to `<data>/data/cli.json`; `relay setup claude-code` installs the
`android-phone` skill that tells Claude when and how to use the phone, and `relay doctor` checks the pieces.
Cowork's sandbox cannot reach penrose, so it gets the same tools as a local MCP server (`relay mcp`) that the
Claude app runs on the computer; `relay setup claude-app` (or Settings › Connect Claude) adds it to the app.
The phone's lease keeps the two brains apart: whoever does not hold it is told who does. The design is in
[docs/CLAUDE_INTEGRATION_PLAN.md](CLAUDE_INTEGRATION_PLAN.md).

## Development

Needs Node 22, Python 3.14 in `.venv` at the repository root, and an Android SDK (the app can download one).

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r backend/requirements-dev.txt
cd frontend && npm ci && cd ../desktop && npm ci
npm run dev
```

`npm run dev` (in `desktop/`) runs Vite and Electron with live reload for the UI, the Python services and the
main process; the phone keeps running across restarts. Details in [docs/DESKTOP.md](DESKTOP.md#development-loop).

Tests and lint:

```bash
cd backend && ../.venv/Scripts/python -m pytest -q
.venv/Scripts/ruff check backend
cd frontend && npx tsc --noEmit -p .
```

penrose on the user's own phone, linked by QR code with end-to-end encryption, is described in
[docs/COMPANION.md](COMPANION.md).

The phone's interaction layer is `backend/app/bridge`. The phone-side pieces it talks to (the relay-bridge
module, the home app, the autoscope Magisk module) are in [android/](../android/README.md): `python android/build.py`
builds them, signed with penrose's release key, into `desktop/assets/android`, where they ship. `install_app` is described in [docs/APP_INSTALL.md](APP_INSTALL.md).
