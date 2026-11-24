"""Pictures for the user (runtime/pictures.py): what clip cuts out of the screen, and how the orchestrator's
references to them become the run's image URLs."""

import io

from app.models import PRIVATE_MARK
from app.runtime import pictures
from PIL import Image


def node(cls="android.view.View", b=None, children=(), **extra):
    n = {"cls": cls, "b": b, **extra}
    if children:
        n["c"] = list(children)
    return n


def screenshot(width=720, height=1600, boxes=()):
    """A grey PNG with each (box, colour) painted on it, as screencap gives one."""
    image = Image.new("RGB", (width, height), (128, 128, 128))
    for box, colour in boxes:
        image.paste(colour, tuple(box))
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def test_the_picture_inside_an_element_is_its_largest_image():
    card = node("android.widget.LinearLayout", [0, 100, 720, 500], children=[
        node("android.widget.ImageView", [10, 110, 60, 160]),  # an icon: too small to be the picture
        node("android.widget.ImageView", [10, 170, 410, 470]),
        node("android.widget.TextView", [420, 170, 700, 200], text="Pegasus Trail"),
    ])
    assert pictures.picture_in(card) == [10, 170, 410, 470]


def test_a_web_image_without_alt_text_counts_as_a_picture():
    card = node("web:a", [0, 0, 400, 600], dom=True, wrole="link", children=[
        node("web:img", [0, 0, 400, 400], dom=True, wrole="img"),  # no desc: the page gave it no alt
        node("web:div", [0, 400, 400, 600], dom=True, text="Lake Louise"),
    ])
    assert pictures.picture_in(card) == [0, 0, 400, 400]


def test_an_element_without_a_picture_has_none():
    assert pictures.picture_in(node("android.widget.TextView", [0, 0, 400, 100], text="Hello")) is None
    assert pictures.picture_in(node("android.widget.ImageButton", [0, 0, 100, 100])) is None  # a control, not a picture


def test_clip_cuts_the_box_and_blacks_out_private_values():
    png = screenshot(boxes=[([100, 200, 400, 500], (200, 30, 30)), ([100, 520, 400, 560], (30, 200, 30))])
    data, size = pictures.clip(png, [100, 200, 400, 600], (720, 1600), private=[[100, 520, 400, 560]])
    assert size == (300, 400)
    cut = Image.open(io.BytesIO(data)).convert("RGB")
    assert cut.getpixel((150, 150))[0] > 150  # the red part is kept
    assert cut.getpixel((150, 340)) == (0, 0, 0)  # the private value is black
    assert pictures.clip(png, [0, 0, 10, 10], (720, 1600)) is None  # too small to show anything


def test_clip_scales_screen_coordinates_to_the_screenshot_and_shrinks_a_whole_screen():
    png = screenshot(width=1080, height=2400, boxes=[([150, 300, 600, 750], (200, 30, 30))])
    data, size = pictures.clip(png, [100, 200, 400, 500], (720, 1600), kind="picture")  # the screen is 720 wide
    assert size == (450, 450)
    assert Image.open(io.BytesIO(data)).convert("RGB").getpixel((225, 225))[0] > 150
    data, size = pictures.clip(png, [0, 0, 720, 1600], (720, 1600), kind="screen")
    assert max(size) == pictures.MAX_SIDE


def test_private_boxes_say_when_a_value_has_no_place():
    snapshot = {"windows": [{"nodes": [node(b=[0, 0, 100, 50], text=f"pin {PRIVATE_MARK}"), node(b=None, text="ok")]}]}
    assert pictures.private_boxes(snapshot) == ([[0, 0, 100, 50]], False)
    snapshot = {"windows": [{"nodes": [node(b=None, desc=PRIVATE_MARK)]}]}
    assert pictures.private_boxes(snapshot) == ([], True)


def test_references_become_the_runs_image_urls_and_unknown_ones_are_dropped():
    held = [{"ref": "image:17", "caption": "Pegasus Trail, $129"}, {"ref": "image:18", "caption": "Order placed"}]
    text = ("Found it.\n\n![Nike Pegasus Trail](image:17)\n\nAlso image:18 and ![gone](image:99) and image:7.\n"
            "A price of 3 image:s is fine.")
    assert pictures.resolve(text, "run1", held) == (
        "Found it.\n\n![Nike Pegasus Trail](/api/runs/run1/images/17)\n\n"
        "Also ![Order placed](/api/runs/run1/images/18) and  and .\nA price of 3 image:s is fine."
    )
    assert pictures.resolve("no pictures here", "run1", []) == "no pictures here"


def test_a_line_of_pictures_becomes_a_paragraph_of_its_own():
    held = [{"ref": "image:1", "caption": "a"}, {"ref": "image:2", "caption": "b"}]
    text = "Two colours:\n![Black](image:1) ![Grey](image:2)\nNothing was ordered.\n- a list item with ![inline](image:1)"
    assert pictures.resolve(text, "r", held) == (
        "Two colours:\n\n![Black](/api/runs/r/images/1) ![Grey](/api/runs/r/images/2)\n\nNothing was ordered.\n"
        "- a list item with ![inline](/api/runs/r/images/1)"
    )


def test_an_apps_or_pages_image_is_kept_as_jpeg_with_transparency_on_white():
    png = io.BytesIO()
    Image.new("RGBA", (300, 200), (255, 0, 0, 0)).save(png, "PNG")  # fully transparent red
    data, size = pictures.as_jpeg(png.getvalue())
    assert size == (300, 200) and Image.open(io.BytesIO(data)).convert("RGB").getpixel((10, 10)) == (255, 255, 255)
    big = io.BytesIO()
    Image.new("RGB", (4000, 2000), (0, 0, 255)).save(big, "JPEG")
    data, size = pictures.as_jpeg(big.getvalue())
    assert size == (1600, 800)
    small = io.BytesIO()
    Image.new("RGB", (20, 20)).save(small, "PNG")
    assert pictures.as_jpeg(small.getvalue()) is None
    jpeg = io.BytesIO()
    Image.new("RGB", (640, 480)).save(jpeg, "JPEG")
    assert pictures.as_jpeg(jpeg.getvalue())[0] == jpeg.getvalue()  # a JPEG of a fair size is kept as it is
