"""Command normalization — cleans raw text/STT transcripts before routing.

Responsibilities (section 6 of the spec):
  * strip wake-word prefixes ("anna", "hey anna", "anastasia", ...)
  * trim, collapse whitespace, drop trailing punctuation/filler
  * fix common STT mistakes ("note pad" -> "notepad")
  * split accidental multi-sentence STT output into candidate sentences
  * flag Whisper hallucinations / garbled transcripts so they never
    reach Ollama blindly

Casing is preserved in the cleaned text — "type Hello World" must keep
its capitals; the router lowercases internally for matching.
"""

import re
from dataclasses import dataclass, field

# Phrases Whisper hallucinates from silence/noise — treat as empty input.
WHISPER_HALLUCINATIONS = {
    "you", "bye", "so", "uh", "um", "the", "thank you", "thanks",
    "thank you for watching", "thanks for watching",
    "thank you so much for watching", "subscribe", "please subscribe",
}

# Common STT mishearings -> canonical command words (word-boundary regex).
STT_FIXES = [
    (r"\bnote\s*pad\b", "notepad"),
    (r"\bnotepat\b", "notepad"),
    (r"\bscreen\s*shot\b", "screenshot"),
    (r"\bpower\s*shell\b", "powershell"),
    (r"\bemma?\s+s\s+paint\b", "ms paint"),
    (r"\bwhat'?s\s*app\b", "whatsapp"),
    (r"\byou\s*tube\b", "youtube"),
]

# Trailing filler phrases that carry no meaning for routing.
TRAILING_FILLER = re.compile(
    r"(?:[\s,]+(?:please|for me|for us|now|right now|thanks|thank you|okay|ok))+[\s.!?]*$",
    re.IGNORECASE)

# Verbs that signal "this was meant as a computer command".
COMMAND_VERBS = ("open", "launch", "start", "close")


@dataclass
class NormalizedCommand:
    raw: str                                   # untouched input (devlog only)
    cleaned: str                               # normalized, case preserved
    sentences: list = field(default_factory=list)  # cleaned, split candidates

    @property
    def empty(self) -> bool:
        return not self.cleaned


def _strip_wake_words(text: str, config) -> str:
    names = {config.assistant_name.lower(), config.assistant_nickname.lower(),
             "anna", "anastasia", "jarvis"}
    alt = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True) if n)
    pattern = rf"^(?:(?:hey|ok|okay)\s+)?(?:{alt})[\s,.:!\-]+"
    prev = None
    while prev != text:  # "hey anna, anna, open paint" -> strip repeatedly
        prev = text
        text = re.sub(pattern, "", text.strip(), flags=re.IGNORECASE).strip()
    return text


def _apply_stt_fixes(text: str) -> str:
    for pattern, repl in STT_FIXES:
        text = re.sub(pattern, repl, text, flags=re.IGNORECASE)
    return text


def _clean_sentence(s: str) -> str:
    s = TRAILING_FILLER.sub("", s.strip())
    s = re.sub(r"\s+", " ", s).strip(" \t,;")
    return s.rstrip(".!?").strip()


def normalize_command(text: str, config) -> NormalizedCommand:
    raw = text or ""
    t = re.sub(r"\s+", " ", raw).strip()
    t = _strip_wake_words(t, config)
    t = _apply_stt_fixes(t)

    sentences = [c for c in (_clean_sentence(s)
                             for s in re.split(r"(?<=[.?!])\s+", t)) if c]
    cleaned = _clean_sentence(t)

    if cleaned.lower().strip(" .!?") in WHISPER_HALLUCINATIONS:
        return NormalizedCommand(raw=raw, cleaned="", sentences=[])
    return NormalizedCommand(raw=raw, cleaned=cleaned, sentences=sentences)


def looks_garbled(cleaned: str) -> bool:
    """Heuristic for STT garble: starts like a command ("open ...") but the
    router matched nothing — e.g. "open no pass for you". Voice input only;
    the pipeline asks for clarification instead of calling the LLM."""
    words = cleaned.lower().split()
    return bool(words) and words[0] in COMMAND_VERBS
