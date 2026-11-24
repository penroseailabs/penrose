"""A type reports what the field holds afterwards when the app changed it (read back by the bridge)."""

from app.bridge.screen import Element, performed, typed_differs


def field():
    return Element(n=14, h=7, node={"id": "new_servings"}, window="Broccoli", role="Field", label="Servings")


def test_a_field_that_drops_words_says_so():
    result = performed({"cmd": "set_text", "text": "8 servings"}, field(), {"via": "setText", "now": "8"})
    assert result.endswith("the field now holds '8', not what was typed: the app changed or dropped part of it")


def test_a_field_that_took_it_says_nothing_more():
    result = performed({"cmd": "set_text", "text": "8 servings"}, field(), {"via": "setText", "now": "8 servings"})
    assert result == "typed into [14] new_servings via setText"


def test_a_private_entry_is_never_echoed():
    assert typed_differs("{private:1}", "hunter2") is None
    result = performed({"cmd": "set_text", "text": "{private:1}"}, field(), {"via": "setText", "now": "hunter2"})
    assert "hunter2" not in result


def test_no_readback_no_claim():
    assert typed_differs("hello", None) is None  # the accessibility path reports no text back
    assert typed_differs("hello ", "hello") is None
    assert typed_differs("x" * 10, "y" * 100).endswith("...")


def test_text_added_to_a_field_is_checked_where_it_was_added():
    note = "9pej0L48OHebXPVJPN3g\n\nInconceivable!\n"
    added = performed({"cmd": "set_text", "text": "9pej0L48OHebXPVJPN3g\n\n", "at": "start"}, field(), {"via": "insert", "now": note})
    assert added == "typed into [14] new_servings via insert"
    lost = performed({"cmd": "set_text", "text": "Title\n", "at": "end"}, field(), {"via": "insert", "now": "Body"})
    assert "the field now holds 'Body'" in lost


def test_prepend_and_append_add_to_what_the_field_holds():
    from app.bridge.screen import options, render
    from app.bridge.tools import resolve
    from phone import field as raw_field, snapshot
    menu = options(render(snapshot(raw_field(6, "Inconceivable!\n"))), [])
    step, why = resolve({"name": "interact", "arguments": {"element": 1, "interaction": "prepend", "value": "Header\n\n"}}, menu)
    description, action = menu[step["keys"][0]]
    assert why is None and action == {"cmd": "insert_text", "n": 1, "text": "Header\n\n", "at": "start"}
    assert description.startswith("Add at the start 'Header\\n\\n' into [1]")


def test_a_picker_takes_an_exact_date():
    from app.bridge.screen import options, render
    from app.bridge.tools import resolve
    from phone import element, snapshot
    picker = element(3, cls="android.widget.DatePicker", flags=("adjust",))
    menu = options(render(snapshot(picker)), [])
    step, why = resolve({"name": "interact", "arguments": {"element": 1, "interaction": "set_value", "value": "2023-10-09"}}, menu)
    assert why is None and menu[step["keys"][0]][1] == {"cmd": "set_value", "n": 1, "value": "2023-10-09"}
    step, why = resolve({"name": "interact", "arguments": {"element": 1, "interaction": "set_value", "value": "next week"}}, menu)
    assert step is None and "set_value needs value" in why
