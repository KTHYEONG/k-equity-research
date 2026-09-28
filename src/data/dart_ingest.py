"""Local ingestion of DART filings with conservative availability."""

from __future__ import annotations

import calendar
import hashlib
import json
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
from src.data.local_paths import checked_local_path
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


_HISTORY_MIN_START = date(2023, 1, 1)
_COMPLETE_JOB = "dart-backfill-complete"
_MAX_REQUEST_DAYS = 93


@dataclass(frozen=True, slots=True)
class HistoricalBackfillSummary:
    """Account for complete DART windows and project-owned source evidence."""

    windows_complete: int
    list_pages_reused: int
    list_pages_fetched: int
    documents_reused: int
    documents_fetched: int
    events_accepted: int


def _next_page_from_cursor(cursor: str | None) -> int:
    if cursor is None or not cursor.startswith("page-"):
        return 1
    try:
        current = int(cursor[len("page-") :])
    except ValueError:
        return 1
    return current + 1 if current >= 1 else 1


def _month_windows(start: date, end: date) -> list[tuple[date, date]]:
    """Split an inclusive date range into calendar-month slices with no gaps or overlap."""
    windows: list[tuple[date, date]] = []
    cursor = date(start.year, start.month, 1)
    first = True
    while True:
        month_end_day = calendar.monthrange(cursor.year, cursor.month)[1]
        month_end = date(cursor.year, cursor.month, month_end_day)
        window_start = start if first else cursor
        window_end = min(month_end, end)
        if window_start <= window_end:
            windows.append((window_start, window_end))
        first = False
        if month_end >= end:
            break
        cursor = date(cursor.year + 1, 1, 1) if cursor.month == 12 else date(cursor.year, cursor.month + 1, 1)
    return windows


def _parse_completion_cursor(cursor: str) -> tuple[int, tuple[str, ...]] | None:
    if not cursor.startswith("complete:"):
        return None
    body = cursor[len("complete:") :]
    pages_text, sep, receipts_text = body.partition(":")
    if not sep:
        return None
    try:
        pages = int(pages_text)
    except ValueError:
        return None
    if pages < 1:
        return None
    if not receipts_text:
        return (pages, ())
    receipts = tuple(part for part in receipts_text.split(",") if part)
    return (pages, receipts)


def _encode_completion_cursor(pages: int, receipts: tuple[str, ...]) -> str:
    return f"complete:{pages}:{','.join(receipts)}"


def _artifact_file_valid(catalog: Catalog, data_root: Path, relative: PurePosixPath, expected_sha: str) -> bool:
    try:
        resolved = checked_local_path(data_root, relative)
    except (ValueError, OSError):
        return False
    if resolved.is_symlink() or not resolved.is_file():
        return False
    try:
        digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
    except OSError:
        return False
    return digest == expected_sha.lower()


def _is_month_complete_valid(
    catalog: Catalog, data_root: Path, window_key: str, snapshot_id: str
) -> tuple[int, tuple[str, ...]] | None:
    """Return (pages, receipts) when the durable completion marker is still verified."""
    raw = catalog.load_checkpoint(_COMPLETE_JOB, window_key, snapshot_id)
    if raw is None:
        return None
    parsed = _parse_completion_cursor(raw)
    if parsed is None:
        return None
    pages, receipts = parsed
    expected_cursor = f"page-{pages}"
    actual_cursor = catalog.load_checkpoint(_JOB, window_key, snapshot_id)
    if actual_cursor != expected_cursor:
        return None
    for page_no in range(1, pages + 1):
        artifact = catalog.find_artifact("dart", "list", f"{window_key}:page-{page_no}", snapshot_id)
        if artifact is None:
            return None
        if not _artifact_file_valid(catalog, data_root, artifact.local_relative_path, artifact.sha256):
            return None
    for rcept_no in receipts:
        artifact = catalog.find_artifact("dart", "document", rcept_no, snapshot_id)
        if artifact is None:
            return None
        if not _artifact_file_valid(catalog, data_root, artifact.local_relative_path, artifact.sha256):
            return None
    return (pages, receipts)


def _resume_page(
    catalog: Catalog,
    request_key: str,
    snapshot_id: str,
) -> int | None:
    """Return the next unfinished page, or None for a completed window.

    A durable completion marker is distinct from a page checkpoint. Its
    validity depends on the verified terminal page and all selected ZIPs.
    """
    data_root = catalog.db_path.parent
    if _is_month_complete_valid(catalog, data_root, request_key, snapshot_id) is not None:
        return None
    cursor = catalog.load_checkpoint(_JOB, request_key, snapshot_id)
    if cursor is None:
        return 1
    if cursor.startswith("complete:"):
        return 1
    return _next_page_from_cursor(cursor)


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
    "HistoricalBackfillSummary",
    "build_filing_version",
    "collect_buyback_history",
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
    page_no = _next_page_from_cursor(cursor)
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


class _HistoryTrackingClient:
    """Observe listed pages and document requests without changing provider contracts."""

    def __init__(self, inner: DartClient) -> None:
        self._inner = inner
        self.doc_requests: list[str] = []

    def list_major_reports(self, start: date, end: date, page: int) -> object:
        return self._inner.list_major_reports(start, end, page)

    def document_zip(self, rcept_no: str) -> bytes:
        self.doc_requests.append(rcept_no)
        return self._inner.document_zip(rcept_no)

    def current_buyback_details(self, corp_code: str, start: date, end: date) -> bytes:
        return self._inner.current_buyback_details(corp_code, start, end)


def _listed_receipts_from_saved_pages(
    catalog: Catalog, data_root: Path, window_key: str, snapshot_id: str, pages: int
) -> tuple[str, ...]:
    """Verify every durable list page and recover the complete selected receipt set."""
    receipts: set[str] = set()
    for page_no in range(1, pages + 1):
        artifact = catalog.find_artifact("dart", "list", f"{window_key}:page-{page_no}", snapshot_id)
        if artifact is None or not _artifact_file_valid(
            catalog, data_root, artifact.local_relative_path, artifact.sha256
        ):
            raise ValueError(f"incomplete source evidence for window {window_key}")
        relative = checked_local_path(data_root, artifact.local_relative_path)
        try:
            document = json.loads(relative.read_bytes().decode("utf-8"))
            if document.get("status") == "010" and pages == 1:
                continue
            if document.get("status") != "000" or int(document["page_no"]) != page_no:
                raise ValueError("list page identity mismatch")
            rows = document["list"]
            if not isinstance(rows, list):
                raise ValueError("invalid list rows")
            for row in rows:
                if _is_buyback_candidate(str(row["report_nm"])) and _is_listed_candidate(
                    str(row["corp_cls"]), str(row["stock_code"])
                ):
                    receipts.add(str(row["rcept_no"]))
        except (OSError, UnicodeError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ValueError(f"invalid retained DART list for window {window_key}") from exc
    return tuple(sorted(receipts))


def _count_filings(catalog: Catalog, receipts: tuple[str, ...]) -> int:
    far_future = datetime.max.replace(tzinfo=UTC)
    total = 0
    for rcept_no in receipts:
        try:
            filing = catalog.get_filing_asof(rcept_no, far_future)
        except (ValueError, OSError):
            continue
        if filing is not None:
            total += 1
    return total


def collect_buyback_history(
    client: DartClient,
    catalog: Catalog,
    lake: LocalLake,
    start: date,
    end: date,
    data_root: Path,
    snapshot_id: str,
    document_limits: DocumentLimits | None = None,
    event_store: EventStore | None = None,
) -> HistoricalBackfillSummary:
    """Collect a resumable official history of buyback disclosures.

    Bound each list request to one calendar month, preserve original list
    responses and receipt ZIPs, and finish a window only after every page
    and selected document is verified. Use HISTORICAL_BACKFILL semantics
    and preserve correction lineage. Raise on incomplete source evidence.
    """
    if start > end:
        raise ValueError("collection window must not be empty")
    if start < _HISTORY_MIN_START:
        raise ValueError("historical backfill starts at 2023-01-01 or later")
    if not snapshot_id or "/" in snapshot_id or snapshot_id in {".", ".."} or ".." in snapshot_id:
        raise ValueError("snapshot id must be non-empty")
    data_root.mkdir(parents=True, exist_ok=True)
    months = _month_windows(start, end)
    if not months:
        raise ValueError("collection window must not be empty")
    for window_start, window_end in months:
        if (window_end - window_start).days > _MAX_REQUEST_DAYS:
            raise ValueError("individual request window exceeds the three-month API limit")
        # Calendar-month slices are at most 31 days, satisfying the provider
        # three-month bound from https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS001&apiId=2019001.

    windows_complete = 0
    list_pages_reused = 0
    list_pages_fetched = 0
    documents_reused = 0
    documents_fetched = 0
    filings_accepted = 0

    for window_start, window_end in months:
        window_key = _window_key(window_start, window_end)
        completion = catalog.load_checkpoint(_COMPLETE_JOB, window_key, snapshot_id)
        verified = _is_month_complete_valid(catalog, data_root, window_key, snapshot_id)
        if verified is not None:
            pages, receipts = verified
            windows_complete += 1
            list_pages_reused += pages
            documents_reused += len(receipts)
            filings_accepted += _count_filings(catalog, receipts)
            continue
        if completion is not None:
            raise ValueError(f"invalid completed DART window {window_key}")
        cursor = catalog.load_checkpoint(_JOB, window_key, snapshot_id)
        if cursor is not None and cursor.startswith("page-"):
            try:
                previous_page = int(cursor[len("page-") :])
            except ValueError as exc:
                raise ValueError(f"invalid DART checkpoint for window {window_key}") from exc
            if previous_page < 1:
                raise ValueError(f"invalid DART checkpoint for window {window_key}")
            catalog.save_checkpoint(_JOB, window_key, snapshot_id, f"page-{previous_page - 1}")
        tracker = _HistoryTrackingClient(client)
        summary = collect_buyback_window(
            tracker,  # type: ignore[arg-type]
            catalog,
            lake,
            window_start,
            window_end,
            data_root,
            snapshot_id,
            "HISTORICAL_BACKFILL",
            document_limits,
            event_store,
        )
        if summary.failed_receipts:
            raise ValueError(f"incomplete source evidence for window {window_key}")
        cursor = catalog.load_checkpoint(_JOB, window_key, snapshot_id)
        if cursor is None or not cursor.startswith("page-"):
            raise ValueError(f"incomplete source evidence for window {window_key}")
        pages = int(cursor[len("page-") :])
        receipts = _listed_receipts_from_saved_pages(catalog, data_root, window_key, snapshot_id, pages)
        # Verify every selected ZIP is durably registered before marking complete.
        for rcept_no in receipts:
            artifact = catalog.find_artifact("dart", "document", rcept_no, snapshot_id)
            if artifact is None:
                raise ValueError(f"incomplete source evidence for window {window_key}")
            if not _artifact_file_valid(catalog, data_root, artifact.local_relative_path, artifact.sha256):
                raise ValueError(f"incomplete source evidence for window {window_key}")
        catalog.save_checkpoint(
            _COMPLETE_JOB, window_key, snapshot_id, _encode_completion_cursor(pages, receipts)
        )
        windows_complete += 1
        list_pages_reused += max(0, pages - summary.pages)
        list_pages_fetched += summary.pages
        documents_fetched += len(set(tracker.doc_requests))
        documents_reused += max(0, len(receipts) - len(set(tracker.doc_requests)))
        filings_accepted += summary.documents_registered

    if event_store is not None:
        try:
            far_future = datetime.max.replace(tzinfo=UTC)
            events_accepted = len(event_store.list_prior_events(far_future))
            # list_prior_events only returns LINKED events with eligible filings;
            # fall back to filing counts when parsing was disabled.
            if document_limits is None and events_accepted == 0:
                events_accepted = filings_accepted
        except (ValueError, OSError):
            events_accepted = filings_accepted
    else:
        events_accepted = filings_accepted

    return HistoricalBackfillSummary(
        windows_complete=windows_complete,
        list_pages_reused=list_pages_reused,
        list_pages_fetched=list_pages_fetched,
        documents_reused=documents_reused,
        documents_fetched=documents_fetched,
        events_accepted=events_accepted,
    )
