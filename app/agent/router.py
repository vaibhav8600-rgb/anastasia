"""Intent router — hybrid rule-based matching + local LLM planning."""

import re
from pathlib import Path
from typing import Optional

from app.llm.intent_parser import ActionPlan, parse_action_plan
from app.llm.prompt_builder import build_intent_messages

# Normalized spoken name -> canonical alias key in config.app_aliases
APP_SYNONYMS = {
    "code": "vscode", "visualstudiocode": "vscode", "vsc": "vscode",
    "googlechrome": "chrome", "microsoftedge": "edge",
    "explorer": "file explorer", "files": "file explorer",
    "windowsexplorer": "file explorer", "fileexplorer": "file explorer",
    "calc": "calculator", "windowsterminal": "terminal",
    "cmd": "terminal", "commandprompt": "terminal",
    "microsoftteams": "teams", "msteams": "teams",
    "windowspowershell": "powershell",
    "microsoftpaint": "paint", "paintbrush": "paint",
}

# Spoken folder name -> safe-folder directory name
FOLDER_SYNONYMS = {
    "project": "projects", "project folder": "projects",
    "download": "downloads", "document": "documents", "docs": "documents",
    "picture": "pictures", "photos": "pictures",
}

KNOWN_SITES = {
    "youtube": "https://www.youtube.com",
    "google": "https://www.google.com",
    "gmail": "https://mail.google.com",
    "github": "https://github.com",
    "stack overflow": "https://stackoverflow.com",
    "stackoverflow": "https://stackoverflow.com",
    "chatgpt": "https://chat.openai.com",
    "claude": "https://claude.ai",
}

HOTKEY_PHRASES = {
    "copy": "ctrl+c", "copy this": "ctrl+c", "copy that": "ctrl+c",
    "paste": "ctrl+v", "paste this": "ctrl+v", "paste it": "ctrl+v",
    "paste that": "ctrl+v", "select all": "ctrl+a",
    "save": "ctrl+s", "save this": "ctrl+s", "save file": "ctrl+s",
    "show desktop": "win+d", "switch window": "alt+tab", "switch windows": "alt+tab",
}


def _norm(s: str) -> str:
    return re.sub(r"[\s.\-_]+", "", s.lower())


def clean_command(text: str, config) -> str:
    """Strip wake-name prefixes like 'hey anna,' from the transcript."""
    from app.agent.normalizer import normalize_command
    return normalize_command(text, config).cleaned


def match_rule(raw: str, config, memory=None) -> Optional[ActionPlan]:
    """Fast rule-based intent matching. Returns None to fall back to the LLM."""
    orig = clean_command(raw, config)
    t = orig.lower().strip(" .!?")
    if not t:
        return None

    def plan(intent, args=None, msg="", risk="low", confirm=False, confirm_msg=""):
        return ActionPlan(assistant_message=msg, intent=intent, tool_name=intent,
                          arguments=args or {}, risk_level=risk,
                          requires_confirmation=confirm, confirmation_message=confirm_msg)

    # --- screenshot -------------------------------------------------
    if t == "screenshot" or re.search(r"\b(take|grab|capture)\b.*\bscreenshot\b", t):
        return plan("take_screenshot", msg="Screenshot coming right up.")

    # --- clipboard --------------------------------------------------
    if re.search(r"\bsummari[sz]e\b.*\bclipboard\b", t):
        return plan("summarize_clipboard", msg="Let me read that and sum it up for you.")
    if re.search(r"\b(read|show)\b.*\bclipboard\b", t) or re.search(r"what'?s (in|on) (my |the )?clipboard", t):
        return plan("clipboard_read", msg="Here's what's on your clipboard.")

    # --- hotkey phrases ---------------------------------------------
    if t in HOTKEY_PHRASES:
        keys = HOTKEY_PHRASES[t]
        return plan("press_hotkey", {"keys": keys}, msg=f"Pressing {keys.replace('+', ' ')}.")

    # --- type text (preserve original casing) ------------------------
    m = re.match(r"type\s+(.+)", orig, flags=re.IGNORECASE)
    if m:
        return plan("type_text", {"text": m.group(1)}, msg="Typing that for you.")

    # --- web search ---------------------------------------------------
    m = re.match(r"(?:search (?:google|the web|web|online) for|google)\s+(.+)", t)
    if m:
        return plan("browser_open", {"query": m.group(1)},
                    msg=f"Searching the web for {m.group(1)}.")
    m = re.match(r"(?:search youtube for|youtube)\s+(.+)", t)
    if m:
        import urllib.parse
        url = "https://www.youtube.com/results?search_query=" + \
              urllib.parse.quote_plus(m.group(1))
        return plan("browser_open", {"url": url},
                    msg=f"Searching YouTube for {m.group(1)}.")
    m = re.match(r"open (?:the )?(?:website|site)\s+(\S+)", t)
    if m:
        return plan("browser_open", {"url": m.group(1)}, msg=f"Opening {m.group(1)}.")

    # --- file search --------------------------------------------------
    m = re.match(r"(?:search|look in)(?: my)?\s+([\w ]+?)\s+(?:folder\s+)?for\s+(.+)", t)
    if m:
        return plan("search_files", {"folder": m.group(1).strip(), "query": m.group(2).strip()},
                    msg=f"Looking for {m.group(2).strip()} in {m.group(1).strip()}.")
    m = re.match(r"find\s+(.+?)\s+in(?: my)?\s+([\w ]+?)(?:\s+folder)?$", t)
    if m:
        return plan("search_files", {"folder": m.group(2).strip(), "query": m.group(1).strip()},
                    msg=f"Looking for {m.group(1).strip()} in {m.group(2).strip()}.")

    # --- open <something> ----------------------------------------------
    m = re.match(r"(open|launch|start|run)\s+(.+)", t)
    if m:
        verb, target = m.group(1), m.group(2).strip()
        target = re.sub(r"^(the |my )", "", target).strip()
        bare = target.removesuffix(" folder").strip()
        norm = _norm(bare)

        # app alias? (first occurrence wins: "vscode" and "vs code" collide)
        alias_map = {}
        for k in config.app_aliases:
            alias_map.setdefault(_norm(k), k)
        key = alias_map.get(norm)
        if not key and norm in APP_SYNONYMS:
            key = alias_map.get(_norm(APP_SYNONYMS[norm]))
        if key:
            return plan("open_app", {"app_name": key}, msg=f"Opening {key} for you.")

        # safe folder?
        folder_name = FOLDER_SYNONYMS.get(bare, bare)
        for f in config.safe_folders:
            if Path(f).name.lower() == folder_name:
                return plan("open_folder", {"folder": folder_name},
                            msg=f"Opening your {folder_name} folder.")

        # known site or domain?
        if bare in KNOWN_SITES:
            return plan("browser_open", {"url": KNOWN_SITES[bare]}, msg=f"Opening {bare}.")
        if re.fullmatch(r"[\w\-]+(\.[\w\-]+)+(/\S*)?", target):
            return plan("browser_open", {"url": target}, msg=f"Opening {target}.")

        if verb == "run":
            return None  # "run npm dev" etc. — let the LLM plan a run_terminal action

    return None


class Agent:
    """Plans and executes commands. GUI drives the confirmation flow."""

    def __init__(self, config, memory, history):
        from app.llm.ollama_client import OllamaClient  # lazy: keeps imports light
        self.config = config
        self.memory = memory
        self.history = history
        self.llm = OllamaClient(config)

    # -- planning -----------------------------------------------------
    def plan_rule(self, text: str) -> Optional[ActionPlan]:
        """Instant rule-based routing; never touches the LLM."""
        return match_rule(text, self.config, self.memory)

    def plan_llm(self, text: str) -> ActionPlan:
        """LLM planning with one strict retry on bad JSON."""
        content = self.llm.chat(build_intent_messages(text, self.config, self.memory))
        ap = parse_action_plan(content)
        if ap is None:
            content = self.llm.chat(
                build_intent_messages(text, self.config, self.memory, strict=True))
            ap = parse_action_plan(content)
        if ap is None:
            return ActionPlan(
                assistant_message="Sorry, I didn't quite get that — could you say it again for me?",
                intent="ask_clarification", tool_name="ask_clarification")
        return ap

    def plan(self, raw_text: str) -> ActionPlan:
        """Rule-based first; LLM fallback (kept for compatibility)."""
        ruled = self.plan_rule(raw_text)
        if ruled is not None:
            return ruled
        return self.plan_llm(clean_command(raw_text, self.config))

    # -- execution ------------------------------------------------------
    def execute(self, plan: ActionPlan):
        from app.tools import ToolContext, ToolResult, run_tool
        if plan.intent in ("ask_clarification", "no_action"):
            return ToolResult(True, plan.assistant_message or "Okay.")
        ctx = ToolContext(config=self.config, memory=self.memory, llm=self.llm)
        return run_tool(plan.tool_name or plan.intent, plan.arguments, ctx)
