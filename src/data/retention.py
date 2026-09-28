"""Project-local time-scoped materialization of imported research datasets."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath
from typing import Any, cast
from zoneinfo import ZoneInfo

import polars as pl

from src.data.imports import ImportManifest, ImportPart
from src.data.local_paths import checked_data_path, checked_local_path

SOURCE_PANEL_DATASET_ID = "market_panel_f0f4f40a51b28a94"
SOURCE_UNIVERSE_DATASET_ID = "ordinary_universe_3342e1b309c8dd8a"
SOURCE_FACTS_DATASET_ID = "financial_facts_3c324e5ca0eda0ea"

DERIVED_PANEL_DATASET_ID = "market_panel_from_20220707_v1"
DERIVED_UNIVERSE_DATASET_ID = "ordinary_universe_from_20220707_v1"
DERIVED_FACTS_DATASET_ID = "financial_facts_from_20220101_v1"

_CHUNK_SIZE = 1024 * 1024
_MANIFEST_NAME = "manifest.json"
_IMPORTS_DIRNAME = "imports"
_PROVENANCE_REL = PurePosixPath("imports/retention_provenance.json")
_PROVENANCE_VERSION = "retention-provenance-v1"


@dataclass(frozen=True)
class RetentionPlan:
    """Define the verified local history required by 2024+ event research.

    Market sessions cover early-2023 analogue events and their pre-event
    estimation windows. Financial availability covers 2022 comparisons.
    The inclusive boundaries are part of the dataset identity.
    """

    first_market_session: date = date(2022, 7, 7)
    first_financial_available_at: datetime = datetime(2022, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class MaterializationSummary:
    """Report immutable derived datasets and their verified source lineage."""

    dataset_ids: tuple[str, str, str]
    source_rows: Mapping[str, int]
    retained_rows: Mapping[str, int]
    source_bytes: Mapping[str, int]
    retained_bytes: Mapping[str, int]
    provenance_path: Path


@dataclass(frozen=True, slots=True)
class _DatasetSpec:
    """Bind one source dataset to its immutable derived identity and filter."""

    source_id: str
    derived_id: str
    filter_column: str
    require_filter_column: bool


@dataclass(frozen=True, slots=True)
class _DatasetCounts:
    """Verified row and byte totals for one materialized dataset."""

    source_rows: int
    retained_rows: int
    source_bytes: int
    retained_bytes: int


_DATASET_SPECS: tuple[_DatasetSpec, ...] = (
    _DatasetSpec(SOURCE_PANEL_DATASET_ID, DERIVED_PANEL_DATASET_ID, "session", require_filter_column=False),
    _DatasetSpec(SOURCE_UNIVERSE_DATASET_ID, DERIVED_UNIVERSE_DATASET_ID, "session", require_filter_column=True),
    _DatasetSpec(SOURCE_FACTS_DATASET_ID, DERIVED_FACTS_DATASET_ID, "available_at", require_filter_column=True),
)


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(_CHUNK_SIZE), b""):
            digest.update(block)
    return digest.hexdigest()


def _parse_manifest(data_root: Path, dataset_id: str, path: Path) -> ImportManifest:
    try:
        document = json.loads(path.read_bytes().decode("utf-8"))
        manifest = ImportManifest(
            dataset_id=str(document["dataset_id"]),
            source_manifest_sha256=str(document["source_manifest_sha256"]),
            imported_at=datetime.fromisoformat(str(document["imported_at"])),
            parts=tuple(
                ImportPart(
                    relative_path=PurePosixPath(str(item["path"])),
                    sha256=str(item["sha256"]),
                    byte_length=int(item["bytes"]),
                )
                for item in document["parts"]
            ),
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"invalid local manifest: {path}") from exc
    if manifest.dataset_id != dataset_id:
        raise ValueError(f"invalid local manifest: {path}")
    for part in manifest.parts:
        text = part.relative_path.as_posix()
        if not text or text == "." or part.relative_path.is_absolute() or ".." in part.relative_path.parts:
            raise ValueError(f"invalid local manifest: {path}")
    _ = checked_data_path(data_root, path)
    return manifest


def _read_source_manifest(data_root: Path, dataset_id: str) -> ImportManifest:
    path = checked_local_path(data_root, PurePosixPath(_IMPORTS_DIRNAME) / dataset_id / _MANIFEST_NAME)
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"missing source import: {dataset_id!r}")
    return _parse_manifest(data_root, dataset_id, path)


def _read_derived_manifest(data_root: Path, dataset_id: str) -> ImportManifest | None:
    path = checked_local_path(data_root, PurePosixPath(_IMPORTS_DIRNAME) / dataset_id / _MANIFEST_NAME)
    if not path.is_file():
        return None
    return _parse_manifest(data_root, dataset_id, path)


def _verified_source_part(data_root: Path, dataset_id: str, part: ImportPart) -> Path:
    rel = part.relative_path.as_posix()
    target = checked_local_path(data_root, PurePosixPath(_IMPORTS_DIRNAME) / dataset_id / part.relative_path)
    if target.is_symlink() or not target.is_file():
        raise ValueError(f"missing local part: {rel!r}")
    if target.stat().st_size != part.byte_length or _sha256_of(target) != part.sha256.lower():
        raise ValueError(f"hash mismatch for source part: {rel!r}")
    try:
        pl.scan_parquet(str(target)).collect_schema()
    except Exception as exc:
        raise ValueError(f"invalid local part: {rel!r}") from exc
    return target


def _row_count(path: Path) -> int:
    return int(pl.scan_parquet(str(path)).select(pl.len()).collect().item())


def _filtered_frame(source: Path, rel: str, spec: _DatasetSpec, plan: RetentionPlan) -> pl.DataFrame | None:
    """Return retained rows for one source part; None when a panel reference part carries no session column."""
    schema = pl.scan_parquet(str(source)).collect_schema()
    if spec.filter_column not in schema.names():
        if spec.require_filter_column:
            raise ValueError(f"schema mismatch for source part: {rel!r}")
        return None
    if spec.filter_column == "session":
        if schema["session"] != pl.Date:
            raise ValueError(f"schema mismatch for source part: {rel!r}")
        return pl.scan_parquet(str(source)).filter(pl.col("session") >= plan.first_market_session).collect()
    dtype = schema["available_at"]
    if not isinstance(dtype, pl.Datetime) or dtype.time_zone is None:
        raise ValueError(f"schema mismatch for source part: {rel!r}")
    floor = plan.first_financial_available_at.astimezone(ZoneInfo(dtype.time_zone))
    return pl.scan_parquet(str(source)).filter(pl.col("available_at") >= floor).collect()


def _check_retained_boundary(frame: pl.DataFrame, rel: str, spec: _DatasetSpec, plan: RetentionPlan) -> None:
    if spec.filter_column == "session":
        earliest = cast("date", frame.select(pl.col("session").min()).item())
        if earliest < plan.first_market_session:
            raise ValueError(f"retention boundary violated for derived part: {rel!r}")
        return
    earliest_at = cast("datetime", frame.select(pl.col("available_at").min()).item())
    if earliest_at.astimezone(UTC) < plan.first_financial_available_at:
        raise ValueError(f"retention boundary violated for derived part: {rel!r}")


def _manifest_payload(manifest: ImportManifest) -> bytes:
    text = json.dumps(
        {
            "dataset_id": manifest.dataset_id,
            "source_manifest_sha256": manifest.source_manifest_sha256,
            "imported_at": manifest.imported_at.isoformat(),
            "parts": [
                {
                    "path": part.relative_path.as_posix(),
                    "sha256": part.sha256,
                    "bytes": part.byte_length,
                }
                for part in manifest.parts
            ],
        },
        indent=2,
        sort_keys=True,
    )
    return (text + "\n").encode("utf-8")


def _read_provenance(data_root: Path) -> dict[str, Any]:
    path = checked_local_path(data_root, _PROVENANCE_REL)
    if not path.is_file():
        return {}
    try:
        document = json.loads(path.read_bytes().decode("utf-8"))
    except ValueError as exc:
        raise ValueError(f"invalid provenance record: {path}") from exc
    if (
        not isinstance(document, dict)
        or document.get("provenance_version") != _PROVENANCE_VERSION
        or not isinstance(document.get("datasets"), dict)
    ):
        raise ValueError(f"invalid provenance record: {path}")
    return cast("dict[str, Any]", document["datasets"])


def _write_provenance(data_root: Path, datasets: dict[str, Any]) -> Path:
    path = checked_local_path(data_root, _PROVENANCE_REL)
    payload = (
        json.dumps(
            {"provenance_version": _PROVENANCE_VERSION, "datasets": datasets},
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    partial = checked_data_path(data_root, path.with_name(path.name + ".partial"))
    partial.write_bytes(payload)
    os.replace(partial, path)
    return path


def _staging_root(data_root: Path, derived_id: str) -> Path:
    return checked_local_path(data_root, PurePosixPath(_IMPORTS_DIRNAME) / f".staging-{derived_id}")


def _reset_staging(data_root: Path, derived_id: str) -> Path:
    staging = _staging_root(data_root, derived_id)
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True, exist_ok=True)
    return staging


def _publish_staged(data_root: Path, derived_id: str, staged: list[tuple[Path, PurePosixPath]]) -> None:
    for staged_file, rel in staged:
        final = checked_local_path(data_root, PurePosixPath(_IMPORTS_DIRNAME) / derived_id / rel)
        if final.is_symlink() or final.exists():
            raise ValueError(f"conflicting materialization for derived dataset: {derived_id!r}")
        final.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staged_file, final)


def _provenance_entry(
    spec: _DatasetSpec,
    plan: RetentionPlan,
    source: ImportManifest,
    derived: ImportManifest,
    derived_sha: str,
    inputs: dict[str, dict[str, object]],
    outputs: dict[str, dict[str, object]],
    counts: _DatasetCounts,
) -> dict[str, object]:
    return {
        "source_dataset_id": spec.source_id,
        "source_manifest_sha256": source.source_manifest_sha256.lower(),
        "derived_manifest_sha256": derived_sha,
        "first_market_session": plan.first_market_session.isoformat(),
        "first_financial_available_at": plan.first_financial_available_at.isoformat(),
        "input_parts": inputs,
        "output_parts": outputs,
        "source_rows": counts.source_rows,
        "retained_rows": counts.retained_rows,
        "source_bytes": counts.source_bytes,
        "retained_bytes": counts.retained_bytes,
        "created_at": derived.imported_at.isoformat(),
    }


def _inputs_match(entry: Mapping[str, Any], source: ImportManifest, inputs: Mapping[str, Mapping[str, object]]) -> bool:
    if entry.get("source_manifest_sha256") != source.source_manifest_sha256.lower():
        return False
    recorded = entry.get("input_parts")
    if not isinstance(recorded, dict) or set(recorded) != set(inputs):
        return False
    return all(recorded[rel] == dict(inputs[rel]) for rel in inputs)


def _verify_published(
    data_root: Path, spec: _DatasetSpec, plan: RetentionPlan, source: ImportManifest, derived: ImportManifest
) -> _DatasetCounts:
    """Re-scan published derived parts against their source parts; raise on any divergence."""
    source_targets = {part.relative_path.as_posix(): _verified_source_part(data_root, spec.source_id, part) for part in source.parts}
    source_rows = 0
    source_bytes = 0
    for part in source.parts:
        source_rows += _row_count(source_targets[part.relative_path.as_posix()])
        source_bytes += part.byte_length
    retained_rows = 0
    retained_bytes = 0
    for part in derived.parts:
        rel = part.relative_path.as_posix()
        final = checked_local_path(data_root, PurePosixPath(_IMPORTS_DIRNAME) / spec.derived_id / part.relative_path)
        if final.stat().st_size != part.byte_length or _sha256_of(final) != part.sha256.lower():
            raise ValueError(f"hash mismatch for derived part: {rel!r}")
        frame = pl.read_parquet(final)
        if rel in source_targets and frame.columns != pl.read_parquet(source_targets[rel]).columns:
            raise ValueError(f"schema mismatch for derived part: {rel!r}")
        if spec.filter_column in frame.columns:
            _check_retained_boundary(frame, rel, spec, plan)
        rows = frame.height
        retained_rows += rows
        retained_bytes += part.byte_length
    if retained_rows == 0:
        raise ValueError(f"incomplete derived dataset: {spec.derived_id!r}")
    return _DatasetCounts(source_rows, retained_rows, source_bytes, retained_bytes)


def _materialize_dataset(data_root: Path, spec: _DatasetSpec, plan: RetentionPlan) -> _DatasetCounts:
    source = _read_source_manifest(data_root, spec.source_id)
    targets = {part.relative_path.as_posix(): _verified_source_part(data_root, spec.source_id, part) for part in source.parts}
    inputs: dict[str, dict[str, object]] = {
        rel: {
            "sha256": next(part.sha256.lower() for part in source.parts if part.relative_path.as_posix() == rel),
            "bytes": targets[rel].stat().st_size,
            "rows": _row_count(targets[rel]),
        }
        for rel in targets
    }
    provenance = _read_provenance(data_root)
    existing = _read_derived_manifest(data_root, spec.derived_id)
    if existing is not None:
        entry = provenance.get(spec.derived_id)
        if not isinstance(entry, dict) or not _inputs_match(entry, source, inputs):
            raise ValueError(f"conflicting materialization for derived dataset: {spec.derived_id!r}")
        manifest_path = checked_local_path(
            data_root, PurePosixPath(_IMPORTS_DIRNAME) / spec.derived_id / _MANIFEST_NAME
        )
        if _sha256_of(manifest_path) != str(entry["derived_manifest_sha256"]):
            raise ValueError(f"conflicting materialization for derived dataset: {spec.derived_id!r}")
        missing = [
            part
            for part in existing.parts
            if not checked_local_path(data_root, PurePosixPath(_IMPORTS_DIRNAME) / spec.derived_id / part.relative_path).is_file()
        ]
        for part in missing:
            _repair_part(data_root, spec, plan, targets, part)
        counts = _verify_published(data_root, spec, plan, source, existing)
        if counts.source_rows != entry["source_rows"] or counts.retained_rows != entry["retained_rows"]:
            raise ValueError(f"conflicting materialization for derived dataset: {spec.derived_id!r}")
        return counts
    staging = _reset_staging(data_root, spec.derived_id)
    staged: list[tuple[Path, PurePosixPath]] = []
    outputs: dict[str, dict[str, object]] = {}
    try:
        for part in source.parts:
            rel = part.relative_path.as_posix()
            frame = _filtered_frame(targets[rel], rel, spec, plan)
            staged_file = staging / PurePosixPath(rel).as_posix()
            staged_file.parent.mkdir(parents=True, exist_ok=True)
            if frame is None:
                shutil.copyfile(targets[rel], staged_file)
                written = pl.read_parquet(staged_file)
            else:
                if frame.height == 0:
                    continue
                frame.write_parquet(staged_file)
                written = frame
            if frame is not None:
                _check_retained_boundary(written, rel, spec, plan)
            outputs[rel] = {
                "sha256": _sha256_of(staged_file),
                "bytes": staged_file.stat().st_size,
                "rows": written.height,
            }
            staged.append((staged_file, part.relative_path))
        if not staged:
            raise ValueError(f"incomplete derived dataset: {spec.derived_id!r}")
        derived_parts = [
            ImportPart(
                relative_path=rel_path,
                sha256=str(outputs[rel_path.as_posix()]["sha256"]),
                byte_length=cast("int", outputs[rel_path.as_posix()]["bytes"]),
            )
            for _, rel_path in staged
        ]
        derived = ImportManifest(
            dataset_id=spec.derived_id,
            source_manifest_sha256=source.source_manifest_sha256,
            imported_at=datetime.now(UTC),
            parts=tuple(derived_parts),
        )
        _publish_staged(data_root, spec.derived_id, staged)
        manifest_path = checked_local_path(
            data_root, PurePosixPath(_IMPORTS_DIRNAME) / spec.derived_id / _MANIFEST_NAME
        )
        partial_manifest = checked_data_path(data_root, manifest_path.with_name(manifest_path.name + ".partial"))
        payload = _manifest_payload(derived)
        partial_manifest.write_bytes(payload)
        os.replace(partial_manifest, manifest_path)
        derived_sha = _sha256_of(manifest_path)
        counts = _verify_published(data_root, spec, plan, source, derived)
        provenance[spec.derived_id] = _provenance_entry(spec, plan, source, derived, derived_sha, inputs, outputs, counts)
        _write_provenance(data_root, provenance)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return counts


def _repair_part(
    data_root: Path,
    spec: _DatasetSpec,
    plan: RetentionPlan,
    targets: dict[str, Path],
    part: ImportPart,
) -> None:
    """Restore one missing derived part from identical source inputs without touching verified files."""
    rel = part.relative_path.as_posix()
    staging = _reset_staging(data_root, spec.derived_id)
    try:
        frame = _filtered_frame(targets[rel], rel, spec, plan)
        staged_file = staging / PurePosixPath(rel).as_posix()
        staged_file.parent.mkdir(parents=True, exist_ok=True)
        if frame is None:
            shutil.copyfile(targets[rel], staged_file)
        else:
            frame.write_parquet(staged_file)
        _publish_staged(data_root, spec.derived_id, [(staged_file, part.relative_path)])
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def materialize_research_scope(
    data_root: Path,
    plan: RetentionPlan = RetentionPlan(),  # noqa: B008 - contract requires this exact default
) -> MaterializationSummary:
    """Build and verify project-local, time-scoped copies of imported datasets.

    Read only SHA-verified parts named by the three source ImportManifests.
    Preserve every retained row and original value. Raise on absent input,
    schema mismatch, hash conflict, or incomplete output; never activate a
    partial dataset. A second call with identical inputs is idempotent.
    """
    if data_root.is_symlink() or not data_root.is_dir():
        raise ValueError(f"missing project data root: {data_root}")
    if plan != RetentionPlan():
        raise ValueError("retention cutoffs are part of the derived dataset identity")
    counts: dict[str, _DatasetCounts] = {}
    for spec in _DATASET_SPECS:
        counts[spec.derived_id] = _materialize_dataset(data_root, spec, plan)
    provenance_path = checked_local_path(data_root, _PROVENANCE_REL)
    dataset_ids = (DERIVED_PANEL_DATASET_ID, DERIVED_UNIVERSE_DATASET_ID, DERIVED_FACTS_DATASET_ID)
    return MaterializationSummary(
        dataset_ids=dataset_ids,
        source_rows={key: counts[key].source_rows for key in dataset_ids},
        retained_rows={key: counts[key].retained_rows for key in dataset_ids},
        source_bytes={key: counts[key].source_bytes for key in dataset_ids},
        retained_bytes={key: counts[key].retained_bytes for key in dataset_ids},
        provenance_path=provenance_path,
    )
