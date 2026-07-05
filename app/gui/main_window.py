"""Main window — minimal dark UI with status card, chat transcript,
big mic button, confirmation panel and text input for testing."""

import customtkinter as ctk

from app.gui.confirmation_dialog import ConfirmationPanel
from app.gui.state_widgets import DevToolsPanel, SetupCard, StatusCard, Transcript

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")


class MainWindow(ctk.CTk):
    """Pure view. All behavior lives in the controller (app/main.py)."""

    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        cfg = controller.config
        self.title(f"{cfg.assistant_name} ({cfg.assistant_nickname}) — local voice assistant")
        self.geometry("760x680")
        self.minsize(620, 540)

        # --- header -----------------------------------------------------
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=16, pady=(14, 6))
        ctk.CTkLabel(header, text=f"💜 {cfg.assistant_nickname}",
                     font=ctk.CTkFont(size=22, weight="bold")).pack(side="left")
        self.status_card = StatusCard(header)
        self.status_card.pack(side="right")

        # --- transcript ----------------------------------------------------
        self.transcript = Transcript(self)
        self.transcript.pack(fill="both", expand=True, padx=16, pady=6)

        # --- confirmation panel (hidden until needed) -----------------------
        self.confirm_panel = ConfirmationPanel(self)

        # --- setup card (hidden unless something critical is missing) --------
        self.setup_card = SetupCard(self)

        # --- controls -------------------------------------------------------
        controls = ctk.CTkFrame(self, fg_color="transparent")
        controls.pack(fill="x", padx=16, pady=(0, 6))
        self._controls = controls

        self.mic_btn = ctk.CTkButton(
            controls, text="🎤", width=84, height=84, corner_radius=42,
            font=ctk.CTkFont(size=30), fg_color="#4f46e5", hover_color="#4338ca",
            command=controller.toggle_mic)
        self.mic_btn.pack(side="left", padx=(0, 14))

        col = ctk.CTkFrame(controls, fg_color="transparent")
        col.pack(side="left", fill="x", expand=True)

        row1 = ctk.CTkFrame(col, fg_color="transparent")
        row1.pack(fill="x", pady=(2, 4))
        self.wake_switch = ctk.CTkSwitch(row1, text="Wake word",
                                         command=controller.toggle_wake_word)
        self.wake_switch.pack(side="left", padx=(0, 12))
        self.voice_switch = ctk.CTkSwitch(row1, text="Voice replies",
                                          command=controller.toggle_voice)
        if cfg.voice_enabled:
            self.voice_switch.select()
        self.voice_switch.pack(side="left", padx=(0, 12))
        self.hotkey_lbl = ctk.CTkLabel(
            row1, text=f"Push-to-talk: {cfg.push_to_talk_hotkey}",
            text_color="#94a3b8", font=ctk.CTkFont(size=12))
        self.hotkey_lbl.pack(side="left")

        row2 = ctk.CTkFrame(col, fg_color="transparent")
        row2.pack(fill="x")
        ctk.CTkButton(row2, text="⚙ Settings", width=100,
                      fg_color="#334155", hover_color="#1e293b",
                      command=controller.open_settings).pack(side="left", padx=(0, 8))
        ctk.CTkButton(row2, text="🕘 History", width=100,
                      fg_color="#334155", hover_color="#1e293b",
                      command=controller.show_history).pack(side="left", padx=(0, 8))
        ctk.CTkButton(row2, text="🧹 Clear history", width=110,
                      fg_color="#334155", hover_color="#1e293b",
                      command=controller.clear_history).pack(side="left", padx=(0, 8))
        ctk.CTkButton(row2, text="</> Dev tools", width=100,
                      fg_color="#334155", hover_color="#1e293b",
                      command=self.toggle_devtools).pack(side="left")

        # --- developer tools (collapsed by default) ---------------------------
        self.devtools = DevToolsPanel(self)
        self._devtools_visible = False

        # --- text input (manual command entry for testing) --------------------
        input_row = ctk.CTkFrame(self, fg_color="transparent")
        input_row.pack(fill="x", padx=16, pady=(0, 14))
        self._input_row = input_row
        self.entry = ctk.CTkEntry(
            input_row, placeholder_text=f"Type a command for {cfg.assistant_nickname}…",
            height=38)
        self.entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.entry.bind("<Return>", lambda _e: self._send())
        ctk.CTkButton(input_row, text="Send ➤", width=90, height=38,
                      command=self._send).pack(side="left")

        self.protocol("WM_DELETE_WINDOW", controller.on_close)

    # ------------------------------------------------------------------
    def _send(self) -> None:
        text = self.entry.get().strip()
        if text:
            self.entry.delete(0, "end")
            self.controller.submit_text(text)

    def set_state(self, state: str, detail: str = "") -> None:
        self.status_card.set_state(state, detail)

    def set_mic_active(self, active: bool) -> None:
        if active:
            self.mic_btn.configure(fg_color="#dc2626", hover_color="#b91c1c", text="⏹")
        else:
            self.mic_btn.configure(fg_color="#4f46e5", hover_color="#4338ca", text="🎤")

    def set_wake_switch(self, on: bool) -> None:
        if on:
            self.wake_switch.select()
        else:
            self.wake_switch.deselect()

    def show_setup_card(self, issues: list, on_recheck) -> None:
        self.setup_card.show(issues, on_recheck, before=self._controls)

    def hide_setup_card(self) -> None:
        self.setup_card.hide()

    def toggle_devtools(self) -> None:
        if self._devtools_visible:
            self.devtools.pack_forget()
        else:
            self.devtools.pack(fill="x", padx=16, pady=(0, 6),
                               before=self._input_row)
        self._devtools_visible = not self._devtools_visible

    def show_history_window(self, rows: list) -> None:
        win = ctk.CTkToplevel(self)
        win.title("Command history")
        win.geometry("720x480")
        win.attributes("-topmost", True)
        box = ctk.CTkTextbox(win, wrap="none", font=ctk.CTkFont(size=12, family="Consolas"))
        box.pack(fill="both", expand=True, padx=10, pady=10)
        if not rows:
            box.insert("end", "No history yet.")
        for r in rows:
            ok = "✔" if r["executed"] else ("✖" if not r["allowed"] else "–")
            box.insert("end",
                       f"[{r['ts']}] {ok} {r['transcript']}\n"
                       f"    intent={r['intent']} tool={r['tool']} args={r['arguments']} "
                       f"risk={r['risk_level']}\n"
                       f"    result: {(r['result'] or r['error'] or '')[:200]}\n\n")
        box.configure(state="disabled")
