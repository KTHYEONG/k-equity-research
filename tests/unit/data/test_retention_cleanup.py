"""Invariant guards for referentially safe local retirement."""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath

import polars as pl
import pytest

from src.core.buyback_document import ParsedBuyback
from src.data.catalog import Catalog, FilingVersion
from src.data.event_store import EventStore
from src.data.financial_evidence import FinancialEvidence
from src.data.local_lake import LocalLake
from src.data.retention import (
    DERIVED_FACTS_DATASET_ID,
    DERIVED_PANEL_DATASET_ID,
    DERIVED_UNIVERSE_DATASET_ID,
    SOURCE_FACTS_DATASET_ID,
    SOURCE_PANEL_DATASET_ID,
    SOURCE_UNIVERSE_DATASET_ID,
)
from src.data.retention_cleanup import (
    execute_local_retirement,
    plan_local_retirement,
)

CORP = "01386916"
FORM = "주요사항보고서(자기주식취득결정)"
RCEPT = "20240531000001"
RECEIPT_DAY = date(2024, 5, 31)
KNOWN_AT = datetime(2024, 6, 3, tzinfo=UTC)
SNAP_DART = "snap-dart"
SNAP_KRX = "snap-krx"
SNAP_OLD = "snap-old"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _frame(rows: list[dict[str, object]]) -> pl.DataFrame:
    return pl.DataFrame(rows)


def _write_dataset(
    root: Path, dataset_id: str, files: dict[str, pl.DataFrame], source_sha: str = "1" * 64
) -> dict[str, str]:
    digests: dict[str, str] = {}
    parts = []
    for rel, frame in files.items():
        target = root / "imports" / dataset_id / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        frame.write_parquet(target)
        digest = _sha(target.read_bytes())
        digests[rel] = digest
        parts.append({"path": rel, "sha256": digest, "bytes": target.stat().st_size})
    manifest = {
        "dataset_id": dataset_id,
        "source_manifest_sha256": source_sha,
        "imported_at": datetime.now(UTC).isoformat(),
        "parts": parts,
    }
    (root / "imports" / dataset_id / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return digests


def _record(filing: str, amount: str, hint: float | None = None) -> dict[str, object]:
    return {
        "account_id": "ifrs-full_Assets",
        "consolidated": True,
        "corp_code": CORP,
        "currency": "KRW",
        "fact": "assets",
        "filing_id": filing,
        "fiscal_period": "2024Q1",
        "ord": "7",
        "rcept_no": filing,
        "sj_div": "BS",
        "thstrm_amount": amount,
        "unit": "KRW",
        "value": hint if hint is not None else float(amount),
    }


def _store_evidence(root: Path, records: list[dict[str, object]]) -> str:
    raw = json.dumps({"records": records}, sort_keys=True).encode()
    digest = _sha(raw)
    base = root / "imports" / "financial_evidence" / digest
    base.mkdir(parents=True, exist_ok=True)
    (base / "payload.json").write_bytes(raw)
    (base / "receipt.json").write_text(json.dumps({"content_hash": digest}), encoding="utf-8")
    return digest


def _register(
    catalog: Catalog, source: str, endpoint: str, key: str, snap: str, raw: bytes, path: str
) -> str:
    return catalog.register_artifact(
        source, endpoint, key, snap, raw, KNOWN_AT, PurePosixPath(path)
    )


def _filing(rcept: str, digest: str) -> FilingVersion:
    return FilingVersion(
        rcept, CORP, RECEIPT_DAY, FORM, "361610", digest, KNOWN_AT, KNOWN_AT,
        "HISTORICAL_BACKFILL", False, False, None, "ORIGINAL", "DATE_ONLY",
    )


def _rig(
    tmp_path: Path,
    *,
    pilot: bool = True,
    corrupt_run: bool = False,
    second_pair: bool = False,
    extras: bool = True,
    index_complete: bool = True,
) -> dict[str, object]:
    root = tmp_path / "data"
    catalog = Catalog(root / "catalog.sqlite")
    store = EventStore(catalog)

    filing_digest = _register(catalog, "dart", "document", RCEPT, SNAP_DART, b"PK-doc", f"raw/dart/{SNAP_DART}/doc-{RCEPT}.zip")
    catalog.upsert_filing(_filing(RCEPT, filing_digest))
    store.store_parsed_batch(
        [_filing(RCEPT, filing_digest)],
        [ParsedBuyback(RCEPT, CORP, RECEIPT_DAY, (), filing_digest, "COMPLETE")],
    )
    catalog.save_checkpoint("dart", "2024-05-30:2024-06-04", SNAP_DART, "page-1")

    krx_raw = b'{"krx": "20240530"}'
    krx_digest = _register(catalog, "krx", "index", "KOSPI:20240530", SNAP_KRX, krx_raw, f"raw/krx/{SNAP_KRX}/KOSPI-20240530.json")
    catalog.save_checkpoint("krx_index", "KOSPI:2024-05-30", SNAP_KRX, "complete")
    krx_kosdaq = _register(
        catalog, "krx", "index", "KOSDAQ:20240530", SNAP_KRX,
        b'{"krx": "KOSDAQ:20240530"}', f"raw/krx/{SNAP_KRX}/KOSDAQ-20240530.json",
    )
    manifest_dir = root / "krx" / "manifests"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_doc = {
        "version": 1,
        "entries": [
            {"market": "KOSPI", "session": "2024-05-30", "sha256": krx_digest},
            *([{"market": "KOSDAQ", "session": "2024-05-30", "sha256": krx_kosdaq}] if index_complete else []),
        ],
    }
    manifest_raw = (json.dumps(manifest_doc, indent=2, sort_keys=True) + "\n").encode()
    (manifest_dir / f"{_sha(manifest_raw)}.json").write_bytes(manifest_raw)

    kept_lineage = _register(
        catalog, "k-stock-engine", "daily_market", "kept-digest", SNAP_DART,
        b"kept-payload", "raw/imported/daily_market/kept-digest/payload.json",
    )
    _register(
        catalog, "k-stock-engine", "daily_market:receipt", "kept-digest", SNAP_DART,
        b"kept-receipt", "raw/imported/daily_market/kept-digest/receipt.json",
    )
    old_payload = _register(
        catalog, "k-stock-engine", "daily_market", "old-digest", SNAP_OLD,
        b"old-payload", "raw/imported/daily_market/old-digest/payload.json",
    )
    old_receipt = _register(
        catalog, "k-stock-engine", "daily_market:receipt", "old-digest", SNAP_OLD,
        b"old-receipt", "raw/imported/daily_market/old-digest/receipt.json",
    )
    pair_keys = [
        ("k-stock-engine", "daily_market", "old-digest", SNAP_OLD),
        ("k-stock-engine", "daily_market:receipt", "old-digest", SNAP_OLD),
    ]
    pair_paths = [
        root / "raw" / "imported" / "daily_market" / "old-digest" / "payload.json",
        root / "raw" / "imported" / "daily_market" / "old-digest" / "receipt.json",
    ]
    if second_pair:
        _register(catalog, "k-stock-engine", "daily_market", "old2", SNAP_OLD, b"p2", "raw/imported/daily_market/old2/payload.json")
        _register(catalog, "k-stock-engine", "daily_market:receipt", "old2", SNAP_OLD, b"p2r", "raw/imported/daily_market/old2/receipt.json")
        pair_keys.extend(
            [
                ("k-stock-engine", "daily_market", "old2", SNAP_OLD),
                ("k-stock-engine", "daily_market:receipt", "old2", SNAP_OLD),
            ]
        )
        pair_paths.extend(
            [
                root / "raw" / "imported" / "daily_market" / "old2" / "payload.json",
                root / "raw" / "imported" / "daily_market" / "old2" / "receipt.json",
            ]
        )

    cited_raw = _register(
        catalog, "dart", "financial-statement", f"{CORP}:2024:11011:CFS", SNAP_OLD,
        b'{"cited": true}', f"raw/dart/financial/{SNAP_OLD}/{CORP}-2024-11011-CFS.json",
    )
    kept_financial = _register(
        catalog, "dart", "financial-statement", f"{CORP}:2024:11013:CFS", SNAP_DART,
        b'{"kept": true}', f"raw/dart/financial/{SNAP_DART}/{CORP}-2024-11013-CFS.json",
    )
    kept_parquet = root / "financial" / SNAP_DART / f"{CORP}-2024-11013-CFS.parquet"
    kept_parquet.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({"a": [1]}).write_parquet(kept_parquet)
    ghost_parquet = root / "financial" / SNAP_OLD / "ghost.parquet"
    ghost_parquet.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({"a": [2]}).write_parquet(ghost_parquet)
    (root / "financial" / SNAP_OLD / "dir.parquet").mkdir(parents=True, exist_ok=True)

    evidence_digest = _store_evidence(root, [_record(RCEPT, "1000")])
    obsolete_evidence = root / "imports" / "financial_evidence" / ("b" * 64)
    obsolete_evidence.mkdir(parents=True, exist_ok=True)
    (obsolete_evidence / "payload.json").write_bytes(b'{"records": []}')
    (obsolete_evidence / "receipt.json").write_text("{}", encoding="utf-8")

    _write_dataset(
        root,
        DERIVED_PANEL_DATASET_ID,
        {"part-00000.parquet": _frame([{"instrument_id": "KRX:005930", "session": date(2024, 5, 30), "market": "KOSPI", "source_hash": kept_lineage}])},
    )
    _write_dataset(
        root,
        DERIVED_UNIVERSE_DATASET_ID,
        {"part-00000.parquet": _frame([{"instrument_id": "KRX:005930", "ticker": "005930", "market": "KOSPI", "source_security_id": "S1", "share_kind": "보통주", "session": date(2024, 5, 30), "available_at": KNOWN_AT}])},
    )
    _write_dataset(
        root,
        DERIVED_FACTS_DATASET_ID,
        {"part-00000.parquet": _frame([{"dart_corp_code": CORP, "filing_id": RCEPT, "fact": "assets", "fiscal_period": "2024Q1", "consolidated": True, "available_at": KNOWN_AT, "value": 1000.0, "unit": "KRW", "source_hash": evidence_digest}])},
    )
    for dataset_id in (SOURCE_PANEL_DATASET_ID, SOURCE_UNIVERSE_DATASET_ID, SOURCE_FACTS_DATASET_ID):
        _write_dataset(root, dataset_id, {"part-00000.parquet": _frame([{"session": date(2020, 1, 1)}])})

    run_ids: list[str] = []
    if pilot or corrupt_run:
        run_id = "run-pilot"
        run_dir = root / "reports" / "evt-1" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        memo = {"evidence": [{"hash": cited_raw}], "note": "pilot"}
        if corrupt_run:
            (run_dir / "memo.json").write_bytes(b"not json")
        else:
            (run_dir / "memo.json").write_text(json.dumps(memo), encoding="utf-8")
        manifest_content = (json.dumps({"run": run_id, "hash": cited_raw}, sort_keys=True) + "\n").encode()
        manifest_path = run_dir / "manifest.json"
        manifest_path.write_bytes(manifest_content)
        catalog.register_research_run(run_id, _sha(manifest_content), "COMPLETE", PurePosixPath(f"reports/evt-1/{run_id}/manifest.json"))
        run_ids.append(run_id)

    staging_paths: list[Path] = []
    if extras:
        staging_file = root / "staging" / "junk.bin"
        staging_file.parent.mkdir(parents=True, exist_ok=True)
        staging_file.write_bytes(b"junk")
        staging_paths.append(staging_file)
        probe_file = root / "probe_old" / "note.txt"
        probe_file.parent.mkdir(parents=True, exist_ok=True)
        probe_file.write_bytes(b"probe")
        staging_paths.append(probe_file)
        probe_top = root / "probe_top.txt"
        probe_top.write_text("top", encoding="utf-8")
        staging_paths.append(probe_top)
        dot_file = root / "imports" / ".staging-x" / "tmp.bin"
        dot_file.parent.mkdir(parents=True, exist_ok=True)
        dot_file.write_bytes(b"tmp")
        staging_paths.append(dot_file)
        partial = root / "raw" / "leftover.partial"
        partial.write_bytes(b"partial")
        staging_paths.append(partial)
        (root / "imports" / "financial_evidence" / "notes.txt").write_text("notes", encoding="utf-8")

    return {
        "root": root,
        "catalog": catalog,
        "store": store,
        "pair_keys": pair_keys,
        "pair_paths": pair_paths,
        "cited_raw": cited_raw,
        "cited_path": root / "raw" / "dart" / "financial" / SNAP_OLD / f"{CORP}-2024-11011-CFS.json",
        "filing_digest": filing_digest,
        "filing_path": root / "raw" / "dart" / SNAP_DART / f"doc-{RCEPT}.zip",
        "evidence_digest": evidence_digest,
        "obsolete_evidence": obsolete_evidence,
        "staging_paths": staging_paths,
        "run_ids": run_ids,
        "ghost_parquet": ghost_parquet,
        "kept_parquet": kept_parquet,
    }


def test_active_parts_never_obsolete(tmp_path: Path) -> None:
    """Active scoped parts stay retained while superseded sources are listed exactly once."""
    rig = _rig(tmp_path)
    plan = plan_local_retirement(rig["root"], rig["catalog"], rig["store"])  # type: ignore[arg-type]
    assert plan.blocking_run_ids == ("run-pilot",)
    for dataset_id in (DERIVED_PANEL_DATASET_ID, DERIVED_UNIVERSE_DATASET_ID, DERIVED_FACTS_DATASET_ID):
        part = rig["root"] / "imports" / dataset_id / "part-00000.parquet"  # type: ignore[operator]
        assert part not in plan.obsolete_import_parts
    for dataset_id in (SOURCE_PANEL_DATASET_ID, SOURCE_UNIVERSE_DATASET_ID, SOURCE_FACTS_DATASET_ID):
        assert rig["root"] / "imports" / dataset_id / "manifest.json" in plan.obsolete_import_parts  # type: ignore[operator]
        assert rig["root"] / "imports" / dataset_id / "part-00000.parquet" in plan.obsolete_import_parts  # type: ignore[operator]
    assert rig["filing_path"] not in plan.obsolete_raw_artifacts
    evidence_payload = rig["root"] / "imports" / "financial_evidence" / rig["evidence_digest"] / "payload.json"  # type: ignore[operator]
    assert evidence_payload not in plan.obsolete_financial_evidence
    assert rig["obsolete_evidence"] / "payload.json" in plan.obsolete_financial_evidence  # type: ignore[operator]
    assert rig["cited_path"] not in plan.obsolete_raw_artifacts
    assert rig["kept_parquet"] not in plan.obsolete_import_parts
    assert rig["ghost_parquet"] in plan.obsolete_import_parts
    for path in rig["staging_paths"]:  # type: ignore[union-attr]
        assert path in plan.obsolete_import_parts
    assert plan.retained_bytes > 0
    assert plan.reclaimable_bytes > 0
    again = plan_local_retirement(rig["root"], rig["catalog"], rig["store"], frozenset())  # type: ignore[arg-type]
    assert again == plan


def test_pilot_citation_blocks_then_retires(tmp_path: Path) -> None:
    """A cited pilot run blocks execution until retired, then retires with its artifacts."""
    rig = _rig(tmp_path)
    plan = plan_local_retirement(rig["root"], rig["catalog"], rig["store"])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="research run reference"):
        execute_local_retirement(plan, rig["root"], rig["catalog"], frozenset())  # type: ignore[arg-type]
    assert rig["cited_path"].is_file()  # type: ignore[union-attr]
    retired = frozenset({*rig["run_ids"], "ghost-run"})  # type: ignore[union-attr]
    approved = plan_local_retirement(rig["root"], rig["catalog"], rig["store"], retired)  # type: ignore[arg-type]
    assert approved.blocking_run_ids == ()
    assert rig["cited_path"] in approved.obsolete_raw_artifacts  # type: ignore[union-attr]
    execute_local_retirement(approved, rig["root"], rig["catalog"], retired)  # type: ignore[arg-type]
    assert not rig["cited_path"].exists()  # type: ignore[union-attr]
    catalog = rig["catalog"]
    assert catalog.find_artifact("dart", "financial-statement", f"{CORP}:2024:11011:CFS", SNAP_OLD) is None  # type: ignore[union-attr]
    rows = catalog._conn.execute("SELECT run_id FROM research_run").fetchall()  # noqa: SLF001
    assert [row["run_id"] for row in rows] == []
    entries = catalog._conn.execute(  # noqa: SLF001
        "SELECT sha256 FROM retirement_entry WHERE retirement_id=?", (approved.plan_digest,)
    ).fetchall()
    assert {row["sha256"] for row in entries}
    assert rig["cited_raw"] in {row["sha256"] for row in entries}  # type: ignore[union-attr]
    record = json.loads((rig["root"] / "retention" / f"retirement-{approved.plan_digest}.json").read_text(encoding="utf-8"))  # type: ignore[operator]
    assert record["retired_run_ids"] == ["ghost-run", "run-pilot"]
    assert record["plan_digest"] == approved.plan_digest
    assert f"raw/dart/financial/{SNAP_OLD}/{CORP}-2024-11011-CFS.json" in record["obsolete_raw_artifacts"]
    again = plan_local_retirement(rig["root"], catalog, rig["store"], retired)  # type: ignore[arg-type]
    assert again.blocking_run_ids == ()
    execute_local_retirement(approved, rig["root"], catalog, retired)  # type: ignore[arg-type]


def test_unknown_run_blocks_without_deletion(tmp_path: Path) -> None:
    """An undecodable research run aborts planning before anything is removed."""
    rig = _rig(tmp_path, corrupt_run=True)
    catalog = rig["catalog"]
    before = {path.read_bytes() for path in rig["pair_paths"]}  # type: ignore[union-attr]
    with pytest.raises(ValueError, match="undecodable"):
        plan_local_retirement(rig["root"], catalog, rig["store"])  # type: ignore[arg-type]
    assert {path.read_bytes() for path in rig["pair_paths"]} == before  # type: ignore[union-attr]
    assert catalog.find_artifact("k-stock-engine", "daily_market", "old-digest", SNAP_OLD) is not None  # type: ignore[union-attr]


def test_paired_payload_receipt_retire_together(tmp_path: Path) -> None:
    """Both lineage rows and files for one request transition in a single retirement."""
    rig = _rig(tmp_path, pilot=False)
    catalog = rig["catalog"]
    plan = plan_local_retirement(rig["root"], catalog, rig["store"])  # type: ignore[arg-type]
    assert plan.blocking_run_ids == ()
    execute_local_retirement(plan, rig["root"], catalog, frozenset())  # type: ignore[arg-type]
    for key in rig["pair_keys"]:  # type: ignore[union-attr]
        assert catalog.find_artifact(*key) is None  # type: ignore[arg-type]
    for path in rig["pair_paths"]:  # type: ignore[union-attr]
        assert not path.exists()
    entries = catalog._conn.execute(  # noqa: SLF001
        "SELECT endpoint FROM retirement_entry WHERE retirement_id=?", (plan.plan_digest,)
    ).fetchall()
    assert {"daily_market", "daily_market:receipt"} <= {row["endpoint"] for row in entries}


def test_interrupted_cleanup_resumes_safely(tmp_path: Path) -> None:
    """A half-finished retirement completes on retry with retained evidence verifiable."""
    rig = _rig(tmp_path, pilot=False, second_pair=True)
    catalog = rig["catalog"]
    plan = plan_local_retirement(rig["root"], catalog, rig["store"])  # type: ignore[arg-type]
    first_keys = tuple(rig["pair_keys"][:2])  # type: ignore[union-attr]
    catalog.retire_artifacts(first_keys, plan.plan_digest)  # type: ignore[arg-type]
    done = execute_local_retirement(plan, rig["root"], catalog, frozenset())  # type: ignore[arg-type]
    assert done.obsolete_import_parts == ()
    assert done.obsolete_raw_artifacts == ()
    assert done.obsolete_financial_evidence == ()
    for key in rig["pair_keys"]:  # type: ignore[union-attr]
        assert catalog.find_artifact(*key) is None  # type: ignore[arg-type]
    lake = LocalLake(
        rig["root"],  # type: ignore[arg-type]
        {
            DERIVED_FACTS_DATASET_ID: _manifest(rig["root"], DERIVED_FACTS_DATASET_ID),  # type: ignore[arg-type]
        },
    )
    financial = FinancialEvidence(rig["root"], lake)  # type: ignore[arg-type]
    facts = financial.facts_asof(CORP, datetime(2024, 6, 4, tzinfo=UTC), frozenset({"assets"}))
    assert [fact.filing_id for fact in facts] == [RCEPT]


def _manifest(root: Path, dataset_id: str) -> object:
    from src.data.imports import ImportManifest, ImportPart

    document = json.loads((root / "imports" / dataset_id / "manifest.json").read_text(encoding="utf-8"))
    return ImportManifest(
        dataset_id=str(document["dataset_id"]),
        source_manifest_sha256=str(document["source_manifest_sha256"]),
        imported_at=datetime.fromisoformat(str(document["imported_at"])),
        parts=tuple(
            ImportPart(PurePosixPath(str(item["path"])), str(item["sha256"]), int(item["bytes"]))
            for item in document["parts"]
        ),
    )


def test_symlink_escape_aborts(tmp_path: Path) -> None:
    """A symlink in staging aborts planning without touching the external target."""
    rig = _rig(tmp_path, pilot=False)
    outside = tmp_path / "outside.txt"
    outside.write_text("external", encoding="utf-8")
    link = rig["root"] / "staging" / "evil.lnk"  # type: ignore[operator]
    link.symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        plan_local_retirement(rig["root"], rig["catalog"], rig["store"])  # type: ignore[arg-type]
    assert outside.read_text(encoding="utf-8") == "external"
    assert rig["pair_paths"][0].is_file()  # type: ignore[union-attr]
    link.unlink()
    nested = rig["root"] / "staging" / "sub"  # type: ignore[operator]
    nested.mkdir(parents=True, exist_ok=True)
    (nested / "inner.lnk").symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        plan_local_retirement(rig["root"], rig["catalog"], rig["store"])  # type: ignore[arg-type]
    assert outside.read_text(encoding="utf-8") == "external"


def test_missing_active_evidence_blocks(tmp_path: Path) -> None:
    """Absent or drifted active files fail the plan instead of silent acceptance."""
    rig = _rig(tmp_path / "p1", pilot=False)
    rig["filing_path"].unlink()  # type: ignore[union-attr]
    with pytest.raises(ValueError, match="missing filing raw artifact"):
        plan_local_retirement(rig["root"], rig["catalog"], rig["store"])  # type: ignore[arg-type]

    rig = _rig(tmp_path / "p1b", pilot=False)
    rig["catalog"]._conn.execute("DELETE FROM raw_artifact WHERE sha256=?", (rig["filing_digest"],))  # noqa: SLF001
    rig["catalog"]._conn.commit()  # noqa: SLF001
    with pytest.raises(ValueError, match="missing filing raw artifact"):
        plan_local_retirement(rig["root"], rig["catalog"], rig["store"])  # type: ignore[arg-type]

    rig = _rig(tmp_path / "p2", pilot=False)
    payload = rig["root"] / "imports" / "financial_evidence" / rig["evidence_digest"] / "payload.json"  # type: ignore[operator]
    payload.unlink()
    with pytest.raises(ValueError, match="financial evidence gap"):
        plan_local_retirement(rig["root"], rig["catalog"], rig["store"])  # type: ignore[arg-type]

    rig = _rig(tmp_path / "p3", pilot=False)
    missing = rig["root"] / "imports" / DERIVED_FACTS_DATASET_ID / "manifest.json"  # type: ignore[operator]
    missing.unlink()
    with pytest.raises(ValueError, match="missing active scoped import"):
        plan_local_retirement(rig["root"], rig["catalog"], rig["store"])  # type: ignore[arg-type]


def test_gap_and_drift_block_planning(tmp_path: Path) -> None:
    """Financial gaps and source drift abort the plan before any deletion."""
    rig = _rig(tmp_path / "p1", pilot=False)
    payload = rig["root"] / "imports" / "financial_evidence" / rig["evidence_digest"] / "payload.json"  # type: ignore[operator]
    payload.write_bytes(b'{"records": []}')
    with pytest.raises(ValueError, match="financial evidence gap"):
        plan_local_retirement(rig["root"], rig["catalog"], rig["store"])  # type: ignore[arg-type]

    rig = _rig(tmp_path / "p2", pilot=False)
    part = rig["root"] / "imports" / DERIVED_PANEL_DATASET_ID / "part-00000.parquet"  # type: ignore[operator]
    with part.open("r+b") as handle:
        handle.seek(0)
        handle.write(b"XX")
    with pytest.raises(ValueError, match="hash mismatch"):
        plan_local_retirement(rig["root"], rig["catalog"], rig["store"])  # type: ignore[arg-type]


def test_changed_state_blocks_execution(tmp_path: Path) -> None:
    """New obsolete objects or altered rows between plan and apply abort execution."""
    rig = _rig(tmp_path / "p1", pilot=False)
    plan = plan_local_retirement(rig["root"], rig["catalog"], rig["store"])  # type: ignore[arg-type]
    _register(rig["catalog"], "dart", "list", "new-window", SNAP_OLD, b"{}", f"raw/dart/{SNAP_OLD}/list-new.json")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="changed since planning"):
        execute_local_retirement(plan, rig["root"], rig["catalog"], frozenset())  # type: ignore[arg-type]

    rig = _rig(tmp_path / "p2", pilot=False)
    plan = plan_local_retirement(rig["root"], rig["catalog"], rig["store"])  # type: ignore[arg-type]
    catalog = rig["catalog"]
    catalog._conn.execute(  # noqa: SLF001
        "DELETE FROM raw_artifact WHERE source='k-stock-engine' AND request_key='old-digest'"
    )
    catalog._conn.commit()
    with pytest.raises(ValueError, match="changed since planning"):
        execute_local_retirement(plan, rig["root"], catalog, frozenset())  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="changed since approval"):
        execute_local_retirement(replace(plan, plan_digest="0" * 64), rig["root"], catalog, frozenset())  # type: ignore[arg-type]


def test_new_active_reference_blocks_previously_planned_deletion(tmp_path: Path) -> None:
    """A planned obsolete file cannot be deleted after becoming filing evidence."""
    rig = _rig(tmp_path, pilot=False)
    root = rig["root"]
    catalog = rig["catalog"]
    store = rig["store"]
    old_raw = root / "raw" / "imported" / "daily_market" / "old-digest" / "payload.json"
    plan = plan_local_retirement(root, catalog, store)  # type: ignore[arg-type]
    assert old_raw in plan.obsolete_raw_artifacts
    catalog.upsert_filing(
        replace(_filing("20240531000002", _sha(old_raw.read_bytes())), report_name="새 공시")
    )  # type: ignore[union-attr]

    with pytest.raises(ValueError, match="changed since planning"):
        execute_local_retirement(plan, root, catalog, frozenset())  # type: ignore[arg-type]
    assert old_raw.is_file()


def test_incomplete_two_market_index_blocks_retirement(tmp_path: Path) -> None:
    """One missing official market bar is an incomplete retained research session."""
    rig = _rig(tmp_path, pilot=False, index_complete=False)
    with pytest.raises(ValueError, match="missing verified index coverage"):
        plan_local_retirement(rig["root"], rig["catalog"], rig["store"])  # type: ignore[arg-type]


def test_catalog_reference_guards(tmp_path: Path) -> None:
    """Run references decode exhaustively and retirement rejects drift and citations."""
    root = tmp_path / "data"
    catalog = Catalog(root / "catalog.sqlite")
    assert catalog.references_to_hashes(frozenset({"a" * 64})) == {}
    raw = b"guarded"
    digest = catalog.register_artifact(
        "dart", "list", "win", "snap", raw, KNOWN_AT, PurePosixPath("raw/dart/snap/page.json")
    )
    with pytest.raises(ValueError, match="unknown retirement key"):
        catalog.retire_artifacts((("dart", "list", "nope", "snap"),), "ret-1")
    catalog.retire_artifacts((), "ret-1")
    run_dir = root / "reports" / "evt" / "run-1"
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest_raw = (json.dumps({"run": "run-1"}) + "\n").encode()
    (run_dir / "manifest.json").write_bytes(manifest_raw)
    (run_dir / "memo.json").write_text(json.dumps({"cites": [digest]}), encoding="utf-8")
    catalog.register_research_run("run-1", _sha(manifest_raw), "COMPLETE", PurePosixPath("reports/evt/run-1/manifest.json"))
    assert catalog.references_to_hashes(frozenset({digest})) == {digest: ("run-1",)}
    with pytest.raises(ValueError, match="research run reference"):
        catalog.retire_artifacts((("dart", "list", "win", "snap"),), "ret-1")
    (run_dir / "manifest.json").unlink()
    with pytest.raises(ValueError, match="unresolvable"):
        catalog.references_to_hashes(frozenset({digest}))
    (run_dir / "manifest.json").write_bytes(manifest_raw)
    target = root / "raw" / "dart" / "snap" / "page.json"
    (run_dir / "extra.json").mkdir()
    with pytest.raises(ValueError, match="unresolvable"):
        catalog.references_to_hashes(frozenset({digest}))
    (run_dir / "extra.json").rmdir()
    target.write_bytes(b"tampered-bytes!!")
    catalog._conn.execute("DELETE FROM research_run WHERE run_id='run-1'")  # noqa: SLF001
    catalog._conn.commit()
    with pytest.raises(ValueError, match="hash drift"):
        catalog.retire_artifacts((("dart", "list", "win", "snap"),), "ret-1")
    target.unlink()
    target.symlink_to(tmp_path / "outside.txt")
    with pytest.raises(ValueError, match="symlink"):
        catalog.retire_artifacts((("dart", "list", "win", "snap"),), "ret-1")


def test_retire_replay_is_idempotent(tmp_path: Path) -> None:
    """Replaying one retirement id under the same id completes without duplication."""
    root = tmp_path / "data"
    catalog = Catalog(root / "catalog.sqlite")
    catalog.register_artifact("dart", "list", "win", "snap", b"{}", KNOWN_AT, PurePosixPath("raw/dart/snap/page.json"))
    catalog.retire_artifacts((("dart", "list", "win", "snap"),), "ret-9")
    catalog.retire_artifacts((("dart", "list", "win", "snap"),), "ret-9")
    rows = catalog._conn.execute(  # noqa: SLF001
        "SELECT COUNT(*) AS n FROM retirement_entry WHERE retirement_id='ret-9'"
    ).fetchone()
    assert int(rows["n"]) == 1


def test_retain_cleanup_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The retain-cleanup command dry-runs a plan and applies only a matching digest."""
    from src.cli.main import main

    rig = _rig(tmp_path, pilot=False)
    root = rig["root"]
    assert main(["data", "retain-cleanup", "--data-root", str(root)]) == 0  # type: ignore[arg-type]
    document = json.loads(capsys.readouterr().out.strip())
    assert document["obsolete_raw_artifacts"] == 3
    assert document["plan_digest"]
    assert main(["data", "retain-cleanup", "--data-root", str(root), "--apply"]) == 2  # type: ignore[arg-type]
    assert "plan-digest" in capsys.readouterr().err
    assert (
        main(["data", "retain-cleanup", "--data-root", str(root), "--apply", "--plan-digest", "0" * 64])  # type: ignore[arg-type]
        == 2
    )
    assert (
        main(  # type: ignore[arg-type]
            ["data", "retain-cleanup", "--data-root", str(root), "--apply", "--plan-digest", document["plan_digest"]]
        )
        == 0
    )
    for key in rig["pair_keys"]:  # type: ignore[union-attr]
        assert rig["catalog"].find_artifact(*key) is None  # type: ignore[arg-type]


def test_guard_helpers(tmp_path: Path) -> None:
    """Roots, run ids, and path checks fail closed on invalid inputs."""
    from src.data.retention_cleanup import _checked_absolute, _plan_relative

    root = tmp_path / "data"
    catalog = Catalog(root / "catalog.sqlite")
    store = EventStore(catalog)
    with pytest.raises(ValueError, match="missing project data root"):
        plan_local_retirement(tmp_path / "absent", catalog, store)
    other = tmp_path / "other"
    other_catalog = Catalog(other / "catalog.sqlite")
    with pytest.raises(ValueError, match="project data root"):
        plan_local_retirement(root, other_catalog, store)
    with pytest.raises(ValueError, match="retired run ids"):
        plan_local_retirement(root, catalog, store, frozenset({""}))
    inside = root / "raw" / "a.json"
    assert _checked_absolute(root.resolve(), inside) == inside
    with pytest.raises(ValueError, match="outside data root"):
        _checked_absolute(root.resolve(), tmp_path / "escape.json")
    assert _plan_relative(root.resolve(), inside) == "raw/a.json"
    with pytest.raises(ValueError, match="outside data root"):
        _plan_relative(root.resolve(), tmp_path / "escape.json")


def _rewrite_manifest(root: Path, dataset_id: str, doc: dict[str, object]) -> None:
    (root / "imports" / dataset_id / "manifest.json").write_text(
        json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _read_manifest_doc(root: Path, dataset_id: str) -> dict[str, object]:
    return json.loads((root / "imports" / dataset_id / "manifest.json").read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def _rewrite_part(root: Path, dataset_id: str, rel: str, frame: pl.DataFrame) -> None:
    target = root / "imports" / dataset_id / rel
    frame.write_parquet(target)
    doc = _read_manifest_doc(root, dataset_id)
    for item in doc["parts"]:  # type: ignore[union-attr]
        if item["path"] == rel:  # type: ignore[index]
            item["sha256"] = _sha(target.read_bytes())  # type: ignore[index]
            item["bytes"] = target.stat().st_size  # type: ignore[index]
    _rewrite_manifest(root, dataset_id, doc)


def test_invalid_active_import_blocks(tmp_path: Path) -> None:
    """Corrupt, mismatched, unsafe, missing, or unreadable scoped parts block planning."""
    rig = _rig(tmp_path / "p1", pilot=False)
    (rig["root"] / "imports" / DERIVED_FACTS_DATASET_ID / "manifest.json").write_bytes(b"not json")  # type: ignore[operator]
    with pytest.raises(ValueError, match="invalid scoped import manifest"):
        plan_local_retirement(rig["root"], rig["catalog"], rig["store"])  # type: ignore[arg-type]

    rig = _rig(tmp_path / "p2", pilot=False)
    doc = _read_manifest_doc(rig["root"], DERIVED_FACTS_DATASET_ID)  # type: ignore[arg-type]
    doc["dataset_id"] = "other"
    _rewrite_manifest(rig["root"], DERIVED_FACTS_DATASET_ID, doc)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="invalid scoped import manifest"):
        plan_local_retirement(rig["root"], rig["catalog"], rig["store"])  # type: ignore[arg-type]

    rig = _rig(tmp_path / "p3", pilot=False)
    doc = _read_manifest_doc(rig["root"], DERIVED_FACTS_DATASET_ID)  # type: ignore[arg-type]
    doc["parts"] = [{"path": "../escape.parquet", "sha256": "a" * 64, "bytes": 8}]  # type: ignore[dict-item]
    _rewrite_manifest(rig["root"], DERIVED_FACTS_DATASET_ID, doc)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unsafe scoped import part"):
        plan_local_retirement(rig["root"], rig["catalog"], rig["store"])  # type: ignore[arg-type]

    rig = _rig(tmp_path / "p4", pilot=False)
    (rig["root"] / "imports" / DERIVED_FACTS_DATASET_ID / "part-00000.parquet").unlink()  # type: ignore[operator]
    with pytest.raises(ValueError, match="missing scoped import part"):
        plan_local_retirement(rig["root"], rig["catalog"], rig["store"])  # type: ignore[arg-type]

    rig = _rig(tmp_path / "p5", pilot=False)
    blob = rig["root"] / "imports" / DERIVED_FACTS_DATASET_ID / "blob.bin"  # type: ignore[operator]
    blob.write_bytes(b"not parquet")
    doc = _read_manifest_doc(rig["root"], DERIVED_FACTS_DATASET_ID)  # type: ignore[arg-type]
    doc["parts"] = [{"path": "blob.bin", "sha256": _sha(b"not parquet"), "bytes": 11}]  # type: ignore[dict-item]
    _rewrite_manifest(rig["root"], DERIVED_FACTS_DATASET_ID, doc)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="invalid scoped import part"):
        plan_local_retirement(rig["root"], rig["catalog"], rig["store"])  # type: ignore[arg-type]


def test_invalid_retained_hash_and_coverage_block(tmp_path: Path) -> None:
    """Bad retained hashes and missing index coverage abort before any deletion."""
    rig = _rig(tmp_path / "p1", pilot=False)
    _rewrite_part(
        rig["root"],  # type: ignore[arg-type]
        DERIVED_FACTS_DATASET_ID,
        "part-00000.parquet",
        _frame([{"dart_corp_code": CORP, "filing_id": RCEPT, "fact": "assets", "fiscal_period": "2024Q1", "consolidated": True, "available_at": KNOWN_AT, "value": 1.0, "unit": "KRW", "source_hash": "zz"}]),
    )
    with pytest.raises(ValueError, match="invalid retained source hash"):
        plan_local_retirement(rig["root"], rig["catalog"], rig["store"])  # type: ignore[arg-type]

    rig = _rig(tmp_path / "p2", pilot=False)
    shutil.rmtree(rig["root"] / "krx" / "manifests")  # type: ignore[operator]
    with pytest.raises(ValueError, match="missing verified index coverage"):
        plan_local_retirement(rig["root"], rig["catalog"], rig["store"])  # type: ignore[arg-type]

    rig = _rig(tmp_path / "p3", pilot=False)
    ghost_doc = {"version": 1, "entries": [{"market": "KOSPI", "session": "2024-05-30", "sha256": "d" * 64}]}
    ghost_raw = (json.dumps(ghost_doc, indent=2, sort_keys=True) + "\n").encode()
    (rig["root"] / "krx" / "manifests" / f"{_sha(ghost_raw)}.json").write_bytes(ghost_raw)  # type: ignore[operator]
    for stale in (rig["root"] / "krx" / "manifests").glob("*.json"):  # type: ignore[operator]
        if stale.name != f"{_sha(ghost_raw)}.json":
            stale.unlink()
    with pytest.raises(ValueError, match="missing indexed raw artifact"):
        plan_local_retirement(rig["root"], rig["catalog"], rig["store"])  # type: ignore[arg-type]

    rig = _rig(tmp_path / "p4", pilot=False)
    (rig["root"] / "raw" / "krx" / SNAP_KRX / "KOSPI-20240530.json").unlink()  # type: ignore[operator]
    with pytest.raises(ValueError, match="missing indexed raw artifact"):
        plan_local_retirement(rig["root"], rig["catalog"], rig["store"])  # type: ignore[arg-type]


def test_event_lineage_blocks(tmp_path: Path) -> None:
    """Ambiguous links and orphaned receipts block retirement until resolved."""
    rig = _rig(tmp_path / "p1", pilot=False)
    catalog = rig["catalog"]
    second = "20240531000002"
    digest = catalog.register_artifact(
        "dart", "document", second, SNAP_DART, b"PK-second",
        KNOWN_AT, PurePosixPath(f"raw/dart/{SNAP_DART}/doc-{second}.zip"),
    )
    filing = FilingVersion(
        second, CORP, RECEIPT_DAY, FORM, "361610", digest, KNOWN_AT, KNOWN_AT,
        "HISTORICAL_BACKFILL", False, False, None, "ORIGINAL", "DATE_ONLY",
    )
    catalog.upsert_filing(filing)
    rig["store"].store_parsed_batch(  # type: ignore[union-attr]
        [_filing(RCEPT, rig["filing_digest"]), filing],  # type: ignore[union-attr]
        [
            ParsedBuyback(RCEPT, CORP, RECEIPT_DAY, (), rig["filing_digest"], "COMPLETE"),  # type: ignore[union-attr]
            ParsedBuyback(second, CORP, RECEIPT_DAY, (), digest, "COMPLETE"),
        ],
    )
    with pytest.raises(ValueError, match="unresolved event lineage"):
        plan_local_retirement(rig["root"], catalog, rig["store"])  # type: ignore[arg-type]

    rig = _rig(tmp_path / "p2", pilot=False)
    ghost_filing = FilingVersion(
        "20240101000009", CORP, date(2024, 1, 2), FORM, "361610", "e" * 64, KNOWN_AT, KNOWN_AT,
        "HISTORICAL_BACKFILL", False, False, None, "ORIGINAL", "DATE_ONLY",
    )
    rig["store"].store_parsed_batch(  # type: ignore[union-attr]
        [ghost_filing],
        [ParsedBuyback("20240101000009", CORP, date(2024, 1, 2), (), "e" * 64, "COMPLETE")],
    )
    with pytest.raises(ValueError, match="orphaned receipt"):
        plan_local_retirement(rig["root"], rig["catalog"], rig["store"])  # type: ignore[arg-type]


def test_bare_catalog_audits_empty(tmp_path: Path) -> None:
    """A catalog without event tables audits an empty issuer set without failing."""

    class _StubStore:
        def list_prior_events(self, as_of: datetime) -> tuple[()]:
            assert as_of.tzinfo is not None
            return ()

    rig = _rig(tmp_path, pilot=False)
    catalog = rig["catalog"]
    catalog._conn.execute("DROP TABLE event_link")  # noqa: SLF001
    catalog._conn.execute("DROP TABLE buyback_receipt")  # noqa: SLF001
    catalog._conn.commit()
    plan = plan_local_retirement(rig["root"], catalog, _StubStore())  # type: ignore[arg-type]
    assert plan.blocking_run_ids == ()


def test_symlink_variants_abort(tmp_path: Path) -> None:
    """Every staging, source, evidence, and collector symlink blocks planning."""
    (tmp_path / "outside.txt").write_text("external", encoding="utf-8")
    for index, rel in enumerate(["probe_old2", "imports/.staging-evil", "staging/sub"]):
        rig = _rig(tmp_path / f"p{index}", pilot=False)
        target = rig["root"] / rel  # type: ignore[operator]
        target.mkdir(parents=True, exist_ok=True)
        (target / "evil.lnk").symlink_to(tmp_path / "outside.txt")
        with pytest.raises(ValueError, match="symlinked staging artifact"):
            plan_local_retirement(rig["root"], rig["catalog"], rig["store"])  # type: ignore[arg-type]

    rig = _rig(tmp_path / "plink", pilot=False)
    (rig["root"] / "probe_old2").symlink_to(tmp_path / "outside.txt")  # type: ignore[operator]
    with pytest.raises(ValueError, match="symlinked staging artifact"):
        plan_local_retirement(rig["root"], rig["catalog"], rig["store"])  # type: ignore[arg-type]

    rig = _rig(tmp_path / "psubdir", pilot=False)
    real = rig["root"] / "staging" / "real"  # type: ignore[operator]
    real.mkdir(parents=True, exist_ok=True)
    (real / "subdir").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinked staging artifact"):
        plan_local_retirement(rig["root"], rig["catalog"], rig["store"])  # type: ignore[arg-type]

    rig = _rig(tmp_path / "proot", pilot=False)
    shutil.rmtree(rig["root"] / "staging")  # type: ignore[operator]
    (rig["root"] / "staging").symlink_to(tmp_path)  # type: ignore[operator]
    with pytest.raises(ValueError, match="symlinked staging artifact"):
        plan_local_retirement(rig["root"], rig["catalog"], rig["store"])  # type: ignore[arg-type]

    rig = _rig(tmp_path / "pdot", pilot=False)
    (rig["root"] / "imports" / ".staging-evil").symlink_to(tmp_path)  # type: ignore[operator]
    with pytest.raises(ValueError, match="symlinked staging artifact"):
        plan_local_retirement(rig["root"], rig["catalog"], rig["store"])  # type: ignore[arg-type]

    rig = _rig(tmp_path / "pev", pilot=False)
    payload = rig["obsolete_evidence"] / "payload.json"  # type: ignore[operator]
    payload.unlink()
    payload.symlink_to(tmp_path / "outside.txt")
    with pytest.raises(ValueError, match="symlinked financial evidence"):
        plan_local_retirement(rig["root"], rig["catalog"], rig["store"])  # type: ignore[arg-type]

    rig = _rig(tmp_path / "pevdir", pilot=False)
    shutil.rmtree(rig["obsolete_evidence"])  # type: ignore[union-attr]
    rig["obsolete_evidence"].symlink_to(tmp_path)  # type: ignore[union-attr]
    with pytest.raises(ValueError, match="symlinked financial evidence"):
        plan_local_retirement(rig["root"], rig["catalog"], rig["store"])  # type: ignore[arg-type]

    rig = _rig(tmp_path / "praw", pilot=False)
    raw_path = rig["pair_paths"][0]  # type: ignore[union-attr]
    raw_path.unlink()
    raw_path.symlink_to(tmp_path / "outside.txt")
    with pytest.raises(ValueError, match="symlink"):
        plan_local_retirement(rig["root"], rig["catalog"], rig["store"])  # type: ignore[arg-type]

    rig = _rig(tmp_path / "pparquet", pilot=False)
    rig["ghost_parquet"].unlink()  # type: ignore[union-attr]
    rig["ghost_parquet"].symlink_to(tmp_path / "outside.txt")  # type: ignore[union-attr]
    with pytest.raises(ValueError, match="symlink"):
        plan_local_retirement(rig["root"], rig["catalog"], rig["store"])  # type: ignore[arg-type]


def test_source_part_states(tmp_path: Path) -> None:
    """Missing source parts stay reclaimable while drift and bad manifests block."""
    rig = _rig(tmp_path / "p1", pilot=False)
    (rig["root"] / "imports" / SOURCE_PANEL_DATASET_ID / "part-00000.parquet").unlink()  # type: ignore[operator]
    plan = plan_local_retirement(rig["root"], rig["catalog"], rig["store"])  # type: ignore[arg-type]
    assert rig["root"] / "imports" / SOURCE_PANEL_DATASET_ID / "manifest.json" in plan.obsolete_import_parts  # type: ignore[operator]

    rig = _rig(tmp_path / "p2", pilot=False)
    part = rig["root"] / "imports" / SOURCE_PANEL_DATASET_ID / "part-00000.parquet"  # type: ignore[operator]
    part.write_bytes(b"drifted")
    with pytest.raises(ValueError, match="hash drift"):
        plan_local_retirement(rig["root"], rig["catalog"], rig["store"])  # type: ignore[arg-type]

    rig = _rig(tmp_path / "p3", pilot=False)
    (rig["root"] / "imports" / SOURCE_PANEL_DATASET_ID / "manifest.json").write_bytes(b"broken")  # type: ignore[operator]
    with pytest.raises(ValueError, match="invalid scoped import manifest"):
        plan_local_retirement(rig["root"], rig["catalog"], rig["store"])  # type: ignore[arg-type]


def test_kept_raw_and_evidence_guards(tmp_path: Path) -> None:
    """Absent kept raws, drifted candidates, and run-cited evidence gaps block planning."""
    rig = _rig(tmp_path / "p1", pilot=False)
    (rig["root"] / "raw" / "imported" / "daily_market" / "kept-digest" / "payload.json").unlink()  # type: ignore[operator]
    with pytest.raises(ValueError, match="missing active raw artifact"):
        plan_local_retirement(rig["root"], rig["catalog"], rig["store"])  # type: ignore[arg-type]

    rig = _rig(tmp_path / "p2", pilot=False)
    rig["pair_paths"][0].write_bytes(b"drifted-payload")  # type: ignore[union-attr]
    with pytest.raises(ValueError, match="hash drift"):
        plan_local_retirement(rig["root"], rig["catalog"], rig["store"])  # type: ignore[arg-type]

    rig = _rig(tmp_path / "p3", pilot=False)
    payload_raw = json.dumps({"records": [_record("20240101000009", CORP, "5")]}).encode()
    payload_digest = _sha(payload_raw)
    cited_evidence = rig["root"] / "imports" / "financial_evidence" / payload_digest  # type: ignore[operator]
    cited_evidence.mkdir(parents=True, exist_ok=True)
    (cited_evidence / "payload.json").write_bytes(payload_raw)
    run_dir = rig["root"] / "reports" / "evt-9" / "run-9"  # type: ignore[operator]
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest_raw = (json.dumps({"run": "run-9"}) + "\n").encode()
    (run_dir / "manifest.json").write_bytes(manifest_raw)
    (run_dir / "memo.json").write_text(json.dumps({"cites": [payload_digest]}), encoding="utf-8")
    rig["catalog"].register_research_run("run-9", _sha(manifest_raw), "COMPLETE", PurePosixPath("reports/evt-9/run-9/manifest.json"))  # type: ignore[union-attr]
    with pytest.raises(ValueError, match="missing active financial evidence"):
        plan_local_retirement(rig["root"], rig["catalog"], rig["store"])  # type: ignore[arg-type]


def test_execute_rechecks_and_conflicts(tmp_path: Path) -> None:
    """Swapped files and conflicting records abort execution before mutation."""
    rig = _rig(tmp_path / "p1", pilot=False)
    plan = plan_local_retirement(rig["root"], rig["catalog"], rig["store"])  # type: ignore[arg-type]
    target = rig["pair_paths"][0]  # type: ignore[union-attr]
    target.unlink()
    target.symlink_to(tmp_path / "outside.txt")
    with pytest.raises(ValueError, match="symlink"):
        execute_local_retirement(plan, rig["root"], rig["catalog"], frozenset())  # type: ignore[arg-type]
    assert not (tmp_path / "outside.txt").exists()
    assert rig["pair_paths"][1].is_file()  # type: ignore[union-attr]

    rig = _rig(tmp_path / "p2", pilot=False)
    plan = plan_local_retirement(rig["root"], rig["catalog"], rig["store"])  # type: ignore[arg-type]
    conflict = rig["root"] / "retention" / f"retirement-{plan.plan_digest}.json"  # type: ignore[operator]
    conflict.parent.mkdir(parents=True, exist_ok=True)
    conflict.write_text(json.dumps({"plan_digest": "other"}), encoding="utf-8")
    with pytest.raises(ValueError, match="conflicting retirement record"):
        execute_local_retirement(plan, rig["root"], rig["catalog"], frozenset())  # type: ignore[arg-type]

    rig = _rig(tmp_path / "p3", pilot=False)
    plan = plan_local_retirement(rig["root"], rig["catalog"], rig["store"])  # type: ignore[arg-type]
    conflict = rig["root"] / "retention" / f"retirement-{plan.plan_digest}.json"  # type: ignore[operator]
    conflict.parent.mkdir(parents=True, exist_ok=True)
    conflict.write_bytes(b"not json")
    with pytest.raises(ValueError, match="conflicting retirement record"):
        execute_local_retirement(plan, rig["root"], rig["catalog"], frozenset())  # type: ignore[arg-type]


def test_catalog_schema_migrates_v1_to_v2(tmp_path: Path) -> None:
    """A version-1 catalog gains the retirement audit table without losing rows."""
    root = tmp_path / "data"
    catalog = Catalog(root / "catalog.sqlite")
    digest = catalog.register_artifact(
        "dart", "list", "win", "snap", b"{}", KNOWN_AT, PurePosixPath("raw/dart/snap/page.json")
    )
    catalog._conn.execute("UPDATE schema_version SET version=1")  # noqa: SLF001
    catalog._conn.execute("DROP TABLE retirement_entry")  # noqa: SLF001
    catalog._conn.commit()
    migrated = Catalog(root / "catalog.sqlite")
    version = migrated._conn.execute("SELECT version FROM schema_version").fetchone()  # noqa: SLF001
    assert int(version["version"]) == 2
    assert migrated.find_artifact("dart", "list", "win", "snap") is not None
    assert migrated.references_to_hashes(frozenset({digest})) == {}
    with pytest.raises(ValueError, match="retirement id"):
        migrated.retire_artifacts((("dart", "list", "win", "snap"),), "")
    with pytest.raises(ValueError, match="retirement key"):
        migrated.retire_artifacts((("dart", "list"),), "ret-1")
    assert migrated.references_to_hashes(frozenset()) == {}
