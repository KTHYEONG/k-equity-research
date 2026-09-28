"""Session-bounded collection of official KRX daily index responses."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath
from typing import cast

from src.data.catalog import Catalog
from src.data.index_store import IndexManifest, merge_index_manifest
from src.data.local_lake import LocalLake
from src.data.local_paths import checked_local_path
from src.integrations.krx_index import KrxIndexClient, KrxSourceError, Market, parse_index_day

_JOB = "krx_index"
_MARKETS = ("KOSPI", "KOSDAQ")


@dataclass(frozen=True, slots=True)
class IndexCollectionSummary:
    """Auditable counts for one session-bounded KRX index collection."""

    snapshot_id: str
    expected_keys: int
    reused_keys: int
    fetched_keys: int
    unresolved_keys: tuple[str, ...]
    manifest_hash: str
    manifest: IndexManifest


def _check_snapshot(snapshot_id: str) -> None:
    if not snapshot_id or "/" in snapshot_id or snapshot_id in {".", ".."} or ".." in snapshot_id:
        raise ValueError("snapshot id must be non-empty")


def _key_label(market: str, session: date) -> str:
    return f"{market}:{session.isoformat()}"


def _request_key(market: str, session: date) -> str:
    return f"{market}:{session:%Y%m%d}"


def _verified_digest(catalog: Catalog, data_root: Path, market: Market, session: date, digest: str) -> str | None:
    """Return the digest when its registered raw bytes hash-verify and parse for this market and session."""
    relative = catalog.get_artifact_path(digest)
    if relative is None:
        return None
    target = checked_local_path(data_root, relative)
    try:
        raw = target.read_bytes()
    except OSError:
        return None
    if hashlib.sha256(raw).hexdigest() != digest.lower():
        return None
    try:
        bar = parse_index_day(raw, market, session, digest)
    except ValueError:
        return None
    return bar.source_hash


def _collect_key(
    client: KrxIndexClient,
    catalog: Catalog,
    market: Market,
    session: date,
    data_root: Path,
    snapshot_id: str,
) -> str | None:
    """Fetch and register one official bar; return its digest or None when the key stays unresolved."""
    try:
        raw = client.fetch_day(market, session)
    except KrxSourceError:
        return None
    observed_at = datetime.now(UTC)
    doc_path = PurePosixPath(f"raw/krx/{snapshot_id}/{market}-{session:%Y%m%d}.json")
    try:
        with catalog.transaction():
            digest = catalog.register_artifact(
                source="krx",
                endpoint="index",
                request_key=_request_key(market, session),
                snapshot_id=snapshot_id,
                raw_bytes=raw,
                retrieved_at=observed_at,
                local_relative_path=doc_path,
            )
            parse_index_day(raw, market, session, digest)
            catalog.save_checkpoint(_JOB, _key_label(market, session), snapshot_id, "complete")
    except ValueError:
        return None
    return digest


def collect_index_sessions(
    client: KrxIndexClient,
    catalog: Catalog,
    lake: LocalLake,
    previous: IndexManifest | None,
    start: date,
    end: date,
    data_root: Path,
    snapshot_id: str,
) -> IndexCollectionSummary:
    """Complete official KOSPI and KOSDAQ bars for verified local sessions.

    Reuse hash-verified prior bars and request only missing market/session
    pairs. Register every accepted response in the project catalog. Raise
    when an expected session has no official bar or conflicting payload;
    never publish a manifest that silently treats a market holiday or a
    provider gap as an observed zero-return session.
    """
    _check_snapshot(snapshot_id)
    data_root.mkdir(parents=True, exist_ok=True)
    sessions = lake.sessions_between(start, end)
    expected: Sequence[tuple[str, date]] = tuple((market, session) for session in sessions for market in _MARKETS)
    prior = dict(previous.entries) if previous is not None else {}
    reused: list[tuple[str, date, str]] = []
    pending: list[tuple[str, date]] = []
    for market, session in expected:
        validated = cast("Market", market)
        digest = prior.get((market, session))
        if digest is not None:
            if _verified_digest(catalog, data_root, validated, session, digest) == digest.lower():
                reused.append((market, session, digest))
                continue
            pending.append((market, session))
            continue
        found = catalog.find_artifact("krx", "index", _request_key(market, session), snapshot_id)
        if found is not None and _verified_digest(catalog, data_root, validated, session, found.sha256) == found.sha256.lower():
            reused.append((market, session, found.sha256.lower()))
            continue
        pending.append((market, session))
    fetched: list[tuple[str, date, str]] = []
    unresolved: list[str] = []
    for market, session in pending:
        digest = _collect_key(client, catalog, cast("Market", market), session, data_root, snapshot_id)
        if digest is None:
            unresolved.append(_key_label(market, session))
            continue
        fetched.append((market, session, digest))
    if unresolved:
        raise ValueError(f"unresolved index keys: {', '.join(sorted(unresolved))}")
    manifest = merge_index_manifest(previous, (*reused, *fetched), data_root)
    return IndexCollectionSummary(
        snapshot_id=snapshot_id,
        expected_keys=len(expected),
        reused_keys=len(reused),
        fetched_keys=len(fetched),
        unresolved_keys=(),
        manifest_hash=manifest.manifest_hash,
        manifest=manifest,
    )


__all__ = ["IndexCollectionSummary", "collect_index_sessions"]
