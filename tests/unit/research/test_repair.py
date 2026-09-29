"""Invariant guards for historical memo evidence repair."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath

import pytest

from src.cli.main import build_parser, main
from src.data.catalog import Catalog
from src.research.repair import (
    RepairPreconditionError,
    ResearchRunConflictError,
    repair_memo_evidence,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
REAL_DATA = PROJECT_ROOT / "data"
OLD_RUN_ID = "20240626000369-20260929-183200p0900"
INDEX_MANIFEST = "a4dfdba7fa75645f090862aaa16b03ccd27876189beebeeb92cf6b7f6e2fffa6"
EVENT_ID = "buyback:01386916:20240626000207"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _cleanup_new_run(data_root: Path, new_run_id: str) -> None:
    catalog = Catalog(data_root / "catalog.sqlite")
    catalog._conn.execute("DELETE FROM research_run WHERE run_id=?", (new_run_id,))  # noqa: SLF001
    catalog._conn.commit()
    rows = catalog._conn.execute("SELECT run_id FROM research_run WHERE run_id=?", (new_run_id,)).fetchall()  # noqa: SLF001
    if rows:
        raise AssertionError("cleanup failed")
    import shutil

    for candidate in (data_root / "reports" / EVENT_ID / new_run_id,):
        if candidate.is_symlink() or candidate.is_file():
            raise AssertionError("unexpected new run file")
        if candidate.is_dir():
            shutil.rmtree(candidate)


@pytest.mark.slow
def test_repair_known_run_replays_exact_economics() -> None:
    """The 2024 run repairs with a byte-valid proof and identical economics."""
    if not (REAL_DATA / "catalog.sqlite").is_file():
        return
    new_run_id = f"{OLD_RUN_ID}-evidence-v2"
    old_dir = REAL_DATA / "reports" / EVENT_ID / OLD_RUN_ID
    before = {name: _sha(old_dir / name) for name in ("memo.json", "memo.md", "manifest.json")}
    try:
        summary = repair_memo_evidence(REAL_DATA, OLD_RUN_ID, INDEX_MANIFEST)
        assert summary.old_run_id == OLD_RUN_ID
        assert summary.new_run_id == new_run_id
        new_dir = REAL_DATA / "reports" / EVENT_ID / new_run_id
        old_memo = json.loads((old_dir / "memo.json").read_bytes().decode())
        new_memo = json.loads((new_dir / "memo.json").read_bytes().decode())
        assert new_memo["facts"] == old_memo["facts"]
        assert new_memo["metrics"] == old_memo["metrics"]
        assert new_memo["statuses"] == old_memo["statuses"]
        assert new_memo["claims"] == old_memo["claims"]
        assert new_memo["metrics"]["analogue_p25"] == "-0.0220411989190667634890743956"
        assert new_memo["metrics"]["analogue_median"] == "0.011938860061081102128190031"
        assert new_memo["metrics"]["analogue_p75"] == "0.0159250449564142733934823083"
        proof = json.loads((new_dir / "analogue-proof.json").read_bytes().decode())
        assert len(proof["observations"]) == 8
        assert proof["quantiles"]["p25"] == new_memo["metrics"]["analogue_p25"]
        new_tool = next(item for item in new_memo["evidence"] if item["id"] == "tool-analogues")
        assert new_tool["local_relative_path"] == f"reports/{EVENT_ID}/{new_run_id}/analogue-proof.json"
        assert new_tool["sha256"] == summary.proof_sha256
        assert hashlib.sha256((new_dir / "analogue-proof.json").read_bytes()).hexdigest() == summary.proof_sha256
        new_manifest = json.loads((new_dir / "manifest.json").read_bytes().decode())
        assert new_manifest["supersedes_run_id"] == OLD_RUN_ID
        assert new_manifest["index_manifest_hash"] == INDEX_MANIFEST
        assert new_manifest["proof_sha256"] == summary.proof_sha256
        assert {name: _sha(old_dir / name) for name in before} == before
        catalog = Catalog(REAL_DATA / "catalog.sqlite")
        assert catalog.get_research_run(new_run_id) is not None
        assert catalog.references_to_hashes(frozenset({proof["inputs"][0]["sha256"]}))
        rerun = repair_memo_evidence(REAL_DATA, OLD_RUN_ID, INDEX_MANIFEST)
        assert rerun.new_run_id == new_run_id
        assert rerun.proof_sha256 == summary.proof_sha256
    finally:
        _cleanup_new_run(REAL_DATA, new_run_id)


def test_repair_wrong_manifest_aborts_without_publish(tmp_path: Path) -> None:
    """A wrong index manifest hash aborts before any new run directory appears."""
    if not (REAL_DATA / "catalog.sqlite").is_file():
        return
    other = "723ce3390598b4023d441f2c367227534845b25f8c49852f3e119f83fd7077aa"
    new_dir = REAL_DATA / "reports" / EVENT_ID / f"{OLD_RUN_ID}-evidence-v2"
    existed = new_dir.exists()
    try:
        repair_memo_evidence(REAL_DATA, OLD_RUN_ID, other)
    except RepairPreconditionError:
        pass
    else:
        raise AssertionError("expected precondition failure")
    assert new_dir.exists() == existed
    _ = tmp_path


def test_repair_unknown_run_aborts(tmp_path: Path) -> None:
    """An unregistered run ID aborts without touching the filesystem."""
    data_root = tmp_path / "data"
    Catalog(data_root / "catalog.sqlite")
    try:
        repair_memo_evidence(data_root, "missing-run", "ab" * 32)
    except RepairPreconditionError:
        pass
    else:
        raise AssertionError("expected precondition failure")
    assert not (data_root / "reports").exists()


def test_repair_invalid_hash_aborts(tmp_path: Path) -> None:
    """A malformed manifest digest is rejected before any catalog read."""
    data_root = tmp_path / "data"
    Catalog(data_root / "catalog.sqlite")
    try:
        repair_memo_evidence(data_root, OLD_RUN_ID, "not-a-hash")
    except RepairPreconditionError:
        pass
    else:
        raise AssertionError("expected precondition failure")


@pytest.mark.slow
def test_repair_conflict_without_overwrite() -> None:
    """A different payload at the new run ID is rejected without overwrite."""
    if not (REAL_DATA / "catalog.sqlite").is_file():
        return
    new_run_id = f"{OLD_RUN_ID}-evidence-v2"
    new_dir = REAL_DATA / "reports" / EVENT_ID / new_run_id
    try:
        summary = repair_memo_evidence(REAL_DATA, OLD_RUN_ID, INDEX_MANIFEST)
        assert summary.new_run_id == new_run_id
        (new_dir / "memo.json").write_bytes(b'{"tampered": true}\n')
        try:
            repair_memo_evidence(REAL_DATA, OLD_RUN_ID, INDEX_MANIFEST)
        except ResearchRunConflictError:
            pass
        else:
            raise AssertionError("expected conflict failure")
    finally:
        _cleanup_new_run(REAL_DATA, new_run_id)


def test_cli_repair_memo_reports_both_runs_and_proof() -> None:
    """CLI repair output names old/new run IDs and the new proof digest."""
    parser = build_parser()
    args = parser.parse_args(
        ["research", "repair-memo", "--run-id", OLD_RUN_ID, "--index-manifest", INDEX_MANIFEST]
    )
    assert args.research_command == "repair-memo"
    assert args.run_id == OLD_RUN_ID
    assert args.index_manifest == INDEX_MANIFEST
    code = main(
        [
            "research",
            "repair-memo",
            "--run-id",
            "missing-run",
            "--index-manifest",
            INDEX_MANIFEST,
            "--data-root",
            str(REAL_DATA),
        ]
    )
    assert code == 2
    assert not (REAL_DATA / "reports" / EVENT_ID / "missing-run-evidence-v2").exists()
    assert PurePosixPath("x").as_posix() == "x"
