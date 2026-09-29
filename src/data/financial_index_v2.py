"""Publish a versioned financial index with source-declared currency units."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import polars as pl

from src.data.financial_evidence import FinancialEvidence
from src.data.imports import ImportManifest, ImportPart
from src.data.local_lake import LocalLake
from src.data.local_paths import checked_local_path

SOURCE_ID = "financial_facts_from_20220101_v1"
DATASET_ID = "financial_facts_from_20220101_v2"
_CURRENCY = re.compile(r"^[A-Z]{3}$")


@dataclass(frozen=True, slots=True)
class FinancialIndexSummary:
    """Describe the immutable index and its corrected row count."""

    row_count: int
    corrected_rows: int
    corrected_hashes: int
    currencies: tuple[str, ...]
    manifest_path: Path


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _manifest(root: Path, dataset_id: str) -> tuple[ImportManifest, str]:
    path = checked_local_path(root, PurePosixPath("imports") / dataset_id / "manifest.json")
    raw = path.read_bytes()
    data = json.loads(raw)
    manifest = ImportManifest(
        dataset_id=str(data["dataset_id"]),
        source_manifest_sha256=str(data["source_manifest_sha256"]),
        imported_at=datetime.fromisoformat(str(data["imported_at"])),
        parts=tuple(
            ImportPart(PurePosixPath(str(part["path"])), str(part["sha256"]), int(part["bytes"]))
            for part in data["parts"]
        ),
    )
    if manifest.dataset_id != dataset_id:
        raise ValueError("financial index manifest identity mismatch")
    return manifest, hashlib.sha256(raw).hexdigest()


def publish_financial_index_v2(data_root: Path) -> FinancialIndexSummary:
    """Correct only unit labels proven by hash-verified local OpenDART records.

    Every source hash is checked. A missing, mixed or ambiguous currency fails
    publication. The original v1 index is never changed.
    """
    source, source_manifest_hash = _manifest(data_root, SOURCE_ID)
    lake = LocalLake(data_root, {SOURCE_ID: source})
    evidence = FinancialEvidence(data_root, lake)
    parts = lake.dataset_parts(SOURCE_ID)
    if len(parts) != 1:
        raise ValueError("v2 publication requires one verified source partition")
    frame = pl.read_parquet(parts[0])
    if frame.is_empty() or frame["source_hash"].null_count() or frame["unit"].null_count():
        raise ValueError("invalid financial index source rows")
    currencies: dict[str, str] = {}
    for digest in frame["source_hash"].unique().to_list():
        records = evidence._load_records(digest)  # noqa: SLF001 - use the primary-source verifier
        if not records:
            raise ValueError(f"unverified financial source: {digest}")
        units = {record.get("currency") for record in records if isinstance(record, dict)}
        if units == {None} and all(
            isinstance(record, dict)
            and record.get("source_kind") == "document_verified"
            and record.get("unit") == "KRW"
            for record in records
        ):
            units = {"KRW"}
        if len(units) != 1:
            raise ValueError(f"ambiguous source currency: {digest}")
        currency = next(iter(units))
        if not isinstance(currency, str) or _CURRENCY.fullmatch(currency) is None:
            raise ValueError(f"invalid source currency: {digest}")
        if currency != "KRW":
            if any(
                not isinstance(record, dict)
                or record.get("source_kind") != "opendart_standard"
                or record.get("unit") != "KRW"
                for record in records
            ):
                raise ValueError(f"unsupported foreign-currency evidence: {digest}")
            currencies[digest] = currency
    if frame.filter(pl.col("unit") != "KRW").height:
        raise ValueError("unexpected non-KRW legacy unit")
    mapping = pl.DataFrame({"source_hash": list(currencies), "corrected_unit": list(currencies.values())})
    corrected = (
        frame.join(mapping, on="source_hash", how="left")
        .with_columns(pl.coalesce("corrected_unit", "unit").alias("unit"))
        .drop("corrected_unit")
        .select(frame.columns)
    )
    if corrected.height != frame.height or corrected.drop("unit").equals(frame.drop("unit")) is False:
        raise ValueError("v2 publication changed non-unit data")
    count = corrected.filter(pl.col("unit") != "KRW").height
    target_dir = checked_local_path(data_root, PurePosixPath("imports") / DATASET_ID)
    target = target_dir / "part-00000.parquet"
    manifest_path = target_dir / "manifest.json"
    if manifest_path.exists():
        existing, _ = _manifest(data_root, DATASET_ID)
        if existing.source_manifest_sha256 != source_manifest_hash or len(existing.parts) != 1:
            raise ValueError("conflicting financial v2 publication")
        part = existing.parts[0]
        if part.relative_path.as_posix() != target.name or _digest(target) != part.sha256:
            raise ValueError("changed financial v2 partition")
        published = pl.read_parquet(target)
        if not published.equals(corrected):
            raise ValueError("conflicting financial v2 rows")
    else:
        if target.exists() or target.is_symlink():
            raise ValueError("unregistered financial v2 partition exists")
        target_dir.mkdir(parents=True, exist_ok=True)
        staging = target_dir / "part-00000.parquet.partial"
        corrected.write_parquet(staging)
        os.replace(staging, target)
        data = {
            "dataset_id": DATASET_ID,
            "imported_at": datetime.now(UTC).isoformat(),
            "parts": [{"path": target.name, "sha256": _digest(target), "bytes": target.stat().st_size}],
            "source_manifest_sha256": source_manifest_hash,
        }
        staging_manifest = target_dir / "manifest.json.partial"
        staging_manifest.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
        os.replace(staging_manifest, manifest_path)
    return FinancialIndexSummary(
        frame.height, count, len(currencies), tuple(sorted(set(currencies.values()))), manifest_path
    )
