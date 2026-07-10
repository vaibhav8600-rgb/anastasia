"""LLM provider abstraction + hybrid brain routing (Phase 8A).

GroqProvider  — cloud brain (OpenAI-compatible API, llama-3.3-70b class).
OllamaProvider — local brain, wraps the existing OllamaClient unchanged.
BrainRouter   — owns provider selection, failover and a circuit breaker.

Architecture principles enforced here:
  * Any provider only ever returns text/JSON — untrusted input that still
    passes the LOCAL safety validator downstream. No elevated privileges.
  * The Groq API key is read from env GROQ_API_KEY (wins) or config; it is
    never logged and never leaves this module except as an auth header.
  * Rules never reach this module at all (router handles them first).
"""

import json
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum

import requests

from app.agent.devlog import devlog
from app.llm.ollama_client import OllamaError


class DataClass(Enum):
    """Privacy classification of everything inside an LLM payload (8C).
    The cloud gate is enforced in the provider, not by caller convention."""

    TRANSCRIPT = "transcript"          # cloud-allowed in hybrid mode
    CHAT_CONTEXT = "chat_context"      # cloud-allowed (recent turns, memory)
    CLIPBOARD = "clipboard"            # cloud only with explicit opt-in
    FILE_CONTENT = "file_content"      # NEVER cloud
    SCREENSHOT = "screenshot"          # NEVER cloud
    AUDIO = "audio"                    # NEVER cloud (batch recording)
    LIVE_AUDIO_STREAM = "live_audio_stream"  # Deepgram STT only, streaming mode
    # Continuous bidirectional mic audio to Google (Phase 10D) — the largest
    # privacy step in the project. Gemini Live sessions only, opt-in only;
    # see live_audio_allowed() for the hard gate.
    LIVE_AUDIO_BIDIRECTIONAL = "live_audio_bidirectional"
    CAMERA = "camera"                  # webcam frames (Phase 11B)


NEVER_CLOUD = {DataClass.FILE_CONTENT, DataClass.SCREENSHOT, DataClass.AUDIO,
               DataClass.LIVE_AUDIO_STREAM, DataClass.CAMERA,
               DataClass.LIVE_AUDIO_BIDIRECTIONAL}  # never to the BRAIN


class PrivacyViolation(Exception):
    """A never-cloud payload class reached the cloud provider."""


def cloud_allowed(payload_classes, config) -> tuple[bool, str]:
    """(allowed, reason-if-not). Used by the router to route locally and by
    the Groq provider as a hard gate."""
    classes = set(payload_classes or ())
    blocked = classes & NEVER_CLOUD
    if blocked:
        names = ", ".join(sorted(c.value for c in blocked))
        return False, f"{names} never leaves this PC"
    if DataClass.CLIPBOARD in classes and \
            not getattr(config, "allow_clipboard_to_cloud", False):
        return False, "clipboard kept local (opt-in is off)"
    return True, ""


def vision_cloud_allowed(config) -> tuple[bool, str]:
    """11B.4 hard gate for the ONLY way a screen/camera frame may leave this
    machine: an explicit, separate cloud-vision consent toggle (off by
    default). Local OCR and heuristics need no consent — they never leave.
    SCREENSHOT/CAMERA stay in NEVER_CLOUD for the text brain regardless."""
    if not getattr(config, "cloud_vision_consent", False):
        return False, "cloud vision consent is off — frames stay on this PC"
    return True, ""


def live_audio_allowed(config) -> tuple[bool, str]:
    """10D hard gate for LIVE_AUDIO_BIDIRECTIONAL's ONLY consumer: a Gemini
    Live session may stream continuous mic audio to Google exclusively when
    the user picked the engine AND set the explicit billing/privacy opt-in.
    Everything else in the app treats this class as never-cloud."""
    if getattr(config, "engine_mode", "pipeline") != "gemini_live":
        return False, "engine_mode is not gemini_live"
    if not getattr(config, "live_audio_consent", False):
        return False, "the continuous-audio consent is off"
    return True, ""

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
CIRCUIT_FAILURES = 3          # consecutive failures that open the circuit
CIRCUIT_COOLDOWN_S = 120.0    # stay open this long, then probe

# Per-kind generation parameters. The 70B model gets the SAME slim prompts;
# only token caps / temperature differ per provider.
KIND_PARAMS = {
    "command": {"json_mode": True, "temperature": 0.1,
                "groq_max_tokens": 300, "ollama_num_predict": None},
    "chat":    {"json_mode": False, "temperature": 0.7,
                "groq_max_tokens": 150, "ollama_num_predict": 100},
}


class BrainUnavailable(OllamaError):
    """Both brains failed. Message is user-facing and honest."""


@dataclass
class LLMResult:
    text: str = ""
    provider: str = ""
    latency_ms: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    error: str = ""       # "" | timeout | auth | rate_limit | network | bad_response
    error_detail: str = ""
    failover: bool = False
    first_token_ms: float = 0.0   # streaming: time to first token (9B)
    aborted: bool = False         # streaming: cancelled by barge-in

    @property
    def ok(self) -> bool:
        return not self.error


def mask_key(key: str) -> str:
    """Display form only: 'gsk_...abcd'. Never reveals the middle."""
    key = key or ""
    if len(key) < 9:
        return "•••" if key else ""
    return f"{key[:4]}...{key[-4:]}"


class GroqProvider:
    name = "groq"

    def __init__(self, config):
        self.config = config

    def api_key(self) -> str:
        return os.environ.get("GROQ_API_KEY") or \
            getattr(self.config, "groq_api_key", "") or ""

    def configured(self) -> bool:
        return bool(self.api_key())

    def health_check(self) -> bool:
        try:
            r = requests.get("https://api.groq.com/openai/v1/models",
                             headers={"Authorization": f"Bearer {self.api_key()}"},
                             timeout=5)
            return r.status_code == 200
        except requests.RequestException:
            return False

    def complete(self, messages, *, json_mode: bool, max_tokens: int,
                 temperature: float, timeout_s: float,
                 payload_classes=None) -> LLMResult:
        allowed, reason = cloud_allowed(payload_classes, self.config)
        if not allowed:
            # Hard gate (spec 8C.1) — never a caller convention.
            raise PrivacyViolation(reason)
        payload = {
            "model": self.config.cloud_model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        started = time.perf_counter()
        try:
            r = requests.post(
                GROQ_URL, json=payload, timeout=(3.05, timeout_s),
                headers={"Authorization": f"Bearer {self.api_key()}"})
        except requests.exceptions.Timeout:
            return LLMResult(provider=self.name, error="timeout",
                             error_detail=f"no answer within {timeout_s:.0f}s")
        except requests.RequestException as e:
            return LLMResult(provider=self.name, error="network",
                             error_detail=type(e).__name__)

        elapsed = (time.perf_counter() - started) * 1000
        if r.status_code in (401, 403):
            return LLMResult(provider=self.name, latency_ms=elapsed,
                             error="auth", error_detail="key rejected")
        if r.status_code == 429:
            retry_after = r.headers.get("retry-after", "?")
            return LLMResult(provider=self.name, latency_ms=elapsed,
                             error="rate_limit",
                             error_detail=f"retry-after={retry_after}s")
        if r.status_code >= 400:
            return LLMResult(provider=self.name, latency_ms=elapsed,
                             error="bad_response",
                             error_detail=f"HTTP {r.status_code}")
        try:
            body = r.json()
            text = body["choices"][0]["message"]["content"] or ""
            usage = body.get("usage", {})
        except (ValueError, KeyError, IndexError, TypeError):
            return LLMResult(provider=self.name, latency_ms=elapsed,
                             error="bad_response", error_detail="unparseable body")
        return LLMResult(text=text, provider=self.name, latency_ms=elapsed,
                         prompt_tokens=usage.get("prompt_tokens", 0),
                         completion_tokens=usage.get("completion_tokens", 0))

    def complete_stream(self, messages, *, max_tokens: int, temperature: float,
                        timeout_s: float, on_token, should_abort=None,
                        payload_classes=None) -> LLMResult:
        """Stream chat tokens (chat mode only). Calls on_token(delta) as text
        arrives; stops early if should_abort() (barge-in). Never JSON mode —
        command planning must NOT stream (safety). Isolated so tests mock the
        SSE response via requests.post."""
        allowed, reason = cloud_allowed(payload_classes, self.config)
        if not allowed:
            raise PrivacyViolation(reason)
        payload = {"model": self.config.cloud_model, "messages": messages,
                   "max_tokens": max_tokens, "temperature": temperature,
                   "stream": True}
        started = time.perf_counter()
        try:
            r = requests.post(
                GROQ_URL, json=payload, stream=True, timeout=(3.05, timeout_s),
                headers={"Authorization": f"Bearer {self.api_key()}"})
        except requests.exceptions.Timeout:
            return LLMResult(provider=self.name, error="timeout",
                             error_detail=f"no answer within {timeout_s:.0f}s")
        except requests.RequestException as e:
            return LLMResult(provider=self.name, error="network",
                             error_detail=type(e).__name__)
        if r.status_code in (401, 403):
            return LLMResult(provider=self.name, error="auth",
                             error_detail="key rejected")
        if r.status_code == 429:
            return LLMResult(provider=self.name, error="rate_limit",
                             error_detail=r.headers.get("retry-after", "?"))
        if r.status_code >= 400:
            return LLMResult(provider=self.name, error="bad_response",
                             error_detail=f"HTTP {r.status_code}")

        parts, first_ms, aborted = [], 0.0, False
        try:
            for raw in r.iter_lines():
                if should_abort is not None and should_abort():
                    aborted = True
                    break
                if not raw:
                    continue
                line = raw.decode("utf-8", "replace").strip() \
                    if isinstance(raw, (bytes, bytearray)) else raw.strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    delta = json.loads(data)["choices"][0].get("delta", {}).get("content")
                except (ValueError, KeyError, IndexError, TypeError):
                    continue
                if delta:
                    if not first_ms:
                        first_ms = (time.perf_counter() - started) * 1000
                    parts.append(delta)
                    on_token(delta)
        finally:
            r.close()
        return LLMResult(text="".join(parts), provider=self.name,
                         latency_ms=(time.perf_counter() - started) * 1000,
                         first_token_ms=first_ms, aborted=aborted)


class OllamaProvider:
    name = "ollama"

    def __init__(self, client):
        # Accepts the client itself or a zero-arg getter. Agent passes a
        # getter so tests that swap agent.llm keep working transparently.
        self._client = client

    @property
    def client(self):
        return self._client() if callable(self._client) else self._client

    def health_check(self) -> bool:
        return self.client.is_available()

    def complete(self, messages, *, json_mode: bool, num_predict,
                 temperature: float, timeout_s: float = None,
                 model: str = None) -> LLMResult:
        started = time.perf_counter()
        text = self.client.chat(messages, json_format=json_mode,
                                temperature=temperature,
                                num_predict=num_predict, model=model,
                                timeout_s=timeout_s)
        return LLMResult(text=text, provider=self.name,
                         latency_ms=(time.perf_counter() - started) * 1000)


class BrainRouter:
    """Provider selection + failover chain + circuit breaker.

    hybrid + key + circuit closed:  Groq -> (on failure) Ollama
    local_only / no key / open:     Ollama only (existing behavior)
    Both failed -> BrainUnavailable with an honest message.
    """

    HONEST_FAILURE = ("My cloud brain is unreachable and my local brain timed "
                      "out. Simple commands still work — try me again in a moment.")

    def __init__(self, config, ollama_client):
        self.config = config
        self.groq = GroqProvider(config)
        self.ollama = OllamaProvider(ollama_client)
        self.history = deque(maxlen=10)   # popover: last LLM calls
        self.last: LLMResult = LLMResult()
        self.on_state_change = None       # callback() — chip refresh
        self._failures = 0
        self._open_until = 0.0
        self._lock = threading.Lock()

    # ----------------------------------------------------------- state
    def mode(self) -> str:
        """Effective mode: hybrid only with a key configured. The local
        engine floor (10C) forces local_only regardless of brain_mode."""
        if getattr(self.config, "engine_mode", "pipeline") == "local":
            return "local_only"
        if getattr(self.config, "brain_mode", "hybrid") == "local_only":
            return "local_only"
        return "hybrid" if self.groq.configured() else "local_only"

    def circuit_open(self) -> bool:
        return time.monotonic() < self._open_until

    def circuit_state(self) -> str:
        if not self.circuit_open():
            return "closed"
        return f"open ({self._open_until - time.monotonic():.0f}s left)"

    def _notify(self) -> None:
        if self.on_state_change:
            try:
                self.on_state_change()
            except Exception:
                pass

    def _record(self, kind: str, result: LLMResult) -> None:
        self.history.append({
            "ts": time.strftime("%H:%M:%S"), "kind": kind,
            "provider": result.provider, "latency_ms": round(result.latency_ms),
            "error": result.error, "failover": result.failover,
            "data_classes": sorted(
                c.value for c in (getattr(self, "last_data_classes", None) or ())),
        })
        self.last = result

    # ------------------------------------------------------------ core
    def complete(self, kind: str, messages: list, model: str = None,
                 payload_classes=None) -> LLMResult:
        """Returns an LLMResult on success. Raises OllamaError family
        (incl. BrainUnavailable) on total failure so the pipeline's existing
        recovery paths keep working. payload_classes drives the privacy
        routing: never-cloud/clipboard-without-opt-in stays on Ollama."""
        params = KIND_PARAMS[kind]
        if payload_classes is None:
            payload_classes = ({DataClass.TRANSCRIPT, DataClass.CHAT_CONTEXT}
                               if kind == "chat" else {DataClass.TRANSCRIPT})
        self.last_data_classes = payload_classes
        allowed, privacy_reason = cloud_allowed(payload_classes, self.config)
        use_groq = self.mode() == "hybrid" and not self.circuit_open() and allowed
        if self.mode() == "hybrid" and not allowed:
            devlog.log(f"Privacy routing: {privacy_reason} — using the local brain.")

        if use_groq:
            result = self.groq.complete(
                messages, json_mode=params["json_mode"],
                max_tokens=params["groq_max_tokens"],
                temperature=params["temperature"],
                timeout_s=getattr(self.config, "cloud_timeout_s", 8.0),
                payload_classes=payload_classes)
            self._record(kind, result)
            if result.ok:
                with self._lock:
                    if self._failures or self._open_until:
                        self._failures = 0
                        self._open_until = 0.0
                        devlog.log("Brain circuit CLOSED — cloud brain healthy again.")
                        self._notify()
                return result
            # Groq failed -> count towards the circuit, fall back to local.
            with self._lock:
                self._failures += 1
                if self._failures >= CIRCUIT_FAILURES and not self.circuit_open():
                    self._open_until = time.monotonic() + CIRCUIT_COOLDOWN_S
                    devlog.warn(f"Brain circuit OPEN for {CIRCUIT_COOLDOWN_S:.0f}s "
                                f"after {self._failures} consecutive cloud failures.")
                    self._notify()
            devlog.warn(f"Cloud brain failed ({result.error}: {result.error_detail}) "
                        "— falling back to the local brain.")
            try:
                fallback = self.ollama.complete(
                    messages, json_mode=params["json_mode"],
                    num_predict=params["ollama_num_predict"],
                    temperature=params["temperature"],
                    timeout_s=15.0,          # capped when acting as fallback
                    model=model)
            except OllamaError as e:
                failed = LLMResult(provider="ollama", error="timeout",
                                   error_detail=str(e)[:120], failover=True)
                self._record(kind, failed)
                raise BrainUnavailable(self.HONEST_FAILURE) from e
            fallback.failover = True
            self._record(kind, fallback)
            return fallback

        # local-only path — existing behavior, existing exceptions.
        result = self.ollama.complete(
            messages, json_mode=params["json_mode"],
            num_predict=params["ollama_num_predict"],
            temperature=params["temperature"], model=model)
        self._record(kind, result)
        return result

    def stream_chat(self, messages: list, on_token, should_abort=None,
                    model: str = None, payload_classes=None) -> LLMResult:
        """Streamed chat (9B). Groq streams tokens via on_token; on cloud
        failure or when streaming isn't available, falls back to a single
        non-streaming local reply (on_token gets the whole text once). Chat
        only — command planning never streams (safety)."""
        if payload_classes is None:
            payload_classes = {DataClass.TRANSCRIPT, DataClass.CHAT_CONTEXT}
        self.last_data_classes = payload_classes
        allowed, privacy_reason = cloud_allowed(payload_classes, self.config)
        use_groq = self.mode() == "hybrid" and not self.circuit_open() and allowed
        if self.mode() == "hybrid" and not allowed:
            devlog.log(f"Privacy routing: {privacy_reason} — using the local brain.")

        if use_groq:
            result = self.groq.complete_stream(
                messages, max_tokens=KIND_PARAMS["chat"]["groq_max_tokens"],
                temperature=KIND_PARAMS["chat"]["temperature"],
                timeout_s=getattr(self.config, "cloud_timeout_s", 8.0),
                on_token=on_token, should_abort=should_abort,
                payload_classes=payload_classes)
            self._record("chat", result)
            if result.ok or result.aborted:
                with self._lock:
                    if self._failures or self._open_until:
                        self._failures = 0
                        self._open_until = 0.0
                        devlog.log("Brain circuit CLOSED — cloud brain healthy again.")
                        self._notify()
                return result
            with self._lock:
                self._failures += 1
                if self._failures >= CIRCUIT_FAILURES and not self.circuit_open():
                    self._open_until = time.monotonic() + CIRCUIT_COOLDOWN_S
                    devlog.warn(f"Brain circuit OPEN for {CIRCUIT_COOLDOWN_S:.0f}s "
                                f"after {self._failures} cloud failures.")
                    self._notify()
            devlog.warn(f"Cloud stream failed ({result.error}) — local brain.")
            # fall through to local, non-streaming

        # local (or fallback) — one shot, emit as a single "token"
        try:
            result = self.ollama.complete(
                messages, json_mode=False,
                num_predict=KIND_PARAMS["chat"]["ollama_num_predict"],
                temperature=KIND_PARAMS["chat"]["temperature"],
                timeout_s=15.0 if use_groq else None, model=model)
        except OllamaError as e:
            failed = LLMResult(provider="ollama", error="timeout",
                               error_detail=str(e)[:120], failover=use_groq)
            self._record("chat", failed)
            raise BrainUnavailable(self.HONEST_FAILURE) from e
        result.failover = use_groq
        if should_abort is None or not should_abort():
            on_token(result.text)
        self._record("chat", result)
        return result

    def info(self) -> dict:
        """For the Brain chip popover. Never includes the API key."""
        return {"mode": self.mode(),
                "cloud_model": getattr(self.config, "cloud_model", ""),
                "circuit": self.circuit_state(),
                "calls": list(self.history)}
