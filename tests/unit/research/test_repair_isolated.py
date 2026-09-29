"""Fast isolated guards for historical memo evidence repair."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo

import pytest

from src.core.revisions import EventLink
from src.data.catalog import Catalog
from src.data.index_store import merge_index_manifest
from src.data.local_lake import SecurityMatch
from src.core.buyback_document import BuybackFact, EvidenceLocation, ParsedBuyback
from src.data.catalog import FilingVersion
from src.research.comparables import AnalogueObservation, ComparableSet
from src.research.context import ResearchUnavailable
from src.research.event_study import StudyResult
from src.research.materiality import MaterialityResult
from src.research.memo import EvidenceRef, build_baseline_memo, memo_to_dict, render_markdown
from src.research.repair import (
    RepairPreconditionError,
    ResearchRunConflictError,
    _parse_memo,
    repair_memo_evidence,
)

KST = ZoneInfo("Asia/Seoul")
AS_OF = datetime(2024, 6, 29, 18, 0, tzinfo=KST)
EVENT_ID = "buyback:01386916:20240626000207"
ANCHOR = "20240626000207"
ACTIVE = "20240626000369"
BAD_PATH = PurePosixPath("raw/dart/pilot-202406-20260928/doc-20240626000369.zip")


def _register(catalog: Catalog, relative: str, raw: bytes) -> str:
    return catalog.register_artifact(
        source="test",
        endpoint="file",
        request_key=relative,
        snapshot_id="snap",
        raw_bytes=raw,
        retrieved_at=datetime(2024, 6, 24, 12, 0, tzinfo=KST),
        local_relative_path=PurePosixPath(relative),
    )


def _build_rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, old_run_id: str = "old-run-1") -> dict[str, object]:
    data_root = tmp_path / "data"
    catalog = Catalog(data_root / "catalog.sqlite")
    manifest = merge_index_manifest(None, [], data_root)
    filing_hash = _register(catalog, "raw/dart/f1.zip", b"filing-1")
    doc_hash = _register(catalog, "raw/dart/p1.json", b"parsed-1")
    target_hash = _register(catalog, "raw/target.bin", b"target")
    obs_hashes: list[tuple[str, str]] = [
        (_register(catalog, f"raw/s{i}.bin", f"s{i}".encode()), _register(catalog, f"raw/x{i}.bin", f"x{i}".encode()))
        for i in range(5)
    ]
    filing = FilingVersion(
        rcept_no=ACTIVE,
        corp_code="01386916",
        receipt_date=date(2024, 6, 26),
        report_name="...",
        stock_code="361610",
        raw_hash=filing_hash,
        first_observed_at=datetime(2024, 6, 26, 9, 0, tzinfo=KST),
        knowledge_available_at=datetime(2024, 6, 26, 18, 0, tzinfo=KST),
        availability_mode="HISTORICAL_BACKFILL",
        correction_flag=False,
        withdrawal_flag=False,
        parent_rcept_no=None,
        link_status="ORIGINAL",
        time_precision="DATE_ONLY",
    )
    parsed = ParsedBuyback(
        rcept_no=ACTIVE,
        corp_code="01386916",
        first_submission_date=date(2024, 6, 26),
        facts=(
            BuybackFact(
                field="ACQ_OSTK",
                value_decimal=Decimal(200000),
                value_text="200000",
                unit="shares",
                evidence=EvidenceLocation(ACTIVE, doc_hash, "report.xml", "ACODE", "ACQ_OSTK", "s", "t", "c"),
                status="VERIFIED",
            ),
        ),
        document_hash=doc_hash,
        parse_status="OK",
    )
    from src.research.context import ResearchContext

    security = SecurityMatch("KRX:361610", "361610", "KOSPI", "KR7361610000", date(2024, 6, 25), "OK")
    event = EventLink(EVENT_ID, (ANCHOR, ACTIVE), "LINKED")
    outcomes = (Decimal("0.01"), Decimal("-0.02"), Decimal("0.03"), Decimal("0.015"), Decimal("-0.005"))
    ids = tuple(f"buyback:00000000:2024010{i}0000{i}" for i in range(5))
    observations = tuple(
        AnalogueObservation(
            ids[i],
            date(2024, 6, 10 + i),
            datetime(2024, 6, 20, 18, 0, tzinfo=KST),
            outcomes[i],
            tuple(sorted(obs_hashes[i])),
        )
        for i in range(5)
    )
    selection = tuple(sorted({target_hash} | {digest for pair in obs_hashes for digest in pair}))
    comparables = ComparableSet(
        event_id=EVENT_ID,
        as_of=AS_OF,
        feature_end_session=date(2024, 6, 25),
        peer_ids=(),
        analogue_event_ids=ids,
        analogue_intraday_excess=outcomes,
        analogue_quantiles={"p25": Decimal("-0.005"), "median": Decimal("0.01"), "p75": Decimal("0.015")},
        exclusions={},
        status="OK",
        analogue_observations=observations,
        selection_source_hashes=selection,
    )
    source_hashes = tuple(sorted({doc_hash, filing_hash} | set(selection)))
    artifact_paths = {digest: catalog.get_artifact_path(digest) for digest in source_hashes}
    assert all(path is not None for path in artifact_paths.values())
    context = ResearchContext(
        anchor_rcept_no=ANCHOR,
        active_rcept_no=ACTIVE,
        event=event,
        filing=filing,
        parsed=parsed,
        security=security,
        financial_facts=(),
        stock_bars=(),
        index_bars=(),
        materiality=MaterialityResult(None, None, (), "OK"),
        study=StudyResult(EVENT_ID, ACTIVE, AS_OF, None, None, None, None, {}, "OK", (), (), "v1", 0),
        comparables=comparables,
        as_of=AS_OF,
        index_manifest_hash=manifest.manifest_hash,
        snapshot_ids=(),
        source_hashes=source_hashes,
        artifact_paths={digest: path for digest, path in artifact_paths.items() if path is not None},
        confounding_receipts=(),
    )
    bad_ref = EvidenceRef("tool-analogues", "tool_result", BAD_PATH, manifest.manifest_hash, "old")
    old_memo = build_baseline_memo(context, analogue_ref=bad_ref)
    memo_raw = (json.dumps(memo_to_dict(old_memo), sort_keys=True, indent=2) + "\n").encode()
    markdown_raw = render_markdown(old_memo).encode()
    manifest_doc = {
        "event_id": EVENT_ID,
        "manifest_hash": old_memo.manifest_hash,
        "markdown_sha256": hashlib.sha256(markdown_raw).hexdigest(),
        "memo_sha256": hashlib.sha256(memo_raw).hexdigest(),
        "run_id": old_run_id,
    }
    manifest_raw = (json.dumps(manifest_doc, sort_keys=True, indent=2) + "\n").encode()
    relative = PurePosixPath(f"reports/{EVENT_ID}/{old_run_id}")
    run_dir = data_root / relative.as_posix()
    run_dir.mkdir(parents=True)
    (run_dir / "memo.json").write_bytes(memo_raw)
    (run_dir / "memo.md").write_bytes(markdown_raw)
    (run_dir / "manifest.json").write_bytes(manifest_raw)
    catalog.register_research_run(
        old_run_id, hashlib.sha256(manifest_raw).hexdigest(), "COMPLETE", relative / "manifest.json"
    )
    monkeypatch.setattr("src.research.repair.build_research_context", lambda *args, **kwargs: context)
    return {"data_root": data_root, "catalog": catalog, "context": context, "manifest": manifest}


def test_isolated_repair_publishes_verified_proof(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A synthetic old run repairs with identical economics and a valid citation."""
    rig = _build_rig(tmp_path, monkeypatch)
    data_root = rig["data_root"]
    assert isinstance(data_root, Path)
    old_dir = data_root / "reports" / EVENT_ID / "old-run-1"
    before = {name: hashlib.sha256((old_dir / name).read_bytes()).hexdigest() for name in ("memo.json", "memo.md", "manifest.json")}
    summary = repair_memo_evidence(data_root, "old-run-1", str(rig["manifest"].manifest_hash))  # type: ignore[union-attr]
    assert summary.old_run_id == "old-run-1"
    assert summary.new_run_id == "old-run-1-evidence-v2"
    new_dir = data_root / "reports" / EVENT_ID / summary.new_run_id
    old_memo = json.loads((old_dir / "memo.json").read_bytes().decode())
    new_memo = json.loads((new_dir / "memo.json").read_bytes().decode())
    assert new_memo["facts"] == old_memo["facts"]
    assert new_memo["metrics"] == old_memo["metrics"]
    assert new_memo["statuses"] == old_memo["statuses"]
    assert new_memo["claims"] == old_memo["claims"]
    new_tool = next(item for item in new_memo["evidence"] if item["id"] == "tool-analogues")
    assert new_tool["sha256"] == summary.proof_sha256
    assert new_tool["local_relative_path"] == f"reports/{EVENT_ID}/{summary.new_run_id}/analogue-proof.json"
    assert hashlib.sha256((new_dir / "analogue-proof.json").read_bytes()).hexdigest() == summary.proof_sha256
    new_manifest = json.loads((new_dir / "manifest.json").read_bytes().decode())
    assert new_manifest["supersedes_run_id"] == "old-run-1"
    assert new_manifest["proof_sha256"] == summary.proof_sha256
    assert {name: hashlib.sha256((old_dir / name).read_bytes()).hexdigest() for name in before} == before
    catalog = Catalog(data_root / "catalog.sqlite")
    proof = json.loads((new_dir / "analogue-proof.json").read_bytes().decode())
    assert catalog.references_to_hashes(frozenset({proof["inputs"][0]["sha256"]}))
    rerun = repair_memo_evidence(data_root, "old-run-1", str(rig["manifest"].manifest_hash))  # type: ignore[union-attr]
    assert rerun.proof_sha256 == summary.proof_sha256


def test_isolated_repair_rejects_second_corrupt_reference(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An additional invalid old reference aborts without publishing."""
    rig = _build_rig(tmp_path, monkeypatch, old_run_id="old-bad")
    data_root = rig["data_root"]
    assert isinstance(data_root, Path)
    context = rig["context"]
    from src.research.context import ResearchContext
    from src.research.memo import MemoClaim

    assert isinstance(context, ResearchContext)
    bad = EvidenceRef("filing-fact-acq_ostk", "dart_filing_zip", PurePosixPath("raw/dart/gone.zip"), "ab" * 32, "x")
    old_memo = build_baseline_memo(
        context,
        analogue_ref=EvidenceRef("tool-analogues", "tool_result", BAD_PATH, str(rig["manifest"].manifest_hash), "old"),  # type: ignore[union-attr]
    )
    drifted = dataclasses.replace(
        old_memo,
        evidence=(*old_memo.evidence, bad),
        claims=(*old_memo.claims, MemoClaim("filing_fact", "Observed.", ("filing-fact-acq_ostk",), None)),
    )
    run_dir = data_root / "reports" / EVENT_ID / "old-bad"
    memo_raw = (json.dumps(memo_to_dict(drifted), sort_keys=True, indent=2) + "\n").encode()
    markdown_raw = render_markdown(drifted).encode()
    manifest_doc = {
        "event_id": EVENT_ID,
        "manifest_hash": drifted.manifest_hash,
        "markdown_sha256": hashlib.sha256(markdown_raw).hexdigest(),
        "memo_sha256": hashlib.sha256(memo_raw).hexdigest(),
        "run_id": "old-bad",
    }
    manifest_raw = (json.dumps(manifest_doc, sort_keys=True, indent=2) + "\n").encode()
    (run_dir / "memo.json").write_bytes(memo_raw)
    (run_dir / "memo.md").write_bytes(markdown_raw)
    (run_dir / "manifest.json").write_bytes(manifest_raw)
    catalog = Catalog(data_root / "catalog.sqlite")
    catalog._conn.execute("DELETE FROM research_run WHERE run_id=?", ("old-bad",))  # noqa: SLF001
    catalog._conn.commit()
    catalog.register_research_run(
        "old-bad",
        hashlib.sha256(manifest_raw).hexdigest(),
        "COMPLETE",
        PurePosixPath(f"reports/{EVENT_ID}/old-bad/manifest.json"),
    )
    with pytest.raises(RepairPreconditionError):
        repair_memo_evidence(data_root, "old-bad", str(rig["manifest"].manifest_hash))  # type: ignore[union-attr]
    assert not (data_root / "reports" / EVENT_ID / "old-bad-evidence-v2").exists()


def test_isolated_repair_rejects_metric_drift(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A replayed outcome that changes economics aborts without publishing."""
    rig = _build_rig(tmp_path, monkeypatch)
    data_root = rig["data_root"]
    assert isinstance(data_root, Path)
    context = rig["context"]
    from src.research.context import ResearchContext

    assert isinstance(context, ResearchContext)
    drifted_outcomes = (Decimal("0.99"), *context.comparables.analogue_intraday_excess[1:])
    ordered = sorted(drifted_outcomes)
    drifted_quantiles = {"p25": ordered[1], "median": ordered[2], "p75": ordered[3]}
    drifted_observations = tuple(
        dataclasses.replace(obs, intraday_excess=drifted_outcomes[i]) for i, obs in enumerate(context.comparables.analogue_observations)
    )
    drifted_comparables = dataclasses.replace(
        context.comparables,
        analogue_intraday_excess=drifted_outcomes,
        analogue_quantiles=drifted_quantiles,
        analogue_observations=drifted_observations,
    )
    drifted_context = dataclasses.replace(context, comparables=drifted_comparables)
    monkeypatch.setattr("src.research.repair.build_research_context", lambda *args, **kwargs: drifted_context)
    with pytest.raises(RepairPreconditionError):
        repair_memo_evidence(data_root, "old-run-1", str(rig["manifest"].manifest_hash))  # type: ignore[union-attr]
    assert not (data_root / "reports" / EVENT_ID / "old-run-1-evidence-v2").exists()


@pytest.mark.parametrize("document", [[], {"evidence": {}, "claims": [], "facts": {}, "metrics": {}, "statuses": []}, {"evidence": [{"id": "x"}], "claims": [], "facts": {}, "metrics": {}, "statuses": []}, {"evidence": ["x"], "claims": [], "facts": {}, "metrics": {}, "statuses": []}, {"evidence": [], "claims": ["x"], "facts": {}, "metrics": {}, "statuses": []}, {"evidence": [], "claims": [{"kind": "x"}], "facts": {}, "metrics": {}, "statuses": []}, {"evidence": [], "claims": [], "facts": [], "metrics": {}, "statuses": []}, {"evidence": [], "claims": [], "facts": {}, "metrics": {}, "statuses": {}}, {}])
def test_parse_memo_rejects_malformed_documents(document: object) -> None:
    """Malformed stored memos abort repair before any replay."""
    with pytest.raises(RepairPreconditionError):
        _parse_memo(document)


@pytest.mark.parametrize("run_id", ["", "../escape", "a/b"])
def test_isolated_repair_rejects_invalid_run_id(tmp_path: Path, run_id: str) -> None:
    """Unsafe run identifiers never reach the catalog."""
    data_root = tmp_path / "data"
    Catalog(data_root / "catalog.sqlite")
    with pytest.raises(RepairPreconditionError):
        repair_memo_evidence(data_root, run_id, "ab" * 32)
    assert not (data_root / "reports").exists()


def test_isolated_repair_rejects_missing_manifest(tmp_path: Path) -> None:
    """A valid digest without local manifest bytes aborts without writes."""
    data_root = tmp_path / "data"
    Catalog(data_root / "catalog.sqlite")
    with pytest.raises(RepairPreconditionError):
        repair_memo_evidence(data_root, "old-run-1", "ab" * 32)
    assert not (data_root / "reports").exists()


def test_isolated_repair_rejects_unavailable_replay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A replay that cannot resolve its inputs aborts without publishing."""
    rig = _build_rig(tmp_path, monkeypatch)
    data_root = rig["data_root"]
    assert isinstance(data_root, Path)

    def _missing(*args: object, **kwargs: object) -> object:
        raise ResearchUnavailable("UNKNOWN_RECEIPT", "gone")

    monkeypatch.setattr("src.research.repair.build_research_context", _missing)
    with pytest.raises(RepairPreconditionError):
        repair_memo_evidence(data_root, "old-run-1", str(rig["manifest"].manifest_hash))  # type: ignore[union-attr]
    assert not (data_root / "reports" / EVENT_ID / "old-run-1-evidence-v2").exists()


def test_isolated_repair_rejects_missing_proof(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A replay without reportable quantiles publishes no revised citation."""
    rig = _build_rig(tmp_path, monkeypatch)
    data_root = rig["data_root"]
    assert isinstance(data_root, Path)
    monkeypatch.setattr("src.research.repair.build_analogue_proof", lambda context: None)
    with pytest.raises(RepairPreconditionError):
        repair_memo_evidence(data_root, "old-run-1", str(rig["manifest"].manifest_hash))  # type: ignore[union-attr]
    assert not (data_root / "reports" / EVENT_ID / "old-run-1-evidence-v2").exists()


def test_isolated_repair_conflict_without_overwrite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Different bytes at the new run ID abort without touching either run."""
    rig = _build_rig(tmp_path, monkeypatch)
    data_root = rig["data_root"]
    assert isinstance(data_root, Path)
    new_id = "old-run-1-evidence-v2"
    new_dir = data_root / "reports" / EVENT_ID / new_id
    new_dir.mkdir(parents=True)
    (new_dir / "memo.json").write_bytes(b'{"other": true}\n')
    (new_dir / "memo.md").write_bytes(b"other\n")
    (new_dir / "manifest.json").write_bytes(b'{"other": true}\n')
    catalog = Catalog(data_root / "catalog.sqlite")
    catalog.register_research_run(
        new_id,
        hashlib.sha256((new_dir / "manifest.json").read_bytes()).hexdigest(),
        "COMPLETE",
        PurePosixPath(f"reports/{EVENT_ID}/{new_id}/manifest.json"),
    )
    old_memo_raw = (data_root / "reports" / EVENT_ID / "old-run-1" / "memo.json").read_bytes()
    with pytest.raises(ResearchRunConflictError):
        repair_memo_evidence(data_root, "old-run-1", str(rig["manifest"].manifest_hash))  # type: ignore[union-attr]
    assert (data_root / "reports" / EVENT_ID / "old-run-1" / "memo.json").read_bytes() == old_memo_raw
    assert (new_dir / "memo.json").read_bytes() == b'{"other": true}\n'


def _write_old_run(
    data_root: Path,
    run_id: str,
    memo_dict: dict[str, object],
    manifest_run_id: str | None = None,
    manifest_raw_override: bytes | None = None,
    manifest_hash_override: str | None = None,
) -> None:
    """Persist one synthetic old run with consistent hashes unless overridden."""
    catalog = Catalog(data_root / "catalog.sqlite")
    relative = PurePosixPath(f"reports/{EVENT_ID}/{run_id}")
    run_dir = data_root / relative.as_posix()
    run_dir.mkdir(parents=True, exist_ok=True)
    memo_raw = (json.dumps(memo_dict, sort_keys=True, indent=2) + "\n").encode()
    markdown_raw = b"md\n"
    manifest_doc = {
        "event_id": EVENT_ID,
        "manifest_hash": manifest_hash_override if manifest_hash_override is not None else memo_dict.get("manifest_hash", ""),
        "markdown_sha256": hashlib.sha256(markdown_raw).hexdigest(),
        "memo_sha256": hashlib.sha256(memo_raw).hexdigest(),
        "run_id": manifest_run_id if manifest_run_id is not None else run_id,
    }
    manifest_raw = manifest_raw_override or (json.dumps(manifest_doc, sort_keys=True, indent=2) + "\n").encode()
    (run_dir / "memo.json").write_bytes(memo_raw)
    (run_dir / "memo.md").write_bytes(markdown_raw)
    (run_dir / "manifest.json").write_bytes(manifest_raw)
    catalog._conn.execute("DELETE FROM research_run WHERE run_id=?", (run_id,))  # noqa: SLF001
    catalog._conn.commit()
    catalog.register_research_run(
        run_id, hashlib.sha256(manifest_raw).hexdigest(), "COMPLETE", relative / "manifest.json"
    )


def _valid_memo_dict(rig: dict[str, object]) -> dict[str, object]:
    data_root = rig["data_root"]
    assert isinstance(data_root, Path)
    return json.loads((data_root / "reports" / EVENT_ID / "old-run-1" / "memo.json").read_bytes().decode())


def test_isolated_old_manifest_rejections(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Corrupt run manifests abort before any replay."""
    rig = _build_rig(tmp_path, monkeypatch)
    data_root = rig["data_root"]
    assert isinstance(data_root, Path)
    index_hash = str(rig["manifest"].manifest_hash)  # type: ignore[union-attr]
    catalog = Catalog(data_root / "catalog.sqlite")
    relative = PurePosixPath(f"reports/{EVENT_ID}/old-manifest")
    run_dir = data_root / relative.as_posix()
    run_dir.mkdir(parents=True)
    (run_dir / "memo.json").write_bytes(b"{}\n")
    (run_dir / "memo.md").write_bytes(b"md\n")
    for label, raw in (("bytes", b"not json"), ("shape", b"[]\n")):
        (run_dir / "manifest.json").write_bytes(raw)
        catalog._conn.execute("DELETE FROM research_run WHERE run_id=?", ("old-manifest",))  # noqa: SLF001
        catalog._conn.commit()
        catalog.register_research_run(
            "old-manifest", hashlib.sha256(raw).hexdigest(), "COMPLETE", relative / "manifest.json"
        )
        with pytest.raises(RepairPreconditionError):
            repair_memo_evidence(data_root, "old-manifest", index_hash)
        assert label in ("bytes", "shape")
    memo_dict = _valid_memo_dict(rig)
    _write_old_run(data_root, "old-manifest", memo_dict, manifest_run_id="other-id")
    with pytest.raises(RepairPreconditionError):
        repair_memo_evidence(data_root, "old-manifest", index_hash)


def test_isolated_old_memo_rejections(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Corrupt stored memos abort before any replay."""
    rig = _build_rig(tmp_path, monkeypatch)
    data_root = rig["data_root"]
    assert isinstance(data_root, Path)
    index_hash = str(rig["manifest"].manifest_hash)  # type: ignore[union-attr]
    base = _valid_memo_dict(rig)

    def _case(run_id: str, mutate: object, manifest_hash_override: str | None = None) -> None:
        memo_dict = json.loads(json.dumps(base))
        assert isinstance(memo_dict, dict)
        mutate(memo_dict)  # type: ignore[operator]
        _write_old_run(data_root, run_id, memo_dict, manifest_hash_override=manifest_hash_override)
        with pytest.raises(RepairPreconditionError):
            repair_memo_evidence(data_root, run_id, index_hash)
        assert not (data_root / "reports" / EVENT_ID / f"{run_id}-evidence-v2").exists()

    def _tool(memo_dict: dict[str, object]) -> dict[str, object]:
        for item in memo_dict["evidence"]:  # type: ignore[union-attr]
            assert isinstance(item, dict)
            if item["id"] == "tool-analogues":
                return item
        raise AssertionError("missing tool ref")

    _case("old-m1", lambda memo: memo.update({"as_of": "2024-06-29T18:00:00"}))
    _case("old-m2", lambda memo: memo.update({"evidence": [*memo["evidence"], memo["evidence"][0]]}))  # type: ignore[union-attr]
    _case("old-m3", lambda memo: memo.update({"evidence": [item for item in memo["evidence"] if item["id"] != "tool-analogues"]}))  # type: ignore[union-attr]
    _case("old-m4", lambda memo: _tool(memo).update({"local_relative_path": "raw/dart/other.zip"}))
    _case("old-m5", lambda memo: memo.update({"evidence": [*memo["evidence"], {"id": "extra", "source_kind": "dart_filing_zip", "local_relative_path": "raw/dart/a.zip", "sha256": "not-a-hash", "locator": "x"}]}))  # type: ignore[union-attr]
    _case("old-m6", lambda memo: memo.update({"evidence": [*memo["evidence"], {"id": "extra", "source_kind": "dart_filing_zip", "local_relative_path": "raw/dart/gone.zip", "sha256": "ab" * 32, "locator": "x"}]}))  # type: ignore[union-attr]
    _case("old-m7", lambda memo: memo.update({"evidence": [*memo["evidence"], {"id": "extra", "source_kind": "dart_filing_zip", "local_relative_path": "raw/dart/f1.zip", "sha256": "ab" * 32, "locator": "x"}]}))  # type: ignore[union-attr]
    _case("old-m8", lambda memo: None, manifest_hash_override="00" * 32)


def test_isolated_replay_and_publish_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Replay, proof, and publication failures abort without a new run."""
    import src.research.repair as repair_module

    real_proof_builder = repair_module.build_analogue_proof
    rig = _build_rig(tmp_path, monkeypatch)
    data_root = rig["data_root"]
    assert isinstance(data_root, Path)
    index_hash = str(rig["manifest"].manifest_hash)  # type: ignore[union-attr]

    def _boom(*args: object, **kwargs: object) -> object:
        raise ValueError("boom")

    monkeypatch.setattr("src.research.repair.build_research_context", _boom)
    with pytest.raises(RepairPreconditionError):
        repair_memo_evidence(data_root, "old-run-1", index_hash)
    rig2_context = rig["context"]
    monkeypatch.setattr("src.research.repair.build_research_context", lambda *args, **kwargs: rig2_context)
    monkeypatch.setattr("src.research.repair.build_analogue_proof", _boom)
    with pytest.raises(RepairPreconditionError):
        repair_memo_evidence(data_root, "old-run-1", index_hash)
    monkeypatch.setattr("src.research.repair.build_research_context", lambda *args, **kwargs: rig2_context)
    monkeypatch.setattr("src.research.repair.build_analogue_proof", real_proof_builder)

    def _publish_boom(data_root: Path, run_id: str, memo: object, proof: object, manifest: object) -> object:
        raise ValueError("boom")

    monkeypatch.setattr("src.research.repair.publish_research_run", _publish_boom)
    with pytest.raises(RepairPreconditionError):
        repair_memo_evidence(data_root, "old-run-1", index_hash)
    assert not (data_root / "reports" / EVENT_ID / "old-run-1-evidence-v2").exists()


def test_isolated_new_memo_drift_rejections(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Any replayed economic drift aborts without publishing."""
    import src.research.repair as repair_module

    rig = _build_rig(tmp_path, monkeypatch)
    data_root = rig["data_root"]
    assert isinstance(data_root, Path)
    index_hash = str(rig["manifest"].manifest_hash)  # type: ignore[union-attr]
    real_builder = repair_module.build_baseline_memo

    def _run_with(memo_mutator: object, proof_mutator: object = None) -> None:
        def _memo(context: object, analogue_ref: object = None) -> object:
            memo = real_builder(context, analogue_ref=analogue_ref)  # type: ignore[arg-type]
            return memo_mutator(memo)  # type: ignore[operator]

        monkeypatch.setattr("src.research.repair.build_baseline_memo", _memo)
        if proof_mutator is not None:
            monkeypatch.setattr("src.research.repair.proof_reference", proof_mutator)
        with pytest.raises(RepairPreconditionError):
            repair_memo_evidence(data_root, "old-run-1", index_hash)
        assert not (data_root / "reports" / EVENT_ID / "old-run-1-evidence-v2").exists()
        monkeypatch.setattr("src.research.repair.build_baseline_memo", real_builder)
        monkeypatch.setattr("src.research.repair.proof_reference", repair_module.proof_reference)

    _run_with(lambda memo: dataclasses.replace(memo, event_id="other"))
    _run_with(lambda memo: dataclasses.replace(memo, anchor_rcept_no="other"))
    _run_with(lambda memo: dataclasses.replace(memo, as_of=datetime(2024, 6, 30, 18, 0, tzinfo=KST)))
    _run_with(lambda memo: dataclasses.replace(memo, facts={**dict(memo.facts), "market": "KOSDAQ"}))
    _run_with(lambda memo: dataclasses.replace(memo, statuses=(*memo.statuses, "EXTRA")))
    _run_with(lambda memo: dataclasses.replace(memo, claims=memo.claims[1:]))
    _run_with(lambda memo: dataclasses.replace(memo, evidence=memo.evidence[1:]))
    _run_with(
        lambda memo: memo,
        lambda run_dir, proof: EvidenceRef("tool-analogues", "tool_result", run_dir / "analogue-proof.json" if isinstance(run_dir, Path) else PurePosixPath("x"), "00" * 32, "x"),
    )


def test_isolated_repair_detects_old_bytes_changed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A mutation of the immutable old run during repair aborts the result."""
    rig = _build_rig(tmp_path, monkeypatch)
    data_root = rig["data_root"]
    assert isinstance(data_root, Path)
    import src.research.repair as repair_module

    real_publish = repair_module.publish_research_run

    def _tampering_publish(data_root: Path, run_id: str, memo: object, proof: object, manifest: object) -> object:
        published = real_publish(data_root, run_id, memo, proof, manifest)  # type: ignore[arg-type]
        (data_root / "reports" / EVENT_ID / "old-run-1" / "memo.md").write_bytes(b"tampered\n")
        return published

    monkeypatch.setattr("src.research.repair.publish_research_run", _tampering_publish)
    with pytest.raises(RepairPreconditionError):
        repair_memo_evidence(data_root, "old-run-1", str(rig["manifest"].manifest_hash))  # type: ignore[union-attr]
