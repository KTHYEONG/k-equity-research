"""Durable local catalog for raw artifacts, filing versions and checkpoints."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from src.data.local_paths import checked_data_path, checked_local_path

_SCHEMA_VERSION = 2
_CHUNK_SIZE = 1024 * 1024
_HEXDIGITS = frozenset("0123456789abcdefABCDEF")


def _is_hex64(value: str) -> bool:
    return len(value) == 64 and all(char in _HEXDIGITS for char in value)


def _walk_hex_tokens(document: Any) -> Iterator[str]:
    if isinstance(document, str):
        if _is_hex64(document):
            yield document.lower()
    elif isinstance(document, Mapping):
        for value in document.values():
            yield from _walk_hex_tokens(value)
    elif isinstance(document, (list, tuple)):
        for value in document:
            yield from _walk_hex_tokens(value)


def _require_aware(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(_CHUNK_SIZE), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class RawArtifact:
    """Immutable metadata for locally stored source bytes."""

    sha256: str
    source: str
    endpoint: str
    request_key: str
    snapshot_id: str
    local_relative_path: PurePosixPath
    retrieved_at: datetime
    byte_length: int


@dataclass(frozen=True, slots=True)
class FilingVersion:
    """One receipt-specific immutable filing version."""

    rcept_no: str
    corp_code: str
    receipt_date: date
    report_name: str
    stock_code: str
    raw_hash: str
    first_observed_at: datetime
    knowledge_available_at: datetime
    availability_mode: Literal["HISTORICAL_BACKFILL", "LIVE"]
    correction_flag: bool
    withdrawal_flag: bool
    parent_rcept_no: str | None
    link_status: str
    time_precision: Literal["DATE_ONLY", "OBSERVED_INSTANT"]


class Catalog:
    """SQLite-backed durable identity for local artifacts and filing facts."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._data_root = db_path.parent
        self._data_root.mkdir(parents=True, exist_ok=True)
        checked_local_path(self._data_root, PurePosixPath(db_path.name))
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._in_transaction = False
        self._init_schema()

    @property
    def db_path(self) -> Path:
        """Return the local catalog database path."""
        return self._db_path

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS raw_artifact (
                source TEXT NOT NULL,
                endpoint TEXT NOT NULL,
                request_key TEXT NOT NULL,
                snapshot_id TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                local_path TEXT NOT NULL,
                retrieved_at TEXT NOT NULL,
                byte_length INTEGER NOT NULL,
                UNIQUE (source, endpoint, request_key, snapshot_id)
            );
            CREATE INDEX IF NOT EXISTS idx_raw_artifact_sha ON raw_artifact (sha256);
            CREATE TABLE IF NOT EXISTS filing_version (
                rcept_no TEXT PRIMARY KEY,
                corp_code TEXT NOT NULL,
                receipt_date TEXT NOT NULL,
                report_name TEXT NOT NULL,
                stock_code TEXT NOT NULL,
                raw_hash TEXT NOT NULL,
                first_observed_at TEXT NOT NULL,
                knowledge_available_at TEXT NOT NULL,
                availability_mode TEXT NOT NULL,
                correction_flag INTEGER NOT NULL,
                withdrawal_flag INTEGER NOT NULL,
                parent_rcept_no TEXT,
                link_status TEXT NOT NULL,
                time_precision TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS checkpoint (
                job TEXT NOT NULL,
                window TEXT NOT NULL,
                snapshot_id TEXT NOT NULL,
                cursor TEXT NOT NULL,
                PRIMARY KEY (job, window, snapshot_id)
            );
            CREATE TABLE IF NOT EXISTS research_run (
                run_id TEXT PRIMARY KEY,
                manifest_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                local_path TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS retirement_entry (
                retirement_id TEXT NOT NULL,
                source TEXT NOT NULL,
                endpoint TEXT NOT NULL,
                request_key TEXT NOT NULL,
                snapshot_id TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                byte_length INTEGER NOT NULL,
                retired_at TEXT NOT NULL,
                PRIMARY KEY (retirement_id, source, endpoint, request_key, snapshot_id)
            );
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            """
        )
        row = self._conn.execute("SELECT version FROM schema_version").fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (_SCHEMA_VERSION, datetime.now().astimezone().isoformat()),
            )
            self._conn.commit()
        elif int(row["version"]) == 1:
            self._conn.execute("UPDATE schema_version SET version=?, applied_at=?", (_SCHEMA_VERSION, datetime.now().astimezone().isoformat()))
            self._conn.commit()
        elif int(row["version"]) != _SCHEMA_VERSION:
            raise ValueError(f"unsupported catalog schema version: {row['version']}")
        else:
            self._conn.commit()

    def _commit_or_hold(self) -> None:
        if not self._in_transaction:
            self._conn.commit()

    def _abort_or_hold(self) -> None:
        if not self._in_transaction:
            self._conn.rollback()

    def _resolve_inside(self, relative: PurePosixPath) -> Path:
        return checked_local_path(self._data_root, relative)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Expose one rollback-safe local catalog transaction to repository modules so raw artifacts, event facts and checkpoints can commit together."""
        if self._in_transaction:
            yield self._conn
            return
        self._conn.execute("BEGIN IMMEDIATE")
        self._in_transaction = True
        try:
            yield self._conn
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        finally:
            self._in_transaction = False

    def register_artifact(
        self,
        source: str,
        endpoint: str,
        request_key: str,
        snapshot_id: str,
        raw_bytes: bytes,
        retrieved_at: datetime,
        local_relative_path: PurePosixPath,
    ) -> str:
        """Record immutable locally stored source bytes and return their SHA-256. Reject conflicting bytes at an existing local path or unsafe paths; never store credentials or a runtime source-project dependency."""
        if not source or not endpoint or not request_key or not snapshot_id:
            raise ValueError("artifact request identity must be non-empty")
        _require_aware(retrieved_at, "retrieved_at")
        destination = self._resolve_inside(local_relative_path)
        digest = _sha256_bytes(raw_bytes)
        try:
            existing = self._conn.execute(
                "SELECT sha256, local_path FROM raw_artifact WHERE source=? AND endpoint=? AND request_key=? AND snapshot_id=?",
                (source, endpoint, request_key, snapshot_id),
            ).fetchone()
            if existing is not None:
                if str(existing["sha256"]) != digest:
                    raise ValueError("conflicting bytes for registered request")
                if str(existing["local_path"]) != local_relative_path.as_posix():
                    raise ValueError("conflicting bytes for registered request")
                current = self._resolve_inside(PurePosixPath(str(existing["local_path"])))
                if current.is_symlink() or not current.is_file() or _sha256_file(current) != digest:
                    raise ValueError("registered local file missing or changed")
                return digest
            clash = self._conn.execute(
                "SELECT sha256 FROM raw_artifact WHERE local_path=?", (local_relative_path.as_posix(),)
            ).fetchone()
            if clash is not None and str(clash["sha256"]) != digest:
                raise ValueError("conflicting bytes at existing local path")
            if destination.is_file():
                if _sha256_file(destination) != digest:
                    raise ValueError("conflicting bytes at existing local path")
            else:
                if destination.exists():
                    raise ValueError("conflicting bytes at existing local path")
                destination.parent.mkdir(parents=True, exist_ok=True)
                partial = checked_data_path(self._data_root, destination.with_name(destination.name + ".partial"))
                partial.write_bytes(raw_bytes)
                os.replace(partial, destination)
            self._conn.execute(
                "INSERT INTO raw_artifact (source, endpoint, request_key, snapshot_id, sha256, local_path, retrieved_at, byte_length) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    source,
                    endpoint,
                    request_key,
                    snapshot_id,
                    digest,
                    local_relative_path.as_posix(),
                    retrieved_at.isoformat(),
                    len(raw_bytes),
                ),
            )
            self._commit_or_hold()
            return digest
        except BaseException:
            self._abort_or_hold()
            raise

    def upsert_filing(self, filing: FilingVersion) -> None:
        """Persist a receipt-specific version without changing earlier facts; reject conflicting receipt identity or artifact hashes."""
        if not filing.rcept_no or not filing.corp_code or not filing.report_name or not filing.stock_code:
            raise ValueError("filing identity must be non-empty")
        if not filing.link_status:
            raise ValueError("filing link status must be non-empty")
        if not _is_hex64(filing.raw_hash.lower()):
            raise ValueError("invalid artifact hash")
        _require_aware(filing.first_observed_at, "first_observed_at")
        _require_aware(filing.knowledge_available_at, "knowledge_available_at")
        try:
            artifact = self._conn.execute(
                "SELECT local_path FROM raw_artifact WHERE sha256=? ORDER BY rowid LIMIT 1",
                (filing.raw_hash.lower(),),
            ).fetchone()
            if artifact is None:
                raise ValueError("missing registered artifact for filing")
            local = self._resolve_inside(PurePosixPath(str(artifact["local_path"])))
            if local.is_symlink() or not local.is_file() or _sha256_file(local) != filing.raw_hash.lower():
                raise ValueError("registered local file missing or changed")
            stored = self._conn.execute(
                "SELECT * FROM filing_version WHERE rcept_no=?", (filing.rcept_no,)
            ).fetchone()
            if stored is not None:
                if (
                    str(stored["corp_code"]) != filing.corp_code
                    or str(stored["receipt_date"]) != filing.receipt_date.isoformat()
                    or str(stored["report_name"]) != filing.report_name
                    or str(stored["stock_code"]) != filing.stock_code
                    or str(stored["raw_hash"]) != filing.raw_hash.lower()
                    or str(stored["first_observed_at"]) != filing.first_observed_at.isoformat()
                    or str(stored["knowledge_available_at"]) != filing.knowledge_available_at.isoformat()
                    or str(stored["availability_mode"]) != filing.availability_mode
                    or int(stored["correction_flag"]) != int(filing.correction_flag)
                    or int(stored["withdrawal_flag"]) != int(filing.withdrawal_flag)
                    or (stored["parent_rcept_no"] is None and filing.parent_rcept_no is not None)
                    or (
                        stored["parent_rcept_no"] is not None
                        and str(stored["parent_rcept_no"]) != (filing.parent_rcept_no or "")
                    )
                    or str(stored["link_status"]) != filing.link_status
                    or str(stored["time_precision"]) != filing.time_precision
                ):
                    raise ValueError("conflicting receipt identity")
                return
            self._conn.execute(
                "INSERT INTO filing_version (rcept_no, corp_code, receipt_date, report_name, stock_code, raw_hash, first_observed_at, knowledge_available_at, availability_mode, correction_flag, withdrawal_flag, parent_rcept_no, link_status, time_precision) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    filing.rcept_no,
                    filing.corp_code,
                    filing.receipt_date.isoformat(),
                    filing.report_name,
                    filing.stock_code,
                    filing.raw_hash.lower(),
                    filing.first_observed_at.isoformat(),
                    filing.knowledge_available_at.isoformat(),
                    filing.availability_mode,
                    int(filing.correction_flag),
                    int(filing.withdrawal_flag),
                    filing.parent_rcept_no,
                    filing.link_status,
                    filing.time_precision,
                ),
            )
            self._commit_or_hold()
        except BaseException:
            self._abort_or_hold()
            raise

    def get_filing_asof(self, rcept_no: str, as_of: datetime) -> FilingVersion | None:
        """Return a version only after its stored policy-specific knowledge boundary. Keep actual first observation separate, and never let a later correction replace the original receipt's historical record."""
        _require_aware(as_of, "as_of")
        row = self._conn.execute("SELECT * FROM filing_version WHERE rcept_no=?", (rcept_no,)).fetchone()
        if row is None:
            return None
        boundary = datetime.fromisoformat(str(row["knowledge_available_at"]))
        if as_of < boundary:
            return None
        parent = row["parent_rcept_no"]
        return FilingVersion(
            rcept_no=str(row["rcept_no"]),
            corp_code=str(row["corp_code"]),
            receipt_date=date.fromisoformat(str(row["receipt_date"])),
            report_name=str(row["report_name"]),
            stock_code=str(row["stock_code"]),
            raw_hash=str(row["raw_hash"]),
            first_observed_at=datetime.fromisoformat(str(row["first_observed_at"])),
            knowledge_available_at=boundary,
            availability_mode=str(row["availability_mode"]),  # type: ignore[arg-type]
            correction_flag=bool(int(row["correction_flag"])),
            withdrawal_flag=bool(int(row["withdrawal_flag"])),
            parent_rcept_no=str(parent) if parent is not None else None,
            link_status=str(row["link_status"]),
            time_precision=str(row["time_precision"]),  # type: ignore[arg-type]
        )

    def save_checkpoint(self, job: str, window: str, snapshot_id: str, cursor: str) -> None:
        """Advance a collection cursor only in the same successful transaction as its page and required documents."""
        if not job or not window or not snapshot_id or not cursor:
            raise ValueError("checkpoint identity and cursor must be non-empty")
        self._conn.execute(
            "INSERT INTO checkpoint (job, window, snapshot_id, cursor) VALUES (?, ?, ?, ?) ON CONFLICT (job, window, snapshot_id) DO UPDATE SET cursor=excluded.cursor",
            (job, window, snapshot_id, cursor),
        )
        self._commit_or_hold()

    def load_checkpoint(self, job: str, window: str, snapshot_id: str) -> str | None:
        """Return the last committed cursor for one source window, or None if no page completed."""
        row = self._conn.execute(
            "SELECT cursor FROM checkpoint WHERE job=? AND window=? AND snapshot_id=?",
            (job, window, snapshot_id),
        ).fetchone()
        if row is None:
            return None
        return str(row["cursor"])

    def get_artifact_path(self, sha256: str) -> PurePosixPath | None:
        """Resolve a registered content hash to a project-local relative path without consulting external origins."""
        row = self._conn.execute(
            "SELECT local_path FROM raw_artifact WHERE sha256=? ORDER BY rowid LIMIT 1", (sha256.lower(),)
        ).fetchone()
        if row is None:
            return None
        relative = PurePosixPath(str(row["local_path"]))
        self._resolve_inside(relative)
        return relative

    def find_artifact(
        self, source: str, endpoint: str, request_key: str, snapshot_id: str
    ) -> RawArtifact | None:
        """Find one locally registered source response by request and pinned collection snapshot; return None when it was never collected."""
        row = self._conn.execute(
            "SELECT * FROM raw_artifact WHERE source=? AND endpoint=? AND request_key=? AND snapshot_id=?",
            (source, endpoint, request_key, snapshot_id),
        ).fetchone()
        if row is None:
            return None
        relative = PurePosixPath(str(row["local_path"]))
        self._resolve_inside(relative)
        return RawArtifact(
            sha256=str(row["sha256"]),
            source=str(row["source"]),
            endpoint=str(row["endpoint"]),
            request_key=str(row["request_key"]),
            snapshot_id=str(row["snapshot_id"]),
            local_relative_path=relative,
            retrieved_at=datetime.fromisoformat(str(row["retrieved_at"])),
            byte_length=int(row["byte_length"]),
        )

    def register_research_run(
        self, run_id: str, manifest_hash: str, status: str, local_relative_path: PurePosixPath
    ) -> None:
        """Commit one complete local report run and immutable manifest reference. Reject conflicting run IDs and paths outside the project data root."""
        if not run_id or not status:
            raise ValueError("research run identity and status must be non-empty")
        normalized = manifest_hash.lower()
        if not _is_hex64(normalized):
            raise ValueError("invalid manifest hash")
        target = self._resolve_inside(local_relative_path)
        try:
            if target.is_symlink() or not target.is_file() or _sha256_file(target) != normalized:
                raise ValueError("registered local file missing or changed")
            stored = self._conn.execute(
                "SELECT manifest_hash, status, local_path FROM research_run WHERE run_id=?", (run_id,)
            ).fetchone()
            if stored is not None:
                if (
                    str(stored["manifest_hash"]) != normalized
                    or str(stored["status"]) != status
                    or str(stored["local_path"]) != local_relative_path.as_posix()
                ):
                    raise ValueError("conflicting research run")
                return
            self._conn.execute(
                "INSERT INTO research_run (run_id, manifest_hash, status, local_path) VALUES (?, ?, ?, ?)",
                (run_id, normalized, status, local_relative_path.as_posix()),
            )
            self._commit_or_hold()
        except BaseException:
            self._abort_or_hold()
            raise

    def references_to_hashes(self, hashes: frozenset[str]) -> Mapping[str, tuple[str, ...]]:
        """Return exact research-run identifiers that cite candidate hashes.

        Include current and legacy run records; fail when a reference format
        cannot be decoded rather than treating an unknown record as unreferenced.
        """
        wanted = {str(value).lower() for value in hashes if str(value)}
        if not wanted:
            return {}
        rows = self._conn.execute("SELECT run_id, local_path FROM research_run ORDER BY run_id").fetchall()
        found: dict[str, set[str]] = {}
        for row in rows:
            run_id = str(row["run_id"])
            target = self._resolve_inside(PurePosixPath(str(row["local_path"])))
            if target.is_symlink() or not target.is_file():
                raise ValueError(f"unresolvable research run reference: {run_id}")
            rundir = target.parent
            for sibling in sorted(rundir.glob("*.json")):
                if sibling.is_symlink() or not sibling.is_file():
                    raise ValueError(f"unresolvable research run reference: {run_id}")
                try:
                    document = json.loads(sibling.read_bytes().decode("utf-8"))
                except (ValueError, UnicodeDecodeError) as exc:
                    raise ValueError(f"undecodable research run reference: {run_id}") from exc
                for token in _walk_hex_tokens(document):
                    if token in wanted:
                        found.setdefault(token, set()).add(run_id)
        return {digest: tuple(sorted(run_ids)) for digest, run_ids in sorted(found.items())}

    def retire_artifacts(
        self,
        keys: tuple[tuple[str, str, str, str], ...],
        retirement_id: str,
    ) -> None:
        """Atomically retire verified catalog artifact pairs by exact keys.

        Record the retirement ID and original hashes for audit. Reject any
        key whose hash or research-run reference changed since planning.
        """
        clean = retirement_id.strip()
        if not clean or "/" in clean or clean in {".", ".."} or ".." in clean:
            raise ValueError("retirement id must be non-empty")
        normalized: list[tuple[str, str, str, str]] = []
        for key in keys:
            if len(key) != 4 or any(not part for part in key):
                raise ValueError("retirement key must name source, endpoint, request, and snapshot")
            normalized.append((key[0], key[1], key[2], key[3]))
        if not normalized:
            return
        retired_at = datetime.now().astimezone().isoformat()
        with self.transaction():
            for source, endpoint, request_key, snapshot_id in normalized:
                row = self._conn.execute(
                    "SELECT sha256, local_path, byte_length FROM raw_artifact WHERE source=? AND endpoint=? AND request_key=? AND snapshot_id=?",
                    (source, endpoint, request_key, snapshot_id),
                ).fetchone()
                if row is None:
                    audit = self._conn.execute(
                        "SELECT sha256 FROM retirement_entry WHERE retirement_id=? AND source=? AND endpoint=? AND request_key=? AND snapshot_id=?",
                        (clean, source, endpoint, request_key, snapshot_id),
                    ).fetchone()
                    if audit is None:
                        raise ValueError(f"unknown retirement key: {request_key!r}")
                    continue
                digest = str(row["sha256"])
                if self.references_to_hashes(frozenset({digest})):
                    raise ValueError(f"retirement blocked by research run reference: {request_key!r}")
                target = self._resolve_inside(PurePosixPath(str(row["local_path"])))
                if target.is_file():
                    if _sha256_file(target) != digest.lower():
                        raise ValueError(f"retirement blocked by hash drift: {request_key!r}")
                    target.unlink()
                self._conn.execute(
                    "INSERT INTO retirement_entry (retirement_id, source, endpoint, request_key, snapshot_id, sha256, byte_length, retired_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (clean, source, endpoint, request_key, snapshot_id, digest.lower(), int(row["byte_length"]), retired_at),
                )
                self._conn.execute(
                    "DELETE FROM raw_artifact WHERE source=? AND endpoint=? AND request_key=? AND snapshot_id=?",
                    (source, endpoint, request_key, snapshot_id),
                )
