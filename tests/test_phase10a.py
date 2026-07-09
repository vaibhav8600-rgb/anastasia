"""Phase 10A: GeminiLiveSession lifecycle — audio in/out, resumption, stall
watchdog, guaranteed teardown, transcripts as display-only. The SDK session is
fully mocked; no real API calls."""

import asyncio
import threading
import time
from types import SimpleNamespace as NS

import pytest

from app.voice.gemini_live import (GeminiLiveSession, GeminiLiveUnavailable,
                                   gemini_live_available, gemini_key)
from tests.fakes import make_config


@pytest.fixture(autouse=True)
def _no_env_keys(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)


def live_config(**over):
    over.setdefault("gemini_api_key", "AIzaTESTKEY123456789")
    over.setdefault("live_stall_timeout_s", 20.0)
    # 10D hard gate: a session only starts with the engine selected AND the
    # explicit continuous-audio consent.
    over.setdefault("engine_mode", "gemini_live")
    over.setdefault("live_audio_consent", True)
    return make_config(**over)


class FakeSession:
    """Mimics the google-genai live session: records sends; receive() is a
    PER-TURN generator like the real SDK — each call yields one scripted
    turn's messages, then ends (the connection stays open). Turns exhausted:
    hold_open=True blocks (open but idle), hold_open=False returns instantly
    with nothing (a closed socket)."""

    def __init__(self, script=None, turns=None, hold_open=True):
        self.sent_audio = []
        self.sent_text = []
        self.sent_tool_responses = []
        self.turns = [list(t) for t in (turns if turns is not None
                                        else ([script] if script else []))]
        self.hold_open = hold_open
        self.exited = threading.Event()

    async def send_realtime_input(self, audio=None, **_k):
        self.sent_audio.append(audio)

    async def send_client_content(self, turns=None, turn_complete=True, **_k):
        self.sent_text.append(turns)

    async def send_tool_response(self, function_responses=None, **_k):
        self.sent_tool_responses.append(function_responses)

    async def receive(self):
        if self.turns:
            for msg in self.turns.pop(0):
                yield msg
            return                     # turn complete — connection still open
        while self.hold_open:          # open but idle
            await asyncio.sleep(0.02)
        # hold_open False -> closed socket: end immediately, no messages

    class _CM:
        def __init__(self, outer): self.outer = outer
        async def __aenter__(self): return self.outer
        async def __aexit__(self, *a):
            self.outer.exited.set()
            return False

    def cm(self):
        return FakeSession._CM(self)


def audio_msg(data=b"\x00\x01" * 2400):
    part = NS(inline_data=NS(data=data))
    return NS(server_content=NS(model_turn=NS(parts=[part]), interrupted=False,
                                input_transcription=None,
                                output_transcription=None, turn_complete=False),
              session_resumption_update=None, go_away=None, tool_call=None)


def make_session(monkeypatch, config=None, script=None, sessions=None, **cb):
    """Session with the SDK connect fully mocked. `sessions` lets a test
    provide successive FakeSessions (for resumption)."""
    config = config or live_config()
    fakes = sessions or [FakeSession(script=script)]
    calls = {"n": 0, "handles": []}

    def fake_connect(self, config_obj):
        calls["handles"].append(getattr(config_obj.session_resumption, "handle", None)
                                if getattr(config_obj, "session_resumption", None)
                                else None)
        fake = fakes[min(calls["n"], len(fakes) - 1)]
        calls["n"] += 1
        return fake.cm()

    monkeypatch.setattr(GeminiLiveSession, "_connect", fake_connect)
    # config building imports google.genai.types (installed) — keep it real.
    session = GeminiLiveSession(config, on_audio_out=cb.pop("on_audio_out", lambda b: None), **cb)
    return session, fakes, calls


# ---- audio in ------------------------------------------------------------------

def test_live_session_streams_input_audio_when_mic_open(monkeypatch):
    session, fakes, _ = make_session(monkeypatch)
    session.start()
    assert session.active
    session.send_audio(b"\x01\x02" * 1600)     # a tee'd recorder frame
    session.send_audio(b"\x03\x04" * 1600)
    deadline = time.time() + 2
    while len(fakes[0].sent_audio) < 2 and time.time() < deadline:
        time.sleep(0.02)
    assert len(fakes[0].sent_audio) == 2
    assert fakes[0].sent_audio[0].mime_type == "audio/pcm;rate=16000"
    assert session.audio_in_seconds > 0        # cost meter runs
    session.close()


# ---- audio out -----------------------------------------------------------------

def test_live_output_audio_plays_through_existing_pipeline(monkeypatch):
    played = []
    session, fakes, _ = make_session(monkeypatch,
                                     script=[audio_msg(b"\x11\x22" * 4800)],
                                     on_audio_out=played.append)
    session.start()
    deadline = time.time() + 2
    while not played and time.time() < deadline:
        time.sleep(0.02)
    assert played and played[0] == b"\x11\x22" * 4800
    assert session.audio_out_seconds > 0
    session.close()


# ---- teardown ------------------------------------------------------------------

def test_session_closes_on_mic_close_no_lingering_socket(monkeypatch):
    closed = []
    session, fakes, _ = make_session(monkeypatch, on_closed=closed.append)
    session.start()
    assert session.active
    session.close("mic closed")
    deadline = time.time() + 2
    while not fakes[0].exited.is_set() and time.time() < deadline:
        time.sleep(0.02)
    assert fakes[0].exited.is_set()            # context manager exited = socket closed
    assert not session.active
    assert closed and closed[0] == "mic closed"
    session.close("again")                     # idempotent
    assert closed.count("mic closed") == 1


# ---- resumption ----------------------------------------------------------------

def test_session_resumption_on_time_limit_preserves_context(monkeypatch):
    resumption = NS(session_resumption_update=NS(resumable=True,
                                                 new_handle="HANDLE-42"),
                    go_away=None, tool_call=None, server_content=None)
    first = FakeSession(script=[resumption], hold_open=False)   # then server ends it
    second = FakeSession()                                       # resumed session
    session, fakes, calls = make_session(monkeypatch, sessions=[first, second])
    session.start()
    deadline = time.time() + 3
    while calls["n"] < 2 and time.time() < deadline:
        time.sleep(0.02)
    assert calls["n"] >= 2                     # reconnected after the cap
    assert session.resumption_handle == "HANDLE-42"
    assert calls["handles"][1] == "HANDLE-42"  # handle passed on reconnect
    assert session.active
    session.close()


# ---- one connection across turns (regression: composite replies) -----------------

def test_multi_turn_conversation_stays_on_one_connection(monkeypatch):
    """Regression (user-reported): receive() ends at EVERY turn_complete in
    the SDK. That must NOT tear the connection down and resume — the resumed
    checkpoint lags the just-delivered answer, so the model re-answered all
    the old turns in one composite reply. Three turns ride ONE connection."""
    turns = [[audio_msg(b"\x01\x01" * 2400)],
             [audio_msg(b"\x02\x02" * 2400)],
             [audio_msg(b"\x03\x03" * 2400)]]
    played = []
    session, fakes, calls = make_session(monkeypatch,
                                         sessions=[FakeSession(turns=turns)],
                                         on_audio_out=played.append)
    session.start()
    deadline = time.time() + 3
    while len(played) < 3 and time.time() < deadline:
        time.sleep(0.02)
    assert len(played) == 3            # every turn's audio arrived...
    assert calls["n"] == 1             # ...on a single connection, no resumes
    session.close()


def test_dropped_connection_resumes_with_handle_not_error(monkeypatch):
    """A mid-conversation socket drop resumes seamlessly with the checkpoint
    handle instead of ending the whole conversation as a failure."""
    resumption = NS(session_resumption_update=NS(resumable=True,
                                                 new_handle="H1"),
                    go_away=None, tool_call=None, server_content=None)

    class DroppingSession(FakeSession):
        async def receive(self):
            if self.turns:
                for msg in self.turns.pop(0):
                    yield msg
                return
            raise ConnectionError("socket dropped mid-conversation")

    errors = []
    session, fakes, calls = make_session(
        monkeypatch, sessions=[DroppingSession(turns=[[resumption]]),
                               FakeSession()],
        on_error=errors.append)
    session.start()
    deadline = time.time() + 3
    while calls["n"] < 2 and time.time() < deadline:
        time.sleep(0.02)
    assert calls["n"] >= 2             # reconnected...
    assert calls["handles"][1] == "H1"  # ...with the checkpoint handle
    assert session.active
    assert errors == []                # seamless — never surfaced as failure
    session.close()


# ---- stall watchdog -------------------------------------------------------------

def test_stalled_session_triggers_teardown(monkeypatch):
    errors, closed = [], []
    session, fakes, _ = make_session(monkeypatch,
                                     config=live_config(live_stall_timeout_s=1.2),
                                     on_error=errors.append,
                                     on_closed=closed.append)
    session.start()
    session.send_text("hello?")                # now awaiting a reply...
    deadline = time.time() + 6
    while session.active and time.time() < deadline:
        time.sleep(0.05)
    assert not session.active                  # watchdog tore it down
    assert any("stalled" in e for e in errors)


# ---- transcripts are display-only ------------------------------------------------

def test_live_transcripts_are_display_only_not_execution_path(monkeypatch):
    transcript_msg = NS(
        server_content=NS(model_turn=None, interrupted=False,
                          input_transcription=NS(text="close chrome"),
                          output_transcription=NS(text="Sure thing!"),
                          turn_complete=True),
        session_resumption_update=None, go_away=None, tool_call=None)
    shown_in, shown_out, executed = [], [], []
    session, fakes, _ = make_session(
        monkeypatch, script=[transcript_msg],
        on_input_transcript=shown_in.append,
        on_output_transcript=shown_out.append)
    # a spy standing in for the pipeline — the session has NO reference to it
    session.start()
    deadline = time.time() + 2
    while not shown_out and time.time() < deadline:
        time.sleep(0.02)
    assert shown_in == ["close chrome"]        # display text delivered...
    assert shown_out == ["Sure thing!"]
    assert executed == []                      # ...and nothing executed:
    # the ONLY execution entry point is on_tool_call -> local validator (10B);
    # transcripts carry no callable and the session holds no pipeline handle.
    assert not hasattr(session, "pipeline") and not hasattr(session, "execute")
    session.close()


# ---- availability gate ------------------------------------------------------------

def test_unavailable_without_key():
    ok, reason = gemini_live_available(make_config(gemini_api_key=""))
    assert not ok and "key" in reason.lower()
    with pytest.raises(GeminiLiveUnavailable):
        GeminiLiveSession(live_config(gemini_api_key=""),
                          on_audio_out=lambda b: None).start()


def test_key_resolution_env_wins(monkeypatch):
    config = live_config(gemini_api_key="AIzaCONFIG")
    assert gemini_key(config) == "AIzaCONFIG"
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaENV")
    assert gemini_key(config) == "AIzaENV"
