"""What the phone agent reads when the bridge's Guard refuses an action (Gmail never deletes email)."""

from app.bridge.screen import performed, refusals, render
from phone import element, snapshot

REASON = ("This action is blocked as a safeguard against prompt injection: penrose never deletes email. "
          "If the user wants it deleted, they can delete it themselves.")
MARK = "deleting email is blocked as a safeguard against prompt injection"


def test_a_blocked_control_says_so_on_its_line():
    screen = render(snapshot(element(1, "Delete", id="delete", blocked=MARK), element(2, "Archive", id="archive"),
                             pkg="com.google.android.gm"))
    lines = {n: el.summary() for n, el in screen.elements.items()}
    assert f'blocked="{MARK}"' in next(s for s in lines.values() if "id=delete" in s)
    assert "blocked" not in next(s for s in lines.values() if "id=archive" in s)


def test_a_refused_tap_fails_with_the_reason_and_the_event_is_not_repeated():
    screen = render(snapshot(element(1, "Delete", id="delete", blocked=MARK), pkg="com.google.android.gm"))
    target = next(iter(screen.elements.values()))
    how = performed({"cmd": "click", "n": target.n}, target, {"ok": False, "refused": True, "error": REASON, "via": "touch"})
    assert how.endswith(f"FAILED: {REASON}")
    event = {"type": "refused", "app": "com.google.android.gm", "what": '"Delete"', "reason": REASON}
    assert refusals([event], how) == []


def test_a_refused_key_is_reported_from_its_event():
    event = {"type": "refused", "app": "com.google.android.gm", "what": "the KEYCODE_DEL key", "reason": REASON}
    assert refusals([event], "pressed Delete") == [f"the phone refused the KEYCODE_DEL key: {REASON}"]
    assert f"the phone refused the KEYCODE_DEL key: {REASON}" in render(snapshot(element(1, "OK")), events=[event]).text
