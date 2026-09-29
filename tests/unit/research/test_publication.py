"""Invariant guards for evidence verification and immutable publication."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo

import pytest

from src.data.catalog import Catalog
from src.research.analogue_proof import AnalogueProof
from src.research.memo import EvidenceRef, MemoClaim, ResearchMemo
from src.research.publication import proof_reference, publish_research_run, validate_memo_evidence

KST = ZoneInfo("Asia/Seoul")
AS_OF = datetime(2024, 6, 24, 18, 0, tzinfo=KST)


def _register(data_root: Path, relative: str, raw: bytes) -> str:
    catalog = Catalog(data_root / "catalog.sqlite")
    return catalog.register_artifact(
        source="test",
        endpoint="file",
        request_key=relative,
        snapshot_id="snap",
        raw_bytes=raw,
        retrieved_at=datetime(2024, 6, 24, 12, 0, tzinfo=KST),
        local_relative_path=PurePosixPath(relative),
    )


def _ref(ref_id: str, relative: str, digest: str) -> EvidenceRef:
    return EvidenceRef(ref_id, "dart_filing_zip", PurePosixPath(relative), digest, f"rcept={ref_id}")


def _memo(evidence: tuple[EvidenceRef, ...] = (), claims: tuple[MemoClaim, ...] = ()) -> ResearchMemo:
    return ResearchMemo(
        event_id="evt-1",
        anchor_rcept_no="20240620000001",
        active_rcept_no="20240620000001",
        as_of=AS_OF,
        facts={},
        metrics={},
        claims=claims,
        evidence=evidence,
        statuses=(),
        manifest_hash="0" * 64,
    )


def _claim(ref_id: str) -> MemoClaim:
    return MemoClaim("filing_fact", f"Observed {ref_id}.", (ref_id,), None)


def _staged_proof(entries: list[object]) -> AnalogueProof:
    document = {"inputs": entries, "proof_schema": "analogue-proof-v1"}
    raw = (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    return AnalogueProof(payload=raw, sha256=hashlib.sha256(raw).hexdigest(), input_hashes=())


def _proof_with_inputs(pairs: list[tuple[str, str]]) -> AnalogueProof:
    return _staged_proof([{"local_path": path, "sha256": digest} for path, digest in pairs])


def _proof_memo(proof: AnalogueProof, relative: str) -> ResearchMemo:
    ref = EvidenceRef("tool-analogues", "tool_result", PurePosixPath(relative), proof.sha256, "proof")
    claim = MemoClaim("peer_context", "Observed analogues.", ("tool-analogues",), "analogue_median")
    return _memo((ref,), (claim,))


def test_proof_reference_uses_reports_relative_convention(tmp_path: Path) -> None:
    """Run-local proof citations stay beneath reports in project-relative form."""
    proof = _staged_proof([])
    absolute = proof_reference(tmp_path / "data" / "reports" / "evt-1" / "run-1", proof)
    assert absolute.id == "tool-analogues"
    assert absolute.local_relative_path == PurePosixPath("reports/evt-1/run-1/analogue-proof.json")
    assert absolute.sha256 == proof.sha256
    bare = proof_reference(Path("run-9"), proof)
    assert bare.local_relative_path == PurePosixPath("run-9/analogue-proof.json")


def test_validate_accepts_resolvable_evidence(tmp_path: Path) -> None:
    """Citations whose bytes match the declared hash pass, including shared files once."""
    data_root = tmp_path / "data"
    digest = _register(data_root, "raw/dart/a.zip", b"filing-bytes")
    first = _ref("filing-a", "raw/dart/a.zip", digest)
    second = _ref("filing-b", "raw/dart/a.zip", digest)
    validate_memo_evidence(data_root, _memo((first, second), (_claim("filing-a"), _claim("filing-b"))))


def test_changed_byte_fails_validation(tmp_path: Path) -> None:
    """A single changed byte in a cited source fails validation."""
    data_root = tmp_path / "data"
    digest = _register(data_root, "raw/dart/a.zip", b"filing-bytes")
    (data_root / "raw/dart/a.zip").write_bytes(b"tampered-bytes")
    with pytest.raises(ValueError, match="hash mismatch"):
        validate_memo_evidence(data_root, _memo((_ref("filing-a", "raw/dart/a.zip", digest),), (_claim("filing-a"),)))


def test_missing_source_file_fails_validation(tmp_path: Path) -> None:
    """A cited source without local bytes cannot validate."""
    data_root = tmp_path / "data"
    Catalog(data_root / "catalog.sqlite")
    with pytest.raises(ValueError, match="missing evidence file"):
        validate_memo_evidence(
            data_root, _memo((_ref("filing-a", "raw/dart/a.zip", "ab" * 32),), (_claim("filing-a"),))
        )


def test_forged_paths_are_rejected(tmp_path: Path) -> None:
    """Absolute paths and parent traversal never resolve beneath the data root."""
    data_root = tmp_path / "data"
    Catalog(data_root / "catalog.sqlite")
    absolute = EvidenceRef("evil", "dart_filing_zip", PurePosixPath("/etc/passwd"), "ab" * 32, "x")
    with pytest.raises(ValueError, match="unsafe local path"):
        validate_memo_evidence(data_root, _memo((absolute,), (_claim("evil"),)))
    traversal = EvidenceRef("evil", "dart_filing_zip", PurePosixPath("../escape.bin"), "ab" * 32, "x")
    with pytest.raises(ValueError, match="unsafe local path"):
        validate_memo_evidence(data_root, _memo((traversal,), (_claim("evil"),)))


def test_symlink_escape_is_rejected(tmp_path: Path) -> None:
    """A symlink pointing outside the data root fails validation."""
    data_root = tmp_path / "data"
    (data_root / "raw").mkdir(parents=True)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"external")
    (data_root / "raw" / "linked.bin").symlink_to(outside)
    digest = hashlib.sha256(b"external").hexdigest()
    with pytest.raises(ValueError, match="symlinked local path"):
        validate_memo_evidence(data_root, _memo((_ref("link", "raw/linked.bin", digest),), (_claim("link"),)))


def test_duplicate_and_dangling_references_fail(tmp_path: Path) -> None:
    """Evidence IDs resolve exactly once and every claimed ID exists."""
    data_root = tmp_path / "data"
    Catalog(data_root / "catalog.sqlite")
    ref = _ref("filing-a", "raw/dart/a.zip", "ab" * 32)
    with pytest.raises(ValueError, match="duplicate evidence id"):
        validate_memo_evidence(data_root, _memo((ref, ref), (_claim("filing-a"),)))
    with pytest.raises(ValueError, match="missing evidence reference"):
        validate_memo_evidence(data_root, _memo((ref,), (_claim("filing-a"), _claim("ghost"))))


def test_malformed_evidence_hash_fails(tmp_path: Path) -> None:
    """A non-hex evidence digest is rejected before any file read."""
    data_root = tmp_path / "data"
    Catalog(data_root / "catalog.sqlite")
    with pytest.raises(ValueError, match="invalid evidence hash"):
        validate_memo_evidence(data_root, _memo((_ref("bad", "raw/dart/a.zip", "not-a-hash"),), (_claim("bad"),)))


def test_staged_proof_verified_from_virtual_bytes(tmp_path: Path) -> None:
    """The staged proof validates from payload bytes before its final path exists."""
    data_root = tmp_path / "data"
    digest = _register(data_root, "raw/krx/day.json", b"index-bytes")
    proof = _proof_with_inputs([("raw/krx/day.json", digest)])
    memo = _proof_memo(proof, "reports/evt-1/run-1/analogue-proof.json")
    validate_memo_evidence(data_root, memo, staged_proof=proof)


def test_staged_proof_mismatch_fails(tmp_path: Path) -> None:
    """A proof whose bytes do not match the citation is rejected."""
    data_root = tmp_path / "data"
    Catalog(data_root / "catalog.sqlite")
    proof = _staged_proof([])
    memo = _proof_memo(AnalogueProof(payload=proof.payload, sha256="00" * 32, input_hashes=()), "reports/evt-1/run-1/analogue-proof.json")
    with pytest.raises(ValueError, match="staged proof hash mismatch"):
        validate_memo_evidence(data_root, memo, staged_proof=proof)


def test_undecodable_and_malformed_proof_fail(tmp_path: Path) -> None:
    """Unparseable or off-schema staged proofs are rejected."""
    data_root = tmp_path / "data"
    Catalog(data_root / "catalog.sqlite")
    raw = b"not json"
    undecodable = AnalogueProof(payload=raw, sha256=hashlib.sha256(raw).hexdigest(), input_hashes=())
    with pytest.raises(ValueError, match="undecodable staged proof"):
        validate_memo_evidence(data_root, _proof_memo(undecodable, "reports/evt-1/run-1/analogue-proof.json"), staged_proof=undecodable)
    off_schema = _staged_proof("not-a-list")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="invalid staged proof"):
        validate_memo_evidence(data_root, _proof_memo(off_schema, "reports/evt-1/run-1/analogue-proof.json"), staged_proof=off_schema)


def test_noncanonical_and_malformed_input_fail(tmp_path: Path) -> None:
    """Non-canonical encodings and malformed input entries are rejected."""
    data_root = tmp_path / "data"
    Catalog(data_root / "catalog.sqlite")
    document = {"inputs": [], "proof_schema": "analogue-proof-v1"}
    raw = (json.dumps(document, sort_keys=True, indent=2) + "\n").encode("utf-8")
    noncanonical = AnalogueProof(payload=raw, sha256=hashlib.sha256(raw).hexdigest(), input_hashes=())
    with pytest.raises(ValueError, match="non-canonical"):
        validate_memo_evidence(data_root, _proof_memo(noncanonical, "reports/evt-1/run-1/analogue-proof.json"), staged_proof=noncanonical)
    malformed = _staged_proof(["not-a-dict"])
    with pytest.raises(ValueError, match="invalid staged proof input"):
        validate_memo_evidence(data_root, _proof_memo(malformed, "reports/evt-1/run-1/analogue-proof.json"), staged_proof=malformed)


def test_publish_writes_proof_sibling_and_registers(tmp_path: Path) -> None:
    """Published proof sits beside the memo and its inputs stay discoverable for cleanup."""
    data_root = tmp_path / "data"
    digest = _register(data_root, "raw/krx/day.json", b"index-bytes")
    proof = _proof_with_inputs([("raw/krx/day.json", digest)])
    run_dir = data_root / "reports" / "evt-1" / "run-1"
    ref = proof_reference(run_dir, proof)
    memo = _proof_memo(proof, ref.local_relative_path.as_posix())
    manifest = {"event_id": "evt-1", "run_id": "run-1"}
    published = publish_research_run(data_root, "run-1", memo, proof, manifest)
    assert (run_dir / "analogue-proof.json").read_bytes() == proof.payload
    assert json.loads((run_dir / "memo.json").read_bytes().decode())["event_id"] == "evt-1"
    assert published.manifest_hash == hashlib.sha256((run_dir / "manifest.json").read_bytes()).hexdigest()
    catalog = Catalog(data_root / "catalog.sqlite")
    assert catalog.get_research_run("run-1") is not None
    assert catalog.references_to_hashes(frozenset({digest})) == {digest: ("run-1",)}
    validate_memo_evidence(data_root, memo)


def test_publish_without_proof_omits_proof_file(tmp_path: Path) -> None:
    """A run without reportable quantiles publishes no proof file or analogue claim."""
    data_root = tmp_path / "data"
    Catalog(data_root / "catalog.sqlite")
    published = publish_research_run(data_root, "run-plain", _memo(), None, {"run_id": "run-plain"})
    run_dir = data_root / "reports" / "evt-1" / "run-plain"
    assert (run_dir / "memo.json").is_file()
    assert not (run_dir / "analogue-proof.json").exists()
    assert published.proof is None


def test_identical_retry_leaves_bytes_unchanged(tmp_path: Path) -> None:
    """A byte-identical retry succeeds without changing file mtimes or catalog identity."""
    data_root = tmp_path / "data"
    Catalog(data_root / "catalog.sqlite")
    manifest = {"run_id": "run-1"}
    first = publish_research_run(data_root, "run-1", _memo(), None, manifest)
    memo_path = data_root / "reports" / "evt-1" / "run-1" / "memo.json"
    before = memo_path.stat().st_mtime_ns
    second = publish_research_run(data_root, "run-1", _memo(), None, manifest)
    assert second.manifest_hash == first.manifest_hash
    assert memo_path.stat().st_mtime_ns == before


def test_conflicting_run_id_fails_without_touching(tmp_path: Path) -> None:
    """A changed memo under an existing run ID fails while old bytes stay intact."""
    data_root = tmp_path / "data"
    catalog = Catalog(data_root / "catalog.sqlite")
    publish_research_run(data_root, "run-1", _memo(), None, {"run_id": "run-1", "v": 1})
    memo_path = data_root / "reports" / "evt-1" / "run-1" / "memo.json"
    manifest_path = data_root / "reports" / "evt-1" / "run-1" / "manifest.json"
    before_memo = memo_path.read_bytes()
    before_manifest = manifest_path.read_bytes()
    before_hash = catalog.get_research_run("run-1")
    assert before_hash is not None
    digest = _register(data_root, "raw/dart/a.zip", b"filing-a-bytes")
    other = _memo(
        (_ref("filing-a", "raw/dart/a.zip", digest),),
        (_claim("filing-a"),),
    )
    with pytest.raises(ValueError, match="conflicting research run"):
        publish_research_run(data_root, "run-1", other, None, {"run_id": "run-1", "v": 2})
    assert memo_path.read_bytes() == before_memo
    assert manifest_path.read_bytes() == before_manifest
    assert catalog.get_research_run("run-1") == before_hash


def test_interrupted_registration_repaired_by_retry(tmp_path: Path) -> None:
    """A crash after rename but before catalog registration is repaired by an identical retry."""
    data_root = tmp_path / "data"
    catalog = Catalog(data_root / "catalog.sqlite")
    publish_research_run(data_root, "run-1", _memo(), None, {"run_id": "run-1"})
    catalog._conn.execute("DELETE FROM research_run WHERE run_id=?", ("run-1",))  # noqa: SLF001
    catalog._conn.commit()
    assert catalog.get_research_run("run-1") is None
    retried = publish_research_run(data_root, "run-1", _memo(), None, {"run_id": "run-1"})
    assert catalog.get_research_run("run-1") is not None
    assert retried.manifest_hash is not None


def test_catalog_conflict_and_orphan_record_fail(tmp_path: Path) -> None:
    """A differing catalog hash or a record without report files blocks publication."""
    data_root = tmp_path / "data"
    catalog = Catalog(data_root / "catalog.sqlite")
    publish_research_run(data_root, "run-1", _memo(), None, {"run_id": "run-1"})
    catalog._conn.execute(  # noqa: SLF001
        "UPDATE research_run SET manifest_hash=? WHERE run_id=?", ("ff" * 32, "run-1")
    )
    catalog._conn.commit()
    with pytest.raises(ValueError, match="conflicting research run"):
        publish_research_run(data_root, "run-1", _memo(), None, {"run_id": "run-1"})
    catalog._conn.execute("DELETE FROM research_run WHERE run_id=?", ("run-1",))  # noqa: SLF001
    catalog._conn.commit()
    manifest_raw = (data_root / "reports" / "evt-1" / "run-1" / "manifest.json").read_bytes()
    catalog.register_research_run("run-2", hashlib.sha256(manifest_raw).hexdigest(), "COMPLETE", PurePosixPath("reports/evt-1/run-1/manifest.json"))
    with pytest.raises(ValueError, match="conflicting research run"):
        publish_research_run(data_root, "run-2", _memo(), None, {"run_id": "run-2"})


def test_invalid_run_request_fails(tmp_path: Path) -> None:
    """Empty, traversing, or non-mapping run requests fail before any write."""
    data_root = tmp_path / "data"
    Catalog(data_root / "catalog.sqlite")
    with pytest.raises(ValueError, match="invalid run id"):
        publish_research_run(data_root, "", _memo(), None, {})
    with pytest.raises(ValueError, match="invalid run id"):
        publish_research_run(data_root, "../escape", _memo(), None, {})
    with pytest.raises(ValueError, match="must be a mapping"):
        publish_research_run(data_root, "run-1", _memo(), None, [])  # type: ignore[arg-type]
    assert not (data_root / "reports").exists()


def test_missing_proof_input_aborts_before_writes(tmp_path: Path) -> None:
    """A proof citing a missing local input cannot yield a published analogue claim."""
    data_root = tmp_path / "data"
    Catalog(data_root / "catalog.sqlite")
    proof = _proof_with_inputs([("raw/krx/gone.json", "ab" * 32)])
    run_dir = data_root / "reports" / "evt-1" / "run-1"
    ref = proof_reference(run_dir, proof)
    memo = _proof_memo(proof, ref.local_relative_path.as_posix())
    with pytest.raises(ValueError, match="missing evidence file"):
        publish_research_run(data_root, "run-1", memo, proof, {"run_id": "run-1"})
    assert not (data_root / "reports").exists()
