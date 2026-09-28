"""Project-local verified import of source datasets and cited evidence."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from src.data.local_paths import checked_data_path, checked_local_path

_MANIFEST_NAME = "manifest.json"
_IMPORTS_DIRNAME = "imports"
_EVIDENCE_DIRNAME = "financial_evidence"
_HEXDIGITS = frozenset("0123456789abcdefABCDEF")
_CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True, slots=True)
class ImportPart:
    """One verified local partition copy."""

    relative_path: PurePosixPath
    sha256: str
    byte_length: int


@dataclass(frozen=True, slots=True)
class ImportManifest:
    """Registered local subset of one source dataset."""

    dataset_id: str
    source_manifest_sha256: str
    imported_at: datetime
    parts: tuple[ImportPart, ...]


def _is_hex64(value: str) -> bool:
    return len(value) == 64 and all(char in _HEXDIGITS for char in value)


def _check_relative_path(value: PurePosixPath) -> None:
    text = value.as_posix()
    if not text or text == "." or value.is_absolute() or ".." in value.parts:
        raise ValueError(f"unsafe relative path: {text!r}")


def _check_dataset_id(dataset_id: str) -> None:
    if not dataset_id or "/" in dataset_id or ".." in dataset_id or dataset_id in {".", ""}:
        raise ValueError(f"invalid dataset id: {dataset_id!r}")


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(_CHUNK_SIZE), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_source_manifest(source_dataset: Path) -> tuple[str, dict[str, tuple[str, int | None]], str]:
    manifest_path = checked_local_path(source_dataset, PurePosixPath(_MANIFEST_NAME))
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError(f"missing source manifest: {manifest_path}")
    raw = manifest_path.read_bytes()
    try:
        document = json.loads(raw.decode("utf-8"))
        dataset_id = document["dataset_id"]
        raw_partitions = document["partitions"]
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"invalid source manifest: {manifest_path}") from exc
    if not isinstance(dataset_id, str):
        raise ValueError(f"invalid source manifest: {manifest_path}")
    _check_dataset_id(dataset_id)
    if not isinstance(raw_partitions, list) or not raw_partitions:
        raise ValueError(f"invalid source manifest: {manifest_path}")
    entries: dict[str, tuple[str, int | None]] = {}
    for item in raw_partitions:
        if not isinstance(item, dict):
            raise ValueError(f"invalid source manifest: {manifest_path}")
        path_value = item.get("path")
        hash_value = item.get("sha256", item.get("parquet_sha256"))
        size_value = item.get("bytes", item.get("byte_length", item.get("size")))
        if not isinstance(path_value, str) or not isinstance(hash_value, str):
            raise ValueError(f"invalid source manifest: {manifest_path}")
        _check_relative_path(PurePosixPath(path_value))
        if not _is_hex64(hash_value):
            raise ValueError(f"invalid source manifest: {manifest_path}")
        size: int | None = None
        if size_value is not None:
            if not isinstance(size_value, int) or size_value < 0:
                raise ValueError(f"invalid source manifest: {manifest_path}")
            size = size_value
        if path_value in entries:
            raise ValueError(f"invalid source manifest: {manifest_path}")
        entries[path_value] = (hash_value.lower(), size)
    return dataset_id, entries, hashlib.sha256(raw).hexdigest()


def _load_local_manifest(path: Path) -> ImportManifest | None:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        document = json.loads(path.read_bytes().decode("utf-8"))
        parts = tuple(
            ImportPart(
                relative_path=PurePosixPath(str(item["path"])),
                sha256=str(item["sha256"]),
                byte_length=int(item["bytes"]),
            )
            for item in document["parts"]
        )
        return ImportManifest(
            dataset_id=str(document["dataset_id"]),
            source_manifest_sha256=str(document["source_manifest_sha256"]),
            imported_at=datetime.fromisoformat(str(document["imported_at"])),
            parts=parts,
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"invalid local manifest: {path}") from exc


def _manifest_payload(manifest: ImportManifest) -> str:
    return json.dumps(
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


def _parts_valid(dest_root: Path, parts: Sequence[ImportPart]) -> bool:
    for part in parts:
        target = checked_local_path(dest_root, part.relative_path)
        if target.is_symlink() or not target.is_file():
            return False
        if target.stat().st_size != part.byte_length or _sha256_of(target) != part.sha256:
            return False
    return True


def import_dataset(
    source_dataset: Path, selected_parts: Sequence[PurePosixPath], data_root: Path
) -> ImportManifest:
    """Materialize selected verified source partitions inside this repository so research can run without the source project. Require the source manifest's dataset ID, listed path, size and SHA-256; return the registered local subset. Raise ValueError for invalid manifests, paths or hashes, and OSError for incomplete copy or commit."""
    if not selected_parts:
        raise ValueError("empty selection")
    selected = [PurePosixPath(str(item)) for item in selected_parts]
    for item in selected:
        _check_relative_path(item)
    if len({item.as_posix() for item in selected}) != len(selected):
        raise ValueError("duplicate relative paths")
    if source_dataset.is_symlink() or not source_dataset.is_dir():
        raise ValueError(f"invalid source dataset: {source_dataset}")
    dataset_id, entries, source_manifest_sha256 = _read_source_manifest(source_dataset)
    expected: list[ImportPart] = []
    for item in selected:
        key = item.as_posix()
        if key not in entries:
            raise ValueError(f"part not listed in source manifest: {key!r}")
        digest, declared_size = entries[key]
        source_file = checked_local_path(source_dataset, item)
        if source_file.is_symlink() or not source_file.is_file():
            raise ValueError(f"invalid source part: {key!r}")
        observed_size = source_file.stat().st_size
        if declared_size is not None and observed_size != declared_size:
            raise ValueError(f"source size mismatch: {key!r}")
        expected.append(ImportPart(relative_path=item, sha256=digest, byte_length=observed_size))
    data_root.mkdir(parents=True, exist_ok=True)
    dest_root = checked_local_path(data_root, PurePosixPath(_IMPORTS_DIRNAME) / dataset_id)
    local_manifest_path = checked_local_path(data_root, PurePosixPath(_IMPORTS_DIRNAME) / dataset_id / _MANIFEST_NAME)
    registered = _load_local_manifest(local_manifest_path)
    if registered is not None:
        if (
            registered.dataset_id != dataset_id
            or registered.source_manifest_sha256 != source_manifest_sha256
            or registered.parts != tuple(expected)
        ):
            raise ValueError(f"conflicting import for dataset: {dataset_id!r}")
        if _parts_valid(dest_root, registered.parts):
            return registered
    staged: list[tuple[Path, Path]] = []
    for part in expected:
        source_file = checked_local_path(source_dataset, part.relative_path)
        destination = checked_local_path(dest_root, part.relative_path)
        if destination.is_symlink() or destination.exists():
            if (
                not destination.is_symlink()
                and destination.exists()
                and (source_file.stat().st_ino, source_file.stat().st_dev)
                == (destination.stat().st_ino, destination.stat().st_dev)
            ):
                raise ValueError(f"hardlinked output: {part.relative_path.as_posix()!r}")
            raise ValueError(f"refusing to overwrite local part: {part.relative_path.as_posix()!r}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = checked_data_path(data_root, destination.with_name(destination.name + ".partial"))
        shutil.copyfile(source_file, partial)
        if partial.stat().st_size != part.byte_length or _sha256_of(partial) != part.sha256:
            partial.unlink(missing_ok=True)
            raise ValueError(f"source manifest hash mismatch: {part.relative_path.as_posix()!r}")
        staged.append((partial, destination))
    manifest = ImportManifest(
        dataset_id=dataset_id,
        source_manifest_sha256=source_manifest_sha256,
        imported_at=datetime.now(UTC),
        parts=tuple(expected),
    )
    for partial, destination in staged:
        os.replace(partial, destination)
    partial_manifest = checked_data_path(data_root, local_manifest_path.with_name(local_manifest_path.name + ".partial"))
    partial_manifest.write_text(_manifest_payload(manifest) + "\n", encoding="utf-8")
    os.replace(partial_manifest, local_manifest_path)
    return manifest


def import_financial_evidence(
    source_receipt_dir: Path, source_hash: str, data_root: Path
) -> tuple[Path, Path]:
    """Copy a cited financial payload and its receipt into local immutable evidence storage. The payload hash must equal source_hash; returned paths are local and never point at source_receipt_dir. Raise ValueError on missing evidence or hash mismatch."""
    normalized = source_hash.lower()
    if not _is_hex64(normalized):
        raise ValueError("invalid source hash")
    if source_receipt_dir.is_symlink() or not source_receipt_dir.is_dir():
        raise ValueError(f"missing evidence: {source_receipt_dir}")
    payload_name = "payload.json" if (source_receipt_dir / "payload.json").exists() else "payload.zip"
    source_payload = checked_local_path(source_receipt_dir, PurePosixPath(payload_name))
    source_receipt = checked_local_path(source_receipt_dir, PurePosixPath("receipt.json"))
    if source_payload.is_symlink() or not source_payload.is_file():
        raise ValueError(f"missing evidence: {source_receipt_dir}")
    if source_receipt.is_symlink() or not source_receipt.is_file():
        raise ValueError(f"missing evidence: {source_receipt_dir}")
    if _sha256_of(source_payload) != normalized:
        raise ValueError("evidence hash mismatch")
    data_root.mkdir(parents=True, exist_ok=True)
    dest_dir = checked_local_path(data_root, PurePosixPath(_IMPORTS_DIRNAME) / _EVIDENCE_DIRNAME / normalized)
    dest_payload = checked_local_path(dest_dir, PurePosixPath(payload_name))
    dest_receipt = checked_local_path(dest_dir, PurePosixPath("receipt.json"))
    if dest_payload.is_file() and not dest_payload.is_symlink() and dest_receipt.is_file():
        if _sha256_of(dest_payload) == normalized and dest_receipt.read_bytes() == source_receipt.read_bytes():
            return dest_payload, dest_receipt
        raise ValueError("conflicting evidence already imported")
    if dest_payload.is_symlink() or dest_payload.exists() or dest_receipt.is_symlink() or dest_receipt.exists():
        raise ValueError("conflicting evidence already imported")
    dest_dir.mkdir(parents=True, exist_ok=True)
    partial_payload = checked_data_path(data_root, dest_payload.with_name(dest_payload.name + ".partial"))
    partial_receipt = checked_data_path(data_root, dest_receipt.with_name(dest_receipt.name + ".partial"))
    shutil.copyfile(source_payload, partial_payload)
    shutil.copyfile(source_receipt, partial_receipt)
    os.replace(partial_payload, dest_payload)
    os.replace(partial_receipt, dest_receipt)
    return dest_payload, dest_receipt
