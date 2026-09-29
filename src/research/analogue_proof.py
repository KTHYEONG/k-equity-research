"""Content-addressed proof for one point-in-time analogue distribution."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.research.context import ResearchContext

_PROOF_SCHEMA = "analogue-proof-v1"
_HEXDIGITS = frozenset("0123456789abcdefABCDEF")
_MIN_OUTCOMES = 5


class AnalogueEvidenceUnavailable(ValueError):  # noqa: N818 - spec-mandated boundary name
    """Required primary-source lineage for a reported analogue distribution is incomplete."""


@dataclass(frozen=True, slots=True)
class AnalogueProof:
    """Canonical, content-addressed calculation record for one point-in-time analogue distribution."""

    payload: bytes
    sha256: str
    input_hashes: tuple[str, ...]


def _is_hex64(value: str) -> bool:
    return len(value) == 64 and all(char in _HEXDIGITS for char in value)


def _nearest_rank(values: tuple[Decimal, ...], probability: Decimal) -> Decimal:
    ordered = sorted(values)
    raw_rank = int((probability * len(ordered)).to_integral_value(rounding="ROUND_CEILING"))
    rank = max(1, min(len(ordered), raw_rank))
    return ordered[rank - 1]


def _median_quantile(values: tuple[Decimal, ...]) -> Decimal:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / Decimal(2)


def _safe_path(value: PurePosixPath) -> bool:
    return not value.is_absolute() and all(part not in ("", ".", "..") for part in value.parts) and len(value.parts) > 0


def build_analogue_proof(context: ResearchContext) -> AnalogueProof | None:
    """Freeze the comparable selection, paired outcomes, quantiles, policy revision, and exact local input hashes into deterministic JSON; return None when no quantile is reportable."""
    comparables = context.comparables
    observations = comparables.analogue_observations
    quantiles = comparables.analogue_quantiles
    if len(observations) < _MIN_OUTCOMES:
        return None
    if quantiles.get("p25") is None or quantiles.get("median") is None or quantiles.get("p75") is None:
        return None
    paired_excess = tuple(obs.intraday_excess for obs in observations)
    pairing_ok = (
        len(paired_excess) == len(comparables.analogue_intraday_excess)
        and all(first == second for first, second in zip(paired_excess, comparables.analogue_intraday_excess, strict=True))
    )
    recomputed = {
        "p25": _nearest_rank(paired_excess, Decimal("0.25")),
        "median": _median_quantile(paired_excess),
        "p75": _nearest_rank(paired_excess, Decimal("0.75")),
    }
    quantiles_ok = (
        recomputed["p25"] == quantiles.get("p25")
        and recomputed["median"] == quantiles.get("median")
        and recomputed["p75"] == quantiles.get("p75")
    )
    if not pairing_ok or not quantiles_ok:
        raise AnalogueEvidenceUnavailable("quantile mismatch for paired analogue observations")
    manifest_hash = context.index_manifest_hash.lower()
    if not _is_hex64(manifest_hash):
        raise AnalogueEvidenceUnavailable("invalid index manifest identity")
    required: set[str] = set(comparables.selection_source_hashes)
    for obs in observations:
        required.update(obs.source_hashes)
    inputs: list[dict[str, str]] = []
    for digest in sorted(required):
        path = context.artifact_paths.get(digest)
        if not _is_hex64(digest) or path is None or not _safe_path(path):
            raise AnalogueEvidenceUnavailable(f"missing local path for input {digest}")
        inputs.append({"local_path": path.as_posix(), "sha256": digest.lower()})
    payload = {
        "analogue_event_ids": list(comparables.analogue_event_ids),
        "as_of": context.as_of.isoformat(),
        "event_id": context.event.event_id,
        "exclusions": {key: comparables.exclusions[key] for key in sorted(comparables.exclusions)},
        "index_manifest_hash": manifest_hash,
        "index_manifest_path": f"krx/manifests/{manifest_hash}.json",
        "inputs": inputs,
        "observations": [
            {
                "event_id": obs.event_id,
                "intraday_excess": str(obs.intraday_excess),
                "outcome_available_at": obs.outcome_available_at.isoformat(),
                "receipt_date": obs.receipt_date.isoformat(),
                "source_hashes": sorted(obs.source_hashes),
            }
            for obs in observations
        ],
        "policy_version": context.study.policy_version,
        "proof_schema": _PROOF_SCHEMA,
        "quantiles": {
            "median": str(quantiles.get("median")),
            "p25": str(quantiles.get("p25")),
            "p75": str(quantiles.get("p75")),
        },
    }
    raw = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    return AnalogueProof(payload=raw, sha256=hashlib.sha256(raw).hexdigest(), input_hashes=tuple(sorted(required)))


__all__ = ["AnalogueEvidenceUnavailable", "AnalogueProof", "build_analogue_proof"]
