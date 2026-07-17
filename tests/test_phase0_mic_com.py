"""Phase 0 split regression: the mic must open off the main thread.

Bisect: the mic works in legacy single-process and fails in --core with
`WdmSyncIoctl: DeviceIoControl GLE = 0x00000492`. Root cause (probes 1–4):
a WASAPI/WDM-KS callback-mode input stream open requires the OPENING THREAD to
be a COM single-threaded apartment. Legacy opens it on the WinForms GUI thread
(already STA); the split opens it on an asyncio ThreadPoolExecutor worker with
no COM apartment, so PortAudio fails. It is NOT a double-open — a single open
on a non-STA thread fails just the same; `CoInitializeEx(STA)` on that thread
is necessary and sufficient.

`Recorder.start()`/`stop()` now call `ensure_com_sta()` first. These guards pin
that so the regression can't silently return.
"""

import asyncio
import sys
import threading
import types

import pytest

import app.voice.recorder as rec_mod
from app.voice.recorder import Recorder, ensure_com_sta
from tests.fakes import make_config


# ---- hardware-independent: STA is ensured BEFORE the stream is opened ---------

def test_recorder_ensures_com_sta_before_opening_the_stream(monkeypatch):
    order = []
    monkeypatch.setattr(rec_mod, "ensure_com_sta", lambda: order.append("sta"))
    monkeypatch.setattr(rec_mod, "resolve_microphone_device",
                        lambda cfg, entries=None: (None, {"label": "fake",
                                                          "default_samplerate": 16000}, ""))
    monkeypatch.setattr(rec_mod, "choose_capture_sample_rate", lambda *a: 16000)

    class FakeStream:
        def __init__(self, **k): order.append("open")
        def start(self): order.append("start")
        def stop(self): pass
        def close(self): pass

    fake_sd = types.SimpleNamespace(InputStream=lambda **k: FakeStream(**k))
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sd)

    Recorder(make_config()).start()
    assert "sta" in order and "open" in order
    assert order.index("sta") < order.index("open"), (
        f"COM STA must be ensured before the stream opens; got {order}")


def test_stop_ensures_com_sta_too(monkeypatch):
    """stop() runs on a different worker thread than start() (the daemon spawns
    _finish_recording), so it needs its own STA before closing the stream."""
    calls = []
    monkeypatch.setattr(rec_mod, "ensure_com_sta", lambda: calls.append("sta"))

    class FakeStream:
        def stop(self): calls.append("close")
        def close(self): pass

    r = Recorder(make_config())
    r._stream = FakeStream()
    r._recording = True
    r.stop()
    assert calls and calls[0] == "sta", f"STA must precede close; got {calls}"


def test_ensure_com_sta_is_idempotent_and_never_raises():
    # Run entirely on a worker thread — never CoInitialize the main pytest
    # thread, whose COM apartment would then persist for the whole session.
    done = {}

    def worker():
        try:
            ensure_com_sta()
            ensure_com_sta()        # second call on the same thread is a no-op
            done["ok"] = True
        except Exception as e:      # must never raise, on any platform
            done["ok"] = repr(e)

    t = threading.Thread(target=worker)
    t.start(); t.join()
    assert done["ok"] is True


# ---- real hardware proof of the exact daemon path (skips without a mic) -------

def _has_input_device() -> bool:
    try:
        import sounddevice as sd
        return any(d.get("max_input_channels", 0) > 0 for d in sd.query_devices())
    except Exception:
        return False


@pytest.mark.skipif(not _has_input_device(), reason="no input device")
def test_real_recorder_opens_on_asyncio_executor_thread():
    """The precise failing path: Recorder.start() on an asyncio executor
    worker (where the daemon runs WS request handlers). Before the fix this
    raised 0x492; now it records."""
    rec = Recorder(make_config())

    async def drive():
        loop = asyncio.get_running_loop()
        started = await loop.run_in_executor(
            None, lambda: (rec.start(), rec.recording)[1])
        assert started, "recorder did not start on the executor thread"
        await asyncio.sleep(0.2)
        # stop on a DIFFERENT executor call — cross-STA-thread close must work
        data = await loop.run_in_executor(None, rec.stop)
        return data

    data = asyncio.run(drive())
    assert not rec.recording
    assert data is not None and len(data) > 0, "captured no audio"
