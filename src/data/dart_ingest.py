"""Local ingestion of DART filings with conservative availability."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath
from typing import Literal

from src.core.buyback_document import DocumentLimits, ParsedBuyback, parse_buyback_document
from src.core.time_policy import knowledge_available_at
from src.data.catalog import Catalog, FilingVersion
from src.data.event_store import EventStore
from src.data.local_lake import LocalLake, SecurityMatch
from src.integrations.dart import DartClient, DartSourceError

_EXACT_FORM = "주요사항보고서(자기주식취득결정)"
_PREFIX_RE = re.compile(r"^\s*\[[^\]]*\]\s*")
_WS_RE = re.compile(r"\s+")
_STOCK_RE = re.compile(r"\d{6}")
_JOB = "dart"


@dataclass(frozen=True, slots=True)
class DartListRow:
    """One locally retained DART list row pending calendar validation."""

    rcept_no: str
    corp_code: str
    stock_code: str
    report_name: str
    rcept_date: date
    corp_cls: str = ""
    rm: str = ""


@dataclass(frozen=True, slots=True)
class CollectionSummary:
    """Auditable counts for one DART buyback collection window."""

    snapshot_id: str
    pages: int
    candidates: int
    listed_candidates: int
    excluded_candidates: int
    documents_registered: int
    failed_receipts: int
    checkpoint_cursor: str


def _normalize_title(value: str) -> str:
    text = value
    while True:
        stripped = _PREFIX_RE.sub("", text, count=1)
        if stripped == text:
            return _WS_RE.sub("", text)
        text = stripped


def _is_buyback_candidate(report_name: str) -> bool:
    return _normalize_title(report_name) == _EXACT_FORM


def _is_listed_candidate(corp_cls: str, stock_code: str) -> bool:
    return corp_cls in {"Y", "K"} and _STOCK_RE.fullmatch(stock_code or "") is not None


def _window_key(start: date, end: date) -> str:
    return f"{start.isoformat()}:{end.isoformat()}"


def _resume_page(cursor: str | None) -> int:
    if cursor is None or not cursor.startswith("page-"):
        return 1
    try:
        current = int(cursor[len("page-"):])
    except ValueError:
        return 1
    return current + 1 if current >= 1 else 1


def build_filing_version(
    row: DartListRow,
    next_session: date,
    observed_at: datetime,
    collection_mode: Literal["HISTORICAL_BACKFILL", "LIVE"],
    raw_hash: str,
    time_precision: Literal["DATE_ONLY", "OBSERVED_INSTANT"] = "DATE_ONLY",
) -> FilingVersion:
    """Construct one FilingVersion after the locally validated next exchange session is known."""
    available_at = knowledge_available_at(row.rcept_date, next_session, observed_at, collection_mode)
    correction = bool(re.match(r"^\s*\[[^\]]*정정[^\]]*\]", row.report_name))
    withdrawal = bool(re.match(r"^\s*\[[^\]]*철회[^\]]*\]", row.report_name))
    return FilingVersion(
        rcept_no=row.rcept_no,
        corp_code=row.corp_code,
        receipt_date=row.rcept_date,
        report_name=row.report_name,
        stock_code=row.stock_code,
        raw_hash=raw_hash,
        first_observed_at=observed_at,
        knowledge_available_at=available_at,
        availability_mode=collection_mode,
        correction_flag=correction,
        withdrawal_flag=withdrawal,
        parent_rcept_no=None,
        link_status="WITHDRAWAL" if withdrawal else "CORRECTION" if correction else "ORIGINAL",
        time_precision=time_precision,
    )


__all__ = [
    "CollectionSummary",
    "DartListRow",
    "build_filing_version",
    "collect_buyback_window",
    "resolve_listed_security",
]


def resolve_listed_security(lake: LocalLake, row: DartListRow, as_of: datetime) -> SecurityMatch:
    """Resolve one DART row's candidate stock identity against validated local sessions."""
    next_session = lake.next_session(row.rcept_date)
    prior_session = lake.previous_session(row.rcept_date)
    if next_session is None or prior_session is None:
        return SecurityMatch(
            instrument_id="",
            ticker=row.stock_code,
            market="",
            source_security_id="",
            session=row.rcept_date,
            status="MISSING_LOCAL",
        )
    match = lake.resolve_security(row.stock_code, prior_session, as_of)
    return match


def collect_buyback_window(
    client: DartClient,
    catalog: Catalog,
    lake: LocalLake,
    start: date,
    end: date,
    data_root: Path,
    snapshot_id: str,
    mode: Literal["HISTORICAL_BACKFILL", "LIVE"],
    document_limits: DocumentLimits | None = None,
    event_store: EventStore | None = None,
) -> CollectionSummary:
    """Persist every page and candidate receipt ZIP locally before advancing its checkpoint. Return page, candidate, exclusion and failure counts for an auditable backfill."""
    if start > end:
        raise ValueError("collection window must not be empty")
    if not snapshot_id or "/" in snapshot_id or snapshot_id in {".", ".."} or ".." in snapshot_id:
        raise ValueError("snapshot id must be non-empty")
    data_root.mkdir(parents=True, exist_ok=True)
    window = _window_key(start, end)
    cursor = catalog.load_checkpoint(_JOB, window, snapshot_id)
    page_no = _resume_page(cursor)
    pages = 0
    candidates = 0
    listed = 0
    excluded = 0
    documents = 0
    failed = 0
    last_cursor = cursor or ""
    while True:
        try:
            page = client.list_major_reports(start, end, page_no)
        except DartSourceError:
            failed += 1
            break
        observed_at = datetime.now(UTC)
        listed_rows = [row for row in page.rows if _is_buyback_candidate(row.report_name)]
        candidates += len(listed_rows)
        zips: list[bytes] = []
        page_failed = 0
        page_listed = 0
        page_excluded = 0
        for candidate in listed_rows:
            if not _is_listed_candidate(candidate.corp_cls, candidate.stock_code):
                page_excluded += 1
                continue
            page_listed += 1
            try:
                raw_zip = client.document_zip(candidate.rcept_no)
            except DartSourceError:
                page_failed += 1
                break
            zips.append(raw_zip)
        if page_failed:
            failed += page_failed
            break
        listed += page_listed
        excluded += page_excluded
        page_filings: list[FilingVersion] = []
        page_parsed: list[ParsedBuyback] = []
        page_label = f"{start:%Y%m%d}-{end:%Y%m%d}-p{page_no}"
        page_path = PurePosixPath(f"raw/dart/{snapshot_id}/list-{page_label}.json")
        with catalog.transaction():
            catalog.register_artifact(
                source="dart",
                endpoint="list",
                request_key=f"{window}:page-{page_no}",
                snapshot_id=snapshot_id,
                raw_bytes=page.raw_bytes,
                retrieved_at=observed_at,
                local_relative_path=page_path,
            )
            zip_index = 0
            for row in listed_rows:
                if not _is_listed_candidate(row.corp_cls, row.stock_code):
                    continue
                raw_zip = zips[zip_index]
                zip_index += 1
                doc_path = PurePosixPath(f"raw/dart/{snapshot_id}/doc-{row.rcept_no}.zip")
                raw_hash = catalog.register_artifact(
                    source="dart",
                    endpoint="document",
                    request_key=row.rcept_no,
                    snapshot_id=snapshot_id,
                    raw_bytes=raw_zip,
                    retrieved_at=observed_at,
                    local_relative_path=doc_path,
                )
                next_session = lake.next_session(row.rcept_date)
                if next_session is None:
                    continue
                local_row = DartListRow(
                    rcept_no=row.rcept_no,
                    corp_code=row.corp_code,
                    stock_code=row.stock_code,
                    report_name=row.report_name,
                    rcept_date=row.rcept_date,
                    corp_cls=row.corp_cls,
                    rm=row.rm,
                )
                filing = catalog.get_filing_asof(row.rcept_no, datetime.max.replace(tzinfo=UTC))
                if filing is None:
                    filing = build_filing_version(local_row, next_session, observed_at, mode, raw_hash)
                elif filing.raw_hash != raw_hash:
                    raise ValueError(f"conflicting document bytes for receipt {row.rcept_no}")
                catalog.upsert_filing(filing)
                documents += 1
                if document_limits is not None:
                    parsed = parse_buyback_document(filing, raw_zip, document_limits)
                    page_filings.append(filing)
                    page_parsed.append(parsed)
        if event_store is not None and page_filings:
            event_store.store_parsed_batch(page_filings, page_parsed)
        last_cursor = f"page-{page_no}"
        catalog.save_checkpoint(_JOB, window, snapshot_id, last_cursor)
        pages += 1
        if page.page_count == 0 or page_no >= page.page_count:
            break
        page_no += 1
    return CollectionSummary(
        snapshot_id=snapshot_id,
        pages=pages,
        candidates=candidates,
        listed_candidates=listed,
        excluded_candidates=excluded,
        documents_registered=documents,
        failed_receipts=failed,
        checkpoint_cursor=last_cursor,
    )
