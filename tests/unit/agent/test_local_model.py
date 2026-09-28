"""Invariant guards for the loopback-only local model client."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

import httpx
import pytest

from src.agent.local_model import LlamaCppClient

BASE_URL = "http://127.0.0.1:8080"


def _client(handler: object) -> LlamaCppClient:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    return LlamaCppClient(BASE_URL, "local-7b-q4", 5.0, httpx.Client(transport=transport))


def _messages() -> Sequence[Mapping[str, str]]:
    return [{"role": "user", "content": "draft"}]


def _ok_payload(content: str) -> bytes:
    return json.dumps({"choices": [{"message": {"content": content}}]}).encode()


def test_remote_endpoint_denied_before_send() -> None:
    """A non-loopback base URL cannot be used to send a request."""
    with pytest.raises(ValueError, match="nonlocal"):
        LlamaCppClient("https://api.example.com", "m", 5.0, httpx.Client())
    with pytest.raises(ValueError, match="nonlocal"):
        LlamaCppClient("http://[::1", "m", 5.0, httpx.Client())


def test_empty_model_and_timeout_rejected() -> None:
    """An empty model name and a non-positive timeout fail closed."""
    with pytest.raises(ValueError, match="non-empty"):
        LlamaCppClient(BASE_URL, "", 5.0, httpx.Client())
    with pytest.raises(ValueError, match="positive"):
        LlamaCppClient(BASE_URL, "m", 0.0, httpx.Client())


def test_structured_response_returns_parsed_json() -> None:
    """A well-formed loopback response returns the parsed mapping."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        return httpx.Response(200, content=_ok_payload('{"claims": []}'))

    assert _client(handler).generate_json(_messages(), "memo_claims") == {"claims": []}


def test_malformed_response_reaches_baseline_fallback() -> None:
    """Invalid JSON from the local server surfaces as a typed value failure."""

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, content=b"not json")

    with pytest.raises(ValueError, match="malformed"):
        _client(handler).generate_json(_messages(), "memo_claims")


def test_missing_choices_reaches_baseline_fallback() -> None:
    """A schema-shaped response without choices surfaces as a typed value failure."""

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, content=json.dumps({"nope": True}).encode())

    with pytest.raises(ValueError, match="malformed"):
        _client(handler).generate_json(_messages(), "tool_plan")


def test_nonmapping_content_reaches_baseline_fallback() -> None:
    """A JSON list payload is rejected as a malformed structured response."""

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, content=_ok_payload("[1, 2]"))

    with pytest.raises(ValueError, match="malformed"):
        _client(handler).generate_json(_messages(), "memo_claims")


def test_error_status_reaches_baseline_fallback() -> None:
    """A non-200 loopback status surfaces as a typed transport failure."""

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(500, content=b"boom")

    with pytest.raises(ConnectionError, match="status 500"):
        _client(handler).generate_json(_messages(), "memo_claims")


def test_timeout_reaches_baseline_fallback() -> None:
    """A loopback timeout surfaces as a typed timeout failure."""

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        raise httpx.ConnectTimeout("slow")

    with pytest.raises(TimeoutError, match="timeout"):
        _client(handler).generate_json(_messages(), "memo_claims")


def test_transport_error_reaches_baseline_fallback() -> None:
    """A loopback connection failure surfaces as a typed transport failure."""

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        raise httpx.ConnectError("down")

    with pytest.raises(ConnectionError, match="transport"):
        _client(handler).generate_json(_messages(), "memo_claims")
