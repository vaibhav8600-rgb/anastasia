"""Status card + chat transcript widgets."""

import customtkinter as ctk

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
