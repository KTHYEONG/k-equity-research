"""Deterministic cited research memo baseline."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import PurePosixPath

from src.research.context import ResearchContext

CODE_REVISION = "memo-baseline-v1"

_FACT_KEYS: dict[str, str] = {
    "ACQ_OSTK_PRC": "planned_amount_krw",
    "ACQ_OSTK": "planned_shares",
    "ACQ_PPS": "purpose_text",
    "ACQ_BGN": "period_begin",
    "ACQ_END": "period_end",
    "BUY_OSTK_LMT": "daily_limit_shares",
}


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    """Frozen resolvable reference to a local primary source or tool result."""

    id: str
    source_kind: str
    local_relative_path: PurePosixPath
    sha256: str
    locator: str


@dataclass(frozen=True, slots=True)
class MemoClaim:
    """Frozen descriptive claim anchored to resolvable evidence."""

    kind: str
    text: str
    evidence_ids: tuple[str, ...]
    metric_key: str | None


@dataclass(frozen=True, slots=True)
class ResearchMemo:
    """Frozen reproducible cited memo for one filing receipt."""

    event_id: str
    anchor_rcept_no: str
    active_rcept_no: str
    as_of: datetime
    facts: Mapping[str, str | None]
    metrics: Mapping[str, str | None]
    claims: tuple[MemoClaim, ...]
    evidence: tuple[EvidenceRef, ...]
    statuses: tuple[str, ...]
    manifest_hash: str


def _manifest_hash(context: ResearchContext) -> str:
    payload = {
        "code_revision": CODE_REVISION,
        "index_manifest_hash": context.index_manifest_hash.lower(),
        "policy_version": context.study.policy_version,
        "snapshot_ids": sorted(context.snapshot_ids),
        "source_hashes": sorted(context.source_hashes),
    }
    raw = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _filing_artifact(context: ResearchContext) -> tuple[PurePosixPath | None, str]:
    for digest in (context.parsed.document_hash, context.filing.raw_hash):
        path = context.artifact_paths.get(digest)
        if path is not None:
            return path, digest
    return None, context.parsed.document_hash


def _format_decimal(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _evidence_by_id(evidence: tuple[EvidenceRef, ...]) -> dict[str, EvidenceRef]:
    return {item.id: item for item in evidence}


def build_baseline_memo(context: ResearchContext) -> ResearchMemo:
    """Render verified filing facts and deterministic research metrics into a reproducible cited memo. Omit unverified values and attach explicit uncertainty statuses instead of filling gaps with prose or invented numbers."""
    if context.as_of.tzinfo is None or context.as_of.utcoffset() is None:
        raise ValueError("as-of instant must be timezone-aware")
    filing_path, filing_digest = _filing_artifact(context)
    facts: dict[str, str | None] = {
        "planned_amount_krw": None,
        "planned_shares": None,
        "purpose_text": None,
        "period_begin": None,
        "period_end": None,
        "daily_limit_shares": None,
        "receipt_date": context.filing.receipt_date.isoformat(),
        "corp_code": context.filing.corp_code,
        "stock_code": context.filing.stock_code,
        "market": context.security.market,
    }
    evidence: list[EvidenceRef] = []
    claims: list[MemoClaim] = []
    statuses: set[str] = set()
    by_field = {fact.field: fact for fact in context.parsed.facts}
    if filing_path is None:
        statuses.add("UNRESOLVED_LINK")
    for field in sorted(_FACT_KEYS):
        key = _FACT_KEYS[field]
        fact = by_field.get(field)
        if fact is None or fact.status != "VERIFIED":
            facts[key] = None
            if fact is not None and fact.status != "NOT_APPLICABLE":
                statuses.add("UNVERIFIED")
            continue
        if fact.value_decimal is not None:
            text_value: str | None = str(fact.value_decimal)
        else:
            text_value = fact.value_text
        if not text_value:
            facts[key] = None
            statuses.add("UNVERIFIED")
            continue
        if filing_path is None:
            facts[key] = None
            continue
        facts[key] = text_value
        locator = (
            f"{fact.evidence.member_name} {fact.evidence.source_kind}:{fact.evidence.source_key} "
            f"section={fact.evidence.section} table={fact.evidence.table} "
            f"cell={fact.evidence.cell} rcept={fact.evidence.rcept_no}"
        )
        ref_id = f"filing-fact-{field.lower()}"
        evidence.append(
            EvidenceRef(
                id=ref_id,
                source_kind="dart_filing_zip",
                local_relative_path=filing_path,
                sha256=filing_digest.lower(),
                locator=locator,
            )
        )
        claims.append(
            MemoClaim(
                kind="filing_fact",
                text=f"Observed filing fact {field} is {text_value}.",
                evidence_ids=(ref_id,),
                metric_key=None,
            )
        )
    metrics: dict[str, str | None] = {
        "amount_to_market_cap": _format_decimal(context.materiality.amount_to_market_cap),
        "shares_to_listed_shares": _format_decimal(context.materiality.shares_to_listed_shares),
        "intraday_excess_s0": _format_decimal(context.study.intraday_excess),
        "model_alpha": _format_decimal(context.study.model_alpha),
        "model_beta": _format_decimal(context.study.model_beta),
        "analogue_p25": _format_decimal(context.comparables.analogue_quantiles.get("p25")),
        "analogue_median": _format_decimal(context.comparables.analogue_quantiles.get("median")),
        "analogue_p75": _format_decimal(context.comparables.analogue_quantiles.get("p75")),
        "peer_count": str(len(context.comparables.peer_ids)),
        "analogue_count": str(len(context.comparables.analogue_event_ids)),
        "analogue_outcome_count": str(len(context.comparables.analogue_intraday_excess)),
        "confounding_receipt_count": str(len(context.confounding_receipts)),
    }
    for horizon in sorted(context.study.horizon_car):
        metrics[f"car_h{horizon}"] = _format_decimal(context.study.horizon_car[horizon])
    tool_path = filing_path if filing_path is not None else PurePosixPath("tool-result/memo-baseline.json")
    materiality_hashes = [h for h in context.materiality.source_hashes if h]
    study_hashes = [h for h in context.study.evidence_hashes if h]
    if metrics["amount_to_market_cap"] is not None or metrics["shares_to_listed_shares"] is not None:
        digest = materiality_hashes[0] if materiality_hashes else context.index_manifest_hash
        ref_path = context.artifact_paths.get(digest, tool_path)
        ref_id = "tool-materiality"
        if ref_id not in _evidence_by_id(tuple(evidence)):
            evidence.append(
                EvidenceRef(
                    id=ref_id,
                    source_kind="tool_result",
                    local_relative_path=ref_path,
                    sha256=digest.lower(),
                    locator=f"materiality policy={context.study.policy_version} basis=prior-session-denominator",
                )
            )
        if metrics["amount_to_market_cap"] is not None:
            claims.append(
                MemoClaim(
                    kind="materiality",
                    text=f"Observed planned amount relative to prior market value is {metrics['amount_to_market_cap']} (KRW basis, prior session).",
                    evidence_ids=(ref_id,),
                    metric_key="amount_to_market_cap",
                )
            )
        if metrics["shares_to_listed_shares"] is not None:
            claims.append(
                MemoClaim(
                    kind="materiality",
                    text=f"Observed planned share quantity relative to listed shares is {metrics['shares_to_listed_shares']} (shares basis, prior session).",
                    evidence_ids=(ref_id,),
                    metric_key="shares_to_listed_shares",
                )
            )
    if metrics["intraday_excess_s0"] is not None:
        digest = study_hashes[0] if study_hashes else context.index_manifest_hash
        ref_path = context.artifact_paths.get(digest, tool_path)
        ref_id = "tool-intraday-s0"
        evidence.append(
            EvidenceRef(
                id=ref_id,
                source_kind="tool_result",
                local_relative_path=ref_path,
                sha256=digest.lower(),
                locator=f"study policy={context.study.policy_version} window=first-safe-session-intraday",
            )
        )
        session = context.study.first_safe_session
        session_text = session.isoformat() if session is not None else "withheld session"
        claims.append(
            MemoClaim(
                kind="price_path",
                text=f"Observed intraday movement on {session_text} is {metrics['intraday_excess_s0']} (stock open-to-close minus index open-to-close, decimal).",
                evidence_ids=(ref_id,),
                metric_key="intraday_excess_s0",
            )
        )
    for horizon in sorted(context.study.horizon_car):
        value = metrics[f"car_h{horizon}"]
        if value is None:
            continue
        digest = study_hashes[0] if study_hashes else context.index_manifest_hash
        ref_path = context.artifact_paths.get(digest, tool_path)
        ref_id = f"tool-car-h{horizon}"
        evidence.append(
            EvidenceRef(
                id=ref_id,
                source_kind="tool_result",
                local_relative_path=ref_path,
                sha256=digest.lower(),
                locator=f"study policy={context.study.policy_version} window=s0+1-to-s0+{horizon}",
            )
        )
        claims.append(
            MemoClaim(
                kind="price_path",
                text=f"Observed cumulative movement over the next {horizon} session(s) is {value} (decimal sum, descriptive only).",
                evidence_ids=(ref_id,),
                metric_key=f"car_h{horizon}",
            )
        )
    if any(metrics[key] is not None for key in ("analogue_p25", "analogue_median", "analogue_p75")):
        ref_id = "tool-analogues"
        evidence.append(
            EvidenceRef(
                id=ref_id,
                source_kind="tool_result",
                local_relative_path=tool_path,
                sha256=context.index_manifest_hash.lower(),
                locator=f"comparables outcomes={len(context.comparables.analogue_intraday_excess)} basis=pre-filing-only",
            )
        )
        claims.append(
            MemoClaim(
                kind="peer_context",
                text=(
                    f"Observed prior analogue intraday movements number "
                    f"{len(context.comparables.analogue_intraday_excess)} with "
                    f"p25={metrics['analogue_p25']}, median={metrics['analogue_median']}, "
                    f"p75={metrics['analogue_p75']} (decimals, pre-filing outcomes only)."
                ),
                evidence_ids=(ref_id,),
                metric_key="analogue_median",
            )
        )
    if context.event.status in ("UNRESOLVED_LINK", "WITHDRAWN"):
        statuses.add(context.event.status)
    for receipt_no, report_name, digest in context.confounding_receipts:
        path = context.artifact_paths.get(digest)
        if path is None:
            statuses.add("CONFOUND_CHECK_INCOMPLETE")
            continue
        ref_id = f"confound-{receipt_no}"
        evidence.append(EvidenceRef(ref_id, "dart_filing_zip", path, digest, f"rcept={receipt_no}"))
        claims.append(MemoClaim(
            "concurrent_disclosure",
            f"Issuer filed {report_name} under receipt {receipt_no} in the event study window.",
            (ref_id,), None,
        ))
    for reason in context.study.reasons:
        if reason == "TIME_AMBIGUOUS":
            statuses.add("TIME_AMBIGUOUS")
        elif reason.startswith("PENDING_H") or reason == "PENDING":
            statuses.add("PENDING")
        elif reason.startswith("NOT_ESTIMABLE"):
            statuses.add("NOT_ESTIMABLE")
        elif reason == "CONFOUND_CHECK_INCOMPLETE":
            statuses.add("CONFOUND_CHECK_INCOMPLETE")
        elif (
            reason in ("CONFOUNDED", "INSUFFICIENT_PAIRS", "ZERO_MARKET_VARIANCE")
            or reason.startswith("CORPORATE_ACTION_BREAK_")
        ):
            statuses.add(reason)
        else:
            statuses.add(reason)
    if context.materiality.status and context.materiality.status != "OK":
        for part in context.materiality.status.split(";"):
            if part:
                statuses.add(part)
    if context.comparables.status and context.comparables.status != "OK":
        for part in context.comparables.status.split(";"):
            if part:
                statuses.add(part)
    if metrics["amount_to_market_cap"] is None and "ACQ_OSTK_PRC" in by_field:
        statuses.add("UNVERIFIED" if by_field["ACQ_OSTK_PRC"].status != "VERIFIED" else "NOT_ESTIMABLE")
    evidence.sort(key=lambda item: item.id)
    claims.sort(key=lambda item: (item.kind, item.metric_key or "", item.text))
    ordered_statuses = tuple(sorted(statuses))
    manifest = _manifest_hash(context)
    return ResearchMemo(
        event_id=context.event.event_id,
        anchor_rcept_no=context.anchor_rcept_no,
        active_rcept_no=context.active_rcept_no,
        as_of=context.as_of,
        facts=facts,
        metrics=metrics,
        claims=tuple(claims),
        evidence=tuple(evidence),
        statuses=ordered_statuses,
        manifest_hash=manifest,
    )


def memo_to_dict(memo: ResearchMemo) -> dict[str, object]:
    """Return a stable JSON-ready mapping for one validated memo without recomputing figures."""
    return {
        "active_rcept_no": memo.active_rcept_no,
        "anchor_rcept_no": memo.anchor_rcept_no,
        "as_of": memo.as_of.isoformat(),
        "claims": [
            {"evidence_ids": list(claim.evidence_ids), "kind": claim.kind, "metric_key": claim.metric_key, "text": claim.text}
            for claim in memo.claims
        ],
        "event_id": memo.event_id,
        "evidence": [
            {
                "id": item.id,
                "local_relative_path": item.local_relative_path.as_posix(),
                "locator": item.locator,
                "sha256": item.sha256,
                "source_kind": item.source_kind,
            }
            for item in memo.evidence
        ],
        "facts": {key: memo.facts[key] for key in sorted(memo.facts)},
        "manifest_hash": memo.manifest_hash,
        "metrics": {key: memo.metrics[key] for key in sorted(memo.metrics)},
        "statuses": list(memo.statuses),
    }


def render_markdown(memo: ResearchMemo) -> str:
    """Render the validated structured memo for human review without recomputing any figure. Return stable Markdown for identical memo inputs."""
    lines = [
        f"# Baseline memo {memo.event_id}",
        "",
        "Descriptive baseline only. No trade action is suggested. Observed movement is described as observed without stating why it moved.",
        "",
        f"Anchor receipt: {memo.anchor_rcept_no}",
        f"Active receipt: {memo.active_rcept_no}",
        f"As of: {memo.as_of.isoformat()}",
        f"Manifest: {memo.manifest_hash}",
        "",
        "## Facts",
        "",
    ]
    for key in sorted(memo.facts):
        value = memo.facts[key]
        lines.append(f"- {key}: {value if value is not None else 'withheld'}")
    lines.extend(("", "## Metrics", ""))
    for key in sorted(memo.metrics):
        value = memo.metrics[key]
        lines.append(f"- {key}: {value if value is not None else 'withheld'}")
    lines.extend(("", "## Claims", ""))
    if memo.claims:
        for claim in memo.claims:
            refs = ", ".join(claim.evidence_ids)
            metric = claim.metric_key if claim.metric_key is not None else "n/a"
            lines.append(f"- [{claim.kind}/{metric}] {claim.text} (evidence: {refs})")
    else:
        lines.append("- withheld: no verified claim is available")
    lines.extend(("", "## Evidence", ""))
    if memo.evidence:
        lines.extend(
            f"- {item.id}: {item.source_kind} {item.local_relative_path.as_posix()} {item.sha256} {item.locator}"
            for item in memo.evidence
        )
    else:
        lines.append("- withheld: no resolvable source is available")
    lines.extend(("", "## Statuses", ""))
    if memo.statuses:
        lines.extend(f"- {status}" for status in memo.statuses)
    else:
        lines.append("- OK")
    lines.append("")
    return "\n".join(lines)


__all__ = ["CODE_REVISION", "EvidenceRef", "MemoClaim", "ResearchMemo", "build_baseline_memo", "memo_to_dict", "render_markdown"]
