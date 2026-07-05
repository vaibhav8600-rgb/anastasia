"""Half-duplex audio gate (spec section 13a).

`speaking` is set for the entire TTS playback plus a short tail so room
echo dies out. While it is set, the microphone recorder AND the wake-word
listener drop all audio — Anna must never transcribe her own voice.
"""

import threading

speaking = threading.Event()

# Extra time after playback before the mic reopens (seconds).
TAIL_SECONDS = 0.4
