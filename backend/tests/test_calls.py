"""Voice calls: the gateway's audio helpers, the plan's call steps and the call runtime against fake sockets."""

import array
import asyncio
import json

import pytest

from app.errors import Rejected
from app.gateway import emulator_frames
from app.gateway.call_audio import Upsampler, frame
from app.plan import StepSpec, new_step
from app.runtime import call as call_runtime
from app.tools.orchestrator import RunStep

BRIEF = {"contact": "Grace", "purpose": "Ask where the spare keys are", "questions": ["Where are the keys?"],
         "may_share": "calling for Alex", "language": "en", "max_minutes": 5}


def test_upsampler_doubles_and_interpolates():
    up = Upsampler()
    out = array.array("h")
    out.frombytes(up(array.array("h", [100, 200]).tobytes()))
    assert list(out) == [50, 100, 150, 200]  # starts from silence, then midpoints between samples
    out2 = array.array("h")
    out2.frombytes(up(array.array("h", [400]).tobytes()))
    assert list(out2) == [300, 400]  # carries the last sample over chunk edges


def test_frame_and_audio_format():
    assert frame(b"F") == b"F\x00\x00\x00\x00"
    assert frame(b"A", b"xy") == b"A\x00\x00\x00\x02xy"
    fmt = emulator_frames.audio_format(24000)
    fields = dict(emulator_frames._fields(fmt))
    assert fields == {1: 24000, 3: emulator_frames.AUD_FMT_S16}
    packet = emulator_frames._varint(3 << 3 | 2) + emulator_frames._varint(4) + b"\x01\x02\x03\x04"
    assert emulator_frames.parse_audio(packet) == b"\x01\x02\x03\x04"


def test_call_step_needs_its_brief():
    spec = StepSpec(id="c1", goal="In Gmail's Meet, call Grace", kind="call", done_when="The call is ringing")
    plan = [new_step(spec)]
    with pytest.raises(Rejected, match="call step without"):
        RunStep(step="c1").check(plan, {})
    with_brief = StepSpec(id="c1", goal="In Gmail's Meet, call Grace", kind="call", done_when="The call is ringing", call=BRIEF)
    RunStep(step="c1").check([new_step(with_brief)], {})
    assert new_step(with_brief)["call"]["max_minutes"] == 5


def test_instructions_fill_the_brief():
    text = call_runtime.instructions(BRIEF, "hi")
    assert "Grace" in text and "spare keys" in text and "Where are the keys?" in text and "Hindi" in text
    assert "[contact]" not in text and "[purpose]" not in text and "[minutes]" not in text


class FakeStore:
    def __init__(self):
        self.events = []

    def event(self, run_id, kind, payload):
        self.events.append((kind, payload))

    def user_locale(self):
        return {"language": "en"}


class FakeRunner:
    def __init__(self):
        self.store = FakeStore()
        self.stopped = False

    def cancelled(self, state):
        if self.stopped:
            raise asyncio.CancelledError()


def step_with_brief():
    step = new_step(StepSpec(id="c1", goal="call", kind="call", done_when="ringing", call=BRIEF))
    return step


def test_report_answered_and_not():
    runner, step = FakeRunner(), step_with_brief()
    c = call_runtime.Call(runner, {"id": "r1", "slot": "device-1"}, step)
    c.answered_at = c.started
    c.outcome = {"outcome": "answered", "summary": "They are under the doormat."}
    report = c.report()
    assert report.status == "completed" and "doormat" in report.summary and "information only" in report.summary
    assert report.facts == {"call_c1": "They are under the doormat."}
    c.outcome = {"outcome": "no_answer", "summary": ""}
    assert c.report().status == "failed" and "did not answer" in c.report().summary


class FakeSocket:
    """An async-iterable socket: yields `incoming` (with small pauses), records what is sent."""

    def __init__(self, incoming, pause=0.05):
        self.incoming, self.pause, self.sent = list(incoming), pause, []

    async def send(self, data):
        self.sent.append(data)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if "hangup" in self.sent or "release" in self.sent:
            raise StopAsyncIteration  # the gateway closes the line once the call is ended
        if not self.incoming:
            await asyncio.sleep(3600)
        await asyncio.sleep(self.pause)
        return self.incoming.pop(0)


def test_conversation_waits_for_words_greets_and_ends(monkeypatch):
    runner, step = FakeRunner(), step_with_brief()
    c = call_runtime.Call(runner, {"id": "r1", "slot": "device-1"}, step)
    voice = b"\x01\x00" * 480  # 20 ms at 24 kHz
    events = [
        {"type": "conversation.item.input_audio_transcription.completed", "transcript": "..."},  # a tone: not words
        {"type": "conversation.item.input_audio_transcription.completed", "transcript": "Hello?"},
        {"type": "response.output_audio.delta", "item_id": "i1", "delta": __import__("base64").b64encode(voice).decode()},
        {"type": "response.output_audio_transcript.done", "transcript": "Hi Grace, this is penrose."},
        {"type": "conversation.item.input_audio_transcription.completed", "transcript": "Under the doormat."},
        {"type": "response.function_call_arguments.done", "name": "end_call",
         "arguments": json.dumps({"outcome": "answered", "summary": "Keys are under the doormat."})},
    ]
    model = FakeSocket([json.dumps(e) for e in events])
    phone = FakeSocket([voice])
    asyncio.run(asyncio.wait_for(c.converse(phone, model), 10))
    sent = [json.loads(m) for m in model.sent if isinstance(m, str)]
    kinds = [m["type"] for m in sent]
    assert kinds[0] == "session.update"
    audio_in = sent[0]["session"]["audio"]["input"]
    assert audio_in["turn_detection"]["create_response"] is False and audio_in["turn_detection"]["interrupt_response"] is False
    assert audio_in["noise_reduction"]["type"] in ("near_field", "far_field")
    assert kinds.count("response.create") == 1  # greeted once, when they said words (not on the tone)
    assert voice in phone.sent and phone.sent[-1] == "hangup"
    report = c.report()
    assert report.status == "completed" and "doormat" in report.summary
    assert [line["who"] for line in step["call_transcript"]] == ["them", "them", "penrose", "them"]


def test_no_answer_ends_the_call(monkeypatch):
    monkeypatch.setattr(call_runtime.settings, "call_ring_seconds", 1)
    runner, step = FakeRunner(), step_with_brief()
    c = call_runtime.Call(runner, {"id": "r1", "slot": "device-1"}, step)
    c.dialled()  # the ring clock runs from the dial
    phone, model = FakeSocket([]), FakeSocket([])
    asyncio.run(asyncio.wait_for(c.converse(phone, model), 10))
    assert phone.sent[-1] == "hangup"
    assert c.report().status == "failed" and "did not answer" in c.report().summary


@pytest.mark.parametrize("dialled, last", [(True, "hangup"), (False, "release")])
def test_stop_hangs_up_a_placed_call_and_leaves_the_app_alone_otherwise(dialled, last):
    runner, step = FakeRunner(), step_with_brief()
    runner.stopped = True
    c = call_runtime.Call(runner, {"id": "r1", "slot": "device-1"}, step)
    if dialled:
        c.dialled()
    phone, model = FakeSocket([]), FakeSocket([])
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(asyncio.wait_for(c.converse(phone, model), 10))
    assert phone.sent[-1] == last


class TimedSocket(FakeSocket):
    """Like FakeSocket, with its own pause before each message: [(seconds, message)]."""

    def __init__(self, incoming):
        super().__init__([m for _, m in incoming])
        self.pauses = [p for p, _ in incoming]

    async def __anext__(self):
        if not self.incoming:
            await asyncio.sleep(3600)
        await asyncio.sleep(self.pauses.pop(0))
        return self.incoming.pop(0)


class PhoneSocket(FakeSocket):
    """The phone's audio, timed like TimedSocket: [(seconds, message)]; the line closes once the call is ended."""

    def __init__(self, incoming):
        super().__init__([m for _, m in incoming])
        self.pauses = [p for p, _ in incoming]

    async def __anext__(self):
        if "hangup" in self.sent or "release" in self.sent:
            raise StopAsyncIteration
        if not self.incoming:
            await asyncio.sleep(3600)
        await asyncio.sleep(self.pauses.pop(0))
        return self.incoming.pop(0)


def talk_over(hold, monkeypatch):
    """Penrose is answered and speaking 2 s of voice; they talk over it for `hold` seconds, then a turn. As with the
    real detector, speech_stopped comes only after 0.6 s of silence (its silence_duration_ms)."""
    monkeypatch.setattr(call_runtime.settings, "call_barge_in_ms", 700)
    runner, step = FakeRunner(), step_with_brief()
    c = call_runtime.Call(runner, {"id": "r1", "slot": "device-1"}, step)
    voice = __import__("base64").b64encode(b"\x01\x00" * 48000).decode()  # 2 s at 24 kHz
    ev = lambda **e: json.dumps(e)
    model = TimedSocket([
        (0.01, ev(type="conversation.item.input_audio_transcription.completed", item_id="t0", transcript="Hello?")),
        (0.01, ev(type="response.created")),
        (0.01, ev(type="response.output_audio.delta", item_id="p1", delta=voice)),
        (0.2, ev(type="input_audio_buffer.speech_started")),
        (hold + 0.6, ev(type="input_audio_buffer.speech_stopped")),
        (0.01, ev(type="input_audio_buffer.committed", item_id="t1")),
        (0.01, ev(type="conversation.item.input_audio_transcription.completed", item_id="t1", transcript="Yakınlıkla dinleyin.")),
        (0.01, ev(type="response.done")),
        (0.01, ev(type="response.function_call_arguments.done", name="end_call",
                  arguments=json.dumps({"outcome": "answered", "summary": "ok"}))),
    ])
    block = array.array("h", [3000, -3000] * 1200).tobytes()  # 0.1 s of loud sound at 24 kHz
    blocks = round(hold / 0.1)
    phone = PhoneSocket([(0.2, block)] + [(0.1, block)] * (blocks - 1))
    asyncio.run(asyncio.wait_for(c.converse(phone, model), 10))
    kinds = [json.loads(m)["type"] for m in model.sent if isinstance(m, str)]
    return c, phone, kinds


def test_a_yeah_over_penrose_is_let_go(monkeypatch):
    c, phone, kinds = talk_over(0.3, monkeypatch)  # a backchannel: shorter than call_barge_in_ms
    assert "flush" not in phone.sent and "conversation.item.truncate" not in kinds and "response.cancel" not in kinds
    assert kinds.count("response.create") == 1  # the greeting only: no reply to the noise
    assert [line["text"] for line in c.transcript if line["who"] == "them"] == ["Hello?"]


def test_talking_over_penrose_cuts_it_off_and_gets_a_reply(monkeypatch):
    c, phone, kinds = talk_over(1.2, monkeypatch)
    assert "flush" in phone.sent and "conversation.item.truncate" in kinds and "response.cancel" in kinds
    assert kinds.count("response.create") == 2  # the greeting, then a reply once the cut-off response was done
    assert kinds.index("response.create", kinds.index("response.cancel")) > kinds.index("response.cancel")


def test_recording_keeps_both_sides(monkeypatch, tmp_path):
    import wave
    monkeypatch.setattr(call_runtime.settings, "data_dir", tmp_path)
    assert call_runtime.Recording.open("r1", "c1") is None  # off unless the folder exists
    (call_runtime.settings.user_dir / "call-recordings").mkdir(parents=True)
    rec = call_runtime.Recording.open("r1", "c1")
    rec.played(array.array("h", [7, 8]).tobytes())
    rec.heard(array.array("h", [1, 2, 3]).tobytes())
    rec.note("cut off after 1.0 s")
    rec.close()
    folder = next((call_runtime.settings.user_dir / "call-recordings").iterdir())
    with wave.open(str(folder / "call.wav")) as w:
        assert w.getnchannels() == 2 and w.getframerate() == 24000
        frames = array.array("h", w.readframes(10))
    assert list(frames) == [1, 7, 2, 8, 3, 0]  # left: what the phone heard, right: penrose, paced by the phone
    assert "cut off" in (folder / "events.log").read_text()


def test_it_greets_once_when_they_pick_up_naming_who_it_calls_for():
    runner, step = FakeRunner(), new_step(StepSpec(id="c1", goal="call", kind="call", done_when="ringing",
                                                    call={**BRIEF, "caller": "Alex"}))
    c = call_runtime.Call(runner, {"id": "r1", "slot": "device-1"}, step)
    model = FakeSocket([])
    asyncio.run(c.answer(model))
    sent = [json.loads(m) for m in model.sent]
    assert [m["type"] for m in sent] == ["conversation.item.create", "response.create"]
    assert '"Hi Grace, this is penrose, calling for Alex."' in sent[0]["item"]["content"][0]["text"]
    # Not in the instructions, which every reply reads: it said the greeting again on each one.
    assert "this is penrose, calling for" not in call_runtime.instructions(BRIEF, "en")


def test_a_call_to_the_user_names_nobody_it_calls_for():
    runner, step = FakeRunner(), step_with_brief()  # no caller in the brief
    c = call_runtime.Call(runner, {"id": "r1", "slot": "device-1"}, step)
    model = FakeSocket([])
    asyncio.run(c.answer(model))
    text = json.loads(model.sent[0])["item"]["content"][0]["text"]
    assert '"Hi Grace, this is penrose."' in text and "calling for" not in text


def test_picked_up_and_silent_is_greeted_and_a_dead_phone_is_reported(monkeypatch):
    """They picked up (the gateway's "connected") and nothing of theirs is heard: penrose greets anyway. With no
    sound from the phone at all, the report says the phone's sound failed, not that they did not answer."""
    monkeypatch.setattr(call_runtime, "PICKED_UP_SECONDS", 0.5)
    runner, step = FakeRunner(), step_with_brief()
    c = call_runtime.Call(runner, {"id": "r1", "slot": "device-1"}, step)
    c.dialled()
    ev = lambda **e: json.dumps(e)
    model = TimedSocket([
        (2.5, ev(type="response.output_audio_transcript.done", transcript="Hi Grace, this is penrose.")),
        (0.01, ev(type="response.function_call_arguments.done", name="end_call", response_id="r9",
                  arguments=json.dumps({"outcome": "other", "summary": "Nobody spoke."}))),
    ])
    c.voiced.add("r9")
    phone = FakeSocket(["connected"])
    asyncio.run(asyncio.wait_for(c.converse(phone, model), 15))
    kinds = [json.loads(m)["type"] for m in model.sent if isinstance(m, str)]
    assert kinds.count("response.create") == 1 and c.unprompted and c.answered.is_set()
    report = c.report()
    assert report.status == "failed" and "played no sound" in report.summary and "did not answer" not in report.summary


def test_the_kept_recording_mixes_both_sides_at_16_khz_from_the_pick_up(tmp_path):
    import wave
    kept = call_runtime.Kept(tmp_path / "chat" / "call.wav")
    ring = array.array("h", [900] * 24000 * 5).tobytes()  # five seconds of ringing: only the last three are kept
    kept.heard(ring)
    kept.begin()
    kept.played(array.array("h", [100] * 2400).tobytes())  # penrose, 0.1 s
    kept.heard(array.array("h", [10] * 2400).tobytes())  # them, the same 0.1 s
    seconds = kept.close()
    assert abs(seconds - 3.1) < 0.01
    with wave.open(str(tmp_path / "chat" / "call.wav")) as w:
        assert (w.getnchannels(), w.getframerate(), w.getsampwidth()) == (1, 16000, 2)
        frames = array.array("h", w.readframes(w.getnframes()))
    assert len(frames) == round(seconds * 16000)
    assert frames[1000] == 900 and frames[-1] == 110  # the ringing's tail, then both voices added


class ChatStore(FakeStore):
    """A store that takes the call's card: the file's row and the message."""

    def __init__(self, tmp_path):
        super().__init__()
        import sqlite3
        self.db = sqlite3.connect(tmp_path / "t.sqlite3", isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("CREATE TABLE attachments(id INTEGER PRIMARY KEY AUTOINCREMENT,chat_id TEXT,name TEXT,mime TEXT,"
                        "kind TEXT,size INTEGER,purpose TEXT,path TEXT,phone_path TEXT,w INTEGER DEFAULT 0,h INTEGER DEFAULT 0,"
                        "text TEXT,note TEXT,created REAL,done INTEGER DEFAULT 0,sent INTEGER DEFAULT 0)")
        self.messages = []

    def connect(self):
        import contextlib

        @contextlib.contextmanager
        def session():
            yield self.db
            if self.db.in_transaction:
                self.db.execute("COMMIT")
        return session()

    def rows(self, sql, args=()):
        return [dict(r) for r in self.db.execute(sql, args)]

    def message(self, chat_id, role, content, source=None, attachments=None):
        self.messages.append({"chat_id": chat_id, "role": role, "content": content, "source": source,
                              "attachments": attachments})
        return {"id": "m1", "content": content, "source": source}


def test_a_finished_call_is_shown_in_the_chat_with_its_recording(monkeypatch, tmp_path):
    monkeypatch.setattr(call_runtime.settings, "data_dir", tmp_path)
    runner, step = FakeRunner(), step_with_brief()
    runner.store = ChatStore(tmp_path)
    c = call_runtime.Call(runner, {"id": "r1", "slot": "device-1", "chat_id": "chat1"}, step)
    c.kept = call_runtime.Kept(tmp_path / "chat1" / "call.wav")
    c.show()  # nobody picked up: nothing is shown, nothing is kept
    assert not runner.store.messages and not (tmp_path / "chat1" / "call.wav").exists()
    c.kept = call_runtime.Kept(tmp_path / "chat1" / "call.wav")
    c.answered_at = c.started
    c.kept.begin()
    c.kept.heard(array.array("h", [5] * 24000 * 2).tobytes())
    c.transcript = [{"who": "them", "text": "..."}, {"who": "them", "text": "Hello?"},
                    {"who": "penrose", "text": "Hi Grace,\nthis is penrose."}]
    c.show()
    (message,) = runner.store.messages
    assert message["source"] == "Call" and message["role"] == "assistant"
    assert message["content"].split("\n") == ["Call with Grace · 0:02", "Grace: Hello?", "penrose: Hi Grace, this is penrose."]
    (row,) = runner.store.rows("SELECT * FROM attachments")
    assert message["attachments"] == [row["id"]] and c.file == f"file:{row['id']}"
    assert (row["kind"], row["purpose"], row["mime"], row["done"], row["sent"], row["note"]) == ("audio", "call", "audio/wav", 1, 1, "0:02")
    assert row["name"] == "Call with Grace.wav" and row["size"] == (tmp_path / "chat1" / "call.wav").stat().st_size
    assert ("call_recorded", {"node_id": "c1", "message": {"id": "m1", "content": message["content"], "source": "Call"}}) in runner.store.events
    c.outcome = {"outcome": "answered", "summary": "Under the doormat."}
    assert f"file:{row['id']}" in c.report().summary


def test_android_counting_a_call_as_connected_is_read_from_telecom():
    from app.gateway.call_audio import ACTIVE_CALL
    dialing = "  mCalls: \n    [Call id=TC@5, state=DIALING, tpac=ComponentInfo{com.whatsapp/x.JetpackConnectionService}, ***]"
    assert not ACTIVE_CALL.search(dialing) and not ACTIVE_CALL.search("CallsManager: \n  mCalls: \n  mCallAudioManager:")
    assert ACTIVE_CALL.search(dialing.replace("DIALING", "ACTIVE"))


def test_it_says_goodbye_before_it_hangs_up():
    runner, step = FakeRunner(), step_with_brief()
    c = call_runtime.Call(runner, {"id": "r1", "slot": "device-1"}, step)
    voice = __import__("base64").b64encode(b"\x01\x00" * 480).decode()
    ev = lambda **e: json.dumps(e)  # noqa: E731
    model = FakeSocket([
        ev(type="conversation.item.input_audio_transcription.completed", item_id="t0", transcript="Hello?"),
        ev(type="response.created"),
        ev(type="response.output_audio.delta", item_id="p1", response_id="R1", delta=voice),
        ev(type="response.done", response={"id": "R1"}),
        ev(type="input_audio_buffer.committed", item_id="t1"),  # they answered
        ev(type="response.created"),
        ev(type="response.function_call_arguments.done", name="end_call", response_id="R2", call_id="C2",
           arguments=json.dumps({"outcome": "answered", "summary": "She has had dinner."})),
        ev(type="response.done", response={"id": "R2"}),  # it hung up without a word: it is asked for a goodbye
        ev(type="response.created"),
        ev(type="response.output_audio.delta", item_id="p3", response_id="R3", delta=voice),
        ev(type="response.done", response={"id": "R3"}),  # the goodbye is said: the call ends after it plays
    ])
    phone = FakeSocket([])
    asyncio.run(asyncio.wait_for(c.converse(phone, model), 10))
    sent = [json.loads(m) for m in model.sent if isinstance(m, str)]
    outputs = [m["item"] for m in sent if m["type"] == "conversation.item.create" and m["item"]["type"] == "function_call_output"]
    assert outputs and outputs[0]["type"] == "function_call_output" and outputs[0]["call_id"] == "C2"
    assert "goodbye" in outputs[0]["output"]
    assert [m["type"] for m in sent].count("response.create") == 3  # greeting, reply, goodbye
    assert phone.sent[-1] == "hangup" and c.report().status == "completed"


class FakeLine:
    """Stands in for the Call in dial_and_talk: `answer_after` seconds after it opens, they speak."""

    opened = []

    def __init__(self, runner, state, step, answer_after=None):
        self.answered, self.dialled_at, self.closed = asyncio.Event(), None, False
        self.answer_after = FakeLine.answer_after
        FakeLine.opened.append(self)

    def dialled(self):
        self.dialled_at = 1

    async def run(self):
        try:
            if self.answer_after is not None:
                await asyncio.sleep(self.answer_after)
                self.answered.set()
            while not self.dialled_at:
                await asyncio.sleep(0.01)
            from app.tools.subagent import Report
            return Report(status="completed", summary="Talked with Grace.")
        finally:
            self.closed = True


class FakeDial:
    def __init__(self, seconds, status="completed"):
        self.seconds, self.status, self.stopped = seconds, status, False

    async def run(self):
        from app.tools.subagent import Report
        try:
            await asyncio.sleep(self.seconds)
        except asyncio.CancelledError:
            self.stopped = True
            raise
        return Report(status=self.status, summary="Placed the call." if self.status == "completed" else "No such contact.")


@pytest.mark.parametrize("answer_after, dial_seconds, status, said, dial_stopped", [
    (0.05, 5, "completed", "Talked with Grace.", True),  # they answer while it is still at the dial: it stops
    (None, 0.05, "completed", "Talked with Grace.", False),  # the dial reports first: the line carries on
    (None, 0.05, "failed", "No such contact.", False),  # the dial fails: the line closes, its report comes back
])
def test_the_line_listens_while_the_subagent_dials(monkeypatch, answer_after, dial_seconds, status, said, dial_stopped):
    from app.runtime import orchestrator as orchestrator_module

    FakeLine.answer_after, FakeLine.opened = answer_after, []
    monkeypatch.setattr(orchestrator_module, "Call", FakeLine)
    o = orchestrator_module.Orchestrator(None, {"id": "r1"})
    dial = FakeDial(dial_seconds, status)
    report = asyncio.run(asyncio.wait_for(o.dial_and_talk(dial, {"id": "c1"}), 10))
    assert report.summary == said and dial.stopped is dial_stopped
    assert FakeLine.opened[0].closed


def test_the_end_call_control_is_found_by_name_or_id():
    from app.gateway.call_audio import end_control
    screen = [{"h": "1", "text": "0:42", "c": [
        {"h": "2", "desc": "Turn speaker phone off"},
        {"h": "3", "desc": "End call"},
    ]}]
    assert end_control(screen)["h"] == "3"
    assert end_control([{"h": "4", "id": "end_call_button"}])["h"] == "4"
    assert end_control([{"h": "5", "text": "Extend"}, {"h": "6", "desc": "Endless"}]) is None
