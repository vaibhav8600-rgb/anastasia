"""Cost transparency for Gemini Live (Phase 10D.2).

Estimates only — audio-seconds counted locally by the session, priced by
config values the user can edit (pricing changes; defaults verified 2026-07).
Month-to-date spend persists in a tiny local JSON file. Nothing here ever
touches the network, and the monthly cap only WARNS — it never blocks.
"""

import json
from datetime import datetime
from pathlib import Path

from app.config import DATA_DIR

SPEND_PATH = DATA_DIR / "live_spend.json"


def session_cost_usd(audio_in_s: float, audio_out_s: float, config) -> float:
    price_in = float(getattr(config, "live_price_in_per_min", 0.005))
    price_out = float(getattr(config, "live_price_out_per_min", 0.018))
    return (max(0.0, float(audio_in_s)) / 60.0) * price_in \
        + (max(0.0, float(audio_out_s)) / 60.0) * price_out


def _load(path: Path) -> dict:
    month = datetime.now().strftime("%Y-%m")
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("month") == month:
            return {"month": month, "usd": float(data.get("usd", 0.0))}
    except Exception:
        pass
    return {"month": month, "usd": 0.0}   # new month (or no file) resets


def month_spend(path: Path = None) -> float:
    """Month-to-date estimated Gemini Live spend in USD."""
    return _load(path or SPEND_PATH)["usd"]


def add_month_spend(usd: float, path: Path = None) -> float:
    """Accumulate a finished session's estimate; returns the new total.
    Best-effort — a write failure must never break session teardown."""
    path = path or SPEND_PATH
    data = _load(path)
    data["usd"] = round(data["usd"] + max(0.0, float(usd)), 6)
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass
    return data["usd"]
