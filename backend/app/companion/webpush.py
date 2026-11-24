"""What a phone's notification says, encrypted on the computer for that phone alone.

A Web Push body is encrypted to the subscription's own key (RFC 8291, `aes128gcm` from RFC 8188). Doing it here, not
in the coordinator, means the coordinator only signs and posts bytes it cannot read, and the push service (Google's,
for Chrome) carries them unread: the text of a reply is readable on the phone and nowhere in between. The coordinator
(coordinator/src/push.ts) has the same encryption for its own content-free ring, used by older computers."""

import base64
import json
import os
import struct

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

RECORD_SIZE = 4096
# A push service takes at most 4096 bytes of body: the header (salt 16, record size 4, key length 1, key 65), the
# padding delimiter (1) and the AES-GCM tag (16) leave this much for the message itself.
MAX_PLAINTEXT = RECORD_SIZE - 86 - 17
BODY_CHARS = 1200  # a notification is a glance; the whole reply is in the chat


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _hkdf(ikm: bytes, salt: bytes, info: bytes, length: int) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)


def encrypt(subscription: dict, plaintext: bytes) -> bytes:
    """`plaintext` as one aes128gcm record that only the subscription's browser can open."""
    if len(plaintext) > MAX_PLAINTEXT:
        raise ValueError("too long for one push")
    phone_key = unb64url(subscription["keys"]["p256dh"])
    auth = unb64url(subscription["keys"]["auth"])
    local = ec.generate_private_key(ec.SECP256R1())
    local_key = local.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    remote = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), phone_key)
    shared = local.exchange(ec.ECDH(), remote)
    ikm = _hkdf(shared, auth, b"WebPush: info\0" + phone_key + local_key, 32)
    salt = os.urandom(16)
    cek = _hkdf(ikm, salt, b"Content-Encoding: aes128gcm\0", 16)
    nonce = _hkdf(ikm, salt, b"Content-Encoding: nonce\0", 12)
    ciphertext = AESGCM(cek).encrypt(nonce, plaintext + b"\x02", None)  # 2: the last record's padding delimiter
    return salt + struct.pack(">IB", RECORD_SIZE, len(local_key)) + local_key + ciphertext


def message(chat_id: str, title: str, status: str, text: str, private: bool = False) -> bytes:
    """The notification as the phone's service worker reads it (frontend/src/companion/sw.ts), cut to fit. `private`:
    the task waits for a password or code, so the notification offers no typed reply."""
    return fitted({"t": "ring", "chat": chat_id, "title": title[:120], "status": status, "private": private}, text)


def briefing(briefing_id: int, title: str, text: str, quiet: bool = False) -> bytes:
    """A briefing in For you (app/briefings), as the service worker reads it: tapping the notification opens the
    briefing, as tapping its card does. `quiet`: it replaces the briefing's earlier notification without ringing
    again (penrose has prepared it)."""
    return fitted({"t": "brief", "brief": briefing_id, "title": title[:120], "quiet": quiet}, text)


def fitted(payload: dict, text: str) -> bytes:
    """`payload` with `text` as its body, the body cut until the whole fits in one push."""
    text = text.strip()
    if len(text) > BODY_CHARS:
        text = text[: BODY_CHARS - 1].rstrip() + "…"
    while True:
        data = json.dumps({**payload, "body": text}, ensure_ascii=False, separators=(",", ":")).encode()
        if len(data) <= MAX_PLAINTEXT or not text:
            return data
        text = text[: max(0, len(text) * 3 // 4 - 1)].rstrip() + "…"
