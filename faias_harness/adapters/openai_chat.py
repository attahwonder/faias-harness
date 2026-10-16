# UNTESTED IN v0.1. No test calls this module. Nobody has run it against a live endpoint.
"""A client for an OpenAI-compatible chat-completions endpoint. It offers the `conversation` capability only.

Rules this client follows:
- It sends requests only to the endpoint that the profile names.
- It reads the API key from the environment variable that the profile names (settings.api_key_env).
  The key never goes in a file, a log or an error message.
- It keeps no log of its own in v0.1.
"""
import json
import os
import urllib.error
import urllib.request

from ..target import Target


class OpenAIChatTarget(Target):
    capabilities = frozenset({"conversation"})

    def __init__(self, endpoint: str, model_id: str, api_key_env: str, timeout_s: float = 60.0,
                 system_prompt: str | None = None, params: dict | None = None):
        if not endpoint.startswith(("http://", "https://")):
            raise ValueError("endpoint must start with http:// or https://")
        self.endpoint = endpoint.rstrip("/")
        self.model_id = model_id
        self.api_key_env = api_key_env
        self.timeout_s = timeout_s
        self.system_prompt = system_prompt
        self.params = dict(params or {})
        self._sessions: dict[str, list[dict]] = {}
        self.last_usage: dict = {}

    def _key(self) -> str:
        key = os.environ.get(self.api_key_env, "")
        if not key:
            raise RuntimeError(f"the environment variable {self.api_key_env} is not set")
        return key

    def _post(self, messages: list[dict]) -> dict:
        body = json.dumps({"model": self.model_id, "messages": messages, **self.params}).encode("utf-8")
        req = urllib.request.Request(
            self.endpoint + "/chat/completions", data=body, method="POST",
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + self._key()})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"the endpoint returned HTTP {e.code}") from None

    # conversation
    def open_session(self) -> str:
        sid = f"s{len(self._sessions) + 1}"
        self._sessions[sid] = [{"role": "system", "content": self.system_prompt}] if self.system_prompt else []
        return sid

    def send(self, session_id: str, message: str) -> str:
        history = self._sessions[session_id]
        history.append({"role": "user", "content": message})
        data = self._post(history)
        self.last_usage = data.get("usage", {})
        reply = data["choices"][0]["message"]["content"]
        history.append({"role": "assistant", "content": reply})
        return reply

    def close_session(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
