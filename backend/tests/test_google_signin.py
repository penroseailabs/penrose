"""The guided Google sign-in (gateway/signin.py): the user gives only their email, Relay types it on the phone and
asks Google for the prompt on the user's own phone; a CAPTCHA, a check or an account without the prompt goes back
to the user. adb, the screens Google shows and the clock are faked."""

import asyncio
import contextlib
import html

import httpx
import pytest
from app.gateway import core, signin
from app.store import Store

GMS = "com.google.android.gms"


def node(i, text="", cls="android.widget.TextView", package=GMS, password=False, desc=""):
    top = 100 + i * 100
    return (f'<node index="{i}" text="{html.escape(text)}" resource-id="" class="{cls}" package="{package}" '
            f'content-desc="{html.escape(desc)}" password="{str(password).lower()}" '
            f'bounds="[0,{top}][1000,{top + 80}]" />')


def center(i):
    return 500, 100 + i * 100 + 40


# Each screen: its texts, top to bottom. ("field", value) is an EditText; ("password", "") a password field.
# The texts of "ease", "email", "password", "methods", "recovery" and "couldnt" are as an Android 15 emulator showed
# them (Google's own buttons are native and upper case).
SCREENS = {
    "ease": ["Sign in - Google Accounts", "Sign in with ease", "We can search for accounts connected to this phone "
             "number by obtaining your number from your carrier and exchanging device info, such as your SIM card "
             "identifier (standard message and data rates may apply).", "SKIP", "NEXT"],
    "email": ["Sign in", "Use your Google Account. The account will be added to this device and available to other "
              "Google apps.", "Learn more about using your account", ("field", ""), "Forgot email?",
              "Create account", "NEXT"],
    "password": ["Welcome", "name@gmail.com", ("password", ""), "Show password", "TRY ANOTHER WAY", "NEXT"],
    "methods": ["Welcome", "name@gmail.com", "Choose how you want to sign in:", "Enter your password",
                "Use your passkey", "Try another way"],
    "recovery": ["Account recovery", "To help keep your account safe, Google wants to make sure it’s really you "
                 "trying to sign in", "name@gmail.com", "Confirm the phone number you provided in your security "
                 "settings: ", "•••••• •••49", "Phone number", ("field", ""), "TRY ANOTHER WAY", "NEXT"],
    "recovery_wrong": ["Account recovery", "Confirm the phone number you provided in your security settings: ",
                       "•••••• •••49", "Wrong number. Try again.", "Phone number", ("field", ""), "TRY ANOTHER WAY",
                       "NEXT"],
    "recovery_email": ["Account recovery", "Confirm the recovery email address you added to your account: ",
                       "j•••@example.com", ("field", ""), "TRY ANOTHER WAY", "NEXT"],
    "code": ["2-Step Verification", "Enter the code we sent", ("field", ""), "TRY ANOTHER WAY", "NEXT"],
    "couldnt": ["Couldn’t sign you in", "name@gmail.com", "You didn’t provide enough info for Google to be sure this "
                "account is really yours.", "TRY AGAIN"],
    "choose": ["Choose how you want to sign in:", "Tap Yes on your phone or tablet",
               "Get a verification code at (•••) •••-••12", "Enter your password"],
    "choose_without_prompt": ["Choose how you want to sign in:", "Get a verification code at (•••) •••-••12",
                              "Enter your password"],
    "prompt": ["Check your Pixel 8", "Google sent a notification to your Pixel 8. Tap Yes on the notification, "
               "then tap the number shown below on your phone to verify it’s you.", "37", "Resend it"],
    "checking": ["Checking info…"],
    "agree": ["Google Terms of Service", "I agree"],
    "wrong_password": ["Welcome", "name@gmail.com", ("password", ""), "Wrong password. Try again or click Forgot "
                       "password to reset it.", "Show password", "TRY ANOTHER WAY", "NEXT"],
    # As a Galaxy owner's account showed them on the emulator, after account recovery.
    "galaxy_prompt": ["Account recovery", "name@gmail.com", "64", "Check your Galaxy S25", "Pull down the "
                      "notification bar and tap the sign-in notification. Tap ", "Yes", ", then tap ", "64",
                      " on your device to verify it’s you.", "Resend it", "I DON’T HAVE MY PHONE"],
    "who": ["Sign in - Google Accounts", "Who will be using this device?", "Set up this device for yourself or a "
            "member of your Google family group", "I will be using this device", "My child will be using this "
            "device", "NEXT"],
    "new_password": ["Sign in - Google Accounts", "Change password", "name@gmail.com", "Create a strong password",
                     "Create a new, strong password that you don’t use for other websites", ("password", ""),
                     "Show password", "NEXT"],
    "captcha": ["Verify it’s you", "Type the text you hear or see", ("field", ""), "Next"],
    "not_found": ["Sign in", "Couldn’t find your Google Account", ("field", "nobody@gmail.com"), "Next"],
    "mystery": ["Something brand new from Google"],
    "launcher": ["Search your phone"],
}


class GooglePhone:
    """Answers adb like a phone showing Google's sign-in. `flow` maps (screen, tapped text) to the next screen;
    `after_reads` moves on by itself after that many reads of a screen (the user acting on their own phone)."""

    def __init__(self, flow, start="email", after_reads=None, accounts=()):
        self.flow = flow
        self.screen = start
        self.after_reads = after_reads or {}
        self.reads = 0
        self.typed = ""
        self.accounts = list(accounts)
        self.commands = []
        self.taps = []
        self.views = []  # (screen, state of the sign-in) as Relay read each screen

    def nodes(self):
        out = []
        for i, item in enumerate(SCREENS[self.screen]):
            if isinstance(item, tuple):
                kind, value = item
                out.append(node(i, self.typed or value, cls="android.widget.EditText", password=kind == "password"))
            else:
                out.append(node(i, item, package="app.relay.home" if self.screen == "launcher" else GMS))
        return out

    def label_at(self, x, y):
        for i, item in enumerate(SCREENS[self.screen]):
            if center(i) == (x, y):
                return item if isinstance(item, str) else item[0]
        return None

    async def adb(self, slot, *args, timeout=25, include_stderr=False):
        assert args[0] == "shell"
        command = args[1]
        self.commands.append(command)
        if command == "dumpsys account":
            return "".join(f"  Account {{name={a}, type=com.google}}\n" for a in self.accounts).encode()
        if "uiautomator dump" in command:
            self.reads += 1
            xml = "<hierarchy>" + "".join(self.nodes()) + "</hierarchy>"
            moves = self.after_reads.get(self.screen)
            if moves and self.reads >= moves[0]:
                self.screen, self.reads = moves[1], 0
                if moves[1] == "done":
                    self.accounts.append("name@gmail.com")
                    self.screen = "launcher"
            return xml.encode()
        if command.startswith("input tap"):
            x, y = map(int, command.split()[2:])
            tapped = self.label_at(x, y)
            self.taps.append(tapped)
            if (self.screen, tapped) in self.flow:
                target = self.flow[(self.screen, tapped)]
                self.screen = target(self) if callable(target) else target
                self.reads, self.typed = 0, ""
        elif command.startswith("input text"):
            self.typed += command.split(" ", 2)[2].strip("'")
        elif command == "input keyevent 66" and (self.screen, "ENTER") in self.flow:
            target = self.flow[(self.screen, "ENTER")]
            self.screen = target(self) if callable(target) else target
            self.reads, self.typed = 0, ""
        return b""


@pytest.fixture
def clock(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(signin, "now", lambda: now[0])

    async def pause(seconds):
        now[0] += seconds
        await asyncio.sleep(0)

    monkeypatch.setattr(signin, "pause", pause)
    return now


def install(monkeypatch, phone):
    monkeypatch.setattr(core, "slots", {"device-1": "emulator-5570"})
    monkeypatch.setattr(core, "locks", {"device-1": asyncio.Lock()})
    monkeypatch.setattr(core, "adb", phone.adb)
    return phone


FULL_FLOW = {
    ("ease", "SKIP"): "email",
    ("email", "NEXT"): "password",
    ("password", "TRY ANOTHER WAY"): "choose",
    ("choose", "Tap Yes on your phone or tablet"): "prompt",
}


async def drive(phone, email="name@gmail.com", method="prompt"):
    job = signin.SignIn(email, method)
    original = job.update

    def update(state, message, **kw):
        original(state, message, **kw)
        phone.views.append((phone.screen, state, kw.get("number")))

    job.update = update
    await signin.drive("device-1", job)
    return job


async def test_email_then_try_another_way_then_the_prompt_signs_in_without_a_password(monkeypatch, clock):
    phone = install(monkeypatch, GooglePhone(FULL_FLOW, start="ease", after_reads={"prompt": (3, "checking"),
                                                                     "checking": (2, "agree"),
                                                                     "agree": (2, "done")}))
    job = await drive(phone)
    assert job.state == "done" and job.account == "name@gmail.com"
    assert any(c.startswith("am start -a android.settings.ADD_ACCOUNT_SETTINGS") for c in phone.commands)
    # Only the email is ever typed, once: never a password.
    assert [c for c in phone.commands if c.startswith("input text")] == ["input text name@gmail.com"]
    # Skip, not Next, on the offer to look the account up through the carrier.
    assert phone.taps == ["SKIP", "field", "NEXT", "TRY ANOTHER WAY", "Tap Yes on your phone or tablet"]
    # The window showed "tap Yes" with Google's number, then Google's terms for the user to agree to.
    states = [state for _, state, _ in phone.views]
    assert ("prompt", "phone", "37") in phone.views
    assert states.index("phone") < states.index("agree") < states.index("done")
    assert "I agree" not in phone.taps  # the terms are the user's to accept


async def test_google_can_go_straight_to_the_prompt_after_the_email(monkeypatch, clock):
    phone = install(monkeypatch, GooglePhone({("email", "NEXT"): "prompt"}, after_reads={"prompt": (2, "done")}))
    job = await drive(phone)
    assert job.state == "done"
    assert "Try another way" not in phone.taps


def password_ok(phone):
    return "prompt" if phone.typed == "correct%shorse" else "wrong_password"  # adb types a space as %s


async def answering_password(phone, *answers, method="prompt", agree=False):
    """Run the sign-in, giving `answers` in turn each time Relay asks for the password (and agreeing to Google's
    terms when asked, if `agree`)."""
    job = signin.SignIn("name@gmail.com", method)
    task = asyncio.create_task(signin.drive("device-1", job))
    asked = []
    answers = list(answers)
    while not task.done():
        await asyncio.sleep(0)
        if job.state == "need_password" and job.password is None:
            asked.append(job.message)
            if not answers:
                task.cancel()
                break
            job.password = answers.pop(0)
            job.update("working", "Entering your password…")
        elif job.state == "agree" and agree and not job.agreed:
            job.agreed = True
            job.update("working", "Signing you in…")
    with contextlib.suppress(asyncio.CancelledError):
        await task
    return job, asked


async def test_no_prompt_in_the_list_asks_for_the_password_never_account_recovery(monkeypatch, clock):
    """As the live phone showed it: password, passkey and "Try another way" in the list. Relay picks the password,
    never "Try another way" (account recovery, which ended in Google insisting on a new password), asks the user for
    it, types it once, and the prompt follows."""
    phone = install(monkeypatch, GooglePhone({
        ("email", "NEXT"): "methods", ("methods", "Enter your password"): "password", ("password", "ENTER"): password_ok,
    }, after_reads={"prompt": (2, "done")}))
    job, asked = await answering_password(phone, "correct horse")
    assert job.state == "done"
    assert len(asked) == 1 and "name@gmail.com" in asked[0]
    assert "Try another way" not in phone.taps and "TRY ANOTHER WAY" not in phone.taps
    assert phone.taps == ["field", "NEXT", "Enter your password", "password"]
    typed = [c for c in phone.commands if c.startswith("input text")]
    assert typed == ["input text name@gmail.com", "input text correct%shorse"]
    assert job.password is None  # typed once, kept nowhere
    assert "correct horse" in core.redactions["device-1"]  # and kept out of the phone's output, like a code


async def test_a_password_screen_with_no_prompt_behind_it_asks_for_the_password(monkeypatch, clock):
    phone = install(monkeypatch, GooglePhone({
        ("email", "NEXT"): "password", ("password", "TRY ANOTHER WAY"): "choose_without_prompt",
        ("choose_without_prompt", "Enter your password"): "password", ("password", "ENTER"): password_ok,
    }, after_reads={"prompt": (1, "done")}))
    job, asked = await answering_password(phone, "correct horse")
    assert job.state == "done" and len(asked) == 1
    assert phone.taps.count("TRY ANOTHER WAY") == 1  # looked for the prompt once, then asked


async def test_a_wrong_password_is_asked_for_again(monkeypatch, clock):
    phone = install(monkeypatch, GooglePhone({
        ("email", "NEXT"): "methods", ("methods", "Enter your password"): "password",
        ("password", "ENTER"): password_ok, ("wrong_password", "ENTER"): password_ok,
    }, after_reads={"prompt": (1, "done")}))
    job, asked = await answering_password(phone, "nope", "correct horse")
    assert job.state == "done"
    assert len(asked) == 2 and "didn’t work" in asked[1]


async def test_the_password_method_asks_at_once_and_a_second_step_still_shows(monkeypatch, clock):
    phone = install(monkeypatch, GooglePhone({("email", "NEXT"): "password", ("password", "ENTER"): password_ok},
                                             after_reads={"prompt": (2, "done")}))
    job, asked = await answering_password(phone, "correct horse", method="password")
    assert job.state == "done" and len(asked) == 1
    assert "TRY ANOTHER WAY" not in phone.taps


async def test_a_new_password_is_never_set(monkeypatch, clock):
    phone = install(monkeypatch, GooglePhone({}, start="new_password"))
    job = await drive(phone)
    assert job.state == "manual" and "won’t change it" in job.message
    assert phone.taps == [] and not [c for c in phone.commands if c.startswith("input text")]


async def test_google_terms_wait_for_the_user_then_relay_agrees(monkeypatch, clock):
    flow = {("agree", "I agree"): "launcher"}
    phone = install(monkeypatch, GooglePhone(flow, start="agree"))
    job, _ = await answering_password(phone)  # nobody agrees: Relay waits and taps nothing
    assert "I agree" not in phone.taps
    phone = install(monkeypatch, GooglePhone(flow, start="agree", accounts=()))
    phone.flow[("agree", "I agree")] = lambda p: p.accounts.append("name@gmail.com") or "launcher"
    job, _ = await answering_password(phone, agree=True)
    assert job.state == "done" and phone.taps == ["I agree"]


async def test_a_galaxy_prompt_and_who_uses_the_device_are_followed(monkeypatch, clock):
    phone = install(monkeypatch, GooglePhone({("who", "NEXT"): "launcher"}, start="galaxy_prompt",
                                             after_reads={"galaxy_prompt": (2, "who")}))
    phone.flow[("who", "NEXT")] = lambda p: p.accounts.append("name@gmail.com") or "launcher"
    job = await drive(phone)
    assert job.state == "done" and ("galaxy_prompt", "phone", "64") in phone.views
    assert phone.taps == ["NEXT"]


def recovered(phone):
    return "prompt" if phone.typed == "+15550100149" else "recovery_wrong"


# Account recovery when Google goes there by itself: Relay asks for the account's phone number and types it.
RECOVERY_FLOW = {("recovery", "NEXT"): recovered, ("recovery_wrong", "NEXT"): recovered}


async def answering(phone, *answers):
    """Run the sign-in, giving `answers` in turn each time Relay's window asks for the phone number."""
    job = signin.SignIn("name@gmail.com")
    task = asyncio.create_task(signin.drive("device-1", job))
    asked = []
    answers = list(answers)
    while not task.done():
        await asyncio.sleep(0)
        if job.state == "need_phone" and job.answer is None:
            asked.append((job.message, job.hint))
            if not answers:
                task.cancel()
                break
            job.answer = answers.pop(0)
            job.update("working", "Entering your phone number…")
    with contextlib.suppress(asyncio.CancelledError):
        await task
    return job, asked


async def test_googles_own_recovery_asks_for_the_accounts_phone_number(monkeypatch, clock):
    phone = install(monkeypatch, GooglePhone(RECOVERY_FLOW, start="recovery", after_reads={"prompt": (2, "done")}))
    job, asked = await answering(phone, "+15550100149")
    assert job.state == "done"
    assert len(asked) == 1 and "ending in 49" in asked[0][0] and asked[0][1] == "49"
    assert phone.taps == ["field", "NEXT"]
    typed = [c for c in phone.commands if c.startswith("input text")]
    assert typed == ["input text +15550100149"]
    assert job.answer is None  # typed once, kept nowhere


async def test_a_wrong_phone_number_is_asked_for_again(monkeypatch, clock):
    phone = install(monkeypatch, GooglePhone(RECOVERY_FLOW, start="recovery", after_reads={"prompt": (1, "done")}))
    job, asked = await answering(phone, "+15550100000", "+15550100149")
    assert job.state == "done"
    assert len(asked) == 2 and "didn’t match" in asked[1][0]


async def test_relay_never_taps_try_another_way_in_account_recovery(monkeypatch, clock):
    phone = install(monkeypatch, GooglePhone({("recovery", "TRY ANOTHER WAY"): "couldnt"}, start="recovery"))
    job, asked = await answering(phone)  # the user does not answer
    assert asked and "TRY ANOTHER WAY" not in phone.taps and phone.screen == "recovery"


async def test_other_recovery_questions_are_left_to_the_user(monkeypatch, clock):
    phone = install(monkeypatch, GooglePhone({}, start="recovery_email"))
    job = await drive(phone)
    assert job.state == "manual" and "confirm your account" in job.message
    assert phone.taps == []


async def test_couldnt_sign_you_in_offers_another_try(monkeypatch, clock):
    phone = install(monkeypatch, GooglePhone({}, start="couldnt"))
    job = await drive(phone)
    assert job.state == "failed" and "Try again" in job.message


async def test_a_captcha_stops_relay_at_once(monkeypatch, clock):
    phone = install(monkeypatch, GooglePhone({("email", "NEXT"): "captcha"}))
    job = await drive(phone)
    assert job.state == "manual" and "person" in job.message
    assert phone.taps[-1] == "NEXT"  # nothing on the CAPTCHA screen itself
    assert len([c for c in phone.commands if c.startswith("input text")]) == 1


async def test_an_unknown_email_asks_the_user_to_check_it(monkeypatch, clock):
    phone = install(monkeypatch, GooglePhone({("email", "NEXT"): "not_found"}))
    job = await drive(phone, email="nobody@gmail.com")
    assert job.state == "failed" and "couldn’t find" in job.message


async def test_a_screen_relay_does_not_know_is_handed_over_after_a_while(monkeypatch, clock):
    phone = install(monkeypatch, GooglePhone({("email", "NEXT"): "mystery"}))
    job = await drive(phone)
    assert job.state == "manual" and "didn’t recognise" in job.message
    assert clock[0] > signin.STUCK


async def test_closing_the_sign_in_ends_it(monkeypatch, clock):
    phone = install(monkeypatch, GooglePhone({("email", "NEXT"): "launcher"}))
    job = await drive(phone)
    assert job.state == "failed" and "closed" in job.message


async def test_a_phone_already_signed_in_is_done_without_opening_anything(monkeypatch, clock):
    phone = install(monkeypatch, GooglePhone({}, accounts=["name@gmail.com"]))
    job = await drive(phone)
    assert job.state == "done" and job.account == "name@gmail.com"
    assert phone.commands == ["dumpsys account"]


async def test_an_email_already_typed_is_not_typed_again(monkeypatch, clock):
    """A slow phone: Next does not take at first; Relay taps Next again rather than typing the email twice."""
    phone = GooglePhone({}, after_reads={})
    install(monkeypatch, phone)
    flow_after = {"n": 0}
    original = phone.adb

    async def adb(slot, *args, **kw):
        out = await original(slot, *args, **kw)
        if args[1].startswith("input tap") and phone.taps[-1] == "NEXT":
            flow_after["n"] += 1
            if flow_after["n"] == 2:
                phone.screen, phone.reads = "prompt", 0
                phone.after_reads = {"prompt": (1, "done")}
        return out

    monkeypatch.setattr(core, "adb", adb)
    job = await drive(phone)
    assert job.state == "done"
    assert phone.taps.count("NEXT") == 2
    assert len([c for c in phone.commands if c.startswith("input text")]) == 1


@pytest.mark.parametrize("texts,kind,number", [
    (["Check your phone", "Tap Yes on the notification, then tap 52 on your phone"], "prompt", "52"),
    (["Google sent a notification to your Pixel. Tap Yes on the notification to verify it's you."], "prompt", None),
    (["Couldn't find your Google Account"], "not_found", None),
    (["Choose how you want to sign in:", "Get a notification on your Pixel"], "choose", None),
    (["Verify it’s you", "To help keep your account safe, Google wants to make sure it’s really you"],
     "check", None),
    ([], "busy", None),
])
def test_read_screen(texts, kind, number):
    from app.android import ui_nodes

    nodes = ui_nodes("".join(node(i, t) for i, t in enumerate(texts)))
    screen = signin.read_screen(nodes)
    assert (screen.kind, screen.number) == (kind, number)


def test_ui_nodes_unescapes_text():
    from app.android import ui_nodes

    [found] = ui_nodes(node(0, "Terms & Privacy"))
    assert found["text"] == "Terms & Privacy" and found["center"] == center(0)


@pytest.fixture
def gateway_client(tmp_path, monkeypatch):
    from app import gateway

    leases = Store(tmp_path)
    monkeypatch.setattr(core, "store", leases)
    monkeypatch.setattr(signin, "jobs", {})
    client = httpx.AsyncClient(base_url="http://gateway", transport=httpx.ASGITransport(app=gateway.app))
    return client, leases


async def test_the_endpoint_starts_follows_and_stops_a_sign_in(gateway_client, monkeypatch, clock):
    client, _ = gateway_client
    monkeypatch.setattr(signin, "LIMIT", 10**9)  # the user has not answered yet, however long the test takes
    phone = install(monkeypatch, GooglePhone({("email", "NEXT"): "prompt"}))
    async with client:
        assert (await client.get("/devices/device-1/google/signin")).json()["state"] == "idle"
        started = await client.post("/devices/device-1/google/signin", json={"email": " name@gmail.com "})
        assert started.status_code == 200 and started.json()["email"] == "name@gmail.com"
        for _ in range(50):
            await asyncio.sleep(0)
            if signin.jobs["device-1"].state == "phone":
                break
        view = (await client.get("/devices/device-1/google/signin")).json()
        assert view["state"] == "phone" and view["message"] == "Check your phone and tap Yes."
        stopped = await client.delete("/devices/device-1/google/signin")
        assert stopped.json()["state"] == "stopped"
    assert phone.screen == "prompt"  # stopping leaves the phone as it is


async def test_the_phone_number_is_taken_only_when_google_asks_for_it(gateway_client, monkeypatch):
    client, _ = gateway_client
    install(monkeypatch, GooglePhone({}))
    async with client:
        not_asked = await client.post("/devices/device-1/google/signin/phone", json={"phone": "+1 555 010 0149"})
        assert not_asked.status_code == 409
        job = signin.jobs["device-1"] = signin.SignIn("name@gmail.com")
        job.task = asyncio.create_task(asyncio.sleep(3600))
        job.update("need_phone", "Google wants…", hint="49")
        bad = await client.post("/devices/device-1/google/signin/phone", json={"phone": "call me"})
        assert bad.status_code == 422
        ok = await client.post("/devices/device-1/google/signin/phone", json={"phone": "+1 (555) 010-0149"})
        assert ok.status_code == 200 and ok.json()["state"] == "working" and "phone" not in ok.json()
        assert job.answer == "+15550100149"
        job.task.cancel()


async def test_the_password_route_hands_a_second_step_to_the_user(monkeypatch, clock):
    phone = install(monkeypatch, GooglePhone({("email", "NEXT"): "password", ("password", "ENTER"): "code"}))
    job, _ = await answering_password(phone, "correct horse", method="password")
    assert job.state == "manual" and "another way" in job.message
    assert "TRY ANOTHER WAY" not in phone.taps


async def test_the_password_and_agreement_are_taken_only_when_google_asks(gateway_client, monkeypatch):
    client, _ = gateway_client
    install(monkeypatch, GooglePhone({}))
    async with client:
        assert (await client.post("/devices/device-1/google/signin/password", json={"password": "x"})).status_code == 409
        assert (await client.post("/devices/device-1/google/signin/agree")).status_code == 409
        job = signin.jobs["device-1"] = signin.SignIn("name@gmail.com")
        job.task = asyncio.create_task(asyncio.sleep(3600))
        job.update("need_password", "Enter the password…")
        ok = await client.post("/devices/device-1/google/signin/password", json={"password": "correct horse"})
        assert ok.status_code == 200 and ok.json()["state"] == "working"
        assert "correct horse" not in ok.text and job.password == "correct horse"
        job.update("agree", "Almost done…")
        assert (await client.post("/devices/device-1/google/signin/agree")).json()["state"] == "working"
        assert job.agreed
        job.task.cancel()


def test_the_phone_app_reaches_the_sign_in_and_no_other_device_route():
    from app.companion import tunnel

    for path in ("/api/devices/device-1/google/signin", "/api/devices/device-1/google/signin/phone",
                 "/api/devices/device-1/google/signin/password", "/api/devices/device-1/google/signin/agree"):
        assert tunnel.allowed("POST", path)
    for path in ("/api/devices/device-1/screenshot", "/api/devices/device-1/wake", "/api/devices/device-1/google",
                 "/api/devices/device-1/google/signin/../../wake", "/api/devices/device-1/google/signin/other"):
        assert not tunnel.allowed("POST", path)


async def test_the_endpoint_takes_the_method(gateway_client, monkeypatch, clock):
    client, _ = gateway_client
    monkeypatch.setattr(signin, "LIMIT", 10**9)
    install(monkeypatch, GooglePhone({}))
    async with client:
        bad = await client.post("/devices/device-1/google/signin", json={"email": "name@gmail.com", "method": "sms"})
        assert bad.status_code == 422
        started = await client.post("/devices/device-1/google/signin",
                                    json={"email": "name@gmail.com", "method": "password"})
        assert started.json()["method"] == "password"
        assert (await client.delete("/devices/device-1/google/signin")).json()["method"] == "password"


@pytest.mark.parametrize("email", ["", "two words@gmail.com", "it's@gmail.com", "50%@gmail.com", "a@b"])
async def test_the_endpoint_refuses_what_is_not_an_email(gateway_client, monkeypatch, email):
    client, _ = gateway_client
    install(monkeypatch, GooglePhone({}))
    async with client:
        response = await client.post("/devices/device-1/google/signin", json={"email": email})
    assert response.status_code == 422


async def test_the_endpoint_waits_while_an_agent_holds_the_phone(gateway_client, monkeypatch):
    client, leases = gateway_client
    install(monkeypatch, GooglePhone({}))
    leases.lease(["device-1"], "run-1:node")
    async with client:
        response = await client.post("/devices/device-1/google/signin", json={"email": "name@gmail.com"})
    assert response.status_code == 409 and not signin.jobs


async def test_the_api_forwards_the_sign_in_and_explains_an_old_gateway(monkeypatch):
    from app.api import common, devices

    seen = []

    def handler(request):
        seen.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(404, json={"detail": "Not Found"})
        return httpx.Response(200, json={"state": "working", "message": "", "number": None, "account": None,
                                         "email": "name@gmail.com"})

    monkeypatch.setattr(common, "gateway", httpx.AsyncClient(base_url="http://gateway",
                                                             transport=httpx.MockTransport(handler)))
    started = await devices.start_google_signin("device-1", devices.SignInInput(email="name@gmail.com"))
    assert started["state"] == "working"
    with pytest.raises(devices.HTTPException) as old:
        await devices.google_signin_status("device-1")
    assert old.value.status_code == 503 and "out of date" in old.value.detail
    assert seen == [("POST", "/devices/device-1/google/signin"), ("GET", "/devices/device-1/google/signin")]
