"""Link previews (app/linkpreview.py): which addresses are fetched at all, what is read from a page, and that a
stranger's picture is re-drawn rather than passed on."""

import asyncio
import base64
import contextlib
import io

import pytest
from app import linkpreview
from app.linkpreview import Refused
from fastapi.testclient import TestClient
from PIL import Image


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "http://example.com:8080/",
        "https://example.com:22/",
        "https://user:pass@example.com/",
        "http:///nohost",
        "http://[::1/",
    ],
)
def test_only_plain_web_addresses_are_fetched(url):
    with pytest.raises(Refused):
        linkpreview.allowed(url)


def test_a_web_address_is_accepted():
    assert linkpreview.allowed("https://www.example.com/a?b=1") == "www.example.com"
    assert linkpreview.allowed("http://example.com:80/") == "example.com:80"


@pytest.mark.parametrize(
    "address,ok",
    [
        ("93.184.216.34", True),
        ("2606:2800:220:1:248:1893:25c8:1946", True),
        ("127.0.0.1", False),
        ("10.0.0.5", False),
        ("172.16.3.4", False),
        ("192.168.1.1", False),
        ("169.254.169.254", False),  # the cloud metadata address
        ("100.64.0.1", False),
        ("0.0.0.0", False),
        ("::1", False),
        ("fe80::1%12", False),
        ("fc00::1", False),
        ("::ffff:127.0.0.1", False),
        ("::ffff:10.1.2.3", False),
        ("224.0.0.1", False),
    ],
)
def test_only_public_addresses_count(address, ok):
    assert linkpreview.public(address) is ok


async def test_a_name_must_resolve_only_to_public_addresses(monkeypatch):
    answers = {
        "fine.test": ["2606:2800::1", "93.184.216.34"],
        "rebind.test": ["93.184.216.34", "127.0.0.1"],  # one public answer does not excuse a private one
        "inside.test": ["192.168.0.10"],
    }

    async def getaddrinfo(self, host, port, **kw):
        return [(2, 1, 6, "", (a, port)) for a in answers[host]]

    monkeypatch.setattr("asyncio.BaseEventLoop.getaddrinfo", getaddrinfo)
    assert await linkpreview.public_address("fine.test", 443) == "93.184.216.34"  # IPv4 first
    for name in ("rebind.test", "inside.test"):
        with pytest.raises(Refused):
            await linkpreview.public_address(name, 443)


class Response:
    def __init__(self, status=200, headers=(), body=b""):
        self.status, self.headers, self.body = status, [(k.encode(), v.encode()) for k, v in headers], body

    async def aiter_stream(self):
        for i in range(0, len(self.body), 1000):
            yield self.body[i : i + 1000]


class Pool:
    """Answers each address from `pages` and notes what was asked for."""

    def __init__(self, pages):
        self.pages, self.asked = pages, []

    @contextlib.asynccontextmanager
    async def stream(self, method, url, headers=None):
        self.asked.append((url, headers))
        yield self.pages[url]


async def test_redirects_are_followed_and_each_hop_is_checked():
    pool = Pool(
        {
            "https://a.test/x": Response(301, [("Location", "/y")]),
            "https://a.test/y": Response(302, [("Location", "http://127.0.0.1:8080/secret")]),
        }
    )
    with pytest.raises(Refused):  # the last hop is not a port that is fetched, and nothing is asked of it
        await linkpreview.fetch(pool, "https://a.test/x", 1000)
    assert [u for u, _ in pool.asked] == ["https://a.test/x", "https://a.test/y"]


async def test_a_redirect_to_another_scheme_or_port_is_refused():
    pool = Pool({"https://a.test/": Response(302, [("Location", "ftp://a.test/")])})
    with pytest.raises(Refused):
        await linkpreview.fetch(pool, "https://a.test/", 1000)
    pool = Pool({"https://a.test/": Response(302, [("Location", "http://a.test:6379/")])})
    with pytest.raises(Refused):
        await linkpreview.fetch(pool, "https://a.test/", 1000)


async def test_endless_redirects_stop():
    pool = Pool({"https://a.test/": Response(302, [("Location", "https://a.test/")])})
    with pytest.raises(Refused):
        await linkpreview.fetch(pool, "https://a.test/", 1000)
    assert len(pool.asked) == linkpreview.REDIRECTS + 1


async def test_the_request_carries_no_cookies_or_referrer():
    pool = Pool({"https://a.test/": Response(200, [("Content-Type", "text/html")], b"<html>")})
    await linkpreview.fetch(pool, "https://a.test/", 1000)
    sent = {k.lower() for k in pool.asked[0][1]}
    assert not sent & {"cookie", "referer", "authorization"}
    assert pool.asked[0][1]["Host"] == "a.test"


async def test_a_page_is_read_only_up_to_the_limit():
    pool = Pool({"https://a.test/": Response(200, [("Content-Type", "text/html")], b"x" * 5000)})
    _, _, body, more = await linkpreview.fetch(pool, "https://a.test/", 1500)
    assert len(body) == 1500 and more


async def test_an_error_status_is_refused():
    pool = Pool({"https://a.test/": Response(403)})
    with pytest.raises(Refused):
        await linkpreview.fetch(pool, "https://a.test/", 1000)


def jpeg(width=800, height=400):
    out = io.BytesIO()
    Image.new("RGB", (width, height), (200, 80, 40)).save(out, "JPEG")
    return out.getvalue()


def test_a_picture_is_redrawn_small_as_a_jpeg():
    shown = linkpreview.thumbnail(jpeg(2000, 1000))
    assert shown.startswith("data:image/jpeg;base64,")
    image = Image.open(io.BytesIO(base64.b64decode(shown.split(",", 1)[1])))
    assert image.format == "JPEG" and max(image.size) <= linkpreview.THUMB


def test_transparent_pictures_are_drawn_on_white():
    out = io.BytesIO()
    Image.new("RGBA", (200, 200), (0, 0, 0, 0)).save(out, "PNG")
    shown = linkpreview.thumbnail(out.getvalue())
    image = Image.open(io.BytesIO(base64.b64decode(shown.split(",", 1)[1])))
    assert image.getpixel((100, 100))[0] > 240


@pytest.mark.parametrize(
    "data",
    [
        b"<svg xmlns='http://www.w3.org/2000/svg' onload='alert(1)'/>",
        b"<html>not a picture</html>",
        b"",
        jpeg(40, 40),  # a favicon, too small for a card
    ],
)
def test_what_is_not_a_picture_worth_showing_is_dropped(data):
    assert linkpreview.thumbnail(data) is None


def test_a_picture_with_too_many_pixels_is_never_decoded():
    out = io.BytesIO()
    Image.new("1", (6000, 6000)).save(out, "PNG")  # 36 million pixels, a few KB: a decompression bomb
    assert linkpreview.thumbnail(out.getvalue()) is None


def reading(monkeypatch, pages):
    """`read` against a Pool of pages, in place of the real connection pool."""
    pool = Pool(pages)

    class FakePool:
        def __init__(self, **_):
            pass

        async def __aenter__(self):
            return pool

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(linkpreview.httpcore, "AsyncConnectionPool", FakePool)
    return pool


def pool_of(pool):
    """A connection pool that is `pool`, as `async with` gives it."""

    class Held:
        async def __aenter__(self):
            return pool

        async def __aexit__(self, *exc):
            return False

    return Held()


PAGE = """<html><head><title>Plain title</title>
<meta property="og:title" content="The &amp; title">
<meta property="og:description" content="  A  description
 over lines ">
<meta property="og:site_name" content="Example">
<meta property="og:image" content="/pic.jpg">
<meta property="og:title" content="A second title that loses">
</head><body><meta property="og:title" content="Body meta is not read"></body></html>"""


async def test_a_page_gives_its_title_text_and_picture(monkeypatch):
    pool = reading(
        monkeypatch,
        {
            "https://www.example.com/a": Response(200, [("Content-Type", "text/html; charset=utf-8")], PAGE.encode()),
            "https://www.example.com/pic.jpg": Response(200, [("Content-Type", "image/jpeg")], jpeg()),
        },
    )
    found = await linkpreview.read("https://www.example.com/a")
    assert found["title"] == "The & title"
    assert found["description"] == "A description over lines"
    assert found["site"] == "Example" and found["host"] == "example.com"
    assert found["image"].startswith("data:image/jpeg;base64,")
    assert pool.asked[1][0] == "https://www.example.com/pic.jpg"


async def test_older_ways_of_naming_a_picture_count_when_there_is_no_og_image(monkeypatch):
    for tag in ('<link rel="image_src" href="/pic.jpg">', '<meta itemprop="image" content="/pic.jpg">'):
        page = f"<head><title>t</title>{tag}</head>".encode()
        reading(
            monkeypatch,
            {
                "https://a.test/": Response(200, [("Content-Type", "text/html")], page),
                "https://a.test/pic.jpg": Response(200, [("Content-Type", "image/jpeg")], jpeg()),
            },
        )
        assert (await linkpreview.read("https://a.test/"))["image"]


async def test_a_page_that_names_no_picture_gives_its_text_alone(monkeypatch):
    page = b'<head><title>Buy gloves</title><meta name="description" content="Strong gloves"></head>'
    reading(monkeypatch, {"https://a.test/": Response(200, [("Content-Type", "text/html")], page)})
    found = await linkpreview.read("https://a.test/")
    assert found["title"] == "Buy gloves" and found["description"] == "Strong gloves" and found["image"] is None


class Stripped:
    """A site that answers a bare request with a stripped page now and then: `answers` in turn, the last one for good."""

    def __init__(self, answers):
        self.answers, self.asked = list(answers), 0

    @contextlib.asynccontextmanager
    async def stream(self, method, url, headers=None):
        if url.endswith("/pic.jpg"):
            yield Response(200, [("Content-Type", "image/jpeg")], jpeg())
            return
        self.asked += 1
        body = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        yield Response(200, [("Content-Type", "text/html")], body)


STRIPPED = b"<head><title> - YouTube</title></head>"
FULL = b'<head><title>A video - YouTube</title><meta property="og:title" content="A video"><meta property="og:image" content="/pic.jpg"></head>'


async def test_a_page_that_names_nothing_is_asked_again(monkeypatch):
    real_sleep = asyncio.sleep
    monkeypatch.setattr(linkpreview.asyncio, "sleep", lambda s: real_sleep(0))
    pool = Stripped([STRIPPED, STRIPPED, FULL])
    monkeypatch.setattr(linkpreview.httpcore, "AsyncConnectionPool", lambda **_: pool_of(pool))
    found = await linkpreview.read("https://a.test/watch")
    assert found["title"] == "A video" and found["image"] and pool.asked == 3


async def test_asking_again_stops_after_three_and_a_bare_title_is_not_a_title(monkeypatch):
    real_sleep = asyncio.sleep
    monkeypatch.setattr(linkpreview.asyncio, "sleep", lambda s: real_sleep(0))
    pool = Stripped([STRIPPED])
    monkeypatch.setattr(linkpreview.httpcore, "AsyncConnectionPool", lambda **_: pool_of(pool))
    with pytest.raises(Refused):  # " - YouTube" is not something to put on a card
        await linkpreview.read("https://a.test/watch")
    assert pool.asked == linkpreview.ATTEMPTS


async def test_a_page_that_names_itself_is_asked_once(monkeypatch):
    pool = Stripped([FULL])
    monkeypatch.setattr(linkpreview.httpcore, "AsyncConnectionPool", lambda **_: pool_of(pool))
    await linkpreview.read("https://a.test/watch")
    assert pool.asked == 1


async def test_a_picture_that_cannot_be_fetched_leaves_the_text(monkeypatch):
    reading(
        monkeypatch,
        {
            "https://a.test/": Response(200, [("Content-Type", "text/html")], PAGE.encode()),
            "https://a.test/pic.jpg": Response(404),
        },
    )
    found = await linkpreview.read("https://a.test/")
    assert found["title"] == "The & title" and found["image"] is None


async def test_a_picture_address_on_this_network_is_not_followed(monkeypatch):
    page = b'<head><title>t</title><meta property="og:image" content="http://192.168.0.1:8080/cam.jpg"></head>'
    pool = reading(monkeypatch, {"https://a.test/": Response(200, [("Content-Type", "text/html")], page)})
    found = await linkpreview.read("https://a.test/")
    assert found["title"] == "t" and found["image"] is None
    assert [u for u, _ in pool.asked] == ["https://a.test/"]


async def test_a_page_with_nothing_to_show_is_refused(monkeypatch):
    reading(monkeypatch, {"https://a.test/": Response(200, [("Content-Type", "text/html")], b"<head></head>")})
    with pytest.raises(Refused):
        await linkpreview.read("https://a.test/")


async def test_something_that_is_not_a_page_is_refused(monkeypatch):
    reading(monkeypatch, {"https://a.test/": Response(200, [("Content-Type", "application/json")], b"{}")})
    with pytest.raises(Refused):
        await linkpreview.read("https://a.test/")


async def test_a_page_is_read_only_to_the_end_of_its_head():
    page = b"<html><head><title>t</title></head><body>" + b"x" * 500_000
    pool = Pool({"https://a.test/": Response(200, [("Content-Type", "text/html")], page)})
    _, _, body, more = await linkpreview.fetch(pool, "https://a.test/", linkpreview.PAGE_BYTES, linkpreview.HEAD_END)
    assert len(body) < 3000 and b"<title>t</title>" in body and not more


async def test_a_head_end_split_between_two_chunks_is_still_found():
    page = b"a" * 995 + b"</head>" + b"b" * 50_000  # the pieces are 1000 bytes: the tag straddles the first two
    pool = Pool({"https://a.test/": Response(200, [("Content-Type", "text/html")], page)})
    _, _, body, _ = await linkpreview.fetch(pool, "https://a.test/", linkpreview.PAGE_BYTES, linkpreview.HEAD_END)
    assert len(body) <= 2000


async def test_a_link_asked_for_twice_at_once_is_read_once(monkeypatch):
    calls = []

    async def read(url):
        calls.append(url)
        await asyncio.sleep(0.05)
        return {"title": "t", "image": None}

    monkeypatch.setattr(linkpreview, "read", read)
    answers = await asyncio.gather(*(linkpreview.preview("https://a.test/") for _ in range(5)))
    assert calls == ["https://a.test/"] and all(a["title"] == "t" for a in answers)
    assert not linkpreview.under_way


async def test_nothing_is_kept_between_reads(monkeypatch):
    calls = []

    async def read(url):
        calls.append(url)
        return {"title": "t", "image": None}

    monkeypatch.setattr(linkpreview, "read", read)
    await linkpreview.preview("https://a.test/")
    await linkpreview.preview("https://a.test/")
    assert len(calls) == 2  # keeping is the window's job (Cache-Control), not this computer's


def test_the_answer_may_be_kept_for_a_week_and_a_refusal_for_five_minutes(store, monkeypatch):
    from app import api

    monkeypatch.setattr(api.common, "store", store)

    async def preview(url):
        if "bad" in url:
            raise Refused("it was http://10.0.0.1/ on this network")
        return {"title": "t"}

    monkeypatch.setattr(linkpreview, "preview", preview)
    client = TestClient(api.app)
    good = client.get("/api/link-preview", params={"url": "https://good.test/"})
    assert good.json() == {"title": "t"} and good.headers["cache-control"] == "private, max-age=604800"
    bad = client.get("/api/link-preview", params={"url": "https://bad.test/"})
    # Not said why, so the endpoint cannot be used to learn what is on this network.
    assert bad.status_code == 404 and bad.json() == {"detail": "No preview"}
    assert bad.headers["cache-control"] == "private, max-age=300"
    assert client.get("/api/link-preview").status_code == 422
