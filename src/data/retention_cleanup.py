"""Referentially safe retirement of obsolete project-local data.

The plan covers three obsolete sets: import-side files (superseded source
dataset parts and manifests, reclaimed staging and probe files, orphaned
collector rows), catalog-backed raw artifacts (unpinned snapshots whose
hashes are cited nowhere), and financial evidence payload pairs. Staging
and probe files live alongside import-side entries because they share the
same file-only deletion path without catalog rows.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath

import polars as pl

from src.data.catalog import Catalog
from src.data.event_store import EventStore
from src.data.financial_evidence import FinancialEvidence
from src.data.financial_hydration import hydrate_event_financial_evidence
from src.data.imports import ImportManifest, ImportPart
from src.data.index_store import load_index_manifest
from src.data.local_lake import LocalLake
from src.data.local_paths import checked_data_path, checked_local_path
from src.data.retention import (
    DERIVED_FACTS_DATASET_ID,
    DERIVED_PANEL_DATASET_ID,
    DERIVED_UNIVERSE_DATASET_ID,
    SOURCE_FACTS_DATASET_ID,
    SOURCE_PANEL_DATASET_ID,
    SOURCE_UNIVERSE_DATASET_ID,
)

_ACTIVE_IDS = (DERIVED_PANEL_DATASET_ID, DERIVED_UNIVERSE_DATASET_ID, DERIVED_FACTS_DATASET_ID)
_SOURCE_IDS = (SOURCE_PANEL_DATASET_ID, SOURCE_UNIVERSE_DATASET_ID, SOURCE_FACTS_DATASET_ID)
_HASHED_IDS = (DERIVED_PANEL_DATASET_ID, DERIVED_FACTS_DATASET_ID)
_EVIDENCE_DIRNAME = "financial_evidence"
_FINANCIAL_DIRNAME = "financial"
_RETENTION_DIRNAME = "retention"
_PARTIAL_DIRS = ("raw", "financial", "krx", "imports", "reports", "eval", "financial_hydration", "retention")
_LINK_STATUSES = ("LINKED", "WITHDRAWN")
_FAR_FUTURE = datetime.max.replace(tzinfo=UTC)
_CHUNK_SIZE = 1024 * 1024
_HEXDIGITS = frozenset("0123456789abcdefABCDEF")


@dataclass(frozen=True)
class RetirementPlan:
    """Enumerate local objects eligible for referentially safe removal."""

    obsolete_import_parts: tuple[Path, ...]
    obsolete_raw_artifacts: tuple[Path, ...]
    obsolete_financial_evidence: tuple[Path, ...]
    blocking_run_ids: tuple[str, ...]
    retained_bytes: int
    reclaimable_bytes: int
    plan_digest: str


def _is_hex64(value: str) -> bool:
    return len(value) == 64 and all(char in _HEXDIGITS for char in value.lower())


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(_CHUNK_SIZE), b""):
            digest.update(block)
    return digest.hexdigest()


def _check_roots(data_root: Path, catalog: Catalog) -> Path:
    if data_root.is_symlink() or not data_root.is_dir():
        raise ValueError(f"missing project data root: {data_root}")
    root = data_root.resolve()
    if catalog.db_path.parent.resolve() != root:
        raise ValueError("catalog must live in the project data root")
    return root


def _check_retired_ids(retired_run_ids: frozenset[str]) -> None:
    for run_id in retired_run_ids:
        if not run_id or "/" in run_id:
            raise ValueError("retired run ids must be non-empty")


def _read_manifest(data_root: Path, dataset_id: str) -> ImportManifest:
    path = checked_local_path(data_root, PurePosixPath("imports") / dataset_id / "manifest.json")
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"missing active scoped import: {dataset_id!r}")
    try:
        document = json.loads(path.read_bytes().decode("utf-8"))
        manifest = ImportManifest(
            dataset_id=str(document["dataset_id"]),
            source_manifest_sha256=str(document["source_manifest_sha256"]),
            imported_at=datetime.fromisoformat(str(document["imported_at"])),
            parts=tuple(
                ImportPart(PurePosixPath(str(item["path"])), str(item["sha256"]), int(item["bytes"]))
                for item in document["parts"]
            ),
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"invalid scoped import manifest: {dataset_id!r}") from exc
    if manifest.dataset_id != dataset_id:
        raise ValueError(f"invalid scoped import manifest: {dataset_id!r}")
    return manifest


def _verify_part_file(data_root: Path, dataset_id: str, part: ImportPart) -> Path:
    rel = part.relative_path.as_posix()
    if not rel or rel == "." or part.relative_path.is_absolute() or ".." in part.relative_path.parts:
        raise ValueError(f"unsafe scoped import part: {rel!r}")
    target = checked_local_path(data_root, PurePosixPath("imports") / dataset_id / part.relative_path)
    if target.is_symlink() or not target.is_file():
        raise ValueError(f"missing scoped import part: {rel!r}")
    if target.stat().st_size != part.byte_length or _sha256_file(target) != part.sha256.lower():
        raise ValueError(f"hash mismatch for scoped import part: {rel!r}")
    try:
        pl.scan_parquet(str(target)).collect_schema()
    except Exception as exc:
        raise ValueError(f"invalid scoped import part: {rel!r}") from exc
    return target


def _dataset_hashes(paths: list[Path]) -> set[str]:
    found: set[str] = set()
    for path in paths:
        if "source_hash" not in pl.scan_parquet(str(path)).collect_schema().names():
            continue
        values = pl.scan_parquet(str(path)).select("source_hash").unique().collect()["source_hash"].to_list()
        for value in values:
            if not isinstance(value, str) or not _is_hex64(value):
                raise ValueError(f"invalid retained source hash in {path.name!r}")
            found.add(value.lower())
    return found


def _pinned_index_hashes(data_root: Path, catalog: Catalog, lake: LocalLake) -> set[str]:
    manifest_dir = checked_local_path(data_root, PurePosixPath("krx/manifests"))
    names = sorted(path.name for path in manifest_dir.glob("*.json") if path.is_file()) if manifest_dir.is_dir() else []
    if not names:
        raise ValueError("missing verified index coverage")
    keep: set[str] = set()
    observed: set[tuple[str, date]] = set()
    for name in names:
        manifest = load_index_manifest(data_root, name[: -len(".json")])
        for key, digest in manifest.entries.items():
            relative = catalog.get_artifact_path(digest)
            if relative is None:
                raise ValueError("missing indexed raw artifact")
            target = checked_local_path(data_root, relative)
            if target.is_symlink() or not target.is_file() or _sha256_file(target) != digest.lower():
                raise ValueError("missing indexed raw artifact")
            keep.add(digest.lower())
            observed.add(key)
    sessions = lake.sessions_between(date.min, date.max)
    expected = {(market, session) for session in sessions for market in ("KOSPI", "KOSDAQ")}
    if not expected or not expected <= observed:
        raise ValueError("missing verified index coverage")
    return keep


def _filing_hashes(catalog: Catalog) -> set[str]:
    keep: set[str] = set()
    rows = catalog._conn.execute("SELECT raw_hash FROM filing_version ORDER BY rcept_no").fetchall()  # noqa: SLF001
    for row in rows:
        digest = str(row["raw_hash"]).lower()
        relative = catalog.get_artifact_path(digest)
        if relative is None:
            raise ValueError("missing filing raw artifact")
        target = checked_local_path(catalog.db_path.parent, relative)
        if target.is_symlink() or not target.is_file() or _sha256_file(target) != digest:
            raise ValueError("missing filing raw artifact")
        keep.add(digest)
    return keep


def _check_event_links(catalog: Catalog) -> None:
    tables = {
        str(row["name"])
        for row in catalog._conn.execute(  # noqa: SLF001
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if not {"event_link", "buyback_receipt"} <= tables:
        return
    links = catalog._conn.execute("SELECT event_id, status FROM event_link ORDER BY event_id").fetchall()  # noqa: SLF001
    for link in links:
        if str(link["status"]) not in _LINK_STATUSES:
            raise ValueError(f"unresolved event lineage blocks retirement: {link['event_id']}")
    receipts = catalog._conn.execute("SELECT rcept_no FROM buyback_receipt ORDER BY rcept_no").fetchall()  # noqa: SLF001
    for receipt in receipts:
        rcept_no = str(receipt["rcept_no"])
        filing = catalog._conn.execute(  # noqa: SLF001
            "SELECT 1 FROM filing_version WHERE rcept_no=?", (rcept_no,)
        ).fetchone()
        if filing is None:
            raise ValueError(f"orphaned receipt blocks retirement: {rcept_no}")


def _walk_files(base: Path) -> list[Path]:
    """List regular files below base in sorted order; refuse any symlink."""
    found: list[Path] = []
    for current, dirs, files in os.walk(base, followlinks=False):
        dirs.sort()
        for name in sorted(files):
            candidate = Path(current) / name
            if candidate.is_symlink():
                raise ValueError(f"symlinked staging artifact blocks retirement: {candidate.name}")
            if candidate.is_file():
                found.append(candidate)
        for name in sorted(dirs):
            if (Path(current) / name).is_symlink():
                raise ValueError(f"symlinked staging artifact blocks retirement: {name}")
    return found


def _staging_candidates(root: Path) -> list[Path]:
    candidates: list[Path] = []
    staging = root / "staging"
    if staging.is_symlink():
        raise ValueError("symlinked staging artifact blocks retirement: staging")
    if staging.is_dir():
        candidates.extend(_walk_files(staging))
    for child in sorted(root.iterdir()):
        if not child.name.startswith("probe_"):
            continue
        if child.is_symlink():
            raise ValueError(f"symlinked staging artifact blocks retirement: {child.name}")
        if child.is_dir():
            candidates.extend(_walk_files(child))
        elif child.is_file():
            candidates.append(child)
    dot_staging = root / "imports"
    if dot_staging.is_dir():
        for child in sorted(dot_staging.iterdir()):
            if not child.name.startswith(".staging-"):
                continue
            if child.is_symlink():
                raise ValueError(f"symlinked staging artifact blocks retirement: {child.name}")
            if child.is_dir():
                candidates.extend(_walk_files(child))
    for dirname in _PARTIAL_DIRS:
        base = root / dirname
        if not base.is_dir() or base.is_symlink():
            continue
        candidates.extend(path for path in _walk_files(base) if path.name.endswith(".partial"))
    return sorted(set(candidates))


def _plan_digest(
    root: Path,
    imports: list[Path],
    raws: list[Path],
    evidence: list[Path],
    blocking: list[str],
    retained: int,
    reclaimable: int,
) -> str:
    def _rel(path: Path) -> str:
        return path.absolute().relative_to(root).as_posix()

    payload = {
        "blocking_run_ids": sorted(blocking),
        "obsolete_financial_evidence": sorted(_rel(path) for path in evidence),
        "obsolete_import_parts": sorted(_rel(path) for path in imports),
        "obsolete_raw_artifacts": sorted(_rel(path) for path in raws),
        "reclaimable_bytes": reclaimable,
        "retained_bytes": retained,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _checked_absolute(root: Path, path: Path) -> Path:
    try:
        relative = path.absolute().relative_to(root)
    except (ValueError, OSError) as exc:
        raise ValueError(f"retirement path outside data root: {path}") from exc
    return checked_local_path(root, PurePosixPath(relative.as_posix()))


def _plan_relative(root: Path, path: Path) -> str:
    try:
        return path.absolute().relative_to(root).as_posix()
    except (ValueError, OSError) as exc:
        raise ValueError(f"retirement path outside data root: {path}") from exc


@dataclass
class _Collection:
    imports: list[Path]
    raws: list[Path]
    evidence: list[Path]
    blocking: list[str]
    retained_bytes: int
    reclaimable_bytes: int


def _collect(
    root: Path,
    catalog: Catalog,
    event_store: EventStore,
    retired_run_ids: frozenset[str],
) -> _Collection:
    manifests = {dataset_id: _read_manifest(root, dataset_id) for dataset_id in _ACTIVE_IDS}
    active_parts: dict[Path, int] = {}
    for dataset_id, manifest in manifests.items():
        for part in manifest.parts:
            target = _verify_part_file(root, dataset_id, part)
            active_parts[target.resolve()] = part.byte_length
    retained_files: dict[Path, int] = dict(active_parts)
    for dataset_id in _ACTIVE_IDS:
        manifest_path = checked_local_path(root, PurePosixPath("imports") / dataset_id / "manifest.json")
        retained_files[manifest_path.resolve()] = manifest_path.stat().st_size

    part_paths = {
        dataset_id: [
            checked_local_path(root, PurePosixPath("imports") / dataset_id / part.relative_path)
            for part in manifests[dataset_id].parts
        ]
        for dataset_id in _ACTIVE_IDS
    }
    keep_hashes = _dataset_hashes(
        part_paths[DERIVED_PANEL_DATASET_ID]
        + part_paths[DERIVED_UNIVERSE_DATASET_ID]
        + part_paths[DERIVED_FACTS_DATASET_ID]
    )
    lake = LocalLake(root, manifests)
    keep_hashes |= _pinned_index_hashes(root, catalog, lake)
    keep_hashes |= _filing_hashes(catalog)
    _check_event_links(catalog)
    for row in catalog._conn.execute("SELECT viewer_hash FROM buyback_viewer_parent").fetchall():  # noqa: SLF001
        digest = str(row["viewer_hash"])
        relative = catalog.get_artifact_path(digest)
        if relative is None:
            raise ValueError("missing DART lineage viewer artifact")
        target = checked_local_path(root, relative)
        if target.is_symlink() or not target.is_file() or _sha256_file(target) != digest:
            raise ValueError("changed DART lineage viewer artifact")
        keep_hashes.add(digest)

    gaps = hydrate_event_financial_evidence(
        catalog, event_store, FinancialEvidence(root, lake), root, _FAR_FUTURE
    )
    if gaps.missing_hashes:
        raise ValueError(f"financial evidence gap blocks retirement: {len(gaps.missing_hashes)} hashes")

    raw_rows = catalog._conn.execute(  # noqa: SLF001
        "SELECT source, endpoint, request_key, snapshot_id, sha256, local_path, byte_length "
        "FROM raw_artifact ORDER BY source, endpoint, request_key, snapshot_id"
    ).fetchall()
    pinned_snapshots = {
        str(row["snapshot_id"])
        for row in catalog._conn.execute("SELECT DISTINCT snapshot_id FROM checkpoint").fetchall()  # noqa: SLF001
    }
    evidence_dirs: dict[str, Path] = {}
    evidence_root = checked_local_path(root, PurePosixPath("imports") / _EVIDENCE_DIRNAME)
    if evidence_root.is_dir():
        for child in sorted(evidence_root.iterdir()):
            if child.is_symlink():
                raise ValueError(f"symlinked financial evidence blocks retirement: {child.name}")
            if not child.is_dir() or not _is_hex64(child.name):
                continue
            evidence_dirs[child.name.lower()] = child

    candidates = {str(row["sha256"]).lower() for row in raw_rows} | set(evidence_dirs)
    for dataset_id in _SOURCE_IDS:
        manifest_path = checked_local_path(root, PurePosixPath("imports") / dataset_id / "manifest.json")
        if manifest_path.is_symlink() or not manifest_path.is_file():
            continue
        source = _read_manifest(root, dataset_id)
        candidates.update(part.sha256.lower() for part in source.parts)
    references = catalog.references_to_hashes(frozenset(candidates))
    blocking = sorted({run for runs in references.values() for run in runs} - set(retired_run_ids))
    cited = {digest for digest, runs in references.items() if set(runs) - set(retired_run_ids)}

    obsolete_imports: list[Path] = []
    obsolete_raws: list[Path] = []
    obsolete_evidence: list[Path] = []
    reclaimable = 0

    for dataset_id in _SOURCE_IDS:
        manifest_path = checked_local_path(root, PurePosixPath("imports") / dataset_id / "manifest.json")
        if manifest_path.is_symlink() or not manifest_path.is_file():
            continue
        source = _read_manifest(root, dataset_id)
        entries = [manifest_path]
        for part in source.parts:
            target = checked_local_path(root, PurePosixPath("imports") / dataset_id / part.relative_path)
            if not target.is_file():
                continue
            if _sha256_file(target) != part.sha256.lower():
                raise ValueError(f"hash drift blocks retirement: {part.relative_path}")
            entries.append(target)
        for entry in entries:
            obsolete_imports.append(entry.resolve())
            reclaimable += entry.stat().st_size

    for row in raw_rows:
        digest = str(row["sha256"]).lower()
        target = checked_local_path(root, PurePosixPath(str(row["local_path"])))
        present = target.is_file()
        keep = str(row["snapshot_id"]) in pinned_snapshots or digest in keep_hashes or digest in cited
        if keep:
            if not present:
                raise ValueError(f"missing active raw artifact blocks retirement: {row['request_key']}")
            resolved = target.resolve()
            retained_files[resolved] = int(row["byte_length"])
            continue
        if present and _sha256_file(target) != digest:
            raise ValueError(f"hash drift blocks retirement: {row['request_key']}")
        obsolete_raws.append(target.resolve() if present else target.absolute())
        reclaimable += int(row["byte_length"]) if present else 0

    for digest, child in evidence_dirs.items():
        payload = child / "payload.json"
        receipt = child / "receipt.json"
        if payload.is_symlink() or receipt.is_symlink():
            raise ValueError(f"symlinked financial evidence blocks retirement: {digest}")
        keep = digest in keep_hashes or digest in cited
        if keep:
            if not payload.is_file() or _sha256_file(payload) != digest or not receipt.is_file():
                raise ValueError(f"missing active financial evidence blocks retirement: {digest}")
            retained_files[payload.resolve()] = payload.stat().st_size
            retained_files[receipt.resolve()] = receipt.stat().st_size
            continue
        for entry in (payload, receipt):
            if entry.is_file():
                obsolete_evidence.append(entry.resolve())
                reclaimable += entry.stat().st_size

    for path in _staging_candidates(root):
        resolved = path.resolve()
        obsolete_imports.append(resolved)
        reclaimable += path.stat().st_size

    kept_slugs = set()
    for row in raw_rows:
        digest = str(row["sha256"]).lower()
        slug = (str(row["snapshot_id"]), str(row["request_key"]).replace(":", "-"))
        if str(row["snapshot_id"]) in pinned_snapshots or digest in keep_hashes or digest in cited:
            kept_slugs.add(slug)
    financial_root = checked_local_path(root, PurePosixPath(_FINANCIAL_DIRNAME))
    if financial_root.is_dir():
        for path in sorted(financial_root.rglob("*.parquet")):
            if not path.is_file():
                continue
            slug = (path.parent.name, path.stem)
            if slug in kept_slugs:
                retained_files[path.resolve()] = path.stat().st_size
            else:
                obsolete_imports.append(path.resolve())
                reclaimable += path.stat().st_size

    retained_bytes = sum(retained_files.values())
    return _Collection(
        imports=sorted(set(obsolete_imports)),
        raws=sorted(set(obsolete_raws)),
        evidence=sorted(set(obsolete_evidence)),
        blocking=blocking,
        retained_bytes=retained_bytes,
        reclaimable_bytes=reclaimable,
    )


def plan_local_retirement(
    data_root: Path,
    catalog: Catalog,
    event_store: EventStore,
    retired_run_ids: frozenset[str] = frozenset(),  # noqa: B008 - contract requires this default
) -> RetirementPlan:
    """Identify unreachable old local data after verified scope migration.

    Check active imports, raw catalog entries, financial fact references,
    accepted events, index manifests, and research runs. Treat unknown
    references or missing active evidence as blockers. Report exact local
    paths and byte counts; do not delete anything.
    """
    _check_retired_ids(retired_run_ids)
    root = _check_roots(data_root, catalog)
    found = _collect(root, catalog, event_store, retired_run_ids)
    digest = _plan_digest(
        root,
        found.imports,
        found.raws,
        found.evidence,
        found.blocking,
        found.retained_bytes,
        found.reclaimable_bytes,
    )
    return RetirementPlan(
        obsolete_import_parts=tuple(found.imports),
        obsolete_raw_artifacts=tuple(found.raws),
        obsolete_financial_evidence=tuple(found.evidence),
        blocking_run_ids=tuple(found.blocking),
        retained_bytes=found.retained_bytes,
        reclaimable_bytes=found.reclaimable_bytes,
        plan_digest=digest,
    )


def _prune_empty_parents(root: Path, leaf: Path) -> None:
    parent = leaf.parent
    while parent != root and parent.is_dir() and not parent.is_symlink():
        with contextlib.suppress(OSError):
            parent.rmdir()
        if parent.exists():
            break
        parent = parent.parent


def _retirement_record_path(root: Path, plan_digest: str) -> Path:
    return checked_local_path(root, PurePosixPath(_RETENTION_DIRNAME) / f"retirement-{plan_digest}.json")


def execute_local_retirement(
    plan: RetirementPlan,
    data_root: Path,
    catalog: Catalog,
    retired_run_ids: frozenset[str],
) -> RetirementPlan:
    """Retire only the already verified and approved local object set.

    Recheck every hash and reference against the plan before modifying
    catalog rows or files. Preserve the active scoped imports, all accepted
    DART and KRX evidence, and every referenced financial payload. Abort on
    any changed state; interrupted execution must be safely resumable.
    """
    _check_retired_ids(retired_run_ids)
    root = _check_roots(data_root, catalog)
    expected = _plan_digest(
        root,
        list(plan.obsolete_import_parts),
        list(plan.obsolete_raw_artifacts),
        list(plan.obsolete_financial_evidence),
        list(plan.blocking_run_ids),
        plan.retained_bytes,
        plan.reclaimable_bytes,
    )
    if expected != plan.plan_digest:
        raise ValueError("retirement plan changed since approval")
    for path in (*plan.obsolete_import_parts, *plan.obsolete_raw_artifacts, *plan.obsolete_financial_evidence):
        _checked_absolute(root, path)

    event_store = EventStore(catalog)
    fresh = _collect(root, catalog, event_store, retired_run_ids)
    if fresh.blocking:
        raise ValueError(f"retirement blocked by research run reference: {', '.join(fresh.blocking)}")
    planned = {
        path.resolve() for path in (*plan.obsolete_import_parts, *plan.obsolete_raw_artifacts, *plan.obsolete_financial_evidence)
    }
    current = {path.resolve() for path in (*fresh.imports, *fresh.raws, *fresh.evidence)}
    if not current <= planned:
        raise ValueError("retirement state changed since planning")
    if any(path.exists() or path.is_symlink() for path in planned - current):
        raise ValueError("retirement state changed since planning")

    with catalog.transaction():
        for run_id in sorted(set(retired_run_ids)):
            catalog._conn.execute("DELETE FROM research_run WHERE run_id=?", (run_id,))  # noqa: SLF001

    record_path = _retirement_record_path(root, plan.plan_digest)
    record = {
        "retired_run_ids": sorted(set(retired_run_ids)),
        "obsolete_financial_evidence": sorted(
            path.resolve().relative_to(root).as_posix() for path in plan.obsolete_financial_evidence
        ),
        "obsolete_import_parts": sorted(
            path.resolve().relative_to(root).as_posix() for path in plan.obsolete_import_parts
        ),
        "obsolete_raw_artifacts": sorted(
            path.resolve().relative_to(root).as_posix() for path in plan.obsolete_raw_artifacts
        ),
        "plan_digest": plan.plan_digest,
        "reclaimable_bytes": plan.reclaimable_bytes,
        "retained_bytes": plan.retained_bytes,
    }
    if record_path.is_file():
        try:
            stored = json.loads(record_path.read_bytes().decode("utf-8"))
        except ValueError as exc:
            raise ValueError("conflicting retirement record") from exc
        if not isinstance(stored, dict) or stored.get("plan_digest") != plan.plan_digest:
            raise ValueError("conflicting retirement record")
    else:
        payload = (json.dumps(record, sort_keys=True, indent=2) + "\n").encode("utf-8")
        record_path.parent.mkdir(parents=True, exist_ok=True)
        partial = checked_data_path(root, record_path.with_name(record_path.name + ".partial"))
        partial.write_bytes(payload)
        os.replace(partial, record_path)

    for path in plan.obsolete_raw_artifacts:
        rel_posix = _plan_relative(root, path)
        row = catalog._conn.execute(  # noqa: SLF001
            "SELECT source, endpoint, request_key, snapshot_id FROM raw_artifact WHERE local_path=?",
            (rel_posix,),
        ).fetchone()
        if row is None:
            continue
        catalog.retire_artifacts(
            ((str(row["source"]), str(row["endpoint"]), str(row["request_key"]), str(row["snapshot_id"])),),
            plan.plan_digest,
        )
    for path in (*plan.obsolete_financial_evidence, *plan.obsolete_import_parts):
        target = _checked_absolute(root, path)
        if target.is_file():
            target.unlink()
            _prune_empty_parents(root, target)

    return plan_local_retirement(data_root, catalog, event_store, retired_run_ids)


__all__ = ["RetirementPlan", "execute_local_retirement", "plan_local_retirement"]
