"""Settings window — edits config.json and memory.json."""

import json

import customtkinter as ctk


class SettingsWindow(ctk.CTkToplevel):
    def __init__(self, master, config, memory, on_saved=None):
        super().__init__(master)
        self.config_obj = config
        self.memory = memory
        self.on_saved = on_saved
        self.title("Anna — Settings")
        self.geometry("560x640")
        self.attributes("-topmost", True)

        body = ctk.CTkScrollableFrame(self)
        body.pack(fill="both", expand=True, padx=12, pady=12)
        self._entries = {}
        self._switches = {}

        def section(text):
            ctk.CTkLabel(body, text=text, font=ctk.CTkFont(size=14, weight="bold")
                         ).pack(anchor="w", pady=(14, 4))

        def entry(key, label, value):
            row = ctk.CTkFrame(body, fg_color="transparent")
            row.pack(fill="x", pady=2)
            ctk.CTkLabel(row, text=label, width=170, anchor="w").pack(side="left")
            e = ctk.CTkEntry(row)
            e.insert(0, str(value))
            e.pack(side="left", fill="x", expand=True)
            self._entries[key] = e

        def switch(key, label, value):
            s = ctk.CTkSwitch(body, text=label)
            if value:
                s.select()
            s.pack(anchor="w", pady=2)
            self._switches[key] = s

        def option(key, label, value, values):
            row = ctk.CTkFrame(body, fg_color="transparent")
            row.pack(fill="x", pady=2)
            ctk.CTkLabel(row, text=label, width=170, anchor="w").pack(side="left")
            var = ctk.StringVar(value=value)
            ctk.CTkOptionMenu(row, variable=var, values=values).pack(side="left")
            self._entries[key] = var

        c = config
        section("Assistant")
        entry("assistant_name", "Full name", c.assistant_name)
        entry("assistant_nickname", "Nickname / call name", c.assistant_nickname)
        switch("voice_enabled", "Voice responses", c.voice_enabled)
        switch("wake_word_enabled", "Wake word on startup", c.wake_word_enabled)
        option("confirmation_mode", "Confirmation strictness",
               c.confirmation_mode, ["strict", "normal"])
        entry("push_to_talk_hotkey", "Push-to-talk hotkey", c.push_to_talk_hotkey)

        section("Local AI (Ollama)")
        entry("ollama_url", "Ollama URL", c.ollama_url)
        entry("ollama_model", "Model name", c.ollama_model)

        section("Speech to text")
        option("stt_backend", "STT backend", c.stt_backend,
               ["faster_whisper", "whisper_cpp"])
        entry("faster_whisper_model", "faster-whisper model", c.faster_whisper_model)
        entry("whisper_cpp_exe", "whisper.cpp exe path", c.whisper_cpp_exe)
        entry("whisper_cpp_model", "whisper.cpp model path", c.whisper_cpp_model)

        section("Text to speech")
        option("tts_backend", "TTS backend", c.tts_backend,
               ["auto", "piper", "windows", "off"])
        entry("piper_exe", "Piper exe path", c.piper_exe)
        entry("piper_voice", "Piper voice (.onnx) path", c.piper_voice)

        section("Tools")
        entry("default_browser", "Default browser alias", c.default_browser)
        entry("screenshot_dir", "Screenshot folder", c.screenshot_dir)

        section("Safe folders (one per line)")
        self.folders_box = ctk.CTkTextbox(body, height=90)
        self.folders_box.insert("1.0", "\n".join(c.safe_folders))
        self.folders_box.pack(fill="x")

        section("App aliases (JSON)")
        self.aliases_box = ctk.CTkTextbox(body, height=140)
        self.aliases_box.insert("1.0", json.dumps(c.app_aliases, indent=2))
        self.aliases_box.pack(fill="x")

        section("Memory (memory.json — no secrets!)")
        self.memory_box = ctk.CTkTextbox(body, height=140)
        self.memory_box.insert("1.0", json.dumps(memory.data, indent=2))
        self.memory_box.pack(fill="x")

        self.status_lbl = ctk.CTkLabel(self, text="", text_color="#f87171")
        self.status_lbl.pack()
        ctk.CTkButton(self, text="💾 Save settings", command=self._save
                      ).pack(pady=(0, 12))

    def _save(self) -> None:
        c = self.config_obj
        try:
            for key, widget in self._entries.items():
                value = widget.get()
                current = getattr(c, key)
                if isinstance(current, bool):
                    value = bool(value)
                elif isinstance(current, int):
                    value = int(value)
                elif isinstance(current, float):
                    value = float(value)
                setattr(c, key, value)
            for key, s in self._switches.items():
                setattr(c, key, bool(s.get()))
            folders = [ln.strip() for ln in
                       self.folders_box.get("1.0", "end").splitlines() if ln.strip()]
            c.safe_folders = folders
            aliases = json.loads(self.aliases_box.get("1.0", "end"))
            if not isinstance(aliases, dict):
                raise ValueError("App aliases must be a JSON object.")
            c.app_aliases = {str(k): str(v) for k, v in aliases.items()}
            mem = json.loads(self.memory_box.get("1.0", "end"))
            if not isinstance(mem, dict):
                raise ValueError("Memory must be a JSON object.")
        except (ValueError, json.JSONDecodeError) as e:
            self.status_lbl.configure(text=f"Not saved: {e}")
            return

        c.save()
        self.memory.replace(mem)
        if self.on_saved:
            self.on_saved()
        self.destroy()
