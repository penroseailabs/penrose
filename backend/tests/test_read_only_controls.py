"""What a read-only agent (the verifier) may not tap: controls that change something, and only those."""

import pytest

from app.bridge.screen import Element
from app.runtime.phone import commits


def el(label="", role="Button", node=None, list_pos=""):
    return Element(n=1, h=1, node=node or {}, window="w", role=role, label=label, list_pos=list_pos)


@pytest.mark.parametrize("element,word", [
    (el("Delete"), "Delete"),
    (el("Place order"), "Place order"),
    (el("Add to cart"), "Add to cart"),
    (el("Send"), "Send"),
    (el("", role="Image", node={"id": "btn_delete"}), "delete"),
    (el("Save", role="Text", node={"id": "action_save"}), "Save"),
])
def test_controls_that_change_something(element, word):
    assert (commits(element) or "").lower() == word.lower()


@pytest.mark.parametrize("element", [
    el("Order #1234", role="Item", list_pos="list#2/9"),
    el("Your order has shipped"),
    el("Order #1234"),
    el("Record-1"),
    el("Address book"),
    el("Start date", role="Field"),
    el("Tutoring Services · $100.16", role="Item", node={"li": 3}),
    el("Back"),
    el("Filter", node={"id": "aggregated_statistics_filter"}),
    el("", role="Image", node={"id": "text_input_start_icon"}),
    el("", role="Image", node={"id": "post_card"}),
    el("", role="Image", node={"id": "record_list_header"}),
])
def test_rows_fields_and_texts_that_only_hold_the_word(element):
    assert commits(element) is None
