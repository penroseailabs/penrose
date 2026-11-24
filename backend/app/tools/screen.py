"""The phone agents' own tool beside the bridge's (app/bridge/tools.py:
interact, type_and_submit, press_key, swipe and the device-level calls, each checked against the
Interactions the screen offers). Phone agents act only on numbered elements, never on coordinates.

`log` is the subagent's running account of its session, one line a turn in its own words: what its last
action did and what it learned, so a long session reads as a short story instead of a list of raw actions.
`note` is the verifier's: what it will need later, since old screens are not kept. `progress` is the agent's
own plan for its step, the one thing besides the brief that stays in front of it every turn: the
orchestrator's task list, one level down. `read_page` reads a whole web page in one call instead of
scrolling through it a screen at a time. `clip` keeps a picture from the screen for the user's answer
(runtime/pictures.py): the agent names the element, the runtime cuts it out of one screenshot.
"""

from typing import Literal

from pydantic import Field

from .base import Tool, TurnTool


class Log(Tool):
    """Your log: a line, in your own words, on every turn that learned something from the screen in front of
    you: what your last action showed (from Last result and the screen), with the exact values you will need
    (names, prices, ratings, counts, references, what is missing or did not work). It is all you keep of the
    screens you leave: your log is shown back to you every turn, the screens are not, and the runtime adds
    where you were. A turn that only moved and learned nothing needs none: the runtime logs its raw result.
    It joins the turn's action and does nothing on the phone. Never a code or a password."""

    tool: Literal["log"] = "log"
    text: str = Field(
        description="One line, e.g. 'Target results for \"desk lamp\": Brightline LED $34 4.5★ (1.2K) is the only one under "
        "$40; Lumo Arc $52. Opening Brightline LED.' or 'Tapping Filters did nothing; Sort is the way.'",
    )


class Note(Tool):
    """Write down what you will need later in this session: a price, a name, an order reference, what you
    found out. Your notes are shown back to you every turn; the screens you saw are not. It may join any
    turn and does nothing on the phone. Never a code or a password."""

    tool: Literal["note"] = "note"
    text: str = Field(description="One line, e.g. 'Cart total $11.98 for 2 × Horizon Organic whole milk'.")


class ReadPage(Tool):
    """Read the whole web page on screen again (Chrome or a web page inside an app): every element of it,
    including everything below the screen, read as Screen reads them (text and labels such as ratings), in
    your next turn. A page is read for you the turn it opens (open_link, a tap, a search), and its Page
    text stays in your state while you are on it: never scroll a web page to read it. Call read_page only
    for a page that changed in place (more rows loaded after a scroll, a search on the same page) or whose
    text is missing (it was still loading). It may join any turn and runs last, after the turn's other
    actions. It has no element numbers: scroll only to reach an element you need to tap. Its images are
    numbered in page order ((image 3 "its alt text")) so that clip can keep one with page_image=3 while
    the text is shown. A list that loads more as you scroll shows only what has loaded."""

    tool: Literal["read_page"] = "read_page"


class AppAction(TurnTool):
    """Only when your state has an "App data" part, which few apps have (when it is absent, this tool does nothing:
    work on Screen). App data is the content of the app in front read from the app's own data on the phone, and it
    names the actions you can run here: what each takes (item: a number from its list; items: several numbers;
    text: a name) and what it does, e.g. opening a chat by name or loading older messages in one step instead of
    several taps and scrolls. The action runs with a checked outcome, on the app's own screens or in App data alone,
    and the next App data shows the result. If it fails twice the same way, do it on Screen instead. The only call
    of its turn."""

    tool: Literal["app_action"] = "app_action"
    action: str = Field(description="The action's name as App data lists it.")
    item: int = Field(default=0, ge=0, description="The number of the item in App data's list it acts on. 0: none.")
    text: str = Field(default="", description="Instead of item: the item's name as App data shows it.")
    items: list[int] = Field(default_factory=list, description="For an action that takes several items at once: "
                                                               "their numbers in App data's list.")

    async def run(self, session):
        from ..highways import act

        return await act(session, self.action.strip(), self.item, self.text.strip(), self.items)


class Progress(Tool):
    """Your plan for this step, rewritten whole: the parts of the work in order, each marked [done], [now]
    or [next]. Write it on your first turn when the step has more than one part (log in, set the address,
    search, add to cart) and rewrite it whenever a part is done or the approach changes. It is shown back
    to you every turn, so you never have to work out where you are from your recent actions. It may join
    any turn and does nothing on the phone."""

    tool: Literal["progress"] = "progress"
    text: str = Field(
        description="One line per part, e.g. '[done] Logged in\\n[now] Set the address to Home\\n[next] Add 2 × Horizon Organic whole milk (half gallon)'.",
    )


class Clip(Tool):
    """Keep a picture for the user's answer while you work: the runtime cuts it out of the screen or takes it
    from the page itself (you never see it, it costs no turn). Keep what the orchestrator may show the user
    when it presents the result: the thing the step is about (the product, the room, the dish, the place:
    the element that holds its photo, with picture=true), each option the user will choose between, the
    confirmation of what you did (an order, a booking, a sent message: the card or panel that shows it, or
    element 0 for the whole screen), or, on a research step, a picture of a place or thing the answer will
    name, from a page you are reading anyway (page_image=N from Page text). Only what is relevant: one or
    two per step, not every screen, never a screen with the user's private data (addresses, card numbers,
    codes). The caption is what the user will read under it. It may join any turn and runs before the turn's
    actions, on this Screen."""

    tool: Literal["clip"] = "clip"
    element: int = Field(default=0, ge=0, description="The numbered element on Screen to cut out, or 0 for the whole screen.")
    page_image: int = Field(
        default=0, ge=0,
        description="Instead of an element: the number of an image in this turn's Page text, (image 3 ...) -> 3. "
        "The runtime fetches the page's own image file.",
    )
    picture: bool = Field(
        default=True,
        description="true: the largest image inside the element (a product photo, a hotel photo), clean, for showing "
        "the thing itself. false: the element as drawn, words and all (a result card, a confirmation, a chart).",
    )
    caption: str = Field(description="A few words for the user, e.g. 'Brightline LED lamp, $34' or 'Your Instacart order IC-1234'.")
