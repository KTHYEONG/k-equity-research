"""Copy source payloads cited by imported panels into the project catalog."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Literal

import polars as pl

from src.data.catalog import Catalog
from src.data.imports import ImportManifest, ImportPart
from src.data.local_lake import LocalLake
from src.data.local_paths import checked_local_path

SourceKind = Literal["daily_market", "security_master"]
_HEXDIGITS = frozenset("0123456789abcdef")


def _import_manifest(data_root: Path, dataset_id: str) -> ImportManifest:
    path = checked_local_path(data_root, PurePosixPath("imports") / dataset_id / "manifest.json")
    try:
        document = json.loads(path.read_bytes().decode("utf-8"))
        if document["dataset_id"] != dataset_id:
            raise ValueError("dataset id mismatch")
        parts = tuple(
            ImportPart(PurePosixPath(item["path"]), item["sha256"], item["bytes"]) for item in document["parts"]
        )
        return ImportManifest(
            dataset_id,
            document["source_manifest_sha256"],
            datetime.fromisoformat(document["imported_at"]),
            parts,
        )
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid imported dataset manifest: {dataset_id}") from exc


def _source_hashes(lake: LocalLake, dataset_id: str) -> tuple[str, ...]:
    files = [
        str(path)
        for path in lake.dataset_parts(dataset_id)
        if "source_hash" in pl.scan_parquet(path).collect_schema().names()
    ]
    if not files:
        raise ValueError(f"dataset has no source hashes: {dataset_id}")
    hashes = pl.scan_parquet(files).select("source_hash").unique().collect()["source_hash"].to_list()
    if any(not isinstance(value, str) or len(value) != 64 or set(value.lower()) - _HEXDIGITS for value in hashes):
        raise ValueError(f"invalid source hash in dataset: {dataset_id}")
    return tuple(sorted({value.lower() for value in hashes}))


def _source_paths(
    source_root: Path | None, kind: SourceKind, digest: str
) -> tuple[Path, Path, PurePosixPath, PurePosixPath]:
    if source_root is None:
        raise ValueError(f"source root required for {kind}")
    external = PurePosixPath(kind) / digest
    local = PurePosixPath("raw") / "imported" / kind / digest
    return (
        checked_local_path(source_root, external / "payload.json"),
        checked_local_path(source_root, external / "receipt.json"),
        local / "payload.json",
        local / "receipt.json",
    )


def register_imported_lineage(
    data_root: Path,
    dataset_id: str,
    kind: SourceKind,
    source_root: Path | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> int:
    """Register hash-verified source payloads and receipts locally for one imported dataset.

    The external source root is used only during this copy. Later research reads the
    project catalog and project-local files, so removing the source project is safe.
    """
    if kind not in ("daily_market", "security_master"):
        raise ValueError(f"unsupported source kind: {kind}")
    manifest = _import_manifest(data_root, dataset_id)
    lake = LocalLake(data_root, {dataset_id: manifest})
    hashes = _source_hashes(lake, dataset_id)
    catalog = Catalog(data_root / "catalog.sqlite")
    for offset in range(0, len(hashes), 1000):
        with catalog.transaction():
            for digest in hashes[offset : offset + 1000]:
                payload, receipt, local_payload, local_receipt = _source_paths(source_root, kind, digest)
                raw = payload.read_bytes()
                if hashlib.sha256(raw).hexdigest() != digest:
                    raise ValueError(f"source payload hash mismatch: {kind}/{digest}")
                receipt_raw = receipt.read_bytes()
                try:
                    receipt_doc = json.loads(receipt_raw.decode("utf-8"))
                    if receipt_doc["content_hash"] != digest or receipt_doc["kind"] != kind:
                        raise ValueError("source receipt identity mismatch")
                    observed_at = datetime.fromisoformat(receipt_doc["retrieved_at"])
                    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
                        raise ValueError("source receipt timestamp is naive")
                except (KeyError, TypeError, ValueError) as exc:
                    raise ValueError(f"invalid source receipt: {kind}/{digest}") from exc
                snapshot = manifest.source_manifest_sha256
                catalog.register_artifact(
                    "k-stock-engine", kind, digest, snapshot, raw, observed_at, local_payload
                )
                catalog.register_artifact(
                    "k-stock-engine", f"{kind}:receipt", digest, snapshot, receipt_raw, observed_at, local_receipt
                )
        if progress is not None:
            progress(min(offset + 1000, len(hashes)), len(hashes))
    return len(hashes)
