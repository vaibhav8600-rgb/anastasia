"""Intent router — hybrid rule-based matching + local LLM planning."""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.llm.intent_parser import ActionPlan, parse_action_plan
from app.llm.prompt_builder import (
    CHAT_HANDOFF, build_chat_messages, build_intent_messages,
)

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


_MONITOR_WORDS = {"one": 1, "first": 1, "primary": 1, "main": 1,
                  "two": 2, "second": 2, "three": 3, "third": 3}
_MONITOR_NUM = r"(\d+|one|two|three|first|second|third|primary|main)"


def _monitor_number(t: str) -> int:
    """'screen two' / 'monitor 1' / 'second display' -> 2 / 1 / 2. 0 = none."""
    match = re.search(rf"\b(?:screen|monitor|display)\s*(?:number\s*)?{_MONITOR_NUM}\b", t) \
        or re.search(rf"\b{_MONITOR_NUM}\s+(?:screen|monitor|display)\b", t)
    if not match:
        return 0
    word = match.group(1)
    return int(word) if word.isdigit() else _MONITOR_WORDS.get(word, 0)


def _norm(s: str) -> str:
    return re.sub(r"[\s.\-_]+", "", s.lower())


def clean_command(text: str, config) -> str:
    """Strip wake-name prefixes like 'hey anna,' from the transcript."""
    from app.agent.normalizer import normalize_command
    return normalize_command(text, config).cleaned


_COMMAND_WORDS = (
    "open", "close", "launch", "start", "run", "type", "copy", "paste",
    "search", "find", "take", "capture", "read", "show", "summarize",
    "minimize", "maximize", "switch", "save", "google", "youtube",
)
_CHAT_OPENERS = (
    "who", "what", "why", "when", "where", "how", "are you", "do you",
    "did you", "would you", "could you", "tell me", "explain", "hello",
    "hi", "hey", "thanks", "thank you", "good morning", "good evening",
)


def classify_input_mode(raw: str, config=None) -> str:
    """Cheap, deterministic classifier. Ambiguity deliberately means command."""
    text = clean_command(raw, config) if config is not None else raw
    text = (text or "").lower().strip()
    normalized = text.strip(" .!?")
    leading_word = re.split(r"[^\w']+", normalized, maxsplit=1)[0] if normalized else ""
    if not text:
        return "command"
    if leading_word in _COMMAND_WORDS:
        return "command"
    if re.search(r"\b(?:open|close|launch|run|type|copy|paste|minimize|maximize)\b",
                 normalized):
        return "command"
    if any(re.match(rf"^{re.escape(opener)}(?:$|[^\w]+)", normalized)
           for opener in _CHAT_OPENERS):
        return "chat"
    if normalized in {"okay", "ok", "yes", "no", "maybe", "nice", "cool"}:
        return "chat"
    return "command"


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
    if re.search(r"\bscreenshot\b", t) and (
            t == "screenshot" or re.search(r"\b(take|grab|capture)\b", t)
            or re.match(r"screenshot\b", t)):
        # optional monitor target: "screenshot of screen 2" / "monitor 1" /
        # "second screen" / "primary monitor"
        args = {}
        mapping = {"one": 1, "first": 1, "primary": 1, "main": 1,
                   "two": 2, "second": 2, "three": 3, "third": 3}
        num = r"(\d+|one|two|three|first|second|third|primary|main)"
        m = re.search(rf"\b(?:screen|monitor|display)\s*(?:number\s*)?{num}\b", t) \
            or re.search(rf"\b{num}\s+(?:screen|monitor|display)\b", t)
        if m:
            word = m.group(1)
            args["screen"] = int(word) if word.isdigit() else mapping.get(word, 0)
        elif re.search(r"\ball\b.*\bscreens?\b|\bboth\b.*\bscreens?\b", t):
            args["screen"] = 0   # explicit all-screens
        return plan("take_screenshot", args=args, msg="Screenshot coming right up.")

    # --- vision (11B) — capture happens ONLY on these explicit triggers ---
    if re.search(r"\bprivacy mode\b", t):
        return plan("privacy_mode", msg="Privacy mode.")
    if re.search(r"\bstop (looking|watching)\b", t) \
            or re.search(r"\bturn off (the )?(screen vision|screen watching)\b", t) \
            or re.search(r"\bstop (the )?screen vision\b", t):
        return plan("stop_screen_watch", msg="Okay, I'll stop looking.")
    # "anyway" = the user overriding a sensitive-content refusal; the safety
    # validator turns that into a confirmation (never a silent bypass).
    anyway = bool(re.search(r"\banyway\b|\bgo ahead and look\b", t))
    extra = {"allow_sensitive": True} if anyway else {}
    # Naming the screen must ALWAYS beat the bare "what do you see" camera
    # phrase: "what do you see on the screen" opened the webcam otherwise.
    means_screen = re.search(r"\b(screen|monitor|display|desktop|window|page|tab)\b", t)
    means_camera = re.search(r"\b(camera|webcam)\b", t)
    if (means_camera and re.search(r"\b(look|see|through|use|open|check|show)\b", t)) \
            or (re.match(r"what do you see\b", t) and not means_screen):
        return plan("camera_look", args=dict(extra),
                    msg="Taking a quick look through the camera.")
    if re.search(r"\b(watch|keep an eye on)\b.*\bscreen\b", t) \
            or re.search(r"\bstart (the )?screen (vision|watching)\b", t):
        return plan("start_screen_watch", msg="Watching your screen.")
    if re.search(r"\b(analy[sz]e|read|look at|describe)\b.*\b(this|the|active) window\b", t):
        return plan("active_window_capture", args=dict(extra),
                    msg="Looking at that window.")
    if re.search(r"\bunder (my |the )?(mouse|cursor)\b", t) \
            or re.search(r"\b(look at|read)\b.*\b(my |the )?cursor\b", t):
        return plan("region_capture", args=dict(extra),
                    msg="Looking around your cursor.")
    if re.search(r"\b(look at|check|describe)\b\s+(my |the )?screen\b", t) \
            or re.search(r"\bwhat'?s on (my |the )?screen\b", t) \
            or re.search(r"\bwhat is on (my |the )?screen\b", t) \
            or re.search(r"\bcan you see (my |the )?screen\b", t) \
            or (re.search(r"\bwhat do you see\b", t) and means_screen) \
            or re.search(r"\bread (this|the) error\b", t) \
            or re.search(r"\bsummari[sz]e (this|the) page\b", t):
        args = dict(extra)
        target = _monitor_number(t)     # "…on screen two" -> just that monitor
        if target:
            args["screen"] = target
        return plan("look_at_screen", args=args,
                    msg="Taking a look at your screen.")

    # --- email (11E): "email <who> saying/that <body>" -> open a draft ---
    m = re.match(r"(?:email|e-?mail|write (?:an? )?email to|send (?:an? )?email to)\s+"
                 r"(.+?)(?:\s+(?:saying|that says?|and say|about|with subject|:)\s+(.+))?$",
                 orig, flags=re.IGNORECASE)
    if m:
        who = m.group(1).strip().rstrip(",")
        rest = (m.group(2) or "").strip()
        args = {"to": who}
        if rest:
            args["body"] = rest
        return plan("compose_email", args=args,
                    msg=f"Opening an email draft to {who}.")

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
    m = re.match(r"open (?:the )?youtube (?:and )?search(?: for)?\s+(.+)", t)
    if m:
        import urllib.parse
        query = m.group(1).strip()
        url = "https://www.youtube.com/results?search_query=" + \
              urllib.parse.quote_plus(query)
        return plan("browser_open", {"url": url},
                    msg=f"Searching YouTube for {query}.")
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

    # --- window control (always confirmed by safety.py) ---------------
    m = re.fullmatch(r"(close|minimize|maximize)(?: (?:this|the))? window", t)
    if not m:
        m = re.fullmatch(r"(close|minimize|maximize) this", t)
    if m:
        action = m.group(1)
        return plan("window_control", {"action": action}, risk="medium",
                    confirm=True, confirm_msg=f"{action.title()} the active window?")
    m = re.fullmatch(r"close\s+(.+)", t)
    if m:
        target = re.sub(r"^(the )", "", m.group(1)).strip()
        alias_map = {_norm(key): key for key in config.app_aliases}
        key = alias_map.get(_norm(target))
        if key:
            return plan("window_control", {"action": "close", "app": key},
                        risk="medium", confirm=True,
                        confirm_msg=f"Close {key.title()}?")

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


@dataclass(frozen=True)
class FuzzyMatch:
    plan: ActionPlan
    score: float
    heard_target: str
    matched_target: str

    @property
    def needs_confirmation(self) -> bool:
        return 65 <= self.score < 85


def _fuzzy_score(query: str, candidate: str) -> float:
    """RapidFuzz score with a conservative boost for short STT homophones."""
    try:
        from rapidfuzz import fuzz
        ratio = float(fuzz.ratio(query, candidate))
        partial = float(fuzz.partial_ratio(query, candidate))
    except ImportError:  # keeps typed/rule commands usable before dependencies install
        from difflib import SequenceMatcher
        ratio = partial = SequenceMatcher(None, query, candidate).ratio() * 100
    score = max(ratio, partial * 0.9)
    shared = len(set(query) & set(candidate))
    if (" " not in query and " " not in candidate
            and len(query) >= 4 and len(candidate) >= 4 and query[0] == candidate[0]
            and abs(len(query) - len(candidate)) <= 1 and shared >= 3):
        score = max(score, 72.0)
    return min(score, 100.0)


def _display_target(target: str) -> str:
    special = {"vscode": "VS Code", "vs code": "VS Code", "mspaint": "Paint"}
    return special.get(target, target.title())


def match_fuzzy_command(raw: str, config) -> Optional[FuzzyMatch]:
    """Recover short command-shaped STT mistakes between rules and the LLM."""
    text = clean_command(raw, config).lower().strip(" .!?")
    match = re.fullmatch(r"(\S+)\s+(.+)", text)
    if not match:
        return None
    heard_verb, heard_target = match.groups()
    heard_target = re.sub(r"^(?:the|my)\s+", "", heard_target).strip()
    if len(heard_target.split()) > 4:
        return None

    verbs = ("open", "launch", "start", "run", "close", "minimize", "maximize")
    verb = max(verbs, key=lambda item: _fuzzy_score(heard_verb, item))
    verb_score = _fuzzy_score(heard_verb, verb)
    if verb_score < 65:
        return None

    candidates = []
    seen = set()
    if verb in ("open", "launch", "start", "run", "close"):
        for key in config.app_aliases:
            normalized = _norm(key)
            if normalized not in seen:
                candidates.append(("app", key))
                seen.add(normalized)
    if verb in ("open", "launch", "start", "run"):
        for folder in config.safe_folders:
            name = Path(folder).name.lower()
            if _norm(name) not in seen:
                candidates.append(("folder", name))
                seen.add(_norm(name))
        for site in KNOWN_SITES:
            if _norm(site) not in seen:
                candidates.append(("site", site))
                seen.add(_norm(site))
    if not candidates:
        return None

    kind, target = max(candidates,
                       key=lambda item: _fuzzy_score(heard_target, item[1]))
    target_score = _fuzzy_score(heard_target, target)
    score = min(verb_score, target_score)
    if score < 65:
        return None

    display = _display_target(target)
    if verb == "close":
        plan = ActionPlan(
            assistant_message=f"Closing {display}.", intent="window_control",
            tool_name="window_control", arguments={"action": "close", "app": target},
            risk_level="medium", requires_confirmation=True,
            confirmation_message=f"Close {display}?",
        )
    elif kind == "app":
        plan = ActionPlan(assistant_message=f"Opening {display} for you.",
                          intent="open_app", tool_name="open_app",
                          arguments={"app_name": target})
    elif kind == "folder":
        plan = ActionPlan(assistant_message=f"Opening your {display} folder.",
                          intent="open_folder", tool_name="open_folder",
                          arguments={"folder": target})
    else:
        plan = ActionPlan(assistant_message=f"Opening {display}.",
                          intent="browser_open", tool_name="browser_open",
                          arguments={"url": KNOWN_SITES[target]})
    return FuzzyMatch(plan=plan, score=score, heard_target=heard_target,
                      matched_target=display)


class Agent:
    """Plans and executes commands. GUI drives the confirmation flow."""

    def __init__(self, config, memory, history):
        from app.llm.ollama_client import OllamaClient  # lazy: keeps imports light
        self.config = config
        self.memory = memory
        self.history = history
        self.llm = OllamaClient(config)
        from app.llm.providers import BrainRouter
        self.brain = BrainRouter(config, lambda: self.llm)
        self.recent_chat_turns = None   # set by controller -> conversation reader
        self.vision = None              # VisionService (11B), set by controller

    # -- planning -----------------------------------------------------
    def plan_rule(self, text: str) -> Optional[ActionPlan]:
        """Instant rule-based routing; never touches the LLM."""
        return match_rule(text, self.config, self.memory)

    def plan_fuzzy(self, text: str) -> Optional[FuzzyMatch]:
        return match_fuzzy_command(text, self.config)

    def plan_llm(self, text: str) -> ActionPlan:
        """LLM planning with one strict retry on bad JSON. The brain router
        picks Groq or Ollama; the plan still passes local safety either way."""
        result = self.brain.complete(
            "command", build_intent_messages(text, self.config, self.memory))
        ap = parse_action_plan(result.text)
        if ap is None:
            result = self.brain.complete(
                "command",
                build_intent_messages(text, self.config, self.memory, strict=True))
            ap = parse_action_plan(result.text)
        if ap is None:
            return ActionPlan(
                assistant_message="Sorry, I didn't quite get that — could you say it again for me?",
                intent="ask_clarification", tool_name="ask_clarification")
        return ap

    def plan_chat(self, text: str) -> tuple[ActionPlan, bool]:
        """Fast plain-text chat; command handoff re-enters structured planning."""
        chat_model = self.config.chat_model or self.config.ollama_model
        # Conversational continuity (8D): 10 turns on the cloud brain (70B
        # handles it, ~free on Groq), a smaller window on the local fallback.
        cloud = self.brain.mode() == "hybrid" and not self.brain.circuit_open()
        max_turns = 10 if cloud else 4
        turns = []
        if callable(self.recent_chat_turns):
            try:
                turns = self.recent_chat_turns(max_turns)
            except Exception:
                turns = []
        result = self.brain.complete(
            "chat",
            build_chat_messages(text, self.config, self.memory, history_turns=turns),
            model=chat_model)   # model applies to the local fallback only
        content = result.text.strip()
        normalized = content.strip("` \r\n")
        if normalized.startswith("json\n"):
            normalized = normalized[5:].strip()
        if normalized == CHAT_HANDOFF:
            return self.plan_llm(text), True
        if not content:
            content = "I'm here — try me once more?"
        return ActionPlan(assistant_message=content, intent="no_action",
                          tool_name="no_action"), False

    def plan_chat_stream(self, text: str, on_sentence, should_abort=None
                         ) -> tuple[ActionPlan, bool]:
        """Streamed chat (9B): emits complete sentences to on_sentence as the
        brain generates them, so TTS can start on sentence 1. Returns the same
        (ActionPlan, was_handoff) contract as plan_chat. The handoff sentinel
        is held back (never spoken); if the whole reply is the sentinel we
        re-enter structured command planning."""
        from app.voice import StreamingSentencer
        chat_model = self.config.chat_model or self.config.ollama_model
        cloud = self.brain.mode() == "hybrid" and not self.brain.circuit_open()
        turns = []
        if callable(self.recent_chat_turns):
            try:
                turns = self.recent_chat_turns(10 if cloud else 4)
            except Exception:
                turns = []

        sentencer = StreamingSentencer()
        acc = {"text": "", "handoff_ruled_out": False, "emitted": False}

        def handoff_possible(buf: str) -> bool:
            stripped = buf.strip().strip("`").lstrip("json").strip()
            return CHAT_HANDOFF.startswith(stripped[:len(CHAT_HANDOFF)]) \
                and len(stripped) <= len(CHAT_HANDOFF)

        def on_token(delta: str):
            acc["text"] += delta
            # Hold all emission until we can rule out the handoff sentinel
            # (it arrives as the very first tokens if present).
            if not acc["handoff_ruled_out"]:
                if handoff_possible(acc["text"]):
                    return
                acc["handoff_ruled_out"] = True
            for sentence in sentencer.feed(delta):
                acc["emitted"] = True
                on_sentence(sentence)

        result = self.brain.stream_chat(
            build_chat_messages(text, self.config, self.memory, history_turns=turns),
            on_token=on_token, should_abort=should_abort, model=chat_model)

        content = result.text.strip()
        normalized = content.strip("` \r\n")
        if normalized.startswith("json\n"):
            normalized = normalized[5:].strip()
        if normalized == CHAT_HANDOFF:
            return self.plan_llm(text), True
        # flush any trailing partial sentence that never hit a boundary
        if acc["handoff_ruled_out"] and not (should_abort and should_abort()):
            tail = sentencer.flush()
            if tail:
                on_sentence(tail)
        if not content:
            content = "I'm here — try me once more?"
        return ActionPlan(assistant_message=content, intent="no_action",
                          tool_name="no_action"), False

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
        ctx = ToolContext(config=self.config, memory=self.memory, llm=self.llm,
                          brain=self.brain, vision=self.vision)
        return run_tool(plan.tool_name or plan.intent, plan.arguments, ctx)
