"""Pictures for the user: what a phone agent clips from the screen while it works, so the answer can show the
thing it is about (a product, a room, a place), the options the user must choose between, or the proof of
what was done (a booking, an order), without anyone revisiting a page.

A clip is anchored to a numbered element of the Screen the agent is reading as text: the runtime takes one
screenshot and cuts the element out of it. With `picture` set, it cuts the largest image drawn inside the
element instead (the product photo in a result card, the hotel's photo at the top of its page): no model
ever sees the pixels, so an image needs no alt text to be clipped; the caption comes from the words the
agent read around it. Element 0 is the whole screen, for a confirmation that is not one element.

The orchestrator sees the clips as `pictures` (a reference, a caption, where it was taken) and puts them in
its answer as markdown images with the reference as the source (`![caption](image:17)`); `resolve` turns
those into the run's image URLs and drops any reference the run does not hold.
"""

import io
import re

from PIL import Image

from ..models import PRIVATE_MARK

IMAGE_CLASSES = ("imageview", "web:img", "image", "photo", "thumbnail")
MIN_SIDE = 32  # a crop smaller than this says nothing
MIN_PICTURE_SIDE = 48  # an image inside an element must be at least this big to be "the picture"
MAX_SIDE = 1080  # a whole screen or a card is kept no larger than this on its longer side
JPEG_QUALITY = 85

REF = re.compile(r"image:(\d+)")
IMAGE_MD = re.compile(r"!\[([^\]]*)\]\(\s*image:(\d+)\s*\)")


def is_image(node):
    """A node that draws a picture: a web <img>, an ImageView, or a view whose class or role says image."""
    if node.get("wrole") == "img":
        return True
    names = " ".join(str(node.get(k) or "") for k in ("cls", "base", "a11y_cls", "a11y_role")).lower()
    return any(part in names for part in IMAGE_CLASSES) and "button" not in names


def box_of(node):
    b = node.get("b")
    return [int(v) for v in b] if isinstance(b, list) and len(b) == 4 else None


def area(b):
    return max(0, b[2] - b[0]) * max(0, b[3] - b[1])


def picture_node(node):
    """The largest image drawn inside `node` (itself included), or None when there is none big enough."""
    best = None

    def walk(n):
        nonlocal best
        b = box_of(n)
        if b and is_image(n) and min(b[2] - b[0], b[3] - b[1]) >= MIN_PICTURE_SIDE and (best is None or area(b) > area(box_of(best))):
            best = n
        for child in n.get("c", []):
            walk(child)

    walk(node)
    return best


def picture_in(node):
    """The bounds of the largest image inside `node`, or None."""
    found = picture_node(node)
    return box_of(found) if found else None


def shows_private(node):
    """Whether a privately typed value is drawn anywhere inside `node`."""
    if any(PRIVATE_MARK in str(node.get(k) or "") for k in ("text", "desc", "value")):
        return True
    return any(shows_private(c) for c in node.get("c", []))


def clip(jpeg, box, screen, private=(), kind="card"):
    """Cut `box` (screen coordinates, `screen` = (width, height)) out of the screenshot, with every private
    box blacked out first. Returns (jpeg bytes, (w, h)) or None when the box is too small to show anything."""
    image = Image.open(io.BytesIO(jpeg)).convert("RGB")
    scale = image.width / screen[0] if screen and screen[0] else 1
    if private:
        from PIL import ImageDraw
        draw = ImageDraw.Draw(image)
        for x1, y1, x2, y2 in private:
            draw.rectangle([x1 * scale - 8, y1 * scale - 8, x2 * scale + 8, y2 * scale + 8], fill=(0, 0, 0))
    x1, y1, x2, y2 = (round(v * scale) for v in box)
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(image.width, x2), min(image.height, y2)
    if x2 - x1 < MIN_SIDE or y2 - y1 < MIN_SIDE:
        return None
    cut = image.crop((x1, y1, x2, y2))
    if kind != "picture" and max(cut.size) > MAX_SIDE:
        cut.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
    out = io.BytesIO()
    cut.save(out, "JPEG", quality=JPEG_QUALITY, optimize=True)
    return out.getvalue(), cut.size


MAX_SOURCE_SIDE = 1600  # an original image from a page or an app is kept no larger than this


def as_jpeg(data):
    """Image bytes as an app or browser handed them over (JPEG, PNG, WebP, GIF...) as (jpeg, (w, h)) for the chat:
    re-encoded only when they are not a JPEG already or are too big, with transparency on white. None when they
    are not an image or too small to show anything."""
    image = Image.open(io.BytesIO(data))
    if min(image.size) < MIN_SIDE:
        return None
    if image.format == "JPEG" and max(image.size) <= MAX_SOURCE_SIDE:
        return data, image.size
    if image.mode in ("RGBA", "LA", "P"):
        image = image.convert("RGBA")
        flat = Image.new("RGB", image.size, (255, 255, 255))
        flat.paste(image, mask=image.getchannel("A"))
        image = flat
    else:
        image = image.convert("RGB")
    if max(image.size) > MAX_SOURCE_SIDE:
        image.thumbnail((MAX_SOURCE_SIDE, MAX_SOURCE_SIDE), Image.LANCZOS)
    out = io.BytesIO()
    image.save(out, "JPEG", quality=JPEG_QUALITY, optimize=True)
    return out.getvalue(), image.size


def private_boxes(snapshot):
    """The bounds of every node showing a privately typed value, and whether one is on screen whose place
    cannot be told (then no picture may be taken at all)."""
    boxes, hidden = [], False

    def walk(node):
        nonlocal hidden
        if any(PRIVATE_MARK in str(node.get(k) or "") for k in ("text", "desc", "value")):
            if b := box_of(node):
                boxes.append(b)
            else:
                hidden = True
        for child in node.get("c", []):
            walk(child)

    for window in (snapshot or {}).get("windows", []):
        for node in window.get("nodes", []):
            walk(node)
    return boxes, hidden


SIZE = re.compile(r"(\d+)\s*[×x]\s*(\d+)")


def url_of(run_id, image_id, size=""):
    """The run's image URL; with the picture's size ("540×1200") as a fragment (`#540x1200`), which the browser never
    sends, so the chat can hold a picture's shape while it loads instead of jumping when it arrives."""
    found = SIZE.search(size or "")
    return f"/api/runs/{run_id}/images/{image_id}" + (f"#{found[1]}x{found[2]}" if found else "")


def resolve(text, run_id, pictures):
    """The orchestrator's markdown with every `image:N` reference turned into the run's image URL. A bare
    reference becomes an image with its caption; a reference to a picture this run does not hold is dropped,
    so the user never sees a broken image."""
    held = {int(REF.search(p["ref"]).group(1)): p for p in pictures if REF.search(p.get("ref", ""))}

    def image(match):
        caption, n = match.group(1), int(match.group(2))
        if n not in held:
            return ""
        return f"![{caption or held[n].get('caption', '')}]({url_of(run_id, n, held[n].get('size', ''))})"

    text = IMAGE_MD.sub(image, text)

    def bare(match):
        n = int(match.group(1))
        if n not in held:
            return ""
        return f"![{held[n].get('caption', '')}]({url_of(run_id, n, held[n].get('size', ''))})"

    # A reference written on its own (not already inside a link or image we just made).
    text = re.sub(r"(?<![\w/])image:(\d+)(?!\d)", bare, text)
    return paragraphs(re.sub(r"[ \t]+\n", "\n", text))


def paragraphs(text):
    """Every line of pictures in a paragraph of its own: markdown joins a picture to the sentence before it
    when no blank line separates them, and the chat lays a picture paragraph out as a row of pictures."""
    out, lines = [], text.split("\n")
    for i, line in enumerate(lines):
        pic = line.strip().startswith("![")
        before = out[-1].strip() if out else ""
        if pic and before and not before.startswith("!["):
            out.append("")
        elif not pic and line.strip() and before.startswith("!["):
            out.append("")
        out.append(line)
    return "\n".join(out)
