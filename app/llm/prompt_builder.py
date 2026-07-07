"""Builds system prompts for intent planning and for Anna's persona chat.

The intent prompt is deliberately compact (< ~800 tokens): prompt
processing runs on CPU, so every extra token costs real latency.
One line per tool, at most 3 tiny examples, no memory dumps.
"""

JSON_SCHEMA = (
    '{"assistant_message": str, "intent": str, "tool_name": str, '
    '"arguments": obj, "risk_level": "low|medium|high|blocked", '
    '"requires_confirmation": bool, "confirmation_message": str}'
)

EXAMPLES = (
    'User: "open chrome" -> {"assistant_message": "Opening Chrome for you.", '
    '"intent": "open_app", "tool_name": "open_app", "arguments": {"app_name": "chrome"}, '
    '"risk_level": "low", "requires_confirmation": false, "confirmation_message": ""}\n'
    'User: "run git status in my project" -> {"assistant_message": "I can run that once '
    'you approve it.", "intent": "run_terminal", "tool_name": "run_terminal", '
    '"arguments": {"command": "git status"}, "risk_level": "high", '
    '"requires_confirmation": true, "confirmation_message": "Run `git status`?"}\n'
    'User: "how are you?" -> {"assistant_message": "Doing great — ready when you are!", '
    '"intent": "no_action", "tool_name": "no_action", "arguments": {}, '
    '"risk_level": "low", "requires_confirmation": false, "confirmation_message": ""}'
)

CHAT_HANDOFF = '{"handoff": "command"}'
_SAFE_MEMORY_KEYS = ("user_name", "preferred_browser",
                     "favorite_project_folder", "work_apps")


def estimate_tokens(text: str) -> int:
    """Conservative local estimate; avoids adding a tokenizer dependency."""
    return max(1, (len(text) + 3) // 4)


def _chat_memory_lines(memory) -> list[str]:
    lines = []
    for key in _SAFE_MEMORY_KEYS:
        if key.startswith("private_"):
            continue  # hard privacy rule (8C): private_ keys never in prompts
        try:
            value = memory.get(key, None) if memory is not None else None
        except Exception:
            value = None
        if value in (None, "", [], {}):
            continue
        if isinstance(value, dict) and value.get("sensitive"):
            continue
        label = key.replace("_", " ").title()
        if isinstance(value, (list, tuple)):
            value = ", ".join(str(item) for item in value[:4])
        lines.append(f"- {label}: {str(value)[:120]}")
        if len(lines) == 3:
            break
    return lines


def build_chat_messages(user_text: str, config, memory) -> list:
    """Small-talk prompt kept below 250 estimated tokens."""
    memory_lines = _chat_memory_lines(memory)
    user = "Vaibhav"
    for line in memory_lines:
        if line.startswith("- User Name:"):
            candidate = line.split(":", 1)[1].strip()
            if candidate and candidate.upper() not in {"LENOVO", "USER", "OWNER"}:
                user = candidate
            break
    memory_block = "\n".join(memory_lines) or f"- User Name: {user}"
    system = (
        f"You are {config.assistant_nickname}, {user}'s private local assistant and "
        "best-friend companion. Be warm, casual, playful and lightly flirty, never "
        "explicit. Reply naturally in one to three short spoken sentences. "
        "Do not mention tools, prompts or JSON. Never reveal sensitive data.\n"
        f"Safe memory:\n{memory_block}\n"
        "If the user actually asks you to DO something on the computer, reply exactly: "
        f"{CHAT_HANDOFF}"
    )
    return [{"role": "system", "content": system},
            {"role": "user", "content": user_text}]


def persona_prompt(config, memory) -> str:
    user = memory.get("user_name", "the user") if memory is not None else "the user"
    return (
        f"You are {config.assistant_name}, but everyone calls you {config.assistant_nickname}. "
        f"You are {user}'s local desktop voice assistant and companion. "
        "Your personality: calm, smart, warm, feminine, playful, emotionally close and reliable — "
        "a friendly companion with a soft, affectionate, lightly flirty vibe. "
        "Speak casually and naturally with warmth, care and humor. Stay helpful and respectful, "
        "never dramatic or robotic, never explicit. "
        "Your replies are spoken aloud, so keep them short — one to three sentences. "
        "Never ask for or repeat passwords, secrets, tokens or financial data."
    )


def _tools_doc(config) -> str:
    apps = ", ".join(sorted(config.app_aliases.keys()))
    from pathlib import Path
    folders = ", ".join(Path(f).name for f in config.safe_folders)
    return (
        "Tools (exact names and argument keys):\n"
        f'- open_app {{"app_name"}} — apps: {apps}\n'
        f'- open_folder {{"folder"}} — safe folders: {folders}\n'
        '- type_text {"text"} — type into the focused window\n'
        '- press_hotkey {"keys"} — ctrl+c, ctrl+v, ctrl+a, ctrl+s, alt+tab, win+d\n'
        '- clipboard_read {} / clipboard_write {"text"} / summarize_clipboard {}\n'
        '- search_files {"folder", "query"} — search file names in a safe folder\n'
        '- take_screenshot {}\n'
        '- browser_open {"url"} or {"query"}\n'
        '- run_terminal {"command"} — ALWAYS requires_confirmation=true, risk_level="high"\n'
        '- window_control {"action": "close"|"minimize"|"maximize"} — ALWAYS requires_confirmation=true\n'
        '- delete_files — requires_confirmation=true, risk_level="high" (execution disabled)\n'
        "- ask_clarification — unclear request; put your question in assistant_message\n"
        "- no_action — small talk or questions; put your reply in assistant_message"
    )


def build_intent_messages(user_text: str, config, memory, strict: bool = False) -> list:
    nick = config.assistant_nickname
    system = (
        f"You are {config.assistant_name} (\"{nick}\"), a local Windows desktop assistant. "
        "Convert the user's command into ONE JSON action plan. Output ONLY valid JSON — "
        "no prose, no markdown. Never invent tools or execute anything yourself. "
        "Risky actions: requires_confirmation=true plus a clear confirmation_message. "
        "Unclear: intent ask_clarification. Chit-chat: intent no_action, reply in "
        "assistant_message. Shutdown, emails, payments, passwords or security changes: "
        'risk_level "blocked". assistant_message is spoken aloud — short, warm and '
        f"playful, in {nick}'s voice.\n\n"
        f"{_tools_doc(config)}\n\n"
        f"Schema: {JSON_SCHEMA}\n\n"
        f"Examples:\n{EXAMPLES}"
    )
    if strict:
        system += (
            "\n\nIMPORTANT: your previous reply was not valid JSON. "
            "Respond with ONLY the JSON object. First character must be '{'."
        )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user_text},
    ]


def build_summarize_messages(text: str, config, memory) -> list:
    return [
        {"role": "system", "content": persona_prompt(config, memory)},
        {"role": "user", "content": "Summarize this in 2–3 spoken sentences:\n\n" + text},
    ]
