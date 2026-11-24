"""The backend's device tools, beside the harness's device-level calls (open_app, open_link, wait_for,
set_setting... in app/bridge/tools.py). Each is a turn of its own: the phone may change under it, so the
agent looks again before acting on the screen."""

import re
from typing import Literal

from pydantic import Field

from .base import TurnTool

SCREEN_COMMAND = re.compile(r"\buiautomator\b|\bscreencap\b|\bdumpsys\s+window")
# With or without a source ("input touchscreen swipe ..." went through on 2026-11-14).
INPUT_COMMAND = re.compile(
    r"\binput\s+(-d\s+\S+\s+)?([a-z]+\s+)?(text|tap|swipe|draganddrop|motionevent|roll|keyevent|keycombination|press)\b")
# Commands that move or reconfigure the phone: its position, settings, permissions, system properties, apps.
# The phone is the user's; the location is theirs and is in every brief; settings go through set_setting and
# permissions through grant, where the runtime can undo and journal them.
MOVES_PHONE = re.compile(
    r"^\s*adb\s+(-s\s+\S+\s+)?emu\b|\bgeo\s+fix\b|\bsettings\s+(put|delete)\b|\bcmd\s+location\b|\bappops\b|"
    r"\bsetprop\b|\bsvc\s+(wifi|data|bluetooth|power|nfc)\b|\bwm\s+(size|density)\s+(\d|reset)|\breboot\b|(^|[\s;&|\x22\x27])su(\s|$)|"
    r"\bpm\s+(uninstall|clear|disable|disable-user|enable|grant|revoke|set-|reset-permissions)|\bdevice_config\b|\bime\s+set\b",
    re.I,
)
# adb push and pull reach any path on the computer. Files get onto the phone through the user's uploads and the
# orchestrator's put_on_phone (app/attachments.py), into the one folder the agents are told about.
FILE_TRANSFER = re.compile(r"(^|[\s;&|])adb\s+(-s\s+\S+\s+)?(push|pull)\b", re.I)
# A tunnel between the computer and the phone (adb forward to Chrome's DevTools, 2026-11-14) goes round every guard.
TUNNEL = re.compile(r"(^|[\s;&|])adb\s+(-s\s+\S+\s+)?(forward|reverse)\b", re.I)
LOCATION_READ = re.compile(r"\bdumpsys\s+location\b|\bcmd\s+location\b", re.I)
# Fetching a page from the shell is a way round a site that blocks the browser: not done.
SHELL_FETCH = re.compile(r"\b(curl|wget|nc|netcat|toybox\s+wget)\b", re.I)
# Banks, wallets, brokerages, remittance: money movement is not Relay's work, and these apps refuse this phone
# anyway. Matched on the package's dot-separated parts, plus any part that contains "bank".
FINANCE_PARTS = frozenset((
    "banking monzo revolut starling barclays hsbc lloyds natwest santander chase wellsfargo citi citibank capitalone "
    "bofa paytm phonepe venmo cashapp cash paypal zelle robinhood coinbase binance kraken etoro zerodha upstox groww "
    "nubank mpesa momo n26 klarna fidelity schwab vanguard trading212 gpay wallet walletnfcrel paisa upi bhim mobikwik "
    "freecharge sofi usbank pnc truist scotiabank bbva caixabank unicredit commerzbank sparkasse dkb itau bradesco "
    "picpay mercadopago gcash paymaya gopay grabpay alipay kakaobank toss kotak icici yono hdfc payzapp chipper opay "
    "palmpay kuda transferwise remitly worldremit westernunion xoom moneygram sendwave taptapsend lemfi"
).split())


OUTPUT_LIMIT = 2500  # characters of a command's output the agent sees in one turn


def clipped(output, limit=OUTPUT_LIMIT):
    """The output, or its start with a note that it was cut. Cut silently at 900 characters, a 300-line file read
    as its first recipes: the agent ran the same `cat` twenty times, then answered from the part it had seen
    (2026-11-15)."""
    if len(output) <= limit:
        return output
    return (f"{output[:limit]}\n[cut: these are the first {limit} of {len(output)} characters; the same command shows "
            "the same part again. Read the rest in parts (sed -n '40,80p' <file>, grep -n, tail).]")


def finance_package(package):
    parts = package.lower().split(".")
    return any(p in FINANCE_PARTS or "bank" in p for p in parts)


class AdbCommand(TurnTool):
    """One ADB command, for what no other tool does (an app's version, force-stopping an app, a system
    property). Apps open with open_app, links with open_link, and the installed apps are in your brief (`installed_apps`).
    A media file's length, size or date is in Android's media index: adb shell content query --uri
    content://media/external/audio/media --projection title:duration (video and images likewise).
    Never for screen input or UI dumps: those are refused, and the screen is in every turn already."""

    tool: Literal["adb_command"] = "adb_command"
    command: str = Field(description="One command starting with adb, e.g. adb shell pm list packages doordash.")

    async def run(self, s):
        if SCREEN_COMMAND.search(self.command):
            return ("NOT RUN: Screen shows the screen every turn. A screenshot to keep, send or upload is a clip (element 0 "
                    "is the whole screen): the orchestrator can put it on the phone. request_screenshot only shows you the "
                    "screen and keeps nothing.")
        if INPUT_COMMAND.search(self.command):
            return "NOT RUN: on-screen input goes through interact, press_key and swipe."
        if LOCATION_READ.search(self.command):
            return ("NOT RUN: the phone's position is in your brief (phone_location) and apps get it when they listen; "
                    "dumpsys shows nothing until one does. Work from the brief.")
        if TUNNEL.search(self.command):
            return "NOT RUN: adb forward and adb reverse are not available: work on the phone's screen."
        if FILE_TRANSFER.search(self.command):
            return ("NOT RUN: adb push and adb pull are not available. Files the user uploaded are on the phone already, in "
                    "Download/penrose (phone_files in your brief lists them); to read or use one, open it on the phone.")
        if MOVES_PHONE.search(self.command):
            return ("NOT RUN: the phone, its location, settings, permissions and apps are the user's. Its position is the "
                    "user's real one (phone_location in your brief): never move it. Settings go through set_setting, "
                    "permissions through grant; if the step cannot be done without more, report it.")
        if SHELL_FETCH.search(self.command):
            return ("NOT RUN: pages are read in Chrome, never fetched from the shell. A site that blocks the browser is "
                    "a blocked site: use another source or report it.")

        async def work():
            r = await s.devices.action(s.slot, s.owner, {"kind": "adb_command", "command": self.command})
            full, stderr = str(r.get("output", "")), str(r.get("stderr", ""))[:2000]
            output = full[:4000]
            if r.get("ok"):
                return True, f"ok: {clipped(full)}", {"output": output}
            return False, f"FAILED: {stderr or 'exit ' + str(r.get('exit_code'))}"[:600], {"output": output, "stderr": stderr}

        _, line = await s.device_action("adb_command", self.command, work)
        s.packages = None  # the installed apps may have changed
        return line


class InstallApp(TurnTool):
    """Install an app by its package ID, from sources that need no Google sign-in. A failure is final for
    that package: follow its next step (Play Store through the screen, or the website if no suitable app
    exists)."""

    tool: Literal["install_app"] = "install_app"
    package: str = Field(max_length=200, description="The Android package ID, e.g. com.dd.doordash.")

    async def run(self, s):
        if finance_package(self.package):
            return (f"NOT RUN: {self.package} looks like a bank, wallet, payment or trading app. penrose does not move money "
                    "or sign in to such apps (they also refuse this phone): report failed and say so plainly; the user "
                    "does that themselves. Paying inside a shop or booking app with a saved card is unaffected.")

        async def work():
            r = await s.devices.action(s.slot, s.owner, {"kind": "install_app", "package": self.package})
            line = f"{r.get('status')}: {r.get('message', '')} {r.get('next_step', '')}".strip()
            return bool(r.get("ok")), line, {"stderr": r.get("detail", "")}

        ok, line = await s.device_action("install_app", f"install_app {self.package}", work)
        if not ok and line.startswith("FAILED"):  # the device service did not answer
            line += " The app was not installed. Check the screen. Install from Play Store, or use the website if no suitable app exists."
        s.packages = None
        return line


DEVICE_TOOLS = (AdbCommand, InstallApp)
