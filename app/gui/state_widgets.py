"""Status card, chat transcript, setup card and developer tools widgets."""

import customtkinter as ctk

from app.agent.devlog import devlog

STATES = {
    "idle":                 ("●", "#6b7280", "Idle — ready when you are"),
    "ready":                ("●", "#22d3ee", "Ready — ask me anything"),
    "listening":            ("●", "#22c55e", "Listening…"),
    "transcribing":         ("●", "#eab308", "Transcribing…"),
    "thinking":             ("●", "#3b82f6", "Thinking…"),
    "waiting_confirmation": ("●", "#f97316", "Waiting for your confirmation"),
    "executing":            ("●", "#a855f7", "Executing…"),
    "speaking":             ("●", "#ec4899", "Speaking…"),
    "error":                ("●", "#ef4444", "Error"),
}


class StatusCard(ctk.CTkFrame):
    def __init__(self, master, **kwargs):
        super().__init__(master, corner_radius=12, **kwargs)
        self.dot = ctk.CTkLabel(self, text="●", text_color="#6b7280",
                                font=ctk.CTkFont(size=22))
        self.dot.pack(side="left", padx=(14, 6), pady=8)
        self.label = ctk.CTkLabel(self, text="Idle — ready when you are",
                                  font=ctk.CTkFont(size=14, weight="bold"))
        self.label.pack(side="left", padx=(0, 14), pady=8)

    def set_state(self, state: str, detail: str = "") -> None:
        dot, color, text = STATES.get(state, STATES["idle"])
        self.dot.configure(text=dot, text_color=color)
        self.label.configure(text=detail or text)


class Transcript(ctk.CTkTextbox):
    """Read-only chat-style transcript with colored roles."""

    def __init__(self, master, **kwargs):
        super().__init__(master, corner_radius=12, wrap="word",
                         font=ctk.CTkFont(size=13), **kwargs)
        tb = getattr(self, "_textbox", None)
        if tb is not None:
            tb.tag_config("user", foreground="#7dd3fc", spacing1=10)
            tb.tag_config("assistant", foreground="#f9a8d4", spacing1=4)
            tb.tag_config("intent", foreground="#94a3b8", spacing1=2)
            tb.tag_config("info", foreground="#a3a3a3", spacing1=2)
            tb.tag_config("error", foreground="#f87171", spacing1=4)
        self.configure(state="disabled")

    def _append(self, text: str, tag: str) -> None:
        self.configure(state="normal")
        tb = getattr(self, "_textbox", self)
        tb.insert("end", text + "\n", (tag,))
        self.configure(state="disabled")
        self.see("end")

    def add_user(self, text: str) -> None:
        self._append(f"🧑 You: {text}", "user")

    def add_assistant(self, text: str, name: str = "Anna") -> None:
        self._append(f"💜 {name}: {text}", "assistant")

    def add_intent(self, text: str) -> None:
        self._append(f"   ⚙ {text}", "intent")

    def add_info(self, text: str) -> None:
        self._append(f"ℹ {text}", "info")

    def add_error(self, text: str) -> None:
        self._append(f"⚠ {text}", "error")

    def clear(self) -> None:
        self.configure(state="normal")
        tb = getattr(self, "_textbox", self)
        tb.delete("1.0", "end")
        self.configure(state="disabled")


class SetupCard(ctk.CTkFrame):
    """One clean card for critical startup problems (spec sec 18).
    Hidden when everything is healthy."""

    def __init__(self, master, **kwargs):
        super().__init__(master, corner_radius=12, border_width=2,
                         border_color="#fbbf24", **kwargs)
        ctk.CTkLabel(self, text="⚠ Setup needed",
                     font=ctk.CTkFont(size=14, weight="bold")).pack(
            anchor="w", padx=14, pady=(8, 0))
        self.message_lbl = ctk.CTkLabel(self, text="", anchor="w", justify="left",
                                        wraplength=620, text_color="#fcd34d",
                                        font=ctk.CTkFont(size=12))
        self.message_lbl.pack(fill="x", padx=14, pady=(2, 0))
        self.recheck_btn = ctk.CTkButton(self, text="Recheck", width=90,
                                         fg_color="#334155", hover_color="#1e293b")
        self.recheck_btn.pack(anchor="w", padx=14, pady=(6, 10))
        self._pack_kwargs = {}

    def show(self, issues: list, on_recheck, **pack_kwargs) -> None:
        self.message_lbl.configure(text="\n".join(f"• {i}" for i in issues))
        self.recheck_btn.configure(command=on_recheck)
        self._pack_kwargs = pack_kwargs or self._pack_kwargs
        self.pack(fill="x", padx=16, pady=(0, 6), **self._pack_kwargs)

    def hide(self) -> None:
        self.pack_forget()


class DevToolsPanel(ctk.CTkFrame):
    """Collapsible developer log view — every diagnostic line lives here,
    never in the chat (spec sec 5). Subscribes to the devlog."""

    def __init__(self, master, **kwargs):
        super().__init__(master, corner_radius=12, **kwargs)
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=10, pady=(6, 0))
        ctk.CTkLabel(header, text="</> Developer tools", text_color="#94a3b8",
                     font=ctk.CTkFont(size=12, weight="bold")).pack(side="left")
        ctk.CTkButton(header, text="Clear", width=60, height=22,
                      fg_color="#334155", hover_color="#1e293b",
                      command=self._clear).pack(side="right")
        self.box = ctk.CTkTextbox(self, wrap="none", height=170,
                                  font=ctk.CTkFont(size=11, family="Consolas"))
        self.box.pack(fill="both", expand=True, padx=10, pady=(4, 8))
        self.box.configure(state="disabled")
        for entry in devlog.entries():
            self._append(entry)
        devlog.subscribe(self._on_entry)

    def _on_entry(self, entry: dict) -> None:
        try:  # marshal onto the GUI thread; window may be closing
            self.after(0, lambda: self._append(entry))
        except Exception:
            pass

    def _append(self, entry: dict) -> None:
        self.box.configure(state="normal")
        tb = getattr(self.box, "_textbox", self.box)
        tb.insert("end", f"[{entry['ts']}] [{entry['category']}] {entry['message']}\n")
        self.box.configure(state="disabled")
        self.box.see("end")

    def _clear(self) -> None:
        devlog.clear()
        self.box.configure(state="normal")
        tb = getattr(self.box, "_textbox", self.box)
        tb.delete("1.0", "end")
        self.box.configure(state="disabled")
