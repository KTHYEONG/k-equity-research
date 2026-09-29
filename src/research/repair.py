"""Offline repair for one historical memo with a mis-cited analogue reference."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

from src.data.catalog import Catalog
from src.data.event_store import EventStore
from src.data.financial_evidence import FinancialEvidence
from src.data.index_store import load_index_manifest
from src.data.local_lake import LocalLake
from src.data.local_paths import checked_local_path
from src.research.analogue_proof import AnalogueProof, build_analogue_proof
from src.research.context import ResearchUnavailable, build_research_context
from src.research.event_study import StudyPolicy
from src.research.memo import CODE_REVISION, EvidenceRef, MemoClaim, ResearchMemo, build_baseline_memo
from src.research.publication import proof_reference, publish_research_run

_HEXDIGITS = frozenset("0123456789abcdefABCDEF")
_KNOWN_BAD_PATH = PurePosixPath("raw/dart/pilot-202406-20260928/doc-20240626000369.zip")


class RepairPreconditionError(ValueError):  # noqa: N818 - spec-mandated boundary name
    """Reject an unsafe or drifting historical repair before publication."""


class ResearchRunConflictError(ValueError):  # noqa: N818 - spec-mandated boundary name
    """Signal a conflicting existing run ID during historical repair."""


@dataclass(frozen=True, slots=True)
class RepairSummary:
    """Stored identity for one repaired historical memo run."""

    old_run_id: str
    new_run_id: str
    manifest_hash: str
    proof_sha256: str


def _is_hex64(value: str) -> bool:
    return len(value) == 64 and all(char in _HEXDIGITS for char in value)


def _parse_memo(document: object) -> ResearchMemo:
    if not isinstance(document, dict):
        raise RepairPreconditionError("old memo is not a JSON object")
    raw: dict[str, object] = dict(document)
    try:
        evidence_raw = raw["evidence"]
        claims_raw = raw["claims"]
        facts_raw = raw["facts"]
        metrics_raw = raw["metrics"]
        statuses_raw = raw["statuses"]
        if not isinstance(evidence_raw, list) or not isinstance(claims_raw, list):
            raise RepairPreconditionError("undecodable old memo")
        if not isinstance(facts_raw, dict) or not isinstance(metrics_raw, dict):
            raise RepairPreconditionError("undecodable old memo")
        if not isinstance(statuses_raw, list):
            raise RepairPreconditionError("undecodable old memo")
        evidence = tuple(
            EvidenceRef(
                id=str(item["id"]),
                source_kind=str(item["source_kind"]),
                local_relative_path=PurePosixPath(str(item["local_relative_path"])),
                sha256=str(item["sha256"]),
                locator=str(item["locator"]),
            )
            for item in evidence_raw
            if isinstance(item, dict)
        )
        if len(evidence) != len(evidence_raw):
            raise RepairPreconditionError("undecodable old memo")
        claims = tuple(
            MemoClaim(
                kind=str(item["kind"]),
                text=str(item["text"]),
                evidence_ids=tuple(str(part) for part in item["evidence_ids"] if isinstance(item, dict)),
                metric_key=str(item["metric_key"]) if item["metric_key"] is not None else None,
            )
            for item in claims_raw
            if isinstance(item, dict)
        )
        if len(claims) != len(claims_raw):
            raise RepairPreconditionError("undecodable old memo")
        as_of = datetime.fromisoformat(str(raw["as_of"]))
        return ResearchMemo(
            event_id=str(raw["event_id"]),
            anchor_rcept_no=str(raw["anchor_rcept_no"]),
            active_rcept_no=str(raw["active_rcept_no"]),
            as_of=as_of,
            facts={str(key): (None if value is None else str(value)) for key, value in facts_raw.items()},
            metrics={str(key): (None if value is None else str(value)) for key, value in metrics_raw.items()},
            claims=claims,
            evidence=evidence,
            statuses=tuple(str(item) for item in statuses_raw),
            manifest_hash=str(raw["manifest_hash"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RepairPreconditionError(f"undecodable old memo: {exc}") from exc


def _read_import_manifests(data_root: Path) -> dict[str, object]:
    from src.cli.batch import _read_import_manifests as _load

    return dict(_load(data_root))


def _verify_old_bytes(data_root: Path, relative: PurePosixPath, expected: str, label: str) -> bytes:
    target = checked_local_path(data_root, relative)
    if target.is_symlink() or not target.is_file():
        raise RepairPreconditionError(f"missing old {label}: {relative.as_posix()}")
    raw = target.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected.lower():
        raise RepairPreconditionError(f"hash mismatch for old {label}: {relative.as_posix()}")
    return raw


def repair_memo_evidence(
    data_root: Path,
    old_run_id: str,
    index_manifest_hash: str,
) -> RepairSummary:
    """Rebuild one historical memo with verified analogue evidence.

    Args:
        data_root: Project-local data root containing the registered old run.
        old_run_id: Immutable run to supersede without modifying it.
        index_manifest_hash: Explicit SHA-256 of the local index manifest used
            for deterministic replay.

    Returns:
        The old and new run IDs, new manifest hash, and verified proof hash.

    Raises:
        RepairPreconditionError: If the old run has unexpected corruption,
            required local inputs are absent, or replayed economics differ.
        ResearchRunConflictError: If the new run ID already has different bytes.
    """
    if not old_run_id or "/" in old_run_id or old_run_id in (".", "..") or ".." in old_run_id:
        raise RepairPreconditionError(f"invalid old run id: {old_run_id!r}")
    if not _is_hex64(index_manifest_hash):
        raise RepairPreconditionError("invalid index manifest hash")
    wanted_manifest = index_manifest_hash.lower()
    try:
        manifest = load_index_manifest(data_root, wanted_manifest)
    except (ValueError, OSError) as exc:
        raise RepairPreconditionError(f"missing index manifest: {exc}") from exc

    catalog = Catalog(data_root / "catalog.sqlite")
    record = catalog.get_research_run(old_run_id)
    if record is None:
        raise RepairPreconditionError(f"unknown old run id: {old_run_id}")
    manifest_raw = _verify_old_bytes(data_root, record.local_path, record.manifest_hash, "run manifest")
    try:
        manifest_doc = json.loads(manifest_raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise RepairPreconditionError("undecodable old run manifest") from exc
    if not isinstance(manifest_doc, dict):
        raise RepairPreconditionError("undecodable old run manifest")
    old_manifest_hash = record.manifest_hash.lower()
    report_dir = checked_local_path(data_root, record.local_path.parent)
    old_memo_raw = _verify_old_bytes(
        data_root,
        record.local_path.parent / "memo.json",
        str(manifest_doc.get("memo_sha256", "")),
        "memo",
    )
    _verify_old_bytes(
        data_root,
        record.local_path.parent / "memo.md",
        str(manifest_doc.get("markdown_sha256", "")),
        "markdown",
    )
    if str(manifest_doc.get("run_id", "")) != old_run_id:
        raise RepairPreconditionError("old run manifest identity mismatch")
    before = {
        name: hashlib.sha256((report_dir / name).read_bytes()).hexdigest()
        for name in ("memo.json", "memo.md", "manifest.json")
        if (report_dir / name).is_file()
    }
    try:
        old_memo_doc = json.loads(old_memo_raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise RepairPreconditionError("undecodable old memo") from exc
    old_memo = _parse_memo(old_memo_doc)
    if old_memo.as_of.tzinfo is None or old_memo.as_of.utcoffset() is None:
        raise RepairPreconditionError("old memo as-of instant must be timezone-aware")
    if old_memo.manifest_hash.lower() != str(manifest_doc.get("manifest_hash", "")).lower():
        raise RepairPreconditionError("old memo manifest mismatch")

    by_id = {ref.id: ref for ref in old_memo.evidence}
    if len(by_id) != len(old_memo.evidence):
        raise RepairPreconditionError("duplicate evidence id in old memo")
    old_tool = by_id.get("tool-analogues")
    if old_tool is None:
        raise RepairPreconditionError("old memo has no tool-analogues reference")
    if old_tool.local_relative_path != _KNOWN_BAD_PATH:
        raise RepairPreconditionError("unexpected tool-analogues path in old memo")
    if old_tool.sha256.lower() != wanted_manifest:
        raise RepairPreconditionError("unexpected tool-analogues digest in old memo")
    seen: dict[tuple[str, str], None] = {}
    for ref in old_memo.evidence:
        if ref.id == "tool-analogues":
            continue
        if not _is_hex64(ref.sha256):
            raise RepairPreconditionError(f"invalid old evidence hash: {ref.id}")
        key = (ref.local_relative_path.as_posix(), ref.sha256.lower())
        if key in seen:
            continue
        target = checked_local_path(data_root, ref.local_relative_path)
        if target.is_symlink() or not target.is_file():
            raise RepairPreconditionError(f"missing old source: {ref.local_relative_path.as_posix()}")
        if hashlib.sha256(target.read_bytes()).hexdigest() != ref.sha256.lower():
            raise RepairPreconditionError(f"hash mismatch for old source: {ref.local_relative_path.as_posix()}")
        seen[key] = None

    lake = LocalLake(data_root, _read_import_manifests(data_root))  # type: ignore[arg-type]
    financial = FinancialEvidence(data_root, lake)
    event_store = EventStore(catalog)
    from src.data.index_store import IndexStore

    index_store = IndexStore(catalog, data_root, manifest)
    try:
        context = build_research_context(
            catalog,
            event_store,
            lake,
            financial,
            index_store,
            old_memo.anchor_rcept_no,
            old_memo.as_of,
            StudyPolicy(),
        )
    except ResearchUnavailable as exc:
        raise RepairPreconditionError(f"replay unavailable: {exc}") from exc
    except (ValueError, OSError) as exc:
        raise RepairPreconditionError(f"replay failed: {exc}") from exc
    if context.event.event_id != old_memo.event_id:
        raise RepairPreconditionError("replayed event identity drift")
    if context.active_rcept_no != old_memo.active_rcept_no:
        raise RepairPreconditionError("replayed active receipt drift")
    if context.as_of != old_memo.as_of:
        raise RepairPreconditionError("replayed as-of drift")
    try:
        proof: AnalogueProof | None = build_analogue_proof(context)
    except (ValueError, OSError) as exc:
        raise RepairPreconditionError(f"proof unavailable: {exc}") from exc
    if proof is None:
        raise RepairPreconditionError("replayed analogue proof is unavailable")

    new_run_id = f"{old_run_id}-evidence-v2"
    relative_dir = PurePosixPath(f"reports/{context.event.event_id}/{new_run_id}")
    analogue_ref = proof_reference(data_root / relative_dir.as_posix(), proof)
    new_memo = build_baseline_memo(context, analogue_ref=analogue_ref)

    if new_memo.event_id != old_memo.event_id:
        raise RepairPreconditionError("replayed event identity drift")
    if new_memo.anchor_rcept_no != old_memo.anchor_rcept_no or new_memo.active_rcept_no != old_memo.active_rcept_no:
        raise RepairPreconditionError("replayed receipt drift")
    if new_memo.as_of != old_memo.as_of:
        raise RepairPreconditionError("replayed as-of drift")
    if dict(new_memo.facts) != dict(old_memo.facts):
        raise RepairPreconditionError("replayed event fact drift")
    if dict(new_memo.metrics) != dict(old_memo.metrics):
        raise RepairPreconditionError("replayed metric drift")
    if tuple(new_memo.statuses) != tuple(old_memo.statuses):
        raise RepairPreconditionError("replayed status drift")
    if tuple(new_memo.claims) != tuple(old_memo.claims):
        raise RepairPreconditionError("replayed claim drift")
    new_by_id = {ref.id: ref for ref in new_memo.evidence}
    if set(new_by_id) != set(by_id):
        raise RepairPreconditionError("replayed evidence identity drift")
    for ref_id, old_ref in by_id.items():
        new_ref = new_by_id[ref_id]
        if ref_id == "tool-analogues":
            if new_ref.sha256.lower() != proof.sha256.lower():
                raise RepairPreconditionError("replayed proof citation drift")
            if new_ref.local_relative_path != relative_dir / "analogue-proof.json":
                raise RepairPreconditionError("replayed proof citation drift")
            continue
        if (
            new_ref.source_kind != old_ref.source_kind
            or new_ref.local_relative_path != old_ref.local_relative_path
            or new_ref.sha256.lower() != old_ref.sha256.lower()
            or new_ref.locator != old_ref.locator
        ):
            raise RepairPreconditionError(f"replayed evidence drift: {ref_id}")

    new_manifest = {
        "code_revision": CODE_REVISION,
        "event_id": context.event.event_id,
        "index_manifest_hash": manifest.manifest_hash,
        "manifest_hash": new_memo.manifest_hash,
        "proof_sha256": proof.sha256,
        "run_id": new_run_id,
        "supersedes_manifest_sha256": old_manifest_hash,
        "supersedes_run_id": old_run_id,
    }
    try:
        published = publish_research_run(data_root, new_run_id, new_memo, proof, new_manifest)
    except ValueError as exc:
        if "conflicting research run" in str(exc):
            raise ResearchRunConflictError(str(exc)) from exc
        raise RepairPreconditionError(str(exc)) from exc

    for name, digest in before.items():
        current = report_dir / name
        if current.is_symlink() or not current.is_file() or hashlib.sha256(current.read_bytes()).hexdigest() != digest:
            raise RepairPreconditionError("old run bytes changed during repair")
    return RepairSummary(
        old_run_id=old_run_id,
        new_run_id=published.run_id,
        manifest_hash=published.manifest_hash,
        proof_sha256=proof.sha256,
    )


__all__ = [
    "RepairPreconditionError",
    "RepairSummary",
    "ResearchRunConflictError",
    "repair_memo_evidence",
]
