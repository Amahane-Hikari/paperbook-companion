
"""Bounded text calls for the story agents; importing never contacts a model."""
import json
import threading
import time
from typing import Callable

import requests


class Gemini3ProAgent:
    def __init__(self, base_url: str, api_key: str, model: str,
                 system_prompt: str = None, track_history: bool = False,
                 request_timeout: float = 30, retry_budget: float = 60):
        self.url = base_url
        self.headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        self.model = model
        self.system_prompt = system_prompt
        self.track_history = bool(track_history)
        self.request_timeout = max(0.1, float(request_timeout))
        self.retry_budget = max(0.1, float(retry_budget))
        self.history = self._initial_history()
        self.last_error = None
        self.last_attempts = 0
        self._lock = threading.RLock()

    def _initial_history(self):
        return [{"role": "system", "content": self.system_prompt}] if self.system_prompt else []

    def basic_success_check(self, response):
        try:
            return isinstance(response["choices"][0]["message"]["content"], str)
        except (KeyError, IndexError, TypeError):
            return False

    def call(self, prompt: str, top_p: float = 0.95, max_new_token: int = 512,
             temperature: float = 1.0, max_try: int = 3,
             success_check_fn: Callable = None, seed=None):
        """Return (content, success). Every failure consumes a retry/time budget."""
        with self._lock:
            prior = list(self.history) if self.track_history else self._initial_history()
            messages = prior + [{"role": "user", "content": str(prompt)}]
            payload = {"model": self.model, "temperature": temperature,
                       "max_tokens": int(max_new_token), "top_p": top_p,
                       "messages": messages, "stream": False}
            if seed is not None:
                payload["seed"] = int(seed)
            self.last_error = None
            self.last_attempts = 0
            last_content = ""
            deadline = time.monotonic() + self.retry_budget
            for attempt in range(max(1, min(int(max_try), 10))):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.last_error = "model retry time budget exhausted"
                    break
                self.last_attempts = attempt + 1
                try:
                    response = requests.request(
                        "POST", self.url, headers=self.headers,
                        json=payload,
                        timeout=min(self.request_timeout, remaining))
                    response.raise_for_status()
                    body = response.json()
                    if not self.basic_success_check(body):
                        raise ValueError("model response has no text choices")
                    last_content = body["choices"][0]["message"]["content"].strip()
                    if not last_content or (success_check_fn and not success_check_fn(last_content)):
                        raise ValueError("model output failed structure validation")
                    if self.track_history:
                        conversation = messages + [{"role": "assistant", "content": last_content}]
                        system = [m for m in conversation if m["role"] == "system"]
                        turns = [m for m in conversation if m["role"] != "system"][-16:]
                        self.history = system[:1] + turns
                    else:
                        self.history = self._initial_history()
                    return last_content, True
                except Exception as exc:
                    # Never print request headers or complete provider payloads.
                    self.last_error = f"{type(exc).__name__}: {exc}"
            if not self.track_history:
                self.history = self._initial_history()
            return last_content, False
