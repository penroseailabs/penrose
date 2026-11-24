from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Set by the desktop app through the environment when it starts the services (desktop/src/main.js). No
    file is read: to change a tuning value in development, set its variable before `npm run dev`
    (desktop/.env.local)."""

    model_config = SettingsConfigDict(env_file=None, extra="ignore")
    data_dir: Path = Path(__file__).parents[2] / "data"
    user_id: str = Field(default="local", pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    # The desktop app's per-launch sign-in (random each launch); empty: the API is open to local callers.
    login_username: str = ""
    login_password: str = ""
    # Where the window loads the app from: the only origin allowed to write.
    public_origin: str = "http://127.0.0.1:8000"
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    fast_model: str = "deepseek-flash"
    heavy_model: str = "deepseek-v4-pro"
    subagent_model: str = "deepseek-flash"
    subagent_reasoning_effort: str = "low"
    # Chinese public holidays (Beijing dates, comma-separated YYYY-MM-DD): DeepSeek bills them off-peak all day.
    deepseek_holidays: str = ""
    # Phone agents (subagent, verifier) read the bridge's numbered screen text and can also be
    # sent small screenshots of the same moments. Only the subagent model is set up for images
    # (deepseek-v4-pro ignores them); with this off no agent is sent pictures.
    subagent_vision: bool = True
    # Wide enough to read on-screen text, small enough to send every turn.
    screenshot_width: int = Field(default=1080, ge=200, le=1080)
    # GPT, chosen per run in the composer: the orchestrator on Sol, everything else on the cheap Luna.
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_heavy_model: str = "gpt-6.1-sol"
    openai_fast_model: str = "gpt-6-luna"
    openai_phone_model: str = "gpt-6-luna"
    # Voice mode (api/voice.py): the phone or window hears the user through OpenAI's transcription and speaks
    # replies through its text-to-speech, whichever provider the tasks run on.
    voice_transcribe_model: str = "gpt-4o-transcribe"
    voice_tts_model: str = "gpt-4o-mini-tts"
    # The reception desk (runtime/desk.py): one Realtime model hears the user, routes their words to the sessions
    # and speaks. On by default; it takes effect once an OpenAI key is set (`desk_on`). Off, voice mode
    # transcribes, asks the front desk (runtime/frontdesk.py) and speaks through text-to-speech instead.
    voice_desk: bool = True
    voice_desk_model: str = "gpt-realtime-2.1-mini"
    voice_tts_voice: str = "marin"
    voice_tts_instructions: str = (
        "Speak as a warm, attentive assistant: natural, unhurried and clear, friendly without being chatty. "
        "Say numbers, times and prices the way people say them."
    )
    # Calls (runtime/call.py): a call step's phone subagent dials, then one Realtime model talks with the other
    # person through the phone's own audio (gateway/call_audio.py) and reports what they said. It needs an OpenAI
    # key, whichever provider the task runs on. The limits are enforced by code, not left to the model.
    call_model: str = "gpt-realtime-2.1-mini"
    call_voice: str = "marin"
    call_default_minutes: int = Field(default=5, ge=1, le=15)
    call_max_minutes: int = Field(default=15, ge=1, le=30)
    call_ring_seconds: int = Field(default=45, ge=10, le=120)  # no words from them by then: no answer
    call_silence_seconds: int = Field(default=20, ge=5, le=120)  # nobody spoke for this long: wrap up
    # What the phone hears goes through OpenAI's noise filter ("near_field" | "far_field") and its voice detector
    # (how sure it must be that someone talks). Sound over penrose's voice cuts it off only once call_barge_in_ms of
    # it is heard (voiced sound, counted from the phone's audio): a "yeah", "mm-hm" or "right" (about 0.2-0.5 s), a
    # click or a burst of line noise does not make it start its sentence over; two or three words do.
    call_noise_reduction: str = "far_field"
    call_vad_threshold: float = Field(default=0.6, ge=0.3, le=0.95)
    call_barge_in_ms: int = Field(default=700, ge=0, le=2000)
    # Claude: the orchestrator on the heavy model, everything else on the fast, cheap one.
    anthropic_api_key: str = ""
    anthropic_heavy_model: str = "claude-opus-5-5"
    # Memory and reflection read a lot and write little: Sonnet, whichever model the user picked for the agent.
    anthropic_support_model: str = "claude-sonnet-5-5"
    anthropic_fast_model: str = "claude-haiku-5-5"
    anthropic_phone_model: str = "claude-haiku-5-5"
    emulator_gateway_url: str = "http://127.0.0.1:8002"
    # A shared secret the gateway requires on every request (the desktop app sets a fresh one per launch,
    # so other programs on the machine cannot drive the phone). Empty: no check (tests).
    gateway_token: str = ""
    # The API, which the worker rings when a run has new events (app/bell.py), with gateway_token as its key.
    # Empty: no ring; the window's event streams read new events on their slow check instead.
    api_url: str = ""
    # Desktop app: the API also serves the built web UI from this folder.
    frontend_dir: Path | None = None
    desktop: bool = False
    # The desktop app's loopback control server (desktop/src/main.js) and the token it requires: the API hands it
    # a key saved from a linked phone, because only the app can open the OS keychain the keys are kept in.
    desktop_control_url: str = ""
    desktop_control_token: str = ""
    # The gateway's own token for that server, good for one thing: asking the app to restart a phone that has
    # stopped answering (gateway/watch.py). The app owns the emulator: it stops the frozen one and starts it again.
    desktop_phone_token: str = ""
    # The gateway asks each phone whether it still answers this often (gateway/watch.py): a frozen emulator says
    # nothing by itself. 0: it is never asked.
    phone_alive_seconds: float = Field(default=60, ge=0)
    # Decodes the phone's H.264 screen recording into frames for the live view.
    ffmpeg_path: str = "ffmpeg"
    # The phone's adb serial (the desktop app runs its emulator on port 5570).
    adb_targets: str = "emulator-5570"
    adb_path: str = "adb"
    # Shell commands and port forwards go straight to adb's server, with no adb program started per command
    # (bridge/adbwire.py). False: every command starts the adb program, as before 2026-11-12.
    adb_direct: bool = True
    # Downloads APKs for install_app; without it install_app reports failure and agents use Play Store.
    apkeep_path: str = "apkeep"
    install_timeout_seconds: float = Field(default=300, gt=0)
    # Permission groups every app gets as it is installed, so Android never asks for them (gateway/grants.py):
    # names from bridge/ops.py PERMISSIONS, comma-separated; "" turns it off.
    auto_grant: str = "notifications,location"
    stream_fps: int = Field(default=30, ge=1, le=60)
    # The phone's screen goes off after this long with nobody using it (no lease, no request), and it goes into
    # Doze this long after that (gateway/sleep.py). 0: it never sleeps.
    phone_sleep_seconds: float = Field(default=15, ge=0)
    phone_doze_seconds: float = Field(default=15, ge=0)
    # In Doze, the emulator hands its memory back to this computer (gateway/memory.py, Windows), and again this
    # often while the phone dozes on, as it creeps back. 0: it keeps its memory.
    phone_trim_seconds: float = Field(default=300, ge=0)
    # ...but only once it has dozed this long. Memory handed back is read from disk again at the wake when this
    # computer used it for something else meanwhile, and then the whole phone is slow for the first half minute
    # (measured 2026-11-20: the wake 4.7 s for 0.3 s, four Gmail searches 25 s for 6 s, frames of 16 s). A phone
    # that is used again within this time never pays that.
    phone_trim_after_seconds: float = Field(default=1800, ge=0)
    # The worker reads the phone's new notifications this often (app/notifications.py): they are kept in a log,
    # and passed to the chat they belong to. 0: notifications are not read.
    notification_seconds: float = Field(default=3, ge=0)
    # After this long in Doze, and again every so long, Android lets apps onto the network for about half a minute
    # (gateway/sleep.py): what Google holds back from a dozing phone arrives then. 0 leaves Android's own pace.
    phone_doze_window_seconds: int = Field(default=900, ge=0)
    # The desktop app starts the gateway with this set while it is updating the phone (provision device-ready):
    # no lease is granted until it says it is done (PUT /devices/{slot}/setup).
    phone_setup: bool = False
    # The live view's frame width. The panel shows the phone about 300 px wide, and about 460 px while the
    # user signs in to an app themselves; 720 reads well at both.
    stream_width: int = Field(default=720, ge=360, le=1080)
    # The companion phone app's origin: it serves the app and the coordinator the computer's tunnel connects to
    # (app/companion). Empty: no companion.
    companion_origin: str = ""
    # The phone app's live view of the phone: frames a second at most, as they cross mobile data and the coordinator.
    companion_stream_fps: int = Field(default=4, ge=1, le=15)
    # How this computer introduces itself to a phone (default: its hostname).
    companion_name: str = ""
    # A step's budget is time: a question step (read and report) gets question_step_minutes, an action step
    # (log in, build a cart, book) action_step_minutes. When it runs out the subagent hands over what it found,
    # and the orchestrator reports the partial answer or changes route. Turns are not a budget (a turn takes 3 s
    # on one model and 20 s on another); subagent_max_turns only stops a session that runs away, and the
    # subagent is not told it. Checkpoints keep a long session short to read, and the repeat and stall checks end
    # one that is stuck.
    subagent_max_turns: int = Field(default=400, ge=1, le=1000)
    question_step_minutes: float = Field(default=4, gt=0)
    action_step_minutes: float = Field(default=30, gt=0)
    # A subagent whose checkpoint finds nothing new since the one before it is not getting anywhere: its
    # session ends with a handover. 0: never.
    phone_stalled_checkpoints: int = Field(default=2, ge=0, le=10)
    # Phone time (every subagent and verifier session added up) one task may use before the orchestrator
    # must ask or finish with what there is. A message from the user starts the clock again.
    task_phone_minutes: float = Field(default=45, gt=0)
    verifier_max_turns: int = Field(default=20, ge=1, le=100)
    # Phone agents answer with native tool calls. Thinking is on at subagent_reasoning_effort (low);
    # turn it off for one short tool call per turn, which is faster and cheaper.
    phone_thinking: bool = True
    # The most tokens one phone-agent turn may write (a report with facts and lessons fits).
    phone_max_tokens: int = Field(default=1500, ge=200, le=8000)
    # Actions a phone agent reads back each turn, newest last, with what each did. Each turn is a fresh model
    # call, so this (with its notes, the screens so far and its last thinking) is all it knows of earlier turns.
    phone_recent_actions: int = Field(default=15, ge=1, le=50)
    # An agent that keeps a log (the subagent: one line a turn in its own words, stamped with where it was) reads
    # back only this many raw actions: the log carries the session, these are the exact record of the last few.
    phone_log_actions: int = Field(default=3, ge=1, le=50)
    # Every this many turns (and when it starts repeating itself) a phone agent sums up its session in a
    # checkpoint: what it found, what did not work, where it is, its plan. The summary replaces its log or notes,
    # progress and older actions, so a long session stays as short to read as a new one. The log already
    # compacts each turn as it goes, so this only keeps a very long session's log from growing. 0: never.
    phone_checkpoint_turns: int = Field(default=25, ge=0, le=100)
    # The most characters of a web page read_page shows (about a quarter as many tokens); the rest is cut.
    page_read_chars: int = Field(default=12000, ge=1000, le=50000)
    # After an action, the longest the look waits for the screen to stop changing. A tap or typed text waits only
    # until the app's bridge sees no layout for settle_quiet_ms (usually 150-250 ms in all); keys, swipes and
    # launches, which the app cannot see coming, wait all of it.
    look_delay_seconds: float = Field(default=0.5, ge=0)
    settle_quiet_ms: int = Field(default=150, ge=0, le=2000)
    # The longest a look after an action goes on waiting when the app is plainly not done: it is between two
    # screens, or a spinner stands where its content will be (bridge.screen.loading). 0: never.
    loading_wait_seconds: float = Field(default=8.0, ge=0, le=20)
    # Orchestrator turns one run may take before it must finish or ask the user. A turn is usually one step.
    max_orchestrator_turns: int = Field(default=60, ge=2, le=200)
    # Times one step may be started before the orchestrator has to change it, ask the user, or finish.
    max_step_attempts: int = Field(default=3, ge=1, le=5)
    # look_up calls (the home app's search and earlier conversations) the orchestrator may make per user message.
    max_lookups: int = Field(default=12, ge=0, le=40)
    # Memory: notes about one app that a subagent reads with its step, and the size the index of the memories
    # is held to (one line each, the limits Claude Code puts on its own index): the agents see no line past it,
    # and the memory agent is asked to merge and prune as it fills.
    memory_app_notes: int = Field(default=8, ge=0, le=30)
    # The characters of those notes a phone agent reads in full with its step, on every turn; the notes past it
    # are given as their index line alone.
    memory_step_chars: int = Field(default=4000, ge=0)
    # An open matter (a memory of what is pending) saved without a last day lapses after this many days.
    memory_open_days: int = Field(default=30, ge=1)
    memory_index_lines: int = Field(default=200, ge=10)
    memory_index_chars: int = Field(default=25000, ge=1000)
    # Conversation compaction (runtime/compaction.py): once the messages an agent reads of a chat are over this
    # many tokens (estimated), all but the newest conversation_keep_messages are folded into the chat's summary
    # after the run, while the agent is idle. 0: never.
    conversation_budget_tokens: int = Field(default=150000, ge=0)
    conversation_keep_messages: int = Field(default=10, ge=0, le=100)
    # Reflection (runtime/reflection.py): every this many finished tasks, the reflection agent reads the recent
    # tasks, the conversation summaries and every memory, and writes what they show together. 0: never.
    reflect_every_tasks: int = Field(default=10, ge=0, le=500)
    device_wait_seconds: int = Field(default=600, ge=1)
    provider_timeout: int = 180
    # look_up also reads what the user searched for and the pages they went to on their other devices, as Chrome
    # keeps it on the phone (gateway/history.py): only the lines that have a query's words, never the history itself.
    history_lookup: bool = True

    @property
    def gateway_headers(self) -> dict:
        return {"X-Gateway-Token": self.gateway_token} if self.gateway_token else {}

    @property
    def user_dir(self) -> Path:
        return self.data_dir / "users" / self.user_id

    @property
    def desk_on(self) -> bool:
        """Voice talks to the reception desk: switched on and an OpenAI key set (the desk is OpenAI's Realtime)."""
        return self.voice_desk and bool(self.openai_api_key)


settings = Settings()
