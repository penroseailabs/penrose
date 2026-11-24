"""YouTube tools: what a video says, read as text instead of watched (app/youtube.py).

`youtube` is the orchestrator's: it finds videos and reads them in seconds, with no phone step, so research that
lives in videos (how-to guides, product reviews, deep dives) costs one or two decisions. `youtube_transcript` is the
phone agent's, for a step that is already in the YouTube app and needs what a video says. How to use them well is
backend/skills/tools/youtube.md.
"""

from typing import Literal

from pydantic import Field

from .. import youtube as yt
from ..errors import Rejected
from .base import Tool, TurnTool

MAX_CALLS = 6  # youtube calls (searches and readings) per user message
MAX_VIDEOS = 3  # videos read in one call
PHONE_WORDS = 3000  # a phone agent keeps what it reads in its log: less than the orchestrator, which decides from it


class Youtube(Tool):
    """Find YouTube videos and read their captions, in seconds, without the phone (when: Where to look). One use
    per call: `search` lists the best videos for a query (title, channel, length, views, id); `videos` reads the
    ones you pick (two or three, different channels) as a header, chapters and timed lines. Ends the turn; you
    are called again with `youtube`. The youtube skill says how to pick and read them; the learn-from-videos
    recipe is the usual plan."""

    tool: Literal["youtube"] = "youtube"
    search: str = Field(
        default="", max_length=120,
        description="Search YouTube for this, worded as a person would type it into YouTube: 'how to replace a toilet "
        "fill valve', 'sony wh-1000xm6 review', 'how do transformers work'. Empty when reading videos.",
    )
    videos: list[str] = Field(
        default_factory=list, max_length=MAX_VIDEOS,
        description="Video ids or links to read (from a search, from the user, from a Google result). One to three.",
    )
    start: str = Field(
        default="", max_length=10,
        description="With one video: where to start reading, as 12:05 or 1:02:30 (from its chapters). Empty: the beginning.",
    )
    end: str = Field(default="", max_length=10, description="With one video: where to stop. Empty: as much as fits.")
    comments: bool = Field(
        default=False,
        description="Also read each video's top comments (most liked first, with the creator's replies): about 3 s more. "
        "True for reviews, guides and anything where viewers add fixes, corrections, long-term experience or "
        "disagreement. Also reads a video that has no captions.",
    )

    def check(self, plan, state):
        if bool(self.search.strip()) == bool(self.videos):
            raise Rejected("youtube needs either `search` (to find videos) or `videos` (to read them), not both and not neither.")
        if len(state.get("youtube", [])) >= MAX_CALLS:
            raise Rejected(f"You used youtube {MAX_CALLS} times for this message: answer from `youtube`.")
        if (self.start or self.end) and len(self.videos) != 1:
            raise Rejected("start and end go with exactly one video.")
        bad = [v for v in self.videos if not yt.video_id(v)]
        if bad:
            raise Rejected(f"Not a YouTube link or video id: {', '.join(repr(b[:60]) for b in bad)}.")

    async def run(self, o):
        await o.youtube(self)


class YoutubeTranscript(TurnTool):
    """Get the transcript (captions) of a YouTube video in seconds, without playing it: its title, chapters and
    the words with a time on every line. Use it whenever your step needs to "watch" a video (a how-to or DIY
    guide, a review, a podcast, a talk's points) instead of scrolling the player, which cannot be read. It
    arrives once, in Last result: write what you need into your log at once. If it fails, the message says why."""

    tool: Literal["youtube_transcript"] = "youtube_transcript"
    video: str = Field(max_length=200, description="The video's link or id.")
    start: str = Field(default="", max_length=10, description="Where to start reading, as 12:05 or 1:02:30. Empty: the beginning.")
    end: str = Field(default="", max_length=10, description="Where to stop. Empty: as much as fits.")
    comments: bool = Field(
        default=False, description="Also read its top comments (most liked first, with the creator's replies): about 3 s more.",
    )

    async def run(self, session):
        try:
            return await yt.read(self.video, self.start, self.end, max_words=PHONE_WORDS, comments=self.comments)
        except yt.YouTubeError as exc:
            return f"FAILED: {exc}"
