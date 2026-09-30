"""Deterministic cited research memo baseline."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import PurePosixPath

from src.core.buyback_document import BuybackFact
from src.research.context import ResearchContext
from src.research.financial_snapshot import build_financial_snapshot

CODE_REVISION = "memo-baseline-v3"

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


def _manifest_hash(context: ResearchContext, analogue_ref: EvidenceRef | None = None) -> str:
    payload = {
        "analogue_proof": analogue_ref.sha256.lower() if analogue_ref is not None else "UNAVAILABLE",
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


def _is_stated_absent(fact: BuybackFact) -> bool:
    """A dash in the optional daily-order-limit cell means the filing states no limit; it is not a parse failure."""
    return fact.field == "BUY_OSTK_LMT" and fact.value_text == "-"


def _format_decimal(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _evidence_by_id(evidence: tuple[EvidenceRef, ...]) -> dict[str, EvidenceRef]:
    return {item.id: item for item in evidence}


def build_baseline_memo(
    context: ResearchContext,
    analogue_ref: EvidenceRef | None = None,
) -> ResearchMemo:
    """Build a memo whose analogue claim exists only when its proof is citable.

    Args:
        context: Deterministic point-in-time research context.
        analogue_ref: Run-local reference to verified analogue proof bytes.

    Returns:
        A memo with unchanged supported calculations and an explicit unavailable
        analogue state when proof is absent.
    """
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
            if fact is not None and fact.status != "NOT_APPLICABLE" and not _is_stated_absent(fact):
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
        "analogue_p25": _format_decimal(context.comparables.analogue_quantiles.get("p25")) if analogue_ref is not None else None,
        "analogue_median": _format_decimal(context.comparables.analogue_quantiles.get("median")) if analogue_ref is not None else None,
        "analogue_p75": _format_decimal(context.comparables.analogue_quantiles.get("p75")) if analogue_ref is not None else None,
        "peer_count": str(len(context.comparables.peer_ids)),
        "analogue_count": str(len(context.comparables.analogue_event_ids)),
        "analogue_outcome_count": str(len(context.comparables.analogue_intraday_excess)),
        "confounding_receipt_count": str(len(context.confounding_receipts)),
    }
    for horizon in sorted(context.study.horizon_car):
        metrics[f"car_h{horizon}"] = _format_decimal(context.study.horizon_car[horizon])
    materiality_hashes = [h for h in context.materiality.source_hashes if h]
    study_hashes = [h for h in context.study.evidence_hashes if h]
    if metrics["amount_to_market_cap"] is not None or metrics["shares_to_listed_shares"] is not None:
        digest = materiality_hashes[0] if materiality_hashes else context.index_manifest_hash
        ref_path = context.artifact_paths.get(digest)
        if ref_path is not None:
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
        ref_path = context.artifact_paths.get(digest)
        if ref_path is not None:
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
        ref_path = context.artifact_paths.get(digest)
        if ref_path is None:
            continue
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
    facts["company_name"] = None
    name_path = context.artifact_paths.get(context.company_name_source_hash or "")
    if context.company_name and context.company_name_source_hash and name_path is not None:
        facts["company_name"] = context.company_name
        evidence.append(
            EvidenceRef(
                id="security-master",
                source_kind="krx_security_master",
                local_relative_path=name_path,
                sha256=context.company_name_source_hash,
                locator=f"ISU_CD={context.security.source_security_id} ISU_ABBRV session={context.security.session.isoformat()}",
            )
        )
        claims.append(
            MemoClaim(
                kind="identity",
                text=f"Observed listed name of {context.filing.stock_code} on {context.security.session.isoformat()} is {context.company_name}.",
                evidence_ids=("security-master",),
                metric_key=None,
            )
        )
    planned_text = facts["planned_amount_krw"]
    snapshot = build_financial_snapshot(
        context.financial_facts,
        context.filing.knowledge_available_at,
        Decimal(planned_text) if planned_text is not None else None,
    )
    facts.update(dict.fromkeys(_FINANCIAL_FACT_KEYS))
    metrics.update(dict.fromkeys(_FINANCIAL_RATIO_KEYS))
    if snapshot is None:
        statuses.add("FINANCIALS_UNAVAILABLE")
    else:
        facts["financials_period"] = snapshot.fiscal_period
        facts["financials_basis"] = snapshot.basis
        provenance = f"{snapshot.fiscal_period} {snapshot.basis}, available {snapshot.available_at.date().isoformat()}"
        fact_ref_ids: dict[str, str] = {}
        for name, fin_fact in sorted(snapshot.facts.items()):
            facts[f"fin_{_FINANCIAL_LABEL[name]}"] = str(fin_fact.value)
            ref_id = f"fin-{name}"
            fact_ref_ids[name] = ref_id
            evidence.append(
                EvidenceRef(
                    id=ref_id,
                    source_kind="financial_bronze",
                    local_relative_path=PurePosixPath("imports/financial_evidence") / fin_fact.source_hash / "payload.json",
                    sha256=fin_fact.source_hash,
                    locator=fin_fact.evidence_key,
                )
            )
            claims.append(
                MemoClaim(
                    kind="financial",
                    text=f"Observed {_FINANCIAL_LABEL[name]} is {fin_fact.value} KRW ({provenance}).",
                    evidence_ids=(ref_id,),
                    metric_key=None,
                )
            )
        amount_ref = ("filing-fact-acq_ostk_prc",) if "filing-fact-acq_ostk_prc" in _evidence_by_id(tuple(evidence)) else ()
        ratio_inputs = {
            "cash_to_assets": ("cash", "assets"),
            "liabilities_to_equity": ("debt", "equity"),
            "amount_to_cash": ("cash",),
            "amount_to_equity": ("equity",),
        }
        for ratio_key, ratio in sorted(snapshot.ratios.items()):
            metrics[ratio_key] = _format_decimal(ratio)
            refs = tuple(fact_ref_ids[name] for name in ratio_inputs[ratio_key])
            claims.append(
                MemoClaim(
                    kind="financial_ratio",
                    text=f"Observed {ratio_key} is {ratio} (deterministic ratio, {provenance}).",
                    evidence_ids=refs + (amount_ref if ratio_key.startswith("amount_") else ()),
                    metric_key=ratio_key,
                )
            )
    if analogue_ref is not None and any(metrics[key] is not None for key in ("analogue_p25", "analogue_median", "analogue_p75")):
        ref_id = "tool-analogues"
        evidence.append(
            EvidenceRef(
                id=ref_id,
                source_kind="tool_result",
                local_relative_path=analogue_ref.local_relative_path,
                sha256=analogue_ref.sha256.lower(),
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
    if analogue_ref is None:
        statuses.add("ANALOGUE_EVIDENCE_UNVERIFIED")
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
    manifest = _manifest_hash(context, analogue_ref)
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


_FINANCIAL_LABEL = {"assets": "assets", "cash": "cash", "debt": "liabilities", "equity": "equity"}
_FINANCIAL_FACT_KEYS = ("financials_period", "financials_basis", "fin_assets", "fin_cash", "fin_equity", "fin_liabilities")
_FINANCIAL_RATIO_KEYS = ("amount_to_cash", "amount_to_equity", "cash_to_assets", "liabilities_to_equity")
_KRW_FACTS = frozenset({"planned_amount_krw", "fin_assets", "fin_cash", "fin_equity", "fin_liabilities"})
_NUMERIC_FACTS = _KRW_FACTS | {"planned_shares", "daily_limit_shares"}
_STATUS_MEANING = {
    "AMOUNT_UNAVAILABLE": "the filing states no common-stock amount (e.g. preferred-share redemption); size ratios are withheld",
    "SHARES_UNAVAILABLE": "the filing states no common-stock quantity (e.g. preferred-share redemption); size ratios are withheld",
    "FINANCIALS_UNAVAILABLE": "no complete KRW balance sheet was observable when the buyback was filed",
    "CONFOUNDED": "material disclosures by the issuer fall inside the study window; movement is not attributable to the buyback alone",
    "LOW_SAMPLE": "too few pre-filing analogues for summary statistics",
    "CONFOUND_CHECK_INCOMPLETE": "concurrent-disclosure coverage is not fully verified",
    "ANALOGUE_EVIDENCE_UNVERIFIED": "analogue statistics are withheld until the analogue proof file is verified",
    "UNVERIFIED": "at least one filing fact could not be verified against the source document",
}
_PERCENT_METRICS = frozenset(
    {"amount_to_cash", "amount_to_equity", "cash_to_assets", "liabilities_to_equity", "amount_to_market_cap", "shares_to_listed_shares", "analogue_median", "analogue_p25", "analogue_p75",
     "car_h1", "car_h5", "car_h20", "intraday_excess_s0"}
)
_RATIO_METRICS = frozenset(
    {"amount_to_market_cap", "shares_to_listed_shares", "amount_to_cash", "amount_to_equity", "cash_to_assets", "liabilities_to_equity"}
)
_CONCURRENT_RE = re.compile(r"^Issuer filed (?P<title>.*) under receipt (?P<rcept>\d{14}) in the event study window\.$")


def _decimal_or_none(text: str | None) -> Decimal | None:
    return None if text is None else Decimal(text)


def _pct(value: Decimal, *, signed: bool) -> str:
    """Percent with two decimals; keep four for tiny non-zero values so they never round to zero."""
    percent = value * 100
    if percent != 0 and abs(percent) < Decimal("0.01"):
        return f"{percent:.4f}%"
    return f"{percent:+.2f}%" if signed else f"{percent:.2f}%"


def _krw(value: Decimal) -> str:
    if value >= Decimal(10) ** 12:
        return f"{value / Decimal(10) ** 12:,.2f}조원"
    if value >= Decimal(10) ** 8:
        return f"{value / Decimal(10) ** 8:,.1f}억원"
    return f"{value:,.0f}원"


def _display_metric(key: str, raw: str | None) -> str:
    value = _decimal_or_none(raw)
    if value is None:
        return "withheld" if raw is None else raw
    if key in _PERCENT_METRICS:
        return _pct(value, signed=key not in _RATIO_METRICS)
    return f"{value:,.0f}" if value == value.to_integral_value() else str(value)


def _display_fact(key: str, raw: str | None) -> str:
    value = _decimal_or_none(raw) if key in _NUMERIC_FACTS else None
    if value is None:
        return "withheld" if raw is None else raw
    return _krw(value) if key in _KRW_FACTS else f"{value:,.0f}"


_DISPLAY_NUMERIC_FACTS = ("planned_amount_krw", "planned_shares", "daily_limit_shares", "fin_assets", "fin_cash", "fin_equity", "fin_liabilities")
_DISPLAY_DATE_FACTS = ("receipt_date", "period_begin", "period_end")
_EXCLUDED_DISPLAY_METRICS = frozenset({"model_alpha", "model_beta"})


def display_figures(memo: ResearchMemo) -> Mapping[str, str]:
    """Return reader-facing display strings for the numeric and date figures of a memo, keyed `fact:<key>` or `metric:<key>`.

    Uses the same formatters as `render_markdown`, so a figure shown to a reader and a figure offered to the local model are
    identical. Withheld (None) values and estimation-window parameters (`model_alpha`, `model_beta`) are omitted. No figure is
    recomputed; exact decimals stay in the structured memo.
    """
    entries: dict[str, str] = {}
    for key in _DISPLAY_NUMERIC_FACTS:
        raw = memo.facts.get(key)
        if raw is None:
            continue
        entries[f"fact:{key}"] = _display_fact(key, raw)
    for key in _DISPLAY_DATE_FACTS:
        raw = memo.facts.get(key)
        if raw is None:
            continue
        entries[f"fact:{key}"] = raw
    for key in sorted(memo.metrics):
        if key in _EXCLUDED_DISPLAY_METRICS:
            continue
        raw = memo.metrics.get(key)
        if raw is None:
            continue
        entries[f"metric:{key}"] = _display_metric(key, raw)
    return dict(sorted(entries.items()))


def _concurrent_lines(memo: ResearchMemo) -> list[str]:
    entries: list[tuple[str, str]] = []
    for claim in memo.claims:
        if claim.kind != "concurrent_disclosure":
            continue
        matched = _CONCURRENT_RE.match(claim.text)
        if matched is None:
            entries.append(("", claim.text))
            continue
        rcept = matched.group("rcept")
        entries.append((f"{rcept[:4]}-{rcept[4:6]}-{rcept[6:8]}", f"{matched.group('title').strip()} ({rcept})"))
    return [f"- {day} {text}".rstrip() if day else f"- {text}" for day, text in sorted(entries)]


def render_markdown(memo: ResearchMemo) -> str:
    """Render a reader-first summary followed by the full audit trail, without recomputing any figure.

    Human-facing figures are display-formatted only; exact decimals stay in the structured memo. Return
    stable Markdown for identical memo inputs.
    """
    facts, metrics = memo.facts, memo.metrics
    lines = [
        f"# Buyback memo: {facts.get('company_name') or facts.get('stock_code') or 'unresolved'}"
        f" ({facts.get('stock_code') or 'withheld'}, {facts.get('market') or 'withheld'}) {memo.event_id}",
        "",
        "Descriptive only. No trade action is suggested. Movement is reported as observed, without stating why it moved.",
        "",
        "## Summary",
        "",
        f"- Filed {facts.get('receipt_date') or 'withheld'}; purpose: {facts.get('purpose_text') or 'withheld'}",
        f"- Planned: {_display_fact('planned_amount_krw', facts.get('planned_amount_krw'))},"
        f" {_display_fact('planned_shares', facts.get('planned_shares'))} shares"
        f" ({_display_metric('amount_to_market_cap', metrics.get('amount_to_market_cap'))} of prior market cap)",
        f"- Buyback period: {facts.get('period_begin') or 'withheld'} to {facts.get('period_end') or 'withheld'}",
        "- Excess return vs index (observed):"
        f" first session {_display_metric('intraday_excess_s0', metrics.get('intraday_excess_s0'))},"
        f" 1d {_display_metric('car_h1', metrics.get('car_h1'))},"
        f" 5d {_display_metric('car_h5', metrics.get('car_h5'))},"
        f" 20d {_display_metric('car_h20', metrics.get('car_h20'))}",
        f"- Balance sheet at filing ({facts.get('financials_period') or 'withheld'}, {facts.get('financials_basis') or 'withheld'}):"
        f" cash {_display_fact('fin_cash', facts.get('fin_cash'))}, equity {_display_fact('fin_equity', facts.get('fin_equity'))},"
        f" liabilities/equity {_display_metric('liabilities_to_equity', metrics.get('liabilities_to_equity'))}",
        f"- Buyback size: {_display_metric('amount_to_cash', metrics.get('amount_to_cash'))} of cash,"
        f" {_display_metric('amount_to_equity', metrics.get('amount_to_equity'))} of equity",
        f"- Prior analogues (first-session excess): n={_display_metric('analogue_outcome_count', metrics.get('analogue_outcome_count'))},"
        f" median {_display_metric('analogue_median', metrics.get('analogue_median'))}"
        f" (p25 {_display_metric('analogue_p25', metrics.get('analogue_p25'))},"
        f" p75 {_display_metric('analogue_p75', metrics.get('analogue_p75'))})",
        "",
        "## Status",
        "",
    ]
    if memo.statuses:
        lines.extend(f"- {status}: {_STATUS_MEANING.get(status, 'see audit trail')}" for status in memo.statuses)
    else:
        lines.append("- OK")
    concurrent = _concurrent_lines(memo)
    if concurrent:
        lines.extend(("", f"## Concurrent disclosures ({len(concurrent)})", "", *concurrent))
    lines.extend(
        (
            "",
            "## Audit trail",
            "",
            f"Anchor receipt: {memo.anchor_rcept_no}",
            f"Active receipt: {memo.active_rcept_no}",
            f"As of: {memo.as_of.isoformat()}",
            f"Manifest: {memo.manifest_hash}",
            "",
            "### Facts",
            "",
        )
    )
    for key in sorted(facts):
        value = facts[key]
        lines.append(f"- {key}: {value if value is not None else 'withheld'}")
    lines.extend(("", "### Metrics", ""))
    for key in sorted(metrics):
        value = metrics[key]
        lines.append(f"- {key}: {value if value is not None else 'withheld'}")
    lines.extend(("", "### Claims", ""))
    if memo.claims:
        for claim in memo.claims:
            refs = ", ".join(claim.evidence_ids)
            metric = claim.metric_key if claim.metric_key is not None else "n/a"
            lines.append(f"- [{claim.kind}/{metric}] {claim.text} (evidence: {refs})")
    else:
        lines.append("- withheld: no verified claim is available")
    lines.extend(("", "### Evidence", ""))
    if memo.evidence:
        lines.extend(
            f"- {item.id}: {item.source_kind} {item.local_relative_path.as_posix()} {item.sha256} {item.locator}"
            for item in memo.evidence
        )
    else:
        lines.append("- withheld: no resolvable source is available")
    if memo.statuses:
        lines.extend(("", "### Statuses", "", *(f"- {status}" for status in memo.statuses)))
    lines.append("")
    return "\n".join(lines)


__all__ = ["CODE_REVISION", "EvidenceRef", "MemoClaim", "ResearchMemo", "build_baseline_memo", "display_figures", "memo_to_dict", "render_markdown"]
