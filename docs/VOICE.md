# Voice mode

The phone app opens talking: the user speaks, penrose answers in a few words while the work starts, and when a task
has something to say the phone rings like a call. The full interface (the task list, the conversation, the live view)
is one tap away and shows the same conversations; the window reaches voice mode from the microphone button in its
title bar.

## How a turn goes

1. **Listening.** The browser streams the microphone (24 kHz PCM) straight to an OpenAI transcription session, with
   a short-lived client secret the API mints (`POST /api/voice/listen`, `backend/app/api/voice.py`). The audio never
   crosses the companion tunnel; only the words come back to penrose. OpenAI's voice detection says when the user
   starts and stops speaking (`frontend/src/voice/speech.ts`, `Listener`).
2. **Placing.** The words go to `POST /api/voice/utterance`. The front desk (`backend/app/runtime/frontdesk.py`,
   prompt `backend/agent_prompts/frontdesk.md`) decides which conversation they belong to: the one the user is in
   the middle of (an answer, a correction, more of the same errand), another recent or related one, or a new one.
   Plain cases are decided by rule without a model: a spoken code for a step that waits for one goes to that step
   privately, as the keypad's private entry does. The rest is a fast-tier call on the user's picked agent provider,
   about a second. The task then runs as any other, with `voice` set in the run so the gate and the orchestrator also
   write `spoken`: one to three plain sentences for the ear beside the written reply (`backend/agent_prompts/shared/spoken.md`).
3. **Acknowledging.** The front desk's one sentence ("Adding that to your New Zealand trip.") is spoken at once
   through `POST /api/voice/speak` (OpenAI text-to-speech, MP3, streamed; markdown is taken out of a reply that has
   no spoken form). A long text is said in parts of a few sentences, each fetched while the one before plays, so
   the first words come sooner and each clip stays within one companion tunnel frame (the API cuts a single call at
   1500 characters as a backstop).
4. **Ringing.** The phone follows the chat list (`/api/updates`). A task that was working and is now completed,
   waiting for the user or failed rings (two tones and a short vibration, once, `Bell`) with a call card that stays
   up until it is answered; picking up says the reply's spoken form. Speaking to a ringing phone picks it up. The phone app keeps each task's status as last seen, so a
   task that finished while the app was closed or in the background rings when the app opens.

   **In the background.** Android freezes a web app that is not in front, so the ring above only sounds while penrose
   is open. Once voice is on, the phone app asks once whether it may ring in the background. On yes, it subscribes
   to Web Push with the coordinator's key and sends the subscription to the computer over the tunnel
   (`frontend/src/companion/push.ts`); the computer keeps it with the phone (`backend/app/companion/identity.py`).
   The computer's tunnel follows the chat list the same way the phone does; when a task finishes it asks the
   coordinator, in plain text, to push to each phone that asked (`{"t":"push","phone_id","subscription"}`), and the
   coordinator signs the request with the mailbox's VAPID key (made once in the Durable Object, `coordinator/src/push.ts`)
   and sends the push service a body that says only `{"t":"ring"}`. The service worker (`frontend/src/companion/sw.ts`)
   shows "penrose has something for you" with a vibration, unless the app is in front, where the ring above already
   sounds; tapping it opens penrose on the voice screen, which fetches the chat list and rings once with the call card.
   The notification's Reply button takes typed text where the browser allows it (sent to that chat by the app) and
   otherwise opens the chat with the keyboard up. Nothing about the task
   crosses: the coordinator and the push service learn only that there was something. A subscription the push
   service reports gone is forgotten. On an iPhone, pushes reach only an app added to the home screen.

**The controls are a call's.** The screen opens listening (no start button): under the presence sit Mute, with
Speaker under it, then the models the next task runs on (the same picker as the composer, `components/ModelsMenu.tsx`)
and the keypad. Browsers only play sound after a tap, so the first tap anywhere unlocks the speaker; until then an
acknowledgment could be silent.

**Speaker or earpiece.** On Android, Chrome runs the microphone in voice-call mode whenever the browser's echo
cancellation is on, and then plays everything through the earpiece. So the default is "speaker": the cancellation is
off and penrose's voice comes out of the loudspeaker. The microphone stays open while penrose speaks, so the user can
speak over it (a tap on the circle also interrupts). penrose's own voice coming back through the microphone is taken
out by the page (`frontend/src/voice/echo.ts`): WebRTC's AEC3, the canceller inside Chrome, compiled to WebAssembly
(`@ennuicastr/webrtcaec3.js`) and run in a worker. Every player (the reply, the desk, the bell) plays through
`playback`, which also feeds a copy into the microphone's audio graph as the reference; AEC3 finds the delay itself.
Offline (synthetic speech, 80–400 ms delay, a clipping loudspeaker) it removed 47–56 dB of echo once settled, a few
seconds in; on a real phone it is untested (2026-10-31). The Speaker key off means "earpiece or headphones", where
the browser's cancellation stays on and the page's is not used. The choice is kept on the phone.

Speaking over penrose pauses it the instant speech is detected. If the words turn out to be the phone's own voice
coming back through the microphone, or a cough, it goes on; otherwise it stops and the words are placed as the next
request, which steers a working task or starts the next one.

## The reception desk (the default)

Voice talks to the reception desk whenever an OpenAI key is set (`settings.desk_on`); `VOICE_DESK=0` switches it
off and brings back the front desk above, which is deprecated and kept only as that fallback. One Realtime model
in place of steps 1 to 3: it hears the user, routes their words to the sessions with tools,
and speaks (`backend/app/runtime/desk.py`, prompt `backend/agent_prompts/desk.md`, `frontend/src/voice/desk.ts`
and `useDesk.ts`; the screen is the same). Its only job is routing: `start_errand`, `pass_on` (an answer, a correction, a
follow-up, an instruction; a working session is steered, a stopped or finished one goes on), `stop_errand`, `resume_errand`, `rewind_errand` (to a
rewind point in the state, with new words), `read_reply` and `find_errand`. A tool returns at once; what the session
does afterwards shows in the state.

**Talking is a call** (2026-11-24, `useDesk.ts` `line`). The screen opens with a Call key and no line. Pressing it
rings (three dots in the key and after "Calling penrose", and a soft ringing tone: `speech.ts` `Ringback`) until the desk answers: the line is up and the microphone
open. Only then is anything the user says sent (what is said while it rings is dropped, `desk.ts` `up`), and the
keys become mute and end call. On picking up a call the user placed with the Call key, the desk greets in two or three words (`desk.ts` `greet`, a reply like any other, made from the state; dropped if the user is already speaking); a call placed by opening a card says that card's news instead, and one that rings again by itself after the chat says nothing. Not answered in 20 seconds (penrose is off, or cannot be reached), the call ends as
"penrose is currently unavailable" with a "Leave a text" button that opens the chat. A line that drops in the middle
of a call reconnects by itself ("Reconnecting to penrose…") with the same 20 seconds. Opening a card or a
notification places the call; leaving the screen for the chat in the middle of a call and coming back rings it
again by itself; a call the user ended stays ended. Before this the screen opened with a mute key while the line
was still coming up, and the user unmuted and spoke to nobody.

**The desk acts on the state, not on a history** (2026-11-24). It keeps no account of the errands. Each time it is
about to speak, the page asks for penrose as it is at that moment (`POST /api/voice/desk/state`,
`backend/app/runtime/deskstate.py`) and puts it last in the conversation in place of the one before: the errands
newest first, each numbered and led by its id (`s:…`), one labelled field per line: `status` (working, waiting for
the user, finished, failed, stopped, or stalled when a working one has saved nothing for four minutes), `doing` and
`on the phone now` (the running step's own "now" line and its last log line about the screen), `asks`, `result`,
`not told yet`, `notes` (the orchestrator's last notes), `said` (the last lines of the chat) and the rewind points.
Under the clock one line says what the desk last acted on, or which card the user opened: a hint, not a claim about
what they mean next. A deleted errand is simply not there. Before this the desk replayed its log (the board once,
then old tool results and events): on 2026-11-24 it spent four replies trying to pause an order whose chat had been
deleted, because a logged "started, status: starting" still said it was running.

Whatever was written at some moment says how long ago, rounded the way people say it (`deskstate.ago`: "a few
seconds ago", "3 min ago", "1 h 20 min ago", "4 h ago"): the status of an errand that ended, its result or
question, each line of `said`. The first voice test of the state (2026-11-24) read "arriving in about 40 minutes"
out of a result four hours old, because the block gave only the clock time; the instructions now say that such
words count from when they were written. The same test showed replies starting over: the user stopped a reply
three words in, and the next one began the same sentence again. The page now says so under the state, once
(`desk.ts` `stopped`: "They stopped your last reply after hearing only …"), and the log keeps how a reply was
stopped (`how`: a tap, or their voice), to tell a false cut from a real one. And a reply that is stopped stays stopped: the sound still on its way when the cancel was sent used to be played, so the desk went on talking, a few words further on, over the user who had just stopped it.

The second test (the same evening) showed the desk hearing itself: its greeting came back through the microphone, stopped the greeting and was transcribed as the user's words. The ring had been played in an AudioContext of its own, which reaches the echo canceller late and through a MediaStream; it now plays in the context the voice plays in (`desk.ts` `audio`). A turn that began over the desk's voice and transcribes as the desk's own words is dropped (`isEcho`). To shorten the wait for a reply: the state is read while the user is still speaking (when they start and at each pause, `readAhead`; used if read within three seconds of the turn's end and nothing changed it since), and after a tool called without a word the reply plays as it comes instead of being held to its end. Each reply to the user logs where its time went (`timing` on its `desk_log` entry: `total` from the end of the user's turn to the first sound, `state`, `model`, `ahead`, `round`).


Beside the state the conversation holds only the last lines said (eight at most, none older than five minutes;
`items()` on connect gives the last six of the last five minutes): older lines, the tool calls and their results
are taken out by the page, since the state says what they led to. Everything is still logged (`desk_log`: what the
user said, what the desk said, each tool call and result, each event): the tools read the user's own words there,
the state reads what the desk last acted on there, and an untold event waits there. The log is a working memory,
not a record: every write prunes it to the newest 200 entries and nothing older than a week (an untold event stays
until it is told), and deleting a chat removes its events. The chats themselves are the record.

**One reply, made when it is said.** The page asks for a reply when something calls for one: the user finished
speaking or typed, they opened a card, the errand being talked about has news, or a tool finished with something
left to say; the state's last line (`Why you speak now`) says which. Nothing is queued to be said later.
The sessions' news is still event-driven (`watch` follows the runner's event rows and logs `progress`, `asks`,
`finished` or `failed`; the `desk` value on `/api/updates` moves and the phone fetches the new events), but to the
page an event is a signal and a card, never words to play. About the errand being talked about, a question, result
or failure asks for a reply, made from the state: it cuts in on the desk's own acknowledgement at its next pause,
never on the telling of other news, and waits for the desk to finish what it is saying otherwise; where a working
errand is asks for one only after eight seconds with nobody speaking. About any other errand it is a card, and the
desk says nothing. An event is marked told once the reply that had it was heard to its end; a reply the user cuts
off has told nothing, and the event is a card again.

A reply nobody has heard a sound of is dropped when the user starts to speak (cancelled, its item deleted), and
asked for again from the state once they have spoken. In a chain of tool calls only the first acknowledgement and
the last word are heard: the sound of a reply that follows a tool is held until the reply ends, and if it only
calls another tool its words are never played, logged or kept. After three calls the desk gets one last word with
tools switched off. A reply with nothing in it (to a fragment, to "hold on") sends the screen back to listening.
The page's reply logic can be checked without a microphone or OpenAI: `node scripts/desk-sim.mjs` in `frontend/`
drives the bundled `Desk` class with a fake socket and a fake API.

**Turns end on the words, not a timer.** The line opens with the API's semantic turn detection (`semantic_vad`,
`eagerness` low, set by the page in `desk.ts` `TURNS` and in `runtime/desk.py` `session_config`): a classifier
judges from what the user has said whether they are done, so a pause in the middle of a thought keeps the turn
open and a long request arrives whole, where the 700 ms silence timer before it (2026-11-09) split it and the desk
acted on the first half. If the model refuses it (an older one; the session.update carries `event_id` "turns" so
the refusal is known), the page falls back to silence, 1.2 s of it. Replies stay the page's to ask for
(`create_response` false). Two things go with it. Nods: when the user pauses about a second in anything they
have said (since 2026-11-24 also in a short request: that pause is when the turn detector is still making up its
mind and nothing else would be heard), and again 0.6 s after their turn has ended if the reply has not begun to
sound and that pause had none, the page plays a short "mm-hm" in the desk's voice (`GET /api/voice/desk/nods`: three clips
made once by the speech model as raw PCM and kept under the data dir's `voice/`), through the same output so the
echo canceller hears it, outside the reply's timeline so it is not a reply and cannot be barged into; never a model
response, which would cost a round trip, take the one response slot and be a turn of its own; at most one every
six seconds, and never on a short request, which gets its answer. And the transcript wait: a long turn transcribes
after the desk has answered it, so before a tool that carries the user's words runs (`start_errand`, `pass_on`,
`rewind_errand`), the page waits up to 2.5 s for the turn's transcript and its log write, so `their_words` finds
the user's words and not the desk's retelling.

A reply that is cut off, by the user speaking over it or by news, is remembered as the user heard it, both
ways: the API truncates the desk's own item at the milliseconds played, and the page logs the words heard by then
(the caption at that moment, `captions.ts`; `heard` on `/api/voice/desk/log`, or `/api/voice/desk/cut` when the
words were logged before the cut) with the reply's entry in `desk_log`. The desk's memory rehydrates the heard
words, not the written ones. The user's words that follow a reply they cut go to the session with what they had
heard (`their_words` in `runtime/desk.py` appends `[interrupting your reply after hearing: "…"]`; the agents are
told what it means in `agent_prompts/shared/spoken.md`), so "add that to the cart" said over "the best value is X,
and other options are…" means X, which the written reply alone could not tell: it lists everything.
Mute gives the microphone back, as on a call (the phone's microphone indicator goes out; unmuting opens it again), and costs nothing: the line needs no audio to stay open, and audio is billed as it is sent. Muting in the middle of a sentence sends a second of silence so voice detection ends the turn. With the line
closed the phone rings with the call card as before, and picking up opens the line. The push notification is
the same nudge as before, with the spoken form as its text: a push cannot play audio or hold a socket, so the
desk speaks once the app is opened.

What was checked against `gpt-realtime-2.1-mini` (2026-10-30, before the state-driven desk): a tool call left
unanswered while the user goes on raises no error and late results are accepted. What needs care: a second
`response.create` while one runs is refused (the page waits for `response.done`). The state-driven desk
(2026-11-24) was checked by the backend tests and a simulated socket; not yet by voice against the model.

## What the surface adds, and what guards it

The three routes sit behind the same guard as every `/api` route (`api/auth.py`): the API listens on 127.0.0.1
only, a window needs its session when a login is set, and the phone reaches them only through the companion tunnel
with the launch's token and the tunnel's allow-list. `listen` hands the browser a client secret that opens
transcription sessions for an hour and nothing else; the phone's browser connects to OpenAI directly with it, so
OpenAI sees the phone's address. `speak` and `utterance` take at most 4000 characters, the models and provider are
checked against the catalog, and the front desk can only pick a conversation from the ones it was shown (anything
else starts a new one). A spoken code goes to a step privately only while that step waits for one.

## Models and cost

Transcription and speech are OpenAI's whichever provider the tasks run on, so voice needs the OpenAI key. Defaults
(`backend/app/config.py`): `gpt-4o-transcribe` (about $0.006 a minute of open microphone, only while the app is in
front), `gpt-4o-mini-tts` with the `marin` voice and a tone instruction. `gpt-4o-mini-tts` is announced to shut down
in January 2027; the call sits in one place (`speak`) to move to `gpt-realtime-mini` then.

## Where things are

- `frontend/src/voice/`: `speech.ts` (the ears, the mouth, the bell), `useVoice.ts` (the phases), `Voice.tsx`,
  `voice.css`. `App.tsx` mounts the screen: open at launch in the phone app, from the title bar in the window.
- `backend/app/api/voice.py`, `backend/app/runtime/frontdesk.py`, `backend/app/tools/frontdesk.py`,
  `backend/agent_prompts/frontdesk.md`, `backend/agent_prompts/shared/spoken.md`; `spoken` on messages
  (`store.py`), `channel` on runs (`delivery.py`), `voice` in the agents' context (`runtime/runner.py`).
- Background ringing: `frontend/src/companion/push.ts`, `frontend/src/companion/sw.ts`, `coordinator/src/push.ts`,
  `Tunnel.watch` and `Peer.handle` ("push") in `backend/app/companion/tunnel.py`.
- The reception desk: `backend/app/runtime/desk.py`, `backend/app/api/desk.py`, `backend/agent_prompts/desk.md`,
  `frontend/src/voice/desk.ts`, `frontend/src/voice/useDesk.ts`; `desk_log` in `store.py`; `voice_desk` and
  `voice_desk_model` in `config.py`.
- Tests: `backend/tests/test_voice.py`, `backend/tests/test_desk.py`, `backend/tests/test_companion.py` (ringing),
  `coordinator/test/push.test.ts` (`npm test` in `coordinator/`).

While voice is on the app holds a screen wake lock, so the phone does not dim and lock mid-sentence (a locked
phone puts the app in the background, where the microphone closes). Stop, or leaving the app, lets the screen
sleep again.

## Not yet

The live view inside the voice screen, a spoken progress note while a long task runs, remembering that voice was
started so it starts without a tap (browsers need one tap for the microphone and the speaker anyway), and a
notification that rings for longer than the phone's own notification sound (a push can only ask the phone to show a
notification; how it sounds is the phone's).
