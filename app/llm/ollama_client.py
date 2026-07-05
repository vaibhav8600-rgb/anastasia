"""Thin client for the local Ollama HTTP API.

Latency root-cause settings (spec section 10a): every chat request pins
think=false (no hidden reasoning tokens), keep_alive (no model unload
between commands), num_predict (bounded generation) and a small num_ctx.
warm_up() preloads the model at startup so the first real command is fast.
"""

import time
from collections import deque

import requests

CONNECT_TIMEOUT = 3.05


class OllamaError(Exception):
    """Base class for Ollama problems."""


class OllamaNotRunning(OllamaError):
    """Ollama is not reachable at the configured URL."""


class OllamaModelMissing(OllamaError):
    """The configured model has not been pulled."""


class OllamaClient:
    def __init__(self, config):
        self.config = config  # live reference — settings changes apply immediately
        self.last_latency_ms: float = 0.0
        self.last_tokens_per_s: float = 0.0
        self.warmed_up: bool = False
        self._latencies = deque(maxlen=20)

    @property
    def base_url(self) -> str:
        return self.config.ollama_url.rstrip("/")

    @property
    def average_latency_ms(self) -> float:
        return sum(self._latencies) / len(self._latencies) if self._latencies else 0.0

    def is_available(self) -> bool:
        try:
            r = requests.get(f"{self.base_url}/api/tags", timeout=3)
            return r.status_code == 200
        except requests.RequestException:
            return False

    def list_models(self) -> list:
        try:
            r = requests.get(f"{self.base_url}/api/tags", timeout=5)
            r.raise_for_status()
            return [m.get("name", "") for m in r.json().get("models", [])]
        except requests.RequestException:
            return []

    # ------------------------------------------------------------------
    def _build_payload(self, messages: list, json_format: bool,
                       temperature: float, num_predict: int = None) -> dict:
        payload = {
            "model": self.config.ollama_model,
            "messages": messages,
            "stream": False,
            "think": False,
            "keep_alive": self.config.ollama_keep_alive,
            "options": {
                "num_predict": num_predict or self.config.ollama_num_predict,
                "temperature": temperature,
                "num_ctx": self.config.ollama_num_ctx,
            },
        }
        if json_format:
            payload["format"] = "json"
        return payload

    def chat(self, messages: list, json_format: bool = True,
             temperature: float = 0.1, num_predict: int = None) -> str:
        """Send a chat request; returns the assistant message content."""
        payload = self._build_payload(messages, json_format, temperature, num_predict)
        started = time.perf_counter()
        try:
            r = requests.post(f"{self.base_url}/api/chat", json=payload,
                              timeout=(CONNECT_TIMEOUT, self.config.ollama_timeout))
        except requests.exceptions.ConnectionError as e:
            raise OllamaNotRunning(
                "Ollama is not reachable. Start it with `ollama serve` "
                "or launch the Ollama app.") from e
        except requests.exceptions.Timeout as e:
            raise OllamaError("The local model took too long to answer.") from e

        if r.status_code == 404 or (r.status_code >= 400 and "not found" in r.text.lower()):
            raise OllamaModelMissing(
                f"Model '{self.config.ollama_model}' is not installed. "
                f"Run: ollama pull {self.config.ollama_model}")
        try:
            r.raise_for_status()
        except requests.HTTPError as e:
            raise OllamaError(f"Ollama error: {r.text[:300]}") from e

        body = r.json()
        self._record_latency(body, (time.perf_counter() - started) * 1000)
        content = body.get("message", {}).get("content", "")
        if "qwen" in self.config.ollama_model.lower():
            # Defensive: qwen-family models may still emit <think> blocks.
            from app.llm.intent_parser import strip_thinking
            content = strip_thinking(content)
        return content

    def warm_up(self) -> float | None:
        """Preload the model (tiny 1-token request) so the first real
        command doesn't pay the load cost. Returns latency in ms, or None."""
        payload = self._build_payload(
            [{"role": "user", "content": "hi"}],
            json_format=False, temperature=0.0, num_predict=1)
        started = time.perf_counter()
        try:
            r = requests.post(f"{self.base_url}/api/chat", json=payload,
                              timeout=(CONNECT_TIMEOUT, 300))
            r.raise_for_status()
        except requests.RequestException:
            return None
        self.warmed_up = True
        return (time.perf_counter() - started) * 1000

    def _record_latency(self, body: dict, elapsed_ms: float) -> None:
        self.last_latency_ms = elapsed_ms
        self._latencies.append(elapsed_ms)
        eval_count = body.get("eval_count") or 0
        eval_ns = body.get("eval_duration") or 0
        self.last_tokens_per_s = (eval_count / (eval_ns / 1e9)) if eval_ns else 0.0
