"""Reading videos as text (app/youtube.py, tools/youtube.py): ids, caption choice, the transcript's shape and budget,
the tool's own refresh, and the tools' checks. Nothing here touches YouTube except the one live test, which runs
only with RELAY_LIVE_TESTS=1 and is the early warning for a YouTube change that the self-update could not absorb."""

import asyncio
import hashlib
import json
import os

import pytest
from app import youtube as yt
from app.agents.registry import ORCHESTRATOR, SUBAGENT
from app.errors import Rejected
from app.tools.youtube import Youtube, YoutubeTranscript


def test_video_id_from_every_kind_of_link():
    for text in ("dQw4w9WgXcQ", "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=30s", "https://youtu.be/dQw4w9WgXcQ?si=x",
                 "https://www.youtube.com/shorts/dQw4w9WgXcQ", "https://m.youtube.com/embed/dQw4w9WgXcQ", " dQw4w9WgXcQ "):
        assert yt.video_id(text) == "dQw4w9WgXcQ", text
    assert yt.video_id("not a link") is None
    assert yt.video_id("https://example.com/watch") is None


def test_times():
    assert yt.seconds_of("1:02:30") == 3750 and yt.seconds_of("12:05") == 725 and yt.seconds_of("90s") == 90
    assert yt.seconds_of("") is None
    assert yt.clock(3750) == "1:02:30" and yt.clock(725) == "12:05"
    with pytest.raises(yt.YouTubeError):
        yt.seconds_of("soon")


def formats(url):
    return [{"ext": "vtt", "url": url + ".vtt"}, {"ext": "json3", "url": url}]


def test_the_creators_captions_win_over_automatic_and_translations_are_never_picked():
    got = yt.pick_track({
        "language": "en-US",
        "subtitles": {"en": formats("manual-en"), "de": formats("manual-de")},
        "automatic_captions": {"en-orig": formats("auto-orig"), "en": formats("auto-en"), "de": formats("auto-de")},
    })
    assert got == ("manual", "en", "manual-en")
    got = yt.pick_track({"language": "en-US", "automatic_captions": {"de": formats("auto-de"), "en-orig": formats("auto-orig"),
                                                                    "en": formats("auto-en")}})
    assert got == ("automatic", "en-orig", "auto-orig")  # the speech itself, not a translation of it
    got = yt.pick_track({"language": "hi", "subtitles": {"en": formats("manual-en")}})
    assert got == ("manual", "en", "manual-en")  # the video's own language has none: English
    assert yt.pick_track({"language": "en", "subtitles": {}, "automatic_captions": {}}) is None
    assert yt.pick_track({"language": "en", "subtitles": {"en": [{"ext": "vtt", "url": "x"}]}}) is None  # no json3 to read


def test_segments_and_blocks():
    data = {"events": [
        {"tStartMs": 0, "segs": [{"utf8": "hello"}, {"utf8": " there\n"}]},
        {"tStartMs": 1000, "segs": [{"utf8": "\n"}]},
        {"tStartMs": 2000},
        {"tStartMs": 12000, "segs": [{"utf8": "this   is  a test"}]},
        {"tStartMs": 31000, "segs": [{"utf8": "second block"}]},
    ]}
    segments = yt.segments_of(data)
    assert segments == [(0, "hello there"), (12, "this is a test"), (31, "second block")]
    assert yt.blocks_of(segments) == [(0, "hello there this is a test"), (31, "second block")]


META = {"id": "dQw4w9WgXcQ", "title": "How to fix it", "channel": "Handy", "duration": 3725, "language": "en", "kind": "manual",
        "views": 1_250_000, "date": "20260102", "chapters": [(0, "Intro"), (600, "Parts")], "description": "Sponsored by X"}
SEGMENTS = [(i * 10, " ".join(["word"] * 5)) for i in range(400)]  # 4000 s of speech, 5 words every 10 s


def test_render_shape_and_ranges():
    text = yt.render(META, SEGMENTS, 600, 660)
    assert text.splitlines()[0].startswith("How to fix it — Handy · 1:02:05 · en manual captions · 2026-01-02 · 1.2M views")
    assert "https://www.youtube.com/watch?v=dQw4w9WgXcQ" in text
    assert "Chapters: 0:00 Intro · 10:00 Parts" in text and "Description: Sponsored by X" in text
    body = [line for line in text.splitlines() if line.startswith("[")]
    assert body[0].startswith("[10:00]") and body[-1].startswith("[10:30]")  # two 30-second lines in the minute
    assert "Transcript 10:00–11:00 (30 words)" in text
    assert "No speech between" in yt.render(META, SEGMENTS, 5000, 6000)


def test_render_stops_at_the_word_budget_and_says_where_to_go_on():
    text = yt.render(META, SEGMENTS, max_words=60)
    assert "Stopped at 2:00: 130 more lines" in text and "start=2:00" in text
    assert text.count("\n[") == 4 and "(60 words)" in text
    assert "automatic: no punctuation" in yt.render({**META, "kind": "automatic"}, SEGMENTS, 0, 30)


def test_published_sums_and_hash(tmp_path):
    sums = "abc123 *yt-dlp.exe\ndef456  yt-dlp_macos\n"
    assert yt.published_sum(sums, "yt-dlp.exe") == "abc123" and yt.published_sum(sums, "yt-dlp_macos") == "def456"
    assert yt.published_sum(sums, "nope") is None
    path = tmp_path / "f"
    path.write_bytes(b"x" * 3_000_000)
    assert yt.sha256(path) == hashlib.sha256(b"x" * 3_000_000).hexdigest()


async def test_a_failure_that_is_not_the_videos_refreshes_the_tool_and_tries_again(monkeypatch):
    calls, refreshed = [], []

    async def run(*args):
        calls.append(args)
        if len(calls) == 1:
            raise yt.YouTubeError("Unable to extract player response")
        return "ok"

    async def refresh(force=False):
        refreshed.append(force)
        return True

    monkeypatch.setattr(yt, "run", run)
    monkeypatch.setattr(yt, "refresh", refresh)
    monkeypatch.setattr(yt, "_last_forced", 0.0)
    assert await yt.call("x") == "ok" and refreshed == [True] and len(calls) == 2
    # and not again within the half hour; a video that is private is not the tool's fault either way
    calls.clear()
    with pytest.raises(yt.YouTubeError):
        monkeypatch.setattr(yt, "run", lambda *a: _fail("Unable to extract player response"))
        await yt.call("x")
    assert refreshed == [True]
    monkeypatch.setattr(yt, "_last_forced", 0.0)
    monkeypatch.setattr(yt, "run", lambda *a: _fail("Private video. Sign in if you've been granted access"))
    with pytest.raises(yt.YouTubeError):
        await yt.call("x")
    assert refreshed == [True]


async def _fail(message):
    raise yt.YouTubeError(message)


async def test_read_uses_the_cache_and_names_a_video_without_captions(monkeypatch):
    yt._cache.clear()
    fetched = []

    async def call(*args):
        fetched.append(args)
        return '{"title": "Quiet", "language": "en", "duration": 5, "subtitles": {}, "automatic_captions": {}}'

    monkeypatch.setattr(yt, "call", call)
    with pytest.raises(yt.YouTubeError, match="has no captions"):
        await yt.read("dQw4w9WgXcQ")
    yt.remember("dQw4w9WgXcQ", META, SEGMENTS)
    assert "How to fix it" in await yt.read("dQw4w9WgXcQ", "0:00", "0:30")
    assert len(fetched) == 1  # the second read came from memory
    with pytest.raises(yt.YouTubeError, match="not a YouTube link"):
        await yt.read("hello")
    yt._cache.clear()


RAW_COMMENTS = [
    {"id": "a", "parent": "root", "author": "@jon", "like_count": 686, "text": "Fixed it for $15.\nTip: mark the  water line",
     "_time_text": "2 years ago"},
    {"id": "a.1", "parent": "a", "author": "@HomeRepairTutor", "like_count": 5, "text": "Great tip!", "author_is_uploader": True},
    {"id": "b", "parent": "root", "author": "@pin", "like_count": 1200, "text": "Use a 1/2 inch nut", "is_pinned": True,
     "is_favorited": True},
]


def test_comments_become_threads_in_youtubes_order():
    threads = yt.comments_of({"comments": RAW_COMMENTS})
    assert [t["author"] for t in threads] == ["@jon", "@pin"]
    assert threads[0]["text"] == "Fixed it for $15. Tip: mark the water line" and threads[0]["when"] == "2 years ago"
    assert threads[0]["replies"] == [{"author": "@HomeRepairTutor", "likes": 5, "text": "Great tip!", "when": "",
                                      "pinned": False, "creator": True, "hearted": False}]
    lines = yt.render_comments(threads)
    assert lines[0].startswith("Top comments (2 threads, most liked first;")
    assert lines[1] == "- 686 likes · @jon · 2 years ago: Fixed it for $15. Tip: mark the water line"
    assert lines[2] == "  - 5 likes · @HomeRepairTutor (creator): Great tip!"
    assert lines[3] == "- 1.2K likes · @pin (pinned, hearted by creator): Use a 1/2 inch nut"
    assert "2 threads" not in yt.render_comments(threads, max_words=12)[0]  # only the first thread fits
    assert "1 more not shown" in yt.render_comments(threads, max_words=12)[0]
    assert "none" in yt.render_comments([])[0]


async def test_comments_are_fetched_once_asked_and_read_a_video_without_captions(monkeypatch):
    yt._cache.clear()
    fetched = []

    async def call(*args):
        fetched.append(args)
        return json.dumps({"title": "Quiet", "language": "en", "duration": 5, "comments": RAW_COMMENTS})

    monkeypatch.setattr(yt, "call", call)
    yt.remember("dQw4w9WgXcQ", META, SEGMENTS)  # read before, without comments
    assert "Top comments" not in await yt.read("dQw4w9WgXcQ")
    text = await yt.read("dQw4w9WgXcQ", comments=True)  # fetched again, now with them
    assert "--write-comments" in fetched[-1] and "Top comments (2 threads" in text
    assert "Transcript: none" in text  # this answer had no captions, and the comments still came
    await yt.read("dQw4w9WgXcQ", comments=True)
    assert len(fetched) == 1  # and are cached with the video
    yt._cache.clear()


def test_youtube_tool_checks():
    state = {}
    Youtube(search="how to tile a floor").check([], state)
    Youtube(videos=["dQw4w9WgXcQ"], start="1:00", end="2:00").check([], state)
    for bad in (Youtube(), Youtube(search="x", videos=["dQw4w9WgXcQ"]), Youtube(videos=["dQw4w9WgXcQ", "aircAruvnKk"], start="1:00"),
                Youtube(videos=["https://example.com"])):
        with pytest.raises(Rejected):
            bad.check([], state)
    with pytest.raises(Rejected):
        Youtube(search="x").check([], {"youtube": [{}] * 6})


def test_the_agents_have_the_tools_and_their_skills():
    assert "youtube" in ORCHESTRATOR.classes and "youtube_transcript" in SUBAGENT.classes
    assert "youtube" not in SUBAGENT.classes and "youtube_transcript" not in ORCHESTRATOR.classes
    for agent in (ORCHESTRATOR, SUBAGENT):
        assert "## How to use youtube" in agent.instructions
        assert "captions" in agent.instructions
    Youtube.model_json_schema()
    assert any(f["function"]["name"] == "youtube_transcript" for f in SUBAGENT.functions)


async def test_the_phone_tool_returns_a_failure_as_a_line(monkeypatch):
    async def read(*args, **kw):
        raise yt.YouTubeError("nothing here")

    monkeypatch.setattr(yt, "read", read)
    assert await YoutubeTranscript(video="dQw4w9WgXcQ").run(None) == "FAILED: nothing here"


@pytest.mark.skipif(os.environ.get("RELAY_LIVE_TESTS") != "1", reason="talks to YouTube: RELAY_LIVE_TESTS=1")
def test_live_search_and_read_still_work(tmp_path, monkeypatch):
    """Fails when YouTube changes something that even a fresh yt-dlp does not yet handle. Uses its own tools folder."""
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    yt._cache.clear()

    async def go():
        found = await yt.search("3blue1brown neural networks")
        assert "id " in found
        text = await yt.read("aircAruvnKk", "0:00", "2:00")
        assert "Transcript 0:" in text and len(text.split()) > 150
        assert (tmp_path / "tools" / yt.asset()).exists()
        text = await yt.read("aircAruvnKk", "0:00", "0:30", comments=True)
        assert "Top comments (" in text and " likes · @" in text

    asyncio.run(go())
