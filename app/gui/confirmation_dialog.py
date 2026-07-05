"""Inline confirmation panel — shown when an action needs user approval."""

import json

import customtkinter as ctk

_RISK_COLORS = {"low": "#22c55e", "medium": "#eab308",
                "high": "#f97316", "blocked": "#ef4444"}


class ConfirmationPanel(ctk.CTkFrame):
    def __init__(self, master, **kwargs):
        super().__init__(master, corner_radius=12, border_width=2,
                         border_color="#f97316", **kwargs)

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=14, pady=(10, 2))
        ctk.CTkLabel(header, text="⚠ Confirmation needed",
                     font=ctk.CTkFont(size=15, weight="bold")).pack(side="left")
        self.risk_badge = ctk.CTkLabel(header, text="RISK: HIGH", corner_radius=6,
                                       fg_color="#f97316", text_color="#111",
                                       font=ctk.CTkFont(size=11, weight="bold"),
                                       padx=8)
        self.risk_badge.pack(side="right")

        self.command_lbl = ctk.CTkLabel(self, text="", anchor="w", justify="left",
                                        wraplength=560, font=ctk.CTkFont(size=12))
        self.command_lbl.pack(fill="x", padx=14)
        self.action_lbl = ctk.CTkLabel(self, text="", anchor="w", justify="left",
                                       wraplength=560, text_color="#cbd5e1",
                                       font=ctk.CTkFont(size=12, family="Consolas"))
        self.action_lbl.pack(fill="x", padx=14, pady=(2, 0))
        self.message_lbl = ctk.CTkLabel(self, text="", anchor="w", justify="left",
                                        wraplength=560, text_color="#fbbf24",
                                        font=ctk.CTkFont(size=12))
        self.message_lbl.pack(fill="x", padx=14, pady=(4, 0))

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.pack(fill="x", padx=14, pady=10)
        self.approve_btn = ctk.CTkButton(btns, text="✔ Approve", width=120,
                                         fg_color="#16a34a", hover_color="#15803d")
        self.approve_btn.pack(side="left", padx=(0, 8))
        self.cancel_btn = ctk.CTkButton(btns, text="✖ Cancel", width=120,
                                        fg_color="#dc2626", hover_color="#b91c1c")
        self.cancel_btn.pack(side="left", padx=(0, 8))
        self.voice_btn = ctk.CTkButton(btns, text="🎤 Say approve / cancel",
                                       width=180, fg_color="#334155",
                                       hover_color="#1e293b")
        self.voice_btn.pack(side="left")

    def show(self, command: str, plan, safety, on_approve, on_cancel, on_voice=None):
        risk = safety.risk_level.upper()
        self.risk_badge.configure(text=f"RISK: {risk}",
                                  fg_color=_RISK_COLORS.get(safety.risk_level, "#f97316"))
        self.command_lbl.configure(text=f"You said: “{command}”")
        args = json.dumps(plan.arguments or {}, ensure_ascii=False)
        self.action_lbl.configure(text=f"Planned action: {plan.tool_name}  {args}")
        msg = plan.confirmation_message or safety.reason or "Do you want me to go ahead?"
        self.message_lbl.configure(text=msg)
        self.approve_btn.configure(command=on_approve)
        self.cancel_btn.configure(command=on_cancel)
        if on_voice:
            self.voice_btn.configure(command=on_voice, state="normal")
        else:
            self.voice_btn.configure(state="disabled")
        self.pack(fill="x", padx=16, pady=(0, 8))

    def hide(self) -> None:
        self.pack_forget()
