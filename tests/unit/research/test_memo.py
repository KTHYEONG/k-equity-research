"""Invariant guards for the deterministic cited memo baseline."""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import PurePosixPath
from zoneinfo import ZoneInfo

from src.core.buyback_document import BuybackFact, EvidenceLocation, ParsedBuyback
from src.core.revisions import EventLink
from src.data.catalog import FilingVersion
from src.data.local_lake import SecurityMatch
from src.research.comparables import ComparableSet
from src.research.context import ResearchContext
from src.research.event_study import StudyPolicy, StudyResult
from src.research.materiality import MaterialityResult
from src.research.memo import build_baseline_memo, memo_to_dict, render_markdown

KST = ZoneInfo("Asia/Seoul")
AS_OF = datetime(2024, 6, 24, 18, 0, tzinfo=KST)
DOC_HASH = "d" * 64
RAW_HASH = "a" * 64
ZIP_PATH = PurePosixPath("raw/dart/20240620000001.zip")
POLICY = StudyPolicy(estimation_start=-8, estimation_end=-2, min_pairs=3, horizons=(1,))


def _location(rcept_no: str, key: str) -> EvidenceLocation:
    return EvidenceLocation(rcept_no, DOC_HASH, "report.xml", "ACODE", key, "s", "TBL_ACQ_STK", "c")


def _fact(field: str, value: Decimal, unit: str, status: str = "VERIFIED") -> BuybackFact:
    return BuybackFact(field, value, str(value), unit, _location("20240620000001", field), status)  # type: ignore[arg-type]


def _filing() -> FilingVersion:
    return FilingVersion(
        rcept_no="20240620000001",
        corp_code="01386916",
        receipt_date=date(2024, 6, 20),
        report_name="report",
        stock_code="000001",
        raw_hash=RAW_HASH,
        first_observed_at=datetime(2024, 6, 20, 9, 0, tzinfo=KST),
        knowledge_available_at=datetime(2024, 6, 20, 18, 0, tzinfo=KST),
        availability_mode="HISTORICAL_BACKFILL",
        correction_flag=False,
        withdrawal_flag=False,
        parent_rcept_no=None,
        link_status="ORIGINAL",
        time_precision="DATE_ONLY",
    )


def _parsed(amount_status: str = "VERIFIED") -> ParsedBuyback:
    return ParsedBuyback(
        rcept_no="20240620000001",
        corp_code="01386916",
        first_submission_date=date(2024, 6, 20),
        facts=(
            _fact("ACQ_OSTK_PRC", Decimal(1_000_000_000), "KRW", amount_status),
            _fact("ACQ_OSTK", Decimal(10_000), "shares"),
        ),
        document_hash=DOC_HASH,
        parse_status="OK",
    )


def _study(intraday: Decimal | None = Decimal("0.01")) -> StudyResult:
    return StudyResult(
        event_id="buyback:01386916:20240620000001",
        active_rcept_no="20240620000001",
        as_of=AS_OF,
        first_safe_session=date(2024, 6, 21),
        intraday_excess=intraday,
        model_alpha=None,
        model_beta=None,
        horizon_car={1: None},
        status="CONFOUND_CHECK_INCOMPLETE" if intraday is not None else "PENDING",
        reasons=("CONFOUND_CHECK_INCOMPLETE",) if intraday is not None else ("PENDING",),
        evidence_hashes=("s" * 64,),
        policy_version=POLICY.version,
        omitted_sessions=0,
    )


def _comparables() -> ComparableSet:
    return ComparableSet(
        event_id="buyback:01386916:20240620000001",
        as_of=AS_OF,
        feature_end_session=date(2024, 6, 19),
        peer_ids=(),
        analogue_event_ids=(),
        analogue_intraday_excess=(),
        analogue_quantiles={"p25": None, "median": None, "p75": None},
        exclusions={},
        status="LOW_SAMPLE;EMPTY_PEERS;NO_ANALOGUES",
    )


def _context(
    parsed: ParsedBuyback | None = None,
    study: StudyResult | None = None,
    artifact_paths: dict[str, PurePosixPath] | None = None,
) -> ResearchContext:
    resolved = parsed if parsed is not None else _parsed()
    resolved_study = study if study is not None else _study()
    paths = artifact_paths if artifact_paths is not None else {DOC_HASH: ZIP_PATH, RAW_HASH: ZIP_PATH}
    return ResearchContext(
        anchor_rcept_no="20240620000001",
        active_rcept_no="20240620000001",
        event=EventLink("buyback:01386916:20240620000001", ("20240620000001",), "LINKED"),
        filing=_filing(),
        parsed=resolved,
        security=SecurityMatch("KRX:000001", "000001", "KOSPI", "KR7000001001", date(2024, 6, 19), "OK"),
        financial_facts=(),
        stock_bars=(),
        index_bars=(),
        materiality=MaterialityResult(
            Decimal("1") / Decimal(700) if resolved.facts[0].status == "VERIFIED" else None,
            Decimal("10_000") / Decimal(10_000_000),
            (DOC_HASH, "s" * 64) if resolved.facts[0].status == "VERIFIED" else (),
            "OK" if resolved.facts[0].status == "VERIFIED" else "AMOUNT_UNAVAILABLE",
        ),
        study=resolved_study,
        comparables=_comparables(),
        as_of=AS_OF,
        index_manifest_hash="m" * 64,
        snapshot_ids=("snap-1",),
        source_hashes=(DOC_HASH, RAW_HASH),
        artifact_paths=paths,
    )


def test_withholds_unverified_amount_with_visible_status() -> None:
    """An unverified amount stays null with an uncertainty status instead of prose."""
    memo = build_baseline_memo(_context(parsed=_parsed("UNVERIFIED")))
    assert memo.facts["planned_amount_krw"] is None
    assert memo.metrics["amount_to_market_cap"] is None
    assert "UNVERIFIED" in memo.statuses


def test_verified_correction_fact_links_to_local_zip_coordinate() -> None:
    """A verified correction fact cites its own receipt ZIP hash and coordinate."""
    memo = build_baseline_memo(_context())
    filing_claims = [claim for claim in memo.claims if claim.kind == "filing_fact"]
    assert filing_claims
    by_id = {item.id: item for item in memo.evidence}
    target = next(claim for claim in filing_claims if "ACQ_OSTK_PRC" in claim.text)
    ref = by_id[target.evidence_ids[0]]
    assert ref.sha256 == DOC_HASH
    assert ref.local_relative_path == ZIP_PATH
    assert "report.xml" in ref.locator
    assert "ACQ_OSTK_PRC" in ref.locator


def test_identical_inputs_render_identical_bytes() -> None:
    """Same context and manifest produce identical structured facts and Markdown."""
    first = build_baseline_memo(_context())
    second = build_baseline_memo(_context())
    assert first.manifest_hash == second.manifest_hash
    assert json.dumps(memo_to_dict(first), sort_keys=True) == json.dumps(memo_to_dict(second), sort_keys=True)
    assert render_markdown(first) == render_markdown(second)
    assert render_markdown(first).encode("utf-8") == render_markdown(second).encode("utf-8")


def test_negative_path_describes_observation_without_attribution() -> None:
    """A negative intraday excess is described as observed movement only."""
    memo = build_baseline_memo(_context(study=_study(Decimal("-0.0205"))))
    markdown = render_markdown(memo)
    assert "-0.0205" in markdown
    lowered = markdown.lower()
    assert "observed" in lowered
    assert "recommend" not in lowered
    assert "causal" not in lowered
    assert "effect" not in lowered


def test_naive_instant_rejected_before_reads() -> None:
    """A naive as-of instant fails before any memo assembly."""
    import dataclasses

    import pytest

    naive = dataclasses.replace(_context(), as_of=datetime(2024, 6, 24, 18, 0))
    with pytest.raises(ValueError, match="timezone-aware"):
        build_baseline_memo(naive)


def test_missing_local_artifact_withholds_claims() -> None:
    """Without a resolvable local ZIP path no filing claim is published."""
    memo = build_baseline_memo(_context(artifact_paths={}))
    assert memo.facts["planned_amount_krw"] is None
    assert "UNRESOLVED_LINK" in memo.statuses
    assert all(claim.kind != "filing_fact" for claim in memo.claims)


def test_rich_tool_metrics_cover_uncertainty_branches() -> None:
    """CAR, analogue quantiles and mixed study reasons render with explicit statuses."""
    import dataclasses

    from src.research.memo import EvidenceRef

    base = _context()
    text_fact = BuybackFact(
        "ACQ_PPS", None, "purpose", None, _location("20240620000001", "ACQ_PPS"), "VERIFIED"
    )
    empty_fact = BuybackFact("BUY_OSTK_LMT", None, None, "shares", _location("20240620000001", "BUY_OSTK_LMT"), "VERIFIED")
    parsed = dataclasses.replace(base.parsed, facts=(*base.parsed.facts, text_fact, empty_fact))
    study = dataclasses.replace(
        base.study,
        horizon_car={1: Decimal("0.03")},
        reasons=("TIME_AMBIGUOUS", "PENDING_H1", "NOT_ESTIMABLE_H1", "CONFOUNDED", "CORPORATE_ACTION_BREAK_H1", "CUSTOM"),
    )
    comparables = dataclasses.replace(
        base.comparables,
        analogue_intraday_excess=(Decimal("0.01"),),
        analogue_quantiles={"p25": Decimal("0.01"), "median": Decimal("0.02"), "p75": Decimal("0.03")},
    )
    event = EventLink(base.event.event_id, base.event.rcept_nos, "UNRESOLVED_LINK")
    context = dataclasses.replace(base, parsed=parsed, study=study, comparables=comparables, event=event)
    analogue_ref = EvidenceRef(
        "tool-analogues", "tool_result", PurePosixPath("reports/evt/run-1/analogue-proof.json"), "ab" * 32, "proof"
    )
    memo = build_baseline_memo(context, analogue_ref=analogue_ref)
    assert memo.facts["purpose_text"] == "purpose"
    assert memo.facts["daily_limit_shares"] is None
    assert memo.metrics["car_h1"] == "0.03"
    assert memo.metrics["analogue_median"] == "0.02"
    assert "TIME_AMBIGUOUS" in memo.statuses
    assert "NOT_ESTIMABLE" in memo.statuses
    assert "UNRESOLVED_LINK" in memo.statuses
    assert "CONFOUNDED" in memo.statuses
    assert "CUSTOM" in memo.statuses


def test_empty_memo_renders_withheld_sections() -> None:
    """A memo without claims, evidence or statuses renders explicit withheld markers."""
    from src.research.memo import ResearchMemo

    memo = ResearchMemo(
        event_id="buyback:01386916:20240620000001",
        anchor_rcept_no="20240620000001",
        active_rcept_no="20240620000001",
        as_of=AS_OF,
        facts={},
        metrics={},
        claims=(),
        evidence=(),
        statuses=(),
        manifest_hash="0" * 64,
    )
    markdown = render_markdown(memo)
    assert "no verified claim" in markdown
    assert "no resolvable source" in markdown
    assert "- OK" in markdown


def _proved_context() -> ResearchContext:
    """Context with five paired analogue observations backed by local test hashes."""
    import dataclasses

    from src.research.comparables import AnalogueObservation

    base = _context()
    observations = tuple(
        AnalogueObservation(
            f"EVT:{number:04d}",
            date(2024, 6, 10),
            datetime(2024, 6, 20, 18, 0, tzinfo=KST),
            Decimal(f"0.0{number}"),
            (format(number, "064x"), format(number + 1000, "064x")),
        )
        for number in range(1, 6)
    )
    hashes = sorted({digest for observation in observations for digest in observation.source_hashes})
    comparables = dataclasses.replace(
        base.comparables,
        analogue_event_ids=tuple(observation.event_id for observation in observations),
        analogue_intraday_excess=tuple(observation.intraday_excess for observation in observations),
        analogue_quantiles={"p25": Decimal("0.02"), "median": Decimal("0.03"), "p75": Decimal("0.04")},
        exclusions={},
        status="OK",
        analogue_observations=observations,
        selection_source_hashes=tuple(hashes),
    )
    paths = {digest: PurePosixPath(f"raw/test/{digest[:8]}.bin") for digest in hashes}
    paths[DOC_HASH] = ZIP_PATH
    paths[RAW_HASH] = ZIP_PATH
    return dataclasses.replace(
        base, comparables=comparables, index_manifest_hash="ab" * 32, artifact_paths=paths
    )


def test_verified_proof_citation_keeps_quantiles() -> None:
    """With verified proof, tool-analogues names the run-local proof with identical quantiles."""
    from pathlib import Path

    from src.research.analogue_proof import build_analogue_proof
    from src.research.publication import proof_reference

    context = _proved_context()
    proof = build_analogue_proof(context)
    assert proof is not None
    ref = proof_reference(Path("reports/evt/run-1"), proof)
    memo = build_baseline_memo(context, analogue_ref=ref)
    by_id = {item.id: item for item in memo.evidence}
    assert by_id["tool-analogues"].local_relative_path == ref.local_relative_path
    assert by_id["tool-analogues"].sha256 == proof.sha256
    assert memo.metrics["analogue_p25"] == "0.02"
    assert memo.metrics["analogue_median"] == "0.03"
    assert memo.metrics["analogue_p75"] == "0.04"
    assert any(claim.evidence_ids == ("tool-analogues",) for claim in memo.claims)


def test_absent_proof_withholds_analogue_claim() -> None:
    """Without proof no analogue claim or quantile is emitted and the status is explicit."""
    memo = build_baseline_memo(_proved_context())
    assert memo.metrics["analogue_p25"] is None
    assert memo.metrics["analogue_median"] is None
    assert memo.metrics["analogue_p75"] is None
    assert "tool-analogues" not in {item.id for item in memo.evidence}
    assert all("tool-analogues" not in claim.evidence_ids for claim in memo.claims)
    assert "ANALOGUE_EVIDENCE_UNVERIFIED" in memo.statuses
    assert memo.facts["planned_amount_krw"] == "1000000000"


def test_proof_digest_change_shifts_manifest() -> None:
    """Changing only the proof digest changes the memo manifest hash."""
    from src.research.memo import EvidenceRef

    context = _proved_context()
    first = build_baseline_memo(
        context,
        analogue_ref=EvidenceRef(
            "tool-analogues", "tool_result", PurePosixPath("reports/evt/run-1/analogue-proof.json"),
            "ab" * 32, "proof",
        ),
    )
    second = build_baseline_memo(
        context,
        analogue_ref=EvidenceRef(
            "tool-analogues", "tool_result", PurePosixPath("reports/evt/run-1/analogue-proof.json"),
            "cd" * 32, "proof",
        ),
    )
    assert first.manifest_hash != second.manifest_hash
    assert build_baseline_memo(context).manifest_hash != first.manifest_hash


def test_unresolvable_tool_hash_withholds_claim_keeps_metric() -> None:
    """A tool metric without a catalog path keeps its value but emits no citation."""
    import dataclasses

    base = _context()
    materiality = dataclasses.replace(base.materiality, source_hashes=("ff" * 32,))
    study = dataclasses.replace(base.study, horizon_car={1: Decimal("0.03")})
    paths = dict(base.artifact_paths)
    paths["s" * 64] = ZIP_PATH
    context = dataclasses.replace(base, materiality=materiality, study=study, artifact_paths=paths)
    memo = build_baseline_memo(context)
    assert memo.metrics["amount_to_market_cap"] is not None
    assert "tool-materiality" not in {item.id for item in memo.evidence}
    assert all(claim.metric_key != "amount_to_market_cap" for claim in memo.claims)
    assert "tool-intraday-s0" in {item.id for item in memo.evidence}
    assert "tool-car-h1" in {item.id for item in memo.evidence}
