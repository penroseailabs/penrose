"""read_page's text: the whole page's elements read as Screen reads them, without what is only for acting."""

from app.runtime.reading import page_on_screen, reading_text
from phone import element, snapshot


def web(text=None, desc=None, role=None, flags=(), children=(), **extra):
    node = {"dom": True, "f": list(flags), **({"text": text} if text else {}), **({"desc": desc} if desc else {}),
            **({"wrole": role} if role else {}), **extra}
    if children:
        node["c"] = list(children)
    return node


def test_each_element_reads_as_on_screen_with_its_parts_under_it():
    page = [
        web("Trail Running Shoes", role="heading"),
        web("Pegasus Trail $139", role="link", href="https://shop.example/pegasus", id="card-1", children=[
            web("4.3", desc="Rated 4.3 out of 5 stars by 2,758 reviews"),  # the rating is only in the label
            web("Add to cart", role="button", flags=("click",)),
        ]),
        web(children=[web("Free delivery")]),  # a wrapper that says nothing adds no line and no depth
        web("trail shoes", desc="Search", role="searchbox", flags=("edit",)),
    ]
    assert reading_text(page, 10_000).splitlines() == [
        "## Trail Running Shoes",
        "Pegasus Trail $139",
        "  Rated 4.3 out of 5 stars by 2,758 reviews",  # the bare number repeats the label
        "  Add to cart",
        "Free delivery",
        "Search: trail shoes",
    ]


def test_a_label_that_only_repeats_the_text_is_said_once_and_repeated_lines_are_dropped():
    page = [web("Sold out", desc="Sold out"), web("Sold out"), web("$99")]
    assert reading_text(page, 10_000).splitlines() == ["Sold out", "$99"]


def test_a_long_page_is_cut_at_a_line_with_a_note():
    page = [web(f"Item {i} costs ${i}00") for i in range(200)]
    text = reading_text(page, 300)
    lines = text.splitlines()
    assert lines[-1].startswith("(… the rest of the page,") and lines[-1].endswith("more characters, is not shown)")
    assert all(line.startswith("Item ") for line in lines[:-1]) and len(text) < 400


def test_the_page_on_screen_is_the_topmost_one_with_its_process_and_state():
    page = element(5, cls="android.webkit.WebView", webview=True, title="Results", url="https://shop.example/s",
                   page={"ready": "loading"})
    found = page_on_screen(type("S", (), {"snapshot": snapshot(page, pkg="com.android.chrome")})())
    assert (found.handle, found.process, found.title, found.loading) == (5, "com.android.chrome", "Results", True)
    assert page_on_screen(type("S", (), {"snapshot": snapshot(element(1, "Continue"))})()) is None


def test_images_of_a_fair_size_are_numbered_with_their_alt_text():
    from app.runtime.reading import reading
    page = [{"dom": True, "wrole": "heading", "text": "Lake Louise", "c": [
        {"dom": True, "cls": "web:img", "wrole": "img", "b": [0, 100, 400, 400], "desc": "The lake in summer", "h": "cdp:1:7"},
        {"dom": True, "cls": "web:img", "wrole": "img", "b": [0, 400, 20, 420]},  # too small
        {"dom": True, "text": "Open from March to May."},
        {"dom": True, "cls": "web:img", "wrole": "img", "b": [0, 500, 300, 700], "h": "cdp:1:9", "file": "trail-map.png"},  # no alt
    ]}]
    text, images = reading(page, 10_000)
    assert text.splitlines() == ["## Lake Louise", '  (image 1 "The lake in summer")', "  Open from March to May.", "  (image 2, trail-map.png)"]
    assert [images[n]["h"] for n in (1, 2)] == ["cdp:1:7", "cdp:1:9"]
    text, images = reading(page, 60)  # cut before the second image: it is not offered
    assert list(images) == [1]
