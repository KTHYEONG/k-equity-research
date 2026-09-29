"""Official all-category DART evidence around accepted buyback event windows."""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Literal
from zoneinfo import ZoneInfo

from src.data.catalog import Catalog
from src.data.dart_ingest import build_filing_version
from src.data.event_store import EventStore
from src.data.local_lake import LocalLake
from src.integrations.dart import DartClient, DartSourceError
from src.research.event_study import StudyPolicy

_JOB = "disclosure-context"
_LIST_ENDPOINT = "list"
_DOC_ENDPOINT = "document"
_MAX_REQUEST_DAYS = 93

_CORRECTION_RE = re.compile(r"^\s*\[[^\]]*정정[^\]]*\]")
_WITHDRAWAL_RE = re.compile(r"^\s*\[[^\]]*철회[^\]]*\]")


@dataclass(frozen=True, slots=True)
class DisclosureContextSummary:
    """Account for verified issuer-window list coverage and document gaps."""

    issuer_windows: int
    pages_verified: int
    receipts_discovered: int
    documents_verified: int
    missing_receipts: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CoverageEntry:
    """One retained issuer-window receipt association for subsequent review."""

    event_id: str
    corp_code: str
    window_start: date
    window_end: date
    rcept_no: str
    report_name: str
    receipt_date: date
    correction_flag: bool
    withdrawal_flag: bool
    document_ok: bool


def _require_aware(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")


def _ensure_schema(catalog: Catalog) -> None:
    """Create disclosure coverage tables without touching versioned catalog schemas."""
    with catalog.transaction():
        catalog._conn.execute(  # noqa: SLF001
            """
            CREATE TABLE IF NOT EXISTS disclosure_receipt (
                corp_code TEXT NOT NULL,
                window_start TEXT NOT NULL,
                window_end TEXT NOT NULL,
                snapshot_id TEXT NOT NULL,
                rcept_no TEXT NOT NULL,
                report_name TEXT NOT NULL,
                stock_code TEXT NOT NULL DEFAULT '',
                receipt_date TEXT NOT NULL,
                correction_flag INTEGER NOT NULL,
                withdrawal_flag INTEGER NOT NULL,
                document_ok INTEGER NOT NULL,
                PRIMARY KEY (corp_code, window_start, window_end, snapshot_id, rcept_no)
            )
            """
        )
        catalog._conn.execute(  # noqa: SLF001
            """
            CREATE TABLE IF NOT EXISTS disclosure_event_link (
                event_id TEXT NOT NULL,
                snapshot_id TEXT NOT NULL,
                rcept_no TEXT NOT NULL,
                PRIMARY KEY (event_id, snapshot_id, rcept_no)
            )
            """
        )
        columns = {
            str(row["name"])
            for row in catalog._conn.execute("PRAGMA table_info(disclosure_receipt)").fetchall()  # noqa: SLF001
        }
        if "stock_code" not in columns:
            catalog._conn.execute(  # noqa: SLF001
                "ALTER TABLE disclosure_receipt ADD COLUMN stock_code TEXT NOT NULL DEFAULT ''"
            )


def _study_session_bounds(lake: LocalLake, receipt_date: date, policy: StudyPolicy) -> tuple[date, date] | None:
    """Derive calendar bounds from the actual event-study sessions.

    Walk the verified session calendar back over the estimation span and
    forward over the longest outcome horizon. No independent hardcoded
    confound window is added.
    """
    safe = lake.next_session(receipt_date)
    if safe is None:
        return None
    back_steps = max(0, -policy.estimation_start)
    fwd_steps = max(0, max(policy.horizons) if policy.horizons else 0)
    start = safe
    for _ in range(back_steps):
        previous = lake.previous_session(start)
        if previous is None:
            break
        start = previous
    end = safe
    for _ in range(fwd_steps):
        coming = lake.next_session(end)
        if coming is None:
            break
        end = coming
    return (start, end)


def _split_request_window(start: date, end: date) -> list[tuple[date, date]]:
    """Split one issuer window into consecutive provider-bounded calendar chunks."""
    chunks: list[tuple[date, date]] = []
    cursor = start
    while True:
        chunk_end = min(end, cursor + timedelta(days=_MAX_REQUEST_DAYS - 1))
        chunks.append((cursor, chunk_end))
        if chunk_end >= end:
            return chunks
        cursor = chunk_end + timedelta(days=1)


def _window_key(corp_code: str, start: date, end: date) -> str:
    return f"disclosure:{corp_code}:{start.isoformat()}:{end.isoformat()}"


def _parse_cursor(cursor: str | None) -> tuple[int, bool]:
    """Return (verified pages, terminal) for one issuer-window checkpoint."""
    if cursor is None:
        return (0, False)
    if cursor.startswith("complete:"):
        try:
            return (int(cursor[len("complete:") :]), True)
        except ValueError:
            return (0, False)
    if cursor.startswith("page-"):
        try:
            current = int(cursor[len("page-") :])
        except ValueError:
            return (0, False)
        return (current, False)
    return (0, False)


def _artifact_valid(catalog: Catalog, data_root: Path, snapshot_id: str, endpoint: str, request_key: str) -> bool:
    from src.data.local_paths import checked_local_path

    artifact = catalog.find_artifact("dart", endpoint, request_key, snapshot_id)
    if artifact is None:
        return False
    try:
        resolved = checked_local_path(data_root, artifact.local_relative_path)
    except (ValueError, OSError):
        return False
    if resolved.is_symlink() or not resolved.is_file():
        return False
    try:
        import hashlib

        digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
    except OSError:
        return False
    return digest == artifact.sha256.lower()


def _known_receipts(
    catalog: Catalog, corp_code: str, start: date, end: date, snapshot_id: str
) -> dict[str, dict[str, object]]:
    rows = catalog._conn.execute(  # noqa: SLF001
        "SELECT rcept_no, report_name, stock_code, receipt_date,"
        " correction_flag, withdrawal_flag, document_ok"
        " FROM disclosure_receipt WHERE corp_code=? AND window_start=? AND window_end=? AND snapshot_id=?",
        (corp_code, start.isoformat(), end.isoformat(), snapshot_id),
    ).fetchall()
    return {str(row["rcept_no"]): dict(row) for row in rows}


def get_event_coverage(catalog: Catalog, event_id: str, snapshot_id: str) -> tuple[CoverageEntry, ...]:
    """Expose the retained issuer-window evidence for one event to subsequent review."""
    _ensure_schema(catalog)
    rows = catalog._conn.execute(  # noqa: SLF001
        "SELECT r.rcept_no, r.corp_code, r.window_start, r.window_end, r.report_name,"
        " r.receipt_date, r.correction_flag, r.withdrawal_flag, r.document_ok"
        " FROM disclosure_receipt r JOIN disclosure_event_link l"
        " ON r.rcept_no=l.rcept_no AND r.snapshot_id=l.snapshot_id"
        " WHERE l.event_id=? AND l.snapshot_id=? ORDER BY r.receipt_date, r.rcept_no",
        (event_id, snapshot_id),
    ).fetchall()
    return tuple(
        CoverageEntry(
            event_id=event_id,
            corp_code=str(row["corp_code"]),
            window_start=date.fromisoformat(str(row["window_start"])),
            window_end=date.fromisoformat(str(row["window_end"])),
            rcept_no=str(row["rcept_no"]),
            report_name=str(row["report_name"]),
            receipt_date=date.fromisoformat(str(row["receipt_date"])),
            correction_flag=bool(int(row["correction_flag"])),
            withdrawal_flag=bool(int(row["withdrawal_flag"])),
            document_ok=bool(int(row["document_ok"])),
        )
        for row in rows
    )


def verified_event_confound(
    catalog: Catalog,
    data_root: Path,
    lake: LocalLake,
    event_id: str,
    corp_code: str,
    receipt_date: date,
    excluded_receipts: frozenset[str],
    as_of: datetime,
    policy: StudyPolicy,
) -> tuple[Literal["KNOWN_CLEAR", "KNOWN_CONFOUNDED", "INCOMPLETE"], tuple[tuple[str, str, str], ...]]:
    """Classify an issuer outcome window only after every list page and document is verified."""
    _require_aware(as_of, "as_of")
    bounds = _study_session_bounds(lake, receipt_date, policy)
    if bounds is None or as_of.astimezone(ZoneInfo("Asia/Seoul")).date() < bounds[1]:
        return "INCOMPLETE", ()
    stamp = as_of.astimezone(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d-%H%M%S%z").replace("+", "p")
    ceiling = f"disclosure-context-{stamp}"
    try:
        with closing(sqlite3.connect(f"file:{catalog.db_path}?mode=ro", uri=True)) as connection:
            rows = connection.execute(
                "SELECT DISTINCT snapshot_id FROM checkpoint WHERE job=? AND snapshot_id<=? ORDER BY snapshot_id DESC",
                (_JOB, ceiling),
            ).fetchall()
    except sqlite3.Error:
        return "INCOMPLETE", ()
    candidates = [str(row[0]) for row in rows if str(row[0]).startswith("disclosure-context-")]
    snapshot_id = ""
    for candidate in candidates:
        valid = True
        for start, end in _split_request_window(*bounds):
            window = _window_key(corp_code, start, end)
            pages, terminal = _parse_cursor(catalog.load_checkpoint(_JOB, window, candidate))
            if not terminal or pages < 1 or any(
                not _artifact_valid(catalog, data_root, candidate, _LIST_ENDPOINT, f"{window}:page-{number}")
                for number in range(1, pages + 1)
            ):
                valid = False
                break
        if valid:
            snapshot_id = candidate
            break
    if not snapshot_id:
        return "INCOMPLETE", ()
    entries = get_event_coverage(catalog, event_id, snapshot_id)
    found: dict[str, tuple[str, str, str]] = {}
    for entry in entries:
        if not entry.document_ok or not _artifact_valid(catalog, data_root, snapshot_id, _DOC_ENDPOINT, entry.rcept_no):
            return "INCOMPLETE", ()
        if not (receipt_date <= entry.receipt_date <= bounds[1]) or entry.rcept_no in excluded_receipts:
            continue
        artifact = catalog.find_artifact("dart", _DOC_ENDPOINT, entry.rcept_no, snapshot_id)
        if artifact is None:
            return "INCOMPLETE", ()
        found[entry.rcept_no] = (entry.rcept_no, entry.report_name, artifact.sha256)
    receipts = tuple(found[key] for key in sorted(found))
    return ("KNOWN_CONFOUNDED" if receipts else "KNOWN_CLEAR", receipts)


def _table_row(info: dict[str, object], corp_code: str) -> object:
    """Rebuild a filing-version-compatible row from retained receipt metadata."""
    from types import SimpleNamespace

    return SimpleNamespace(
        rcept_no=str(info["rcept_no"]),
        corp_code=corp_code,
        stock_code=str(info.get("stock_code", "")),
        report_name=str(info["report_name"]),
        rcept_date=date.fromisoformat(str(info["receipt_date"])),
    )


def _upsert_context_filing(
    catalog: Catalog,
    lake: LocalLake,
    row: object,
    raw_hash: str,
    observed_at: datetime,
    mode: Literal["HISTORICAL_BACKFILL", "LIVE"] = "LIVE",
) -> None:
    """Retain one discovered receipt version without projecting it into the past.

    Newly retrieved evidence uses LIVE availability at the collection instant,
    so a later correction can never appear as earlier-known evidence.
    """
    next_session = lake.next_session(row.rcept_date)  # type: ignore[attr-defined]
    if next_session is None:
        return
    existing = catalog.get_filing_asof(str(row.rcept_no), datetime.max.replace(tzinfo=UTC))  # type: ignore[attr-defined]
    if existing is not None:
        if existing.raw_hash != raw_hash:
            raise ValueError(f"conflicting document bytes for receipt {row.rcept_no}")  # type: ignore[attr-defined]
        return
    filing = build_filing_version(row, next_session, observed_at, mode, raw_hash)  # type: ignore[arg-type]
    catalog.upsert_filing(filing)


def collect_event_disclosure_context(
    client: DartClient,
    catalog: Catalog,
    event_store: EventStore,
    lake: LocalLake,
    policy: StudyPolicy,
    data_root: Path,
    as_of: datetime,
    snapshot_id: str,
    event_id: str | None = None,
) -> DisclosureContextSummary:
    """Collect official all-category filings around accepted event windows.

    Derive issuer and date bounds from verified DART events, study horizons,
    and exchange sessions. Persist every list page and original document
    needed to inspect a discovered contemporaneous filing. Report missing
    evidence explicitly and never classify an issuer-window as clear.
    """
    _require_aware(as_of, "as_of")
    if not snapshot_id or "/" in snapshot_id or snapshot_id in {".", ".."} or ".." in snapshot_id:
        raise ValueError("snapshot id must be non-empty")
    data_root.mkdir(parents=True, exist_ok=True)
    _ensure_schema(catalog)

    prior_events = event_store.list_prior_events(as_of)
    if event_id is not None:
        prior_events = tuple((link, filing) for link, filing in prior_events if link.event_id == event_id)
        if not prior_events:
            raise ValueError(f"unknown eligible event: {event_id}")
    # Group accepted events by exact issuer-window identity so overlapping
    # windows share one fetch while keeping every event association.
    groups: dict[tuple[str, date, date], list[str]] = {}
    for link, original in prior_events:
        if catalog.get_filing_asof(original.rcept_no, as_of) is None:
            continue
        bounds = _study_session_bounds(lake, original.receipt_date, policy)
        if bounds is None:
            continue
        window_start, window_end = bounds
        for chunk_start, chunk_end in _split_request_window(window_start, window_end):
            key = (original.corp_code, chunk_start, chunk_end)
            if link.event_id not in groups.setdefault(key, []):
                groups[key].append(link.event_id)

    issuer_windows = 0
    pages_verified = 0
    discovered: set[str] = set()
    doc_ok: set[str] = set()
    missing: set[str] = set()

    for (corp_code, chunk_start, chunk_end), event_ids in sorted(groups.items()):
        window = _window_key(corp_code, chunk_start, chunk_end)
        issuer_windows += 1
        done_pages, terminal = _parse_cursor(catalog.load_checkpoint(_JOB, window, snapshot_id))
        if terminal:
            valid = True
            for page_no in range(1, done_pages + 1):
                if not _artifact_valid(catalog, data_root, snapshot_id, _LIST_ENDPOINT, f"{window}:page-{page_no}"):
                    valid = False
                    break
            if valid:
                pages_verified += done_pages
                known = _known_receipts(catalog, corp_code, chunk_start, chunk_end, snapshot_id)
                for rcept_no, info in known.items():
                    discovered.add(rcept_no)
                    if _artifact_valid(catalog, data_root, snapshot_id, _DOC_ENDPOINT, rcept_no):
                        doc_ok.add(rcept_no)
                        continue
                    try:
                        raw_zip = client.document_zip(rcept_no)
                    except DartSourceError:
                        missing.add(rcept_no)
                        continue
                    observed_at = datetime.now(UTC)
                    doc_path = PurePosixPath(f"raw/disclosure/{snapshot_id}/doc-{rcept_no}.zip")
                    with catalog.transaction():
                        raw_hash = catalog.register_artifact(
                            source="dart",
                            endpoint=_DOC_ENDPOINT,
                            request_key=rcept_no,
                            snapshot_id=snapshot_id,
                            raw_bytes=raw_zip,
                            retrieved_at=observed_at,
                            local_relative_path=doc_path,
                        )
                        catalog._conn.execute(  # noqa: SLF001
                            "UPDATE disclosure_receipt SET document_ok=1 WHERE corp_code=?"
                            " AND window_start=? AND window_end=? AND snapshot_id=? AND rcept_no=?",
                            (
                                corp_code,
                                chunk_start.isoformat(),
                                chunk_end.isoformat(),
                                snapshot_id,
                                rcept_no,
                            ),
                        )
                    _upsert_context_filing(catalog, lake, _table_row(info, corp_code), raw_hash, observed_at)
                    doc_ok.add(rcept_no)
                for event_id in event_ids:
                    with catalog.transaction():
                        for rcept_no in known:
                            catalog._conn.execute(  # noqa: SLF001
                                "INSERT OR IGNORE INTO disclosure_event_link"
                                " (event_id, snapshot_id, rcept_no) VALUES (?, ?, ?)",
                                (event_id, snapshot_id, rcept_no),
                            )
                for rcept_no in known:
                    if rcept_no not in doc_ok:
                        missing.add(rcept_no)
                if missing:
                    raise ValueError(f"incomplete DART documents for issuer window {window}")
                continue
            done_pages, terminal = 0, False

        # Fresh or resumed walk of this issuer-window chunk.
        known = _known_receipts(catalog, corp_code, chunk_start, chunk_end, snapshot_id)
        # Revisit the last verified page: it may have been terminal while a
        # document fetch failed, so requesting page N+1 would skip the gap.
        page_no = done_pages if done_pages >= 1 else 1
        # Verify previously checkpointed pages before resuming.
        restart = False
        for existing_no in range(1, done_pages + 1):
            if not _artifact_valid(catalog, data_root, snapshot_id, _LIST_ENDPOINT, f"{window}:page-{existing_no}"):
                restart = True
                break
        if restart:
            page_no = 1
        fetched = 0
        complete = False
        while True:
            try:
                page = client.list_reports(chunk_start, chunk_end, page_no, corp_code=corp_code)
            except DartSourceError as exc:
                raise ValueError(f"incomplete DART list for issuer window {window}, page {page_no}: {exc.status}") from exc
            observed_at = datetime.now(UTC)
            issuer_rows = [row for row in page.rows if str(row.corp_code) == corp_code]
            zips: list[bytes] = []
            failed_here: list[str] = []
            for row in issuer_rows:
                rcept_no = str(row.rcept_no)
                if _artifact_valid(catalog, data_root, snapshot_id, _DOC_ENDPOINT, rcept_no):
                    zips.append(b"")
                    continue
                try:
                    zips.append(client.document_zip(rcept_no))
                except DartSourceError:
                    zips.append(b"")
                    failed_here.append(rcept_no)
            page_label = f"ctx-{corp_code}-{chunk_start:%Y%m%d}-{chunk_end:%Y%m%d}-p{page_no}"
            page_path = PurePosixPath(f"raw/disclosure/{snapshot_id}/list-{page_label}.json")
            with catalog.transaction():
                catalog.register_artifact(
                    source="dart",
                    endpoint=_LIST_ENDPOINT,
                    request_key=f"{window}:page-{page_no}",
                    snapshot_id=snapshot_id,
                    raw_bytes=page.raw_bytes,
                    retrieved_at=observed_at,
                    local_relative_path=page_path,
                )
                zip_index = 0
                for row in issuer_rows:
                    rcept_no = str(row.rcept_no)
                    raw_zip = zips[zip_index]
                    zip_index += 1
                    report_name = str(row.report_name)
                    catalog._conn.execute(  # noqa: SLF001
                        "INSERT OR IGNORE INTO disclosure_receipt (corp_code, window_start,"
                        " window_end, snapshot_id, rcept_no, report_name, stock_code,"
                        " receipt_date, correction_flag, withdrawal_flag, document_ok)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
                        (
                            corp_code,
                            chunk_start.isoformat(),
                            chunk_end.isoformat(),
                            snapshot_id,
                            rcept_no,
                            report_name,
                            str(row.stock_code),
                            row.rcept_date.isoformat(),
                            int(bool(_CORRECTION_RE.match(report_name))),
                            int(bool(_WITHDRAWAL_RE.match(report_name))),
                        ),
                    )
                    if rcept_no in failed_here:
                        continue
                    if not raw_zip:
                        artifact = catalog.find_artifact("dart", _DOC_ENDPOINT, rcept_no, snapshot_id)
                        if artifact is None:
                            continue
                        catalog._conn.execute(  # noqa: SLF001
                            "UPDATE disclosure_receipt SET document_ok=1 WHERE corp_code=?"
                            " AND window_start=? AND window_end=? AND snapshot_id=? AND rcept_no=?",
                            (
                                corp_code,
                                chunk_start.isoformat(),
                                chunk_end.isoformat(),
                                snapshot_id,
                                rcept_no,
                            ),
                        )
                        _upsert_context_filing(catalog, lake, row, artifact.sha256, observed_at)
                        continue
                    doc_path = PurePosixPath(f"raw/disclosure/{snapshot_id}/doc-{rcept_no}.zip")
                    raw_hash = catalog.register_artifact(
                        source="dart",
                        endpoint=_DOC_ENDPOINT,
                        request_key=rcept_no,
                        snapshot_id=snapshot_id,
                        raw_bytes=raw_zip,
                        retrieved_at=observed_at,
                        local_relative_path=doc_path,
                    )
                    catalog._conn.execute(  # noqa: SLF001
                        "UPDATE disclosure_receipt SET document_ok=1 WHERE corp_code=?"
                        " AND window_start=? AND window_end=? AND snapshot_id=? AND rcept_no=?",
                        (
                            corp_code,
                            chunk_start.isoformat(),
                            chunk_end.isoformat(),
                            snapshot_id,
                            rcept_no,
                        ),
                    )
                    _upsert_context_filing(catalog, lake, row, raw_hash, observed_at)
            catalog.save_checkpoint(_JOB, window, snapshot_id, f"page-{page_no}")
            fetched += 1
            if page.page_count == 0 or page_no >= page.page_count:
                complete = True
                break
            page_no += 1
        if complete:
            known = _known_receipts(catalog, corp_code, chunk_start, chunk_end, snapshot_id)
            if any(
                info.get("document_ok") != 1
                or not _artifact_valid(catalog, data_root, snapshot_id, _DOC_ENDPOINT, rcept_no)
                for rcept_no, info in known.items()
            ):
                raise ValueError(f"incomplete DART documents for issuer window {window}")
            # Reconcile the exact verified page count from durable artifacts.
            total_pages = 0
            probe = 1
            while _artifact_valid(catalog, data_root, snapshot_id, _LIST_ENDPOINT, f"{window}:page-{probe}"):
                total_pages = probe
                probe += 1
            catalog.save_checkpoint(_JOB, window, snapshot_id, f"complete:{total_pages}")
            pages_verified += total_pages
        else:
            pages_verified += fetched
        known = _known_receipts(catalog, corp_code, chunk_start, chunk_end, snapshot_id)
        for event_id in event_ids:
            with catalog.transaction():
                for rcept_no in known:
                    catalog._conn.execute(  # noqa: SLF001
                        "INSERT OR IGNORE INTO disclosure_event_link"
                        " (event_id, snapshot_id, rcept_no) VALUES (?, ?, ?)",
                        (event_id, snapshot_id, rcept_no),
                    )
        for rcept_no, info in known.items():
            discovered.add(rcept_no)
            if _artifact_valid(catalog, data_root, snapshot_id, _DOC_ENDPOINT, rcept_no) and (
                info.get("document_ok") == 1
            ):
                doc_ok.add(rcept_no)
            else:
                missing.add(rcept_no)

    return DisclosureContextSummary(
        issuer_windows=issuer_windows,
        pages_verified=pages_verified,
        receipts_discovered=len(discovered),
        documents_verified=len(doc_ok),
        missing_receipts=tuple(sorted(missing - doc_ok)),
    )


__all__ = [
    "CoverageEntry",
    "DisclosureContextSummary",
    "collect_event_disclosure_context",
    "get_event_coverage",
    "verified_event_confound",
]
