"""Project-local paths reject symlinked ancestors before reads or writes."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath

import polars as pl
import pytest

from src.data.catalog import Catalog
from src.data.financial_evidence import FinancialEvidence
from src.data.imports import ImportManifest, ImportPart
from src.data.index_store import merge_index_manifest
from src.data.local_lake import PANEL_DATASET_ID, LocalLake
from src.data.local_paths import checked_data_path, checked_local_path
from src.cli.batch import _atomic_write_bytes
from src.cli.main import _atomic_write_bytes as cli_atomic_write_bytes


def test_catalog_refuses_symlinked_raw_parent(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    outside = tmp_path / "outside"
    outside.mkdir()
    catalog = Catalog(data_root / "catalog.sqlite")
    (data_root / "raw").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinked local path"):
        catalog.register_artifact(
            "dart", "document", "receipt", "snapshot", b"proof", datetime.now(UTC), PurePosixPath("raw/proof.bin")
        )
    assert not (outside / "proof.bin").exists()


def test_catalog_refuses_symlinked_partial_file(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    catalog = Catalog(data_root / "catalog.sqlite")
    raw_dir = data_root / "raw"
    raw_dir.mkdir()
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"original")
    (raw_dir / "proof.bin.partial").symlink_to(outside)
    with pytest.raises(ValueError, match="symlinked local path"):
        catalog.register_artifact(
            "dart", "document", "receipt", "snapshot", b"new", datetime.now(UTC), PurePosixPath("raw/proof.bin")
        )
    assert outside.read_bytes() == b"original"


def test_lake_refuses_symlinked_dataset_parent(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    outside = tmp_path / "outside"
    outside.mkdir()
    (data_root / "imports").mkdir(parents=True)
    part = outside / "part.parquet"
    pl.DataFrame({"session": [date(2024, 6, 3)]}).write_parquet(part)
    (data_root / "imports" / PANEL_DATASET_ID).symlink_to(outside, target_is_directory=True)
    manifest = ImportManifest(
        PANEL_DATASET_ID,
        "0" * 64,
        datetime.now(UTC),
        (ImportPart(PurePosixPath("part.parquet"), hashlib.sha256(part.read_bytes()).hexdigest(), part.stat().st_size),),
    )
    with pytest.raises(ValueError, match="symlinked local path"):
        LocalLake(data_root, {PANEL_DATASET_ID: manifest})


def test_catalog_refuses_symlinked_database_file(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    outside = tmp_path / "outside.sqlite"
    data_root.mkdir()
    outside.touch()
    (data_root / "catalog.sqlite").symlink_to(outside)
    with pytest.raises(ValueError, match="symlinked local path"):
        Catalog(data_root / "catalog.sqlite")
    assert outside.stat().st_size == 0


def test_report_and_index_writers_refuse_symlinked_parents(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    outside = tmp_path / "outside"
    data_root.mkdir()
    outside.mkdir()
    (data_root / "reports").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinked local path"):
        _atomic_write_bytes(data_root, data_root / "reports" / "memo.json", b"memo")
    assert not (outside / "memo.json").exists()
    (data_root / "krx").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinked local path"):
        merge_index_manifest(None, [], data_root)
    assert not list(outside.glob("*.json"))


@pytest.mark.parametrize("write_report", [_atomic_write_bytes, cli_atomic_write_bytes])
def test_report_writer_refuses_symlinked_partial_file(
    tmp_path: Path, write_report: Callable[[Path, Path, bytes], None]
) -> None:
    data_root = tmp_path / "data"
    report_dir = data_root / "reports"
    report_dir.mkdir(parents=True)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"original")
    (report_dir / "memo.json.partial").symlink_to(outside)
    with pytest.raises(ValueError, match="symlinked local path"):
        write_report(data_root, report_dir / "memo.json", b"new")
    assert outside.read_bytes() == b"original"


def test_local_path_rejects_symlinked_root_and_outside_target(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_root = tmp_path / "linked_data"
    linked_root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinked data root"):
        checked_local_path(linked_root, PurePosixPath("raw/document.zip"))
    nested_root = linked_root / "nested"
    nested_root.mkdir()
    with pytest.raises(ValueError, match="symlinked data root"):
        checked_local_path(nested_root, PurePosixPath("raw/document.zip"))
    data_root = tmp_path / "data"
    data_root.mkdir()
    with pytest.raises(ValueError, match="path outside data root"):
        checked_data_path(data_root, outside / "document.zip")


def test_financial_evidence_ignores_symlinked_bronze_parent(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    outside = tmp_path / "outside"
    data_root.mkdir()
    outside.mkdir()
    (data_root / "imports").symlink_to(outside, target_is_directory=True)
    financial = FinancialEvidence(data_root, LocalLake(data_root, {}))
    assert financial._load_records("a" * 64) is None  # noqa: SLF001 - exercises the local evidence boundary
