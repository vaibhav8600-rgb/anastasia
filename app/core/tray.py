"""System-tray icon for anna-core (Phase 0, commit 6, D-0.2).

Core-owned (the daemon has no window; the tray is the only always-present
affordance). Menu: **Open Anna** · **Pause listening** (checkable) · **Quit**.

Design rule — **the tray is a convenience, not a heartbeat.** Its thread runs
supervised: if pystray dies (a driver quirk, a shell restart), the exception is
caught, recorded to the event log as component "tray" (so `--doctor` can see the
tray is gone), and CORE KEEPS RUNNING. Nothing about the daemon's liveness,
safety, or the WebSocket depends on the tray being up.

pystray is imported lazily and treated as optional: no pystray, no tray, core
runs headless exactly the same. The pystray-specific object is built by an
injectable `icon_factory`, so the supervision and menu logic are unit-tested
without a display.
"""

import threading

from app.agent.devlog import devlog


def _default_icon_factory(tray: "Tray"):
    """Build a real pystray.Icon. Imported here so a machine without pystray
    (or without a desktop) still runs core — the caller catches ImportError."""
    import pystray
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((10, 10, 54, 54), fill=(56, 130, 246, 255))     # Anna blue orb

    def _menu():
        return pystray.Menu(
            pystray.MenuItem("Open Anna", lambda *_: tray._fire("open"),
                             default=True),
            pystray.MenuItem("Pause listening", lambda *_: tray._fire("pause"),
                             checked=lambda _i: tray.paused),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", lambda *_: tray._fire("quit")),
        )

    return pystray.Icon("anna", image, "Anastasia (Anna)", menu=_menu())


class Tray:
    def __init__(self, *, on_open=None, on_pause_toggle=None, on_quit=None,
                 eventlog=None, icon_factory=None):
        self._on_open = on_open
        self._on_pause_toggle = on_pause_toggle
        self._on_quit = on_quit
        self._eventlog = eventlog
        self._icon_factory = icon_factory or _default_icon_factory
        self._icon = None
        self._thread = None
        self.paused = False
        self.alive = False              # True only while the tray thread runs

    # ------------------------------------------------------------- lifecycle
    def start(self) -> bool:
        """Build the icon and run it on a supervised daemon thread. Returns
        False (and leaves core untouched) if the tray can't be created —
        pystray missing, no desktop, etc."""
        try:
            self._icon = self._icon_factory(self)
        except Exception as e:
            devlog.warn(f"Tray unavailable — core runs without it: "
                        f"{' '.join(str(e).split())[:120]}")
            self._emit_error(f"tray could not start: {e}")
            return False
        self._thread = threading.Thread(target=self._run_guarded, daemon=True,
                                        name="anna-tray")
        self._thread.start()
        return True

    def _run_guarded(self) -> None:
        """The tray's whole life, wrapped. A crash here is recorded and
        contained — it must never propagate into core."""
        self.alive = True
        try:
            self._icon.run()            # blocks until stop()
        except Exception as e:
            # The heartbeat rule: tray death is a doctor-level fact, not a
            # core-fatal one. Record it and let core carry on.
            devlog.warn(f"Tray thread died (core continues): "
                        f"{' '.join(str(e).split())[:120]}")
            self._emit_error(f"tray thread died: {e}")
        finally:
            self.alive = False

    def stop(self) -> None:
        icon = self._icon
        if icon is not None:
            try:
                icon.stop()
            except Exception:
                pass
        self.alive = False

    # -------------------------------------------------------------- actions
    def _fire(self, action: str) -> None:
        """A menu click. Runs on the pystray thread; the callbacks marshal to
        core themselves. Never let a callback exception kill the tray loop."""
        try:
            if action == "open" and self._on_open:
                self._on_open()
            elif action == "pause":
                self.set_paused(not self.paused, notify=True)
            elif action == "quit" and self._on_quit:
                self._on_quit()
        except Exception as e:
            devlog.exception(e, context=f"tray:{action}")

    def set_paused(self, paused: bool, *, notify: bool = False) -> None:
        """Reflect (and optionally drive) the paused state. `notify` fires the
        toggle callback (a menu click); without it this only syncs the tick
        (an external pause, e.g. from the window)."""
        self.paused = bool(paused)
        if notify and self._on_pause_toggle:
            self._on_pause_toggle(self.paused)
        icon = self._icon
        if icon is not None:
            try:
                icon.update_menu()
            except Exception:
                pass

    # ---------------------------------------------------------------- audit
    def _emit_error(self, message: str) -> None:
        if self._eventlog is None:
            return
        try:
            self._eventlog.emit("error", source="tray", component="tray",
                                message=message)
        except Exception:
            pass
