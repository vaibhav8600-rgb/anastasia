"""Local salience rules (Phase 1A commit 5): score every event so the feed shows
a NUMBER to calibrate against. Rules only — no cloud, no routing, no speech.

TOML via stdlib `tomllib` (zero dep, comments, read-only — D-1.3). The rules
file is seeded to the data dir on first run and hot-reloaded; the user annotates
it live during the soak.

Three guarantees (the riders):
  1. **Provenance** — score() returns WHICH rule fired, stamped onto the event
     ("rule"), so the feed shows "disk_low → 7 (rule: disk_low)", not a bare
     number. Calibration needs the reason.
  2. **Fail-safe hot-reload** — a malformed edit KEEPS the last-good rules, is
     reported in --doctor, and never crashes or silently zeroes scores.
  3. **Unknown kinds default to score 2 (log-only), fail quiet** — a new event
     type nobody wrote a rule for is logged, not dropped and not shouted about.
"""

import time
import tomllib
from pathlib import Path

from app.agent.devlog import devlog
from app.config import DATA_DIR

DEFAULT_SCORE = 2
RULES_PATH = DATA_DIR / "salience_rules.toml"

DEFAULT_RULES_TOML = """\
# Salience rules — event kind -> score (0..10). Higher = more attention-worthy.
# Edit this live during the soak; a malformed edit KEEPS the last-good table
# (check: python app\\main.py --doctor). Unknown kinds default to 2 (log-only).
# In Phase 1A nothing speaks — these scores only annotate the feed so you can
# judge "would I have wanted to hear this?" before thresholds get a voice in 1B.

# --- system ---
disk_low = 7
ram_high = 5
battery_low = 6
battery_full = 3

# --- filesystem ---
file_added = 3
file_changed = 2

# --- active window ---
app_switch = 1
focus_session = 4

# --- presence ---
user_idle = 2
user_returned = 2
session_locked = 3
session_unlocked = 3

# --- clock ---
briefing_time = 5
"""


class SalienceEngine:
    def __init__(self, path=None, *, clock=time.monotonic, seed=True,
                 reload_throttle_s=2.0):
        self.path = Path(path) if path else RULES_PATH
        self._clock = clock
        self._throttle = reload_throttle_s
        self._rules = {}
        self._mtime = None
        self._last_check = -1e18
        self.error = None                 # last malformed-reload error (doctor)
        if seed:
            self._seed_default()
        self._load(force=True)

    def _seed_default(self) -> None:
        try:
            if not self.path.exists():
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(DEFAULT_RULES_TOML, encoding="utf-8")
        except Exception:
            pass

    def _load(self, force=False) -> None:
        try:
            mtime = self.path.stat().st_mtime
        except Exception:
            if not self._rules:           # no file at all → embedded defaults
                self._rules = self._coerce(tomllib.loads(DEFAULT_RULES_TOML))
            return
        if not force and mtime == self._mtime:
            return
        try:
            with open(self.path, "rb") as f:
                loaded = tomllib.load(f)
            self._rules = self._coerce(loaded)
            self._mtime = mtime
            if self.error:
                devlog.log("salience rules reloaded OK — recovered from a "
                           "malformed edit.")
            self.error = None
        except Exception as e:
            # FAIL-SAFE: keep last-good rules; report; never crash or zero.
            self._mtime = mtime           # don't re-report the same bad file each event
            self.error = f"{self.path.name}: {' '.join(str(e).split())[:120]}"
            if not self._rules:
                # Malformed on the VERY FIRST load — there is no last-good table
                # to keep, and an empty table would silently collapse every score
                # to the default. Fall back to the embedded defaults so the
                # guarantee ("never zeroes") holds even at cold start.
                self._rules = self._coerce(tomllib.loads(DEFAULT_RULES_TOML))
            devlog.warn(f"salience rules malformed — keeping the last-good table "
                        f"({self.error})")

    @staticmethod
    def _coerce(raw) -> dict:
        out = {}
        for k, v in (raw or {}).items():
            try:
                out[str(k)] = max(0, min(10, int(v)))
            except Exception:
                pass                       # a non-int value is skipped, not fatal
        return out

    def _maybe_reload(self) -> None:
        now = self._clock()
        if now - self._last_check < self._throttle:
            return
        self._last_check = now
        self._load()

    def score(self, event):
        """(score, rule). key = payload.kind or event.type; unknown → (2,
        'default')."""
        self._maybe_reload()
        payload = getattr(event, "payload", None) or {}
        key = str(payload.get("kind") or getattr(event, "type", "") or "")
        if key in self._rules:
            return self._rules[key], key
        return DEFAULT_SCORE, "default"

    def status(self) -> dict:
        return {"rules": len(self._rules), "error": self.error,
                "path": str(self.path)}
