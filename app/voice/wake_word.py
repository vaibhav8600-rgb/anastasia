"""Optional wake word listener using openWakeWord (disabled by default).

Uses the pre-trained "hey jarvis" model — a custom "Hey Anna" model would
need to be trained separately. If openwakeword isn't installed, enabling
the switch shows a friendly message instead of crashing.
"""

import threading
import time


class WakeWordUnavailable(Exception):
    pass


class WakeWordListener(threading.Thread):
    SCORE_THRESHOLD = 0.6
    COOLDOWN_SECONDS = 3.0

    def __init__(self, config, on_wake):
        super().__init__(daemon=True)
        self.config = config
        self.on_wake = on_wake
        self._stop_flag = threading.Event()

        try:
            import openwakeword  # noqa: F401
            import sounddevice  # noqa: F401
        except ImportError as e:
            raise WakeWordUnavailable(
                "Wake word needs the optional 'openwakeword' package: "
                "pip install openwakeword") from e

    def run(self) -> None:
        import numpy as np
        import sounddevice as sd
        from openwakeword.model import Model

        try:
            model = Model(wakeword_models=["hey_jarvis"], inference_framework="onnx")
        except Exception:
            model = Model()  # fall back to all bundled models

        from app.voice import audio_gate

        chunk = 1280  # 80 ms @ 16 kHz — what openWakeWord expects
        last_fire = 0.0
        with sd.InputStream(samplerate=16000, channels=1, dtype="int16",
                            blocksize=chunk) as stream:
            while not self._stop_flag.is_set():
                audio, _ = stream.read(chunk)
                if audio_gate.speaking.is_set():
                    continue  # half-duplex: ignore Anna's own speech (sec 13a)
                scores = model.predict(np.squeeze(audio))
                if any(s >= self.SCORE_THRESHOLD for s in scores.values()):
                    now = time.time()
                    if now - last_fire >= self.COOLDOWN_SECONDS:
                        last_fire = now
                        model.reset()
                        self.on_wake()

    def stop(self) -> None:
        self._stop_flag.set()
