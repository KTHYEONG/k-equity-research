"""Imported source lineage remains verifiable after the source workspace disappears."""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl
import pytest

from src.data.catalog import Catalog
from src.data.lineage_import import register_imported_lineage


def _rig(tmp_path: Path, receipt_hash: str | None = None) -> tuple[Path, Path, str, bytes]:
    data_root = tmp_path / "project" / "data"
    source_root = tmp_path / "source"
    payload = b'{"session":"2024-06-26","price":1000}'
    digest = hashlib.sha256(payload).hexdigest()
    source_dir = source_root / "daily_market" / digest
    source_dir.mkdir(parents=True)
    (source_dir / "payload.json").write_bytes(payload)
    receipt = {
        "content_hash": receipt_hash or digest,
        "kind": "daily_market",
        "retrieved_at": datetime(2024, 6, 26, 18, tzinfo=UTC).isoformat(),
    }
    (source_dir / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
    dataset_id = "market_panel_test"
    imported = data_root / "imports" / dataset_id
    imported.mkdir(parents=True)
    part = imported / "part.parquet"
    pl.DataFrame({"session": [date(2024, 6, 26)], "source_hash": [digest]}).write_parquet(part)
    manifest = {
        "dataset_id": dataset_id,
        "source_manifest_sha256": "a" * 64,
        "imported_at": datetime.now(UTC).isoformat(),
        "parts": [
            {"path": part.name, "sha256": hashlib.sha256(part.read_bytes()).hexdigest(), "bytes": part.stat().st_size}
        ],
    }
    (imported / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return data_root, source_root, digest, payload


def test_register_imported_lineage_survives_source_removal(tmp_path: Path) -> None:
    data_root, source_root, digest, payload = _rig(tmp_path)
    assert register_imported_lineage(data_root, "market_panel_test", "daily_market", source_root) == 1
    shutil.rmtree(source_root)
    catalog = Catalog(data_root / "catalog.sqlite")
    relative = catalog.get_artifact_path(digest)
    assert relative is not None
    assert (data_root / relative).read_bytes() == payload
    assert (data_root / relative.parent / "receipt.json").is_file()


def test_register_imported_lineage_reports_verified_progress(tmp_path: Path) -> None:
    data_root, source_root, _, _ = _rig(tmp_path)
    updates: list[tuple[int, int]] = []
    count = register_imported_lineage(
        data_root, "market_panel_test", "daily_market", source_root, lambda done, total: updates.append((done, total))
    )
    assert count == 1
    assert updates == [(1, 1)]


def test_register_imported_lineage_rejects_receipt_mismatch(tmp_path: Path) -> None:
    data_root, source_root, digest, _ = _rig(tmp_path, "0" * 64)
    with pytest.raises(ValueError, match="invalid source receipt"):
        register_imported_lineage(data_root, "market_panel_test", "daily_market", source_root)
    assert Catalog(data_root / "catalog.sqlite").get_artifact_path(digest) is None


@pytest.mark.parametrize("failure", ["wrong_id", "missing_field"])
def test_register_imported_lineage_rejects_invalid_manifest(tmp_path: Path, failure: str) -> None:
    data_root, source_root, _, _ = _rig(tmp_path)
    manifest_path = data_root / "imports" / "market_panel_test" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if failure == "wrong_id":
        manifest["dataset_id"] = "another_dataset"
    else:
        del manifest["parts"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="invalid imported dataset manifest"):
        register_imported_lineage(data_root, "market_panel_test", "daily_market", source_root)


@pytest.mark.parametrize("source_hashes", [None, "not-a-sha256"])
def test_register_imported_lineage_requires_valid_source_hashes(
    tmp_path: Path, source_hashes: str | None
) -> None:
    data_root, source_root, digest, _ = _rig(tmp_path)
    part = data_root / "imports" / "market_panel_test" / "part.parquet"
    columns: dict[str, list[object]] = {"session": [date(2024, 6, 26)]}
    if source_hashes is not None:
        columns["source_hash"] = [source_hashes]
    pl.DataFrame(columns).write_parquet(part)
    manifest_path = part.parent / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["parts"][0]["sha256"] = hashlib.sha256(part.read_bytes()).hexdigest()
    manifest["parts"][0]["bytes"] = part.stat().st_size
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    expected_error = "dataset has no source hashes" if source_hashes is None else "invalid source hash"
    with pytest.raises(ValueError, match=expected_error):
        register_imported_lineage(data_root, "market_panel_test", "daily_market", source_root)
    assert Catalog(data_root / "catalog.sqlite").get_artifact_path(digest) is None


def test_register_imported_lineage_requires_source_root(tmp_path: Path) -> None:
    data_root, _, _, _ = _rig(tmp_path)
    with pytest.raises(ValueError, match="source root required"):
        register_imported_lineage(data_root, "market_panel_test", "daily_market")


def test_register_imported_lineage_rejects_unknown_source_kind(tmp_path: Path) -> None:
    data_root, source_root, _, _ = _rig(tmp_path)
    with pytest.raises(ValueError, match="unsupported source kind"):
        register_imported_lineage(data_root, "market_panel_test", "unsupported", source_root)  # type: ignore[arg-type]


def test_register_imported_lineage_rejects_changed_payload(tmp_path: Path) -> None:
    data_root, source_root, digest, _ = _rig(tmp_path)
    (source_root / "daily_market" / digest / "payload.json").write_bytes(b"changed")
    with pytest.raises(ValueError, match="source payload hash mismatch"):
        register_imported_lineage(data_root, "market_panel_test", "daily_market", source_root)
    assert Catalog(data_root / "catalog.sqlite").get_artifact_path(digest) is None


def test_register_imported_lineage_rejects_naive_receipt_time(tmp_path: Path) -> None:
    data_root, source_root, digest, _ = _rig(tmp_path)
    receipt_path = source_root / "daily_market" / digest / "receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["retrieved_at"] = "2024-06-26T18:00:00"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid source receipt"):
        register_imported_lineage(data_root, "market_panel_test", "daily_market", source_root)
    assert Catalog(data_root / "catalog.sqlite").get_artifact_path(digest) is None
