"""The companion: the handshake both ways, what a linked phone may reach, and the window's linking API."""

import asyncio
import json

import pytest
from app import api
from app.api import common
from app.companion import crypto, tunnel, webpush
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from fastapi.testclient import TestClient


@pytest.fixture
def client(store, monkeypatch):
    monkeypatch.setattr(common, "store", store)
    return TestClient(api.app)


@pytest.fixture
def computer():
    key = X25519PrivateKey.generate()
    return key, crypto.public_bytes(key)


def pair(computer, phone, secret, reply=None):
    key, public = computer
    hello = phone.pair_hello(public, secret)
    kind, phone_key, info, used, e_p = crypto.read_hello(
        key, hello, lambda sid: secret if sid == crypto.secret_id(secret) else None
    )
    assert kind == crypto.PAIR_HELLO and used == secret and info["name"] == phone.name
    frame, computer_session = crypto.answer_hello(key, hello, phone_key, e_p, secret, reply or {"status": "pending"})
    got, phone_session = phone.read_hello_ok(frame)
    return got, computer_session, phone_session, phone_key


def test_pairing_agrees_on_keys_and_code(computer):
    secret = crypto.new_secret()
    phone = crypto.Phone("Pixel 9")
    reply, computer_session, phone_session, _ = pair(computer, phone, secret)
    assert reply == {"status": "pending"}
    assert computer_session.code == phone_session.code and len(phone_session.code) == 6
    frame = phone_session.encrypt(b'{"t":"ping"}')
    assert computer_session.decrypt(frame) == b'{"t":"ping"}'
    assert phone_session.decrypt(computer_session.encrypt(b"pong")) == b"pong"
    with pytest.raises(crypto.HandshakeError):  # replayed
        computer_session.decrypt(frame)


def test_session_hello_identifies_the_phone(computer):
    key, public = computer
    phone = crypto.Phone()
    _, _, _, phone_key = pair(computer, phone, crypto.new_secret())
    hello = phone.session_hello(public)
    kind, again, _, used, _ = crypto.read_hello(key, hello, lambda sid: None)
    assert kind == crypto.SESSION_HELLO and again == phone_key and used == b""


def test_wrong_secret_or_key_fails(computer):
    key, public = computer
    secret = crypto.new_secret()
    phone = crypto.Phone()
    hello = phone.pair_hello(public, secret)
    with pytest.raises(crypto.HandshakeError):  # another secret with the same id cannot open it
        crypto.read_hello(key, hello, lambda sid: crypto.new_secret())
    with pytest.raises(crypto.HandshakeError):  # no such pairing
        crypto.read_hello(key, hello, lambda sid: None)
    other = X25519PrivateKey.generate()
    with pytest.raises(crypto.HandshakeError):  # not the computer whose key was in the QR code
        crypto.read_hello(other, hello, lambda sid: secret)
    with pytest.raises(crypto.HandshakeError):
        crypto.read_hello(key, b"", lambda sid: secret)


def test_phone_rejects_a_reply_from_a_stranger(computer):
    key, public = computer
    secret = crypto.new_secret()
    phone = crypto.Phone()
    hello = phone.pair_hello(public, secret)
    kind, phone_key, _, _, e_p = crypto.read_hello(key, hello, lambda sid: secret)
    stranger = X25519PrivateKey.generate()
    frame, _ = crypto.answer_hello(stranger, hello, phone_key, e_p, secret, {"status": "pending"})
    with pytest.raises(crypto.HandshakeError):
        phone.read_hello_ok(frame)
    with pytest.raises(crypto.HandshakeError, match="declined"):
        phone.read_hello_ok(crypto.error_frame("declined"))


def test_what_a_phone_may_reach():
    rows = [
        ("GET", "/api/health", True),
        ("GET", "/api/auth/session", True),
        ("POST", "/api/auth/login", False),
        ("POST", "/api/chats", True),
        ("GET", "/api/chats/abc", True),
        ("POST", "/api/runs/r1/answer", True),
        ("GET", "/api/memories", True),
        ("PUT", "/api/memories/m1", True),
        ("DELETE", "/api/memories/m1", True),
        ("PUT", "/api/memory-settings", True),
        ("GET", "/api/profile", True),
        ("POST", "/api/profile/locale", True),
        ("GET", "/api/memoriesx", False),
        ("GET", "/api/link-preview", True),
        ("POST", "/api/link-preview", False),
        ("GET", "/api/keys", True),
        ("PUT", "/api/keys/anthropic", True),
        ("DELETE", "/api/keys/anthropic", False),
        ("POST", "/api/keys/anthropic", False),
        ("GET", "/api/devices", True),
        ("POST", "/api/devices/presence", True),
        ("POST", "/api/devices/emulator-5570/wake", False),
        ("GET", "/api/devices/emulator-5570/stream", False),
        ("POST", "/api/devices/emulator-5570/actions", False),
        ("GET", "/api/companion", False),
        ("GET", "/api/phone/view", False),
        ("GET", "/api/chatsx", False),
        ("GET", "/api/for-you", True),
        ("POST", "/api/for-you/12/open", True),
        ("PUT", "/api/for-you/apps/com.whatsapp", True),
        ("GET", "/api/for-youx", False),
    ]
    for method, path, ok in rows:
        assert tunnel.allowed(method, path) is ok


class FakeTunnel:
    """Enough of Tunnel for a Peer: an identity and a place frames go."""

    def __init__(self, identity):
        self.identity = identity
        self.sent = []
        self.dropped = []

    async def send_raw(self, peer, frame):
        self.sent.append(frame)

    async def drop(self, peer):
        self.dropped.append(peer)

    def changed(self):
        pass


class FakeIdentity:
    def __init__(self, key):
        self.key = key
        self.name = "Desk"
        self.pairings = {}
        self.approvals = {}
        self.phones = {}

    def secret_of(self, sid):
        return self.pairings.get(sid)

    def consume(self, secret):
        self.pairings.pop(crypto.secret_id(secret), None)

    def phone_by_key(self, pubkey):
        return self.phones.get(pubkey)

    def add_phone(self, pubkey, name):
        self.phones[pubkey] = {"id": "p1", "name": name}
        return "p1"

    def seen(self, phone_id):
        pass

    push = {}

    def set_push(self, phone_id, subscription):
        self.push[phone_id] = subscription

    def pushes(self):
        return [(k, v) for k, v in self.push.items() if v]


def test_peer_pairs_waits_for_approval_then_links(computer):
    key, public = computer
    identity = FakeIdentity(key)
    fake = FakeTunnel(identity)
    secret = crypto.new_secret()
    identity.pairings[crypto.secret_id(secret)] = secret
    phone = crypto.Phone("Pixel")
    peer = tunnel.Peer(fake, b"\x01" * 8)

    asyncio.run(peer.handle(phone.pair_hello(public, secret)))
    reply, phone_session = phone.read_hello_ok(fake.sent[-1])
    assert reply["status"] == "pending" and reply["name"] == "Desk"
    assert not peer.linked and identity.pairings == {}  # the secret is spent
    (approval,) = identity.approvals.values()
    assert approval["name"] == "Pixel" and approval["code"] == phone_session.code

    # Not linked yet: requests are refused, the phone is told so.
    asyncio.run(peer.handle(phone_session.encrypt(b'{"t":"req","id":1,"method":"GET","path":"/health"}')))
    assert json.loads(phone_session.decrypt(fake.sent[-1])) == {
        "t": "res", "id": 1, "status": 403, "body": {"detail": "Not linked yet"}
    }

    asyncio.run(peer.decide(True))
    assert peer.linked and identity.phones and identity.approvals == {}
    assert json.loads(phone_session.decrypt(fake.sent[-1]))["t"] == "paired"


def test_peer_turns_away_an_unknown_phone_and_bad_frames(computer):
    key, public = computer
    fake = FakeTunnel(FakeIdentity(key))
    phone = crypto.Phone()
    peer = tunnel.Peer(fake, b"\x02" * 8)
    asyncio.run(peer.handle(phone.session_hello(public)))
    assert fake.sent[-1][0] == crypto.ERROR and fake.dropped == [peer]

    peer = tunnel.Peer(fake, b"\x03" * 8)
    asyncio.run(peer.handle(b"\x07garbage"))
    assert fake.sent[-1][0] == crypto.ERROR


def test_window_api_needs_a_tunnel(client):
    response = client.get("/api/companion")
    assert response.status_code == 503


def test_window_api_lists_phones_and_approvals(client, computer, monkeypatch):
    key, _ = computer
    identity = FakeIdentity(key)
    identity.phones_rows = []

    class Running(tunnel.Tunnel):
        def __init__(self):
            self.identity = identity
            self.connected = True
            self.last_error = ""
            self.peers = {}
            self.token = "t"

        def status(self):
            return {"enabled": True, "connected": True, "pending": [], "online": [], "name": "Desk", "origin": "x", "error": ""}

    identity.phones = lambda: [{"id": "p1", "name": "Pixel", "created": 1, "last_seen": 2}]
    monkeypatch.setattr(tunnel, "current", Running())
    body = client.get("/api/companion").json()
    assert body["phones"] == [{"id": "p1", "name": "Pixel", "created": 1, "last_seen": 2}]
    assert client.post("/api/companion/pending/nope", json={"allow": True}).status_code == 404


def test_mailbox_is_the_hash_of_the_owner_secret(store, monkeypatch, tmp_path):
    """Phones and the coordinator see the mailbox; only the owner secret, which stays on this computer, opens
    the computer's end of it (coordinator/src/index.ts hashes the same way)."""
    import base64
    import hashlib

    from app.companion import identity as companion_identity
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "companion_origin", "https://relay.example")
    first = companion_identity.Identity(store)
    digest = hashlib.sha256(first.owner.encode()).digest()
    assert first.mailbox == base64.urlsafe_b64encode(digest).decode().rstrip("=")
    assert first.owner not in first.new_pairing()["url"]  # the QR carries the mailbox, never the secret
    assert first.mailbox in first.new_pairing()["url"]
    again = companion_identity.Identity(store)  # made once, kept
    assert (again.owner, again.mailbox, again.public) == (first.owner, first.mailbox, first.public)


def test_origin_is_kept_in_the_data_dir_and_set_from_the_window(client, store, monkeypatch, tmp_path):
    """Without an address the window sees "not set up"; the desktop app deploys the coordinator and tells the
    API the address, which starts the tunnel and survives a restart."""
    from app.companion import identity as companion_identity
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "companion_origin", "")
    running = tunnel.Tunnel(api.app, companion_identity.Identity(store))
    monkeypatch.setattr(tunnel, "current", running)
    assert running.identity.origin == ""
    body = client.get("/api/companion").json()
    assert body["enabled"] is False and body["phones"] == []
    assert client.post("/api/companion/pairings").status_code == 503
    assert client.put("/api/companion/origin", json={"origin": "ftp://x"}).status_code == 400

    body = client.put("/api/companion/origin", json={"origin": "https://relay-companion.me.workers.dev/"}).json()
    assert body["enabled"] and body["origin"] == "https://relay-companion.me.workers.dev"
    assert running.url == "wss://relay-companion.me.workers.dev/ws/" + running.identity.owner + "?role=desktop"
    assert companion_identity.Identity(store).origin == "https://relay-companion.me.workers.dev"


def mjpeg(*frames):
    return b"".join(
        b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(f)).encode() + b"\r\n\r\n" + f + b"\r\n"
        for f in frames
    )


def test_jpeg_frames_finds_each_frame_however_the_stream_is_cut():
    stream = mjpeg(b"\xff\xd8one\xff\xd9", b"\xff\xd8two, longer\xff\xd9")

    async def frames(size):
        async def chunks():
            for i in range(0, len(stream), size):
                yield stream[i : i + size]

        return [f async for f in tunnel.jpeg_frames(chunks())]

    for size in (1, 7, len(stream)):
        assert asyncio.run(frames(size)) == [b"\xff\xd8one\xff\xd9", b"\xff\xd8two, longer\xff\xd9"]


def linked_peer(computer):
    """A peer for a phone that is already linked, and that phone's session keys."""
    key, public = computer
    identity = FakeIdentity(key)
    fake = FakeTunnel(identity)
    phone = crypto.Phone()
    identity.phones[crypto.public_bytes(phone.key)] = {"id": "p1", "name": phone.name}
    peer = tunnel.Peer(fake, b"\x04" * 8)
    asyncio.run(peer.handle(phone.session_hello(public)))
    reply, session = phone.read_hello_ok(fake.sent[-1])
    assert reply["status"] == "linked"
    return peer, fake, session


def test_phone_watches_the_screen_at_the_companion_rate(computer, monkeypatch):
    import base64

    import httpx

    asked = []

    def gateway(request):
        asked.append(request.url)
        return httpx.Response(200, content=mjpeg(b"frame-1", b"frame-2"))

    monkeypatch.setattr(common, "gateway", httpx.AsyncClient(transport=httpx.MockTransport(gateway), base_url="http://gw"))
    monkeypatch.setattr(tunnel.settings, "companion_stream_fps", 3)
    peer, fake, session = linked_peer(computer)

    async def watch():
        await peer.handle(session.encrypt(b'{"t":"sub","id":5,"screen":"device-1"}'))
        await asyncio.gather(*peer.subs.values())

    asyncio.run(watch())
    records = [json.loads(session.decrypt(frame)) for frame in fake.sent[1:]]
    assert [r["t"] for r in records] == ["ev", "ev", "end"]
    assert [base64.b64decode(r["data"]["jpeg"]) for r in records[:2]] == [b"frame-1", b"frame-2"]
    assert str(asked[0]) == "http://gw/devices/device-1/stream?fps=3"


def test_screen_subscription_takes_only_a_device_id(computer):
    peer, fake, session = linked_peer(computer)
    asyncio.run(peer.handle(session.encrypt(b'{"t":"sub","id":5,"screen":"../runs"}')))
    assert peer.subs == {} and len(fake.sent) == 1  # nothing followed, nothing sent


# --- ringing in the background -----------------------------------------------------------------------------------

SUBSCRIPTION = {"endpoint": "https://push.example/send/abc", "keys": {"p256dh": "BPhone", "auth": "AUTH"}}


def test_phone_asks_to_be_rung_and_can_stop(computer):
    peer, fake, session = linked_peer(computer)
    fake.identity.push = {}
    record = json.dumps({"t": "push", "subscription": {**SUBSCRIPTION, "expirationTime": None}})
    asyncio.run(peer.handle(session.encrypt(record.encode())))
    assert fake.identity.push == {"p1": SUBSCRIPTION}  # only the subscription's own fields are kept
    asyncio.run(peer.handle(session.encrypt(b'{"t":"push","subscription":null}')))
    assert fake.identity.push == {"p1": None}


def test_what_is_not_a_subscription():
    rows = [
        None,
        "https://push.example/send/abc",
        {"endpoint": "http://push.example/send/abc", "keys": SUBSCRIPTION["keys"]},
        {"endpoint": "https://push.example/send/abc", "keys": {"p256dh": "BPhone"}},
        {"endpoint": "https://push.example/send/abc", "keys": {"p256dh": 7, "auth": "AUTH"}},
        {"endpoint": "https://push.example/" + "x" * 2100, "keys": SUBSCRIPTION["keys"]},
    ]
    for value in rows:
        assert tunnel.subscription_of(value) is None


def test_a_task_that_was_working_and_now_has_something_counts_as_finished():
    before = {"a": "running", "b": "queued", "c": "running", "d": "completed", "e": "running"}
    now = [
        {"id": "a", "status": "completed"},
        {"id": "b", "status": "waiting_user"},
        {"id": "c", "status": "running"},
        {"id": "d", "status": "completed"},  # finished before the last look: not news
        {"id": "f", "status": "failed"},  # never seen working: not news
        {"id": "e", "status": "failed"},
    ]
    assert tunnel.finished(before, now) == ["a", "b", "e"]
    assert tunnel.finished(None, now) == []  # the first look says where things stand


def phone_subscription(endpoint="https://push.example/send/real"):
    """A subscription with real keys, as a browser makes one, and the private key the phone keeps."""
    import os

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    public = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    auth = os.urandom(16)
    return {"endpoint": endpoint, "keys": {"p256dh": webpush.b64url(public), "auth": webpush.b64url(auth)}}, key


def open_push(body: bytes, subscription: dict, key) -> bytes:
    """What the phone's browser does with a push body (RFC 8291), written out independently of webpush.encrypt."""
    import struct

    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    salt, (size, key_length) = body[:16], struct.unpack(">IB", body[16:21])
    sender = body[21 : 21 + key_length]
    assert size == 4096 and key_length == 65
    shared = key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), sender))
    mine = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    auth = webpush.unb64url(subscription["keys"]["auth"])
    hkdf = lambda ikm, salt, info, n: HKDF(hashes.SHA256(), n, salt, info).derive(ikm)  # noqa: E731
    ikm = hkdf(shared, auth, b"WebPush: info\0" + mine + sender, 32)
    cek = hkdf(ikm, salt, b"Content-Encoding: aes128gcm\0", 16)
    nonce = hkdf(ikm, salt, b"Content-Encoding: nonce\0", 12)
    record = AESGCM(cek).decrypt(nonce, body[21 + key_length :], None)
    assert record.endswith(b"\x02")
    return record[:-1]


NOTICE = {"chat": "c" * 32, "title": "Install Outlook", "status": "waiting_user", "text": "Approve the sign-in in Authenticator."}


def test_ringing_sends_each_phone_the_notice_encrypted_for_it_and_forgets_a_gone_subscription(computer):
    key, _ = computer
    identity = FakeIdentity(key)
    real, phone_key = phone_subscription()
    identity.push = {"p1": real, "p2": None, "p3": {**SUBSCRIPTION, "endpoint": "https://push.example/send/p3"}}

    class Socket:
        def __init__(self):
            self.sent = []

        async def send(self, data):
            self.sent.append(data)

    t = tunnel.Tunnel.__new__(tunnel.Tunnel)
    t.identity = identity
    t.ws = Socket()
    t.on_change = None
    t.peers = {}
    asyncio.run(t.ring([NOTICE]))
    records = [json.loads(frame) for frame in t.ws.sent]
    assert [(r["phone_id"], r["subscription"], r["topic"]) for r in records] == [
        ("p1", real, "c" * 32),
        ("p3", identity.push["p3"], "c" * 32),
    ]
    # The phone, and only the phone, reads what the notification says.
    message = json.loads(open_push(webpush.unb64url(records[0]["body"]), real, phone_key))
    assert message == {"t": "ring", "chat": "c" * 32, "title": "Install Outlook", "status": "waiting_user",
                       "body": "Approve the sign-in in Authenticator.", "private": False}
    assert "Authenticator" not in t.ws.sent[0]
    # A subscription whose key is not a real one still rings, with the coordinator's content-free push.
    assert "body" not in records[1]
    asyncio.run(t.receive(json.dumps({"t": "push_gone", "peer": "", "phone_id": "p3"})))
    assert identity.push["p3"] is None
    t.ws = None
    asyncio.run(t.ring([NOTICE]))  # not connected: nothing to send, nothing breaks


def test_a_long_reply_is_cut_to_fit_one_push():
    message = webpush.message("c" * 32, "Title", "completed", "word " * 5000)
    body = json.loads(message)["body"]
    assert len(message) <= webpush.MAX_PLAINTEXT and body.endswith("\u2026") and len(body) <= webpush.BODY_CHARS
    wide = webpush.message("c" * 32, "Title", "completed", "\U0001d11e" * 1199)  # 4 bytes a character
    assert len(wide) <= webpush.MAX_PLAINTEXT and json.loads(wide)["body"].endswith("\u2026")


def test_a_notice_says_the_reply_or_why_it_stopped(store, monkeypatch):
    monkeypatch.setattr(common, "store", store)
    with store.connect() as db:
        db.execute("INSERT INTO chats (id, title, created) VALUES ('a', 'Outlook', 1)")
        db.execute("INSERT INTO messages (chat_id, role, content, created) VALUES ('a', 'user', 'Install Outlook', 1)")
        db.execute(
            "INSERT INTO messages (chat_id, role, content, created) VALUES "
            "('a', 'assistant', 'Approve it in **Microsoft Authenticator**. Enter [60](https://x.example).', 2)"
        )
        db.execute("INSERT INTO chats (id, title, created) VALUES ('b', 'Reservation', 1)")
        db.execute("INSERT INTO messages (chat_id, role, content, created) VALUES ('b', 'user', 'Book a table', 1)")
    got = tunnel.notice({"id": "a", "title": "Outlook", "status": "waiting_user"})
    assert got == {"chat": "a", "title": "Outlook", "status": "waiting_user",
                   "text": "Approve it in Microsoft Authenticator. Enter 60.", "private": False}
    # Nothing from Relay since the user's message: a plain line for the status.
    assert tunnel.notice({"id": "b", "title": "Reservation", "status": "failed"})["text"] == "penrose could not finish this."
    assert tunnel.notice({"id": "b", "title": "", "status": "completed"}) == {
        "chat": "b", "title": "penrose", "status": "completed", "text": "Done.", "private": False}
    # Waiting for a code: the notification must not offer to take it.
    with store.connect() as db:
        db.execute(
            "INSERT INTO runs (id, chat_id, status, state, updated) VALUES ('r', 'a', 'waiting_user', ?, 3)",
            (json.dumps({"questions": [{"node_id": "n", "question": "The code?", "sensitive": True}]}),),
        )
    assert tunnel.notice({"id": "a", "title": "Outlook", "status": "waiting_user"})["private"] is True
    assert tunnel.notice({"id": "a", "title": "Outlook", "status": "completed"})["private"] is False


def test_watching_the_chat_list_rings_when_a_task_finishes(computer, monkeypatch):
    from app.api import updates

    key, _ = computer
    identity = FakeIdentity(key)
    identity.push = {"p1": SUBSCRIPTION}
    rung = []

    class Socket:
        async def send(self, data):
            rung.append(json.loads(data))

    lists = [[{"id": "a", "status": "running"}], [{"id": "a", "status": "running"}], [{"id": "a", "status": "completed"}]]

    async def stream(companion=True):
        assert companion is False
        for chats in lists:
            yield {"chats": chats}
            yield None  # a keepalive

    monkeypatch.setattr(updates, "stream", stream)
    monkeypatch.setattr(tunnel, "notice", lambda chat: {**NOTICE, "chat": chat["id"]})
    t = tunnel.Tunnel.__new__(tunnel.Tunnel)
    t.identity = identity
    t.ws = Socket()
    t.statuses, t.preparing_chats = None, set()
    asyncio.run(t.watch())
    assert [(r["phone_id"], r["topic"]) for r in rung] == [("p1", "a")]
    assert t.statuses == {"a": "completed"}


def test_identity_keeps_a_phone_subscription_until_it_is_dropped(store, monkeypatch, tmp_path):
    from app.companion import identity as companion_identity
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    identity = companion_identity.Identity(store)
    phone_id = identity.add_phone(b"k" * 32, "Pixel")
    assert identity.pushes() == [] and identity.phones()[0]["push"] is False
    identity.set_push(phone_id, SUBSCRIPTION)
    assert identity.pushes() == [(phone_id, SUBSCRIPTION)] and identity.phones()[0]["push"] is True
    assert companion_identity.Identity(store).pushes() == [(phone_id, SUBSCRIPTION)]  # kept, and the table migrates once
    identity.set_push(phone_id, None)
    assert identity.pushes() == []
    identity.set_push(phone_id, SUBSCRIPTION)
    assert identity.forget(phone_id) and identity.pushes() == []


def test_a_get_the_api_lets_be_kept_tells_the_phone_how_long():
    import httpx

    class Client:
        async def request(self, method, path, json=None, headers=None):
            kept = {"cache-control": "private, max-age=604800"} if path.startswith("/api/link-preview") else {}
            return httpx.Response(200, json={"title": "t"}, headers=kept)

    fake = FakeTunnel(FakeIdentity(None))
    fake.client, fake.headers = Client(), {}
    peer = tunnel.Peer(fake, b"\x01" * 8)
    sent = []

    async def send(record):
        sent.append(record)

    peer.send = send
    asyncio.run(peer.request({"id": 1, "method": "GET", "path": "/link-preview?url=https%3A%2F%2Fa.test%2F"}))
    asyncio.run(peer.request({"id": 2, "method": "GET", "path": "/models"}))
    assert sent[0]["cache"] == "private, max-age=604800" and sent[0]["body"] == {"title": "t"}
    assert "cache" not in sent[1]


def test_a_briefing_rings_when_it_arrives_and_quietly_says_what_penrose_found_once_prepared(computer, monkeypatch):
    from app.api import updates

    key, _ = computer
    identity = FakeIdentity(key)
    real, phone_key = phone_subscription()
    identity.push = {"p1": real}
    rung = []

    class Socket:
        async def send(self, data):
            rung.append(json.loads(data))

    release_sync = {"id": 7, "app": {"package": "com.microsoft.teams", "label": "Teams"}, "name": "Release sync",
                "headline": "Release sync on v7 at 8", "seen": False}
    shift = {**release_sync, "id": 8, "name": "Teams", "headline": "Your shift moved"}
    working = {**shift, "chat_id": "c8", "status": "running"}
    ready = {**working, "status": "waiting_user", "found": "Moved to 9 am Thursday; nothing clashes"}
    feeds = [
        {"ready": [release_sync]},  # there when the tunnel started: not news
        {"ready": [release_sync, shift]},  # new: it rings
        {"ready": [release_sync, working]},  # penrose prepares it on its own: nothing to say yet
        {"ready": [release_sync, ready]},  # prepared: its notification is replaced, quietly
        {"ready": [release_sync, {**ready, "seen": True}, {**release_sync, "id": 9, "seen": True}]},  # opened: not news
    ]
    chats = [[{"id": "c8", "status": "running"}], [{"id": "c8", "status": "waiting_user"}]]

    async def stream(companion=True):
        for feed in feeds[:3]:
            yield {"for_you": feed}
        yield {"chats": chats[0]}
        yield {"for_you": feeds[3]}
        yield {"chats": chats[1]}  # its task waits for the user: the briefing's notification said so already
        yield {"for_you": feeds[4]}

    monkeypatch.setattr(updates, "stream", stream)
    t = tunnel.Tunnel.__new__(tunnel.Tunnel)
    t.identity, t.ws, t.statuses, t.briefed, t.preparing_chats = identity, Socket(), None, None, set()
    asyncio.run(t.watch())
    assert [(r["phone_id"], r["topic"]) for r in rung] == [("p1", "brief-8"), ("p1", "brief-8")]
    first, then = (json.loads(open_push(webpush.unb64url(r["body"]), real, phone_key)) for r in rung)
    # An app's own alert is named by the app alone; a conversation's by the app and the conversation.
    assert first == {"t": "brief", "brief": 8, "title": "Teams", "quiet": False, "body": "Your shift moved"}
    assert then == {"t": "brief", "brief": 8, "title": "Teams", "quiet": True, "body": "Moved to 9 am Thursday; nothing clashes"}
    assert json.loads(tunnel.briefing_push(release_sync)[1])["title"] == "Teams · Release sync"
