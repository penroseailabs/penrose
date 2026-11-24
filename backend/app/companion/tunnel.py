"""The computer's end of the companion tunnel.

One WebSocket to the coordinator, kept open while the API runs, carries every phone's frames: each frame is a
peer id the coordinator assigned, then the bytes the phone sent (app/companion/crypto.py). A phone that completes
the handshake becomes a `Peer` with session keys; what it sends from then on is small JSON records, encrypted:

    phone → computer   {"t":"req","id":n,"method":"POST","path":"/chats","body":{...}}
                       {"t":"sub","id":n,"run":"<run id>","after":<event id>}     follow a run's events
                       {"t":"sub","id":n,"screen":"<device id>"}               watch the phone's screen
                       {"t":"unsub","id":n}
                       {"t":"push","subscription":{...}|null}                   ring me in the background (or stop)
    computer → phone   {"t":"res","id":n,"status":201,"body":{...}}
                       (a GET the API let be kept adds "cache":"private, max-age=…", which the phone app honours)
                       {"t":"ev","id":n,"eid":<event id>,"data":{...event...}}    one per event, as it happens
                       {"t":"ev","id":n,"data":{"jpeg":"<base64>"}}              one per frame of the screen
                       {"t":"end","id":n}
                       {"t":"paired","phone_id":...} / {"t":"denied"}            the answer to a pairing

A request is played into this API in-process as the companion principal (api/auth.py), which may reach chats,
runs, memory, health and the Google sign-in on the phone, nothing else. Events are followed straight from the database, like the window's
own stream (api/runs.py). The screen is the gateway's live view, capped at companion_stream_fps: watching only,
nothing the phone sends acts on the phone.

Ringing a phone whose app is in the background: a phone that asked for it sends its Web Push subscription here,
inside the ciphertext, and it is kept with the phone (identity.py). The tunnel follows the chat list as the phone
app does (api/updates.py); when a task that was working is completed, waits for the user or failed, it asks the
coordinator, in a plain text record {"t":"push","phone_id","subscription","body","topic"}, to send that phone's push
service a push. `body` is the notification (the chat, its title and Relay's latest reply), already encrypted here
for that phone's subscription (webpush.py): the coordinator signs and posts it but cannot read it. The coordinator
answers {"t":"push_gone","phone_id"} for a subscription the service no longer knows, and it is forgotten."""

import asyncio
import base64
import contextlib
import json
import logging
import re
import secrets
import time
from urllib.parse import urlsplit

import httpx
import websockets
from websockets.asyncio.client import connect

from ..config import settings
from . import crypto, webpush

log = logging.getLogger("companion")
COMPANION_HEADER = "X-Relay-Companion"
# The companion principal's reach: (methods or None for any, path prefix, exact).
ALLOWED = (
    (("GET",), "/api/health", True),
    (("GET",), "/api/auth/session", True),
    (None, "/api/chats", False),
    (None, "/api/runs", False),
    (None, "/api/uploads", False),  # files for a message, in chunks (app/attachments.py)
    (None, "/api/memories", False),
    (None, "/api/memory-settings", True),
    (None, "/api/profile", False),
    (("GET",), "/api/devices", True),
    (("POST",), "/api/devices/presence", True),  # the phone app came up or the user is typing: wake the phone
    (("GET",), "/api/models", True),
    (("GET",), "/api/link-preview", True),  # the title and picture for a link card (app/linkpreview.py)
    (("GET",), "/api/keys", True),
    (("PUT",), "/api/keys", False),
    (None, "/api/voice", False),
    (None, "/api/for-you", False),  # opening and dismissing a briefing, the apps to be briefed on (api/foryou.py)
)
# The guided Google sign-in on the computer's phone (api/devices.py), for a user who is never at the computer: start,
# follow and stop it, and answer what it asks (the account's phone number, the password, Google's terms).
SIGNIN = re.compile(r"/api/devices/[A-Za-z0-9_-]{1,40}/google/signin(/phone|/password|/agree)?")
APPROVAL_SECONDS = 600
RETRY_SECONDS = (1, 2, 5, 10, 20, 30)
MAX_FRAME = 4 * 1024 * 1024
DEVICE_ID = re.compile(r"[A-Za-z0-9_-]{1,40}")
WORKING = ("queued", "running")
HEARD = ("completed", "waiting_user", "failed")  # the statuses the phone rings for (frontend/src/voice/useVoice.ts)
PART = re.compile(rb"Content-Length:\s*(\d+)\r\n\r\n", re.IGNORECASE)


# The API's tunnel, while it runs (api/__init__.py opens it).
current = None


def token() -> str:
    return current.token if current else ""


def subscription_of(value) -> dict | None:
    """A Web Push subscription as the browser gives it (endpoint, keys.p256dh, keys.auth), or None."""
    if not isinstance(value, dict) or not isinstance(value.get("keys"), dict):
        return None
    endpoint, keys = value.get("endpoint"), value["keys"]
    if not isinstance(endpoint, str) or not endpoint.startswith("https://") or len(endpoint) > 2048:
        return None
    if not all(isinstance(keys.get(k), str) and 0 < len(keys[k]) <= 200 for k in ("p256dh", "auth")):
        return None
    return {"endpoint": endpoint, "keys": {"p256dh": keys["p256dh"], "auth": keys["auth"]}}


def finished(before: dict | None, chats: list) -> list[str]:
    """The chats whose task was working at the last look and has something for the user now."""
    if before is None:
        return []
    return [c["id"] for c in chats if before.get(c["id"]) in WORKING and c.get("status") in HEARD]


def unseen(feed: dict) -> dict[int, dict]:
    """The briefings For you has ready that the user has not opened, by id (app/briefings/feed.py). penrose may
    have begun one on its own (briefings/prepare.py): it has a chat, and is still unseen."""
    return {b["id"]: b for b in feed.get("ready") or [] if not b.get("seen")}


def prepared(item: dict) -> bool:
    """Whether penrose's preparing of the briefing is done: it waits for the user with what it found, or ended."""
    return item.get("status") in ("waiting_user", "completed")


def briefing_news(before: dict | None, feed: dict) -> list[tuple[dict, bool]]:
    """What to ring about: (briefing, quiet) for each unseen briefing that is new since the last look (it rings),
    or that penrose has just finished preparing (its notification is replaced, quietly). None at the first look:
    what was there when the tunnel started is not news."""
    if before is None:
        return []
    news = []
    for briefing_id, item in unseen(feed).items():
        if briefing_id not in before:
            news.append((item, False))
        elif prepared(item) and not before[briefing_id]:
            news.append((item, True))
    return news


def briefing_push(item: dict, quiet: bool = False) -> tuple[str, bytes]:
    """A briefing's notification (topic, plaintext): where it is from, and what penrose made of it, or once it is
    prepared, what it found. One topic and tag per briefing, so the later replaces the earlier."""
    app = item.get("app") or {}
    where = f"{app.get('label') or 'penrose'}" + (f" ({app['site']})" if app.get("site") else "")
    if item.get("name") and item.get("name") != app.get("label"):
        where += f" · {item['name']}"
    text = (item.get("found") if prepared(item) else "") or item.get("headline") or ""
    return f"brief-{item['id']}"[:32], webpush.briefing(item["id"], where, text, quiet)


def notice(chat: dict) -> dict:
    """What a phone's notification says about a chat that has something: Relay's reply to the user's latest message
    (the question it waits on, or the result), or for a failed task without one, why it failed."""
    from ..api import common
    from ..api.voice import plain
    from ..runtime import frontdesk

    status = chat.get("status") or ""
    text = ""
    private = False
    try:
        with common.store.connect() as db:
            last = db.execute(
                "SELECT role, content, spoken FROM messages WHERE chat_id=? AND content<>'' ORDER BY created DESC LIMIT 1",
                (chat["id"],),
            ).fetchone()
            run = db.execute("SELECT state FROM runs WHERE chat_id=? ORDER BY updated DESC LIMIT 1", (chat["id"],)).fetchone()
            state = json.loads(run["state"]) if run else {}
            question = frontdesk.question_of(state) if status == "waiting_user" else None
            if question and question.get("question"):
                # What the task waits on; the latest message can be older news or the user's own last answer.
                text = plain(question["question"])
            elif last and last["role"] == "assistant":
                # A reply written for the ear (voice mode) is the shorter, and what the desk will say.
                text = last["spoken"] or plain(last["content"])
            elif status == "failed":
                text = str(state.get("error") or "")
            # A password or code is answered in the app's private field, never typed into a notification.
            questions = (state.get("questions") or []) if status == "waiting_user" else []
            private = any(isinstance(q, dict) and (q.get("sensitive") or q.get("kind") == "login") for q in questions)
    except Exception:  # noqa: BLE001
        log.exception("companion: reading chat %s for its notification", chat["id"])
    fallback = {"waiting_user": "penrose needs you to continue.", "failed": "penrose could not finish this."}
    return {"chat": chat["id"], "title": chat.get("title") or "penrose", "status": status,
            "text": text or fallback.get(status, "Done."), "private": private}


def allowed(method: str, path: str) -> bool:
    if SIGNIN.fullmatch(path):
        return True
    for methods, prefix, exact in ALLOWED:
        if methods and method not in methods:
            continue
        if (path == prefix) if exact else (path == prefix or path.startswith(prefix + "/")):
            return True
    return False


class Peer:
    """One phone's connection through the coordinator."""

    def __init__(self, tunnel, peer_id: bytes):
        self.tunnel = tunnel
        self.id = peer_id
        self.session: crypto.Session | None = None
        self.phone_id: str | None = None
        self.name = ""
        self.approval: str | None = None
        self.subs: dict[int, asyncio.Task] = {}
        self.lock = asyncio.Lock()  # frames leave in counter order
        # A hello names a key; the first frame that opens under the session keys proves the phone holds it.
        self.confirmed = False

    @property
    def linked(self):
        return self.phone_id is not None

    async def handle(self, payload: bytes):
        if self.session is None:
            await self.handshake(payload)
            return
        try:
            record = json.loads(self.session.decrypt(payload))
        except (crypto.HandshakeError, ValueError) as exc:
            log.warning("companion: dropping phone %s: %s", self.id.hex(), exc)
            await self.tunnel.drop(self)
            return
        if not isinstance(record, dict):
            return
        if not self.confirmed:
            self.confirmed = True
            if self.phone_id:
                self.tunnel.identity.seen(self.phone_id)
                self.tunnel.changed()  # online now
        kind = record.get("t")
        if kind == "ping":
            await self.send({"t": "pong"})
        elif not self.linked:
            await self.send({"t": "res", "id": record.get("id"), "status": 403, "body": {"detail": "Not linked yet"}})
        elif kind == "req":
            asyncio.create_task(self.request(record))
        elif kind == "sub":
            self.subscribe(record)
        elif kind == "unsub":
            self.unsubscribe(record.get("id"))
        elif kind == "push":
            subscription = subscription_of(record.get("subscription"))
            self.tunnel.identity.set_push(self.phone_id, subscription)
            log.info("companion: %s %s", self.name, "asks to be rung in the background" if subscription else "no longer asks to be rung")
            self.tunnel.changed()

    async def handshake(self, payload: bytes):
        identity = self.tunnel.identity
        try:
            kind, phone_key, info, secret, e_p = crypto.read_hello(identity.key, payload, identity.secret_of)
        except crypto.HandshakeError as exc:
            await self.tunnel.send_raw(self, crypto.error_frame(str(exc)))
            await self.tunnel.drop(self)
            return
        self.name = str(info.get("name") or "Phone")[:80]
        if kind == crypto.PAIR_HELLO:
            identity.consume(secret)
            reply = {"status": "pending", "name": identity.name, "v": crypto.VERSION}
        else:
            phone = identity.phone_by_key(phone_key)
            if not phone:
                await self.tunnel.send_raw(self, crypto.error_frame("This phone is not linked to this computer"))
                await self.tunnel.drop(self)
                return
            self.phone_id = phone["id"]
            reply = {"status": "linked", "name": identity.name, "phone_id": self.phone_id, "v": crypto.VERSION}
        frame, self.session = crypto.answer_hello(identity.key, payload, phone_key, e_p, secret, reply)
        await self.tunnel.send_raw(self, frame)
        if kind == crypto.PAIR_HELLO:
            self.approval = secrets.token_hex(8)
            identity.approvals[self.approval] = {
                "id": self.approval,
                "name": self.name,
                "code": self.session.code,
                "created": time.time(),
                "expires": time.time() + APPROVAL_SECONDS,
                "peer": self,
                "phone_key": phone_key,
            }
            log.info("companion: %s asks to link (code %s)", self.name, self.session.code)
            self.tunnel.changed()
            # The request lapses after APPROVAL_SECONDS: the window hears of that too.
            asyncio.get_running_loop().call_later(APPROVAL_SECONDS + 1, self.tunnel.changed)

    async def decide(self, allow: bool):
        approval = self.tunnel.identity.approvals.pop(self.approval, None) if self.approval else None
        self.approval = None
        if not approval or self.session is None:
            return
        if allow:
            self.phone_id = self.tunnel.identity.add_phone(approval["phone_key"], self.name)
            await self.send({"t": "paired", "phone_id": self.phone_id, "name": self.tunnel.identity.name})
            log.info("companion: linked %s", self.name)
        else:
            await self.send({"t": "denied"})
            await self.tunnel.drop(self)
        self.tunnel.changed()

    async def send(self, record: dict):
        if self.session is None:
            return
        async with self.lock:
            frame = self.session.encrypt(json.dumps(record, separators=(",", ":")).encode())
            await self.tunnel.send_raw(self, frame)

    async def request(self, record: dict):
        rid = record.get("id")
        method = str(record.get("method") or "GET").upper()
        path = "/api" + str(record.get("path") or "")
        if "?" in path:
            path, _, query = path.partition("?")
        else:
            query = ""
        if "/." in path or "//" in path or not allowed(method, path):
            await self.send({"t": "res", "id": rid, "status": 403, "body": {"detail": "Not available from the phone"}})
            return
        try:
            response = await self.tunnel.client.request(
                method, path + ("?" + query if query else ""), json=record.get("body"), headers=self.tunnel.headers
            )
            content_type = response.headers.get("content-type", "")
            if content_type.startswith(("image/", "audio/")):  # a picture or speech: the records carry JSON, so base64
                key = "image" if content_type.startswith("image/") else "audio"
                body = {key: base64.b64encode(response.content).decode(), "content_type": content_type}
            else:
                try:
                    body = response.json()
                except ValueError:
                    body = response.text
            record = {"t": "res", "id": rid, "status": response.status_code, "body": body}
            if method == "GET" and "max-age" in response.headers.get("cache-control", ""):
                record["cache"] = response.headers["cache-control"]  # the phone app keeps what the API says it may
            await self.send(record)
        except Exception as exc:  # noqa: BLE001 - the phone gets an answer whatever went wrong
            log.exception("companion: request failed")
            await self.send({"t": "res", "id": rid, "status": 500, "body": {"detail": str(exc)}})

    def subscribe(self, record: dict):
        sid = record.get("id")
        run_id = str(record.get("run") or "")
        screen = str(record.get("screen") or "")
        if sid is None or not (run_id or record.get("updates") or DEVICE_ID.fullmatch(screen)):
            return
        self.unsubscribe(sid)
        if record.get("updates"):  # the window's update stream: health, the phone, the chat list
            self.subs[sid] = asyncio.create_task(self.follow_updates(sid))
        elif screen:
            self.subs[sid] = asyncio.create_task(self.follow_screen(sid, screen))
        else:
            self.subs[sid] = asyncio.create_task(self.follow(sid, run_id, int(record.get("after") or 0)))

    def unsubscribe(self, sid):
        task = self.subs.pop(sid, None)
        if task:
            task.cancel()

    async def follow(self, sid, run_id, after):
        # The API's own event follower; imported here, not at the top, as api imports this module.
        from ..api import runs

        try:
            async for event in runs.follow(run_id, after):
                if event is None:
                    continue
                await self.send({"t": "ev", "id": sid, "eid": event["id"], "data": event})
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("companion: event stream failed")
        finally:
            self.subs.pop(sid, None)
            with contextlib.suppress(Exception):
                await self.send({"t": "end", "id": sid})

    async def follow_updates(self, sid):
        from ..api import updates

        changes = updates.stream(companion=False)
        try:
            async for change in changes:
                if change is not None:
                    await self.send({"t": "ev", "id": sid, "data": change})
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("companion: update stream failed")
        finally:
            await changes.aclose()
            self.subs.pop(sid, None)
            with contextlib.suppress(Exception):
                await self.send({"t": "end", "id": sid})

    async def follow_screen(self, sid, device):
        """The phone's screen as the window's live view shows it, a frame at a time and fewer of them."""
        from ..api import common

        try:
            async with common.gateway.stream(
                "GET", f"/devices/{device}/stream", params={"fps": settings.companion_stream_fps}, timeout=None
            ) as response:
                response.raise_for_status()
                async for jpeg in jpeg_frames(response.aiter_bytes()):
                    await self.send({"t": "ev", "id": sid, "data": {"jpeg": base64.b64encode(jpeg).decode()}})
        except asyncio.CancelledError:
            raise
        except httpx.HTTPError as exc:
            log.info("companion: screen stream ended: %s", exc)
        except Exception:  # noqa: BLE001
            log.exception("companion: screen stream failed")
        finally:
            self.subs.pop(sid, None)
            with contextlib.suppress(Exception):
                await self.send({"t": "end", "id": sid})

    def close(self):
        for task in self.subs.values():
            task.cancel()
        self.subs.clear()
        if self.approval:
            self.tunnel.identity.approvals.pop(self.approval, None)
        if self.approval or self.confirmed:
            self.tunnel.changed()


async def jpeg_frames(chunks):
    """The JPEGs in the gateway's MJPEG stream, whose parts each give their Content-Length."""
    buffer = b""
    async for chunk in chunks:
        buffer += chunk
        while (part := PART.search(buffer)) and len(buffer) >= part.end() + int(part[1]):
            end = part.end() + int(part[1])
            yield buffer[part.end() : end]
            buffer = buffer[end:]


class Tunnel:
    def __init__(self, app, identity):
        self.app = app
        self.identity = identity
        self.token = secrets.token_urlsafe(32)
        self.headers = {COMPANION_HEADER: self.token}
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://companion", timeout=120)
        self.peers: dict[bytes, Peer] = {}
        self.ws = None
        self.task: asyncio.Task | None = None
        self.watcher: asyncio.Task | None = None
        # Each chat's status at the last look at the list, to see a task finish (watch).
        self.statuses: dict[str, str | None] | None = None
        # The unseen briefings For you had ready at the last look, each with whether it was prepared, to see a new one
        # arrive or one get ready (watch); and their chats, whose task penrose began on its own and whose news the
        # briefing's own notification gives.
        self.briefed: dict[int, bool] | None = None
        self.preparing_chats: set[str] = set()
        self.connected = False
        self.last_error = ""
        # Told when what status() reports changes (api/updates.py pushes it to the window).
        self.on_change = None

    def changed(self):
        if self.on_change:
            self.on_change()

    @property
    def url(self):
        parts = urlsplit(self.identity.origin)
        scheme = "wss" if parts.scheme == "https" else "ws"
        return f"{scheme}://{parts.netloc}/ws/{self.identity.owner}?role=desktop"

    def start(self):
        if self.identity.origin and not self.task:
            self.task = asyncio.create_task(self.run())

    async def restart(self):
        """After the origin changed: drop the connection (and every phone on it) and connect to the new one."""
        await self._cancel()
        self.last_error = ""
        self.start()

    async def _cancel(self):
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
            self.task = None

    async def stop(self):
        await self._cancel()
        await self.client.aclose()

    async def run(self):
        attempt = 0
        while True:
            try:
                async with connect(self.url, max_size=MAX_FRAME, ping_interval=20, ping_timeout=20) as ws:
                    self.ws = ws
                    self.connected = True
                    self.last_error = ""
                    attempt = 0
                    self.changed()
                    log.info("companion: connected to the coordinator")
                    self.watcher = asyncio.create_task(self.watch())
                    async for message in ws:
                        await self.receive(message)
            except asyncio.CancelledError:
                raise
            except (OSError, websockets.exceptions.WebSocketException, ValueError) as exc:
                self.last_error = str(exc) or exc.__class__.__name__
                log.info("companion: coordinator connection: %s", self.last_error)
                self.changed()
            finally:
                if self.watcher:
                    self.watcher.cancel()
                    self.watcher = None
                if self.connected:
                    self.changed()
                self.connected = False
                self.ws = None
                for peer in list(self.peers.values()):
                    peer.close()
                self.peers.clear()
            await asyncio.sleep(RETRY_SECONDS[min(attempt, len(RETRY_SECONDS) - 1)])
            attempt += 1

    async def receive(self, message):
        if isinstance(message, str):
            try:
                control = json.loads(message)
            except ValueError:
                return
            peer_id = bytes.fromhex(str(control.get("peer") or ""))
            if control.get("t") == "close" and peer_id in self.peers:
                self.peers.pop(peer_id).close()
            elif control.get("t") == "push_gone" and isinstance(control.get("phone_id"), str):
                log.info("companion: phone %s can no longer be rung; forgetting its subscription", control["phone_id"])
                self.identity.set_push(control["phone_id"], None)
                self.changed()
            return
        if len(message) < 9:
            return
        peer_id, payload = bytes(message[:8]), bytes(message[8:])
        peer = self.peers.get(peer_id)
        if peer is None:
            peer = self.peers[peer_id] = Peer(self, peer_id)
        await peer.handle(payload)

    async def send_raw(self, peer: Peer, frame: bytes):
        if self.ws is None:
            return
        with contextlib.suppress(websockets.exceptions.WebSocketException):
            await self.ws.send(peer.id + frame)

    async def drop(self, peer: Peer):
        peer.close()
        self.peers.pop(peer.id, None)
        if self.ws is not None:
            with contextlib.suppress(websockets.exceptions.WebSocketException):
                await self.ws.send(json.dumps({"t": "close", "peer": peer.id.hex()}))

    async def watch(self):
        """Follow the chat list and For you while connected; when a task finishes, or a briefing is newly ready, ring
        the phones that asked."""
        from ..api import updates

        changes = updates.stream(companion=False)
        try:
            async for change in changes:
                if change is None:
                    continue
                if "chats" in change:
                    done = [c for c in finished(self.statuses, change["chats"]) if c not in self.preparing_chats]
                    self.statuses = {c["id"]: c.get("status") for c in change["chats"]}
                    if done:
                        chats = {c["id"]: c for c in change["chats"]}
                        await self.ring([await asyncio.to_thread(notice, chats[chat_id]) for chat_id in done])
                if "for_you" in change:
                    news = briefing_news(self.briefed, change["for_you"])
                    # Kept across reconnects: a briefing that became ready while the tunnel was down still rings.
                    waiting = unseen(change["for_you"])
                    self.briefed = {i: prepared(b) for i, b in waiting.items()}
                    self.preparing_chats = {b["chat_id"] for b in waiting.values() if b.get("chat_id")}
                    if news:
                        await self.push([briefing_push(item, quiet) for item, quiet in news])
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("companion: watching the chat list failed")
        finally:
            await changes.aclose()

    async def ring(self, notices: list[dict]):
        """Ring every phone that asked about each chat's notice (notice())."""
        await self.push([
            (item["chat"][:32], webpush.message(item["chat"], item["title"], item["status"], item["text"], item.get("private", False)))
            for item in notices
        ])

    async def push(self, messages: list[tuple[str, bytes]]):
        """Ask the coordinator to push each (topic, message) to every phone that asked, encrypted for that phone. A
        push with the topic of one not yet delivered replaces it."""
        if self.ws is None:
            return
        for phone_id, subscription in self.identity.pushes():
            for topic, message in messages:
                record = {"t": "push", "phone_id": phone_id, "subscription": subscription, "topic": topic}
                try:
                    record["body"] = webpush.b64url(webpush.encrypt(subscription, message))
                except (ValueError, KeyError):
                    # a subscription key that is not a P-256 point: the coordinator's own ring, which says nothing
                    log.warning("companion: cannot encrypt for phone %s; ringing without the text", phone_id)
                with contextlib.suppress(websockets.exceptions.WebSocketException):
                    await self.ws.send(json.dumps(record, separators=(",", ":")))
            log.info("companion: rang phone %s with %d notification(s)", phone_id, len(messages))

    async def decide(self, approval_id: str, allow: bool) -> bool:
        approval = self.identity.approvals.get(approval_id)
        if not approval:
            return False
        await approval["peer"].decide(allow)
        return True

    def status(self):
        self.identity.sweep()
        return {
            "enabled": bool(self.identity.origin),
            "origin": self.identity.origin,
            "connected": self.connected,
            "error": self.last_error,
            "name": self.identity.name,
            "online": sorted({p.phone_id for p in self.peers.values() if p.phone_id and p.confirmed}),
            "pending": [
                {k: a[k] for k in ("id", "name", "code", "created", "expires")}
                for a in self.identity.approvals.values()
            ],
        }
