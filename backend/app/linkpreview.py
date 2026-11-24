"""Link previews: the title, description and picture a page names for itself (Open Graph), for the chat's link cards.

The app's windows cannot read other sites' pages (browsers refuse it), so this computer reads them on the window's
behalf, when a link is shown, and keeps nothing: the answer goes out with a Cache-Control header (api/links.py), so the
window's own cache holds it, and it never reaches the database or an agent.

The address is the agent's (it came from a page or a message), so it is never trusted:

- Only http(s) on ports 80 and 443, without a name or password in the address.
- A name must resolve only to public addresses, checked when the connection is made and on every redirect, and the
  connection goes to the address that was checked (so a name cannot answer differently the second time). Addresses
  on this computer or its network (127.x, 10.x, 192.168.x, link-local, the cloud metadata address) are refused.
- No cookies and no referrer are sent; at most four redirects; 8 seconds in all; a page is read only to the end of
  its head (the tags are all that is wanted) and never past 2 MB; 2 MB of picture.
- The picture is decoded and re-drawn as a small JPEG here, so the window never opens bytes a stranger chose.
- Text is returned as text and cut short; the window draws it as text.

    preview(url) -> {"url", "host", "site", "title", "description", "image" (a data: URL or None)}

Raises Refused when there is nothing to show (or the address is not allowed)."""

import asyncio
import base64
import io
import ipaddress
import logging
import re
import socket
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpcore
from PIL import Image

log = logging.getLogger("linkpreview")

PORTS = {"http": 80, "https": 443}
PAGE_BYTES = 2 * 1024 * 1024  # a ceiling: reading stops at the end of the head (YouTube's is 800 KB in)
IMAGE_BYTES = 2 * 1024 * 1024
IMAGE_PIXELS = 24_000_000
MIN_IMAGE_WIDTH = 80
THUMB = 640
REDIRECTS = 4
TIMEOUT = 8.0
AT_ONCE = 4
USER_AGENT = "Mozilla/5.0 (compatible; LinkPreview/1.0)"
HEAD_END = re.compile(rb"</head\s*>|<body[\s>]", re.IGNORECASE)
CHARSET = re.compile(rb"""charset\s*=\s*["']?\s*([A-Za-z0-9_.:-]+)""", re.IGNORECASE)
TITLE_KEYS = ("og:title", "twitter:title")
TEXT_KEYS = ("og:description", "twitter:description", "description")
IMAGE_KEYS = (
    "og:image", "og:image:secure_url", "og:image:url", "twitter:image", "twitter:image:src", "link:image_src", "image"
)
ATTEMPTS = 3  # a page that names nothing for itself is asked again: some sites answer a bare request with a stripped page
BARE_TITLE = re.compile(r"^[\s\-\u2013\u2014|\u00b7:]")  # " - YouTube": the page's own title was left out


class Refused(Exception):
    """Nothing to show for this address, or it is not one to fetch."""


def public(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%")[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


async def public_address(host: str, port: int) -> str:
    """The address `host` names, when every address it names is public; IPv4 first."""
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        raise Refused("unknown host") from None
    addresses = sorted({info[4][0] for info in infos}, key=lambda a: ":" in a)
    if not addresses or not all(public(a) for a in addresses):
        raise Refused("not a public address")
    return addresses[0]


class PublicOnly(httpcore.AsyncNetworkBackend):
    """Connects only to the public address a name resolves to (the one just checked, so it cannot change between)."""

    def __init__(self):
        self.inner = httpcore.AnyIOBackend()

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        address = await public_address(host, port)
        return await self.inner.connect_tcp(
            address, port, timeout=timeout, local_address=local_address, socket_options=socket_options
        )

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise Refused("no sockets")

    async def sleep(self, seconds):
        await asyncio.sleep(seconds)


def allowed(url: str) -> str:
    try:
        parts = urlsplit(url)
        port = parts.port or PORTS.get(parts.scheme)
    except ValueError:
        raise Refused("bad address") from None
    if parts.scheme not in PORTS or not parts.hostname or parts.username or parts.password or port != PORTS[parts.scheme]:
        raise Refused("not an address to fetch")
    return parts.netloc


async def fetch(pool, url: str, limit: int, until=None) -> tuple[str, str, bytes, bool]:
    """-> (final address, content type, the first `limit` bytes, whether there was more). Follows a few redirects.
    With `until` (a pattern), reading stops as soon as it has appeared."""
    for _ in range(REDIRECTS + 1):
        host = allowed(url)
        headers = {
            "Host": host,
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,image/*;q=0.8",
            "Accept-Language": "en",
            "Accept-Encoding": "identity",
        }
        async with pool.stream("GET", url, headers=headers) as response:
            sent = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in response.headers}
            if response.status in (301, 302, 303, 307, 308) and sent.get("location"):
                url = urljoin(url, sent["location"])
                continue
            if response.status != 200:
                raise Refused(f"status {response.status}")
            body = b""
            more = False
            async for chunk in response.aiter_stream():
                body += chunk
                if len(body) > limit:
                    body, more = body[:limit], True
                    break
                if until and until.search(body, max(0, len(body) - len(chunk) - 16)):
                    break
            return url, sent.get("content-type", "").lower(), body, more
    raise Refused("too many redirects")


class Head(HTMLParser):
    """The meta tags and title of a page's head."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.title = ""
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        a = {k: v for k, v in attrs if v is not None}
        if tag == "meta":
            key = (a.get("property") or a.get("name") or a.get("itemprop") or "").strip().lower()
            if key and a.get("content"):
                self.meta.setdefault(key, a["content"])
        elif tag == "link" and "image_src" in a.get("rel", "").lower().split() and a.get("href"):
            self.meta.setdefault("link:image_src", a["href"])
        elif tag == "title" and not self.title:
            self.in_title = True

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False

    def handle_data(self, data):
        if self.in_title:
            self.title += data


def decode(body: bytes, content_type: str) -> str:
    found = CHARSET.search(content_type.encode()) or CHARSET.search(body[:2048])
    try:
        return body.decode(found.group(1).decode() if found else "utf-8", errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def clean(text: str, limit: int) -> str | None:
    text = " ".join((text or "").split())
    if BARE_TITLE.match(text):
        return None
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text or None


def first(meta: dict[str, str], keys) -> str:
    return next((meta[k] for k in keys if meta.get(k)), "")


def thumbnail(data: bytes) -> str | None:
    """The picture as a small JPEG data URL, or None when it is not a picture worth showing."""
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in ("JPEG", "PNG", "WEBP", "GIF") or image.width * image.height > IMAGE_PIXELS:
                return None
            if image.width < MIN_IMAGE_WIDTH:
                return None
            image.thumbnail((THUMB, THUMB))
            flat = Image.new("RGB", image.size, "white")
            rgba = image.convert("RGBA")
            flat.paste(rgba, mask=rgba.getchannel("A"))
            out = io.BytesIO()
            flat.save(out, "JPEG", quality=78, optimize=True)
    except Exception:  # noqa: BLE001 - anything that is not a clean picture is simply not shown
        return None
    return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode()


async def page(pool, url: str) -> tuple[str, Head]:
    """The final address and the head of the page: asked again (up to ATTEMPTS) while it names nothing."""
    for attempt in range(ATTEMPTS):
        final, content_type, body, _ = await fetch(pool, url, PAGE_BYTES, HEAD_END)
        if "html" not in content_type:
            raise Refused("not a page")
        end = HEAD_END.search(body)
        head = Head()
        head.feed(decode(body[: end.start()] if end else body, content_type))
        if first(head.meta, TITLE_KEYS) or first(head.meta, IMAGE_KEYS):
            break
        if attempt < ATTEMPTS - 1:
            await asyncio.sleep(0.25)
    return final, head


async def read(url: str) -> dict:
    async with httpcore.AsyncConnectionPool(network_backend=PublicOnly(), max_connections=4, retries=0) as pool:
        final, head = await page(pool, url)
        host = (urlsplit(final).hostname or "").removeprefix("www.")
        found = {
            "url": final,
            "host": host,
            "site": clean(head.meta.get("og:site_name", ""), 80),
            "title": clean(first(head.meta, TITLE_KEYS) or head.title, 200),
            "description": clean(first(head.meta, TEXT_KEYS), 300),
            "image": None,
        }
        source = first(head.meta, IMAGE_KEYS)
        if source:
            try:
                _, kind, data, more = await fetch(pool, urljoin(final, source.strip()), IMAGE_BYTES)
                if kind.startswith("image/") and not more:
                    found["image"] = thumbnail(data)
            except (Refused, httpcore.ConnectError, httpcore.TimeoutException, httpcore.ProtocolError) as exc:
                log.info("link preview: no picture for %s (%s)", host, exc)
    if not (found["title"] or found["image"]):
        raise Refused("nothing on the page to show")
    return found


# The reads under way, so a link asked for twice at once is read once; and how many are read at a time.
under_way: dict[str, asyncio.Task] = {}
slots = None


async def preview(url: str) -> dict:
    global slots
    allowed(url)
    if slots is None:
        slots = asyncio.Semaphore(AT_ONCE)

    async def work():
        async with slots:
            async with asyncio.timeout(TIMEOUT):
                return await read(url)

    task = under_way.get(url)
    if task is None:
        task = under_way[url] = asyncio.create_task(work())
        task.add_done_callback(lambda _t: under_way.pop(url, None))
    try:
        return await asyncio.shield(task)
    except (Refused, OSError, TimeoutError, httpcore.TimeoutException, httpcore.NetworkError, httpcore.ProtocolError) as exc:
        log.info("link preview: %s: %s", urlsplit(url).hostname, exc)
    except Exception:  # noqa: BLE001 - a page that breaks the reader still just has no card
        log.exception("link preview failed")
    raise Refused("no preview")
