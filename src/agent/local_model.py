"""Local llama.cpp model client over loopback HTTP."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _is_loopback(base_url: str) -> bool:
    try:
        host = urlparse(base_url).hostname
    except ValueError:
        return False
    return host is not None and host.lower() in _LOOPBACK_HOSTS


@dataclass(frozen=True, slots=True)
class LlamaCppClient:
    """Loopback-only client for a locally hosted llama.cpp server."""

    base_url: str
    model: str
    timeout_seconds: float
    http_client: httpx.Client

    def __post_init__(self) -> None:
        if not _is_loopback(self.base_url):
            raise ValueError(f"refusing nonlocal model endpoint: {self.base_url}")
        if not self.model:
            raise ValueError("model name must be non-empty")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout must be positive")

    def generate_json(self, messages: Sequence[Mapping[str, str]], schema_name: str) -> Mapping[str, object]:
        """Request one structured response from a locally hosted llama.cpp model and return parsed JSON. Reject nonlocal endpoints, timeout, invalid JSON and schema transport failure so the baseline can remain the safe fallback."""
        url = self.base_url.rstrip("/") + "/v1/chat/completions"
        payload = {
            "messages": [{"content": item["content"], "role": item["role"]} for item in messages],
            "model": self.model,
            "response_format": {"type": "json_object"},
        }
        try:
            response = self.http_client.post(url, json=payload, timeout=self.timeout_seconds)
        except httpx.TimeoutException as exc:
            raise TimeoutError(f"local model timeout: {schema_name}") from exc
        except httpx.HTTPError as exc:
            raise ConnectionError(f"local model transport failure: {schema_name}") from exc
        if response.status_code != 200:
            raise ConnectionError(f"local model status {response.status_code}: {schema_name}")
        try:
            document = json.loads(response.text)
            content = document["choices"][0]["message"]["content"]
            parsed = json.loads(content)
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ValueError(f"local model malformed response: {schema_name}") from exc
        if not isinstance(parsed, dict):
            raise ValueError(f"local model malformed response: {schema_name}")
        return parsed


__all__ = ["LlamaCppClient"]
