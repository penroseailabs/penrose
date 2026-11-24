# penrose on your phone

The companion is penrose's window on the user's own phone: the same chat, streamed live, from anywhere. It is linked
to the computer by scanning a QR code, with no account anywhere. Everything between the phone and the computer is
encrypted end to end; the only server in between, the coordinator, passes ciphertext it cannot read and keeps nothing.
The computer's database stays the one copy of every chat: lose the computer and the history is gone, clear the phone
and it is simply no longer linked.

## Parts

| Where | What |
| --- | --- |
| `backend/app/companion/` | the computer's side: its identity key, the phones it trusts, the handshake and frames, and the tunnel to the coordinator that plays a phone's requests into the API |
| `backend/app/api/companion.py` | what the window calls: show a QR code, allow or deny a phone, unlink one |
| `frontend/src/companion/` | the phone's side: the same crypto in WebCrypto, the tunnel with `fetch` and `EventSource` shims, the pairing screens, the installable app's entry point |
| `frontend/src/components/Phones.tsx` | the "penrose on your phone" sheet in Settings |
| `coordinator/` | a Cloudflare Worker with one Durable Object per mailbox; it also serves the companion app (`frontend/dist-companion`) |

## How linking works

1. The computer has a long-lived X25519 key and a random owner secret, made once (`<data dir>/companion/`). Its
   mailbox, the name the coordinator routes by, is the SHA-256 of that secret: phones learn the mailbox from the QR
   code, but only the secret attaches as the computer's end of it. The phone makes its own key pair in the browser,
   non-exportable, kept in IndexedDB.
2. "Link a phone" shows a QR code: the companion app's address with the computer's public key, the mailbox and a
   one-time 256-bit pairing secret after the `#`, which browsers never send to any server. It is good for ten minutes.
3. The phone opens the address, and the two run a Noise-IK-style handshake through the coordinator with the secret as a
   pre-shared key (`crypto.py` describes the messages). Only a phone that saw the screen can complete it, and the
   computer learns the phone's key from it. Both sides derive the same six-digit code.
4. The window shows "<phone> wants to link" with the code; the phone shows the same code. Allow, and the phone's key is
   pinned. From then on every connection runs the same handshake without the secret, with fresh ephemeral keys, and
   every frame is AES-GCM under per-direction keys with counters, so nothing can be replayed.

The coordinator sees a mailbox name, connection timing and frame sizes. It cannot read a frame, cannot impersonate
either side (it never has the pairing secret or either private key), and cannot attach a phone that the computer has
not approved. When a phone asks to be rung in the background (docs/VOICE.md), the coordinator also sends Web Push on
the computer's word: it holds the mailbox's VAPID key and is handed the phone's push subscription with each push,
and what it sends says only that there is something.

## What a phone may do

A linked phone's requests are played into this API as the companion principal (`api/auth.py`), which reaches chats,
runs, memory and health, nothing else: no provisioning, no settings, no linking, no sign-ins. The live phone view is
not sent to the phone.

## Running it

Nobody runs a server for anyone else. The title bar's phone button (or Settings, "penrose on your phone") sets the
companion up with one click: the desktop app opens a Cloudflare sign-in in the browser (a free account), registers
a `workers.dev` address if the account has none, and deploys the bundled coordinator and companion app to that
account with wrangler, which ships inside penrose (`desktop/src/companion.js`). The address is kept by the API in
`<data dir>/companion/origin` and the QR code appears as soon as the computer is connected. An app update with a
newer companion deploys again by itself; the sign-in token stays in `<data dir>/cloudflare`.

In development the same button works from `npm run dev` (it needs `npm run build:companion` in `frontend/` and
`npm install` in `coordinator/`), or `COMPANION_ORIGIN` points the API at a coordinator deployed by hand:

```bash
cd frontend && npm run build:companion          # the installable app, into frontend/dist-companion
cd coordinator && npm run dev                   # the coordinator and the app on http://127.0.0.1:8787
cd coordinator && npm run deploy                # or to your own Cloudflare account
```

For a phone to reach a development coordinator on this computer it needs HTTPS: `cloudflared tunnel --url
http://127.0.0.1:8787` gives a temporary address. The phone remembers the address it linked through, so a phone
linked through a temporary address must scan again once the coordinator has a permanent one. The address is also
the installed app's identity on the phone, which is why the permanent one matters.

## Installing on a phone

Scan the QR code with the phone's camera app. On Android, Chrome pairs in the tab and offers to install; the installed
app shares the tab's storage, so it opens linked. On an iPhone, a home-screen app has storage of its own, so the page
asks the user to add it to the home screen first (the manifest's start URL carries the QR code's address) and the app
pairs when it is first opened; "Continue in Safari instead" pairs in the tab.
