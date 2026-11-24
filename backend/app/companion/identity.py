"""Who this computer is to its phones, and which phones it trusts.

The identity key and the owner secret are made once and kept in the data dir. The mailbox, the name the
coordinator routes by, is the hash of the owner secret: phones and the coordinator know the mailbox, only this
computer knows its preimage, and only the preimage attaches as the computer's end of the mailbox. Linked phones are rows in the database: their public key, a name, when they
were last here. A pairing is a one-time secret shown in a QR code for a few minutes; a phone that completes the
handshake with it waits in `approvals` until the user allows it in the window."""

import base64
import contextlib
import hashlib
import json
import os
import secrets
import socket
import time
from urllib.parse import quote

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from ..config import settings
from . import crypto

PAIRING_SECONDS = 600  # long enough to add the page to an iPhone's home screen and open it
TABLE = "CREATE TABLE IF NOT EXISTS companion_phones(id TEXT PRIMARY KEY,pubkey TEXT UNIQUE,name TEXT,created REAL,last_seen REAL)"
# `push`: the phone's Web Push subscription (JSON, as the browser gives it) while it asks to be rung in the background.
PUSH_COLUMN = "ALTER TABLE companion_phones ADD COLUMN push TEXT"


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class Identity:
    def __init__(self, store):
        self.store = store
        self.store.execute(TABLE)
        if "push" not in {row["name"] for row in self.store.rows("PRAGMA table_info(companion_phones)")}:
            self.store.execute(PUSH_COLUMN)
        folder = settings.data_dir / "companion"
        folder.mkdir(parents=True, exist_ok=True)
        self.key = self._load_key(folder / "identity.key")
        self.owner = self._load_text(folder / "owner", lambda: secrets.token_urlsafe(32))
        # Where the coordinator and companion app are deployed: set from the window (the desktop app deploys it
        # to the user's own Cloudflare account) and kept here, or COMPANION_ORIGIN in development.
        self.origin_path = folder / "origin"
        self.origin = settings.companion_origin.rstrip("/") or self._load_text(self.origin_path, lambda: "").rstrip("/")
        self.mailbox = b64(hashlib.sha256(self.owner.encode()).digest())
        self.public = crypto.public_bytes(self.key)
        self.name = settings.companion_name or socket.gethostname() or "This computer"
        # Pairings shown as QR codes, by secret id: {sid: (secret, expires)}.
        self.pairings: dict[bytes, tuple[bytes, float]] = {}
        # Phones that completed a pairing handshake and wait for the user: {id: approval dict}.
        self.approvals: dict[str, dict] = {}

    @staticmethod
    def _load_text(path, make):
        with contextlib.suppress(OSError):
            text = path.read_text().strip()
            if text:
                return text
        text = make()
        if not text:
            return text
        path.write_text(text)
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)
        return text

    def _load_key(self, path):
        with contextlib.suppress(OSError, ValueError):
            raw = path.read_bytes()
            if len(raw) == 32:
                return X25519PrivateKey.from_private_bytes(raw)
        key = X25519PrivateKey.generate()
        path.write_bytes(key.private_bytes_raw())
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)
        return key

    def set_origin(self, origin: str):
        self.origin = origin.strip().rstrip("/")
        self.origin_path.write_text(self.origin)

    # --- pairings -----------------------------------------------------------------------------------------

    def new_pairing(self) -> dict:
        """A fresh QR code: the companion page's address with everything the phone needs after the #."""
        self.sweep()
        secret = crypto.new_secret()
        expires = time.time() + PAIRING_SECONDS
        self.pairings[crypto.secret_id(secret)] = (secret, expires)
        origin = self.origin
        fragment = (
            f"v={crypto.VERSION}&m={self.mailbox}&d={b64(self.public)}&s={b64(secret)}"
            f"&n={quote(self.name)}&x={int(expires)}"
        )
        return {"url": f"{origin}/#{fragment}", "expires": expires}

    def sweep(self):
        now = time.time()
        for sid in [s for s, (_, expires) in self.pairings.items() if expires < now]:
            del self.pairings[sid]
        for key in [k for k, a in self.approvals.items() if a["expires"] < now]:
            self.approvals.pop(key, None)

    def secret_of(self, sid: bytes):
        self.sweep()
        entry = self.pairings.get(sid)
        return entry[0] if entry else None

    def consume(self, secret: bytes):
        self.pairings.pop(crypto.secret_id(secret), None)

    # --- phones -------------------------------------------------------------------------------------------

    def phones(self):
        rows = self.store.rows("SELECT id,name,created,last_seen,push FROM companion_phones ORDER BY created")
        return [{**row, "push": row["push"] is not None} for row in rows]

    def phone_by_key(self, pubkey: bytes):
        rows = self.store.rows("SELECT * FROM companion_phones WHERE pubkey=?", (b64(pubkey),))
        return rows[0] if rows else None

    def add_phone(self, pubkey: bytes, name: str) -> str:
        phone_id = secrets.token_hex(8)
        now = time.time()
        self.store.execute(
            "INSERT OR REPLACE INTO companion_phones(id,pubkey,name,created,last_seen) VALUES (?,?,?,?,?)", (phone_id, b64(pubkey), name[:80], now, now)
        )
        return phone_id

    def seen(self, phone_id: str):
        self.store.execute("UPDATE companion_phones SET last_seen=? WHERE id=?", (time.time(), phone_id))

    # --- ringing in the background ------------------------------------------------------------------------

    def set_push(self, phone_id: str, subscription: dict | None):
        """The phone's push subscription, or None when it no longer wants to be rung."""
        value = json.dumps(subscription, separators=(",", ":")) if subscription else None
        self.store.execute("UPDATE companion_phones SET push=? WHERE id=?", (value, phone_id))

    def pushes(self) -> list[tuple[str, dict]]:
        """Each phone that asked to be rung, with its subscription."""
        rows = self.store.rows("SELECT id,push FROM companion_phones WHERE push IS NOT NULL ORDER BY created")
        out = []
        for row in rows:
            with contextlib.suppress(ValueError):
                out.append((row["id"], json.loads(row["push"])))
        return out

    def forget(self, phone_id: str) -> bool:
        before = self.store.rows("SELECT id FROM companion_phones WHERE id=?", (phone_id,))
        self.store.execute("DELETE FROM companion_phones WHERE id=?", (phone_id,))
        return bool(before)
