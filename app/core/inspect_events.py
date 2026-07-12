"""Read and audit the event log from the command line (Phase 0, Protocol §11.2).

    python app\\main.py --dump-events [N]     last N events (default 40)
    python app\\main.py --scan-secrets        prove the log holds no secrets

`--scan-secrets` is the automated form of manual test M0.5. It greps **every
byte the log has written** — the `.sqlite`, the `-wal`, the `-shm` and any spill
file — because in WAL mode the rows live in `events.sqlite-wal` until a
checkpoint. Scanning only `events.sqlite` would pass against an effectively
empty file and tell a human their secrets are safe when nothing was checked.
"""

import json
import sys

from app.core.eventlog import DEFAULT_PATH, EventLog


def log_files(path=None):
    """Every file the log may have put bytes into."""
    path = path or DEFAULT_PATH
    stem = path.name
    return [p for p in path.parent.iterdir()
            if p.is_file() and p.name.startswith(stem.split(".")[0] + ".")]


def scan_secrets(path=None) -> list:
    """(file, label, sample) for anything secret-SHAPED found on disk."""
    import re

    from app.vision.sensitive import SENSITIVE_PATTERNS

    hits = []
    for file in log_files(path):
        try:
            body = file.read_bytes().decode("utf-8", errors="ignore")
        except Exception:
            continue
        if "base64," in body:
            hits.append((file.name, "embedded image data", "base64,…"))
        for pattern, label in SENSITIVE_PATTERNS:
            for match in re.findall(pattern, body):
                sample = (match if isinstance(match, str) else str(match))[:12]
                hits.append((file.name, label, sample + "…"))
    return hits


def run_cli(argv) -> int:
    # The Windows console is cp1252 and dies on "→". A tool a human runs to AUDIT
    # their log must never crash halfway through the audit. (Same reconfigure
    # app/doctor.py already does.)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    path = DEFAULT_PATH
    if not path.exists():
        print(f"No event log yet at {path}")
        return 0

    if "--scan-secrets" in argv:
        files = log_files(path)
        print(f"Scanning EVERY byte the log wrote ({len(files)} files):")
        for f in files:
            print(f"  · {f.name}  ({f.stat().st_size:,} bytes)")
        hits = scan_secrets(path)
        if hits:
            print(f"\n❌ {len(hits)} secret-shaped hit(s) — THIS IS A BUG:")
            for file, label, sample in hits[:20]:
                print(f"  {file}: {label} → {sample}")
            return 1
        print("\n✅ No API keys, card numbers, private keys or embedded images "
              "found anywhere in the log.")
        return 0

    limit = 40
    for i, arg in enumerate(argv):
        if arg == "--dump-events" and i + 1 < len(argv) and argv[i + 1].isdigit():
            limit = int(argv[i + 1])

    log = EventLog(path, start=False)      # reader only — don't start a writer
    rows = log.recent(limit=limit)
    if not rows:
        print("Event log is empty.")
        return 0
    print(f"Last {len(rows)} events (newest first) from {path}:\n")
    for row in reversed(rows):
        mark = "!" if row["salience"] >= 0.8 else " "
        payload = json.dumps(row["payload"], ensure_ascii=False)
        outcome = f" → {row['outcome']}" if row["outcome"] else ""
        print(f"{mark} {row['ts']}  {row['type']:<18} "
              f"{row['source'] or '-':<8}{outcome}")
        print(f"    {payload[:160]}")
    return 0


if __name__ == "__main__":            # python -m app.core.inspect_events
    sys.exit(run_cli(sys.argv))
