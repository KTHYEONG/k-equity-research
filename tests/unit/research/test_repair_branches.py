"""Tail guards for historical memo evidence repair branches."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo

import pytest

from datetime import datetime

from src.data.catalog import Catalog
from src.data.local_paths import checked_local_path
from src.research.memo import EvidenceRef, MemoClaim, memo_to_dict
from src.research.repair import RepairPreconditionError, _parse_memo, repair_memo_evidence

from tests.unit.research.test_repair_isolated import (
    EVENT_ID,
    _build_rig,
    _valid_memo_dict,
    _write_old_run,
)

KST = ZoneInfo("Asia/Seoul")


def _index_hash(rig: dict[str, object]) -> str:
    return str(rig["manifest"].manifest_hash)  # type: ignore[union-attr]


def test_old_bytes_rejections(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing or changed old report bytes abort before any replay."""
    rig = _build_rig(tmp_path, monkeypatch)
    data_root = rig["data_root"]
    assert isinstance(data_root, Path)
    index_hash = _index_hash(rig)
    run_dir = data_root / "reports" / EVENT_ID / "old-run-1"
    memo_raw = (run_dir / "memo.json").read_bytes()
    (run_dir / "memo.json").unlink()
    with pytest.raises(RepairPreconditionError):
        repair_memo_evidence(data_root, "old-run-1", index_hash)
    (run_dir / "memo.json").write_bytes(b"tampered")
    with pytest.raises(RepairPreconditionError):
        repair_memo_evidence(data_root, "old-run-1", index_hash)
    (run_dir / "memo.json").write_bytes(memo_raw)


def test_memo_bytes_undecodable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Stored memo bytes that are not JSON abort before any replay."""
    rig = _build_rig(tmp_path, monkeypatch)
    data_root = rig["data_root"]
    assert isinstance(data_root, Path)
    index_hash = _index_hash(rig)
    memo_dict = _valid_memo_dict(rig)
    run_id = "old-undecodable"
    relative = PurePosixPath(f"reports/{EVENT_ID}/{run_id}")
    run_dir = data_root / relative.as_posix()
    run_dir.mkdir(parents=True)
    memo_raw = b"not json"
    markdown_raw = b"md\n"
    manifest_doc = {
        "event_id": EVENT_ID,
        "manifest_hash": memo_dict.get("manifest_hash", ""),
        "markdown_sha256": hashlib.sha256(markdown_raw).hexdigest(),
        "memo_sha256": hashlib.sha256(memo_raw).hexdigest(),
        "run_id": run_id,
    }
    manifest_raw = (json.dumps(manifest_doc, sort_keys=True, indent=2) + "\n").encode()
    (run_dir / "memo.json").write_bytes(memo_raw)
    (run_dir / "memo.md").write_bytes(markdown_raw)
    (run_dir / "manifest.json").write_bytes(manifest_raw)
    catalog = Catalog(data_root / "catalog.sqlite")
    catalog.register_research_run(
        run_id, hashlib.sha256(manifest_raw).hexdigest(), "COMPLETE", relative / "manifest.json"
    )
    with pytest.raises(RepairPreconditionError):
        repair_memo_evidence(data_root, run_id, index_hash)


def test_repeated_citations_validate_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Repeated citations of one valid file validate once, then drift still aborts."""
    rig = _build_rig(tmp_path, monkeypatch)
    data_root = rig["data_root"]
    assert isinstance(data_root, Path)
    index_hash = _index_hash(rig)
    target = checked_local_path(data_root, PurePosixPath("raw/dart/f1.zip"))
    filing_hash = hashlib.sha256(target.read_bytes()).hexdigest()
    old_memo = _parse_memo(_valid_memo_dict(rig))
    ref_a = EvidenceRef("extra-a", "dart_filing_zip", PurePosixPath("raw/dart/f1.zip"), filing_hash, "x")
    ref_b = EvidenceRef("extra-b", "dart_filing_zip", PurePosixPath("raw/dart/f1.zip"), filing_hash, "x")
    drifted = dataclasses.replace(
        old_memo,
        evidence=(*old_memo.evidence, ref_a, ref_b),
        claims=(
            *old_memo.claims,
            MemoClaim("filing_fact", "Observed.", ("extra-a",), None),
            MemoClaim("filing_fact", "Observed.", ("extra-b",), None),
        ),
    )
    _write_old_run(data_root, "old-dedup", json.loads(json.dumps(memo_to_dict(drifted))))
    with pytest.raises(RepairPreconditionError):
        repair_memo_evidence(data_root, "old-dedup", index_hash)


def test_evidence_attribute_drift_rejection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A changed locator on a known evidence ID aborts without publishing."""
    import src.research.repair as repair_module

    rig = _build_rig(tmp_path, monkeypatch)
    data_root = rig["data_root"]
    assert isinstance(data_root, Path)
    index_hash = _index_hash(rig)
    real_builder = repair_module.build_baseline_memo

    def _tampered_builder(context: object, analogue_ref: object = None) -> object:
        memo = real_builder(context, analogue_ref=analogue_ref)  # type: ignore[arg-type]
        by_id = {ref.id: ref for ref in memo.evidence}
        others = tuple(
            dataclasses.replace(ref, locator="tampered") if ref.id == "filing-fact-acq_ostk" else ref
            for ref in memo.evidence
        )
        assert "filing-fact-acq_ostk" in by_id
        return dataclasses.replace(memo, evidence=others)

    monkeypatch.setattr("src.research.repair.build_baseline_memo", _tampered_builder)
    with pytest.raises(RepairPreconditionError):
        repair_memo_evidence(data_root, "old-run-1", index_hash)
    assert not (data_root / "reports" / EVENT_ID / "old-run-1-evidence-v2").exists()


def test_context_identity_drift_rejections(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Replayed identity drift aborts without publishing."""
    from src.research.context import ResearchContext

    rig = _build_rig(tmp_path, monkeypatch)
    data_root = rig["data_root"]
    assert isinstance(data_root, Path)
    index_hash = _index_hash(rig)
    context = rig["context"]
    assert isinstance(context, ResearchContext)
    variants = [
        dataclasses.replace(context, event=dataclasses.replace(context.event, event_id="other")),
        dataclasses.replace(context, active_rcept_no="other"),
        dataclasses.replace(context, as_of=datetime(2024, 6, 30, 18, 0, tzinfo=KST)),
    ]
    for variant in variants:
        monkeypatch.setattr(
            "src.research.repair.build_research_context", lambda *args, _variant=variant, **kwargs: _variant
        )
        with pytest.raises(RepairPreconditionError):
            repair_memo_evidence(data_root, "old-run-1", index_hash)
        assert not (data_root / "reports" / EVENT_ID / "old-run-1-evidence-v2").exists()


def test_proof_citation_path_drift_rejection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A proof citation at the wrong run-local path aborts without publishing."""
    import src.research.repair as repair_module

    rig = _build_rig(tmp_path, monkeypatch)
    data_root = rig["data_root"]
    assert isinstance(data_root, Path)
    index_hash = _index_hash(rig)

    real_reference = repair_module.proof_reference

    def _wrong_path(run_dir: object, proof: object) -> EvidenceRef:
        ref = real_reference(run_dir, proof)  # type: ignore[arg-type]
        return dataclasses.replace(ref, local_relative_path=PurePosixPath("reports/elsewhere/proof.json"))

    monkeypatch.setattr("src.research.repair.proof_reference", _wrong_path)
    with pytest.raises(RepairPreconditionError):
        repair_memo_evidence(data_root, "old-run-1", index_hash)
    assert not (data_root / "reports" / EVENT_ID / "old-run-1-evidence-v2").exists()


def test_cli_repair_memo_success_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """CLI repair output names both run IDs and the new proof digest."""
    import json as _json

    from src.cli.main import main as _main

    rig = _build_rig(tmp_path, monkeypatch)
    data_root = rig["data_root"]
    assert isinstance(data_root, Path)
    index_hash = _index_hash(rig)
    code = _main(
        ["research", "repair-memo", "--run-id", "old-run-1", "--index-manifest", index_hash, "--data-root", str(data_root)]
    )
    assert code == 0
    payload = _json.loads(capsys.readouterr().out)
    assert payload["old_run_id"] == "old-run-1"
    assert payload["new_run_id"] == "old-run-1-evidence-v2"
    assert payload["proof_sha256"] == payload["analogue_proof"]
