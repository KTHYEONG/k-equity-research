"""Versioned financial index keeps all values while correcting evidenced units."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import polars as pl
import pytest

from src.data.financial_evidence import FinancialEvidence
from src.data.financial_index_v2 import DATASET_ID, SOURCE_ID, publish_financial_index_v2


def _source(root: Path) -> None:
    base = root / "imports" / SOURCE_ID
    base.mkdir(parents=True)
    part = base / "part-00000.parquet"
    pl.DataFrame(
        {
            "source_hash": ["a" * 64, "b" * 64],
            "unit": ["KRW", "KRW"],
            "value": [12.0, 23.0],
        }
    ).write_parquet(part)
    (base / "manifest.json").write_text(
        json.dumps(
            {
                "dataset_id": SOURCE_ID,
                "source_manifest_sha256": "c" * 64,
                "imported_at": "2026-09-29T00:00:00+00:00",
                "parts": [
                    {
                        "path": part.name,
                        "sha256": hashlib.sha256(part.read_bytes()).hexdigest(),
                        "bytes": part.stat().st_size,
                    }
                ],
            }
        )
    )


def test_publish_corrects_only_unit_and_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _source(tmp_path)
    monkeypatch.setattr(
        FinancialEvidence,
        "_load_records",
        lambda self, digest: [
            {
                "currency": "USD" if digest.startswith("a") else "KRW",
                "source_kind": "opendart_standard",
                "unit": "KRW",
            }
        ],
    )
    first = publish_financial_index_v2(tmp_path)
    second = publish_financial_index_v2(tmp_path)
    assert first.corrected_rows == second.corrected_rows == 1
    source = pl.read_parquet(tmp_path / "imports" / SOURCE_ID / "part-00000.parquet")
    corrected = pl.read_parquet(tmp_path / "imports" / DATASET_ID / "part-00000.parquet")
    assert source["unit"].to_list() == ["KRW", "KRW"]
    assert corrected["unit"].to_list() == ["USD", "KRW"]
    assert corrected.drop("unit").equals(source.drop("unit"))


def test_publish_refuses_ambiguous_currency(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _source(tmp_path)
    monkeypatch.setattr(
        FinancialEvidence,
        "_load_records",
        lambda self, digest: [
            {"currency": "USD", "source_kind": "opendart_standard", "unit": "KRW"},
            {"currency": "JPY", "source_kind": "opendart_standard", "unit": "KRW"},
        ],
    )
    with pytest.raises(ValueError, match="ambiguous source currency"):
        publish_financial_index_v2(tmp_path)
    assert not (tmp_path / "imports" / DATASET_ID).exists()
