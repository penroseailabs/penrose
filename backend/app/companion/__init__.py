"""The companion: Relay on the user's own phone, linked to this computer by a QR code.

    crypto.py    the keys, the handshake and the frames (the phone's copy: frontend/src/companion/crypto.ts)
    identity.py  this computer's key and mailbox, the phones it trusts, pairings in progress
    tunnel.py    the connection to the coordinator and what linked phones may do through it

The coordinator (coordinator/) only passes ciphertext between a computer's mailbox and its phones."""
