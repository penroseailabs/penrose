"""Turn a bridge snapshot into what the phone agent reads: one numbered line per thing it can act on, with
resource id, icon name, state, list position and, for look-alikes, the words around it.

The numbers are labels for one turn only. Behind each number sits the bridge handle (stable
while the View lives) and a durable key (id + label + context + list row) used to find the
element again if the handle went stale between reading and acting.
"""

import re
from dataclasses import dataclass, field

ROLES = [  # first match on the class or its framework base class
    ("EditText", "Field"), ("AutoCompleteTextView", "Field"),
    ("RadioButton", "Radio"), ("CheckBox", "Checkbox"), ("Switch", "Switch"), ("ToggleButton", "Toggle"),
    ("Chip", "Chip"), ("FloatingActionButton", "IconButton"), ("ImageButton", "IconButton"),
    ("Button", "Button"), ("ImageView", "Image"), ("TabView", "Tab"), ("TabLayout", "Tabs"),
    ("RecyclerView", "List"), ("ListView", "List"), ("GridView", "Grid"), ("ViewPager", "Pager"),
    ("ScrollView", "Scroll"), ("WebView", "Web"), ("SeekBar", "Slider"), ("RatingBar", "Rating"),
    ("NumberPicker", "Picker"), ("ProgressBar", "Progress"),
    ("TextView", "Text"), ("CardView", "Card"),
]
WEB_ROLES = {  # ARIA role (explicit or implied by the tag) of a DOM element read from a WebView's page
    "link": "Link", "button": "Button", "textbox": "Field", "searchbox": "Field", "spinbutton": "Field",
    "checkbox": "Checkbox", "radio": "Radio", "switch": "Switch", "tab": "Tab", "combobox": "Select",
    "menuitem": "MenuItem", "menuitemcheckbox": "MenuItem", "menuitemradio": "MenuItem", "option": "Option",
    "treeitem": "Item", "slider": "Slider", "heading": "Heading", "img": "Image", "dialog": "Dialog",
    "frame": "Frame", "file": "FileInput", "canvas": "Canvas", "video": "Video",
}
TAP_FLAGS = {"listener", "click", "checkable"}
SCROLL_FLAGS = {"can_scroll_down": "down", "can_scroll_up": "up", "can_scroll_right": "right", "can_scroll_left": "left"}
SCROLL_ENDS = {"can_scroll_up": "top", "can_scroll_down": "bottom", "can_scroll_left": "left end", "can_scroll_right": "right end"}
SCROLL_BEYOND = {"can_scroll_up": "above", "can_scroll_down": "below", "can_scroll_left": "to the left", "can_scroll_right": "to the right"}
SHOWN_FLAGS = ["checked", "selected", "focused", "disabled", "password", "spinner",
               "covered", "required", "readonly", "invalid", "loading", "modal", "blocked_frame", "picker"]
MAX_SCREEN_CHARS = 14000
# The word before a web text line that says what kind of words they are.
LEADS = {"heading": "heading ", "dialog": "dialog ", "alert": "alert ", "status": "status ", "progress": "progress "}


def web_role(node):
    """Role of a DOM node: the page script already named it; containers are lists or scroll areas."""
    flags = set(node.get("f", []))
    if node.get("wrole") in WEB_ROLES:
        return WEB_ROLES[node["wrole"]]
    if "list" in flags or "items" in node:
        return "List"
    return "Scroll" if flags & set(SCROLL_FLAGS) else "Item"


def account_chip(chip):
    """A keyboard chip that fills a saved account, as opposed to Google Password Manager's pinned key that opens
    its full list. The pin tells them apart: Android types an account Google must confirm first (a password
    saved for a related site, as sellercentral.amazon.com for amazon.com) as an action, like the key. A chip seen
    without its response (the hook came late) has only the type."""
    return not chip["pinned"] if "pinned" in chip else chip.get("kind") == "suggestion"


def inline_label(node):
    """What a keyboard chip is: Google Password Manager's saved sign-ins come as chips whose text another
    process draws (never readable here), numbered in Google's order, plus its own key that opens the full list."""
    chip = node.get("inline") or {}
    if account_chip(chip):
        label = f"saved sign-in {chip.get('index')} of {chip['of']}" if chip.get("of", 0) > 1 else "saved sign-in"
        if (chip.get("type") or chip.get("kind", "")).endswith("action"):
            label += " (Google confirms it before filling)"
        return label
    return "all saved sign-ins (opens Google Password Manager's list, by account name)"


def role(node):
    if node.get("dom"):
        return web_role(node)
    if node.get("inline"):
        return "Saved sign-in" if account_chip(node["inline"]) else "Password manager"
    names = [node.get("cls", ""), node.get("base", "")]
    if "edit" in node.get("f", []):
        return "Field"
    # The role a control declares to accessibility services comes first: React Native, Compose and
    # custom views name it there ("android.widget.CheckBox", "Toggle button") when their class says nothing.
    declared = node.get("a11y_cls", "").rsplit(".", 1)[-1]
    for needle, name in ROLES:
        if declared and needle in declared:
            return name
    if node.get("a11y_role"):
        return clean(node["a11y_role"])[:1].upper() + clean(node["a11y_role"])[1:]
    for needle, name in ROLES:
        if any(needle in n.rsplit(".", 1)[-1] for n in names if n):
            return name
    if container(node):
        return "List"
    short = names[0].rsplit(".", 1)[-1].split("$")[-1]
    # Obfuscated (Qz, le0) or plain layout classes say nothing about the element.
    if len(short) <= 3 or short in ("View", "ViewGroup", "FrameLayout", "LinearLayout", "RelativeLayout", "ConstraintLayout"):
        return "Item"
    return short


def app_class(node):
    """The app's own class name when it says more than the role (FollowButton), else None."""
    cls = node.get("cls", "")
    if node.get("virtual") or cls.startswith(("android.", "androidx.", "com.google.android.material.")):
        return None
    short = cls.rsplit(".", 1)[-1].split("$")[-1]
    return short if len(short) > 3 else None  # a, b, aB: obfuscated


def clean(s):
    """One line: Compose descriptions put each part on its own line ("Walmart
Groceries")."""
    return ", ".join(part for line in str(s).splitlines() if (part := " ".join(line.split())))


def own_text(node):
    """text and desc, once: launchers repeat the label as the description ("Phone · Phone")."""
    if node.get("dom") and {"edit", "select"} & set(node.get("f", [])):
        # A web field's text is its value (shown as value=); what names it is the label.
        return [clean(node["desc"])] if node.get("desc") else []
    words = [clean(v) for k in ("text", "desc") if (v := node.get(k))]
    if len(words) == 2 and (words[0] in words[1] or words[1] in words[0]):
        return [max(words, key=len)]
    return [w for w in words if w]


def container(node):
    """A list or scroll area: its children get their own lines, so it is not named after them."""
    flags = set(node.get("f", []))
    return "list" in flags or "items" in node or bool(flags & set(SCROLL_FLAGS)) or bool(node.get("webview"))


def clip_words(s, limit):
    return s if len(s) <= limit else s[:limit].rsplit(" ", 1)[0] + "…"


def clip_middle(s, limit, tail=50):
    """At most `limit` characters, keeping the end: a row's price, rating or time often comes last
    ("Ceramic mug, 12 oz, dishwasher safe, … · $39 · 4.8 stars")."""
    if len(s) <= limit:
        return s
    end = s[-tail:]
    end = end.split(" ", 1)[1] if " " in end[1:] else end
    return clip_words(s, limit - len(end) - 3).rstrip("…") + " … " + end


TIME_OF_DAY = re.compile(r"\b\d{1,2}:\d{2}\b")


def bubble_side(node, list_box):
    """A chat bubble's side: ("right"|"left", shows a time) for a list row as wide as its list whose words all sit
    against one side of it (Messages, WhatsApp, Telegram draw the user's messages on the right), else ()."""
    b = node.get("b")
    if not (isinstance(b, list) and isinstance(list_box, list)):
        return ()
    width = list_box[2] - list_box[0]
    if width < 300 or b[2] - b[0] < 0.9 * width:
        return ()
    boxes, words = [], []

    def walk(n):
        if (texts := own_text(n)) and isinstance(n.get("b"), list):
            boxes.append(n["b"])
            words.extend(texts)
        for c in n.get("c", []):
            walk(c)
    walk(node)
    if not boxes:
        return ()
    left, right = min(x[0] for x in boxes) - b[0], b[2] - max(x[2] for x in boxes)
    near, far = 0.08 * width, 0.2 * width
    side = "right" if right <= near and left >= far else "left" if left <= near and right >= far else ""
    return (side, bool(TIME_OF_DAY.search(" ".join(words)))) if side else ()


def hidden_behind(windows, i):
    """Whether unreadable window i is an app's screen covered by another of the same app's screens that is read
    (the step before, left open under the next one): nothing of it shows, so it is not listed."""
    w = windows[i]
    if w.get("kind") != "unreadable" or w.get("wm_type") != "BASE_APPLICATION" or w.get("focused") or not w.get("frame"):
        return False
    x1, y1, x2, y2 = w["frame"]
    return any(a.get("kind") == "activity" and a.get("package") == w.get("package") and (f := a.get("frame"))
               and f[0] <= x1 and f[1] <= y1 and f[2] >= x2 and f[3] >= y2 for a in windows[i + 1:])


def holds_anything(window):
    """Whether a window shows anything: words, or something to act on. An empty one (a dialog drawn
    with nothing in it, as Photos puts over its backup screen) hides nothing beneath it."""
    def walk(nodes):
        return any(own_text(n) or interactive(n) or n.get("webview") or walk(n.get("c", [])) for n in nodes)
    return walk(window.get("nodes", []))


@dataclass
class Element:
    n: int
    h: int
    node: dict
    window: str
    role: str
    label: str
    ctx: str = ""
    list_pos: str = ""
    flags: set = field(default_factory=set)
    process: str | None = None  # the bridge that owns this element; None: the focused one
    bubble: tuple = ()  # a chat bubble's (side, shows a time, its list) from bubble_side
    sender: str = ""  # "you" or "them": who wrote that chat message

    @property
    def key(self):
        return (self.process, self.window, self.node.get("id"), self.role, self.label, self.ctx, self.list_pos)

    @property
    def tappable(self):
        return bool(self.flags & TAP_FLAGS) and not {"disabled", "covered"} & self.flags

    @property
    def editable(self):
        return "edit" in self.flags and not {"disabled", "covered"} & self.flags

    @property
    def adjustable(self):
        """A slider, rating bar or number picker the bridge can set to a value directly."""
        return "adjust" in self.flags and not {"disabled", "covered"} & self.flags and not self.node.get("dom")

    @property
    def native_list(self):
        """A RecyclerView / ListView / GridView with a known row count, which can jump to its ends."""
        return ("list" in self.flags and not self.node.get("virtual") and not self.node.get("dom")
                and (self.node.get("items") or 0) > 1)

    @property
    def choosable(self):
        """A web <select>: the options are picked by name, not by opening a popup."""
        return "select" in self.flags and not {"disabled", "covered"} & self.flags and bool(self.node.get("choices"))

    def short_href(self):
        """Where a link goes, only as far as it helps: another site's host, or for a link with no name the
        first path segment. Same-site links with a name show nothing (the full URL stays in the node)."""
        href = self.node.get("href") or ""
        if href == "javascript:":
            return href
        if m := re.match(r"https?://([^/?#]+)", href):
            return m.group(1)
        if not self.label and (m := re.match(r"/[^/?#]+", href)):
            return "…" + m.group(0)
        return ""

    def summary(self, choices=True):
        # The app's own class only when nothing else names the element ("Button<FollowButton>" with no label).
        parts = [self.role + (f"<{c}>" if not self.label and (c := app_class(self.node)) and c != self.role else "")]
        if self.label:
            parts.append(f'"{self.label}"')
        if self.node.get("dom") and {"edit", "select"} & self.flags and self.node.get("text"):
            parts.append(f'value="{clip_words(clean(self.node["text"]), 80)}"')
        if self.node.get("hint") and self.node.get("hint") != self.label:
            parts.append(f'hint="{self.node["hint"]}"')
        if self.node.get("text_len"):  # the snapshot holds only its start
            parts.append(f"cut: only the start of its {self.node['text_len']} characters is shown, read_text([{self.n}]) reads all")
        if self.node.get("note"):  # the page's help text for it, or why it was rejected
            parts.append(f'note="{clip_words(clean(self.node["note"]), 120)}"')
        if self.node.get("id"):
            parts.append(f"id={self.node['id']}")
        if self.node.get("blocked"):  # the phone refuses this control whoever asks (the bridge's Guard)
            parts.append(f'blocked="{self.node["blocked"]}"')
        if self.node.get("name"):
            parts.append(f"name={self.node['name']}")
        if self.node.get("type"):
            parts.append(f"type={self.node['type']}")
        if self.node.get("icon"):
            parts.append(f"icon={self.node['icon']}")
        if href := self.short_href():
            parts.append(f"href={href}")
        if self.node.get("webview") and self.node.get("url"):
            parts.append(f'page="{self.node.get("title") or ""}" url={clip_words(self.node["url"], 90)}')
        if self.node.get("webview") and (wait := web_wait(self.node)):
            parts.append(f'waiting="{wait}"')
        if choices and self.node.get("choices"):
            shown = self.node["choices"][:12]
            parts.append("choices=[" + " | ".join(shown) + (f" | … {len(self.node['choices'])} in all" if len(self.node["choices"]) > 12 else "") + "]")
        if self.node.get("state"):
            parts.append(f"state={self.node['state']}")
        if self.node.get("progress"):
            parts.append(f"progress={self.node['progress']}")
        # A pager's "2/5". A web page's node keeps its load state under the same key, as a dict (chrome.py, the
        # bridge's Web.java): that is for the runtime, and printing it cost about 290 characters on every web turn.
        if isinstance(self.node.get("page"), str):
            parts.append(f"page={self.node['page']}")
        if self.node.get("items", -1) >= 0:
            parts.append(f"items={self.node['items']}" + rows_shown(self.node))
        if self.list_pos:
            parts.append(f"list={self.list_pos}")
        if self.sender:
            parts.append(f"from={self.sender}")
        if self.ctx:
            parts.append(f'ctx="{self.ctx}"')
        shown = [f for f in SHOWN_FLAGS if f in self.flags]
        shown += [f"scroll {d}" for f, d in SCROLL_FLAGS.items() if f in self.flags]
        if shown:
            parts.append("{" + ", ".join(shown) + "}")
        if self.node.get("actions"):
            parts.append("actions=" + "/".join(self.node["actions"]))
        return " ".join(parts)


@dataclass
class Screen:
    text: str
    elements: dict
    snapshot: dict
    header: dict

    def find(self, key):
        return next((e for e in self.elements.values() if e.key == key), None)


def rows_shown(node):
    """Which of a list's rows are on screen, beside its count, so "only three" is never read off a list of 46,
    and which way the rest lie: a chat opens at its newest message, its last row, with the older ones above
    (2026-11-19: a subagent read "items=5 (3 on screen)" as rows still below, and scrolled for minutes for
    messages that were not there). '' when every row is shown."""
    items, rows = node["items"], node.get("c", [])
    if not 0 < len(rows) < items:
        return ""
    placed = sorted(r["li"] for r in rows if isinstance(r.get("li"), int))
    if not placed:
        return f" ({len(rows)} on screen)"
    first, last = placed[0] + 1, placed[-1] + 1
    shown = f"rows {first}–{last}" if last > first else f"row {first}"
    if last >= items and first > 1:
        return f" ({shown} on screen: the end; the rest are above)"
    if first == 1 and last < items:
        return f" ({shown} on screen: the start; the rest are below)"
    return f" ({shown} on screen)"


def interactive(node):
    flags = set(node.get("f", []))
    return bool(flags & (TAP_FLAGS | {"long", "edit", "select", "adjust"} | set(SCROLL_FLAGS)))


ADAPTER_LISTS = {"ListView", "GridView", "ExpandableListView"}


def adapter_rows(nodes):
    """A ListView or GridView takes a tap on a row itself and hands it to the row it lands on (onItemClick), so
    its rows are not clickable on their own: the file picker's "Open from" drawer, many dropdowns and popup
    menus. Each row that shows words is marked tappable, in place, as devices.accessibility_nodes does for
    lists read through accessibility; a tap is a touch at the row's centre, which the list then takes."""
    for node in nodes:
        children = node.get("c", [])
        kinds = {str(node.get(k) or "").rsplit(".", 1)[-1] for k in ("cls", "base", "a11y_cls")}
        if "list" in node.get("f", []) and kinds & ADAPTER_LISTS:
            for row in children:
                flags = row.get("f", [])
                if row.get("li") is not None and not set(flags) & TAP_FLAGS and "disabled" not in flags \
                        and (own_text(row) or subtree_texts(row)):
                    row["f"] = [*flags, "click"]
        adapter_rows(children)


def subtree_texts(node, skip=None):
    """Visible words in a subtree in reading order, skipping the subtree rooted at `skip` and
    any interactive descendant's own words (they get their own line)."""
    out = []

    def walk(n, top):
        if n is skip:
            return
        if top or not interactive(n):
            out.extend(own_text(n))
        for c in n.get("c", []):
            walk(c, False)

    walk(node, True)
    return list(dict.fromkeys(out))


def walk_nodes(nodes):
    for node in nodes:
        yield node
        yield from walk_nodes(node.get("c", []))


def web_wait(node):
    """What a web page is still waiting for after the last tap, in words, or "": the page its tab has asked for
    (chrome.py `awaiting`), or the site's answer to what the tap sent (`waiting`)."""
    if (awaiting := node.get("awaiting")) is not None:
        return "the next page" + (f" ({awaiting['url']})" if awaiting.get("url") else "") + " has not arrived yet"
    if waiting := node.get("waiting"):
        return "the site has not answered " + ", ".join(waiting) + " yet"
    return ""


def window_key(w):
    """What a window is, across two reads: a window whose key the read before an action lacked has just opened."""
    return (w.get("package") or w.get("process"), w.get("kind"), w.get("title"), w.get("wm_type"))


def loading(snapshot, new_screen=False, opened=()):
    """Why the screen is not yet what the last action brought up, or None. Three signs are sure enough to wait on
    (device.driver.observe, bounded by loading_wait_seconds): the app in front is between two screens (the window
    manager lists its window while its bridge does not yet, or the settle ended with a screen still opening); a
    spinner that stands where content will be (nothing readable or listed beside it in its list, pager, scroll
    area or window: controls do not count, a chat's composer is there before its messages); and content areas
    (lists, pagers, scroll areas) with nothing in them yet, as a list that says it has rows and shows none, or, on
    a screen that has just opened (`new_screen`), no words at all. A spinner beside content (a picture still
    loading in a message, a sync mark in a header) is not one: many apps keep one running, and the agent reads the
    rest. A web page is otherwise left to its own loading signal (runtime.phone.watch_web, read_page), but for what
    Chrome itself reports of the last tap (`web_wait`): the tab has asked for another page that has not arrived,
    or the page sent the site a request that has no answer yet. Until then the page shows the tap's effect
    nowhere, and a second tap would do the thing twice.

    A window that has just opened over the same screen (`opened`: their window_key) counts as a new screen: a
    sheet's window is there a moment before what it holds. Read then, a home-services app's booking sheet was a bare
    Close button, and the agent closed it as the wrong item again and again (2026-11-07)."""
    windows = snapshot.get("windows", [])
    focused = next((w for w in reversed(windows) if w.get("focused")), None)
    if focused and focused.get("kind") == "unreadable" and focused.get("why") == "the bridge does not list this window":
        return "the app is opening a screen"
    settle = snapshot.get("settle") or {}
    if settle.get("settled") is False and settle.get("busy") == "opening a screen":
        return "the app is opening a screen"
    process = snapshot.get("process")
    if snapshot.get("web_page"):
        for w in windows:
            for node in walk_nodes(w.get("nodes", [])):
                if node.get("webview") and (wait := web_wait(node)):
                    return wait
    for w in windows:
        # An empty popup (Teams opens two over a chat) says nothing about the screen.
        if w.get("kind") in ("unreadable", "keyboard") or (process and w.get("process") != process) or not w.get("nodes"):
            continue
        if spinner_stands_for_content(w.get("nodes", [])):
            return "a spinner stands where the content will be"
        if not snapshot.get("web_page") and content_pending(w.get("nodes", []), new_screen or window_key(w) in opened):
            return "its content areas are still empty"
    return None


def is_content(node):
    """Words to read or a listed item, as opposed to a control: a screen with only controls has not got its content.
    Text is drawn words; a description alone names an image or a control (a microphone's "Record voice message")."""
    return node.get("li") is not None or (bool(node.get("text")) and not interactive(node))


def content_pending(nodes, new_screen=False):
    """Nothing to read yet: the window has content areas (lists, pagers, scroll areas) and none holds anything,
    or, on a screen that has just opened, no words or listed items at all (controls only, as a chat's header and
    composer before its messages, whose list the bridge does not yet mark as one).

    A field's own text is content too (a note open in an editor is one field holding the whole note), and an area
    with two or more fields is a form, which has what it shows even while they are empty. Taken for empty, a note
    editor and a new-item form were each waited on for the full loading wait and handed over as still loading,
    and the agent left the editor for the shell (2026-11-15). A chat's lone empty composer is still no content."""
    found = []

    def walk(node, inside):
        if container(node) and not node.get("webview") and not inside:
            found.append(any(has_content(c) for c in node.get("c", [])) or fields(node) >= 2)
            return
        for c in node.get("c", []):
            walk(c, inside)

    def is_field(n):
        return "edit" in n.get("f", [])

    def fields(n):
        return sum(is_field(c) + fields(c) for c in n.get("c", []))

    def has_content(n):
        return is_content(n) or (is_field(n) and bool(n.get("text"))) or any(has_content(c) for c in n.get("c", []))

    if new_screen and not any(has_content(n) for n in nodes) and sum(is_field(n) + fields(n) for n in nodes) < 2:
        return True
    for n in nodes:
        walk(n, False)
    return bool(found) and not any(found)


def spinner_stands_for_content(nodes):
    """A spinner whose nearest list, pager, scroll area or window holds nothing else readable or usable."""
    root = {"c": nodes}

    def content_besides(holder, spinner):
        def any_content(n):
            if n is spinner:
                return False
            if n is not holder and is_content(n):
                return True
            return any(any_content(c) for c in n.get("c", []))
        return any_content(holder)

    def walk(node, holder):
        if "spinner" in node.get("f", []) and not content_besides(holder, node):
            return True
        inner = node if container(node) else holder
        return any(walk(c, inner) for c in node.get("c", []))

    return walk(root, root)


def area(b):
    return max(0, b[2] - b[0]) * max(0, b[3] - b[1])


def reading_order(nodes):
    """Text nodes as a person reads them: line by line (a centre within half a line counts as the same
    line), left to right."""
    return sorted(nodes, key=lambda n: ((n["b"][1] + n["b"][3]) // 2 // 30, n["b"][0]))


def adoptions(roots):
    """Words drawn apart from what they belong to. Some screens put every word in one branch of the tree
    and the cells you tap in another (Compose screens that draw text in Android views through
    AndroidViewsHandler; custom views with a click layer drawn over their labels): only their positions tie
    them together, so the cells read as unlabelled look-alikes and the words as a heap at the top. Each
    such loose text goes to the smallest node of another branch whose bounds hold its centre, when that
    node is smaller than the text's own parent: a tab or date cell gets its words, a list row its title.
    Screens whose words sit inside what they label are left as they are.
    Returns ({id(host): [text nodes in reading order]}, {id(text node)})."""
    texts, hosts = [], []

    def walk(node, parent, path):
        b = node.get("b")
        usable = isinstance(b, list) and len(b) == 4 and not node.get("dom") and area(b) > 0
        children = node.get("c", [])
        if (usable and parent is not None and isinstance(parent.get("b"), list) and not children
                and not interactive(node) and own_text(node)):
            texts.append((node, parent, path))
        if usable and (interactive(node) or node.get("virtual") or node.get("li") is not None):
            hosts.append(node)
        for child in children:
            walk(child, node, path | {id(node)})

    for root in roots:
        walk(root, None, frozenset())
    adopted, claimed = {}, set()
    if not texts or not hosts:
        return adopted, claimed
    hosts = sorted(((area(h["b"]), h) for h in hosts), key=lambda pair: pair[0])  # smallest first: the first fit wins
    for text, parent, path in texts:
        b = text["b"]
        cx, cy = (b[0] + b[2]) // 2, (b[1] + b[3]) // 2
        limit = area(parent["b"])
        for size, host in hosts:
            if size >= limit:
                break
            h = host["b"]
            if id(host) not in path and h[0] <= cx <= h[2] and h[1] <= cy <= h[3]:
                adopted.setdefault(id(host), []).append(text)
                claimed.add(id(text))
                break
    return {k: reading_order(v) for k, v in adopted.items()}, claimed


def render(snapshot, events=()):
    """Build the Screen for the phone agent. `events` are bridge events since the last action."""
    elements = {}
    lines = []
    numbered = []  # (element, node, ancestors) for the context pass
    bubbles = {}  # (window, list) -> the sides its rows' chat bubbles sit on
    lent = {}  # id(node) -> the words drawn elsewhere that belong to it (adoptions), for the context pass
    windows = snapshot.get("windows", [])  # bottom to top

    def reachable_at(i):
        """A dialog blocks the windows of its own app beneath it, not other apps' windows
        (a picture-in-picture video stays usable under another app's dialog)."""
        process = windows[i].get("process")
        return not any(w.get("kind") == "dialog" and w.get("process") == process and holds_anything(w)
                       for w in windows[i + 1:])

    several_apps = len({w.get("process") for w in windows if w.get("process") and w.get("kind") != "keyboard"}) > 1

    activity = (snapshot.get("activity") or "?").rsplit(".", 1)[-1]
    fragments = ", ".join(f.rsplit(".", 1)[-1] for f in snapshot.get("fragments", []))
    header = {
        "app": snapshot.get("pkg"), "activity": snapshot.get("activity"),
        "fragments": snapshot.get("fragments", []), "keyboard": snapshot.get("ime_visible"),
    }
    lines.append(f"app: {snapshot.get('pkg')}  activity: {activity}" + (f"  fragments: {fragments}" if fragments else ""))
    lines.append(f"keyboard: {'shown' if snapshot.get('ime_visible') else 'hidden'}")
    if events:
        lines.append("since last action:")
        lines.extend("  " + describe_event(e) for e in collapse(events))

    for index in reversed(range(len(windows))):
        w = windows[index]
        if hidden_behind(windows, index):
            continue
        reachable = reachable_at(index) and w.get("kind") != "unreadable"
        title = w.get("kind", "window")
        if w.get("kind") == "keyboard":
            # The keyboard's keys are the user's; its saved-sign-in chips are the only part the agent acts on.
            chips = inline_nodes(w.get("nodes", []))
            if not chips:
                continue
            w = {**w, "nodes": chips}
            title = "keyboard: saved sign-ins offered by Google Password Manager (chips, in its order)"
        elif several_apps and w.get("package"):
            title = f"{w['package']} {title}"
        if w.get("mode") == "pinned":
            title += " (picture-in-picture)"
        elif w.get("mode") == "multi-window":
            title += " (split screen)"
        if w.get("frame") and w.get("mode") in ("pinned", "multi-window", "freeform"):
            x1, y1, x2, y2 = w["frame"]
            title += f" at [{x1},{y1}][{x2},{y2}]"
        if w.get("kind") == "unreadable":
            title += f": {w.get('title')} ({w.get('wm_type')}; not readable, {w.get('why')})"
        elif w.get("read_by") == "accessibility":
            title += " (read through accessibility: its elements take taps only)"
        elif not reachable:
            title += " (behind the dialog; not reachable)"
        if w.get("focused") and len(windows) > 1:
            title += " (focused)"
        if index == len(windows) - 1 and len(windows) > 1:
            title += " (on top)"
        lines.append(f"--- {title} ---")
        start = len(lines)
        adapter_rows(w.get("nodes", []))
        adopted, claimed = adoptions(w.get("nodes", []))
        lent.update({key: [t for text in texts for t in own_text(text)] for key, texts in adopted.items()})

        def adopted_words(node):
            return list(dict.fromkeys(t for text in adopted.get(id(node), []) for t in own_text(text)))

        def walk(node, depth, ancestors, list_info, covered):
            flags = set(node.get("f", []))
            li = node.get("li")
            if li is not None and list_info:
                list_info = {**list_info, "row": li}
            if "list" in flags or "items" in node:
                list_info = {"name": node.get("id") or "list", "items": node.get("items"), "b": node.get("b"),
                             "window": index}
            indent = "  " * min(depth, 8)
            # A text too long for a snapshot (a note, a long message, an article) is numbered too, so read_text
            # can read it whole: shown cut, it read as the note's first lines (2026-11-15).
            if reachable and (interactive(node) or (node.get("text_len") and not node.get("dom"))):
                n = len(elements) + 1
                label_words = own_text(node) or ([] if container(node) else subtree_texts(node)[:4])
                if node.get("inline"):
                    label_words = [inline_label(node)]
                if not own_text(node) and not container(node) and (extra := adopted_words(node)):
                    label_words = list(dict.fromkeys([*label_words, *extra]))[:6]
                el = Element(
                    n=n, h=node["h"], node=node, window=w.get("kind", ""), role=role(node),
                    label=clip_middle(" · ".join(label_words), 160), flags=flags, process=w.get("process"),
                    list_pos=f"{list_info['name']}#{list_info['row'] + 1}"
                    + (f"/{list_info['items']}" if (list_info.get("items") or 0) > 0 else "")
                    if list_info and "row" in list_info and (list_info.get("items") or 0) != 1 else "",
                )
                if list_info and "row" in list_info:
                    if side := bubble_side(node, list_info.get("b")):
                        key = (list_info["window"], list_info["name"])
                        el.bubble = (*side, key)
                        bubbles.setdefault(key, set()).add(side[0])
                elements[n] = el
                numbered.append((el, node, ancestors))
                lines.append(f"{indent}[{n}] {{SUMMARY:{n}}}")
                printed = True
                covered = covered | set(label_words)  # its children's words are already in the label
                if container(node) and (extra := [t for t in adopted_words(node) if t not in covered]):
                    lines.append(f'{indent}  "{" · ".join(extra)[:200]}"')
            else:
                # Words drawn elsewhere that belong here (adoptions) come first; words this node lent to
                # another are printed there, not here.
                words = [] if id(node) in claimed else [t for t in own_text(node) if t not in covered]
                if extra := [t for t in adopted_words(node) if t not in covered and t not in words]:
                    words = [*extra, *words]
                borrowed = {f for text in adopted.get(id(node), []) for f in text.get("f", [])}
                marks = [f for f in ("spinner", "selected", "checked", "disabled") if f in flags | borrowed]
                printed = bool(words) or "spinner" in flags
                if "blocked_frame" in flags:  # content the page script cannot reach: say so, don't hide it
                    printed = True
                    lines.append(f'{indent}(frame "{node.get("desc") or "?"}": cross-origin, its content is not readable)')
                elif words:
                    extra = f"  ·{node['id']}" if node.get("id") else ""
                    lead = LEADS.get(node.get("wrole"), "")
                    lines.append(f'{indent}{lead}"{" · ".join(words)[:200]}"{extra}' + (f" {{{', '.join(marks)}}}" if marks else ""))
                elif "spinner" in flags:
                    lines.append(f"{indent}(loading spinner)")
                elif node.get("webview"):  # a page that fits its WebView has no scroll flag to number it by
                    printed = True
                    lines.append(f'{indent}(web page "{node.get("title") or ""}" {clip_words(node.get("url") or "", 90)}'
                                 + (", loading" if "loading" in flags else "")
                                 + (f": {wait}" if (wait := web_wait(node)) else "") + ")")
                elif node.get("wrole") == "dialog":
                    printed = True
                    lines.append(f"{indent}(dialog)")
                elif "loading" in flags and node.get("dom"):  # aria-busy: said, never waited on
                    printed = True
                    lines.append(f"{indent}(loading)")
            for child in node.get("c", []):
                walk(child, depth + printed, ancestors + [node], list_info, covered)

        for node in w.get("nodes", []):
            walk(node, 0, [], None, set())
        if len(lines) == start and reachable:
            lines.append("  (nothing readable in this window)")
        if not reachable and len(lines) - start > 12:
            hidden = len(lines) - start - 12
            del lines[start + 12:]
            lines.append(f"  … {hidden} more lines behind the dialog")

    # Who wrote each chat message: the right side is the user's, the left the other person's, when the list has
    # bubbles on the other side too or the row shows a time as messages do. A settings row hugs the left, and a
    # menu row (an icon, then a title stretched to the edge) reads as hugging the right.
    for el in elements.values():
        if el.bubble:
            side, timed, key = el.bubble
            other = "left" if side == "right" else "right"
            if timed or other in bubbles[key]:
                el.sender = "you" if side == "right" else "them"

    # Context only where it disambiguates: same role + label appearing more than once.
    tally = {}
    for el, _, _ in numbered:
        tally[(el.role, el.label)] = tally.get((el.role, el.label), 0) + 1
    for el, node, ancestors in numbered:
        if (tally[(el.role, el.label)] > 1 or not el.label) and not container(node):
            el.ctx = clip_words(context(node, ancestors, lent), 80)

    text = "\n".join(lines)
    for n, el in elements.items():
        text = text.replace(f"{{SUMMARY:{n}}}", el.summary(), 1)
    if not windows:
        lines.append("(no app window is visible)")
    if snapshot.get("truncated"):
        text += "\n(screen truncated by the bridge at 1500 nodes)"
    if len(text) > MAX_SCREEN_CHARS:
        text = text[:MAX_SCREEN_CHARS] + "\n… (screen text truncated)"
    return Screen(text=text, elements=elements, snapshot=snapshot, header=header)


def inline_nodes(nodes):
    """The keyboard's autofill chips, in reading order (Google's), without the keys around them; the account chips
    numbered among themselves."""
    found = []

    def walk(nodes):
        for node in nodes:
            if node.get("inline"):
                found.append({k: v for k, v in node.items() if k != "c"})
            else:
                walk(node.get("c", []))

    walk(nodes)
    chips = reading_order(found)
    accounts = [c for c in chips if account_chip(c["inline"])]
    for index, c in enumerate(accounts, 1):
        c["inline"] = {**c["inline"], "index": index, "of": len(accounts)}
    return chips


def context(node, ancestors, lent=None):
    """Words near a node: from the nearest ancestor that holds other text (its own, or words drawn
    elsewhere that belong to it), stopping at a list row so one row's context never leaks into another's."""
    lent = lent or {}
    for ancestor in reversed(ancestors):
        words = [t for t in dict.fromkeys([*lent.get(id(ancestor), []), *subtree_texts(ancestor, skip=node)])
                 if t not in own_text(node)]
        if words:
            return " · ".join(words[:2])
        if ancestor.get("li") is not None:
            break
    return ""


def describe_event(e):
    """One line for an event; a collapsed repeat says how many times it happened."""
    text = event_line(e)
    n = e.get("count", 1)
    return text if n == 1 or e.get("type") == "http" else f"{text} (x{n})"


def event_line(e):
    t = e.get("type")
    if t == "toast":
        return f'toast "{e.get("text")}"'
    if t == "snackbar":
        return f'snackbar "{e.get("text")}"'
    if t == "dialog_shown":
        return f'dialog opened: "{e.get("text")}"'
    if t == "dialog_dismissed":
        return "dialog closed"
    if t == "popup_shown":
        return f'popup opened: "{e.get("text")}"'
    if t == "activity_resumed":
        return f"screen changed to {str(e.get('activity')).rsplit('.', 1)[-1]}"
    if t == "inline_suggestions":
        n, actions = e.get("suggestions") or 0, e.get("actions") or 0
        if actions > 1:  # an older bridge counted the accounts Google confirms first with its key
            return f"the keyboard offers {n + actions} chips from Google Password Manager: numbered elements in the keyboard window"
        chips = f"{n} saved sign-in chip{'s' if n != 1 else ''}" if n else "no saved sign-in"
        more = " and its key for the full list" if actions else ""
        return f"the keyboard offers {chips} from Google Password Manager{more}: numbered elements in the keyboard window"
    if t == "fragment_resumed":
        return f"section shown: {str(e.get('fragment')).rsplit('.', 1)[-1]}"
    if t == "http":
        status = e.get("code") or e.get("error")
        path = str(e.get("url", "")).split("?")[0]
        if e.get("count", 1) > 1:
            return f"network {e.get('method')} {path} -> {status} (x{e['count']})"
        return f"network {e.get('method')} {path} -> {status} ({e.get('ms')}ms)"
    if t == "log_error":
        return f"app logged an error [{e.get('tag')}]: {e.get('message') or ''} {e.get('exception') or ''}".strip()
    if t == "crash":
        return f"APP CRASHED: {e.get('exception')}: {e.get('message')}"
    if t == "intent":
        return f"opened {e.get('component') or e.get('action')} {e.get('data') or ''}".strip()
    if t == "web_page":
        return f'page loaded: "{e.get("title") or ""}" {str(e.get("url") or "")[:90]}'.strip()
    if t == "web_error":
        return f"page failed to load {str(e.get('url') or '')[:90]}: {e.get('error')} ({e.get('code')})"
    if t == "js_dialog":
        return f'page {e.get("kind")}: "{e.get("text")}"'
    if t == "notification":
        who = e.get("label") or e.get("app")
        words = " - ".join(str(x) for x in (e.get("title"), e.get("text")) if x)
        return f"notification from {who}: {words}".rstrip(": ")
    if t == "notification_removed":
        return f"notification from {e.get('app')} dismissed: {e.get('title') or ''}".rstrip(": ")
    if t == "ops":
        return f"operator: {e.get('text')}"
    if t == "refused":
        return f"the phone refused {e.get('what')}: {e.get('reason')}"
    if t == "failed_request":
        return f"the app's request {e.get('method')} {e.get('where')} failed: HTTP {e.get('code')}"
    if t == "failed_requests_more":
        return f"{e.get('n')} more of the app's requests failed"
    return f"{t}: {e}"


def refusals(events, how):
    """Lines for what the phone refused since the action (a key the Guard swallowed), unless `how`, the action's
    own report, already says why: a refused tap fails with the reason itself."""
    return [describe_event(e) for e in collapse(events)
            if e.get("type") == "refused" and str(e.get("reason")) not in how]


WRITES = ("POST", "PUT", "PATCH", "DELETE")
FAILED_REQUESTS_SHOWN = 3
# Where apps report on themselves (tracking, logs, crash and ad services), by the words of the host and path. It
# errs on the side of hiding: a request it wrongly takes for one of these is not shown, as before.
REPORTING = re.compile(
    r"track|analytic|telemetr|metric|beacon|clickstream|eventstore|deviceevents|sdklog|pixel|crashlytic|sentry|bugsnag|"
    r"datadog|mixpanel|amplitude|appsflyer|app-measurement|doubleclick|googleads|"
    r"(^|[./_-])(logs?|logging|collect|ads?|adtech|adreq|traces?|rum|heartbeat|ping)([./_-]|$)", re.I)


def failed_request(e):
    """A network call the agent hears about, as its own event, or None. Only a request that sends something
    (not a GET) and that the app's server answered with an error (4xx, 5xx): what a tap did when the screen does
    not say (a coupon the server refused, a payment it declined). Every other call stays out: in seven captures
    of five apps being browsed (2026-11-22), all 300 requests that were not GETs were tracking, ads, settings or
    pages loaded by POST, every one answered 2xx, and the only errors were GETs for pictures and manifests. So a
    turn in which nothing failed reads exactly as it did before. The address is the host and path, never the
    query, with long numbers (an order, a phone number) left out."""
    code = e.get("code")
    # 400-599 only: apps also build responses of their own with numbers that are not HTTP's (one shopping app: 1022).
    if (e.get("type") != "http" or str(e.get("method", "")).upper() not in WRITES or not isinstance(code, int)
            or not 400 <= code <= 599):
        return None
    address = str(e.get("url", "")).split("?")[0].split("#")[0].split("://", 1)[-1]
    if REPORTING.search(address):  # a tracker that fails means nothing for the task, and some fail all day
        return None
    return {"type": "failed_request", "method": str(e.get("method")).upper(), "code": code, "t": e.get("t", 0),
            "where": re.sub(r"\d{6,}", "…", address)[:100]}


def events_for_agent(events):
    """What the agent hears about: what the app showed (toasts, snackbars, dialogs, popups, pages),
    crashes and notifications. Network calls, intents, fragments and Log.e lines stay in the raw log
    for the human: they are the app's internals, and most of what fails there (analytics) means
    nothing for the task. The one exception is a request the app's server refused (failed_request), at most
    FAILED_REQUESTS_SHOWN different ones a look."""
    kept, failed = [], []
    for e in events:
        if e.get("type") == "http":
            if (f := failed_request(e)) is not None:
                key = (f["method"], f["where"], f["code"])
                if key not in failed:
                    failed.append(key)
                if failed.index(key) < FAILED_REQUESTS_SHOWN:  # repeats of a shown one are counted by collapse
                    kept.append(f)
        elif e.get("type") not in ("intent", "fragment_resumed", "log_error"):
            kept.append(e)
    if len(failed) > FAILED_REQUESTS_SHOWN:
        kept.append({"type": "failed_requests_more", "n": len(failed) - FAILED_REQUESTS_SHOWN})
    return kept


def collapse(events):
    """One entry per repeated event, at its first position, with a count: the same toast or failed
    call a hundred times is one line, not a hundred."""
    merged = {}
    for e in events:
        if e.get("type") == "http":
            sig = ("http", e.get("method"), str(e.get("url", "")).split("?")[0], e.get("code"), e.get("error"))
        else:
            sig = (e.get("type"), event_line(e))
        if sig in merged:
            merged[sig]["count"] += 1
        else:
            merged[sig] = {**e, "count": 1}
    return list(merged.values())


def options(screen, inputs):
    """The phone agent's choice menu: one entry per possible action on a numbered element, plus navigation
    and terminal statuses. Each maps to a bridge command the session can execute."""
    menu = {}
    for n, el in screen.elements.items():
        s = el.summary()
        # Virtual nodes (Compose, web) carry no listener flag, only "click"; a covered or disabled web element
        # cannot be tapped by anyone, so it offers nothing (the line still shows why).
        dead = "covered" in el.flags or (el.node.get("dom") and "disabled" in el.flags)
        # A list whose rows are listed is never tapped itself: the tap lands on whatever row is in its middle. A
        # popup menu's list tapped so opened "Delete track?" in OpenTracks (2026-11-15).
        rows_listed = container(el.node) and any(c.get("li") is not None and set(c.get("f", [])) & TAP_FLAGS
                                                 for c in el.node.get("c", []))
        if (el.tappable or el.node.get("virtual") and "click" in el.flags and not dead) and not rows_listed:
            menu[f"tap_{n}"] = (f"Tap [{n}] {s}", {"cmd": "click", "n": n})
        if el.choosable:
            for i, choice in enumerate(el.node["choices"][:25]):
                menu[f"pick_{n}_{i}"] = (
                    f"Choose {choice!r} in [{n}] {el.summary(choices=False)}",
                    {"cmd": "select", "n": n, "text": choice},
                )
        if "long" in el.flags and "disabled" not in el.flags:
            menu[f"long_{n}"] = (f"Long press [{n}] {s}", {"cmd": "long_click", "n": n})
        if el.editable:
            for i, value in enumerate(inputs):
                menu[f"type_{n}_{i}"] = (
                    f"Type {value!r} into [{n}] {s} (replaces what the field holds)",
                    {"cmd": "set_text", "n": n, "text": value},
                )
        if el.editable:
            # Free text (the model chooses it): the value is filled in when the call is resolved (tools.resolve).
            menu[f"type_{n}"] = (f"Type text into [{n}] {s} (replaces what the field holds)", {"cmd": "set_text", "n": n, "text": None})
        if el.editable:
            menu[f"focus_{n}"] = (f"Focus [{n}] {s} without typing", {"cmd": "focus", "n": n})
            # A Compose field is a virtual node: the bridge cannot send its keyboard action, Enter does it.
            # A web field is submitted by its page (web.js: Enter on the field, then its form).
            compose = el.node.get("virtual") and not el.node.get("dom")
            menu[f"submit_{n}"] = (
                f"Press the keyboard's action key (search / next / done) in [{n}] {s}",
                {"key": "enter", "label": "Enter"} if compose else {"cmd": "ime_action", "n": n},
            )
        for flag, direction in SCROLL_FLAGS.items():
            if flag in el.flags:
                menu[f"scroll_{direction}_{n}"] = (f"Scroll {direction} in [{n}] {s}", {"cmd": "scroll", "n": n, "dir": direction})
        if el.native_list and "can_scroll_up" in el.flags:
            menu[f"scroll_top_{n}"] = (f"Jump to the first row of [{n}] {s}", {"cmd": "scroll_to", "n": n, "pos": "top"})
        if el.native_list and "can_scroll_down" in el.flags:
            menu[f"scroll_bottom_{n}"] = (f"Jump to the last row of [{n}] {s}", {"cmd": "scroll_to", "n": n, "pos": "bottom"})
        if el.adjustable:
            for word, text in (("min", "its minimum"), ("max", "its maximum"), ("up", "one step up"), ("down", "one step down")):
                menu[f"value_{word}_{n}"] = (f"Set [{n}] {s} to {text}", {"cmd": "set_value", "n": n, "value": word})
    menu = dict(list(menu.items())[:236])
    # The phone's own navigation keys, named android_* so they are never mistaken for an app's
    # Back arrow, Navigate up or Home tab, which are numbered elements above.
    menu["android_back"] = (
        "Press the phone's system Back key (Android navigation bar), not a back arrow inside the app",
        {"key": "back", "label": "Android Back"},
    )
    menu["android_home"] = (
        "Press the phone's system Home key: leaves the app for the Android home screen (launcher), where other apps' "
        "icons are. The way to open an app the goal names that is not on screen. Not the app's own Home tab, logo or Navigate up",
        {"key": "home", "label": "Android Home"},
    )
    menu["android_recents"] = (
        "Press the phone's system Recents key: shows the Android recent-apps overview to switch apps",
        {"key": "recents", "label": "Android Recents"},
    )
    for direction in ("up", "down", "left", "right"):
        menu[f"swipe_{direction}"] = (f"Swipe a finger {direction} across the screen", {"gesture": direction})
    menu["enter"] = ("Press the Enter key", {"key": "enter", "label": "Enter"})
    if screen.header.get("keyboard"):
        menu["hide_keyboard"] = (
            "Hide the on-screen keyboard without leaving the screen (the system Back key may leave it instead)",
            {"cmd": "hide_keyboard"},
        )
    menu["done"] = (
        "The goal is done: its result is visible on screen or in the events since the last action.",
        {"status": "completed"},
    )
    menu["needs_input"] = (
        "A missing input value, private verification code, external device approval, or material user decision is required.",
        {"status": "needs_input"},
    )
    menu["blocked"] = ("The screen shows a permanent restriction or inaccessible UI. Do not bypass it.", {"status": "blocked"})
    menu["error"] = (
        "The screen or events show an error that prevents continuing, or the goal needs an app/page that is not on screen "
        "and does not ask you to open it.",
        {"status": "error"},
    )
    return menu


def typed_differs(typed, now, at=None):
    """What a text field holds after a type, when that is not what was typed (a number-only field that dropped the
    words, a length limit, a mask that reformatted it), else None. Read back by the bridge at the moment of typing,
    so even a burst of types without a look shows it. A private entry is never echoed back, nor compared."""
    if not isinstance(typed, str) or not isinstance(now, str) or "{private:" in typed:
        return None
    if at == "start" and now.startswith(typed) or at == "end" and now.endswith(typed):
        return None  # added to what the field held: the rest is its own
    if not at and (now == typed or now.strip() == typed.strip()):
        return None
    return now if len(now) <= 80 else now[:77] + "..."


def performed(action, target, response):
    """What a bridge command did, in the words the phone agent sees in state.last_result:
    "tapped [4] btn_add via performClick", "hid the keyboard via hideSoftInput"."""
    cmd = action["cmd"]
    via = response.get("via", "?")
    if target is None:  # a command on the whole screen, not on a numbered element
        text = {"hide_keyboard": "hid the keyboard"}.get(cmd, cmd) + f" via {via}"
    else:
        verb = {"click": "tapped", "long_click": "long-pressed", "set_text": "typed into", "focus": "focused",
                "insert_text": f"added at the {action.get('at')} of",
                "select": f"chose {action.get('text', '')!r} in", "scroll": f"scrolled {action.get('dir', '')} in",
                "ime_action": "pressed the keyboard action in", "scroll_to": f"jumped to the {action.get('pos')} of",
                "set_value": f"set to {action.get('value')}:"}[cmd]
        label = target.node.get("id") or target.label[:40] or target.role
        text = f"{verb} [{target.n}] {label}" + (f" ({target.list_pos})" if target.list_pos else "") + f" via {via}"
        if cmd == "set_value" and response.get("ok", True) and "to" in response:
            text += f" ({response.get('from')} -> {response['to']})"
            if response.get("at_limit"):
                text += " — it was already at the limit of its range; nothing more to set"
            elif response.get("limited"):
                text += " — the control stopped there: that is the limit of its range"
    if not response.get("ok", True):
        text += f" — FAILED: {response.get('error')}"
    elif cmd == "insert_text" and response.get("via") != "insert":
        text += " — FAILED: the text was not added (this field or bridge cannot add to its text): type the whole text"
    elif cmd in ("set_text", "insert_text") and (held := typed_differs(action.get("text"), response.get("now"), action.get("at"))) is not None:
        text += f" — the field now holds {held!r}, not what was typed: the app changed or dropped part of it"
    elif response.get("opening"):  # a web tap after which the tab asked for another page (chrome.py click)
        text += f", which asked for {response['opening']}"
    return text


def web_pages(screen: Screen):
    """(title, url) of each WebView page on the screen, so a navigation inside a page is noticed."""
    found = []

    def walk(node):
        if node.get("webview"):
            found.append((node.get("title") or "", node.get("url") or ""))
        for child in node.get("c", []):
            walk(child)

    for w in screen.snapshot.get("windows", []):
        for node in w.get("nodes", []):
            walk(node)
    return found


def body(text):
    """Screen lines below the header."""
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith("---")), 0)
    return lines[start:]


def selected_labels(screen: Screen):
    """The labels of the numbered elements marked selected (a tab, a date, a chip), in screen order."""
    return list(dict.fromkeys(e.label for e in screen.elements.values() if "selected" in e.flags and e.label))


def selection_moved(before: Screen, after: Screen, target=None):
    """"selected now: Sun · 11 · Apr (was Sat · 10 · Apr)" when the selection moved somewhere other than the
    target (whose own flags are reported already): a swipe on a pager or a tap elsewhere can change the tab,
    the date or the filter the screen shows, and the text alone would not say so. "" when it did not move."""
    own = target.label if target is not None else None
    was = [label for label in selected_labels(before) if label != own]
    now = [label for label in selected_labels(after) if label != own]
    if set(was) == set(now) or not now:
        return ""
    shown = ", ".join(clip_words(label, 40) for label in now[:3])
    gone = [label for label in was if label not in now]
    return f"selected now: {shown}" + (f" (was {', '.join(clip_words(label, 40) for label in gone[:3])})" if gone else "")


QUOTED = re.compile(r'^\s*(?:heading |dialog |alert |status |progress )?"([^"]+)"')


def words_shown(lines, limit=2):
    """The first plain text lines (not numbered elements) among `lines`: what a screen or a change is
    about, in its own words ("Ranabaali", "No results")."""
    found = []
    for line in lines:
        if (m := QUOTED.match(line)) and (text := m.group(1).strip(" ·")):
            found.append(clip_words(text, 50))
            if len(found) == limit:
                break
    return found


def changes(before: Screen, after: Screen, target=None):
    """A one-line outcome: what the screen text shows changed after the last action, or "" when it
    shows nothing (which does not mean nothing happened)."""
    notes = []
    # A new screen is named by its class and by its first words, which say which film, product or chat it is.
    headline = " | ".join(f'"{w}"' for w in words_shown(body(after.text)))
    if before.header.get("app") != after.header.get("app"):
        notes.append(f"now in {after.header.get('app')} on {str(after.header.get('activity')).rsplit('.', 1)[-1]}"
                     + (f" showing {headline}" if headline else ""))
    elif before.header.get("activity") != after.header.get("activity"):
        notes.append(f"now on {str(after.header.get('activity')).rsplit('.', 1)[-1]}"
                     + (f" showing {headline}" if headline else ""))
    kinds_before = [w.get("kind") for w in before.snapshot.get("windows", [])]
    kinds_after = [w.get("kind") for w in after.snapshot.get("windows", [])]
    if kinds_after.count("dialog") > kinds_before.count("dialog"):
        notes.append("a dialog opened")
    elif kinds_after.count("dialog") < kinds_before.count("dialog"):
        notes.append("a dialog closed")
    if before.header.get("keyboard") != after.header.get("keyboard"):
        notes.append("keyboard " + ("opened" if after.header.get("keyboard") else "closed"))
    pages_before, pages_after = web_pages(before), web_pages(after)
    if pages_after and pages_before != pages_after:
        title, url = pages_after[0]
        notes.append(f'page now "{title}" {clip_words(url, 90)}' if title else f"page now {clip_words(url, 90)}")
    if target is not None:
        same = after.find(target.key) or next((e for e in after.elements.values() if e.h == target.h), None)
        if same is None:
            notes.append("the element is gone")
        else:
            for attr in ("text", "icon", "state", "progress"):
                a, b = target.node.get(attr), same.node.get(attr)
                if a != b:
                    notes.append(f"{attr} {a!r} -> {b!r}")
            for flag in ("checked", "selected", "focused", "disabled"):
                if (flag in target.flags) != (flag in same.flags):
                    notes.append(("now " if flag in same.flags else "no longer ") + flag)
            # A list or page that could scroll a way and now cannot has reached that end (the app's own
            # canScrollVertically, or the page's scroll offset): said outright, so no scroll is spent finding out.
            for flag, end in SCROLL_ENDS.items():
                if flag in target.flags and flag not in same.flags:
                    notes.append(f"now at the {end}: nothing more {SCROLL_BEYOND[flag]}")
    if moved := selection_moved(before, after, target):
        notes.append(moved)
    old, new = set(body(before.text)), set(body(after.text))
    if pages_after:
        # A tap on a web control mostly changes the page elsewhere (a counter, a message): say
        # what text appeared, since the target's own flags (it took focus) say nothing of it.
        fresh = [line.strip() for line in body(after.text) if line not in old and line.strip().startswith(('"', *LEADS.values()))]
        if fresh:
            notes.append("page text now: " + " | ".join(clip_words(line, 70) for line in fresh[:3]))
    if not notes and old != new:
        fresh = words_shown([line for line in body(after.text) if line not in old])
        notes.append(f"screen changed ({len(new - old)} new lines" + (f": {' | '.join(repr(w) for w in fresh)})" if fresh else ")"))
    # Never "screen unchanged": the text cannot see a tick, a colour or a picture swap, so its
    # silence says nothing about whether the action worked.
    return "; ".join(notes)
