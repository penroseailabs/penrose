# Plan: Claude Code and Cowork using penrose's phone

Status: phases 1 and 2 built on 2026-10-24: the phone API, the `relay` command, the `android-phone` skill
(`backend/skills/claude/android-phone/SKILL.md`), the launcher, `doctor` and `setup`, the Connect Claude page
in Settings (detection, Add to PATH, Install skill), and the phone panel's "Claude Code is using the phone"
row with Take back. Tested from a terminal and from the local `claude` CLI against the running app.

Phase 3 built the same day. Cowork's shell runs in a Hyper-V Linux VM with filtered networking and cannot reach
the computer's 127.0.0.1, where penrose listens on purpose. The Claude app, though, starts the user's local MCP
servers on the computer itself and bridges their tools into Cowork sessions. So Cowork uses `relay mcp`
(`backend/app/mcp.py`), a stdio MCP server over the same phone API, with the Cowork skill
(`backend/skills/claude/cowork/android-phone/SKILL.md`) as its instructions. Connect Claude adds it to the
Claude app's `claude_desktop_config.json` (or `relay setup claude-app`) and exports the skill as a ZIP for
claude.ai. Tested through the local `claude` CLI with the same MCP entry and no skill: it opened Clock, read
its tabs and released the phone in 6 turns. A real Cowork session is still to be tried. Open:
`run`/`reply`/`cancel` (phase 4).

## The idea

penrose today is a closed loop: its own gate, orchestrator and phone subagent drive the emulator through the
gateway. This plan opens the phone to a second brain on the same computer: Claude Code (the CLI, and the Code
tab of the Claude desktop app) and Cowork. Both have a shell, so the integration is a command-line tool that
gives them the same hands the phone subagent has (look at the screen, act on a numbered element, open an app
or link, wait), plus a skill file that tells the model how to work a phone well. penrose's
window keeps working as before; the two brains just cannot hold the phone at the same moment.

Out of scope, by decision: Claude Desktop's plain chat (no shell; would need an MCP server) and remote
connectors (need a public HTTPS server). No TLS is involved anywhere here: the CLI talks to penrose's API over
loopback, as the window does.

## Verdict: viable, small

What makes it easy:

- The phone surface already exists as model-facing tools: `interact`, `type_and_submit`, `press_key`,
  `swipe`, `open_app`, `open_link`, `list_apps`, `wait_for`, `set_setting`,
  `request_screenshot` (`backend/app/bridge/tools.py`), validated against the live screen, and the screen is
  already rendered as numbered text (`backend/app/bridge/screen.py`). A CLI command is a thin wrapper over
  `Driver` and `Session`; no new device code.
- The gateway already has exclusive leases, so penrose's worker and an outside agent cannot collide: the loser
  gets 409 "phone is busy".
- The guardrails (finance packages, forbidden adb commands, screenshot masking of private fields) live in the
  tool layer the CLI reuses, not in the prompts.
- `relay-server` is a single PyInstaller bundle shipped inside the app, so the CLI needs no Node or Python on
  the user's machine, on Windows, macOS or Linux alike.
- Claude Code skills are plain Markdown with front matter; `allowed-tools: Bash(relay *)` lets the model run
  the CLI without a permission prompt per command.

What constrains it:

- penrose's API port and login are random per launch, so the CLI must find the running app through a
  discovery file, and the API needs a second way in besides the window's cookie.
- Cowork runs its shell inside a VM that cannot reach the host's 127.0.0.1 (checked in phase 3), and penrose
  will not listen anywhere else. Cowork reaches the phone through a local MCP server instead, which the Claude
  app runs on the computer (below).
- penrose has no Linux build yet (`desktop/package.json` builds win and mac). The design is portable; Linux
  arrives when penrose does.

## Architecture

```
Claude Code / Cowork ──bash──▶ relay phone <cmd> ──HTTP + bearer (loopback)──▶ relay-server api  /api/phone/*
                                   ▲ reads                                           │ Driver / Session / skills
                           <data dir>/cli.json                                       ▼
                        (url, token; written by the app)                   gateway ──adb──▶ phone

Cowork ──MCP tool call──▶ Claude app ──stdio──▶ relay mcp (on the computer) ──same HTTP + bearer──▶ /api/phone/*
```

1. **Phone endpoints in the API** (`backend/app/api/phone.py`): one route per command, all over the existing
   `Devices` client, `Driver` and the screen renderer. The API process already holds the gateway token, the
   store and the skills loader, so the logic lives there once; the CLI stays dumb.
2. **The CLI**: `relay-server phone <command>` inside the bundled binary, plus a short launcher named `relay`
   on the PATH (`relay.cmd` on Windows, a shell script on macOS and Linux) that the app installs on request.
   It reads the discovery file, calls the endpoint, prints the result as text for a model to read, and exits
   non-zero with one plain line when penrose is not running ("Open penrose first").
3. **Discovery file**, written by `desktop/src/main.js` at launch and removed on quit:
   `<data dir>/cli.json` = `{ "url": "http://127.0.0.1:<api port>", "token": "<integration token>", "pid": … }`.
   Data dir is `%LOCALAPPDATA%\Relay`, `~/Library/Application Support/Relay`, or `RELAY_DATA_DIR`.
4. **Integration token**: durable, generated once, kept in the data dir with user-only permissions, accepted
   as `Authorization: Bearer` by `backend/app/api/auth.py` for `/api/phone/*` only. Cookie and Origin rules
   stay for the window; the per-launch gateway token never leaves the API and worker.
5. **Lease**: the first command leases the phone as owner `cli:<session>`; see "Sharing the phone" below.
6. **Session state** (event cursors, last screen, numbered handles) is kept server-side per owner, so each
   CLI call is stateless and short-lived, as the subagent's `Driver` already keeps it per step.

## Sharing the phone

Every touch on the phone goes through the gateway, and the gateway gives the phone to one owner at a time:
it refuses any action whose owner does not hold the lease (`gateway/actions.py` `check_owner`), including
the window's own taps and the ops lane. A penrose run holds the lease under its run id for the whole run
(`runtime/runner.py` `lease`); a second run waits up to `device_wait_seconds` showing "Waiting for the
phone", then fails. Claude Code becomes one more owner, `cli:<session>`, under the same rule.

| situation | what happens |
| --- | --- |
| Claude Code holds the phone, the user starts a penrose errand | the run waits as it does today, labelled "Waiting for the phone (Claude Code)"; it resumes when the CLI releases or idles out, or when the user clicks "Take the phone back" in the window |
| the phone is taken back | the CLI's next command exits 3 with "penrose took the phone back"; the skill says to tell the user and stop |
| penrose holds the phone, Claude Code wants it | exit 3, "The phone is busy with penrose: <task title>. Ask the user to finish or cancel it." The CLI never evicts a run; a run that is only waiting for the user's answer yields the phone, as it does for penrose itself (`take_phone`) |
| two Claude Code sessions | separate owners, same busy rule; the session id comes from the CLI's parent process, so each terminal or Code tab is one owner |
| the user taps the phone panel while Claude Code holds it | allowed (today `api/devices.py` `manual_action` refuses this for an unknown owner; `cli:` owners are added to the exception, since Claude Code acts for the user and may need them to type an OTP). Element numbers are validated against the live screen on every action, so a tap after the user's change is refused as stale, not misdirected |
| Claude's session ends without releasing | the lease is renewed by each command and dropped after about five minutes without one; `relay phone release` drops it at once |

The lease does not cover the state left behind: an app left open, a half-filled cart, a changed setting or
location. penrose already copes with that: the gateway tells a new owner whether anyone used the phone since
it last held it (`kept`), and runs start from the home screen and read the screen before acting. Two rules
in the skill close the rest: change settings or location only when asked, and `release` (and the idle
release) restores anything the session changed.

Work this adds to phase 1: `cli:` owners in `take_phone` (not evictable, not a run) and in `manual_action`;
the busy message naming the holder; "Take the phone back" and the "phone in use by Claude Code" indicator
in the window (phase 2).

## Commands

| command | does |
| --- | --- |
| `relay phone status` | connected, booted, who holds the lease, Google account signed in, location, penrose version |
| `relay phone screen [--shot FILE]` | numbered screen text and new events (toasts, dialogs, notifications); `--shot` also writes a masked JPEG |
| `relay phone tap N`, `long-press N`, `type N TEXT`, `submit N TEXT`, `scroll N up\|down\|left\|right\|top\|bottom`, `choose N TEXT`, `set N min\|max\|up\|down` | the `interact` tool, one verb each; each prints the new screen |
| `relay phone key back\|home\|recents\|enter\|hide-keyboard`, `swipe up\|down\|left\|right` | as in the subagent |
| `relay phone open APP`, `link URL`, `apps [QUERY]`, `wait TEXT [--seconds N]` | as in the subagent |
| `relay phone setting NAME on\|off`, `location LAT LNG` | same allowlist as today |
| `relay phone knowhow APP` | the app skill (`backend/skills/apps/*.md`) and matching task recipes, for a package or app name |
| `relay phone release` | drop the lease |
| `relay run "TEXT"`, `relay reply "TEXT"`, `relay cancel` | hand a whole errand to penrose's orchestrator through the chat API, stream its progress, print the result or its question |
| `relay doctor` | find penrose, the token, the phone; print what is wrong |

Output is plain text: the same numbered screen the subagent reads, events as lines, errors as one line
starting with `error:`. `--json` on every command for programs. Exit codes: 0 ok, 1 the action was refused
(stale number, not allowed), 2 penrose not running, 3 phone busy.

Not exposed: raw adb, `install_app` in phase 1, anything that moves money (same `finance_package` refusal).

## Context for the model

A skill `relay-phone` (`skills/relay-phone/SKILL.md`), which is the condensed phone subagent brief plus the
command table: run `relay phone status` first; look before acting; numbers come from the live screen and go
stale after any action, so act from the latest output; prefer deep links (`relay phone knowhow APP` first);
how to dismiss sheets and promos; `wait` instead of repeated `screen`; never enter an OTP, the user types
private codes on the phone panel in penrose's window; no finance apps; the phone is the user's, do not change its
settings unless asked. Front matter: `name`, `description` ("Use penrose's Android phone to do things in mobile
apps: order, book, message, read"), `allowed-tools: Bash(relay *)`.

Where it is installed:

- Claude Code: `~/.claude/skills/relay-phone/SKILL.md` (user scope, every project), written by penrose's setup
  button or by `relay setup claude-code`. A project can also carry it under `.claude/skills`.
- Cowork: the MCP server's instructions carry the same text, so Cowork knows the phone tools without a skill.
  The skill (written for the `phone_*` tools) helps it decide when to reach for the phone: penrose exports it as
  `<data>/claude/android-phone.zip` and shows the file, and the user uploads it in claude.ai under Customize >
  Skills. The app keeps synced skills in its own managed folder, so penrose does not write there.
- Later, a Claude Code plugin (`.claude-plugin/plugin.json` with the skill) for one-command install from a
  marketplace, when distribution matters.

## Detection and setup

A "Connect Claude" page in penrose's settings. Detection is advisory; setup is a button and the command behind it,
shown so the user can run it themselves.

- Claude Code: `claude --version` on PATH, then known paths: `~/.local/bin/claude`,
  `%USERPROFILE%\.local\bin\claude.exe`, npm global (`%APPDATA%\npm\claude.cmd`, `/usr/local/bin/claude`),
  Homebrew, and the desktop app's bundled copy under `%LOCALAPPDATA%\Claude-Data\…\claude-code\`.
- Claude desktop app (for Cowork and the Code tab): macOS `/Applications/Claude.app`; Windows the MSIX package
  (`Get-AppxPackage Claude*`) or `%LOCALAPPDATA%\Claude-Data`; Linux `~/.config/Claude` (beta, unverified).
- Setup does three things: puts `relay` on the PATH (Windows: a `relay.cmd` in `%LOCALAPPDATA%\Relay\bin` added
  to the user PATH; macOS and Linux: a symlink in `~/.local/bin` or `/usr/local/bin` with the user's consent),
  writes the skill, and runs `relay doctor` to show green or what is missing.

## Phases

1. **API and CLI** (backend + a launcher). `/api/phone/*` routes, bearer integration token, discovery file,
   lease owner and idle release, the `phone` commands over `Driver`/`Session`, `knowhow`, `doctor`. Test from
   a terminal and then from the `claude` CLI already on this machine with a hand-written skill. About two days.
2. **Skill and setup**: the `relay-phone` skill, the settings page with detection, PATH, skill install, the
   "phone in use" indicator in the window. One day.
3. **Cowork** (done): the sandbox cannot reach penrose, so `relay mcp`, a stdio MCP server the Claude app starts;
   its registration in `claude_desktop_config.json` (a copy of the file as it was is kept as
   `.before-relay`; a file that does not parse is left alone), the skill ZIP, and both on the Connect Claude
   page. The app needs a restart to load a new MCP server.
4. **`run` / `reply` / `cancel`**, `--json`, docs. Half a day.
5. Optional later: a resident mode (`relay phone serve`, commands over stdin) if process start-up per command
   proves slow; a plugin. (The MCP server from phase 3 also serves the Claude app's ordinary chat.)

## Risks and open questions

- Claude's model sees the screen text, so an OTP shown on screen could reach it. penrose's own design keeps
  codes out of models. Mitigation: mask private fields in the text as the screenshot masking already does;
  the skill tells the user to enter codes on the phone panel.
- Process start-up per command (PyInstaller, roughly half a second to a second on Windows) over hundreds of
  taps. The resident mode in phase 5 fixes it if it matters.
- An organization can turn off local MCP servers in the Claude app (the isLocalDevMcpEnabled policy); Cowork
  then cannot use the phone.
- Linux has no penrose build.

## Sources

- Claude Code skills: https://code.claude.com/docs/en/skills.md; permissions syntax:
  https://code.claude.com/docs/en/permissions.md; plugins: https://code.claude.com/docs/en/plugins/manifest-reference.md
- Skills in claude.ai and Cowork, and the sync to `~/.claude/skills/synced/`:
  https://support.claude.com/en/articles/12512180-using-skills-in-claude
- Install locations: https://code.claude.com/docs/en/setup.md, https://code.claude.com/docs/en/desktop-linux.md
- Local MCP servers in the Claude app: https://modelcontextprotocol.io/docs/develop/connect-local-servers
- Why remote connectors are out: https://support.claude.com/en/articles/11175166-getting-started-with-custom-connectors-using-remote-mcp
