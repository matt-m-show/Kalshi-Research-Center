"""Minimal async client for a local Ollama server (native /api endpoints).

Used for high-volume, low-stakes work — search planning, page summarization,
plain-English settings parsing — so the paid model only does the final
probability call. Every call returns schema-constrained JSON.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

from src.common.logging import get_logger

log = get_logger("ollama")


class OllamaUnavailable(RuntimeError):
    pass


def default_host() -> str:
    return os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/")


class OllamaClient:
    def __init__(self, model: str, host: str | None = None, timeout: float = 120.0,
                 num_ctx: int = 16384) -> None:
        self.model = model
        self.host = (host or default_host()).rstrip("/")
        self.timeout = timeout
        self.num_ctx = num_ctx
        self.prompt_tokens = 0
        self.output_tokens = 0
        self.calls = 0

    async def health(self) -> tuple[bool, str]:
        """(ok, detail). ok only if the server answers and the model is installed."""
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                tags = (await c.get(f"{self.host}/api/tags")).json()
        except Exception as e:
            return False, f"Ollama not reachable at {self.host}: {e}"
        names = {m.get("name") for m in tags.get("models", [])} | {m.get("model") for m in tags.get("models", [])}
        wanted = {self.model, f"{self.model}:latest"}
        if not names & wanted:
            return False, f"Model {self.model!r} is not installed (run: ollama pull {self.model})"
        return True, "ok"

    async def json_chat(self, system: str, user: str, schema: dict[str, Any],
                        temperature: float = 0.2) -> dict[str, Any]:
        """One chat turn constrained to ``schema``; returns the parsed object."""
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "format": schema,
            "stream": False,
            "think": False,
            "options": {"temperature": temperature, "num_ctx": self.num_ctx},
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                resp = await c.post(f"{self.host}/api/chat", json=body)
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            raise OllamaUnavailable(f"Ollama call failed: {e}") from e
        self.calls += 1
        self.prompt_tokens += int(data.get("prompt_eval_count") or 0)
        self.output_tokens += int(data.get("eval_count") or 0)
        content = (data.get("message") or {}).get("content", "")
        try:
            return json.loads(content)
        except json.JSONDecodeError as e:
            raise OllamaUnavailable(f"Ollama returned non-JSON output: {content[:200]}") from e
