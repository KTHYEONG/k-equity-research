"""Invariant guards for bounded local-model orchestration and output gating."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from pathlib import PurePosixPath
from zoneinfo import ZoneInfo

import pytest

from src.agent.workflow import AGENT_SCHEMAS, AgentPolicy, AgentRunner
from src.core.buyback_document import BuybackFact, EvidenceLocation, ParsedBuyback
from src.core.revisions import EventLink
from src.data.catalog import FilingVersion
from src.data.local_lake import SecurityMatch
from src.research.comparables import ComparableSet
from src.research.context import ResearchContext
from src.research.event_study import StudyPolicy, StudyResult
from src.research.materiality import MaterialityResult
from src.research.memo import MemoClaim, ResearchMemo, build_baseline_memo

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


def _baseline_with_metrics(extra: dict[str, str | None]) -> tuple[ResearchContext, ResearchMemo]:
    import dataclasses

    context, baseline = _baseline()
    merged = dict(baseline.metrics)
    merged.update(extra)
    replaced = dataclasses.replace(baseline, metrics=merged)
    return context, replaced


def test_display_figures_pass_gate_with_reason_ok() -> None:
    context, baseline = _baseline_with_metrics({"amount_to_cash": "0.2615802262734371504861186017"})
    plan = _plan({"arguments": {}, "name": "get_event"})
    model = _ScriptedModel([plan, _narrative("현금 대비 26.16% 규모로 관측됨.", baseline.evidence[0].id)])
    memo = AgentRunner().run(context, baseline, model, POLICY)  # type: ignore[arg-type]
    assert "AGENT_OK" in memo.statuses
    assert "AGENT_REASON:ungrounded_number" not in memo.statuses
    assert not any(status.startswith("AGENT_REASON:") for status in memo.statuses)
    assert len(memo.claims) == len(baseline.claims) + 1


def test_invented_number_rejected_with_reason() -> None:
    context, baseline = _baseline_with_metrics({"amount_to_cash": "0.2615802262734371504861186017"})
    plan = _plan({"arguments": {}, "name": "get_event"})
    model = _ScriptedModel([plan, _narrative("현금 대비 27.40% 규모로 관측됨.", baseline.evidence[0].id)])
    memo = AgentRunner().run(context, baseline, model, POLICY)  # type: ignore[arg-type]
    assert memo.claims == baseline.claims
    assert "AGENT_REJECTED" in memo.statuses
    assert "AGENT_REASON:ungrounded_number" in memo.statuses


def test_sign_flip_rejected() -> None:
    context, baseline = _baseline_with_metrics({"car_h5": "-0.0349"})
    plan = _plan({"arguments": {}, "name": "get_event"})
    model = _ScriptedModel([plan, _narrative("5일 구간에서 3.49% 하락이 관측됨.", baseline.evidence[0].id)])
    memo = AgentRunner().run(context, baseline, model, POLICY)  # type: ignore[arg-type]
    assert "AGENT_REJECTED" in memo.statuses
    assert "AGENT_REASON:ungrounded_number" in memo.statuses


def test_horizon_label_exempt_and_bare_numeral_rejected() -> None:
    context, baseline = _baseline()
    value = baseline.metrics["intraday_excess_s0"]
    assert value is not None
    from src.research.memo import display_figures

    shown = display_figures(baseline)["metric:intraday_excess_s0"]
    plan = _plan({"arguments": {}, "name": "get_event"})
    model = _ScriptedModel([plan, _narrative(f"5일 초과수익률 {shown} 관측됨.", baseline.evidence[0].id)])
    memo = AgentRunner().run(context, baseline, model, POLICY)  # type: ignore[arg-type]
    assert "AGENT_OK" in memo.statuses

    context2, baseline2 = _baseline()
    plan2 = _plan({"arguments": {}, "name": "get_event"})
    model2 = _ScriptedModel([plan2, _narrative("5% 상승이 관측됨.", baseline2.evidence[0].id)])
    memo2 = AgentRunner().run(context2, baseline2, model2, POLICY)  # type: ignore[arg-type]
    assert "AGENT_REJECTED" in memo2.statuses
    assert "AGENT_REASON:ungrounded_number" in memo2.statuses

    context3, baseline3 = _baseline()
    plan3 = _plan({"arguments": {}, "name": "get_event"})
    model3 = _ScriptedModel([plan3, _narrative("2050일 관측됨.", baseline3.evidence[0].id)])
    memo3 = AgentRunner().run(context3, baseline3, model3, POLICY)  # type: ignore[arg-type]
    assert "AGENT_REJECTED" in memo3.statuses


def test_reason_codes_for_gate_paths() -> None:
    import dataclasses

    context, baseline = _baseline()
    eid = baseline.evidence[0].id
    cases: list[tuple[dict[str, object], str]] = [
        ({"claims": [{"evidence_ids": [eid], "kind": "bogus", "metric_key": None, "text": "관측됨."}]}, "unknown_kind"),
        ({"claims": [{"evidence_ids": [eid], "kind": "narrative", "metric_key": None, "text": "   "}]}, "empty_text"),
        ({"claims": [{"evidence_ids": [], "kind": "narrative", "metric_key": None, "text": "관측됨."}]}, "no_evidence"),
        (
            {"claims": [{"evidence_ids": [eid], "kind": "narrative", "metric_key": "missing_key", "text": "관측됨."}]},
            "unknown_metric_key",
        ),
    ]
    for payload, code in cases:
        plan = _plan({"arguments": {}, "name": "get_event"})
        model = _ScriptedModel([plan, payload])
        memo = AgentRunner().run(context, baseline, model, POLICY)  # type: ignore[arg-type]
        assert f"AGENT_REASON:{code}" in memo.statuses, code
        assert "AGENT_REJECTED" in memo.statuses
        assert sum(1 for status in memo.statuses if status.startswith("AGENT_REASON:")) == 1

    plan_bad = _plan({"arguments": {}, "name": "get_event"})
    model_ok = _ScriptedModel([plan_bad, {"claims": []}])
    memo_ok = AgentRunner().run(context, baseline, model_ok, POLICY)  # type: ignore[arg-type]
    assert "AGENT_OK" in memo_ok.statuses
    assert not any(status.startswith("AGENT_REASON:") for status in memo_ok.statuses)

    for raw_plan, code, legacy in [
        ({"tool_calls": "nope"}, "plan_schema", "AGENT_REJECTED"),
        (_plan({"arguments": {}, "name": "shell"}), "tool_use", "AGENT_REJECTED"),
    ]:
        model = _ScriptedModel([raw_plan])
        memo = AgentRunner().run(context, baseline, model, POLICY)  # type: ignore[arg-type]
        assert f"AGENT_REASON:{code}" in memo.statuses, code
        assert legacy in memo.statuses

    tight = AgentPolicy(max_tool_calls=0, model_timeout_seconds=5.0, prompt_version="v1")
    model = _ScriptedModel([_plan({"arguments": {}, "name": "get_event"})])
    memo = AgentRunner().run(context, baseline, model, tight)  # type: ignore[arg-type]
    assert "AGENT_REASON:plan_over_budget" in memo.statuses

    model = _ScriptedModel([TimeoutError("slow")])
    memo = AgentRunner().run(context, baseline, model, POLICY)  # type: ignore[arg-type]
    assert "AGENT_REASON:plan_unavailable" in memo.statuses
    assert "AGENT_UNAVAILABLE" in memo.statuses

    model = _ScriptedModel([_plan({"arguments": {}, "name": "get_event"}), TimeoutError("slow")])
    memo = AgentRunner().run(context, baseline, model, POLICY)  # type: ignore[arg-type]
    assert "AGENT_REASON:narrative_unavailable" in memo.statuses

    model = _ScriptedModel([_plan({"arguments": {}, "name": "get_event"}), {"claims": [{"kind": "narrative"}]}])
    memo = AgentRunner().run(context, baseline, model, POLICY)  # type: ignore[arg-type]
    assert "AGENT_REASON:narrative_schema" in memo.statuses

    _ = dataclasses  # keep import used if branches shift


def test_model_provenance_recorded() -> None:
    context, baseline = _baseline()
    plan = _plan({"arguments": {}, "name": "get_event"})
    provenance = AgentPolicy(max_tool_calls=3, model_timeout_seconds=5.0, prompt_version="v2", model_id="gemma-4-12b-qat")
    value = baseline.metrics["intraday_excess_s0"]
    assert value is not None
    model = _ScriptedModel([plan, _narrative(f"관측된 움직임은 {value}.", baseline.evidence[0].id)])
    memo = AgentRunner().run(context, baseline, model, provenance)  # type: ignore[arg-type]
    assert "AGENT_MODEL:gemma-4-12b-qat" in memo.statuses

    model = _ScriptedModel([plan, _narrative("Projected value is 999999.", baseline.evidence[0].id)])
    memo = AgentRunner().run(context, baseline, model, provenance)  # type: ignore[arg-type]
    assert "AGENT_MODEL:gemma-4-12b-qat" in memo.statuses
    assert "AGENT_REJECTED" in memo.statuses

    model = _ScriptedModel([TimeoutError("slow")])
    memo = AgentRunner().run(context, baseline, model, provenance)  # type: ignore[arg-type]
    assert "AGENT_MODEL:gemma-4-12b-qat" in memo.statuses
    assert "AGENT_UNAVAILABLE" in memo.statuses

    model = _ScriptedModel([plan, _narrative(f"관측된 움직임은 {value}.", baseline.evidence[0].id)])
    memo = AgentRunner().run(context, baseline, model, POLICY)  # type: ignore[arg-type]
    assert not any(status.startswith("AGENT_MODEL:") for status in memo.statuses)


def test_prompt_carries_display_figures_only() -> None:
    import dataclasses

    from src.agent.workflow import _summary_messages
    from src.research.memo import display_figures

    _, baseline = _baseline_with_metrics({"amount_to_cash": "0.2615802262734371504861186017"})
    raw_claim = MemoClaim(
        "financial_ratio",
        "Observed amount_to_cash is 0.2615802262734371504861186017 (deterministic ratio).",
        (baseline.evidence[0].id,),
        "amount_to_cash",
    )
    baseline = dataclasses.replace(baseline, claims=(*baseline.claims, raw_claim))
    policy = AgentPolicy(max_tool_calls=3, model_timeout_seconds=5.0, prompt_version="v2")
    messages = _summary_messages(baseline, policy)
    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    user = messages[1]["content"]
    assert "26.16%" in user
    assert "0.2615802262734371504861186017" not in messages[0]["content"]
    assert "0.2615802262734371504861186017" not in user
    assert "AGENT_PROMPT:v2" in user or "AGENT_PROMPT:v2" in messages[0]["content"]
    assert display_figures(baseline)["metric:amount_to_cash"] == "26.16%"
    first = _summary_messages(baseline, policy)
    second = _summary_messages(baseline, policy)
    assert first == second


def test_schema_model_agreement_and_policy_compat() -> None:
    from pydantic import ValidationError

    from src.agent.workflow import AGENT_SCHEMAS, _NarrativeOutput

    assert set(AGENT_SCHEMAS.keys()) == {"tool_plan", "memo_claims"}
    kinds = set(AGENT_SCHEMAS["memo_claims"]["properties"]["claims"]["items"]["properties"]["kind"]["enum"])  # type: ignore[index]
    assert kinds == {"filing_fact", "materiality", "narrative", "peer_context", "price_path", "uncertainty"}
    items = AGENT_SCHEMAS["tool_plan"]["properties"]["tool_calls"]["items"]  # type: ignore[index]
    tools = {shape["properties"]["name"]["const"] for shape in items["anyOf"]}  # type: ignore[index]
    assert tools == {"get_event", "get_financial_asof", "get_market_window", "get_peers", "get_analogues", "get_revision_diff"}
    with __import__("pytest").raises(ValidationError):
        _NarrativeOutput.model_validate(
            {"claims": [{"evidence_ids": ["e"], "extra": "x", "kind": "narrative", "text": "t"}]}
        )
    legacy = AgentPolicy(3, 30.0, "v1")
    assert legacy.model_id == ""
    assert legacy.prompt_version == "v1"


def test_tool_plan_schema_derives_from_specs() -> None:
    """One per-tool shape with fixed name, declared arguments and closed properties."""
    from src.agent.tools import TOOL_SPECS
    from src.agent.workflow import AGENT_SCHEMAS

    items = AGENT_SCHEMAS["tool_plan"]["properties"]["tool_calls"]["items"]  # type: ignore[index]
    assert items["anyOf"] is not None
    assert len(items["anyOf"]) == len(TOOL_SPECS)
    assert AGENT_SCHEMAS["tool_plan"]["properties"]["tool_calls"]["maxItems"] <= 3  # type: ignore[index]
    by_name = {shape["properties"]["name"]["const"]: shape for shape in items["anyOf"]}  # type: ignore[index]
    assert set(by_name) == set(TOOL_SPECS)
    for name, spec in TOOL_SPECS.items():
        shape = by_name[name]
        assert shape["required"] == ["name", "arguments"]
        assert shape["additionalProperties"] is False
        arguments = shape["properties"]["arguments"]
        assert set(arguments["properties"]) == {arg.name for arg in spec.args}
        assert arguments["additionalProperties"] is False
        assert set(arguments.get("required", [])) == {arg.name for arg in spec.args if arg.required}


def test_invalid_argument_unrepresentable() -> None:
    """The get_event shape declares no properties so event_id is not expressible."""
    from src.agent.workflow import AGENT_SCHEMAS

    items = AGENT_SCHEMAS["tool_plan"]["properties"]["tool_calls"]["items"]  # type: ignore[index]
    by_name = {shape["properties"]["name"]["const"]: shape for shape in items["anyOf"]}  # type: ignore[index]
    arguments = by_name["get_event"]["properties"]["arguments"]
    assert arguments["properties"] == {}
    assert arguments["additionalProperties"] is False


def test_date_arguments_pattern_bound() -> None:
    """get_market_window start and end are required ISO-date strings."""
    from src.agent.workflow import AGENT_SCHEMAS

    items = AGENT_SCHEMAS["tool_plan"]["properties"]["tool_calls"]["items"]  # type: ignore[index]
    by_name = {shape["properties"]["name"]["const"]: shape for shape in items["anyOf"]}  # type: ignore[index]
    arguments = by_name["get_market_window"]["properties"]["arguments"]
    assert set(arguments["required"]) == {"start", "end"}
    for key in ("start", "end"):
        assert arguments["properties"][key]["type"] == "string"
        assert "pattern" in arguments["properties"][key]


def test_schema_never_stricter_than_model() -> None:
    """A representative valid plan for each tool passes the permissive pydantic model."""
    from src.agent.tools import TOOL_SPECS
    from src.agent.workflow import _ToolPlan

    for name, spec in TOOL_SPECS.items():
        arguments = {arg.name: "2024-06-20" if arg.kind == "iso_date" else "assets" for arg in spec.args}
        plan = _ToolPlan.model_validate({"tool_calls": [{"arguments": arguments, "name": name}]})
        assert plan.tool_calls[0].name == name


def test_prompt_lists_typed_signatures() -> None:
    """System message carries each tool name with argument marks and the fail-closed rule."""
    from src.agent.workflow import _summary_messages

    _, baseline = _baseline()
    messages = _summary_messages(baseline, POLICY)
    system = messages[0]["content"]
    for name in ("get_event", "get_financial_asof", "get_market_window", "get_peers", "get_analogues", "get_revision_diff"):
        assert name in system
    assert "fact" in system
    assert "optional" in system
    assert "start" in system
    assert "end" in system
    assert "required" in system
    assert "invalid" in system.lower()
    assert "discards the whole narrative" in system
    assert "as_of" in system
    first = _summary_messages(baseline, POLICY)
    assert first == messages


def test_malformed_output_is_rejection() -> None:
    """ValueError from the model client is a rejection, never unavailable."""
    context, baseline = _baseline()
    plan = _plan({"arguments": {}, "name": "get_event"})
    model = _ScriptedModel([ValueError("truncated")])
    memo = AgentRunner().run(context, baseline, model, POLICY)  # type: ignore[arg-type]
    assert "AGENT_REJECTED" in memo.statuses
    assert "AGENT_REASON:plan_malformed" in memo.statuses
    assert "AGENT_UNAVAILABLE" not in memo.statuses
    model = _ScriptedModel([plan, ValueError("bad json")])
    memo = AgentRunner().run(context, baseline, model, POLICY)  # type: ignore[arg-type]
    assert "AGENT_REJECTED" in memo.statuses
    assert "AGENT_REASON:narrative_malformed" in memo.statuses


def test_transport_failure_stays_unavailable() -> None:
    """Timeout, connection and generic faults stay unavailable at either stage."""
    context, baseline = _baseline()
    plan = _plan({"arguments": {}, "name": "get_event"})
    for error, code in [(TimeoutError("slow"), "plan_unavailable"), (ConnectionError("down"), "plan_unavailable"), (RuntimeError("boom"), "plan_unavailable")]:
        model = _ScriptedModel([error])
        memo = AgentRunner().run(context, baseline, model, POLICY)  # type: ignore[arg-type]
        assert "AGENT_UNAVAILABLE" in memo.statuses
        assert f"AGENT_REASON:{code}" in memo.statuses
        assert memo.claims == baseline.claims
    for error, code in [(TimeoutError("slow"), "narrative_unavailable"), (ConnectionError("down"), "narrative_unavailable"), (RuntimeError("boom"), "narrative_unavailable")]:
        model = _ScriptedModel([plan, error])
        memo = AgentRunner().run(context, baseline, model, POLICY)  # type: ignore[arg-type]
        assert "AGENT_UNAVAILABLE" in memo.statuses
        assert f"AGENT_REASON:{code}" in memo.statuses


def test_invalid_tool_use_still_fail_closed() -> None:
    """Plans bypassing the schema with unknown tools or args are rejected without a narrative request."""
    context, baseline = _baseline()
    model = _ScriptedModel([_plan({"arguments": {}, "name": "shell"})])
    memo = AgentRunner().run(context, baseline, model, POLICY)  # type: ignore[arg-type]
    assert "AGENT_REJECTED" in memo.statuses
    assert "AGENT_REASON:tool_use" in memo.statuses
    assert model.schemas == ["tool_plan"]
    model = _ScriptedModel([_plan({"arguments": {"event_id": "00127255"}, "name": "get_event"})])
    memo = AgentRunner().run(context, baseline, model, POLICY)  # type: ignore[arg-type]
    assert "AGENT_REJECTED" in memo.statuses
    assert "AGENT_REASON:tool_use" in memo.statuses
    assert model.schemas == ["tool_plan"]


def test_legacy_status_compatibility() -> None:
    """Every new path carries exactly one legacy AGENT_REJECTED or AGENT_UNAVAILABLE marker."""
    context, baseline = _baseline()
    plan = _plan({"arguments": {}, "name": "get_event"})
    cases = [
        _ScriptedModel([ValueError("x")]),
        _ScriptedModel([plan, ValueError("x")]),
        _ScriptedModel([TimeoutError("x")]),
        _ScriptedModel([plan, ConnectionError("x")]),
        _ScriptedModel([plan, RuntimeError("x")]),
        _ScriptedModel([_plan({"arguments": {"event_id": "1"}, "name": "get_event"})]),
    ]
    for model in cases:
        memo = AgentRunner().run(context, baseline, model, POLICY)  # type: ignore[arg-type]
        legacy = [status for status in memo.statuses if status in ("AGENT_REJECTED", "AGENT_UNAVAILABLE")]
        assert len(legacy) == 1


def test_prompt_never_echoes_baseline_claims() -> None:
    """Baseline claim text (raw decimals, English prose) is not offered to the model; kind and evidence ids are."""
    import dataclasses

    from src.agent.workflow import _summary_messages

    _, baseline = _baseline_with_metrics({"amount_to_cash": "0.2615802262734371504861186017"})
    evidence_id = baseline.evidence[0].id
    raw_claim = MemoClaim(
        "financial_ratio",
        "Observed amount_to_cash is 0.2615802262734371504861186017 (deterministic ratio).",
        (evidence_id,),
        "amount_to_cash",
    )
    baseline = dataclasses.replace(baseline, claims=(raw_claim,))
    messages = _summary_messages(baseline, AgentPolicy(3, 5.0, "v2"))
    joined = "\n".join(message["content"] for message in messages)
    assert "0.2615802262734371504861186017" not in joined
    assert "deterministic ratio" not in joined
    assert f"financial_ratio|amount_to_cash|{evidence_id}" in joined


def test_prompt_states_bounds() -> None:
    from src.agent.workflow import _MAX_CLAIMS, _MAX_TEXT_CHARS, _summary_messages

    _, baseline = _baseline()
    system = _summary_messages(baseline, AgentPolicy(3, 5.0, "v2"))[0]["content"]
    assert f"at most {_MAX_CLAIMS} new claims" in system
    assert f"at most {_MAX_TEXT_CHARS} characters" in system
    assert "Do not repeat, translate or restate the existing claims" in system


def test_schema_carries_bounds() -> None:
    from src.agent.workflow import _MAX_CLAIMS, _MAX_TEXT_CHARS

    claims = AGENT_SCHEMAS["memo_claims"]["properties"]["claims"]  # type: ignore[index]
    assert claims["maxItems"] == _MAX_CLAIMS
    assert claims["items"]["properties"]["text"]["maxLength"] == _MAX_TEXT_CHARS


def _claims_payload(texts: list[str], evidence_id: str) -> dict[str, object]:
    return {
        "claims": [
            {"evidence_ids": [evidence_id], "kind": "narrative", "metric_key": None, "text": text} for text in texts
        ]
    }


def test_too_many_claims_rejected() -> None:
    from src.agent.workflow import _MAX_CLAIMS

    context, baseline = _baseline()
    payload = _claims_payload(["관측됨."] * (_MAX_CLAIMS + 1), baseline.evidence[0].id)
    model = _ScriptedModel([_plan({"arguments": {}, "name": "get_event"}), payload])
    memo = AgentRunner().run(context, baseline, model, POLICY)  # type: ignore[arg-type]
    assert memo.claims == baseline.claims
    assert "AGENT_REJECTED" in memo.statuses
    assert "AGENT_REASON:too_many_claims" in memo.statuses


def test_claims_at_bound_accepted_and_overlong_text_rejected() -> None:
    from src.agent.workflow import _MAX_CLAIMS, _MAX_TEXT_CHARS

    context, baseline = _baseline()
    evidence_id = baseline.evidence[0].id
    plan = _plan({"arguments": {}, "name": "get_event"})
    at_bound = _claims_payload(["가" * _MAX_TEXT_CHARS] * _MAX_CLAIMS, evidence_id)
    ok = AgentRunner().run(context, baseline, _ScriptedModel([plan, at_bound]), POLICY)  # type: ignore[arg-type]
    assert "AGENT_OK" in ok.statuses
    assert len(ok.claims) == len(baseline.claims) + _MAX_CLAIMS
    overlong = _claims_payload(["가" * (_MAX_TEXT_CHARS + 1)], evidence_id)
    bad = AgentRunner().run(context, baseline, _ScriptedModel([plan, overlong]), POLICY)  # type: ignore[arg-type]
    assert "AGENT_REASON:text_too_long" in bad.statuses
    assert bad.claims == baseline.claims


def test_bounded_schema_payload_passes_pydantic_model() -> None:
    from src.agent.workflow import _MAX_CLAIMS, _MAX_TEXT_CHARS, _NarrativeOutput

    payload = _claims_payload(["가" * _MAX_TEXT_CHARS] * _MAX_CLAIMS, "e1")
    assert len(_NarrativeOutput.model_validate(payload).claims) == _MAX_CLAIMS


def _tool_result(payload: dict[str, object], evidence_ids: tuple[str, ...] = ()) -> object:
    from src.agent.tools import ToolResult

    return ToolResult(name="get_financial_asof", payload=payload, evidence_ids=evidence_ids, as_of=AS_OF)


def _narrative_text(results: list[object]) -> str:
    from src.agent.workflow import _narrative_messages

    return "\n".join(m["content"] for m in _narrative_messages([{"role": "system", "content": "s"}], results))  # type: ignore[arg-type]


def test_large_tool_list_compacted_to_count_head_tail() -> None:
    rows = [{"fact": f"row{i:03d}"} for i in range(100)]
    text = _narrative_text([_tool_result({"facts": rows})])
    assert '"count": 100' in text
    for kept in ("row000", "row001", "row002", "row097", "row098", "row099"):
        assert kept in text
    assert "row050" not in text


def test_small_tool_list_untouched() -> None:
    items = [f"s{i}" for i in range(8)]
    text = _narrative_text([_tool_result({"sessions": items})])
    assert all(item in text for item in items)
    assert '"count"' not in text


def test_nested_tool_lists_compacted_and_result_not_mutated() -> None:
    payload: dict[str, object] = {"facts": [{"inner": [f"v{i:02d}" for i in range(50)]} for _ in range(3)]}
    result = _tool_result({"facts": payload["facts"]}, ("e1",))
    text = _narrative_text([result])
    assert "v25" not in text
    assert text.count('"count": 50') == 3
    assert len(result.payload["facts"][0]["inner"]) == 50  # type: ignore[attr-defined]
    assert result.evidence_ids == ("e1",)  # type: ignore[attr-defined]


def test_tool_evidence_line_bounded() -> None:
    ids = tuple(f"ev{i:02d}" for i in range(50))
    text = _narrative_text([_tool_result({}, ids)])
    assert "ev19" in text
    assert "ev20" not in text
    assert "(+30 more)" in text


def test_evidence_id_omitted_from_prompt_remains_citable(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.agent import workflow
    from src.agent.tools import ToolResult

    ids = tuple(f"ev{i:02d}" for i in range(50))
    monkeypatch.setattr(workflow, "execute_tool", lambda request, context: ToolResult(request.name, {}, ids, AS_OF))
    context, baseline = _baseline()
    plan = _plan({"arguments": {}, "name": "get_event"})
    accepted = AgentRunner().run(context, baseline, _ScriptedModel([plan, _narrative("관측됨.", "ev49")]), POLICY)  # type: ignore[arg-type]
    assert "AGENT_OK" in accepted.statuses
    rejected = AgentRunner().run(context, baseline, _ScriptedModel([plan, _narrative("관측됨.", "ev50")]), POLICY)  # type: ignore[arg-type]
    assert "AGENT_REASON:unknown_evidence" in rejected.statuses


def test_narrative_prompt_size_bounded_for_huge_payload() -> None:
    rows = [{"fact": f"fact-{i}", "value": str(i * 1_000_003), "unit": "KRW"} for i in range(5000)]
    assert len(_narrative_text([_tool_result({"facts": rows}, tuple(f"k{i}" for i in range(5000)))])) < 12_000


def test_schema_and_prompt_carry_evidence_bound() -> None:
    from src.agent.workflow import _MAX_EVIDENCE_PER_CLAIM, _summary_messages

    claims = AGENT_SCHEMAS["memo_claims"]["properties"]["claims"]  # type: ignore[index]
    assert claims["items"]["properties"]["evidence_ids"]["maxItems"] == _MAX_EVIDENCE_PER_CLAIM
    _, baseline = _baseline()
    system = _summary_messages(baseline, AgentPolicy(3, 5.0, "v2"))[0]["content"]
    assert f"at most {_MAX_EVIDENCE_PER_CLAIM} per claim" in system


def test_seven_evidence_ids_rejected_and_six_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.agent import workflow
    from src.agent.tools import ToolResult
    from src.agent.workflow import _MAX_EVIDENCE_PER_CLAIM

    ids = tuple(f"ev{i}" for i in range(_MAX_EVIDENCE_PER_CLAIM + 1))
    monkeypatch.setattr(workflow, "execute_tool", lambda request, context: ToolResult(request.name, {}, ids, AS_OF))
    context, baseline = _baseline()
    plan = _plan({"arguments": {}, "name": "get_event"})

    def claim(cited: tuple[str, ...]) -> dict[str, object]:
        return {"claims": [{"evidence_ids": list(cited), "kind": "narrative", "metric_key": None, "text": "관측됨."}]}

    bad = AgentRunner().run(context, baseline, _ScriptedModel([plan, claim(ids)]), POLICY)  # type: ignore[arg-type]
    assert "AGENT_REASON:too_many_evidence" in bad.statuses
    assert bad.claims == baseline.claims
    good = AgentRunner().run(context, baseline, _ScriptedModel([plan, claim(ids[:-1])]), POLICY)  # type: ignore[arg-type]
    assert "AGENT_OK" in good.statuses


def test_glossary_only_for_present_figure_keys() -> None:
    from src.agent.workflow import _summary_messages

    _, baseline = _baseline_with_metrics({"car_h1": "0.0295", "car_h5": None})
    system = _summary_messages(baseline, AgentPolicy(3, 5.0, "v2"))[0]["content"]
    assert "metric:car_h1: cumulative market-model abnormal return over the 1 trading session after the first actionable session" in system
    assert "metric:car_h5" not in system
    assert "never describe them as months, weeks, hours or minutes" in system


@pytest.mark.parametrize("text", ["1개월간 +2.95%", "초반 0분 동안 +0.92%", "2주간 +2.95%", "3 시간 동안 +2.95%"])
def test_invented_duration_rejected(text: str) -> None:
    context, baseline = _baseline_with_metrics({"car_h1": "0.0295", "car_h5": "0.0295"})
    plan = _plan({"arguments": {}, "name": "get_event"})
    memo = AgentRunner().run(context, baseline, _ScriptedModel([plan, _narrative(text, baseline.evidence[0].id)]), POLICY)  # type: ignore[arg-type]
    assert "AGENT_REASON:ungrounded_number" in memo.statuses
    assert memo.claims == baseline.claims


def test_quarter_and_trading_day_wording_allowed_but_longer_number_rejected() -> None:
    context, baseline = _baseline_with_metrics({"car_h5": "0.0437", "financials_period": None})
    plan = _plan({"arguments": {}, "name": "get_event"})
    evidence_id = baseline.evidence[0].id
    ok_text = "5거래일 동안 +4.37%를 기록했습니다."
    ok = AgentRunner().run(context, baseline, _ScriptedModel([plan, _narrative(ok_text, evidence_id)]), POLICY)  # type: ignore[arg-type]
    assert "AGENT_OK" in ok.statuses
    bad = AgentRunner().run(context, baseline, _ScriptedModel([plan, _narrative("50거래일 동안 +4.37%", evidence_id)]), POLICY)  # type: ignore[arg-type]
    assert "AGENT_REASON:ungrounded_number" in bad.statuses
    from src.agent.workflow import _find_ungrounded_token

    assert _find_ungrounded_token("2022년 3분기 현금 기준", {"2022", "3"}) is None


def _gate(text: str, metrics: dict[str, str | None]) -> tuple[str, ...]:
    context, baseline = _baseline_with_metrics(metrics)
    plan = _plan({"arguments": {}, "name": "get_event"})
    memo = AgentRunner().run(context, baseline, _ScriptedModel([plan, _narrative(text, baseline.evidence[0].id)]), POLICY)  # type: ignore[arg-type]
    return memo.statuses


def test_supplied_identifiers_tolerated_and_others_checked() -> None:
    metrics: dict[str, str | None] = {"car_h20": "0.0752", "intraday_excess_s0": "0.0244"}
    assert "AGENT_OK" in _gate("car_h20은 +7.52%이다.", metrics)
    assert "AGENT_OK" in _gate("S0 거래 세션에서 +2.44%를 기록했다.", metrics)
    assert "AGENT_OK" in _gate("intraday_excess_s0는 +2.44%이다.", metrics)
    assert "AGENT_REASON:ungrounded_number" in _gate("car_h7은 +2.44%이다.", metrics)
    assert "AGENT_REASON:ungrounded_number" in _gate("S05 구간에서 +2.44%", metrics)


def test_prompt_no_longer_teaches_s0_label() -> None:
    import re

    from src.agent.workflow import _summary_messages

    _, baseline = _baseline_with_metrics({"intraday_excess_s0": "0.0244", "car_h1": "0.0295"})
    system = _summary_messages(baseline, AgentPolicy(3, 5.0, "v2"))[0]["content"]
    assert re.search(r"\bS0\b", system) is None
    assert "do not print figure keys or session labels" in system
