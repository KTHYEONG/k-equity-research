"""Immutable evidence-backed research run publication."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from src.data.catalog import Catalog
from src.data.local_paths import checked_local_path
from src.research.analogue_proof import AnalogueProof
from src.research.memo import EvidenceRef, ResearchMemo, memo_to_dict, render_markdown

_PROOF_FILENAME = "analogue-proof.json"
_PROOF_SCHEMA = "analogue-proof-v1"
_HEXDIGITS = frozenset("0123456789abcdefABCDEF")


@dataclass(frozen=True, slots=True)
class PublishedResearchRun:
    """Immutable published research run with verified evidence."""

    run_id: str
    memo: ResearchMemo
    proof: AnalogueProof | None
    manifest_hash: str
    report_dir: PurePosixPath


def _is_hex64(value: str) -> bool:
    return len(value) == 64 and all(char in _HEXDIGITS for char in value)


def proof_reference(run_dir: Path, proof: AnalogueProof) -> EvidenceRef:
    """Create the run-local citation for deterministic analogue proof bytes."""
    parts = run_dir.parts
    if "reports" in parts:
        relative = PurePosixPath(*parts[parts.index("reports"):]) / _PROOF_FILENAME
    else:
        relative = PurePosixPath(run_dir.name) / _PROOF_FILENAME
    return EvidenceRef(
        id="tool-analogues",
        source_kind="tool_result",
        local_relative_path=relative,
        sha256=proof.sha256.lower(),
        locator=f"analogue-proof inputs={len(proof.input_hashes)} schema={_PROOF_SCHEMA}",
    )


def _verify_local_file(
    data_root: Path, relative: PurePosixPath, digest: str, seen: dict[tuple[str, str], None]
) -> None:
    key = (relative.as_posix(), digest.lower())
    if key in seen:
        return
    target = checked_local_path(data_root, relative)
    if target.is_symlink() or not target.is_file():
        raise ValueError(f"missing evidence file: {relative.as_posix()}")
    if hashlib.sha256(target.read_bytes()).hexdigest() != digest.lower():
        raise ValueError(f"hash mismatch for evidence file: {relative.as_posix()}")
    seen[key] = None


def _verify_staged_proof(
    data_root: Path, ref: EvidenceRef, proof: AnalogueProof, seen: dict[tuple[str, str], None]
) -> None:
    if ref.sha256.lower() != proof.sha256.lower() or hashlib.sha256(proof.payload).hexdigest() != proof.sha256.lower():
        raise ValueError("staged proof hash mismatch")
    try:
        document = json.loads(proof.payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("undecodable staged proof") from exc
    if (
        not isinstance(document, dict)
        or document.get("proof_schema") != _PROOF_SCHEMA
        or not isinstance(document.get("inputs"), list)
    ):
        raise ValueError("invalid staged proof")
    canonical = (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    if canonical != proof.payload:
        raise ValueError("non-canonical staged proof encoding")
    inputs = document["inputs"]
    for entry in inputs:
        if not isinstance(entry, dict):
            raise ValueError("invalid staged proof input")
        _verify_local_file(
            data_root, PurePosixPath(str(entry.get("local_path", ""))), str(entry.get("sha256", "")), seen
        )


def _verify_reference(
    data_root: Path,
    ref: EvidenceRef,
    staged_proof: AnalogueProof | None,
    seen: dict[tuple[str, str], None],
) -> None:
    if not _is_hex64(ref.sha256):
        raise ValueError(f"invalid evidence hash: {ref.id}")
    if ref.id == "tool-analogues" and staged_proof is not None:
        _verify_staged_proof(data_root, ref, staged_proof, seen)
        return
    _verify_local_file(data_root, ref.local_relative_path, ref.sha256, seen)


def validate_memo_evidence(
    data_root: Path,
    memo: ResearchMemo,
    staged_proof: AnalogueProof | None = None,
) -> None:
    """Reject unsafe, missing, or byte-invalid citations before publication.

    The staged proof is verified from its virtual bytes before its final path
    exists; every other reference is checked against an existing local file.
    """
    by_id: dict[str, EvidenceRef] = {}
    for ref in memo.evidence:
        if ref.id in by_id:
            raise ValueError(f"duplicate evidence id: {ref.id}")
        by_id[ref.id] = ref
    wanted: set[str] = set()
    for claim in memo.claims:
        wanted.update(claim.evidence_ids)
    for ref_id in sorted(wanted):
        if ref_id not in by_id:
            raise ValueError(f"missing evidence reference: {ref_id}")
    seen: dict[tuple[str, str], None] = {}
    for ref in sorted(memo.evidence, key=lambda item: item.id):
        _verify_reference(data_root, ref, staged_proof, seen)


def publish_research_run(
    data_root: Path,
    run_id: str,
    memo: ResearchMemo,
    proof: AnalogueProof | None,
    manifest: Mapping[str, object],
) -> PublishedResearchRun:
    """Verify and atomically publish one immutable research run.

    A byte-identical existing run is returned unchanged. A conflicting run ID
    raises before any existing report file or catalog row is modified.
    """
    if not run_id or "/" in run_id or run_id in (".", "..") or ".." in run_id:
        raise ValueError(f"invalid run id: {run_id!r}")
    if not isinstance(manifest, Mapping):
        raise ValueError("run manifest must be a mapping")
    validate_memo_evidence(data_root, memo, staged_proof=proof)
    relative_dir = PurePosixPath("reports") / memo.event_id / run_id
    run_dir = checked_local_path(data_root, relative_dir)
    memo_payload = (json.dumps(memo_to_dict(memo), sort_keys=True, indent=2) + "\n").encode("utf-8")
    markdown_payload = render_markdown(memo).encode("utf-8")
    manifest_payload = (json.dumps(dict(manifest), sort_keys=True, indent=2) + "\n").encode("utf-8")
    manifest_hash = hashlib.sha256(manifest_payload).hexdigest()
    wanted: dict[str, bytes] = {"memo.json": memo_payload, "memo.md": markdown_payload, "manifest.json": manifest_payload}
    if proof is not None:
        wanted[_PROOF_FILENAME] = proof.payload
    catalog = Catalog(data_root / "catalog.sqlite")
    record = catalog.get_research_run(run_id)
    if run_dir.is_dir():
        for name, payload in wanted.items():
            target = run_dir / name
            if target.is_symlink() or not target.is_file() or target.read_bytes() != payload:
                raise ValueError(f"conflicting research run: {run_id}")
        if record is not None and record.manifest_hash != manifest_hash:
            raise ValueError(f"conflicting research run: {run_id}")
        if record is None:
            catalog.register_research_run(run_id, manifest_hash, "COMPLETE", relative_dir / "manifest.json")
        return PublishedResearchRun(run_id, memo, proof, manifest_hash, relative_dir)
    if record is not None:
        raise ValueError(f"conflicting research run: {run_id}")
    staging = run_dir.parent / (run_dir.name + ".staging")
    staging.mkdir(parents=True, exist_ok=True)
    for name, payload in wanted.items():
        (staging / name).write_bytes(payload)
    os.replace(staging, run_dir)
    catalog.register_research_run(run_id, manifest_hash, "COMPLETE", relative_dir / "manifest.json")
    return PublishedResearchRun(run_id, memo, proof, manifest_hash, relative_dir)


__all__ = [
    "PublishedResearchRun",
    "proof_reference",
    "publish_research_run",
    "validate_memo_evidence",
]
