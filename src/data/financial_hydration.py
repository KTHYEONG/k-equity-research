"""Event-scoped audit of retained financial evidence with collector-ready gaps."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl

from src.data.catalog import Catalog
from src.data.dart_statements.document_statements import DocumentParseResult
from src.data.event_store import EventStore
from src.data.financial_evidence import _FACT_COLUMNS, FinancialEvidence
from src.data.local_lake import FACTS_DATASET_ID
from src.integrations.dart import FinancialStatementRequest

_FACT_NAMES = frozenset(
    {
        "assets",
        "capex",
        "cash",
        "debt",
        "equity",
        "gross_profit",
        "net_income",
        "operating_cash_flow",
        "operating_profit",
        "sales",
    }
)
_PERIOD_RE = re.compile(r"^(\d{4})Q([1-4])$")
_QUARTER_REPRT = {"1": "11013", "2": "11012", "3": "11014", "4": "11011"}


@dataclass(frozen=True, slots=True)
class HydrationSummary:
    """Separate absent or corrupt source payloads from present but unverified financial rows."""

    event_corp_count: int
    required_hash_count: int
    verified_hash_count: int
    missing_hashes: tuple[str, ...]
    missing_requests: tuple[FinancialStatementRequest, ...]
    unverified_hashes: tuple[str, ...] = ()


def _require_aware(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")


def _check_roots(catalog: Catalog, financial_evidence: FinancialEvidence, data_root: Path) -> None:
    root = data_root.resolve()
    evidence_root = financial_evidence._data_root.resolve()  # noqa: SLF001
    if catalog.db_path.parent.resolve() != root or evidence_root != root:
        raise ValueError("hydration inputs must share one project data root")


def _asof_literal(files: list[str], as_of: datetime) -> datetime:
    dtype = pl.scan_parquet(files[0]).collect_schema()["available_at"]
    zone = dtype.time_zone if isinstance(dtype, pl.Datetime) else None
    return as_of.astimezone(ZoneInfo(zone or "UTC"))


def _derive_request(corp_code: str, fiscal_period: str, consolidated: bool) -> FinancialStatementRequest | None:
    """Map one failed index row to a project-owned collector request, or None when unmappable."""
    corp = corp_code.strip()
    match = _PERIOD_RE.match(fiscal_period.strip())
    if match is None or len(corp) != 8 or not corp.isdigit():
        return None
    year = int(match.group(1))
    if year < 2015 or year > 2100:
        return None
    return FinancialStatementRequest(
        corp_code=corp,
        bsns_year=year,
        reprt_code=_QUARTER_REPRT[match.group(2)],
        fs_div="CFS" if consolidated else "OFS",
    )


def hydrate_event_financial_evidence(
    catalog: Catalog,
    event_store: EventStore,
    financial_evidence: FinancialEvidence,
    data_root: Path,
    as_of: datetime,
) -> HydrationSummary:
    """Verify local evidence for every event issuer and report source gaps.

    Derive the issuer set from verified project-local DART events, not a
    Drive candidate list. Resolve each retained point-in-time fact to a
    local payload and exact amount. Identify requests suitable for the
    project-owned financial collector without changing past availability.
    """
    _require_aware(as_of, "as_of")
    _check_roots(catalog, financial_evidence, data_root)
    issuers = sorted(
        {filing.corp_code for _, filing in event_store.list_prior_events(as_of) if filing.corp_code}
    )
    if not issuers:
        return HydrationSummary(0, 0, 0, (), ())
    lake = financial_evidence._lake  # noqa: SLF001
    try:
        parts = lake.dataset_parts(FACTS_DATASET_ID)
    except ValueError:
        return HydrationSummary(len(issuers), 0, 0, (), ())
    if not parts:
        return HydrationSummary(len(issuers), 0, 0, (), ())
    files = [str(path) for path in parts]
    cutoff = _asof_literal(files, as_of)
    rows = (
        pl.scan_parquet(files)
        .filter(
            (pl.col("dart_corp_code").is_in(issuers))
            & (pl.col("fact").is_in(list(_FACT_NAMES)))
            & (pl.col("available_at") <= cutoff)
        )
        .select(list(_FACT_COLUMNS))
        .collect()
        .to_dicts()
    )
    by_hash: dict[str, list[dict[str, Any]]] = {}
    failed: list[dict[str, Any]] = []
    for row in rows:
        digest = str(row.get("source_hash") or "")
        if digest:
            by_hash.setdefault(digest, []).append(row)
        else:
            failed.append(row)
    verified: set[str] = set()
    missing_source: set[str] = set()
    unverified: set[str] = set()
    document_cache: dict[str, DocumentParseResult | None] = {}
    for digest in sorted(by_hash):
        records = financial_evidence._load_records(digest)  # noqa: SLF001
        if records is None:
            missing_source.add(digest)
            failed.extend(by_hash[digest])
            continue
        resolved = True
        for row in by_hash[digest]:
            if financial_evidence._verify_row(row, records, document_cache) is None:  # noqa: SLF001
                failed.append(row)
                resolved = False
        if resolved:
            verified.add(digest)
        else:
            unverified.add(digest)
    missing = tuple(sorted(missing_source))
    requests = sorted(
        {
            request
            for row in failed
            if (
                request := _derive_request(
                    str(row.get("dart_corp_code") or ""),
                    str(row.get("fiscal_period") or ""),
                    bool(row.get("consolidated")),
                )
            )
            is not None
        },
        key=lambda item: (item.corp_code, item.bsns_year, item.reprt_code, item.fs_div),
    )
    return HydrationSummary(
        event_corp_count=len(issuers),
        required_hash_count=len(by_hash),
        verified_hash_count=len(verified),
        missing_hashes=missing,
        missing_requests=tuple(requests),
        unverified_hashes=tuple(sorted(unverified)),
    )


__all__ = ["HydrationSummary", "hydrate_event_financial_evidence"]
