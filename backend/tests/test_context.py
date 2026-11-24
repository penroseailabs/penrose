"""What a phone agent reads, and how a long session stays short: words drawn apart from the cells they
label, lists that say how much of them is shown, outcomes that name the new screen, the check before a
report that rests on a list's first rows, and checkpoints (the agent's own compaction)."""

from app.agents.registry import SUBAGENT
from app.bridge.screen import changes, options, render
from app.config import settings
from app.plan import unreported
from app.runtime.orchestrator import previous_attempt
from fakes import checkpoint, click, log, phone, report
from phone import FakePhone, element, field, label, screen_of, snapshot
from test_phone_agent import LOG, Session, section


def text_node(h, text, b):
    return {"h": h, "cls": "com.example.ui.HostedTextView", "f": [], "text": text, "b": b}


def cell(h, b, *children, flags=("click",)):
    return element(h, None, flags=flags, cls="android.view.View", virtual=True, b=b, c=list(children))


def hosted():
    """A Compose screen that draws its words in hosted Android views: every word in one branch, the cells
    you tap in another, tied together only by where they are."""
    words = element(10, None, flags=(), cls="androidx.compose.ui.platform.AndroidViewsHandler", b=[0, 0, 1080, 2400], c=[
        text_node(11, "SAT", [100, 300, 160, 340]), text_node(12, "24", [100, 350, 160, 400]),
        text_node(13, "SUN", [300, 300, 360, 340]), text_node(14, "25", [300, 350, 360, 400]),
        text_node(15, "Riverside Cinema", [100, 800, 600, 850]),
    ])
    cells = cell(20, [0, 280, 1080, 1000],
                 cell(21, [80, 290, 240, 420]), cell(22, [260, 290, 420, 420]),
                 cell(23, [60, 780, 1000, 1000], element(24, "07:15 PM", cls="android.view.View", virtual=True,
                                                         b=[100, 900, 300, 980]), flags=()),
                 flags=())
    return snapshot(element(1, None, flags=(), cls="android.widget.FrameLayout", b=[0, 0, 1080, 2400], c=[words, cells]))


def test_cells_take_the_words_drawn_over_them():
    text = render(hosted()).text
    assert '"SAT · 24"' in text and '"SUN · 25"' in text
    # The row's title is printed in its row, above its times, and no word is printed twice.
    lines = text.splitlines()
    title = next(i for i, line in enumerate(lines) if "Riverside Cinema" in line)
    assert "07:15 PM" in lines[title + 1] and text.count("Riverside Cinema") == 1
    assert not any(line.strip() in ('"SAT"', '"24"') for line in lines)


def test_words_inside_what_they_label_stay_where_they_are():
    # A click layer the size of its container is no smaller than the text's parent: nothing moves.
    row = element(2, None, flags=(), cls="android.widget.FrameLayout", b=[0, 0, 500, 100], c=[
        {**label(3, "Settings"), "b": [10, 10, 200, 90]},
        element(4, None, cls="android.view.View", b=[0, 0, 500, 100]),
    ])
    text = render(snapshot(row, element(5, "Help", b=[0, 200, 500, 300]))).text
    assert text.splitlines()[3:] == ['"Settings"', '[1] Item ctx="Settings"', '[2] Button "Help"']


def test_a_list_says_how_many_of_its_rows_are_on_screen():
    rows = [element(10 + i, f"Row {i}", flags=("click",)) for i in range(3)]
    listing = element(1, None, flags=("list", "can_scroll_down"), cls="androidx.recyclerview.widget.RecyclerView",
                      items=46, c=rows)
    assert "items=46 (3 on screen)" in render(snapshot(listing)).text
    whole = element(1, None, flags=("list",), cls="androidx.recyclerview.widget.RecyclerView", items=3, c=rows)
    assert "on screen" not in render(snapshot(whole)).text


def test_a_list_says_which_of_its_rows_are_on_screen_and_where_the_rest_are():
    # A chat opens at its newest message, its last row: what is not shown is above (2026-11-19, WhatsApp).
    def listing(first, items):
        rows = [{**element(10 + i, f"Row {first + i}", flags=("click",)), "li": first + i} for i in range(3)]
        scrolls = (("can_scroll_up",) if first else ()) + (("can_scroll_down",) if first + 3 < items else ())
        return element(1, None, flags=("list", *scrolls), cls="android.widget.ListView", items=items, c=rows)
    assert "items=5 (rows 3–5 on screen: the end; the rest are above)" in render(snapshot(listing(2, 5))).text
    assert "items=46 (rows 1–3 on screen: the start; the rest are below)" in render(snapshot(listing(0, 46))).text
    assert "items=46 (rows 11–13 on screen)" in render(snapshot(listing(10, 46))).text


def test_a_compose_field_submits_with_enter():
    compose = options(render(snapshot(field(3, virtual=True))), [])
    assert compose["submit_1"][1] == {"key": "enter", "label": "Enter"}
    native = options(render(snapshot(field(3))), [])
    assert native["submit_1"][1] == {"cmd": "ime_action", "n": 1}


def test_an_outcome_names_the_new_screen_in_its_own_words():
    before = render(screen_of("Search results"))
    after = render(snapshot(label(5, "Ranabaali"), label(6, "Action, Drama"), element(1, "Book"),
                            activity="com.example.DetailActivity"))
    assert 'now on DetailActivity showing "Ranabaali" | "Action, Drama"' in changes(before, after)
    same = render(screen_of("Search results", "No results for this filter"))
    assert "new lines: 'No results for this filter'" in changes(before, same)


def moving(done):
    """A screen that is somewhere new after every action."""
    return snapshot(label(100, f"Screen {done}"), element(1, f"Next {done}"))


def long_list():
    rows = [element(10 + i, f"Show {i}", flags=("click",)) for i in range(3)]
    return snapshot(element(1, None, flags=("list", "can_scroll_down"), cls="androidx.recyclerview.widget.RecyclerView",
                            items=46, c=rows))


async def test_a_report_off_the_first_rows_of_a_long_list_waits_once(store):
    seen = []

    def decide(text):
        seen.append(text)
        return phone(report(summary="Only three shows."))

    s = Session(store, decide, phone=FakePhone(lambda done: long_list()))
    end = await s.run()
    assert end.status == "completed" and len(seen) == 2
    assert "holds 46 rows and 3 are on screen" in section(seen[1], "Last result")


async def test_a_checkpoint_replaces_notes_progress_and_older_actions(store, monkeypatch):
    monkeypatch.setattr(settings, "phone_checkpoint_turns", 3)
    seen, summaries = [], []

    def decide(text):
        seen.append(text)
        if len(seen) <= 3:
            return phone(click(1), log(f"Price {len(seen)} is $6{len(seen)}"))
        return phone(report())

    def summarize(text):
        summaries.append(text)
        return phone(checkpoint(found=["Price 1 is $61", "Price 2 is $62", "Price 3 is $63"],
                                tried=["[4] opens an unrelated item"], plan="[done] Read prices\n[now] Report"))

    s = Session(store, decide, phone=FakePhone(moving),
                summarize=summarize)
    await s.run()
    assert len(summaries) == 1
    asked = summaries[0]
    assert section(asked, "Turn").startswith("checkpoint") and section(asked, "Interactions") is None
    assert "Price 3 is $63" in section(asked, LOG)
    after = seen[3]
    assert "Price 2 is $62" in after and "Tried, did not work:\n- [4] opens an unrelated item" in after
    assert section(after, "Your log since the checkpoint, oldest first (time @ where you were | what you wrote)") == "(nothing yet)" and section(after, "Your progress") == "[done] Read prices\n[now] Report"
    assert "3 earlier, summed up in your checkpoint and your log" in after and "(none)" in after
    assert s.mem["checkpoint"]["found"][0] == "Price 1 is $61" and s.mem["log"] == []


async def test_repeating_itself_brings_a_checkpoint_that_keeps_the_warning(store, monkeypatch):
    monkeypatch.setattr(settings, "phone_checkpoint_turns", 0)
    seen, summaries = [], []

    def decide(text):
        seen.append(text)
        return phone(report()) if len(seen) == 7 else phone(click(1))

    def summarize(text):
        summaries.append(text)
        return phone(checkpoint(found=["The list shows two items"], tried=["Continue does nothing here"]))

    # Four turns of progress, then the same tap on the same screen: the second time brings the warning, and
    # with it a checkpoint, before the next turn.
    s = Session(store, decide, phone=FakePhone(lambda done: moving(done) if done < 4 else screen_of("Same")),
                summarize=summarize)
    await s.run()
    assert len(summaries) == 1 and "2 times" in section(summaries[0], "Notices")
    assert "2 times" in section(seen[6], "Notices") and "Continue does nothing here" in seen[6]


async def test_a_repeat_soon_after_the_start_or_a_checkpoint_brings_none(store, monkeypatch):
    monkeypatch.setattr(settings, "phone_checkpoint_turns", 0)
    summaries = []
    s = Session(store, lambda text: phone(click(1)), phone=FakePhone(lambda done: screen_of("Same")),
                summarize=lambda text: summaries.append(text) or phone(checkpoint()))
    await s.run()  # repeats from the first turn: the runtime ends it, with no checkpoint in between
    assert summaries == []


async def test_a_checkpoint_that_drops_what_was_found_is_not_taken(store, monkeypatch):
    monkeypatch.setattr(settings, "phone_checkpoint_turns", 2)
    seen = []

    def decide(text):
        seen.append(text)
        return phone(click(1), log("Cart total $62")) if len(seen) <= 2 else phone(report())

    s = Session(store, decide, phone=FakePhone(moving),
                summarize=lambda text: phone(checkpoint(found=[])))
    await s.run()
    assert s.mem["checkpoint"] is None and "Cart total $62" in section(seen[2], LOG)


def test_the_next_attempt_and_an_unreported_step_keep_the_checkpoint():
    kept = {"turn": 10, "at": "T+02:00", "found": ["Sat 24: 7:15 PM"], "tried": ["swiping the strip"], "where": "list"}
    step = {"goal": "Read showtimes", "report": {"status": "unfinished", "summary": "ended"},
            "sessions": {SUBAGENT.prompt: {"checkpoint": kept, "notes": [], "trail": []}}}
    assert previous_attempt(step)["checkpoint"] == kept
    assert unreported(step)["checkpoint"] == kept


def look_alike_rows(title):
    """A part of a long list: a title, then rows that read the same in every part (times, "Add")."""
    rows = [element(10, None, flags=(), cls="android.widget.LinearLayout", c=[label(11, title),
                                                                           element(12, "07:15 PM"), element(13, "09:30 PM")])]
    return snapshot(element(1, None, flags=("list", "can_scroll_down"), cls="androidx.recyclerview.widget.RecyclerView",
                            items=46, c=rows))


def test_parts_of_a_list_that_differ_only_in_their_titles_are_different_screens():
    from app.device.driver import fingerprint
    assert fingerprint(render(look_alike_rows("Cinema A"))) != fingerprint(render(look_alike_rows("Cinema B")))
    assert fingerprint(render(look_alike_rows("Cinema A"))) == fingerprint(render(look_alike_rows("Cinema A")))


async def test_scrolling_through_a_list_of_look_alike_rows_is_not_circling(store):
    seen = []

    def decide(text):
        seen.append(text)
        return phone({"tool": "interact", "element": 1, "interaction": "scroll_down"}) if len(seen) <= 6 else phone(report())

    s = Session(store, decide, phone=FakePhone(lambda done: look_alike_rows(f"Cinema {done}")))
    end = await s.run()
    assert end.status == "completed" and not any("same actions" in (section(t, "Notices") or "") for t in seen)


async def test_a_long_handover_comes_through_whole(store):
    from app.agents.registry import HANDOVER
    from fakes import handover

    def decide(text):
        if section(text, "Turn").startswith("handover"):
            return phone(handover("A long list of findings. " * 120, "on_track"))
        return phone(click(1))

    s = Session(store, decide, phone=FakePhone(lambda done: screen_of("Same")), handover=HANDOVER)
    end = await s.run()
    assert end.status == "unfinished" and len(end.handover["found"]) == 3000


def test_a_web_field_submits_through_its_page_and_every_field_offers_submit():
    """A web field's node is virtual too, but its page presses Enter in it and submits its form: type_and_submit
    was refused on every web page while its submit was a bare Enter key that belonged to no element."""
    from app.bridge.tools import index, interactions_text

    web = options(render(snapshot(field(3, virtual=True, dom=True))), [])
    assert web["submit_1"][1] == {"cmd": "ime_action", "n": 1}
    for menu in (web, options(render(snapshot(field(3, virtual=True))), []), options(render(snapshot(field(3))), [])):
        assert index(menu)[(1, "submit", None)] == "submit_1"
        assert "submit" in interactions_text(menu)
