"""Invariant guards for scoped research data materialization."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath

import polars as pl
import pytest

from src.data.imports import ImportManifest, ImportPart
from src.data.retention import (
    DERIVED_FACTS_DATASET_ID,
    DERIVED_PANEL_DATASET_ID,
    DERIVED_UNIVERSE_DATASET_ID,
    SOURCE_FACTS_DATASET_ID,
    SOURCE_PANEL_DATASET_ID,
    SOURCE_UNIVERSE_DATASET_ID,
    RetentionPlan,
    materialize_research_scope,
)

BEFORE = date(2022, 7, 6)
CUTOFF = date(2022, 7, 7)
AFTER = date(2022, 7, 8)
FLOOR = datetime(2022, 1, 1, tzinfo=UTC)
PRE_FLOOR = datetime(2021, 12, 31, 23, 59, 59, tzinfo=UTC)


def _sha_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _manifest_payload(manifest: ImportManifest) -> str:
    return json.dumps(
        {
            "dataset_id": manifest.dataset_id,
            "source_manifest_sha256": manifest.source_manifest_sha256,
            "imported_at": manifest.imported_at.isoformat(),
            "parts": [
                {"path": part.relative_path.as_posix(), "sha256": part.sha256, "bytes": part.byte_length}
                for part in manifest.parts
            ],
        },
        indent=2,
        sort_keys=True,
    )


def _write_source_import(
    data_root: Path, dataset_id: str, frames: dict[str, pl.DataFrame], source_sha: str = "1" * 64
) -> ImportManifest:
    parts = []
    for relative, frame in frames.items():
        target = data_root / "imports" / dataset_id / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        frame.write_parquet(target)
        parts.append(ImportPart(PurePosixPath(relative), _sha_of(target), target.stat().st_size))
    manifest = ImportManifest(dataset_id, source_sha, datetime.now(UTC), tuple(parts))
    (data_root / "imports" / dataset_id / "manifest.json").write_text(_manifest_payload(manifest) + "\n", encoding="utf-8")
    return manifest


def _panel_frame(sessions: list[date]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "instrument_id": ["KRX:005930"] * len(sessions),
            "session": sessions,
            "close": [71000] * len(sessions),
        }
    )


def _universe_frame(session: date) -> pl.DataFrame:
    return pl.DataFrame({"instrument_id": ["KRX:005930"], "ticker": ["005930"], "session": [session]})


def _facts_frame(entries: list[tuple[str, datetime, float, str]]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "dart_corp_code": ["01386916"] * len(entries),
            "filing_id": [filing for filing, _, _, _ in entries],
            "fact": ["assets"] * len(entries),
            "fiscal_period": ["2021Q4"] * len(entries),
            "consolidated": [True] * len(entries),
            "available_at": [at for _, at, _, _ in entries],
            "value": [value for _, _, value, _ in entries],
            "unit": ["KRW"] * len(entries),
            "source_hash": [digest for _, _, _, digest in entries],
        }
    )


def _seed_minimal(data_root: Path) -> None:
    _write_source_import(
        data_root,
        SOURCE_PANEL_DATASET_ID,
        {
            "year=2022/part.parquet": _panel_frame([CUTOFF]),
            "instrument_exits.parquet": pl.DataFrame({"instrument_id": ["KRX:000030"], "last_session": [BEFORE]}),
        },
    )
    _write_source_import(
        data_root, SOURCE_UNIVERSE_DATASET_ID, {f"session={CUTOFF.isoformat()}/part.parquet": _universe_frame(CUTOFF)}
    )
    _write_source_import(
        data_root, SOURCE_FACTS_DATASET_ID, {"part-00000.parquet": _facts_frame([("20220101000001", FLOOR, 10.0, "a" * 64)])}
    )


def _read_derived_frames(data_root: Path, derived_id: str) -> dict[str, pl.DataFrame]:
    document = json.loads((data_root / "imports" / derived_id / "manifest.json").read_text(encoding="utf-8"))
    return {
        str(item["path"]): pl.read_parquet(data_root / "imports" / derived_id / str(item["path"]))
        for item in document["parts"]
    }


def _snapshot_tree(data_root: Path, dataset_id: str) -> dict[str, bytes]:
    return {
        path.relative_to(data_root).as_posix(): path.read_bytes()
        for path in sorted((data_root / "imports" / dataset_id).rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def test_boundary_session_inclusion(tmp_path: Path) -> None:
    """Rows before 2022-07-07 are dropped while the cutoff session is kept in both market outputs."""
    data_root = tmp_path / "data"
    _write_source_import(
        data_root,
        SOURCE_PANEL_DATASET_ID,
        {
            "year=2022/part.parquet": _panel_frame([BEFORE, CUTOFF]),
            "instrument_exits.parquet": pl.DataFrame({"instrument_id": ["KRX:000030"], "last_session": [BEFORE]}),
        },
    )
    _write_source_import(
        data_root,
        SOURCE_UNIVERSE_DATASET_ID,
        {
            f"session={BEFORE.isoformat()}/part.parquet": _universe_frame(BEFORE),
            f"session={CUTOFF.isoformat()}/part.parquet": _universe_frame(CUTOFF),
        },
    )
    _write_source_import(
        data_root, SOURCE_FACTS_DATASET_ID, {"part-00000.parquet": _facts_frame([("20220101000001", FLOOR, 10.0, "a" * 64)])}
    )
    before_sources = {
        dataset: _snapshot_tree(data_root, dataset)
        for dataset in (SOURCE_PANEL_DATASET_ID, SOURCE_UNIVERSE_DATASET_ID, SOURCE_FACTS_DATASET_ID)
    }
    summary = materialize_research_scope(data_root)
    assert summary.dataset_ids == (DERIVED_PANEL_DATASET_ID, DERIVED_UNIVERSE_DATASET_ID, DERIVED_FACTS_DATASET_ID)
    panel = _read_derived_frames(data_root, DERIVED_PANEL_DATASET_ID)
    assert sorted(panel) == ["instrument_exits.parquet", "year=2022/part.parquet"]
    assert panel["year=2022/part.parquet"]["session"].to_list() == [CUTOFF]
    assert panel["instrument_exits.parquet"].to_dicts() == [{"instrument_id": "KRX:000030", "last_session": BEFORE}]
    universe = _read_derived_frames(data_root, DERIVED_UNIVERSE_DATASET_ID)
    assert list(universe) == [f"session={CUTOFF.isoformat()}/part.parquet"]
    assert universe[f"session={CUTOFF.isoformat()}/part.parquet"].height == 1
    for dataset, snapshot in before_sources.items():
        assert _snapshot_tree(data_root, dataset) == snapshot


def test_availability_boundary(tmp_path: Path) -> None:
    """Facts before 2022-01-01 UTC are dropped while the exact floor fact keeps its evidence references."""
    data_root = tmp_path / "data"
    _write_source_import(data_root, SOURCE_PANEL_DATASET_ID, {"year=2022/part.parquet": _panel_frame([CUTOFF])})
    _write_source_import(
        data_root, SOURCE_UNIVERSE_DATASET_ID, {f"session={CUTOFF.isoformat()}/part.parquet": _universe_frame(CUTOFF)}
    )
    _write_source_import(
        data_root,
        SOURCE_FACTS_DATASET_ID,
        {
            "part-00000.parquet": _facts_frame(
                [("20211231000001", PRE_FLOOR, 5.0, "b" * 64), ("20220101000001", FLOOR, 10.0, "a" * 64)]
            )
        },
    )
    summary = materialize_research_scope(data_root)
    assert summary.source_rows[DERIVED_FACTS_DATASET_ID] == 2
    assert summary.retained_rows[DERIVED_FACTS_DATASET_ID] == 1
    facts = _read_derived_frames(data_root, DERIVED_FACTS_DATASET_ID)
    assert list(facts) == ["part-00000.parquet"]
    frame = facts["part-00000.parquet"]
    assert frame.height == 1
    assert frame["filing_id"].to_list() == ["20220101000001"]
    assert frame["available_at"].to_list() == [FLOOR]
    assert frame["value"].to_list() == [10.0]
    assert frame["source_hash"].to_list() == ["a" * 64]


def test_annual_partition_filtering(tmp_path: Path) -> None:
    """A 2022 panel part spanning the cutoff keeps its path but loses every earlier row."""
    data_root = tmp_path / "data"
    _write_source_import(
        data_root, SOURCE_PANEL_DATASET_ID, {"year=2022/part.parquet": _panel_frame([BEFORE, CUTOFF, AFTER])}
    )
    _write_source_import(
        data_root, SOURCE_UNIVERSE_DATASET_ID, {f"session={CUTOFF.isoformat()}/part.parquet": _universe_frame(CUTOFF)}
    )
    _write_source_import(
        data_root, SOURCE_FACTS_DATASET_ID, {"part-00000.parquet": _facts_frame([("20220101000001", FLOOR, 10.0, "a" * 64)])}
    )
    summary = materialize_research_scope(data_root)
    assert summary.source_rows[DERIVED_PANEL_DATASET_ID] == 3
    assert summary.retained_rows[DERIVED_PANEL_DATASET_ID] == 2
    frame = _read_derived_frames(data_root, DERIVED_PANEL_DATASET_ID)["year=2022/part.parquet"]
    assert frame["session"].to_list() == [CUTOFF, AFTER]
    assert frame["close"].to_list() == [71000, 71000]


def test_corrupt_source_aborts_without_publication(tmp_path: Path) -> None:
    """A source part whose bytes disagree with its manifest blocks every derived publication."""
    data_root = tmp_path / "data"
    _seed_minimal(data_root)
    target = data_root / "imports" / SOURCE_PANEL_DATASET_ID / "year=2022/part.parquet"
    with target.open("ab+") as handle:
        handle.write(b"tampered")
    with pytest.raises(ValueError, match="hash mismatch"):
        materialize_research_scope(data_root)
    assert not (data_root / "imports" / DERIVED_PANEL_DATASET_ID / "manifest.json").exists()
    assert not (data_root / "imports" / "retention_provenance.json").exists()
    assert not list((data_root / "imports").glob(".staging-*"))


def test_immutable_retry(tmp_path: Path) -> None:
    """Identical reruns succeed without rewriting while changed source inputs fail as conflicts."""
    data_root = tmp_path / "data"
    _seed_minimal(data_root)
    first = materialize_research_scope(data_root)
    snapshots = {
        dataset: _snapshot_tree(data_root, dataset)
        for dataset in (DERIVED_PANEL_DATASET_ID, DERIVED_UNIVERSE_DATASET_ID, DERIVED_FACTS_DATASET_ID)
    }
    mtimes = {
        dataset: {
            path.relative_to(data_root).as_posix(): path.stat().st_mtime_ns
            for path in sorted((data_root / "imports" / dataset).rglob("*"))
            if path.is_file() and not path.is_symlink()
        }
        for dataset in (DERIVED_PANEL_DATASET_ID, DERIVED_UNIVERSE_DATASET_ID, DERIVED_FACTS_DATASET_ID)
    }
    second = materialize_research_scope(data_root)
    assert second.source_rows == first.source_rows
    assert second.retained_rows == first.retained_rows
    for dataset, snapshot in snapshots.items():
        assert _snapshot_tree(data_root, dataset) == snapshot
        current = {
            path.relative_to(data_root).as_posix(): path.stat().st_mtime_ns
            for path in sorted((data_root / "imports" / dataset).rglob("*"))
            if path.is_file() and not path.is_symlink()
        }
        assert current == mtimes[dataset]
    target = data_root / "imports" / SOURCE_PANEL_DATASET_ID / "year=2022/part.parquet"
    _panel_frame([CUTOFF, AFTER]).write_parquet(target)
    manifest_path = data_root / "imports" / SOURCE_PANEL_DATASET_ID / "manifest.json"
    manifest = ImportManifest(
        SOURCE_PANEL_DATASET_ID, "2" * 64, datetime.now(UTC), (ImportPart(PurePosixPath("year=2022/part.parquet"), _sha_of(target), target.stat().st_size),)
    )
    manifest_path.write_text(_manifest_payload(manifest) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="conflict"):
        materialize_research_scope(data_root)
    for dataset, snapshot in snapshots.items():
        assert _snapshot_tree(data_root, dataset) == snapshot


def test_retain_build_reports_verified_scope(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The retain-build command prints derived IDs, counts, bytes, and the provenance path."""
    from src.cli.main import main

    data_root = tmp_path / "data"
    _seed_minimal(data_root)
    assert main(["data", "retain-build", "--data-root", str(data_root)]) == 0
    document = json.loads(capsys.readouterr().out.strip())
    assert document["dataset_ids"] == [DERIVED_PANEL_DATASET_ID, DERIVED_UNIVERSE_DATASET_ID, DERIVED_FACTS_DATASET_ID]
    assert document["retained_rows"][DERIVED_PANEL_DATASET_ID] == 2
    assert document["source_rows"][DERIVED_PANEL_DATASET_ID] == 2
    assert document["source_bytes"][DERIVED_PANEL_DATASET_ID] > 0
    assert document["retained_bytes"][DERIVED_PANEL_DATASET_ID] > 0
    assert Path(document["provenance"]).is_file()
    provenance = json.loads(Path(document["provenance"]).read_text(encoding="utf-8"))
    assert set(provenance["datasets"]) == set(document["dataset_ids"])
    entry = provenance["datasets"][DERIVED_PANEL_DATASET_ID]
    assert entry["source_dataset_id"] == SOURCE_PANEL_DATASET_ID
    assert entry["first_market_session"] == CUTOFF.isoformat()
    assert entry["first_financial_available_at"] == FLOOR.isoformat()


def _fresh_materialized(tmp_path: Path, name: str) -> Path:
    data_root = tmp_path / name
    _seed_minimal(data_root)
    materialize_research_scope(data_root)
    return data_root


def _refresh_derived_part(data_root: Path, derived_id: str, rel: str, frame: pl.DataFrame) -> None:
    """Rewrite one derived part and refresh its manifest and provenance entries consistently."""
    target = data_root / "imports" / derived_id / rel
    frame.write_parquet(target)
    digest = _sha_of(target)
    size = target.stat().st_size
    manifest_path = data_root / "imports" / derived_id / "manifest.json"
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    for item in document["parts"]:
        if item["path"] == rel:
            item["sha256"] = digest
            item["bytes"] = size
    manifest_path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    provenance_path = data_root / "imports" / "retention_provenance.json"
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    entry = provenance["datasets"][derived_id]
    entry["derived_manifest_sha256"] = _sha_of(manifest_path)
    entry["output_parts"][rel] = {"sha256": digest, "bytes": size, "rows": frame.height}
    entry["retained_rows"] = sum(part["rows"] for part in entry["output_parts"].values())
    entry["retained_bytes"] = sum(part["bytes"] for part in entry["output_parts"].values())
    provenance_path.write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def test_absent_source_input_fails(tmp_path: Path) -> None:
    """Missing data roots and missing source imports fail before any derived work."""
    with pytest.raises(ValueError, match="missing project data root"):
        materialize_research_scope(tmp_path / "absent-root")
    data_root = tmp_path / "data"
    data_root.mkdir(parents=True)
    with pytest.raises(ValueError, match="missing source import"):
        materialize_research_scope(data_root)
    with pytest.raises(ValueError, match="cutoffs are part of the derived dataset identity"):
        materialize_research_scope(data_root, RetentionPlan(first_market_session=date(2022, 7, 8)))


def test_invalid_source_manifest_fails_closed(tmp_path: Path) -> None:
    """Unreadable, mismatched, and unsafe source manifests are rejected."""
    data_root = tmp_path / "data"
    _seed_minimal(data_root)
    manifest_path = data_root / "imports" / SOURCE_PANEL_DATASET_ID / "manifest.json"
    manifest_path.write_text("not json", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid local manifest"):
        materialize_research_scope(data_root)
    manifest = ImportManifest(
        "other_dataset",
        "1" * 64,
        datetime.now(UTC),
        (ImportPart(PurePosixPath("year=2022/part.parquet"), "0" * 64, 10),),
    )
    manifest_path.write_text(_manifest_payload(manifest) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid local manifest"):
        materialize_research_scope(data_root)
    target = data_root / "imports" / SOURCE_PANEL_DATASET_ID / "year=2022/part.parquet"
    manifest = ImportManifest(
        SOURCE_PANEL_DATASET_ID,
        "1" * 64,
        datetime.now(UTC),
        (ImportPart(PurePosixPath("../escape.parquet"), _sha_of(target), target.stat().st_size),),
    )
    manifest_path.write_text(_manifest_payload(manifest) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid local manifest"):
        materialize_research_scope(data_root)


def test_missing_and_invalid_parts_fail(tmp_path: Path) -> None:
    """Listed but absent parts and non-parquet payloads fail without publication."""
    data_root = tmp_path / "data"
    manifest = _write_source_import(
        data_root, SOURCE_PANEL_DATASET_ID, {"year=2022/part.parquet": _panel_frame([CUTOFF])}
    )
    (data_root / "imports" / SOURCE_PANEL_DATASET_ID / "year=2022/part.parquet").unlink()
    with pytest.raises(ValueError, match="missing local part"):
        materialize_research_scope(data_root)
    target = data_root / "imports" / SOURCE_PANEL_DATASET_ID / "year=2022/part.parquet"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"not parquet")
    manifest = ImportManifest(
        SOURCE_PANEL_DATASET_ID,
        manifest.source_manifest_sha256,
        datetime.now(UTC),
        (ImportPart(PurePosixPath("year=2022/part.parquet"), _sha_of(target), target.stat().st_size),),
    )
    (data_root / "imports" / SOURCE_PANEL_DATASET_ID / "manifest.json").write_text(
        _manifest_payload(manifest) + "\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="invalid local part"):
        materialize_research_scope(data_root)


def test_schema_mismatch_fails(tmp_path: Path) -> None:
    """Parts missing filter columns or carrying mistyped keys are rejected."""
    case = tmp_path / "case-universe"
    _write_source_import(case, SOURCE_PANEL_DATASET_ID, {"year=2022/part.parquet": _panel_frame([CUTOFF])})
    _write_source_import(
        case,
        SOURCE_UNIVERSE_DATASET_ID,
        {f"session={CUTOFF.isoformat()}/part.parquet": pl.DataFrame({"ticker": ["005930"]})},
    )
    _write_source_import(
        case, SOURCE_FACTS_DATASET_ID, {"part-00000.parquet": _facts_frame([("20220101000001", FLOOR, 10.0, "a" * 64)])}
    )
    with pytest.raises(ValueError, match="schema mismatch"):
        materialize_research_scope(case)
    case = tmp_path / "case-panel-dtype"
    _write_source_import(
        case,
        SOURCE_PANEL_DATASET_ID,
        {
            "year=2022/part.parquet": pl.DataFrame(
                {"instrument_id": ["KRX:005930"], "session": [CUTOFF.isoformat()], "close": [71000]}
            )
        },
    )
    _write_source_import(
        case, SOURCE_UNIVERSE_DATASET_ID, {f"session={CUTOFF.isoformat()}/part.parquet": _universe_frame(CUTOFF)}
    )
    _write_source_import(
        case, SOURCE_FACTS_DATASET_ID, {"part-00000.parquet": _facts_frame([("20220101000001", FLOOR, 10.0, "a" * 64)])}
    )
    with pytest.raises(ValueError, match="schema mismatch"):
        materialize_research_scope(case)
    case = tmp_path / "case-facts-dtype"
    _write_source_import(case, SOURCE_PANEL_DATASET_ID, {"year=2022/part.parquet": _panel_frame([CUTOFF])})
    _write_source_import(
        case, SOURCE_UNIVERSE_DATASET_ID, {f"session={CUTOFF.isoformat()}/part.parquet": _universe_frame(CUTOFF)}
    )
    _write_source_import(
        case,
        SOURCE_FACTS_DATASET_ID,
        {
            "part-00000.parquet": pl.DataFrame(
                {
                    "dart_corp_code": ["01386916"],
                    "filing_id": ["20220101000001"],
                    "fact": ["assets"],
                    "available_at": [FLOOR.isoformat()],
                    "value": [10.0],
                    "unit": ["KRW"],
                    "source_hash": ["a" * 64],
                }
            )
        },
    )
    with pytest.raises(ValueError, match="schema mismatch"):
        materialize_research_scope(case)
    case = tmp_path / "case-facts-naive"
    _write_source_import(case, SOURCE_PANEL_DATASET_ID, {"year=2022/part.parquet": _panel_frame([CUTOFF])})
    _write_source_import(
        case, SOURCE_UNIVERSE_DATASET_ID, {f"session={CUTOFF.isoformat()}/part.parquet": _universe_frame(CUTOFF)}
    )
    _write_source_import(
        case,
        SOURCE_FACTS_DATASET_ID,
        {
            "part-00000.parquet": pl.DataFrame(
                {
                    "dart_corp_code": ["01386916"],
                    "filing_id": ["20220101000001"],
                    "fact": ["assets"],
                    "available_at": [datetime(2022, 1, 1)],
                    "value": [10.0],
                    "unit": ["KRW"],
                    "source_hash": ["a" * 64],
                }
            )
        },
    )
    with pytest.raises(ValueError, match="schema mismatch"):
        materialize_research_scope(case)


def test_empty_scope_is_incomplete(tmp_path: Path) -> None:
    """A source with no rows inside the retained range never activates a derived dataset."""
    data_root = tmp_path / "data"
    _write_source_import(data_root, SOURCE_PANEL_DATASET_ID, {"year=2022/part.parquet": _panel_frame([CUTOFF])})
    _write_source_import(
        data_root, SOURCE_UNIVERSE_DATASET_ID, {f"session={CUTOFF.isoformat()}/part.parquet": _universe_frame(CUTOFF)}
    )
    _write_source_import(
        data_root,
        SOURCE_FACTS_DATASET_ID,
        {"part-00000.parquet": _facts_frame([("20211231000001", PRE_FLOOR, 5.0, "b" * 64)])},
    )
    with pytest.raises(ValueError, match="incomplete derived dataset"):
        materialize_research_scope(data_root)
    assert not (data_root / "imports" / DERIVED_FACTS_DATASET_ID / "manifest.json").exists()
    data_root = tmp_path / "degenerate-panel"
    _write_source_import(
        data_root,
        SOURCE_PANEL_DATASET_ID,
        {
            "instrument_exits.parquet": pl.DataFrame(
                {"instrument_id": pl.Series([], dtype=pl.String), "last_session": pl.Series([], dtype=pl.Date)}
            )
        },
    )
    _write_source_import(
        data_root, SOURCE_UNIVERSE_DATASET_ID, {f"session={CUTOFF.isoformat()}/part.parquet": _universe_frame(CUTOFF)}
    )
    _write_source_import(
        data_root, SOURCE_FACTS_DATASET_ID, {"part-00000.parquet": _facts_frame([("20220101000001", FLOOR, 10.0, "a" * 64)])}
    )
    with pytest.raises(ValueError, match="incomplete derived dataset"):
        materialize_research_scope(data_root)


def test_changed_content_same_manifest_fails(tmp_path: Path) -> None:
    """Changed selected content under an existing derived ID is a conflict even without a new manifest hash."""
    data_root = _fresh_materialized(tmp_path, "data")
    target = data_root / "imports" / SOURCE_PANEL_DATASET_ID / "year=2022/part.parquet"
    _panel_frame([CUTOFF, AFTER]).write_parquet(target)
    manifest_path = data_root / "imports" / SOURCE_PANEL_DATASET_ID / "manifest.json"
    manifest = ImportManifest(
        SOURCE_PANEL_DATASET_ID,
        "1" * 64,
        datetime.now(UTC),
        (ImportPart(PurePosixPath("year=2022/part.parquet"), _sha_of(target), target.stat().st_size),),
    )
    manifest_path.write_text(_manifest_payload(manifest) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="conflict"):
        materialize_research_scope(data_root)


def test_derived_tampering_fails(tmp_path: Path) -> None:
    """Tampered derived bytes, manifests, and provenance records fail instead of being trusted."""
    data_root = _fresh_materialized(tmp_path, "tampered-bytes")
    target = data_root / "imports" / DERIVED_PANEL_DATASET_ID / "year=2022/part.parquet"
    with target.open("ab+") as handle:
        handle.write(b"tampered")
    with pytest.raises(ValueError, match="hash mismatch"):
        materialize_research_scope(data_root)
    data_root = _fresh_materialized(tmp_path, "tampered-manifest")
    manifest_path = data_root / "imports" / DERIVED_PANEL_DATASET_ID / "manifest.json"
    with manifest_path.open("a", encoding="utf-8") as handle:
        handle.write(" ")
    with pytest.raises(ValueError, match="conflict"):
        materialize_research_scope(data_root)
    data_root = _fresh_materialized(tmp_path, "tampered-provenance-entry")
    provenance_path = data_root / "imports" / "retention_provenance.json"
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    del provenance["datasets"][DERIVED_PANEL_DATASET_ID]
    provenance_path.write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="conflict"):
        materialize_research_scope(data_root)
    data_root = _fresh_materialized(tmp_path, "tampered-provenance-counts")
    provenance_path = data_root / "imports" / "retention_provenance.json"
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    provenance["datasets"][DERIVED_PANEL_DATASET_ID]["retained_rows"] += 1
    provenance_path.write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="conflict"):
        materialize_research_scope(data_root)
    data_root = _fresh_materialized(tmp_path, "tampered-provenance-inputs")
    provenance_path = data_root / "imports" / "retention_provenance.json"
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    del provenance["datasets"][DERIVED_PANEL_DATASET_ID]["input_parts"]["instrument_exits.parquet"]
    provenance_path.write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="conflict"):
        materialize_research_scope(data_root)


def test_derived_boundary_and_schema_tampering_fails(tmp_path: Path) -> None:
    """Derived parts smuggling pre-cutoff rows or reshaped columns fail verification."""
    data_root = _fresh_materialized(tmp_path, "boundary-panel")
    _refresh_derived_part(data_root, DERIVED_PANEL_DATASET_ID, "year=2022/part.parquet", _panel_frame([BEFORE, CUTOFF]))
    with pytest.raises(ValueError, match="boundary violated"):
        materialize_research_scope(data_root)
    data_root = _fresh_materialized(tmp_path, "boundary-facts")
    _refresh_derived_part(
        data_root,
        DERIVED_FACTS_DATASET_ID,
        "part-00000.parquet",
        _facts_frame([("20211231000001", PRE_FLOOR, 5.0, "b" * 64), ("20220101000001", FLOOR, 10.0, "a" * 64)]),
    )
    with pytest.raises(ValueError, match="boundary violated"):
        materialize_research_scope(data_root)
    data_root = _fresh_materialized(tmp_path, "schema-universe")
    _refresh_derived_part(
        data_root,
        DERIVED_UNIVERSE_DATASET_ID,
        f"session={CUTOFF.isoformat()}/part.parquet",
        _universe_frame(CUTOFF).with_columns(pl.lit("extra").alias("reshaped")),
    )
    with pytest.raises(ValueError, match="schema mismatch"):
        materialize_research_scope(data_root)
    data_root = _fresh_materialized(tmp_path, "corrupt-provenance")
    provenance_path = data_root / "imports" / "retention_provenance.json"
    provenance_path.write_text("not json", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid provenance record"):
        materialize_research_scope(data_root)
    provenance_path.write_text(json.dumps({"provenance_version": "other"}) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid provenance record"):
        materialize_research_scope(data_root)


def test_missing_derived_part_is_repaired(tmp_path: Path) -> None:
    """A deleted derived part is restored from identical source inputs without touching verified files."""
    data_root = _fresh_materialized(tmp_path, "data")
    universe_target = data_root / "imports" / DERIVED_UNIVERSE_DATASET_ID / f"session={CUTOFF.isoformat()}/part.parquet"
    exits_target = data_root / "imports" / DERIVED_PANEL_DATASET_ID / "instrument_exits.parquet"
    expected = {universe_target: universe_target.read_bytes(), exits_target: exits_target.read_bytes()}
    snapshots = {
        dataset: {path: path.read_bytes() for path in sorted((data_root / "imports" / dataset).rglob("*.parquet"))}
        for dataset in (DERIVED_PANEL_DATASET_ID, DERIVED_FACTS_DATASET_ID)
    }
    universe_target.unlink()
    exits_target.unlink()
    summary = materialize_research_scope(data_root)
    for target, payload in expected.items():
        assert target.read_bytes() == payload
    assert summary.retained_rows[DERIVED_UNIVERSE_DATASET_ID] == 1
    assert summary.retained_rows[DERIVED_PANEL_DATASET_ID] == 2
    for files in snapshots.values():
        for path, payload in files.items():
            if path not in expected:
                assert path.read_bytes() == payload


def test_staging_residue_and_stray_files(tmp_path: Path) -> None:
    """Crash residue staging is reset while stray derived files without lineage are conflicts."""
    data_root = tmp_path / "staging"
    _seed_minimal(data_root)
    residue = data_root / "imports" / f".staging-{DERIVED_PANEL_DATASET_ID}"
    residue.mkdir(parents=True, exist_ok=True)
    (residue / "junk.parquet").write_bytes(b"junk")
    materialize_research_scope(data_root)
    assert not residue.exists()
    assert (data_root / "imports" / DERIVED_PANEL_DATASET_ID / "year=2022/part.parquet").is_file()
    data_root = tmp_path / "stray"
    _seed_minimal(data_root)
    stray = data_root / "imports" / DERIVED_PANEL_DATASET_ID / "year=2022/part.parquet"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_bytes(b"stray")
    with pytest.raises(ValueError, match="conflict"):
        materialize_research_scope(data_root)
