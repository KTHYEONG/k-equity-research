"""Bounded local-model orchestration with deterministic output gate."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, cast

from pydantic import BaseModel, ValidationError

from src.agent.tools import ToolName, ToolRequest, ToolResult, execute_tool
from src.research.context import ResearchContext
from src.research.memo import MemoClaim, ResearchMemo

_NUMBER_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")
_NARRATIVE_KINDS = frozenset({"filing_fact", "materiality", "narrative", "peer_context", "price_path", "uncertainty"})
_TOOL_NAMES = ("get_event", "get_financial_asof", "get_market_window", "get_peers", "get_analogues", "get_revision_diff")


@dataclass(frozen=True, slots=True)
class AgentPolicy:
    """Explicit run configuration for bounded local-model orchestration."""

    max_tool_calls: int
    model_timeout_seconds: float
    prompt_version: str


class LocalModelClient(Protocol):
    """Structural client for one locally hosted model."""

    def generate_json(self, messages: Sequence[Mapping[str, str]], schema_name: str) -> Mapping[str, object]: ...  # pragma: no cover


class _ToolCallProposal(BaseModel):
    name: str
    arguments: dict[str, str] = {}


class _ToolPlan(BaseModel):
    tool_calls: list[_ToolCallProposal] = []


class _ClaimProposal(BaseModel):
    kind: str
    text: str
    evidence_ids: list[str]
    metric_key: str | None = None


class _NarrativeOutput(BaseModel):
    claims: list[_ClaimProposal] = []


def _summary_messages(baseline: ResearchMemo, policy: AgentPolicy) -> list[dict[str, str]]:
    metrics = ",".join(f"{key}={baseline.metrics[key]}" for key in sorted(baseline.metrics))
    evidence = ",".join(sorted(item.id for item in baseline.evidence))
    return [
        {
            "role": "system",
            "content": f"prompt={policy.prompt_version} tools={','.join(_TOOL_NAMES)} max_calls={policy.max_tool_calls}",
        },
        {
            "role": "user",
            "content": f"event={baseline.event_id} as_of={baseline.as_of.isoformat()} evidence={evidence} metrics={metrics}",
        },
    ]


def _tool_trace(results: Sequence[ToolResult]) -> tuple[str, ...]:
    return tuple(sorted({f"AGENT_TOOL:{result.name}" for result in results}))


def _narrative_messages(
    summary: Sequence[Mapping[str, str]], results: Sequence[ToolResult]
) -> list[dict[str, str]]:
    messages = [dict(item) for item in summary]
    for result in results:
        payload = json.dumps(result.payload, sort_keys=True, default=str)
        messages.append(
            {"role": "user", "content": f"tool={result.name} evidence={','.join(sorted(result.evidence_ids))} payload={payload}"}
        )
    return messages


def _allowed_numbers(baseline: ResearchMemo) -> set[str]:
    allowed: set[str] = set()
    for value in (*baseline.facts.values(), *baseline.metrics.values()):
        if value is not None:
            allowed.update(token.replace(",", "") for token in _NUMBER_RE.findall(value))
    for claim in baseline.claims:
        allowed.update(token.replace(",", "") for token in _NUMBER_RE.findall(claim.text))
    return allowed


def _fallback(baseline: ResearchMemo, policy: AgentPolicy, status: str, trace: Sequence[str]) -> ResearchMemo:
    statuses = tuple(sorted(set(baseline.statuses) | {status, f"AGENT_PROMPT:{policy.prompt_version}"} | set(trace)))
    return ResearchMemo(
        event_id=baseline.event_id,
        anchor_rcept_no=baseline.anchor_rcept_no,
        active_rcept_no=baseline.active_rcept_no,
        as_of=baseline.as_of,
        facts=baseline.facts,
        metrics=baseline.metrics,
        claims=baseline.claims,
        evidence=baseline.evidence,
        statuses=statuses,
        manifest_hash=baseline.manifest_hash,
    )


class AgentRunner:
    """Dispatch bounded tools for a local model and gate its narrative."""

    def run(
        self,
        context: ResearchContext,
        baseline: ResearchMemo,
        model: LocalModelClient,
        policy: AgentPolicy,
    ) -> ResearchMemo:
        """Let a local model select bounded evidence tools and draft cited narrative, then return only a validated memo. On timeout, invalid tool use, malformed output or unsupported claim, return the deterministic baseline with failure status."""
        available = {item.id for item in baseline.evidence}
        summary = _summary_messages(baseline, policy)
        try:
            plan_raw = model.generate_json(summary, "tool_plan")
        except Exception:
            return _fallback(baseline, policy, "AGENT_UNAVAILABLE", ())
        try:
            plan = _ToolPlan.model_validate(plan_raw)
        except ValidationError:
            return _fallback(baseline, policy, "AGENT_REJECTED", ())
        if len(plan.tool_calls) > policy.max_tool_calls:
            return _fallback(baseline, policy, "AGENT_REJECTED", ())
        results: list[ToolResult] = []
        try:
            for call in plan.tool_calls:  # noqa: PERF401 - sequential dispatch must stop at the first invalid tool use
                result = execute_tool(ToolRequest(cast(ToolName, call.name), call.arguments), context)
                results.append(result)
        except ValueError:
            return _fallback(baseline, policy, "AGENT_REJECTED", ())
        known = set(available)
        for result in results:
            known.update(result.evidence_ids)
        try:
            narrative_raw = model.generate_json(_narrative_messages(summary, results), "memo_claims")
        except Exception:
            return _fallback(baseline, policy, "AGENT_UNAVAILABLE", _tool_trace(results))
        try:
            narrative = _NarrativeOutput.model_validate(narrative_raw)
        except ValidationError:
            return _fallback(baseline, policy, "AGENT_REJECTED", _tool_trace(results))
        allowed = _allowed_numbers(baseline)
        gated: list[MemoClaim] = []
        for proposal in narrative.claims:
            if (
                proposal.kind not in _NARRATIVE_KINDS
                or not proposal.text.strip()
                or not proposal.evidence_ids
                or any(ref not in known for ref in proposal.evidence_ids)
                or (
                    proposal.metric_key is not None
                    and proposal.metric_key not in baseline.metrics
                    and proposal.metric_key not in baseline.facts
                )
                or any(token.replace(",", "") not in allowed for token in _NUMBER_RE.findall(proposal.text))
            ):
                return _fallback(baseline, policy, "AGENT_REJECTED", _tool_trace(results))
            gated.append(
                MemoClaim(
                    kind=proposal.kind,
                    text=proposal.text,
                    evidence_ids=tuple(proposal.evidence_ids),
                    metric_key=proposal.metric_key,
                )
            )
        statuses = tuple(
            sorted(set(baseline.statuses) | {"AGENT_OK", f"AGENT_PROMPT:{policy.prompt_version}"} | set(_tool_trace(results)))
        )
        return ResearchMemo(
            event_id=baseline.event_id,
            anchor_rcept_no=baseline.anchor_rcept_no,
            active_rcept_no=baseline.active_rcept_no,
            as_of=baseline.as_of,
            facts=baseline.facts,
            metrics=baseline.metrics,
            claims=(*baseline.claims, *gated),
            evidence=baseline.evidence,
            statuses=statuses,
            manifest_hash=baseline.manifest_hash,
        )


__all__ = ["AgentPolicy", "AgentRunner", "LocalModelClient"]
