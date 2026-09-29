"""Invariant guards for content-addressed analogue proofs."""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import PurePosixPath
from zoneinfo import ZoneInfo

import pytest

from src.core.revisions import EventLink
from src.data.catalog import FilingVersion
from src.data.local_lake import SecurityMatch
from src.research.analogue_proof import AnalogueEvidenceUnavailable, build_analogue_proof
from src.research.comparables import AnalogueObservation, ComparableSet
from src.research.context import ResearchContext
from src.research.event_study import StudyPolicy, StudyResult
from src.research.materiality import MaterialityResult

KST = ZoneInfo("Asia/Seoul")
AS_OF = datetime(2024, 6, 28, 18, 0, tzinfo=KST)
POLICY = StudyPolicy(estimation_start=-10, estimation_end=-2, min_pairs=5, horizons=(1,))

_MANIFEST = "ab" * 32


def _tag(number: int) -> str:
    return format(number + 100, "064x")


def _observations(count: int, start: int = 1) -> tuple[AnalogueObservation, ...]:
    return tuple(
        AnalogueObservation(
            f"EVT:{number:04d}",
            date(2024, 6, 10),
            datetime(2024, 6, 20, 18, 0, tzinfo=KST),
            Decimal(f"0.0{number}"),
            (_tag(number), _tag(number + 1000), _tag(number + 2000), _tag(number + 3000)),
        )
        for number in range(start, start + count)
    )


def _context(
    observations: tuple[AnalogueObservation, ...],
    quantiles: dict[str, Decimal | None] | None = None,
    drop_hash: str | None = None,
    manifest: str = _MANIFEST,
    artifact_order: bool = False,
) -> ResearchContext:
    excess = tuple(obs.intraday_excess for obs in observations)
    if quantiles is None:
        quantiles = {"p25": Decimal("0.02"), "median": Decimal("0.045"), "p75": Decimal("0.06")}
    selection = sorted({h for obs in observations for h in obs.source_hashes} | {_tag(1)})
    paths: dict[str, PurePosixPath] = {}
    items = sorted(selection) if not artifact_order else sorted(selection, reverse=True)
    for digest in items:
        if digest == drop_hash:
            continue
        paths[digest] = PurePosixPath(f"raw/test/{digest[:8]}.bin")
    comparables = ComparableSet(
        event_id="buyback:target",
        as_of=AS_OF,
        feature_end_session=date(2024, 6, 24),
        peer_ids=("KRX:000002",),
        analogue_event_ids=tuple(obs.event_id for obs in observations),
        analogue_intraday_excess=excess,
        analogue_quantiles=quantiles,
        exclusions={"EVT:0099": "OUTSIDE_QUINTILE"},
        status="OK",
        analogue_observations=observations,
        selection_source_hashes=tuple(sorted(selection)),
    )
    filing = FilingVersion(
        rcept_no="20240624000001",
        corp_code="01386916",
        receipt_date=date(2024, 6, 24),
        report_name="주요사항보고서(자기주식취득결정)",
        stock_code="000001",
        raw_hash="aa" * 32,
        first_observed_at=datetime(2024, 6, 24, 9, 0, tzinfo=KST),
        knowledge_available_at=datetime(2024, 6, 24, 18, 0, tzinfo=KST),
        availability_mode="HISTORICAL_BACKFILL",
        correction_flag=False,
        withdrawal_flag=False,
        parent_rcept_no=None,
        link_status="ORIGINAL",
        time_precision="DATE_ONLY",
    )
    study = StudyResult(
        event_id="buyback:target",
        active_rcept_no="20240624000001",
        as_of=AS_OF,
        first_safe_session=date(2024, 6, 26),
        intraday_excess=None,
        model_alpha=None,
        model_beta=None,
        horizon_car={1: None},
        status="OK",
        reasons=(),
        evidence_hashes=(),
        policy_version=POLICY.version,
        omitted_sessions=0,
    )
    return ResearchContext(
        anchor_rcept_no="20240624000001",
        active_rcept_no="20240624000001",
        event=EventLink("buyback:target", ("20240624000001",), "LINKED"),
        filing=filing,
        parsed=None,  # type: ignore[arg-type]
        security=SecurityMatch("KRX:000001", "000001", "KOSPI", "KR7000001001", date(2024, 6, 23), "OK"),
        financial_facts=(),
        stock_bars=(),
        index_bars=(),
        materiality=MaterialityResult(None, None, (), "OK"),
        study=study,
        comparables=comparables,
        as_of=AS_OF,
        index_manifest_hash=manifest,
        snapshot_ids=(),
        source_hashes=tuple(sorted(paths)),
        artifact_paths=paths,
        confounding_receipts=(),
    )


def test_canonical_replay_is_byte_identical() -> None:
    """Same context in different mapping orders yields identical payload bytes and SHA."""
    observations = _observations(8)
    first = _context(observations)
    second = _context(observations, artifact_order=True)
    first_proof = build_analogue_proof(first)
    second_proof = build_analogue_proof(second)
    assert first_proof is not None
    assert second_proof is not None
    assert first_proof.payload == second_proof.payload
    assert first_proof.sha256 == second_proof.sha256
    document = json.loads(first_proof.payload.decode("utf-8"))
    assert document["quantiles"]["median"] == "0.045"
    assert {"local_path": f"krx/manifests/{_MANIFEST}.json", "sha256": _MANIFEST} in document["inputs"]
    assert _MANIFEST in first_proof.input_hashes


def test_exact_decimal_distribution_reproduces_memo_values() -> None:
    """Eight paired outcomes reproduce memo quantiles from Decimal strings without floats."""
    observations = _observations(8)
    proof = build_analogue_proof(_context(observations))
    assert proof is not None
    document = json.loads(proof.payload.decode("utf-8"))
    assert [item["event_id"] for item in document["observations"]] == [f"EVT:{number:04d}" for number in range(1, 9)]
    assert [item["intraday_excess"] for item in document["observations"]] == [f"0.0{number}" for number in range(1, 9)]
    assert document["quantiles"] == {"median": "0.045", "p25": "0.02", "p75": "0.06"}
    assert all(isinstance(item["intraday_excess"], str) for item in document["observations"])
    assert "0.045" in proof.payload.decode("utf-8")


def test_missing_input_path_raises_without_proof() -> None:
    """One selected outcome hash without a local path raises instead of returning a proof."""
    observations = _observations(8)
    missing = observations[0].source_hashes[0]
    context = _context(observations, drop_hash=missing)
    with pytest.raises(AnalogueEvidenceUnavailable):
        build_analogue_proof(context)


def test_low_sample_returns_no_proof() -> None:
    """Fewer than five observations or missing quantiles produce no quantile proof."""
    assert build_analogue_proof(_context(_observations(4))) is None
    assert build_analogue_proof(_context(_observations(8), {"p25": None, "median": None, "p75": None})) is None


def test_odd_count_median_uses_middle_observation() -> None:
    """Seven paired outcomes resolve the median to the middle Decimal without floats."""
    observations = _observations(7)
    context = _context(
        observations, {"p25": Decimal("0.02"), "median": Decimal("0.04"), "p75": Decimal("0.06")}
    )
    proof = build_analogue_proof(context)
    assert proof is not None
    document = json.loads(proof.payload.decode("utf-8"))
    assert document["quantiles"]["median"] == "0.04"


def test_quantile_mismatch_raises() -> None:
    """Copied metric text that does not match recomputed quantiles is rejected."""
    observations = _observations(8)
    context = _context(observations, {"p25": Decimal("0.99"), "median": Decimal("0.045"), "p75": Decimal("0.06")})
    with pytest.raises(AnalogueEvidenceUnavailable):
        build_analogue_proof(context)


def test_invalid_manifest_raises() -> None:
    """A malformed pinned manifest identity cannot produce a proof."""
    with pytest.raises(AnalogueEvidenceUnavailable):
        build_analogue_proof(_context(_observations(8), manifest="not-a-hash"))
