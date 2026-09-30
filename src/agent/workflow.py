"""Bounded local-model orchestration with deterministic output gate."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Protocol, cast

from pydantic import BaseModel, ConfigDict, ValidationError

from src.agent.tools import TOOL_SPECS, ToolName, ToolRequest, ToolResult, execute_tool
from src.research.context import ResearchContext
from src.research.memo import MemoClaim, ResearchMemo, display_figures

logger = logging.getLogger(__name__)

_NUMBER_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")
_NARRATIVE_KINDS = frozenset({"filing_fact", "materiality", "narrative", "peer_context", "price_path", "uncertainty"})
_TOOL_NAMES: Final[tuple[str, ...]] = tuple(TOOL_SPECS.keys())

_ISO_DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"
_TOOL_BUDGET_CEILING: Final[int] = 3

DEFAULT_PROMPT_VERSION: Final[str] = "v2"

_HORIZON_TOKENS = frozenset({"1", "5", "20"})
_HORIZON_SUFFIXES: Final[tuple[str, ...]] = ("거래일", "일")
_INVENTED_DURATION_RE = re.compile(r"\d ?(?:개월|주일|주간|시간|분(?!기))")

_FIGURE_GLOSSARY: Final[Mapping[str, str]] = {
    "metric:amount_to_cash": "planned amount divided by cash at filing",
    "metric:amount_to_equity": "planned amount divided by equity at filing",
    "metric:amount_to_market_cap": "planned amount divided by prior market capitalisation",
    "metric:car_h1": "cumulative market-model abnormal return over the 1 trading session after the first actionable session (trading sessions, not calendar time)",
    "metric:car_h20": "cumulative market-model abnormal return over the 20 trading sessions after the first actionable session (trading sessions, not calendar time)",
    "metric:car_h5": "cumulative market-model abnormal return over the 5 trading sessions after the first actionable session (trading sessions, not calendar time)",
    "metric:cash_to_assets": "cash divided by total assets",
    "metric:intraday_excess_s0": "stock open-to-close return minus index open-to-close return on the first actionable trading session after the filing",
    "metric:liabilities_to_equity": "total liabilities divided by equity",
    "metric:shares_to_listed_shares": "planned shares divided by listed shares",
}

_TOOL_LIST_LIMIT: Final[int] = 8
_TOOL_LIST_EDGE: Final[int] = 3
_TOOL_EVIDENCE_SHOWN: Final[int] = 20
_MAX_CLAIMS: Final[int] = 5
_MAX_TEXT_CHARS: Final[int] = 140
_MAX_EVIDENCE_PER_CLAIM: Final[int] = 6


@dataclass(frozen=True, slots=True)
class AgentPolicy:
    """Explicit run configuration for bounded local-model orchestration.

    `model_id` names the served model; it is recorded in memo statuses and evaluation fingerprints so narrative provenance is auditable.
    """

    max_tool_calls: int
    model_timeout_seconds: float
    prompt_version: str
    model_id: str = ""


class LocalModelClient(Protocol):
    """Structural client for one locally hosted model."""

    def generate_json(self, messages: Sequence[Mapping[str, str]], schema_name: str) -> Mapping[str, object]: ...  # pragma: no cover


class _ToolCallProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    arguments: dict[str, str] = {}


class _ToolPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_calls: list[_ToolCallProposal] = []


class _ClaimProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    text: str
    evidence_ids: list[str]
    metric_key: str | None = None


class _NarrativeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claims: list[_ClaimProposal] = []


def _tool_plan_schema() -> Mapping[str, object]:
    shapes: list[dict[str, object]] = []
    for name in TOOL_SPECS:
        spec = TOOL_SPECS[name]
        properties: dict[str, dict[str, str]] = {}
        required: list[str] = []
        for arg in spec.args:
            if arg.kind == "iso_date":
                properties[arg.name] = {"type": "string", "pattern": _ISO_DATE_PATTERN}
            else:
                properties[arg.name] = {"type": "string"}
            if arg.required:
                required.append(arg.name)
        arguments_schema: dict[str, object] = {
            "type": "object",
            "properties": properties,
            "additionalProperties": False,
        }
        if required:
            arguments_schema["required"] = required
        shapes.append(
            {
                "type": "object",
                "properties": {"name": {"const": name}, "arguments": arguments_schema},
                "required": ["name", "arguments"],
                "additionalProperties": False,
            }
        )
    return {
        "type": "object",
        "properties": {
            "tool_calls": {"type": "array", "items": {"anyOf": shapes}, "maxItems": _TOOL_BUDGET_CEILING},
        },
        "additionalProperties": False,
    }


AGENT_SCHEMAS: Final[Mapping[str, Mapping[str, object]]] = {
    "tool_plan": _tool_plan_schema(),
    "memo_claims": {
        "type": "object",
        "properties": {
            "claims": {
                "type": "array",
                "maxItems": _MAX_CLAIMS,
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "enum": sorted(_NARRATIVE_KINDS)},
                        "text": {"type": "string", "maxLength": _MAX_TEXT_CHARS},
                        "evidence_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                            "maxItems": _MAX_EVIDENCE_PER_CLAIM,
                        },
                        "metric_key": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    },
                    "required": ["kind", "text", "evidence_ids"],
                    "additionalProperties": False,
                },
            },
        },
        "additionalProperties": False,
    },
}
"""JSON Schemas keyed by the `schema_name` passed to `LocalModelClient.generate_json`, for grammar-constrained decoding. Derived from the validation models so transport and gate cannot drift."""


def _tool_signature_lines() -> str:
    lines: list[str] = []
    for name in TOOL_SPECS:
        spec = TOOL_SPECS[name]
        if not spec.args:
            signature = "no arguments"
        else:
            signature = ", ".join(
                f"{arg.name}: {arg.kind} ({'required' if arg.required else 'optional'})" for arg in spec.args
            )
        lines.append(f"- {name}({signature}): {spec.description}")
    return "\n".join(lines)


def _summary_messages(baseline: ResearchMemo, policy: AgentPolicy) -> list[dict[str, str]]:
    figures = display_figures(baseline)
    figure_lines = "\n".join(f"{key}={figures[key]}" for key in sorted(figures))
    evidence_ids = sorted(item.id for item in baseline.evidence)
    statuses = ",".join(sorted(baseline.statuses))
    glossary = "\n".join(f"- {key}: {_FIGURE_GLOSSARY[key]}" for key in sorted(_FIGURE_GLOSSARY) if key in figures)
    system = (
        "You output one JSON object matching the named schema (tool_plan for tool selection, memo_claims for narrative). "
        "Write claim text in Korean. "
        "Copy every figure in text verbatim from the provided figures, preserving sign, decimals and unit. "
        "Never compute, sum, round, convert or infer a number. "
        "Horizons are trading sessions; never describe them as months, weeks, hours or minutes. "
        "Describe figures in Korean and do not print figure keys or session labels. "
        f"Cite only the provided evidence ids, with at least one and at most {_MAX_EVIDENCE_PER_CLAIM} per claim, choosing the most relevant. "
        "Describe observed movement without stating why it moved and without any trade suggestion. "
        "When CONFOUNDED is among the statuses, state that the observed movement is not attributable to the buyback alone. "
        "Return an empty claims list when nothing is supported. "
        f"Write at most {_MAX_CLAIMS} new claims, each one Korean sentence of at most {_MAX_TEXT_CHARS} characters. "
        "Do not repeat, translate or restate the existing claims listed in the user message; "
        "add only synthesis supported by the provided figures and tool results. "
        f"You may call at most {policy.max_tool_calls} tool(s) within the tool-call budget. "
        f"Figure definitions:\n{glossary}\n"
        "Available tools:\n"
        f"{_tool_signature_lines()}\n"
        "Arguments outside the listed signature are invalid and an invalid tool call discards the whole narrative. "
        "Choose get_market_window start and end dates on or before the as_of instant given in the user message. "
        f"prompt={policy.prompt_version} AGENT_PROMPT:{policy.prompt_version}"
    )
    existing_claims = "\n".join(
        sorted(f"{claim.kind}|{claim.metric_key or ''}|{','.join(claim.evidence_ids)}" for claim in baseline.claims)
    )
    user_lines = [
        f"event={baseline.event_id}",
        f"as_of={baseline.as_of.isoformat()}",
        f"statuses={statuses}",
        f"prompt={policy.prompt_version}",
        f"agent_prompt=AGENT_PROMPT:{policy.prompt_version}",
        "figures:",
        figure_lines,
        f"evidence={','.join(evidence_ids)}",
        "existing_claims (kind|metric_key|evidence_ids):",
        existing_claims,
    ]
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n".join(user_lines)},
    ]


def _tool_trace(results: Sequence[ToolResult]) -> tuple[str, ...]:
    return tuple(sorted({f"AGENT_TOOL:{result.name}" for result in results}))


def _compact_payload(value: object) -> object:
    """Shrink long lists to count plus head and tail so large tool results fit the model context."""
    if isinstance(value, Mapping):
        return {key: _compact_payload(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        if len(value) > _TOOL_LIST_LIMIT:
            return {
                "count": len(value),
                "head": [_compact_payload(item) for item in value[:_TOOL_LIST_EDGE]],
                "tail": [_compact_payload(item) for item in value[-_TOOL_LIST_EDGE:]],
            }
        return [_compact_payload(item) for item in value]
    return value


def _evidence_line(evidence_ids: Sequence[str]) -> str:
    ordered = sorted(evidence_ids)
    shown = ",".join(ordered[:_TOOL_EVIDENCE_SHOWN])
    omitted = len(ordered) - _TOOL_EVIDENCE_SHOWN
    return shown if omitted <= 0 else f"{shown},(+{omitted} more)"


def _narrative_messages(
    summary: Sequence[Mapping[str, str]], results: Sequence[ToolResult]
) -> list[dict[str, str]]:
    messages = [dict(item) for item in summary]
    for result in results:
        payload = json.dumps(_compact_payload(result.payload), sort_keys=True, default=str)
        messages.append(
            {"role": "user", "content": f"tool={result.name} evidence={_evidence_line(result.evidence_ids)} payload={payload}"}
        )
    return messages


def _allowed_numbers(baseline: ResearchMemo) -> set[str]:
    allowed: set[str] = set()
    for figure in display_figures(baseline).values():
        allowed.update(token.replace(",", "") for token in _NUMBER_RE.findall(figure))
    for raw in (*baseline.facts.values(), *baseline.metrics.values()):
        if raw is not None:
            allowed.update(token.replace(",", "") for token in _NUMBER_RE.findall(raw))
    for claim in baseline.claims:
        allowed.update(token.replace(",", "") for token in _NUMBER_RE.findall(claim.text))
    return allowed


def _agent_statuses(
    baseline: ResearchMemo, policy: AgentPolicy, extra: Sequence[str], *, reason: str | None
) -> tuple[str, ...]:
    statuses = {status for status in baseline.statuses if not status.startswith("AGENT_REASON:")}
    statuses = {status for status in statuses if not status.startswith("AGENT_MODEL:")}
    if reason is not None:
        statuses.discard("AGENT_OK")
    else:
        statuses = {status for status in statuses if status != "AGENT_REJECTED" and status != "AGENT_UNAVAILABLE"}
    statuses.add(f"AGENT_PROMPT:{policy.prompt_version}")
    if policy.model_id:
        statuses.add(f"AGENT_MODEL:{policy.model_id}")
    statuses.update(extra)
    if reason is not None:
        statuses.add(f"AGENT_REASON:{reason}")
    return tuple(sorted(statuses))


def _reject(
    baseline: ResearchMemo,
    policy: AgentPolicy,
    legacy: str,
    reason: str,
    trace: Sequence[str],
    claim_index: int = -1,
    token: str | None = None,
) -> ResearchMemo:
    if token is not None:
        logger.warning(
            "[RISK] agent_narrative_rejected event=%s reason=%s claim_index=%s token=%s",
            baseline.event_id,
            reason,
            claim_index,
            token,
        )
    else:
        logger.warning(
            "[RISK] agent_narrative_rejected event=%s reason=%s claim_index=%s",
            baseline.event_id,
            reason,
            claim_index,
        )
    statuses = _agent_statuses(baseline, policy, [legacy, *trace], reason=reason)
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


def _is_horizon_exempt(text: str, match: re.Match[str]) -> bool:
    token = match.group(0).replace(",", "")
    if token not in _HORIZON_TOKENS:
        return False
    end = match.end()
    if not any(text.startswith(suffix, end) for suffix in _HORIZON_SUFFIXES):
        return False
    return not (match.start() > 0 and text[match.start() - 1] in "0123456789.")


def _mask_identifiers(text: str, identifiers: frozenset[str]) -> str:
    if not identifiers:
        return text
    pattern = "|".join(re.escape(name) for name in sorted(identifiers, key=len, reverse=True))
    return re.sub(rf"(?<![A-Za-z0-9_])(?:{pattern})(?![A-Za-z0-9_])", lambda m: " " * len(m.group(0)), text)


def _supplied_identifiers(baseline: ResearchMemo) -> frozenset[str]:
    return frozenset({"S0", *(key.split(":", 1)[1] for key in display_figures(baseline))})


def _find_ungrounded_token(text: str, allowed: set[str], identifiers: frozenset[str] = frozenset()) -> str | None:
    duration = _INVENTED_DURATION_RE.search(text)
    if duration is not None:
        return duration.group(0)
    text = _mask_identifiers(text, identifiers)
    for match in _NUMBER_RE.finditer(text):
        if _is_horizon_exempt(text, match):
            continue
        token = match.group(0).replace(",", "")
        if token not in allowed:
            return token
    return None


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
        except (TimeoutError, ConnectionError):
            return _reject(baseline, policy, "AGENT_UNAVAILABLE", "plan_unavailable", ())
        except ValueError:
            return _reject(baseline, policy, "AGENT_REJECTED", "plan_malformed", ())
        except Exception:
            return _reject(baseline, policy, "AGENT_UNAVAILABLE", "plan_unavailable", ())
        try:
            plan = _ToolPlan.model_validate(plan_raw)
        except ValidationError:
            return _reject(baseline, policy, "AGENT_REJECTED", "plan_schema", ())
        if len(plan.tool_calls) > policy.max_tool_calls:
            return _reject(baseline, policy, "AGENT_REJECTED", "plan_over_budget", ())
        results: list[ToolResult] = []
        try:
            for call in plan.tool_calls:  # noqa: PERF401 - sequential dispatch must stop at the first invalid tool use
                result = execute_tool(ToolRequest(cast(ToolName, call.name), call.arguments), context)
                results.append(result)
        except ValueError:
            return _reject(baseline, policy, "AGENT_REJECTED", "tool_use", _tool_trace(results))
        known = set(available)
        for result in results:
            known.update(result.evidence_ids)
        try:
            narrative_raw = model.generate_json(_narrative_messages(summary, results), "memo_claims")
        except (TimeoutError, ConnectionError):
            return _reject(baseline, policy, "AGENT_UNAVAILABLE", "narrative_unavailable", _tool_trace(results))
        except ValueError:
            return _reject(baseline, policy, "AGENT_REJECTED", "narrative_malformed", _tool_trace(results))
        except Exception:
            return _reject(baseline, policy, "AGENT_UNAVAILABLE", "narrative_unavailable", _tool_trace(results))
        try:
            narrative = _NarrativeOutput.model_validate(narrative_raw)
        except ValidationError:
            return _reject(baseline, policy, "AGENT_REJECTED", "narrative_schema", _tool_trace(results))
        if len(narrative.claims) > _MAX_CLAIMS:
            return _reject(baseline, policy, "AGENT_REJECTED", "too_many_claims", _tool_trace(results))
        allowed = _allowed_numbers(baseline)
        identifiers = _supplied_identifiers(baseline)
        gated: list[MemoClaim] = []
        for index, proposal in enumerate(narrative.claims):
            if proposal.kind not in _NARRATIVE_KINDS:
                return _reject(baseline, policy, "AGENT_REJECTED", "unknown_kind", _tool_trace(results), index)
            if not proposal.text.strip():
                return _reject(baseline, policy, "AGENT_REJECTED", "empty_text", _tool_trace(results), index)
            if len(proposal.text) > _MAX_TEXT_CHARS:
                return _reject(baseline, policy, "AGENT_REJECTED", "text_too_long", _tool_trace(results), index)
            if not proposal.evidence_ids:
                return _reject(baseline, policy, "AGENT_REJECTED", "no_evidence", _tool_trace(results), index)
            if len(proposal.evidence_ids) > _MAX_EVIDENCE_PER_CLAIM:
                return _reject(baseline, policy, "AGENT_REJECTED", "too_many_evidence", _tool_trace(results), index)
            if any(ref not in known for ref in proposal.evidence_ids):
                return _reject(baseline, policy, "AGENT_REJECTED", "unknown_evidence", _tool_trace(results), index)
            if (
                proposal.metric_key is not None
                and proposal.metric_key not in baseline.metrics
                and proposal.metric_key not in baseline.facts
            ):
                return _reject(baseline, policy, "AGENT_REJECTED", "unknown_metric_key", _tool_trace(results), index)
            offending = _find_ungrounded_token(proposal.text, allowed, identifiers)
            if offending is not None:
                return _reject(
                    baseline, policy, "AGENT_REJECTED", "ungrounded_number", _tool_trace(results), index, offending
                )
            gated.append(
                MemoClaim(
                    kind=proposal.kind,
                    text=proposal.text,
                    evidence_ids=tuple(proposal.evidence_ids),
                    metric_key=proposal.metric_key,
                )
            )
        statuses = _agent_statuses(baseline, policy, ["AGENT_OK", *_tool_trace(results)], reason=None)
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


__all__ = ["AGENT_SCHEMAS", "DEFAULT_PROMPT_VERSION", "AgentPolicy", "AgentRunner", "LocalModelClient"]
