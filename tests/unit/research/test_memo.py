"""Invariant guards for the deterministic cited memo baseline."""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import PurePosixPath
from zoneinfo import ZoneInfo

import pytest

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


@pytest.mark.parametrize(
    ("value", "signed", "expected"),
    [
        (Decimal("0.036415"), True, "+3.64%"),
        (Decimal("-0.0205"), True, "-2.05%"),
        (Decimal("0"), True, "+0.00%"),
        (Decimal("0.0000512219"), False, "0.0051%"),
        (Decimal("0.0329504"), False, "3.30%"),
    ],
)
def test_percent_display_never_rounds_small_nonzero_to_zero(value: Decimal, signed: bool, expected: str) -> None:
    from src.research.memo import _pct

    assert _pct(value, signed=signed) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal(159_957_600), "1.6억원"),
        (Decimal(40_004_340_000_000), "40.00조원"),
        (Decimal(9_500_000), "9,500,000원"),
    ],
)
def test_krw_display_scales_by_magnitude(value: Decimal, expected: str) -> None:
    from src.research.memo import _krw

    assert _krw(value) == expected


def test_markdown_leads_with_summary_and_keeps_exact_values_in_audit_trail() -> None:
    memo = build_baseline_memo(_context(study=_study(Decimal("-0.0205468634105692"))))
    markdown = render_markdown(memo)
    assert markdown.index("## Summary") < markdown.index("## Audit trail")
    summary = markdown.split("## Audit trail")[0]
    assert "-2.05%" in summary
    assert "-0.0205468634105692" not in summary
    assert "-0.0205468634105692" in markdown.split("## Audit trail")[1]
    assert "CONFOUND_CHECK_INCOMPLETE: concurrent-disclosure coverage is not fully verified" in summary


def test_concurrent_disclosures_are_listed_chronologically_with_receipts() -> None:
    import dataclasses

    from src.research.memo import MemoClaim

    base = build_baseline_memo(_context())
    claims = (
        MemoClaim("concurrent_disclosure", "Issuer filed 배당결정 under receipt 20240703000004 in the event study window.", ("e",), None),
        MemoClaim("concurrent_disclosure", "Issuer filed 잠정실적 under receipt 20240628000001 in the event study window.", ("e",), None),
        MemoClaim("concurrent_disclosure", "unparseable claim text", ("e",), None),
    )
    memo = dataclasses.replace(base, claims=claims)
    section = render_markdown(memo).split("## Concurrent disclosures (3)")[1].split("## Audit trail")[0]
    lines = [line for line in section.splitlines() if line.startswith("- ")]
    assert lines == [
        "- unparseable claim text",
        "- 2024-06-28 잠정실적 (20240628000001)",
        "- 2024-07-03 배당결정 (20240703000004)",
    ]


def test_counts_display_as_plain_numbers() -> None:
    import dataclasses

    base = build_baseline_memo(_context())
    memo = dataclasses.replace(base, metrics={**base.metrics, "analogue_outcome_count": "8"})
    assert "n=8," in render_markdown(memo).split("## Audit trail")[0]


def _sheet_facts(available: datetime = datetime(2024, 5, 20, tzinfo=KST)) -> tuple[object, ...]:
    from src.data.financial_evidence import VerifiedFinancialFact

    values = {"assets": "10000000000", "cash": "4000000000", "debt": "2000000000", "equity": "8000000000"}
    return tuple(
        VerifiedFinancialFact(
            corp_code="01386916", filing_id="F1", fact=name, fiscal_period="2024Q1", consolidated=True,
            value=Decimal(value), unit="KRW", available_at=available, source_hash=name[0] * 64, evidence_key=f"key-{name}",
        )
        for name, value in values.items()
    )


def test_financial_snapshot_adds_cited_facts_ratios_and_summary() -> None:
    import dataclasses

    context = dataclasses.replace(_context(), financial_facts=_sheet_facts())  # type: ignore[arg-type]
    memo = build_baseline_memo(context)
    assert memo.facts["financials_period"] == "2024Q1"
    assert memo.facts["financials_basis"] == "consolidated"
    assert memo.facts["fin_liabilities"] == "2000000000"
    assert memo.metrics["amount_to_cash"] == str(Decimal("1000000000") / Decimal("4000000000"))
    assert memo.metrics["liabilities_to_equity"] == str(Decimal("2000000000") / Decimal("8000000000"))
    assert "FINANCIALS_UNAVAILABLE" not in memo.statuses
    by_id = {ref.id: ref for ref in memo.evidence}
    assert by_id["fin-cash"].sha256 == "c" * 64
    assert by_id["fin-cash"].local_relative_path == PurePosixPath("imports/financial_evidence") / ("c" * 64) / "payload.json"
    cash_ratio = next(claim for claim in memo.claims if claim.metric_key == "amount_to_cash")
    assert set(cash_ratio.evidence_ids) == {"fin-cash", "filing-fact-acq_ostk_prc"}
    summary = render_markdown(memo).split("## Audit trail")[0]
    assert "Balance sheet at filing (2024Q1, consolidated): cash 40.0억원, equity 80.0억원, liabilities/equity 25.00%" in summary
    assert "Buyback size: 25.00% of cash, 12.50% of equity" in summary


def test_financials_after_filing_knowledge_are_not_used_and_status_is_explicit() -> None:
    import dataclasses

    late = datetime(2024, 7, 1, tzinfo=KST)
    context = dataclasses.replace(_context(), financial_facts=_sheet_facts(available=late))  # type: ignore[arg-type]
    memo = build_baseline_memo(context)
    assert "FINANCIALS_UNAVAILABLE" in memo.statuses
    assert memo.facts["fin_cash"] is None
    assert memo.metrics["amount_to_cash"] is None
    assert not any(claim.kind.startswith("financial") for claim in memo.claims)
    assert "no complete KRW balance sheet" in render_markdown(memo)


def _limit_fact(text: str) -> BuybackFact:
    return BuybackFact("BUY_OSTK_LMT", None, text, "shares", _location("20240620000001", "BUY_OSTK_LMT"), "UNVERIFIED")


@pytest.mark.parametrize(("cell", "flagged"), [("-", False), ("abc", True)])
def test_dash_daily_limit_is_stated_absence_but_other_unparsed_values_stay_flagged(cell: str, flagged: bool) -> None:
    import dataclasses

    base = _parsed()
    parsed = dataclasses.replace(base, facts=(*base.facts, _limit_fact(cell)))
    memo = build_baseline_memo(_context(parsed=parsed))
    assert memo.facts["daily_limit_shares"] is None
    assert ("UNVERIFIED" in memo.statuses) is flagged


def test_company_name_is_cited_and_leads_the_title() -> None:
    import dataclasses

    digest = "e" * 64
    path = PurePosixPath("raw/imported/security_master") / digest / "payload.json"
    context = _context()
    context = dataclasses.replace(
        context, company_name="테스트전자", company_name_source_hash=digest, artifact_paths={**context.artifact_paths, digest: path}
    )
    memo = build_baseline_memo(context)
    assert memo.facts["company_name"] == "테스트전자"
    ref = next(item for item in memo.evidence if item.id == "security-master")
    assert (ref.local_relative_path, ref.sha256) == (path, digest)
    assert render_markdown(memo).splitlines()[0].startswith("# Buyback memo: 테스트전자 (000001,")


def test_unresolvable_company_name_falls_back_to_stock_code() -> None:
    import dataclasses

    context = dataclasses.replace(_context(), company_name="테스트전자", company_name_source_hash="e" * 64)
    memo = build_baseline_memo(context)
    assert memo.facts["company_name"] is None
    assert all(item.id != "security-master" for item in memo.evidence)
    assert render_markdown(memo).splitlines()[0].startswith("# Buyback memo: 000001 (000001,")


def _display_memo() -> object:
    from src.research.memo import ResearchMemo

    return ResearchMemo(
        event_id="buyback:01386916:20240620000001",
        anchor_rcept_no="20240620000001",
        active_rcept_no="20240620000001",
        as_of=AS_OF,
        facts={
            "planned_amount_krw": "2999920000",
            "planned_shares": "10000",
            "daily_limit_shares": None,
            "fin_assets": "10000000000",
            "fin_cash": "11468450971",
            "fin_equity": "8000000000",
            "fin_liabilities": "2000000000",
            "receipt_date": "2024-06-20",
            "period_begin": "2024-06-21",
            "period_end": "2024-09-20",
        },
        metrics={
            "amount_to_cash": "0.2615802262734371504861186017",
            "amount_to_equity": "0.125",
            "analogue_median": None,
            "analogue_p25": "0.01",
            "analogue_p75": "0.03",
            "car_h1": "0.03",
            "model_alpha": "0.001",
            "model_beta": "0.9",
            "peer_count": "8",
        },
        claims=(),
        evidence=(),
        statuses=(),
        manifest_hash="0" * 64,
    )


def test_display_matches_rendered_summary() -> None:
    from src.research.memo import display_figures

    memo = _display_memo()  # type: ignore[arg-type]
    figures = display_figures(memo)
    assert figures["metric:amount_to_cash"] == "26.16%"
    assert "26.16%" in render_markdown(memo)  # type: ignore[arg-type]
    assert list(figures.keys()) == sorted(figures.keys())


def test_display_withheld_and_estimation_excluded() -> None:
    from src.research.memo import display_figures

    memo = _display_memo()  # type: ignore[arg-type]
    figures = display_figures(memo)
    assert "metric:analogue_median" not in figures
    assert "metric:model_alpha" not in figures
    assert "metric:model_beta" not in figures
    assert figures["metric:analogue_p25"] == "+1.00%"
    assert figures["metric:peer_count"] == "8"


def test_display_krw_scale_and_determinism() -> None:
    from src.research.memo import display_figures

    memo = _display_memo()  # type: ignore[arg-type]
    figures = display_figures(memo)
    assert figures["fact:fin_cash"] == "114.7억원"
    assert figures["fact:planned_amount_krw"] == "30.0억원"
    assert figures["fact:receipt_date"] == "2024-06-20"
    assert "fact:daily_limit_shares" not in figures
    again = display_figures(memo)
    assert figures == again
    assert list(figures.keys()) == list(again.keys())
