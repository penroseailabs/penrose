"""A whole web page as text to read: what read_page shows a phone agent.

Screen shows the part of a web page that is on the display, as numbered elements to act on. read_page shows all
of the page, read from the same element tree (the page script's dump with scope "page", off-screen elements
included), so every element reads exactly as it does on Screen: its own text and accessible label
(bridge.screen.own_text), a field's value, a heading marked as one. What is there only for acting is left out:
numbers, ids, link targets, flags. One line per element that says something, in page order, each element's
parts indented under it; a line that repeats the one before it is dropped. Images of a fair size are numbered
in page order ((image 3 "its alt text"), or (image 3, hotel-pool.jpg) when the page gave it no words but
a file name), so clip can keep one for the user by that number.
"""

from dataclasses import dataclass

from ..bridge.screen import clean, own_text, web_role
from . import pictures

INDENT_MAX = 6  # deeper parts are shown at this depth: nesting beyond it says nothing more


@dataclass
class Page:
    """The topmost web page on screen: the handle its reader answers to and the process that draws it."""
    handle: str
    process: str
    title: str
    url: str
    loading: bool


def page_on_screen(screen):
    """The topmost web page on screen (Chrome's, read over DevTools, or a WebView inside an app), or None."""
    if not screen:
        return None

    def find(nodes):
        for node in nodes:
            if node.get("webview") and node.get("h"):
                return node
            if found := find(node.get("c", [])):
                return found
        return None

    for window in reversed(screen.snapshot.get("windows", [])):  # bottom first: the last is on top
        if node := find(window.get("nodes", [])):
            return Page(node["h"], window.get("process"), node.get("title") or "", node.get("url") or "",
                        (node.get("page") or {}).get("ready") == "loading")
    return None


def words(node):
    """What one element says: its text and label as Screen gives them, a field's value, a heading as such."""
    said = " · ".join(own_text(node))
    if {"edit", "select"} & set(node.get("f", [])) and node.get("text"):
        value = clean(node["text"])
        said = f"{said}: {value}" if said else value
    if said and web_role(node) == "Heading":
        said = f"## {said}"
    return said


def reading(nodes, limit):
    """The page's elements (web_page's tree) as reading text, cut at `limit` characters with a note saying so,
    and its images by the number the text gives them ({3: node}): the ones big enough to show the user something
    (pictures.MIN_PICTURE_SIDE), icons and spacers left out."""
    lines, images = [], {}

    def walk(node, depth):
        said = words(node)
        b = pictures.box_of(node)
        if pictures.is_image(node) and b and min(b[2] - b[0], b[3] - b[1]) >= pictures.MIN_PICTURE_SIDE:
            images[len(images) + 1] = node
            hint = f' "{said}"' if said else f", {node['file']}" if node.get("file") else ""
            said = f"(image {len(images)}{hint})"
        if said:
            lines.append("  " * min(depth, INDENT_MAX) + said)
        for child in node.get("c", []):
            walk(child, depth + 1 if said else depth)

    for node in nodes:
        walk(node, 0)
    kept = [line for i, line in enumerate(lines) if i == 0 or line.strip() != lines[i - 1].strip()]
    text = "\n".join(kept)
    if len(text) > limit:
        cut = text[:limit].rsplit("\n", 1)[0]
        text = cut + f"\n(… the rest of the page, {len(text) - len(cut)} more characters, is not shown)"
        images = {n: node for n, node in images.items() if f"(image {n})" in cut or f'(image {n} "' in cut}
    return text, images


def reading_text(nodes, limit):
    return reading(nodes, limit)[0]
