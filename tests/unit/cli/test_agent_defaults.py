"""Invariant guards for agent wiring defaults in CLI and batch."""

from __future__ import annotations

from src.agent.local_model import DEFAULT_AGENT_MODEL, DEFAULT_AGENT_TIMEOUT_SECONDS
from src.agent.workflow import DEFAULT_PROMPT_VERSION
from src.cli.main import build_parser


def _parse(argv: list[str]):  # type: ignore[no-untyped-def]
    return build_parser().parse_args(argv)


def test_memo_defaults_track_constants() -> None:
    args = _parse(["research", "memo", "--rcept-no", "20240620000001", "--as-of", "2024-06-24T18:00:00+09:00", "--index-manifest", "m"])
    assert args.agent_model == DEFAULT_AGENT_MODEL
    assert args.agent_timeout_seconds == DEFAULT_AGENT_TIMEOUT_SECONDS
    assert args.agent_prompt_version == DEFAULT_PROMPT_VERSION


def test_replay_defaults_track_constants() -> None:
    args = _parse(["eval", "replay", "--cases", "data/eval/cases.json"])
    assert args.agent_model == DEFAULT_AGENT_MODEL
    assert args.agent_timeout_seconds == DEFAULT_AGENT_TIMEOUT_SECONDS
    assert args.agent_prompt_version == DEFAULT_PROMPT_VERSION


def test_daily_defaults_track_constants() -> None:
    args = _parse(
        ["batch", "daily", "--dart-start", "2024-06-01", "--dart-end", "2024-06-20", "--as-of", "2024-06-24T18:30:00+09:00"]
    )
    assert args.agent_model == DEFAULT_AGENT_MODEL
    assert args.agent_timeout_seconds == DEFAULT_AGENT_TIMEOUT_SECONDS
    assert args.agent_prompt_version == DEFAULT_PROMPT_VERSION


def test_batch_policy_carries_model_identity(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from src.cli.batch import _try_agent_clients

    monkeypatch.setenv("AGENT_BASE_URL", "http://127.0.0.1:8080")
    monkeypatch.delenv("AGENT_MODEL", raising=False)
    client, policy = _try_agent_clients()
    assert client is not None
    assert policy is not None
    assert policy.model_id == DEFAULT_AGENT_MODEL
    assert policy.prompt_version == DEFAULT_PROMPT_VERSION
    assert policy.model_timeout_seconds == DEFAULT_AGENT_TIMEOUT_SECONDS


def test_batch_honours_agent_model(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from src.cli.batch import _try_agent_clients

    monkeypatch.setenv("AGENT_BASE_URL", "http://127.0.0.1:8080")
    monkeypatch.setenv("AGENT_MODEL", "qwen3.5-9b")
    client, policy = _try_agent_clients()
    assert client is not None
    assert policy is not None
    assert client.model == "qwen3.5-9b"
    assert policy.model_id == "qwen3.5-9b"


def test_batch_without_base_url_disables_agent(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from src.cli.batch import _try_agent_clients

    monkeypatch.delenv("AGENT_BASE_URL", raising=False)
    assert _try_agent_clients() == (None, None)
