"""Invariant guards for bounded local-model orchestration and output gating."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from pathlib import PurePosixPath
from zoneinfo import ZoneInfo

from src.agent.workflow import AgentPolicy, AgentRunner
from src.core.buyback_document import BuybackFact, EvidenceLocation, ParsedBuyback
from src.core.revisions import EventLink
from src.data.catalog import FilingVersion
from src.data.local_lake import SecurityMatch
from src.research.comparables import ComparableSet
from src.research.context import ResearchContext
from src.research.event_study import StudyPolicy, StudyResult
from src.research.materiality import MaterialityResult
from src.research.memo import ResearchMemo, build_baseline_memo

KST = ZoneInfo("Asia/Seoul")
AS_OF = datetime(2024, 6, 24, 18, 0, tzinfo=KST)
DOC_HASH = "d" * 64
RAW_HASH = "a" * 64
ZIP_PATH = PurePosixPath("raw/dart/20240620000001.zip")
STUDY_POLICY = StudyPolicy(estimation_start=-8, estimation_end=-2, min_pairs=3, horizons=(1,))
POLICY = AgentPolicy(max_tool_calls=3, model_timeout_seconds=5.0, prompt_version="v1")


class _ScriptedModel:
    def __init__(self, responses: list[object]) -> None:
        self._responses = list(responses)
        self.schemas: list[str] = []

    def generate_json(self, messages: Sequence[Mapping[str, str]], schema_name: str) -> Mapping[str, object]:
        del messages
        self.schemas.append(schema_name)
        response = self._responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        assert isinstance(response, dict)
        return response


def _baseline() -> tuple[ResearchContext, ResearchMemo]:
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
        policy_version=STUDY_POLICY.version,
        omitted_sessions=0,
    )
    comparables = ComparableSet(
        event_id="buyback:01386916:20240620000001",
        as_of=AS_OF,
        feature_end_session=date(2024, 6, 19),
        peer_ids=(),
        analogue_event_ids=(),
        analogue_intraday_excess=(),
        analogue_quantiles={"p25": None, "median": None, "p75": None},
        exclusions={},
        status="LOW_SAMPLE",
    )
    context = ResearchContext(
        anchor_rcept_no="20240620000001",
        active_rcept_no="20240620000001",
        event=EventLink("buyback:01386916:20240620000001", ("20240620000001",), "LINKED"),
        filing=filing,
        parsed=parsed,
        security=SecurityMatch("KRX:000001", "000001", "KOSPI", "KR7000001001", date(2024, 6, 19), "OK"),
        financial_facts=(),
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
    return context, build_baseline_memo(context)


def _plan(*calls: Mapping[str, object]) -> dict[str, object]:
    return {"tool_calls": list(calls)}


def _narrative(text: str, evidence_id: str) -> dict[str, object]:
    return {"claims": [{"evidence_ids": [evidence_id], "kind": "narrative", "metric_key": None, "text": text}]}


def test_new_number_in_prose_returns_rejected_baseline() -> None:
    """Prose with an unsupported financial number keeps baseline facts with rejection status."""
    context, baseline = _baseline()
    plan = _plan({"arguments": {}, "name": "get_event"})
    model = _ScriptedModel([plan, _narrative("Projected value is 999999.", baseline.evidence[0].id)])
    memo = AgentRunner().run(context, baseline, model, POLICY)  # type: ignore[arg-type]
    assert memo.facts == baseline.facts
    assert memo.metrics == baseline.metrics
    assert memo.claims == baseline.claims
    assert "AGENT_REJECTED" in memo.statuses


def test_model_timeout_keeps_publishable_baseline() -> None:
    """A timing-out model leaves the deterministic baseline publishable."""
    context, baseline = _baseline()
    model = _ScriptedModel([TimeoutError("slow")])
    memo = AgentRunner().run(context, baseline, model, POLICY)  # type: ignore[arg-type]
    assert memo.facts == baseline.facts
    assert memo.evidence == baseline.evidence
    assert memo.manifest_hash == baseline.manifest_hash
    assert "AGENT_UNAVAILABLE" in memo.statuses


def test_valid_plan_keeps_metrics_and_records_trace() -> None:
    """One allowed tool and cited narrative keep metrics identical with a tool trace."""
    context, baseline = _baseline()
    value = baseline.metrics["intraday_excess_s0"]
    assert value is not None
    plan = _plan({"arguments": {}, "name": "get_event"})
    model = _ScriptedModel([plan, _narrative(f"Observed movement is {value}.", baseline.evidence[0].id)])
    memo = AgentRunner().run(context, baseline, model, POLICY)  # type: ignore[arg-type]
    assert memo.metrics == baseline.metrics
    assert memo.facts == baseline.facts
    assert memo.manifest_hash == baseline.manifest_hash
    assert len(memo.claims) == len(baseline.claims) + 1
    assert "AGENT_OK" in memo.statuses
    assert "AGENT_TOOL:get_event" in memo.statuses


def test_plan_beyond_limit_executes_no_tool() -> None:
    """A plan larger than the policy is rejected before any tool runs."""
    context, baseline = _baseline()
    plan = _plan({"arguments": {}, "name": "get_event"})
    model = _ScriptedModel([plan])
    tight = AgentPolicy(max_tool_calls=0, model_timeout_seconds=5.0, prompt_version="v1")
    memo = AgentRunner().run(context, baseline, model, tight)  # type: ignore[arg-type]
    assert memo.claims == baseline.claims
    assert "AGENT_REJECTED" in memo.statuses
    assert not any(status.startswith("AGENT_TOOL:") for status in memo.statuses)
    assert model.schemas == ["tool_plan"]


def test_malformed_plan_returns_rejected_baseline() -> None:
    """A plan that fails schema validation keeps baseline content."""
    context, baseline = _baseline()
    model = _ScriptedModel([{"tool_calls": "nope"}])
    memo = AgentRunner().run(context, baseline, model, POLICY)  # type: ignore[arg-type]
    assert memo.claims == baseline.claims
    assert "AGENT_REJECTED" in memo.statuses


def test_disallowed_tool_name_returns_rejected_baseline() -> None:
    """A shell-style tool inside a plan is rejected before data access."""
    context, baseline = _baseline()
    model = _ScriptedModel([_plan({"arguments": {}, "name": "shell"})])
    memo = AgentRunner().run(context, baseline, model, POLICY)  # type: ignore[arg-type]
    assert memo.claims == baseline.claims
    assert "AGENT_REJECTED" in memo.statuses


def test_second_call_timeout_records_partial_trace() -> None:
    """A timeout on narrative drafting keeps baseline with the executed tool trace."""
    context, baseline = _baseline()
    plan = _plan({"arguments": {}, "name": "get_event"})
    model = _ScriptedModel([plan, TimeoutError("slow")])
    memo = AgentRunner().run(context, baseline, model, POLICY)  # type: ignore[arg-type]
    assert memo.metrics == baseline.metrics
    assert "AGENT_UNAVAILABLE" in memo.statuses
    assert "AGENT_TOOL:get_event" in memo.statuses


def test_malformed_narrative_returns_rejected_baseline() -> None:
    """Narrative missing required fields keeps baseline content."""
    context, baseline = _baseline()
    plan = _plan({"arguments": {}, "name": "get_event"})
    model = _ScriptedModel([plan, {"claims": [{"kind": "narrative"}]}])
    memo = AgentRunner().run(context, baseline, model, POLICY)  # type: ignore[arg-type]
    assert memo.claims == baseline.claims
    assert "AGENT_REJECTED" in memo.statuses


def test_unknown_evidence_id_returns_rejected_baseline() -> None:
    """A claim citing an unavailable evidence ID is rejected."""
    context, baseline = _baseline()
    plan = _plan({"arguments": {}, "name": "get_event"})
    model = _ScriptedModel([plan, _narrative("Observed movement noted.", "missing-id")])
    memo = AgentRunner().run(context, baseline, model, POLICY)  # type: ignore[arg-type]
    assert memo.claims == baseline.claims
    assert "AGENT_REJECTED" in memo.statuses
