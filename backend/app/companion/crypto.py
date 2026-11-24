"""The companion's keys and wire protocol: what the computer and a phone agree on, and nothing the coordinator
can read.

Each side has a long-lived X25519 key (the computer's is `identity`, kept in the data dir; the phone's lives in
its browser). A phone is linked by scanning a QR code off the computer's screen, which carries the computer's
public key and a one-time pairing secret. The handshake is a Noise-IK-style exchange with that secret mixed in
as a pre-shared key, so only a phone that saw the screen can complete it, and the computer learns the phone's
key from the one message it could only have sent with the secret. Afterwards the two keys are pinned and every
connection runs the same exchange without the secret.

    hello    (phone)     T | e_p | AEAD(k1, P | info)          k1 from X25519(e_p, D) and the secret, if pairing
    hello-ok (computer)  3 | e_d | AEAD(k, reply)              k from the three X25519 shares and the transcript
    data     (either)    16 | counter | AEAD(k_dir, payload)   one key per direction, counters never repeat

The phone-side implementation is frontend/src/companion/crypto.ts; the two must stay in step."""

import hashlib
import json
import os
import struct

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

VERSION = 1
PAIR_HELLO = 1  # a phone with the pairing secret from the QR code
SESSION_HELLO = 2  # a linked phone
HELLO_OK = 3
ERROR = 4  # plaintext JSON: the computer declines before any key is agreed
DATA = 16

KEY = 32
NONCE_ZERO = bytes(12)


class HandshakeError(Exception):
    pass


def hkdf(ikm: bytes, salt: bytes, info: bytes, length: int = KEY) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt or None, info=info).derive(ikm)


def seal(key: bytes, nonce: bytes, data: bytes, ad: bytes = b"") -> bytes:
    return AESGCM(key).encrypt(nonce, data, ad)


def open_(key: bytes, nonce: bytes, data: bytes, ad: bytes = b"") -> bytes:
    try:
        return AESGCM(key).decrypt(nonce, data, ad)
    except InvalidTag as exc:
        raise HandshakeError("bad ciphertext") from exc


def public_bytes(key: X25519PrivateKey) -> bytes:
    return key.public_key().public_bytes_raw()


def shared(private: X25519PrivateKey, public: bytes) -> bytes:
    return private.exchange(X25519PublicKey.from_public_bytes(public))


class Session:
    """The keys of one connection, after the handshake: a key and a counter per direction."""

    def __init__(self, root: bytes, initiator: bool):
        to_phone = hkdf(root, b"", b"relay-companion-d2p")
        to_computer = hkdf(root, b"", b"relay-companion-p2d")
        self.send_key, self.recv_key = (to_computer, to_phone) if initiator else (to_phone, to_computer)
        self.sent = 0
        self.received = 0
        # Shown on both screens while the user approves a new phone: proof they are looking at the same handshake.
        code = int.from_bytes(hkdf(root, b"", b"relay-companion-sas", 4), "big") % 1_000_000
        self.code = f"{code:06d}"

    def encrypt(self, payload: bytes) -> bytes:
        self.sent += 1
        counter = struct.pack(">Q", self.sent)
        return bytes([DATA]) + counter + seal(self.send_key, bytes(4) + counter, payload)

    def decrypt(self, frame: bytes) -> bytes:
        if len(frame) < 9 or frame[0] != DATA:
            raise HandshakeError("not a data frame")
        counter = struct.unpack(">Q", frame[1:9])[0]
        if counter <= self.received:
            raise HandshakeError("replayed frame")
        payload = open_(self.recv_key, bytes(4) + frame[1:9], frame[9:])
        self.received = counter
        return payload


def _hello_key(ss1: bytes, secret: bytes) -> bytes:
    return hkdf(ss1, secret, b"relay-companion-hello-v1")


def _root(ss1: bytes, ss2: bytes, ss3: bytes, secret: bytes, transcript: bytes) -> bytes:
    return hkdf(ss1 + ss2 + ss3, secret, b"relay-companion-session-v1" + hashlib.sha256(transcript).digest())


# --- the computer's side ----------------------------------------------------------------------------------


def read_hello(identity: X25519PrivateKey, frame: bytes, secret_of) -> tuple[int, bytes, dict, bytes, bytes]:
    """Open a phone's hello. `secret_of(sid)` returns the pairing secret with that id, or None, for a pairing
    hello. Returns (type, the phone's public key, its info, the secret used, the ephemeral key)."""
    if not frame:
        raise HandshakeError("empty hello")
    kind = frame[0]
    if kind == PAIR_HELLO:
        if len(frame) < 1 + 8 + 32 + 16:
            raise HandshakeError("short hello")
        secret = secret_of(frame[1:9])
        if not secret:
            raise HandshakeError("unknown or expired pairing code")
        head, e_p, body = frame[:9], frame[9:41], frame[41:]
    elif kind == SESSION_HELLO:
        if len(frame) < 1 + 32 + 16:
            raise HandshakeError("short hello")
        secret = b""
        head, e_p, body = frame[:1], frame[1:33], frame[33:]
    else:
        raise HandshakeError("unexpected message")
    k1 = _hello_key(shared(identity, e_p), secret)
    plain = open_(k1, NONCE_ZERO, body, head + e_p)
    if len(plain) < 32:
        raise HandshakeError("short hello body")
    phone_key = plain[:32]
    try:
        info = json.loads(plain[32:] or b"{}")
    except ValueError as exc:
        raise HandshakeError("bad hello info") from exc
    if not isinstance(info, dict):
        raise HandshakeError("bad hello info")
    return kind, phone_key, info, secret, e_p


def answer_hello(identity: X25519PrivateKey, hello: bytes, phone_key: bytes, e_p: bytes, secret: bytes, reply: dict):
    """The computer's reply and the connection's keys."""
    e_d = X25519PrivateKey.generate()
    e_d_pub = public_bytes(e_d)
    ss1 = shared(identity, e_p)
    ss2 = shared(e_d, phone_key)
    ss3 = shared(e_d, e_p)
    root = _root(ss1, ss2, ss3, secret, hello + e_d_pub)
    session = Session(root, initiator=False)
    body = seal(root, NONCE_ZERO, json.dumps(reply).encode(), b"hello-ok" + e_d_pub)
    return bytes([HELLO_OK]) + e_d_pub + body, session


def error_frame(message: str) -> bytes:
    return bytes([ERROR]) + json.dumps({"error": message}).encode()


# --- the phone's side, for tests ---------------------------------------------------------------------------


class Phone:
    """A phone as the computer sees it, in Python: the tests pair and talk with it."""

    def __init__(self, name="Test phone"):
        self.key = X25519PrivateKey.generate()
        self.name = name
        self.hello = b""
        self.e_p = None
        self.secret = b""
        self.computer_key = b""

    def pair_hello(self, computer_key: bytes, secret: bytes) -> bytes:
        return self._hello(PAIR_HELLO, computer_key, secret, hashlib.sha256(secret).digest()[:8])

    def session_hello(self, computer_key: bytes) -> bytes:
        return self._hello(SESSION_HELLO, computer_key, b"", b"")

    def _hello(self, kind, computer_key, secret, sid):
        self.e_p = X25519PrivateKey.generate()
        self.secret, self.computer_key = secret, computer_key
        e_p_pub = public_bytes(self.e_p)
        head = bytes([kind]) + sid
        plain = public_bytes(self.key) + json.dumps({"name": self.name, "v": VERSION}).encode()
        k1 = _hello_key(shared(self.e_p, computer_key), secret)
        self.hello = head + e_p_pub + seal(k1, NONCE_ZERO, plain, head + e_p_pub)
        return self.hello

    def read_hello_ok(self, frame: bytes) -> tuple[dict, Session]:
        if frame and frame[0] == ERROR:
            raise HandshakeError(json.loads(frame[1:]).get("error", "declined"))
        if len(frame) < 1 + 32 + 16 or frame[0] != HELLO_OK:
            raise HandshakeError("unexpected reply")
        e_d_pub = frame[1:33]
        ss1 = shared(self.e_p, self.computer_key)
        ss2 = shared(self.key, e_d_pub)
        ss3 = shared(self.e_p, e_d_pub)
        root = _root(ss1, ss2, ss3, self.secret, self.hello + e_d_pub)
        reply = json.loads(open_(root, NONCE_ZERO, frame[33:], b"hello-ok" + e_d_pub))
        return reply, Session(root, initiator=True)


def new_secret() -> bytes:
    return os.urandom(32)


def secret_id(secret: bytes) -> bytes:
    return hashlib.sha256(secret).digest()[:8]
