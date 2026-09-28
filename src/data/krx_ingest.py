"""Local collection of official KRX daily index responses."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Literal, cast

from src.data.catalog import Catalog
from src.integrations.krx_index import KrxIndexClient, KrxSourceError, parse_index_day

_JOB = "krx_index"
_MARKETS = ("KOSPI", "KOSDAQ")


@dataclass(frozen=True, slots=True)
class IndexCollectionSummary:
    """Auditable counts for one KRX index collection range."""

    snapshot_id: str
    market: str
    requested_sessions: int
    bars_registered: int
    missing_sessions: int
    failed_sessions: int
    checkpoint_cursor: str


def _window_key(market: str, start: date, end: date) -> str:
    return f"{market}:{start.isoformat()}:{end.isoformat()}"


def _resume_after(cursor: str | None) -> date | None:
    if cursor is None or not cursor:
        return None
    try:
        return date.fromisoformat(cursor) + timedelta(days=1)
    except ValueError:
        return None


def collect_index_range(
    client: KrxIndexClient,
    catalog: Catalog,
    market: str,
    start: date,
    end: date,
    data_root: Path,
    snapshot_id: str,
) -> IndexCollectionSummary:
    """Fetch each requested session's official index response and register validated bars locally. Advance the checkpoint only past registered bars; record unavailable days as missing with their typed reason and stop before transport or validation failures."""
    if market not in _MARKETS:
        raise ValueError("market must be KOSPI or KOSDAQ")
    if start > end:
        raise ValueError("collection range must not be empty")
    if not snapshot_id or "/" in snapshot_id or snapshot_id in {".", ".."} or ".." in snapshot_id:
        raise ValueError("snapshot id must be non-empty")
    data_root.mkdir(parents=True, exist_ok=True)
    window = _window_key(market, start, end)
    cursor = catalog.load_checkpoint(_JOB, window, snapshot_id)
    resume = _resume_after(cursor)
    requested = (end - start).days + 1
    bars = 0
    missing = 0
    failed = 0
    last_cursor = cursor or ""
    session = start
    if resume is not None and resume > session:
        session = resume
    validated_market = cast("Literal['KOSPI', 'KOSDAQ']", market)
    while session <= end:
        try:
            raw = client.fetch_day(validated_market, session)
        except KrxSourceError as exc:
            if exc.status == "NO_DATA" and not exc.retryable:
                missing += 1
                session += timedelta(days=1)
                continue
            failed += 1
            break
        observed_at = datetime.now(UTC)
        doc_path = PurePosixPath(f"raw/krx/{snapshot_id}/{market}-{session:%Y%m%d}.json")
        try:
            with catalog.transaction():
                raw_hash = catalog.register_artifact(
                    source="krx",
                    endpoint="index",
                    request_key=f"{market}:{session:%Y%m%d}",
                    snapshot_id=snapshot_id,
                    raw_bytes=raw,
                    retrieved_at=observed_at,
                    local_relative_path=doc_path,
                )
                parse_index_day(raw, validated_market, session, raw_hash)
                last_cursor = session.isoformat()
                catalog.save_checkpoint(_JOB, window, snapshot_id, last_cursor)
        except ValueError:
            failed += 1
            break
        bars += 1
        session += timedelta(days=1)
    return IndexCollectionSummary(
        snapshot_id=snapshot_id,
        market=market,
        requested_sessions=requested,
        bars_registered=bars,
        missing_sessions=missing,
        failed_sessions=failed,
        checkpoint_cursor=last_cursor,
    )


__all__ = ["IndexCollectionSummary", "collect_index_range"]
