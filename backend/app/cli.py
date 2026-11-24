"""The `relay` command: Relay's phone for another agent on this computer (Claude Code, Cowork), over the phone
API (app/api/phone.py). Plain text out, made to be read by a model: each result on a line, then the events,
the numbered screen and what each element accepts; `--json` for programs.

    relay phone status                       the phone, who holds it, the Google account, the location
    relay phone screen [--shot FILE]         the screen now (--shot also saves a masked JPEG)
    relay phone tap N | long-press N | type N TEXT | submit N TEXT | scroll N up|down|left|right|top|bottom
                choose N TEXT | set N min|max|up|down
    relay phone key back|home|recents|enter|hide-keyboard | swipe up|down|left|right
    relay phone open APP | link URL [--package ID] | apps QUERY
    relay phone wait [TEXT] [--gone TEXT] [--activity NAME] [--seconds N]
    relay phone setting NAME VALUE | location LAT LNG | grant PACKAGE PERMISSION...
    relay phone knowhow APP                  what Relay knows about an app (deep links, login, where things are)
    relay phone release                      give the phone back (it is given back after 5 idle minutes anyway)
    relay doctor                             is everything in place?
    relay setup claude-code [--memory]       install the android-phone skill for Claude Code
    relay setup claude-app [--remove]        add Relay's phone tools to the Claude app (Cowork) as an MCP server
    relay mcp                                that MCP server (stdio; the Claude app starts it)

Exit codes: 0 done; 1 the command was refused or the action failed (the output says why); 2 Relay is not
running or the phone is not up; 3 the phone is busy (Relay's own task, the user, or another session).

Finds the running app through the discovery file the API writes (<data dir>/cli.json). Standard library
only: a fresh process runs for every command."""

import argparse
import base64
import json
import os
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

EXIT_OK, EXIT_REFUSED, EXIT_NO_RELAY, EXIT_BUSY = 0, 1, 2, 3
SKILL_NAME = "android-phone"
MEMORY_MARK = "<!-- relay android-phone -->"
MEMORY_BLOCK = f"""{MEMORY_MARK}
## Android phone
This computer runs penrose, which provides an Android phone that agents may use for anything done in mobile apps.
Use it through the `relay phone` command; start with `relay phone status`. The android-phone skill explains it.
{MEMORY_MARK}
"""


class Stop(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


# --- finding Relay ---------------------------------------------------------------------------------------------

def client_id():
    """Who holds the phone: one owner per Claude Code session (its session id), or RELAY_CLIENT."""
    return os.environ.get("RELAY_CLIENT") or os.environ.get("CLAUDE_CODE_SESSION_ID", "")[:8] or "default"


def chosen_data_dir():
    """The data folder the user chose for the installed app, or None: penrose/data-location.txt in the per-user app
    data folder, on every platform (desktop/build/installer.nsh writes it, desktop/src/main.js chosenDataDir reads it)."""
    if not getattr(sys, "frozen", False):
        return None
    home = Path.home()
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = home / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    try:
        raw = (base / "penrose" / "data-location.txt").read_bytes()
    except OSError:
        return None
    text = raw.decode("utf-16-le" if b"\0" in raw else "utf-8", "ignore").lstrip("\ufeff").strip()
    return Path(text) if text and Path(text).is_absolute() else None


def data_dirs():
    """Where Relay keeps its data, most likely first: RELAY_DATA_DIR, the folder chosen at install, the platform's app
    data folder and, in development, the repository (the same places desktop/src/main.js and app/config.py use)."""
    dirs = []
    if env := os.environ.get("RELAY_DATA_DIR"):
        dirs.append(Path(env) / "data")
    if chosen := chosen_data_dir():
        dirs.append(chosen / "data")
    home = Path.home()
    if sys.platform == "win32":
        if local := os.environ.get("LOCALAPPDATA"):
            dirs.append(Path(local) / "Relay" / "data")
    elif sys.platform == "darwin":
        dirs.append(home / "Library" / "Application Support" / "Relay" / "data")
    else:
        dirs.append(home / ".config" / "Relay" / "data")
    if not getattr(sys, "frozen", False):
        dirs.append(Path(__file__).resolve().parents[2] / "data")
    return dirs


def cli_files():
    if file := os.environ.get("RELAY_CLI_FILE"):
        return [Path(file)]
    return [d / "cli.json" for d in data_dirs()]


def discover():
    """{url, token} of the running Relay, or Stop(2)."""
    for path in cli_files():
        try:
            info = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if info.get("url") and info.get("token") and alive(info["url"]):
            return info
    raise Stop(EXIT_NO_RELAY, "penrose is not running. Ask the user to open penrose (its window starts the phone), then try again.")


def alive(url):
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/api/health", timeout=3) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


class Relay:
    def __init__(self, info):
        self.url, self.token = info["url"].rstrip("/"), info["token"]

    def call(self, method, path, body=None, timeout=150):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.url + path, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}", "Content-Type": "application/json", "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(request, timeout=timeout) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as exc:
            detail = error_detail(exc)
            if exc.code == 401:
                raise Stop(EXIT_NO_RELAY, "penrose did not accept this command's token. Restart penrose and try again.") from None
            if exc.code == 409:
                raise Stop(EXIT_BUSY, detail) from None
            if exc.code == 503:
                raise Stop(EXIT_NO_RELAY, detail) from None
            raise Stop(EXIT_REFUSED, detail) from None
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise Stop(EXIT_NO_RELAY, f"penrose did not answer ({reason}). Is it still running?") from None


def error_detail(exc):
    try:
        detail = json.loads(exc.read()).get("detail")
    except (ValueError, AttributeError, OSError):
        detail = None
    if isinstance(detail, list):  # request validation
        detail = "; ".join(f"{'.'.join(map(str, d.get('loc', [])[1:]))}: {d.get('msg')}" for d in detail if isinstance(d, dict))
    return str(detail or f"penrose answered with HTTP {exc.code}")


# --- the phone commands ----------------------------------------------------------------------------------------

def tool_call(tool, **arguments):
    return {"name": tool, "arguments": {k: v for k, v in arguments.items() if v is not None}}


def interact(n, interaction, value=None):
    return tool_call("interact", element=n, interaction=interaction, value=value)


def phone_call(args):
    """The tool call behind a `relay phone` action (the phone subagent's tools, bridge/tools.py)."""
    c = args.command
    if c == "tap":
        return interact(args.n, "tap")
    if c == "long-press":
        return interact(args.n, "long_press")
    if c == "type":
        return interact(args.n, "type", args.text)
    if c == "submit":
        return tool_call("type_and_submit", element=args.n, text=args.text)
    if c == "scroll":
        return interact(args.n, f"scroll_{args.direction}")
    if c == "choose":
        return interact(args.n, "choose", args.text)
    if c == "set":
        return interact(args.n, "set_value", args.value)
    if c == "key":
        return tool_call("press_key", key=args.key.replace("-", "_"))
    if c == "swipe":
        return tool_call("swipe", direction=args.direction)
    if c == "open":
        return tool_call("open_app", app=args.app, shortcut=args.shortcut)
    if c == "link":
        return tool_call("open_link", uri=args.url, package=args.package)
    if c == "apps":
        return tool_call("list_apps", query=args.query)
    if c == "wait":
        return tool_call("wait_for", text=args.text or None, gone=args.gone, activity=args.activity, timeout=args.seconds)
    if c == "setting":
        return tool_call("set_setting", name=args.name, value=args.value)
    if c == "location":
        return tool_call("set_location", lat=args.lat, lng=args.lng)
    if c == "grant":
        return tool_call("grant", package=args.package, permissions=args.permissions)
    raise Stop(EXIT_REFUSED, f"unknown phone command {c!r}")


def show_view(out):
    """A command's answer as the model reads it."""
    lines = list(out.get("results", []))
    lines += [f"note: {n}" for n in out.get("notes", [])]
    if out.get("packages"):
        lines.append("Apps:")
        lines += [f"- {p.get('package')}" + (" (user-installed)" if p.get("user") else "") for p in out["packages"]]
    if events := out.get("events"):
        lines.append("Events:")
        lines += [f"- {e}" for e in events]
    lines.append("Screen:")
    lines.append(out.get("screen") or "(empty)")
    lines.append("Interactions:")
    lines.append(out.get("interactions") or "(no numbered element accepts anything)")
    if out.get("no_shot"):
        lines.append(f"Screenshot: {out['no_shot']}")
    return "\n".join(lines)


def show_status(out):
    phone, lines = out.get("phone", {}), []
    if not phone.get("connected"):
        lines.append("Phone: not connected. penrose's window starts it; wait for it to boot.")
    elif phone.get("yours"):
        lines.append("Phone: connected, held by this session.")
    elif phone.get("holder"):
        lines.append(f"Phone: connected, busy. {phone.get('busy') or ''}".rstrip())
    else:
        lines.append("Phone: connected and free. Any command takes it; `relay phone release` gives it back.")
    account = out.get("google_account")
    lines.append(f"Google account: {account.get('email') if isinstance(account, dict) else account or 'none signed in'}")
    fix = out.get("location")
    if isinstance(fix, dict) and fix.get("lat") is not None:
        lines.append(f"Location: {fix['lat']}, {fix['lng']} (the user's real position; apps get it when they ask)")
    else:
        lines.append("Location: unknown")
    lines.append(f"Session: {out.get('client')}")
    return "\n".join(lines)


def phone(args, relay):
    client = client_id()
    if args.command == "status":
        out = relay.call("GET", f"/api/phone/status?client={client}", timeout=20)
        return out, show_status(out), EXIT_OK
    if args.command == "release":
        out = relay.call("POST", "/api/phone/release", {"client": client}, timeout=60)
        return out, "Released." if out.get("released") else "This session held no phone.", EXIT_OK
    if args.command == "knowhow":
        out = relay.call("GET", "/api/phone/knowhow?app=" + urllib.request.quote(args.app), timeout=20)
        if out.get("skill"):
            return out, out["skill"], EXIT_OK
        known = ", ".join(out.get("known_apps", [])) or "none"
        return out, f"Nothing written down about {args.app}. Apps with notes: {known}.", EXIT_OK
    if args.command == "screen":
        out = relay.call("POST", "/api/phone/screen", {"client": client, "shot": bool(args.shot)})
        text = show_view(out)
        if args.shot and out.get("shot"):
            target = Path(os.environ.get("RELAY_CWD") or os.getcwd()) / args.shot
            target.write_bytes(base64.b64decode(out["shot"]))
            text += f"\nScreenshot: {target}"
        return out, text, EXIT_OK
    out = relay.call("POST", "/api/phone/act", {"client": client, "calls": [phone_call(args)]})
    return out, show_view(out), EXIT_OK if out.get("ok", True) else EXIT_REFUSED


# --- the installation (shared with the window's Connect Claude page, api/phone.py) -------------------------------

def skill_source():
    """The android-phone skill shipped with Relay (backend/skills/claude, bundled like the app skills)."""
    return Path(__file__).resolve().parents[1] / "skills" / "claude" / SKILL_NAME / "SKILL.md"


def skill_target(project=None):
    root = Path(project).resolve() if project else Path.home()
    return root / ".claude" / "skills" / SKILL_NAME / "SKILL.md"


def skill_state(target=None):
    """"installed", "outdated" (differs from the shipped skill) or "missing"."""
    target = target or skill_target()
    if not target.is_file():
        return "missing"
    try:
        return "installed" if target.read_text(encoding="utf-8") == skill_source().read_text(encoding="utf-8") else "outdated"
    except OSError:
        return "outdated"


def install_skill(project=None):
    source = skill_source()
    if not source.is_file():
        raise Stop(EXIT_REFUSED, f"the skill is missing from this penrose ({source})")
    target = skill_target(project)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    return target


def note_in_memory():
    """A short note about the phone in ~/.claude/CLAUDE.md, once. Returns whether it was added now."""
    memory = Path.home() / ".claude" / "CLAUDE.md"
    existing = memory.read_text(encoding="utf-8") if memory.is_file() else ""
    if MEMORY_MARK in existing:
        return False
    memory.parent.mkdir(parents=True, exist_ok=True)
    memory.write_text(existing.rstrip() + ("\n\n" if existing.strip() else "") + MEMORY_BLOCK, encoding="utf-8")
    return True


def claude_code_path():
    """Where Claude Code is, or ""."""
    if found := shutil.which("claude"):
        return found
    home = Path.home()
    candidates = [home / ".local" / "bin" / "claude", home / ".local" / "bin" / "claude.exe"]
    if sys.platform == "win32":
        if appdata := os.environ.get("APPDATA"):
            candidates.append(Path(appdata) / "npm" / "claude.cmd")
    else:
        candidates += [Path("/usr/local/bin/claude"), Path("/opt/homebrew/bin/claude")]
    return next((str(c) for c in candidates if c.exists()), "")


def claude_desktop_present():
    """Whether the Claude desktop app (Cowork, the Code tab) is installed, as far as its folders tell."""
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "")
        return bool(local) and any((Path(local) / name).exists() for name in ("Claude-Data", "AnthropicClaude", "Programs/claude-desktop"))
    if sys.platform == "darwin":
        return Path("/Applications/Claude.app").exists() or (Path.home() / "Applications" / "Claude.app").exists()
    return (Path.home() / ".config" / "Claude").exists()


def launcher_dir():
    """Where the desktop app puts the `relay` launcher (desktop/src/main.js writeLauncher)."""
    return data_dirs()[0].parent / "bin"


def launcher_file(bin_dir):
    return Path(bin_dir) / ("relay.cmd" if sys.platform == "win32" else "relay")


def user_path():
    """The PATH new programs get: on Windows the user's persistent Path (the registry), elsewhere this process's."""
    if sys.platform == "win32":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                value, _ = winreg.QueryValueEx(key, "Path")
        except OSError:
            value = ""
        return [p for p in str(value).split(";") if p]
    return [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]


def on_path(directory, entries=None):
    def plain(p):
        p = os.path.normpath(os.path.expandvars(os.path.expanduser(str(p))))
        return p.lower() if sys.platform == "win32" else p
    return plain(directory) in {plain(p) for p in (user_path() if entries is None else entries)}


def put_on_path(bin_dir):
    """Make `relay` reachable from new terminals: Windows adds the launcher folder to the user's Path; elsewhere a
    symlink in ~/.local/bin (made if needed). Returns what the user still has to do."""
    bin_dir = Path(bin_dir)
    if sys.platform == "win32":
        import ctypes
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ | winreg.KEY_WRITE) as key:
            try:
                value, kind = winreg.QueryValueEx(key, "Path")
            except OSError:
                value, kind = "", winreg.REG_EXPAND_SZ
            if not on_path(bin_dir, str(value).split(";")):
                winreg.SetValueEx(key, "Path", 0, kind, (str(value).rstrip(";") + ";" if value else "") + str(bin_dir))
        # Explorer and new terminals learn of the change now (WM_SETTINGCHANGE "Environment").
        with __import__("contextlib").suppress(Exception):
            ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x1A, 0, "Environment", 2, 5000, None)
        return "Restart Claude Code (or the Claude app), or open a new terminal, to pick it up."
    local = Path.home() / ".local" / "bin"
    local.mkdir(parents=True, exist_ok=True)
    link = local / "relay"
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(bin_dir / "relay")
    if on_path(local):
        return "Open a new terminal, or restart Claude Code, to pick it up."
    return (f"Add {local} to your PATH: put `export PATH=\"$HOME/.local/bin:$PATH\"` in ~/.zprofile (macOS) or "
            "~/.profile, then restart Claude Code.")


# --- the Claude app (Cowork): Relay's MCP server in its config ---------------------------------------------------

MCP_NAME = "relay"


def claude_app_config():
    """The Claude desktop app's claude_desktop_config.json (it may not exist yet), or None when the app is not
    installed. CLAUDE_USER_DATA_DIR overrides. Windows: the Store build keeps it in
    %LOCALAPPDATA%\\Claude-Data\\claude_<publisher> (the most recently used, if several), older builds in
    %APPDATA%\\Claude; macOS ~/Library/Application Support/Claude; Linux ~/.config/Claude."""
    name = "claude_desktop_config.json"
    if override := os.environ.get("CLAUDE_USER_DATA_DIR"):
        return Path(override) / name
    if sys.platform == "win32":
        local, roaming = os.environ.get("LOCALAPPDATA", ""), os.environ.get("APPDATA", "")
        store = [p for p in (Path(local) / "Claude-Data").glob("claude_*") if p.is_dir()] if local else []
        store.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        folders = store + ([Path(roaming) / "Claude"] if roaming else [])
    elif sys.platform == "darwin":
        folders = [Path.home() / "Library" / "Application Support" / "Claude"]
    else:
        folders = [Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "Claude"]
    found = next((f for f in folders if (f / name).is_file()), None) or next((f for f in folders if f.is_dir()), None)
    return found / name if found else None


def mcp_entry(cli_file=None):
    """How the Claude app starts Relay's MCP server: this program with `mcp` (the bundled relay-server, or the
    repository's Python in development), pointed at the discovery file."""
    if getattr(sys, "frozen", False):
        entry = {"command": sys.executable, "args": ["mcp"], "env": {}}
    else:
        backend = str(Path(__file__).resolve().parents[1])
        entry = {"command": sys.executable, "args": ["-m", "app.desktop", "mcp"], "env": {"PYTHONPATH": backend}}
    cli_file = cli_file or next((p for p in cli_files() if p.is_file()), None)
    if cli_file:
        entry["env"]["RELAY_CLI_FILE"] = str(cli_file)
    return entry


def read_config(path):
    """The app's config as a dict ({} when absent). Refuses one it cannot parse rather than overwrite it."""
    if not path.is_file():
        return {}
    try:
        config = json.loads(path.read_text(encoding="utf-8") or "{}")
    except ValueError:
        raise Stop(EXIT_REFUSED, f"{path} is not valid JSON; fix it in the Claude app (Settings > Developer > Edit Config) first.") from None
    if not isinstance(config, dict):
        raise Stop(EXIT_REFUSED, f"{path} does not hold a JSON object.")
    return config


def claude_app_registered(path=None):
    path = path or claude_app_config()
    try:
        return bool(path) and MCP_NAME in (read_config(path).get("mcpServers") or {})
    except Stop:
        return False


def register_claude_app(entry=None, path=None):
    """Add (or update) Relay's MCP server in the Claude app's config, keeping everything else in it. The file as it
    was is kept beside it (.before-relay) the first time. Returns the config's path."""
    path = path or claude_app_config()
    if not path:
        raise Stop(EXIT_REFUSED, "The Claude desktop app was not found.")
    config = read_config(path)
    backup = path.with_name(path.name + ".before-relay")
    if path.is_file() and not backup.exists():
        backup.write_bytes(path.read_bytes())
    servers = config.get("mcpServers") if isinstance(config.get("mcpServers"), dict) else {}
    config["mcpServers"] = {**servers, MCP_NAME: entry or mcp_entry()}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return path


def unregister_claude_app(path=None):
    """Take Relay's MCP server out of the Claude app's config. False when it was not there."""
    path = path or claude_app_config()
    if not path or not path.is_file():
        return False
    config = read_config(path)
    if MCP_NAME not in (config.get("mcpServers") or {}):
        return False
    del config["mcpServers"][MCP_NAME]
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return True


def cowork_skill_source():
    return Path(__file__).resolve().parents[1] / "skills" / "claude" / "cowork" / SKILL_NAME / "SKILL.md"


def write_cowork_zip(target):
    """The Cowork skill as the ZIP claude.ai's Customize > Skills takes: one folder holding SKILL.md."""
    import zipfile

    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{SKILL_NAME}/SKILL.md", cowork_skill_source().read_text(encoding="utf-8"))
    return target


def cowork_zip_path():
    return data_dirs()[0].parent / "claude" / f"{SKILL_NAME}.zip"


def installation(bin_dir=None):
    """Everything the Connect Claude page and `relay doctor` show about this computer."""
    bin_dir = Path(bin_dir or launcher_dir())
    link = Path.home() / ".local" / "bin" / "relay"
    app_config = claude_app_config()
    return {
        "claude_code": claude_code_path(),
        "claude_app_config": str(app_config or ""),
        "claude_app_registered": claude_app_registered(app_config) if app_config else False,
        "claude_desktop": claude_desktop_present(),
        "skill": skill_state(),
        "skill_path": str(skill_target()),
        "launcher": str(launcher_file(bin_dir)),
        "launcher_present": launcher_file(bin_dir).exists(),
        "on_path": bool(shutil.which("relay")) or on_path(bin_dir) or (sys.platform != "win32" and link.is_symlink() and on_path(link.parent)),
        "platform": sys.platform,
    }


# --- doctor and setup --------------------------------------------------------------------------------------------

def doctor(args):
    checks, ok = [], True

    def check(good, text):
        nonlocal ok
        ok = ok and good
        checks.append(f"[{'ok' if good else '!!'}] {text}")

    found = next((p for p in cli_files() if p.is_file()), None)
    check(bool(found), f"discovery file: {found}" if found else "discovery file: none of " + ", ".join(map(str, cli_files())))
    try:
        info = discover()
        check(True, f"penrose API: {info['url']}")
        out = Relay(info).call("GET", f"/api/phone/status?client={client_id()}", timeout=20)
        check(True, "integration token: accepted")
        phone = out.get("phone", {})
        check(bool(phone.get("connected")), "phone: connected" if phone.get("connected") else "phone: not connected (penrose's window starts it)")
    except Stop as exc:
        check(False, str(exc))
    state = installation()
    check(state["on_path"], "`relay` on PATH" if state["on_path"] else f"`relay` is not on PATH: run `relay setup claude-code --path` (the launcher is {state['launcher']})")
    check(bool(state["claude_code"]), f"Claude Code: {state['claude_code']}" if state["claude_code"] else "Claude Code (`claude`) not found")
    project = skill_target(os.getcwd())
    installed = project if project.is_file() else skill_target() if state["skill"] != "missing" else None
    check(bool(installed), f"skill: {installed}" + (" (older than this penrose's: run `relay setup claude-code`)" if state["skill"] == "outdated" else "")
          if installed else f"skill not installed: run `relay setup claude-code` (writes {state['skill_path']})")
    if state["claude_app_config"]:
        checks.append("[ok] Claude app: penrose's phone tools registered (Cowork)" if state["claude_app_registered"]
                      else "[--] Claude app: penrose's phone tools not registered; for Cowork run `relay setup claude-app`")
    return {"ok": ok, "checks": checks, **state}, "\n".join(checks), EXIT_OK if ok else EXIT_REFUSED


def setup_claude_app(args):
    if args.remove:
        removed = unregister_claude_app()
        return {"removed": removed}, ("Took penrose out of the Claude app's config; restart the Claude app." if removed
                                      else "penrose was not in the Claude app's config."), EXIT_OK
    path = register_claude_app()
    archive = write_cowork_zip(cowork_zip_path())
    lines = [f"Added penrose's phone tools to the Claude app ({path}). Quit and reopen the Claude app to load them.",
             f"Optional: upload {archive} in claude.ai under Customize > Skills, so Cowork also knows when to reach for the phone.",
             "Try it: in Cowork, ask for something done on the phone, e.g. \"open YouTube on the phone and tell me what's trending\"."]
    return {"config": str(path), "skill_zip": str(archive), **installation()}, "\n".join(lines), EXIT_OK


def setup(args):
    if args.target == "claude-app":
        return setup_claude_app(args)
    target = install_skill(args.project)
    lines = [f"Wrote the {SKILL_NAME} skill to {target}."]
    if args.memory:
        lines.append(f"Added a note about the phone to {Path.home() / '.claude' / 'CLAUDE.md'}." if note_in_memory()
                     else "~/.claude/CLAUDE.md already mentions the phone.")
    state = installation()
    if args.path:
        lines.append(put_on_path(launcher_dir()))
    elif not state["on_path"]:
        lines.append("`relay` is not on PATH yet: Claude Code finds it only once it is. Run `relay setup claude-code --path`, "
                     f"or add {launcher_dir()} to your PATH yourself.")
    lines.append("Try it: in Claude Code, ask for something done on the phone, e.g. \"open YouTube on the phone and tell me what's trending\".")
    return {"skill": str(target), **installation()}, "\n".join(lines), EXIT_OK


# --- main ---------------------------------------------------------------------------------------------------------

def parser():
    p = argparse.ArgumentParser(
        prog="relay", description="penrose's Android phone, for agents on this computer.",
        epilog="Look before acting: `relay phone screen`, then act on an element by its [n] number from the latest "
               "output; every action prints the new screen. Exit codes: 0 done, 1 refused or failed, 2 penrose not running, "
               "3 phone busy.", formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--json", action="store_true", help="answer as JSON")
    sub = p.add_subparsers(dest="group", required=True)
    ph = sub.add_parser("phone", help="look at the phone and act on it").add_subparsers(dest="command", required=True)
    ph.add_parser("status", help="the phone, who holds it, the Google account, the location")
    ph.add_parser("screen", help="the screen now, as numbered text").add_argument("--shot", metavar="FILE", help="also save a JPEG")
    for name, text in (("tap", "tap element N"), ("long-press", "long-press element N")):
        ph.add_parser(name, help=text).add_argument("n", type=int)
    for name, text in (("type", "type TEXT into field N (replaces its text)"), ("submit", "type TEXT into N and press its keyboard key (search)"),
                       ("choose", "pick the option TEXT in select N")):
        q = ph.add_parser(name, help=text)
        q.add_argument("n", type=int)
        q.add_argument("text", nargs="+")
    q = ph.add_parser("scroll", help="scroll list or page N")
    q.add_argument("n", type=int)
    q.add_argument("direction", choices=["up", "down", "left", "right", "top", "bottom"])
    q = ph.add_parser("set", help="set slider, rating or picker N")
    q.add_argument("n", type=int)
    q.add_argument("value", choices=["min", "max", "up", "down"])
    ph.add_parser("key", help="a system key").add_argument("key", choices=["back", "home", "recents", "enter", "hide-keyboard"])
    ph.add_parser("swipe", help="a finger swipe across the screen").add_argument("direction", choices=["up", "down", "left", "right"])
    q = ph.add_parser("open", help="open an app by name or package")
    q.add_argument("app", nargs="+")
    q.add_argument("--shortcut", metavar="LABEL", help="open it at one of the shortcuts its icon offers (Orders, New timer)")
    q = ph.add_parser("link", help="open a URL or deep link")
    q.add_argument("url")
    q.add_argument("--package", help="the app that must handle it")
    ph.add_parser("apps", help="find installed apps").add_argument("query", nargs="+")
    q = ph.add_parser("wait", help="wait until the screen shows TEXT (up to 5 s)")
    q.add_argument("text", nargs="*")
    q.add_argument("--gone", metavar="TEXT", help="until TEXT is no longer shown")
    q.add_argument("--activity", metavar="NAME", help="until the screen's name contains NAME")
    q.add_argument("--seconds", type=float, default=5)
    q = ph.add_parser("setting", help="flip a phone setting")
    q.add_argument("name", choices=["wifi", "mobile_data", "airplane", "bluetooth", "location", "dark_mode", "rotation", "brightness", "dnd", "keep_awake"])
    q.add_argument("value")
    q = ph.add_parser("location", help="set the GPS position (only if the user asks)")
    q.add_argument("lat", type=float)
    q.add_argument("lng", type=float)
    q = ph.add_parser("grant", help="pre-approve an app's permissions")
    q.add_argument("package")
    q.add_argument("permissions", nargs="+")
    ph.add_parser("knowhow", help="what penrose knows about an app").add_argument("app", nargs="+")
    ph.add_parser("release", help="give the phone back")
    sub.add_parser("doctor", help="check the installation")
    q = sub.add_parser("setup", help="install the skill for a Claude product")
    q.add_argument("target", choices=["claude-code", "claude-app"])
    q.add_argument("--remove", action="store_true", help="claude-app: take penrose out of the Claude app's config")
    q.add_argument("--path", action="store_true", help="also put `relay` on your PATH (Windows: the user's Path; elsewhere ~/.local/bin)")
    q.add_argument("--memory", action="store_true", help="also note the phone in ~/.claude/CLAUDE.md")
    q.add_argument("--project", metavar="DIR", help="install into DIR/.claude/skills instead of the user's skills")
    sub.add_parser("mcp", help="serve the phone to the Claude app as an MCP server (stdio; the app starts it)")
    for name in ("run", "reply", "cancel"):
        sub.add_parser(name, help="hand a whole errand to penrose (not yet available)").add_argument("text", nargs="*")
    return p


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        with __import__("contextlib").suppress(Exception):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = parser().parse_args(argv)
    if args.group == "mcp":
        from .mcp import serve

        return serve()
    for key in ("text", "app", "query"):
        if isinstance(getattr(args, key, None), list):
            setattr(args, key, " ".join(getattr(args, key)))
    try:
        if args.group == "doctor":
            out, text, code = doctor(args)
        elif args.group == "setup":
            out, text, code = setup(args)
        elif args.group == "phone":
            out, text, code = phone(args, Relay(discover()))
        else:
            raise Stop(EXIT_REFUSED, f"`relay {args.group}` is not available yet: ask the user to type the request in penrose's window.")
    except Stop as exc:
        if args.json:
            print(json.dumps({"error": str(exc), "exit": exc.code}))
        else:
            print(f"error: {exc}")
        return exc.code
    print(json.dumps(out, ensure_ascii=False) if args.json else text)
    return code


if __name__ == "__main__":
    sys.exit(main())
