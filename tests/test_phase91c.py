"""Phase 9.1C: Deepgram Aura TTS backend — streams PCM, circuit breaker with
Piper fallback, barge-in abort, reply-text sanitization, Piper stays default."""

import wave

import pytest

import app.voice.tts_deepgram as aura
from app.voice.speech_output import SpeechOutput
from tests.fakes import make_config


def aura_config(**over):
    over.setdefault("tts_backend", "deepgram")
    over.setdefault("deepgram_api_key", "dg_TESTKEY1234567890")
    return make_config(**over)


class FakeAuraResponse:
    def __init__(self, pcm=b"\x00\x01" * 8000, status=200, text=""):
        self.status_code = status
        self._pcm = pcm
        self.text = text
    def iter_content(self, chunk_size=8192):
        for i in range(0, len(self._pcm), chunk_size):
            yield self._pcm[i:i + chunk_size]


# ---- synthesis ---------------------------------------------------------------

def test_deepgram_tts_streams_pcm_chunks(monkeypatch, tmp_path):
    captured = {}
    def fake_post(url, params=None, json=None, headers=None, timeout=None, stream=None):
        captured["url"] = url; captured["params"] = params
        captured["text"] = json["text"]; captured["auth"] = headers["Authorization"]
        return FakeAuraResponse()
    monkeypatch.setattr(aura.requests, "post", fake_post)
    wav = tmp_path / "a.wav"
    aura.synthesize_deepgram("Hello there.", aura_config(), wav)
    assert wav.exists()
    with wave.open(str(wav), "rb") as wf:
        assert wf.getframerate() == 16000 and wf.getnframes() > 0
    assert captured["params"]["encoding"] == "linear16"
    assert captured["params"]["model"] == "aura-2-luna-en"
    assert captured["auth"] == "Token dg_TESTKEY1234567890"
    assert captured["text"] == "Hello there."


def test_deepgram_tts_auth_error_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(aura.requests, "post",
                        lambda *a, **k: FakeAuraResponse(status=401))
    with pytest.raises(RuntimeError):
        aura.synthesize_deepgram("hi", aura_config(), tmp_path / "a.wav")


# ---- circuit breaker + fallback ----------------------------------------------

def make_speech(**cfg):
    speech = SpeechOutput(aura_config(**cfg))
    speech.shutdown()
    calls = {"aura": 0, "piper_or_win": 0}
    def bad_aura(text):
        calls["aura"] += 1
        raise RuntimeError("Aura down")
    speech._speak_deepgram = bad_aura
    speech._speak_piper_or_windows = lambda t: calls.__setitem__(
        "piper_or_win", calls["piper_or_win"] + 1)
    return speech, calls


def test_deepgram_tts_failure_falls_back_to_piper():
    speech, calls = make_speech()
    speech._speak("hello")
    assert calls["aura"] == 1 and calls["piper_or_win"] == 1   # fell back


def test_tts_circuit_opens_after_2_aura_failures():
    from app.agent.devlog import devlog
    devlog.clear(); devlog.echo_to_stdout = False
    try:
        speech, calls = make_speech()
        for _ in range(4):
            speech._speak("hello")
        assert speech.deepgram_tts_unhealthy
        assert calls["aura"] == 2            # not attempted after the bench
        assert calls["piper_or_win"] == 4    # every line still spoken (Piper)
        benched = [e for e in devlog.entries(50)
                   if "Aura disabled for this session" in e["message"]]
        assert len(benched) == 1             # one warning, no spam
    finally:
        devlog.echo_to_stdout = True


def test_aura_circuit_resets_on_revalidate():
    speech, _ = make_speech()
    speech.deepgram_tts_unhealthy = True
    speech._aura_failures = 2
    speech.reset_aura_circuit()
    assert not speech.deepgram_tts_unhealthy and speech._aura_failures == 0


def test_barge_in_aborts_aura_stream(monkeypatch, tmp_path):
    """If cancel fires during synthesis, playback is skipped."""
    monkeypatch.setattr(aura.requests, "post", lambda *a, **k: FakeAuraResponse())
    speech = SpeechOutput(aura_config())
    speech.shutdown()
    played = []
    speech._play_wav_cancellable = lambda p: played.append(p)
    speech._cancel.set()                     # barge-in already happened
    speech._speak_deepgram("some long reply")
    assert played == []                      # nothing played after barge-in


# ---- sanitization + default --------------------------------------------------

def test_reply_text_sanitized_before_aura():
    """Emoji/markdown/URLs are stripped before any backend (speak_async)."""
    from app.voice import split_sentences
    parts = split_sentences("Done ✅ — see **https://example.com/x** now! 🎉")
    joined = " ".join(parts)
    assert "✅" not in joined and "🎉" not in joined
    assert "**" not in joined and "https://example.com" not in joined


def test_piper_remains_default_local_option():
    # 'auto' never picks Deepgram — the local Piper->SAPI chain is the default
    speech = SpeechOutput(make_config(tts_backend="auto"))
    speech.shutdown()
    used = []
    monkey = __import__("app.voice.tts_piper", fromlist=["piper_available"])
    speech._speak_piper = lambda t: used.append("piper")
    speech._speak_windows = lambda t: used.append("windows")
    speech._speak_deepgram = lambda t: used.append("deepgram")
    import app.voice.tts_piper as tp
    orig = tp.piper_available
    tp.piper_available = lambda c: True
    try:
        speech._speak("hi")
    finally:
        tp.piper_available = orig
    assert "deepgram" not in used            # auto stays local
