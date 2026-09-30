"""Invariant guards for bounded read-only research tools."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import PurePosixPath
from zoneinfo import ZoneInfo

import pytest

from src.agent.tools import ToolRequest, execute_tool
from src.core.buyback_document import BuybackFact, EvidenceLocation, ParsedBuyback
from src.core.revisions import EventLink
from src.data.catalog import FilingVersion
from src.data.financial_evidence import VerifiedFinancialFact
from src.data.local_lake import SecurityMatch
from src.research.comparables import ComparableSet
from src.research.context import ResearchContext
from src.research.event_study import StudyPolicy, StudyResult
from src.research.materiality import MaterialityResult

KST = ZoneInfo("Asia/Seoul")
AS_OF = datetime(2024, 6, 24, 18, 0, tzinfo=KST)
DOC_HASH = "d" * 64
RAW_HASH = "a" * 64
ZIP_PATH = PurePosixPath("raw/dart/20240620000001.zip")
POLICY = StudyPolicy(estimation_start=-8, estimation_end=-2, min_pairs=3, horizons=(1,))


def _context() -> ResearchContext:
    location = EvidenceLocation("20240620000001", DOC_HASH, "report.xml", "ACODE", "ACQ_OSTK_PRC", "s", "t", "c")
    parsed = ParsedBuyback(
        rcept_no="20240620000001",
        corp_code="01386916",
        first_submission_date=date(2024, 6, 20),
        facts=(BuybackFact("ACQ_OSTK_PRC", Decimal(1_000_000_000), "1000000000", "KRW", location, "VERIFIED"),),
        document_hash=DOC_HASH,
        parse_status="OK",
    )
    filing = FilingVersion(
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
    financial = VerifiedFinancialFact(
        corp_code="01386916",
        filing_id="20240620000001",
        fact="assets",
        fiscal_period="2024Q1",
        consolidated=True,
        value=Decimal("100"),
        unit="KRW",
        available_at=datetime(2024, 6, 20, 18, 0, tzinfo=KST),
        source_hash="f" * 64,
        evidence_key="financial_evidence/" + "f" * 64 + "#1",
    )
    study = StudyResult(
        event_id="buyback:01386916:20240620000001",
        active_rcept_no="20240620000001",
        as_of=AS_OF,
        first_safe_session=date(2024, 6, 21),
        intraday_excess=Decimal("0.01"),
        model_alpha=None,
        model_beta=None,
        horizon_car={1: None},
        status="CONFOUND_CHECK_INCOMPLETE",
        reasons=("CONFOUND_CHECK_INCOMPLETE",),
        evidence_hashes=("s" * 64,),
        policy_version=POLICY.version,
        omitted_sessions=0,
    )
    comparables = ComparableSet(
        event_id="buyback:01386916:20240620000001",
        as_of=AS_OF,
        feature_end_session=date(2024, 6, 19),
        peer_ids=("KRX:000002",),
        analogue_event_ids=(),
        analogue_intraday_excess=(),
        analogue_quantiles={"p25": None, "median": None, "p75": None},
        exclusions={},
        status="LOW_SAMPLE",
    )
    return ResearchContext(
        anchor_rcept_no="20240620000001",
        active_rcept_no="20240620000001",
        event=EventLink("buyback:01386916:20240620000001", ("20240620000001",), "LINKED"),
        filing=filing,
        parsed=parsed,
        security=SecurityMatch("KRX:000001", "000001", "KOSPI", "KR7000001001", date(2024, 6, 19), "OK"),
        financial_facts=(financial,),
        stock_bars=(),
        index_bars=(),
        materiality=MaterialityResult(Decimal("1") / Decimal(700), None, (DOC_HASH,), "SHARES_UNAVAILABLE"),
        study=study,
        comparables=comparables,
        as_of=AS_OF,
        index_manifest_hash="m" * 64,
        snapshot_ids=("snap-1",),
        source_hashes=(DOC_HASH, RAW_HASH),
        artifact_paths={DOC_HASH: ZIP_PATH, RAW_HASH: ZIP_PATH},
    )


def test_future_market_window_returns_nothing() -> None:
    """A 2025 window against a fixed 2024 as-of fails before any bar is read."""
    request = ToolRequest("get_market_window", {"start": "2025-01-01", "end": "2025-01-02"})
    with pytest.raises(ValueError, match="beyond as_of"):
        execute_tool(request, _context())


def test_unknown_tool_name_fails_before_access() -> None:
    """A shell-style tool name is rejected before execution."""
    request = ToolRequest("shell", {})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unknown tool"):
        execute_tool(request, _context())


def test_revision_diff_keeps_original_and_correction_ids() -> None:
    """A revision diff attaches evidence IDs for both linked receipts."""
    import dataclasses

    context = dataclasses.replace(
        _context(),
        event=EventLink("buyback:01386916:20240620000001", ("20240620000001", "20240626000369"), "LINKED"),
    )
    result = execute_tool(ToolRequest("get_revision_diff", {}), context)
    assert result.as_of == AS_OF
    assert "20240620000001" in result.payload["rcept_nos"]  # type: ignore[index]
    assert any("20240620000001" in item for item in result.evidence_ids)
    assert any("20240626000369" in item for item in result.evidence_ids)


def test_event_tool_returns_fixed_asof_view() -> None:
    """A valid event read returns the fixed as-of linkage."""
    result = execute_tool(ToolRequest("get_event", {}), _context())
    assert result.payload["event_id"] == "buyback:01386916:20240620000001"
    assert result.as_of == AS_OF


def test_financial_tool_filters_to_named_fact() -> None:
    """A named financial fact returns only its verified row and key."""
    result = execute_tool(ToolRequest("get_financial_asof", {"fact": "assets"}), _context())
    assert result.payload["count"] == 1
    assert result.evidence_ids[0].startswith("financial_evidence/")


def test_market_tool_returns_bounded_empty_window() -> None:
    """An eligible past window returns deterministic counts without future bars."""
    result = execute_tool(ToolRequest("get_market_window", {"start": "2024-06-20", "end": "2024-06-21"}), _context())
    assert result.payload["stock_count"] == 0
    assert result.payload["index_count"] == 0


def test_peer_and_analogue_tools_return_deterministic_sets() -> None:
    """Peer and analogue reads return the validated selection verbatim."""
    peers = execute_tool(ToolRequest("get_peers", {}), _context())
    assert peers.payload["peer_ids"] == ["KRX:000002"]
    analogues = execute_tool(ToolRequest("get_analogues", {}), _context())
    assert analogues.payload["outcome_count"] == 0
    assert analogues.payload["quantiles"] == {"p25": None, "median": None, "p75": None}


def test_unknown_argument_name_rejected() -> None:
    """An unexpected argument such as a data-root choice fails validation."""
    request = ToolRequest("get_event", {"data_root": "elsewhere"})
    with pytest.raises(ValueError, match="unknown argument"):
        execute_tool(request, _context())


def test_path_like_and_nonstring_values_rejected() -> None:
    """Source-path values and non-string values never reach data access."""
    with pytest.raises(ValueError, match="invalid argument value"):
        execute_tool(ToolRequest("get_financial_asof", {"fact": "../escape"}), _context())
    with pytest.raises(ValueError, match="invalid argument value"):
        execute_tool(ToolRequest("get_financial_asof", {"fact": "a/b"}), _context())
    with pytest.raises(ValueError, match="invalid argument value"):
        execute_tool(ToolRequest("get_financial_asof", {"fact": 7}), _context())  # type: ignore[dict-item]


def test_malformed_window_arguments_rejected() -> None:
    """Missing, inverted or unparsable window bounds fail before reads."""
    with pytest.raises(ValueError, match="requires start and end"):
        execute_tool(ToolRequest("get_market_window", {"start": "2024-06-20"}), _context())
    with pytest.raises(ValueError, match="must not be after end"):
        execute_tool(ToolRequest("get_market_window", {"start": "2024-06-22", "end": "2024-06-21"}), _context())
    with pytest.raises(ValueError, match="isoformat"):
        execute_tool(ToolRequest("get_market_window", {"start": "someday", "end": "2024-06-21"}), _context())


def test_tool_specs_match_executable_contract() -> None:
    """Each tool with exactly its required arguments executes without unknown-argument errors."""
    from src.agent.tools import TOOL_SPECS

    context = _context()
    for name, spec in TOOL_SPECS.items():
        arguments = {arg.name: "2024-06-20" if arg.kind == "iso_date" else "assets" for arg in spec.args if arg.required}
        result = execute_tool(ToolRequest(name, arguments), context)  # type: ignore[arg-type]
        assert result.name == name
        with pytest.raises(ValueError, match="unknown argument"):
            execute_tool(ToolRequest(name, {**arguments, "unexpected": "x"}), context)  # type: ignore[arg-type]


def test_tool_set_is_closed() -> None:
    """TOOL_SPECS keys equal the six ToolName literal values exactly."""
    from src.agent.tools import TOOL_SPECS, ToolName
    from typing import get_args

    assert tuple(TOOL_SPECS.keys()) == get_args(ToolName)


def test_zero_argument_tools_stay_zero_argument() -> None:
    """Zero-argument tools declare no args and reject any argument."""
    from src.agent.tools import TOOL_SPECS

    for name in ("get_event", "get_peers", "get_analogues", "get_revision_diff"):
        assert TOOL_SPECS[name].args == ()  # type: ignore[literal-required]
        with pytest.raises(ValueError, match="unknown argument"):
            execute_tool(ToolRequest(name, {"event_id": "00127255"}), _context())  # type: ignore[arg-type]


def test_required_arguments_enforced() -> None:
    """get_market_window with start only requires both bounds."""
    with pytest.raises(ValueError, match="requires start and end"):
        execute_tool(ToolRequest("get_market_window", {"start": "2024-06-20"}), _context())
