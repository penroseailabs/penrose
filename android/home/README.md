# Relay home: the agent-first home screen

The phone's default launcher, replaced by a search box that both the user and the agent use. Sesame and
Nova search apps and their shortcuts by name and land on the right screen inside the app; this does the
same, and answers the agent over a socket in a format made for it: numbered lines, each with the actions it
accepts, so "not installed" becomes "install from Play Store" and an app's Orders screen is one call away.

```
search "doordash orders"
[1] Shortcut "Orders" DoorDash {open}
[2] App "DoorDash" com.dd.doordash {open | info}
[3] Activity "Order History" DoorDash (internal) {open}
[4] Web "doordash orders" {google | maps | youtube | play}
```

## Sources (phase 0)

| kind | from | actions |
|---|---|---|
| App | launcher activities (PackageManager) | open, info |
| Shortcut | published App Shortcuts, static + dynamic + pinned (LauncherApps; needs the HOME role) | open |
| Activity | every exported activity in every manifest, scanned on install: `OrderHistoryActivity` reads "Order History" (internal ones wait for the bridge, phase 2) | open |
| Contact | ContactsContract | call, sms, whatsapp, email, show |
| Setting | a table of Settings pages | open |
| Mail | every query, when Gmail is installed: the emails Gmail's server search finds, in the background (below), and a row with Gmail's own search intent (`ACTION_SEARCH` to `MailActivityGmail`) | open (the email in Gmail; Gmail on its results) |
| Play | when no app or shortcut matched: the query as a Play Store search | install |
| Web | always last: the query as a search elsewhere | google (Chrome), maps, youtube, play |

A shortcut or activity matches on its app's name and its own label together ("whatsapp jordan", "jordan"); a
lone word that is an app's name lists the app, not its screens. Ranking: whole-word > word prefix >
substring > subsequence, exact match on top. At most 50 results; the Mail, Play and Web rows stay when the
phone's own hits fill the 50.

## Every result on the agent's screen

The bridge reads only what is on screen, and a ListView builds only the rows that fit, so the agent would
see 10 of 50 results and scroll for the rest. `ResultsList` describes every result to accessibility
instead: one virtual node per result (text `title · kind subtitle`, its row number, its actions by name),
marked visible because each is a tap away. The agent's screen lists all of them:

```
[2] List<ResultsList> id=results items=50 {scroll down}
  [3] Result "Settings · App com.android.settings" list=results#1/50 actions=open/info
  ...
  [52] Result "e · Web" list=results#50/50 actions=google/maps/youtube/play
```

A tap on any of them runs its first action and a long press opens the chooser with all of them, through
the accessibility action, scrolled into view or not. A row off screen has an empty box at the list's edge.
The rows draw their own text (`RowView`), so the rows on screen are not read a second time.

## Gmail, searched in the background

Every query also asks Gmail, without Gmail coming on screen, so the agent's one search brings emails along
with apps, contacts and the rest. Gmail runs its live server search only for callers outside its own process,
and its mail provider is private, so the search runs as root in `GmailHelper`, a small process started from
this APK through su (`app_process`, process name `relay-gmail`):

1. `Gmail.java` starts it when a query is typed (`warm`), talks to it over stdin/stdout (one JSON line each
   way: `{"id":1,"q":"invoice","limit":25}` -> `{"id":1,"emails":[...],"server":true,"ms":900,"steps":"..."}`)
   and keeps it for the next search. It exits after five minutes without a search (a task's searches come
   minutes apart), or when the home app goes away, so it uses memory (about 45 MB) only around searches.
2. It asks Gmail's content providers directly, as Android's `content` tool does, but in one process: each
   `content` run starts a new Java process (about a second), which made the old shell version take 6-17 s.
3. It searches through the mail contract Gmail serves its widget and the AOSP mail UI (UIProvider), by
   column name: each account's `searchUri` (from `gm2.accountcache`, so every account in Gmail) with
   `query` and `query_identifier`, the search folder's `conversationListUri`, and the conversation columns
   (subject, snippet, date, read, starred, attachments, messages). The list first holds the matches already
   on the phone; Gmail signals it when its servers answer, and its extras flag
   `cursor_received_server_results`. This is Gmail's server search, not the synced mail only: results go
   back over a year. If the servers do not answer in 6 s, the phone's matches come back, marked so; while
   they have just failed to answer (the last half minute), the next searches wait 2.5 s for them.
4. The sender comes from each row's `conversationInfo` (message count, draft count, three snippets, the
   sender's name and address). It is used only when its count matches the row's `numMessages` and the
   address has an @; otherwise (a changed Gmail) the sender comes from the conversation's message list,
   which is slower (Gmail fetches the message). Unknown columns read as null, so a Gmail update that drops
   one loses a field, not the search.

Root comes from Relay's phone setup: a Magisk policy for this app with no toast or log
(`adbui.home_root` in backend/app/provision), and the global setting
`relay_home_root` = 1. Without that setting no su is run, since an ungranted su would put Magisk's prompt on
screen. The query reaches the helper as a JSON string, never through a shell.

A search takes about a second, nearly all of it Gmail's servers (the first one after the helper starts
adds about half a second). The screen shows the phone's own results at once, with a spinner (the agent's screen reads
`{spinner}`) and the Gmail row saying "looking for emails…"; Gmail is asked once typing pauses (400 ms), and
the emails appear as Mail rows, newest first, at most 25:

```
[7] Result "Your order has shipped · Mail Amazon · Oct 22 · unread · Your package is on its way…" actions=open
[8] Result "amazon order · Mail search Gmail · 25 emails" actions=open
```

Gmail runs one search at a time, so queries wait their turn in one queue (`Mail.java`), and nobody waits on it
longer than they said: a search answers at its caller's deadline with what the phone has, the Gmail row saying
"NOT SEARCHED: Gmail has not answered yet", while the search goes on in its turn and its answer is kept for
whoever asks next. The Gmail row says "no emails found" only when Gmail's servers answered that; without their
answer it says NOT SEARCHED and why, and `"retry": true` asks them again. Measured on a test phone: a second or two per query on a phone in use; 9 s, 5 s, then 2 s
each for the first searches after a wake from Doze; four at once on a phone awake for a minute, 7 to 11 s.

An answer from Gmail's servers is kept for two minutes; one without them (the phone's matches alone, or a
failure) for ten seconds, so the next search asks again. `open` on an email uses the contract's conversation link with
the account's mail type (how Gmail's widget opens one) and falls back to Gmail's search for its subject and
sender. Without root, or without an account, the Gmail row says why and still opens the search.

## The agent socket

Abstract unix socket `relayhome`, newline-delimited JSON, only adb and root may connect (the bridge's
dialect). `adb forward tcp:0 localabstract:relayhome`, then:

```
{"cmd":"hello"}                       -> {"ok":true,"version":"0.5.0","default_home":true,"apps":42}
{"cmd":"search","q":"maps"}           -> {"ok":true,"text":"[1] App \"Maps\" ...","results":[{"n":1,"kind":"App",...}],"mail":"answered"}
                                         (waits for Gmail's emails, "wait_ms" at most (15 s); "mail":false answers
                                         without them. "mail" in the answer: answered (Gmail's servers), phone_only,
                                         failed, pending (not in by the deadline: search again), off;
                                         "retry":true asks Gmail's servers again when they did not answer)
{"cmd":"mail","q":"amazon order"}     -> only the Mail rows: the emails, then the open-in-Gmail row
{"cmd":"act","n":1,"action":"open"}   -> {"ok":true,"result":"opened Maps (com.google.android.apps.maps)"}
{"cmd":"apps"}                        -> {"ok":true,"apps":[{"name":"Amazon","package":"in.amazon…","shortcuts":["Search","Orders","Cart"]},…]}
                                         (every app with an icon, no limit, with its entry points; nothing on screen)
{"cmd":"open","app":"Instacart"}      -> search + open in one call (a name or package ID)
{"cmd":"open","app":"in.amazon…","shortcut":"Orders"}
                                      -> opens the app at that entry point, by its label; an unknown label answers
                                         with the app's entry points
{"cmd":"context"}                     -> time, locale, battery, wifi, airplane, home app, app count
```

`act` numbers refer to the last `search` of any connection (one list for the process), so a caller that may run
beside another (a look-up while a step works) names what it opens with `open` instead.

**Entry points** (0.5.0) are the shortcuts that are fixed ways into an app (Search, Orders, New timer), not people,
chats or accounts: a shortcut declared in the app's manifest, or a dynamic one whose icon is one of the app's
resources and which is not long-lived (`Apps.isEntryPoint`). The two flags are not in the public API; they are read
from what `ShortcutInfo.toString()` prints (`flags=0x… [DynIc-rStr]`), and when that fails only the manifest's
shortcuts count. An app's dynamic ones exist only once it has run.

**`act` and `open` work only while the home screen is in front.** Android drops an activity start from a launcher
that is not showing, without an error (logcat: "Background activity launch blocked", measured 2026-11-22), so both
refuse then and say so. The backend's shortcut launch (`launch` with `shortcut`, backend/app/bridge/ops.py) brings
the home screen to the front first and takes what is in front afterwards as the outcome.

## Build and install

```
python android/home/build.py        # -> android/home/build/relay-home.apk
python android/build.py home        # the same, copied into desktop/assets/android to ship
```

The desktop app installs it and makes it the home screen on every start (`adbui.home_setup`). Plain Java Views,
no Gradle, same toolchain as the bridge (android/toolchain.py). View ids are named (`search`, `results`,
`status`) so the bridge dump of this screen is as clean as the socket.

## Next

Phase 1 wires `home_search` into the playground and the backend. Phase 2: notifications with inline
reply, the intent log from the bridge feeding learned deep links (with the extras that worked), on-device
files, `open_activity` through the bridge for internal screens. Phase 4: web results in a hidden WebView.
Drive and other Google data stay apps the agent opens: no OAuth of our own.
