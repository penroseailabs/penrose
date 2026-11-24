"""`relay mcp`: Relay's phone as a local MCP server (stdio, JSON-RPC 2.0 one message per line), for Cowork.

Cowork runs its shell in a sandboxed VM that cannot reach Relay on 127.0.0.1, but the Claude app starts the
user's local MCP servers on the computer itself and bridges their tools into the session. So this process
runs on the computer, finds Relay through its discovery file like the `relay` command does (app/cli.py), and
offers the phone subagent's tools as `phone_*` tools over the same phone API (app/api/phone.py). Its
instructions are the Cowork skill's text (backend/skills/claude/cowork/android-phone/SKILL.md), so Claude knows
the phone exists and how to use it even before the skill is uploaded. No network listener, so no TLS.

Standard library only."""

import contextlib
import json
import re
import sys
import urllib.parse

from . import cli

PROTOCOL = "2025-06-18"
SKILL = cli.Path(__file__).resolve().parents[1] / "skills" / "claude" / "cowork" / "android-phone" / "SKILL.md"
ELEMENT = {"type": "integer", "description": "The [n] number of the element on the latest screen."}


def tool(name, description, properties=None, required=(), read_only=False):
    return {"name": name, "description": description,
            "inputSchema": {"type": "object", "properties": properties or {}, "required": list(required)},
            "annotations": {"readOnlyHint": read_only, "openWorldHint": True}}


TOOLS = [
    tool("phone_status", "Is the user's Android phone (in penrose) up, who holds it, which Google account is signed in, and "
         "where it is. Call first. Takes nothing and changes nothing.", read_only=True),
    tool("phone_screen", "The phone's screen now, as numbered text: events since the last look, then each element with "
         "its [n] number, then what each element accepts. Takes the phone for this session. `screenshot` adds a "
         "picture, only for what text cannot show (a map, a colour, a canvas).",
         {"screenshot": {"type": "boolean", "description": "Also return a JPEG of the screen."}}),
    tool("phone_interact", "Act on ONE numbered element of the latest screen, then get the new screen. tap: buttons, "
         "tabs, rows, links. long_press. type: put `value` into a field (replaces its text; never tap it first). "
         "submit: press the keyboard's action key. scroll_up/down/left/right: reveal more of a list or page; "
         "scroll_top/scroll_bottom jump a native list to its ends. choose: pick the option `value` in a Select. "
         "set_value: `value` min|max|up|down for a slider, rating or picker. Only what the screen's Interactions "
         "list for that element is accepted; anything else is refused with the reason.",
         {"element": ELEMENT, "interaction": {"type": "string", "enum": [
             "tap", "long_press", "type", "focus", "submit", "scroll_up", "scroll_down", "scroll_left", "scroll_right",
             "scroll_top", "scroll_bottom", "choose", "set_value"]},
          "value": {"type": "string", "description": "Only for type, choose and set_value."}}, ["element", "interaction"]),
    tool("phone_type_and_submit", "Type `text` into field `element` and press its keyboard action key (search/go) in "
         "one step: the way to search. Not when you want to read autocomplete suggestions first.",
         {"element": ELEMENT, "text": {"type": "string"}}, ["element", "text"]),
    tool("phone_press_key", "A phone key: back (previous screen or close a dialog), home, recents, enter, hide_keyboard "
         "(close the keyboard without leaving the screen). The app's own back arrow is a numbered element instead.",
         {"key": {"type": "string", "enum": ["back", "home", "recents", "enter", "hide_keyboard"]}}, ["key"]),
    tool("phone_swipe", "Swipe a finger across the whole screen; `direction` is where the finger moves. For pull to "
         "refresh, flipping a pager or dragging a sheet; prefer phone_interact scroll on a listed list.",
         {"direction": {"type": "string", "enum": ["up", "down", "left", "right"]}}, ["direction"]),
    tool("phone_open_app", "Open an installed app straight to its main screen, by name or package ID (\"Uber Eats\", "
         "\"com.android.chrome\"), or with `shortcut` at one of the shortcuts its icon offers, by label (\"Orders\", "
         "\"New timer\"; phone_list_apps shows each app's). If it is not installed, open its Play Store page with "
         "phone_open_link market://details?id=PACKAGE.",
         {"app": {"type": "string"}, "shortcut": {"type": "string"}}, ["app"]),
    tool("phone_list_apps", "Find installed apps by part of their name or package ID; no match means not installed.",
         {"query": {"type": "string"}}, ["query"], read_only=True),
    tool("phone_open_link", "Open a URL or deep link in the app that handles it: https://, market://details?id=…, "
         "geo:lat,lng?q=…, tel:, mailto:. `package` forces one app.",
         {"uri": {"type": "string"}, "package": {"type": "string"}}, ["uri"]),
    tool("phone_wait_for", "Wait until the screen shows `text`, no longer shows `gone`, or its activity name contains "
         "`activity` (all given conditions), up to `timeout` seconds (at most 5), then get the screen. A timeout "
         "is information, not an error.",
         {"text": {"type": "string"}, "gone": {"type": "string"}, "activity": {"type": "string"},
          "timeout": {"type": "number"}}),
    tool("phone_set_setting", "Flip a phone setting, only when the task needs it: wifi, mobile_data, airplane, bluetooth, "
         "location, dnd, keep_awake (on|off); dark_mode (on|off|auto); rotation (auto|portrait|landscape); "
         "brightness (0-100). Put back when the session ends.",
         {"name": {"type": "string", "enum": ["wifi", "mobile_data", "airplane", "bluetooth", "location", "dark_mode",
                                              "rotation", "brightness", "dnd", "keep_awake"]},
          "value": {"type": "string"}}, ["name", "value"]),
    tool("phone_set_location", "Set the phone's GPS position. Only if the user asks: the phone already reports their "
         "real position.", {"lat": {"type": "number"}, "lng": {"type": "number"}}, ["lat", "lng"]),
    tool("phone_grant", "Pre-approve an app's runtime permissions so no permission dialog appears: location, camera, "
         "microphone, contacts, calendar, notifications, phone, sms, media, storage, nearby_devices.",
         {"package": {"type": "string"}, "permissions": {"type": "array", "items": {"type": "string"}}},
         ["package", "permissions"]),
    tool("phone_knowhow", "What penrose has written down about an app: deep links, login, where things are. Read it "
         "before working in an app.", {"app": {"type": "string"}}, ["app"], read_only=True),
    tool("phone_release", "Give the phone back (to penrose's own tasks and the user). Call when the work is done; it is "
         "also given back after five idle minutes."),
]
ACTIONS = {"phone_interact": "interact", "phone_type_and_submit": "type_and_submit", "phone_press_key": "press_key",
           "phone_swipe": "swipe", "phone_open_app": "open_app", "phone_list_apps": "list_apps",
           "phone_open_link": "open_link", "phone_wait_for": "wait_for",
           "phone_set_setting": "set_setting", "phone_set_location": "set_location", "phone_grant": "grant"}


def instructions():
    """The Cowork skill without its front matter: the server's instructions, shown to the model."""
    try:
        text = SKILL.read_text(encoding="utf-8")
    except OSError:
        return "penrose's Android phone: start with phone_status, then phone_screen, and act on elements by number."
    return re.sub(r"\A---\s*\n.*?\n---\s*\n", "", text, flags=re.S).strip()


def call(name, args, client):
    """One tool call: MCP content blocks and whether it is an error."""
    try:
        relay = cli.Relay(cli.discover())
        if name == "phone_status":
            return [text(cli.show_status(relay.call("GET", f"/api/phone/status?client={client}", timeout=20)))], False
        if name == "phone_release":
            out = relay.call("POST", "/api/phone/release", {"client": client}, timeout=60)
            return [text("Released." if out.get("released") else "This session held no phone.")], False
        if name == "phone_knowhow":
            out = relay.call("GET", "/api/phone/knowhow?app=" + urllib.parse.quote(str(args.get("app", ""))), timeout=20)
            known = ", ".join(out.get("known_apps", [])) or "none"
            return [text(out.get("skill") or f"Nothing written down about {args.get('app')}. Apps with notes: {known}.")], False
        if name == "phone_screen":
            out = relay.call("POST", "/api/phone/screen", {"client": client, "shot": bool(args.get("screenshot"))})
            blocks = [text(cli.show_view(out))]
            if out.get("shot"):
                blocks.append({"type": "image", "data": out["shot"], "mimeType": "image/jpeg"})
            return blocks, False
        if name in ACTIONS:
            arguments = {k: v for k, v in args.items() if v is not None and v != ""}
            out = relay.call("POST", "/api/phone/act", {"client": client, "calls": [{"name": ACTIONS[name], "arguments": arguments}]})
            return [text(cli.show_view(out))], not out.get("ok", True)
        return [text(f"Unknown tool {name}.")], True
    except cli.Stop as exc:
        return [text(f"error: {exc}")], True


def text(value):
    return {"type": "text", "text": value}


def answer(message, client):
    """The reply to one JSON-RPC message, or None for a notification."""
    method, ident, params = message.get("method"), message.get("id"), message.get("params") or {}
    if ident is None:
        return None
    if method == "initialize":
        result = {"protocolVersion": params.get("protocolVersion") or PROTOCOL, "capabilities": {"tools": {}},
                  "serverInfo": {"name": "relay", "title": "penrose phone", "version": "1.0.0"},
                  "instructions": instructions()}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        content, error = call(params.get("name"), params.get("arguments") or {}, client)
        result = {"content": content, "isError": error}
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": ident, "error": {"code": -32601, "message": f"Method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": ident, "result": result}


def serve(stdin=None, stdout=None):
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    # UTF-8 both ways, and bare \n line ends on Windows: one JSON message per line.
    for stream, newline in ((stdin, None), (stdout, "\n")):
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(encoding="utf-8", newline=newline)
    client =cli.client_id() if cli.client_id() != "default" else "claude-app"
    for line in stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except ValueError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        else:
            batch = message if isinstance(message, list) else [message]
            replies = [r for r in (answer(m, client) for m in batch if isinstance(m, dict)) if r]
            reply = replies if isinstance(message, list) else (replies[0] if replies else None)
        if reply:
            stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            stdout.flush()
    return 0
