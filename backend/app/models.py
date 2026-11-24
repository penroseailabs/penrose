"""The shapes the API and the device gateway exchange: device actions.
What agents send and receive is defined by their tools (app/tools)."""

import re
from typing import Literal

from pydantic import BaseModel, Field

# What the gateway shows in place of a privately typed value wherever the screen is read.
PRIVATE_MARK = "[private]"
# What an agent writes as the text to type for the user's private entry N (store.add_private): the runtime types
# the value in its place, and the agent never sees it.
PRIVATE_REF = re.compile(r"\{private:(\d{1,2})\}")


class Action(BaseModel):
    """A device action outside the bridge: subagent commands and installs, the private typing of
    codes, Android's system keys and the bridge's coordinate fallback for the UI agent, and the user's
    own input from the phone panel (google_signin opens Google's add-account screen)."""

    kind: Literal[
        "adb_command",
        "install_app",
        "tap",
        "swipe",
        "type_text",
        "back",
        "home",
        "recents",
        "enter",
        "delete",
        "google_signin",
    ]
    x: int = Field(default=0, ge=0, le=20000)
    y: int = Field(default=0, ge=0, le=20000)
    x2: int = Field(default=0, ge=0, le=20000)
    y2: int = Field(default=0, ge=0, le=20000)
    duration_ms: int = Field(default=350, ge=50, le=3000)
    # type_text: typed into the focused field.
    text: str = Field(default="")
    package: str = Field(default="", max_length=200)
    command: str = Field(default="")
    sensitive: bool = False
