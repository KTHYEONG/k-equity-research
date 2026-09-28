"""Bounded read-only tools over validated research context."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

from src.research.context import ResearchContext

ToolName = Literal[
    "get_event",
    "get_financial_asof",
    "get_market_window",
    "get_peers",
    "get_analogues",
    "get_revision_diff",
]

_ALLOWED_ARGS: dict[str, frozenset[str]] = {
    "get_event": frozenset(),
    "get_financial_asof": frozenset({"fact"}),
    "get_market_window": frozenset({"start", "end"}),
    "get_peers": frozenset(),
    "get_analogues": frozenset(),
    "get_revision_diff": frozenset(),
}


@dataclass(frozen=True, slots=True)
class ToolRequest:
    """Frozen typed request for one bounded context read."""

    name: ToolName
    arguments: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class ToolResult:
    """Frozen cited result produced only from validated context."""

    name: ToolName
    payload: Mapping[str, object]
    evidence_ids: tuple[str, ...]
    as_of: datetime


def _event_payload(context: ResearchContext) -> tuple[dict[str, object], tuple[str, ...]]:
    payload: dict[str, object] = {
        "active_rcept_no": context.active_rcept_no,
        "anchor_rcept_no": context.anchor_rcept_no,
        "event_id": context.event.event_id,
        "rcept_nos": list(context.event.rcept_nos),
        "status": context.event.status,
    }
    return payload, tuple(sorted(f"filing:{rcept_no}" for rcept_no in context.event.rcept_nos))


def _financial_payload(
    context: ResearchContext, arguments: Mapping[str, str]
) -> tuple[dict[str, object], tuple[str, ...]]:
    wanted = arguments.get("fact")
    selected = context.financial_facts if wanted is None else tuple(f for f in context.financial_facts if f.fact == wanted)
    rows = [
        {
            "evidence_key": fact.evidence_key,
            "fact": fact.fact,
            "filing_id": fact.filing_id,
            "fiscal_period": fact.fiscal_period,
            "unit": fact.unit,
            "value": str(fact.value),
        }
        for fact in selected
    ]
    payload: dict[str, object] = {"count": len(rows), "fact_filter": wanted or "", "facts": rows}
    return payload, tuple(sorted({fact.evidence_key for fact in selected}))


def _market_payload(
    context: ResearchContext, arguments: Mapping[str, str]
) -> tuple[dict[str, object], tuple[str, ...]]:
    raw_start = arguments.get("start")
    raw_end = arguments.get("end")
    if raw_start is None or raw_end is None:
        raise ValueError("market window requires start and end dates")
    start = date.fromisoformat(raw_start)
    end = date.fromisoformat(raw_end)
    if start > end:
        raise ValueError("market window start must not be after end")
    today = context.as_of.date()
    if start > today or end > today:
        raise ValueError("market window must not extend beyond as_of")
    stock = [bar for bar in context.stock_bars if start <= bar.session <= end and bar.available_at <= context.as_of]
    index = [
        bar for bar in context.index_bars if start <= bar.session <= end and bar.batch_available_at <= context.as_of
    ]
    payload: dict[str, object] = {
        "end": end.isoformat(),
        "index_count": len(index),
        "index_sessions": sorted(bar.session.isoformat() for bar in index),
        "start": start.isoformat(),
        "stock_count": len(stock),
        "stock_sessions": sorted(bar.session.isoformat() for bar in stock),
    }
    return payload, tuple(sorted({bar.source_hash for bar in stock} | {bar.source_hash for bar in index}))


def _peers_payload(context: ResearchContext) -> tuple[dict[str, object], tuple[str, ...]]:
    payload: dict[str, object] = {
        "feature_end_session": context.comparables.feature_end_session.isoformat(),
        "peer_ids": list(context.comparables.peer_ids),
        "status": context.comparables.status,
    }
    return payload, ()


def _analogues_payload(context: ResearchContext) -> tuple[dict[str, object], tuple[str, ...]]:
    payload: dict[str, object] = {
        "analogue_event_ids": list(context.comparables.analogue_event_ids),
        "outcome_count": len(context.comparables.analogue_intraday_excess),
        "quantiles": {
            key: None if value is None else str(value) for key, value in context.comparables.analogue_quantiles.items()
        },
        "status": context.comparables.status,
    }
    return payload, ()


def _revision_payload(context: ResearchContext) -> tuple[dict[str, object], tuple[str, ...]]:
    rows = [
        {
            "field": fact.field,
            "member": fact.evidence.member_name,
            "rcept_no": fact.evidence.rcept_no,
            "source_key": fact.evidence.source_key,
            "status": fact.status,
            "unit": fact.unit or "",
            "value": str(fact.value_decimal) if fact.value_decimal is not None else (fact.value_text or ""),
        }
        for fact in context.parsed.facts
    ]
    payload: dict[str, object] = {
        "active_rcept_no": context.active_rcept_no,
        "anchor_rcept_no": context.anchor_rcept_no,
        "facts": rows,
        "rcept_nos": list(context.event.rcept_nos),
    }
    evidence = tuple(
        sorted({f"filing:{rcept_no}:{fact.field}" for rcept_no in context.event.rcept_nos for fact in context.parsed.facts})
    )
    return payload, evidence


def execute_tool(request: ToolRequest, context: ResearchContext) -> ToolResult:
    """Read only bounded, previously validated local research context at its fixed as_of instant. Reject unknown tools, arguments, time expansion and source paths; return cited deterministic results without asking the model to calculate."""
    allowed = _ALLOWED_ARGS.get(request.name)
    if allowed is None:
        raise ValueError(f"unknown tool: {request.name}")
    for key, value in request.arguments.items():
        if key not in allowed:
            raise ValueError(f"unknown argument: {key}")
        if not isinstance(value, str) or not value or ".." in value or "/" in value or "\\" in value:
            raise ValueError(f"invalid argument value for {key}")
    if request.name == "get_event":
        payload, evidence = _event_payload(context)
    elif request.name == "get_financial_asof":
        payload, evidence = _financial_payload(context, request.arguments)
    elif request.name == "get_market_window":
        payload, evidence = _market_payload(context, request.arguments)
    elif request.name == "get_peers":
        payload, evidence = _peers_payload(context)
    elif request.name == "get_analogues":
        payload, evidence = _analogues_payload(context)
    else:
        payload, evidence = _revision_payload(context)
    return ToolResult(name=request.name, payload=payload, evidence_ids=evidence, as_of=context.as_of)


__all__ = ["ToolName", "ToolRequest", "ToolResult", "execute_tool"]
