"""Google sign-in on the phone, driven for the user. They give their email in Relay's window or the phone app and choose
how to sign in: the passwordless way ("Tap Yes on your phone or tablet", approved on their real phone, where Google
may also ask them to tap a number Relay shows), or their password. The gateway opens Google's own sign-in screens,
types the email, and looks for the prompt past the password. When Google offers no prompt, or the user chose their
password, it asks for the password (state "need_password"): the user types it in the window or the phone app, and
Relay types it on the phone once and keeps it nowhere (redacted from the phone's output like any private code).

What Relay never does: go into Google's account recovery (the method list's own "Try another way"), which ends in a
forced password change; change a password; answer a CAPTCHA or any other check that it is a person; or agree to
Google's terms without the user's say-so (state "agree" waits for them). Those screens and anything it does not
recognise are handed back to the user on the phone (state "manual"), with the sign-in left open where it is. The
device's own record of accounts (`dumpsys account`) says when it is done.

One sign-in per phone, kept in memory: the window polls GET for its state."""

import asyncio
import contextlib
import re
import time
import typing
from dataclasses import dataclass

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from ..android import google_accounts, ui_nodes
from . import core, sleep, watch

router = APIRouter(prefix="/devices/{slot}/google/signin")

# A Gmail address, a Workspace address, or a bare Gmail username (Google adds @gmail.com). `input text` takes it
# as is: no spaces, quotes or percent signs.
EMAIL = re.compile(r"[A-Za-z0-9._+-]+(@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+)?")

DUMP = "/data/local/tmp/relay-signin.xml"
# Packages whose screens are the sign-in: Google Play services draws it, Settings opens it.
SIGNIN_PACKAGES = ("com.google.android.gms", "com.google.android.gsf", "com.android.settings")

STEP = 1.5  # seconds between screen reads
LIMIT = 15 * 60  # the whole sign-in, waiting on the user included
STUCK = 60  # on a screen Relay does not know, or one that does not move on after Relay acts, before handing over
BUSY = 120  # on a loading screen
AGAIN = 8  # before acting again on a screen that has not changed

CAPTCHA = ("type the text you hear or see", "not a robot", "recaptcha", "captcha")
PROMPT = ("google sent a notification", "tap yes on the notification", "check your phone", "open the gmail app",
          "tap yes on your phone to", "tap the sign-in notification", "pull down the notification bar")
PROMPT_OPTION = ("tap yes on your phone", "on your phone or tablet", "get a notification on your", "google prompt")
NOT_FOUND = ("couldn't find your google account", "enter a valid email", "enter an email or phone number")
REFUSED = ("denied", "couldn't sign you in", "may not be secure", "didn't confirm", "sign-in attempt was blocked")
AGREE = ("i agree", "terms of service", "google services")
AGREE_BUTTON = ("i agree", "agree", "accept")
# After account recovery Google may insist on a new password: the user's decision, never Relay's.
NEW_PASSWORD = ("change password", "create a strong password", "create a new, strong password")
# A wrong password: Google says so under the password field and stays on the screen.
WRONG_PASSWORD = ("wrong password", "password was changed", "incorrect")
PASSWORD_TRIES = 3
CHECK = ("verify it's you", "confirm it's you", "2-step verification", "verify that it's you")
# Google's account recovery. Confirming the account's phone number can lead on to the prompt: Relay asks the user
# for the number in its window and types it. Any other question (a recovery email, a code) is the user's to answer
# on the phone. Recovery's own "Try another way" ended in "Couldn't sign you in" on a new emulator: never tapped.
RECOVERY = ("account recovery", "confirm the recovery email")
RECOVERY_PHONE = ("confirm the phone number you provided",)
WRONG = ("wrong number", "doesn't match", "didn't match", "try again", "enter a valid phone", "invalid")
# What a phone number field may show before anything is typed.
PLACEHOLDERS = ("", "email or phone", "phone number", "enter phone number")
BUSY_TEXT = ("checking info", "just a sec", "one moment", "loading", "please wait", "signing in", "getting your account")

HAND_OVER = {
    "captcha": "Google wants to check you’re a person. Finish signing in on the phone beside this.",
    "no_prompt": "Google didn’t offer a “Tap Yes” prompt for this account on this phone. Finish signing in on the "
                 "phone beside this, with your password or another way Google offers there.",
    "new_password": "Google wants a new password for this account before it signs in. penrose won’t change it: "
                    "start again and sign in with your current password instead.",
    "wrong_password": "Google didn’t accept that password. Start again, or sign in on the phone beside this.",
    "recovery": "Google wants to confirm your account details. Finish signing in on the phone beside this.",
    "check": "Google wants to check it’s you another way. Finish signing in on the phone beside this.",
    "unknown": "penrose didn’t recognise Google’s screen. Finish signing in on the phone beside this.",
    "slow": "Google is taking a long time. Finish signing in on the phone beside this.",
    "email": "penrose couldn’t enter your email. Type it on the phone beside this.",
    "timeout": "Signing in is taking too long. Finish on the phone beside this.",
    "error": "Something went wrong reading the phone. Finish signing in on the phone beside this.",
}


def norm(value):
    return (value or "").replace("’", "'").replace(" ", " ").strip().lower()


def label(node):
    return norm(node.get("text") or node.get("content-desc"))


def find(nodes, *phrases, exact=False):
    """The first node whose text or description is (exact) or contains one of `phrases` (lower case)."""
    for node in nodes:
        value = label(node)
        if value and any(value == p if exact else p in value for p in phrases):
            return node
    return None


@dataclass
class Screen:
    kind: str
    tap: dict | None = None  # what Relay taps on this screen
    field: dict | None = None  # the email field
    number: str | None = None  # the number Google asks the user to tap on their phone
    password: dict | None = None  # the method list's "Enter your password"
    hint: str | None = None  # the last digits of the phone number Google asks the user to confirm
    error: bool = False  # Google says what was entered did not match


def prompt_number(nodes, blob):
    """The number Google asks the user to tap on their phone ("…then tap 37 on your phone"), shown large on its own."""
    if found := re.search(r"\btap (\d{1,3})\b", blob):
        return found[1]
    return next((label(n) for n in nodes if re.fullmatch(r"\d{1,3}", label(n))), None)


def read_screen(nodes):
    """Which of Google's sign-in screens `nodes` (from ui_nodes) show. The order matters: the prompt screen says
    "verify it's you" too, and the method list names the prompt it leads to."""
    blob = "\n".join(label(n) for n in nodes)

    def has(*phrases):
        return any(p in blob for p in phrases)

    field = next((n for n in nodes if "EditText" in n.get("class", "")), None)
    another = find(nodes, "try another way", exact=True)
    if has(*CAPTCHA):
        return Screen("captcha")
    if has("choose how you want to sign in"):
        return Screen("choose", tap=find(nodes, *PROMPT_OPTION), password=find(nodes, "enter your password"))
    if has(*PROMPT):
        return Screen("prompt", number=prompt_number(nodes, blob))
    if has("sign in with ease", "accounts connected to this phone number"):
        # Google offers to find the account from the phone's number through the carrier: Skip, share nothing.
        return Screen("skip", tap=find(nodes, "skip", exact=True))
    if has("who will be using this device"):
        # "I will be using this device" is chosen already: Next.
        return Screen("skip", tap=find(nodes, "next", exact=True))
    if has(*NOT_FOUND):
        return Screen("not_found")
    if has(*REFUSED):
        return Screen("refused")
    if has(*NEW_PASSWORD):
        return Screen("new_password")
    if field and has("email or phone", "forgot email"):
        return Screen("email", field=field, tap=find(nodes, "next", exact=True))
    if field and (field.get("password") == "true" or has("enter your password", "show password")):
        return Screen("password", tap=another, field=field, error=has(*WRONG_PASSWORD))
    if has(*AGREE):
        return Screen("agree", tap=next((n for n in reversed(nodes) if label(n) in AGREE_BUTTON), None))
    if field and has(*RECOVERY_PHONE):
        masked = next((label(n) for n in nodes if re.fullmatch(r"[•*·.\s()+-]*[•*·][•*·.\s()+-]*\d{2,4}", label(n))), "")
        return Screen("recovery_phone", field=field, tap=find(nodes, "next", exact=True),
                      hint=(re.findall(r"\d+", masked) or [None])[-1], error=has(*WRONG))
    if has(*RECOVERY):
        return Screen("recovery")
    if another:  # a code, a passkey, a security key: there may be a prompt among the other ways
        return Screen("other", tap=another)
    if has(*CHECK):
        return Screen("check")
    if has("add an account", "add account") and (google := find(nodes, "google", exact=True)):
        return Screen("pick", tap=google)
    if not blob or has(*BUSY_TEXT):
        return Screen("busy")
    return Screen("unknown")


class SignIn:
    """One sign-in's state, as the window shows it. `method`: "prompt", the "Tap Yes" prompt on the user's own phone
    (Relay looks for it past the password, and asks for the password when Google offers none); "password", the
    user's password straight away."""

    def __init__(self, email, method="prompt"):
        self.email = email
        self.method = method
        self.state = "working"
        self.message = "Opening Google sign-in on the phone…"
        self.number = None
        self.account = None
        self.hint = None
        self.task = None
        # The phone number the user gave for Google's account recovery: typed once, then forgotten.
        self.answer = None
        # The password the user gave (state "need_password"): typed once, then forgotten. Never in view().
        self.password = None
        # The user said yes to Google's terms (state "agree"): Relay taps its button once.
        self.agreed = False

    def update(self, state, message, number=None, account=None, hint=None):
        self.state, self.message, self.number, self.account, self.hint = state, message, number, account, hint
        watch.notify()  # the window follows the sign-in on the gateway's device list (gateway/devices.py)
        if state in ENDED:
            for listener in ended:
                listener()

    def view(self):
        return {"state": self.state, "message": self.message, "number": self.number, "account": self.account,
                "email": self.email, "hint": self.hint, "method": self.method}


jobs: dict[str, SignIn] = {}
# A sign-in that has ended may have changed the phone's accounts: told then (gateway/devices.py reads them again).
ENDED = ("done", "failed", "manual", "stopped")
ended = []

# The clock and pause, swapped in tests.
now = time.monotonic


async def pause(seconds):
    await asyncio.sleep(seconds)


async def accounts(slot):
    with contextlib.suppress(HTTPException):
        return google_accounts((await core.adb(slot, "shell", "dumpsys account", timeout=10)).decode(errors="replace"))
    return []


async def read(slot):
    """The screen's nodes, or [] when uiautomator cannot read it (an animation, a busy phone). The old dump goes
    first, so a failed read is never the last screen again."""
    with contextlib.suppress(HTTPException):
        xml = await core.adb(slot, "shell", f"rm -f {DUMP}; uiautomator dump {DUMP} >/dev/null 2>&1; "
                                            f"cat {DUMP} 2>/dev/null; rm -f {DUMP}; true", timeout=45)
        return ui_nodes(xml.decode(errors="replace"))
    return []


async def tap(slot, node):
    x, y = node["center"]
    async with core.locks[slot]:
        await core.shell(slot, "input", "tap", x, y)


async def enter(slot, screen, text):
    """Into the screen's field: tap it, clear what is there, type, and Next (or Enter)."""
    await tap(slot, screen.field)
    existing = screen.field.get("text") or ""
    async with core.locks[slot]:
        if norm(existing) not in PLACEHOLDERS:
            await core.shell(slot, "input", "keyevent", "123", *["67"] * (len(existing) + 2))  # end, then delete
        await core.shell(slot, "input", "text", text)
    if screen.tap:
        await tap(slot, screen.tap)
    else:
        async with core.locks[slot]:
            await core.shell(slot, "input", "keyevent", "66")


async def enter_password(slot, screen, password):
    """The user's password into the password field, as a private code (redacted from the phone's output), then Next."""
    from .actions import type_text  # actions imports devices, which imports this module

    await tap(slot, screen.field)
    async with core.locks[slot]:
        await type_text(slot, password, sensitive=True)
        await core.shell(slot, "input", "keyevent", "66")


async def open_signin(slot):
    """Android's Add account screen, for Google only: Settings goes straight on to Google's sign-in."""
    async with core.locks[slot]:
        await core.shell(slot, "am", "start", "-a", "android.settings.ADD_ACCOUNT_SETTINGS",
                         "--esa", "account_types", "com.google")


async def drive(slot, job):
    """Walk Google's sign-in screens until the phone has an account, the user has to take over, or it fails."""
    started = now()
    if found := await accounts(slot):
        return job.update("done", f"Signed in as {found[0]}.", account=found[0])
    await open_signin(slot)
    last, since = None, now()
    acted = {}  # screen kind -> when Relay last acted on it
    counts = {}  # screen kind -> how many times Relay acted on it
    closed = 0

    def ready(kind, most):
        """Whether Relay may act on this screen: the first time at once, again only once it has not moved on."""
        return counts.get(kind, 0) < most and now() - acted.get(kind, -AGAIN) >= AGAIN

    def did(kind):
        acted[kind] = now()
        counts[kind] = counts.get(kind, 0) + 1

    while True:
        if found := await accounts(slot):
            return job.update("done", f"Signed in as {found[0]}.", account=found[0])
        if now() - started > LIMIT:
            return job.update("manual", HAND_OVER["timeout"])
        nodes = await read(slot)
        if nodes and not any(n.get("package", "").startswith(SIGNIN_PACKAGES) for n in nodes):
            closed += 1
            if closed >= 3:  # three reads in a row, not one passing screen
                return job.update("failed", "The sign-in closed before you were signed in. Try again.")
        else:
            closed = 0
        screen = read_screen(nodes)
        if screen.kind != last:
            last, since = screen.kind, now()
        waited = now() - since

        if screen.kind == "captcha":
            return job.update("manual", HAND_OVER["captcha"])
        if screen.kind in ("check", "recovery"):
            return job.update("manual", HAND_OVER[screen.kind])
        if screen.kind == "not_found":
            return job.update("failed", "Google couldn’t find an account with that email. Check it and try again.")
        if screen.kind == "refused":
            return job.update("failed", "Google didn’t sign you in this way. Try again, or sign in on the phone "
                                        "yourself with your password.")
        if screen.kind == "email":
            if norm(screen.field.get("text")) == job.email.lower():
                if ready("next", 3):  # typed, but still here: Next again
                    did("next")
                    if screen.tap:
                        await tap(slot, screen.tap)
                elif waited > STUCK:
                    return job.update("manual", HAND_OVER["email"])
            elif ready("email", 2):
                did("email")
                did("next")  # enter_email taps Next too: the next one waits until it has not taken
                job.update("working", "Entering your email…")
                await enter(slot, screen, job.email)
            elif waited > STUCK:
                return job.update("manual", HAND_OVER["email"])
        elif screen.kind == "new_password":
            return job.update("manual", HAND_OVER["new_password"])
        elif screen.kind == "password" and (job.password or job.method == "password" or counts.get("password")
                                            or not screen.tap):
            # The password: the user chose it, or Google offers no prompt here (Relay looked already, or there is
            # no other way to look). Typed once when the user gives it, then forgotten.
            typed = counts.get("typed", 0)
            if job.password:
                password, job.password = job.password, None
                did("typed")
                job.update("working", "Signing you in…")
                try:
                    await enter_password(slot, screen, password)
                except HTTPException as exc:
                    job.update("need_password", f"{exc.detail} Enter your password again.")
                del password
            elif job.state == "need_password":
                pass  # waiting on the user, for as long as the whole sign-in may take
            elif typed and now() - acted["typed"] < AGAIN:
                pass  # just typed: Google is checking it
            elif typed >= PASSWORD_TRIES:
                return job.update("manual", HAND_OVER["wrong_password"])
            else:
                job.update("need_password", "That password didn’t work. Enter it again." if typed else
                           f"Enter the password for {job.email}. penrose types it on the phone once and keeps it "
                           "nowhere; no AI model sees it.")
        elif job.method == "password" and screen.kind in ("other", "choose", "recovery_phone"):
            # Past the password, Google wants another step that is not the prompt: the user's to finish.
            return job.update("manual", HAND_OVER["check"])
        elif screen.kind == "choose" and not screen.tap:
            # No prompt in the list: the password, never the list's own "Try another way" (account recovery, which
            # ends in Google insisting on a new password).
            if screen.password and ready("choose_password", 1):
                did("choose_password")
                did("password")  # the password screen next: ask for it rather than look for a prompt again
                await tap(slot, screen.password)
            elif not screen.password or waited > STUCK:
                return job.update("manual", HAND_OVER["no_prompt"])
        elif screen.kind == "recovery_phone":
            ending = f", ending in {screen.hint}" if screen.hint else ""
            if job.answer:
                answer, job.answer = job.answer, None
                did("recovery_phone")
                job.update("working", "Entering your phone number…")
                await enter(slot, screen, answer)
            elif job.state == "need_phone":
                pass  # waiting on the user, for as long as the whole sign-in may take
            elif counts.get("recovery_phone") and now() - acted["recovery_phone"] < AGAIN:
                pass  # just typed: Google is checking it
            elif counts.get("recovery_phone", 0) >= 3:
                return job.update("manual", HAND_OVER["recovery"])
            else:
                again = counts.get("recovery_phone") or screen.error
                job.update("need_phone", (f"That number didn’t match. Enter the phone number on your Google account"
                                          f"{ending} again." if again else
                                          f"Google wants to confirm the phone number on your Google account{ending}. "
                                          f"Enter it here and penrose will type it on the phone."), hint=screen.hint)
        elif screen.kind in ("password", "other", "choose"):
            # Never the password: the other ways to sign in, and among them the prompt on the user's phone.
            if not screen.tap:
                return job.update("manual", HAND_OVER["no_prompt"])
            if ready(screen.kind, 2 if screen.kind == "choose" else 3):
                did(screen.kind)
                job.update("working", "Asking Google to send a prompt to your phone…")
                await tap(slot, screen.tap)
            elif waited > STUCK:
                return job.update("manual", HAND_OVER["no_prompt"])
        elif screen.kind == "prompt":
            job.update("phone", "Check your phone and tap Yes.", number=screen.number)
        elif screen.kind == "agree":
            # Google's terms: the user's to accept. Relay taps the button only once they said yes, which holds for
            # the rest of this sign-in (Google may show its terms and then its services).
            if job.agreed and screen.tap and ready("agree", 3):
                did("agree")
                job.update("working", "Signing you in…")
                await tap(slot, screen.tap)
            elif job.state != "agree" and not (counts.get("agree") and now() - acted["agree"] < AGAIN):
                job.update("agree", "Almost done: Google asks you to agree to its Terms of Service and Privacy "
                                    "Policy.")
        elif screen.kind in ("pick", "skip"):
            if screen.tap and ready(screen.kind, 2):
                did(screen.kind)
                await tap(slot, screen.tap)
            elif waited > STUCK:
                return job.update("manual", HAND_OVER["unknown"])
        elif screen.kind == "busy":
            if job.state in ("phone", "agree"):  # the user answered: Google is finishing
                job.update("working", "Signing you in…")
            if waited > BUSY:
                return job.update("manual", HAND_OVER["slow"])
        elif waited > STUCK:  # unknown
            return job.update("manual", HAND_OVER["unknown"])
        await pause(STEP)


async def run(slot, job):
    try:
        async with sleep.held(slot):  # it holds no lease: the phone stays awake while it is driven
            await drive(slot, job)
    except asyncio.CancelledError:
        raise
    except Exception:
        job.update("manual", HAND_OVER["error"])


class SignInRequest(BaseModel):
    email: str = Field(min_length=1, max_length=254)
    method: typing.Literal["prompt", "password"] = "prompt"

    @field_validator("email")
    @classmethod
    def plain_email(cls, value):
        value = value.strip()
        if not EMAIL.fullmatch(value):
            raise ValueError("Enter your Google email address, like name@gmail.com.")
        return value


@router.post("")
async def start(slot: str, body: SignInRequest):
    """Start (or start over) signing in to Google with `email`. Refused while an agent holds the phone."""
    core.require_slot(slot)
    rows = core.store.rows("SELECT owner FROM leases WHERE slot=?", (slot,))
    if rows and not rows[0]["owner"].startswith("manual:"):
        raise HTTPException(409, "An agent is using the phone. Try again once it’s done.")
    await stop(slot)
    job = jobs[slot] = SignIn(body.email, body.method)
    job.task = asyncio.create_task(run(slot, job))
    return job.view()


class PhoneAnswer(BaseModel):
    phone: str = Field(min_length=1, max_length=40)

    @field_validator("phone")
    @classmethod
    def plain_phone(cls, value):
        digits = re.sub(r"[\s().-]", "", value)
        if not re.fullmatch(r"\+?\d{6,15}", digits):
            raise ValueError("Enter the phone number with digits only, like +1 555 010 0149.")
        return digits


@router.post("/phone")
async def answer_phone(slot: str, body: PhoneAnswer):
    """The phone number Google asked for (state "need_phone"): Relay types it on the phone once, and keeps it
    nowhere else."""
    core.require_slot(slot)
    job = jobs.get(slot)
    if not job or job.state != "need_phone" or not job.task or job.task.done():
        raise HTTPException(409, "Google isn’t asking for a phone number right now.")
    job.answer = body.phone
    job.update("working", "Entering your phone number…")
    return job.view()


class PasswordAnswer(BaseModel):
    password: str = Field(min_length=1, max_length=200)


@router.post("/password")
async def answer_password(slot: str, body: PasswordAnswer):
    """The user's password (state "need_password"): Relay types it on the phone once, as a private code, and keeps it
    nowhere else."""
    core.require_slot(slot)
    job = jobs.get(slot)
    if not job or job.state != "need_password" or not job.task or job.task.done():
        raise HTTPException(409, "Google isn’t asking for a password right now.")
    job.password = body.password
    job.update("working", "Entering your password…")
    return job.view()


@router.post("/agree")
async def agree(slot: str):
    """The user agrees to Google's terms (state "agree"): Relay taps Google's own button."""
    core.require_slot(slot)
    job = jobs.get(slot)
    if not job or job.state != "agree" or not job.task or job.task.done():
        raise HTTPException(409, "Google isn’t asking you to agree to anything right now.")
    job.agreed = True
    job.update("working", "Signing you in…")
    return job.view()


@router.get("")
async def status(slot: str):
    core.require_slot(slot)
    job = jobs.get(slot)
    return job.view() if job else {"state": "idle", "message": "", "number": None, "account": None, "email": None,
                                   "hint": None, "method": None}


@router.delete("")
async def stop(slot: str):
    """Stop driving the sign-in; what is on the phone stays there for the user."""
    core.require_slot(slot)
    job = jobs.get(slot)
    if job and job.task and not job.task.done():
        job.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await job.task
        job.update("stopped", "")
    return await status(slot)
