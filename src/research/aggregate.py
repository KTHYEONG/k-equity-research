"""Cross-event aggregation of descriptive post-filing price paths."""

from __future__ import annotations

import math
import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Literal, cast

from src.data.catalog import Catalog
from src.data.disclosure_context import verified_event_confound
from src.data.event_store import EventStore
from src.data.index_store import IndexStore
from src.data.local_lake import LocalLake
from src.integrations.krx_index import IndexBar
from src.research.context import _HISTORY_LOOKBACK
from src.research.event_study import StudyPolicy, study_buyback
from src.research.materiality import calculate_materiality

HORIZONS = (1, 5, 20)
_RATIO_BUCKETS = ((Decimal("0.001"), "<0.1%"), (Decimal("0.01"), "0.1-1%"), (Decimal("0.03"), "1-3%"))
_TOP_BUCKET = ">=3%"
_UNAVAILABLE = "UNAVAILABLE"
_CANCEL_TOKENS = ("소각",)
_COMPENSATION_TOKENS = ("임직원", "보상", "스톡", "stock", "성과급", "상여")


@dataclass(frozen=True, slots=True)
class EventOutcome:
    """One event's study result reduced to the fields used for grouping and statistics."""

    event_id: str
    receipt_date: date
    market: str
    purpose_class: str
    amount_ratio: Decimal | None
    confound: str
    status: str
    horizon_car: dict[int, Decimal | None]
    intraday_excess: Decimal | None


@dataclass(frozen=True, slots=True)
class GroupStat:
    """Cross-sectional summary of one horizon within one group; denominators are explicit."""

    dimension: str
    key: str
    horizon: int
    n: int
    mean: float
    median: float
    stdev: float | None
    t_stat: float | None


@dataclass(frozen=True, slots=True)
class Coverage:
    """Event counts by study status so every rate has its denominator."""

    linked_total: int
    total: int
    with_any_car: int
    by_status: dict[str, int]
    by_confound: dict[str, int]


@dataclass(frozen=True, slots=True)
class AggregateReport:
    """Frozen aggregate of all measured events at one as-of instant."""

    as_of: datetime
    policy_version: str
    index_manifest_hash: str
    coverage: Coverage
    stats: tuple[GroupStat, ...]


def classify_purpose(purpose_text: str | None) -> str:
    """Bucket the free-text acquisition purpose; cancellation wins over compensation when both appear."""
    if not purpose_text:
        return _UNAVAILABLE
    lowered = purpose_text.lower()
    if any(token in lowered for token in _CANCEL_TOKENS):
        return "CANCELLATION"
    if any(token in lowered for token in _COMPENSATION_TOKENS):
        return "COMPENSATION"
    return "OTHER"


def ratio_bucket(ratio: Decimal | None) -> str:
    """Bucket planned amount over prior market cap (fraction, not percent)."""
    if ratio is None:
        return _UNAVAILABLE
    for bound, label in _RATIO_BUCKETS:
        if ratio < bound:
            return label
    return _TOP_BUCKET


def collect_outcomes(
    catalog: Catalog,
    event_store: EventStore,
    lake: LocalLake,
    index_store: IndexStore,
    as_of: datetime,
    policy: StudyPolicy,
    progress: Callable[[int, int], None] | None = None,
) -> tuple[EventOutcome, ...]:
    """Run the point-in-time study for every linked event without building analogue sets.

    Events whose security or filing cannot be resolved at ``as_of`` are omitted; the caller must pass the linked-event
    count to ``aggregate_outcomes`` so the omission stays visible in the report.
    """
    events = event_store.list_prior_events(as_of)
    outcomes: list[EventOutcome] = []
    for position, (_link, original) in enumerate(events, start=1):
        outcome = _measure_event(catalog, event_store, lake, index_store, original.rcept_no, as_of, policy)
        if outcome is not None:
            outcomes.append(outcome)
        if progress is not None and (position % 50 == 0 or position == len(events)):
            progress(position, len(events))
    return tuple(outcomes)


def _measure_event(
    catalog: Catalog,
    event_store: EventStore,
    lake: LocalLake,
    index_store: IndexStore,
    anchor_rcept_no: str,
    as_of: datetime,
    policy: StudyPolicy,
) -> EventOutcome | None:
    resolved = event_store.get_event_asof(anchor_rcept_no, as_of)
    assert resolved is not None  # list_prior_events only yields LINKED events with an eligible original filing
    event, parsed = resolved
    filing = catalog.get_filing_asof(parsed.rcept_no, as_of)
    prior_session = lake.previous_session(filing.receipt_date) if filing is not None else None
    if filing is None or prior_session is None:
        return None
    security = lake.resolve_security(filing.stock_code, prior_session, as_of)
    if security.status != "OK":
        return None
    prior_bar = lake.market_bar(security.instrument_id, prior_session, as_of)
    ratio = calculate_materiality(parsed, filing.receipt_date, prior_bar).amount_to_market_cap if prior_bar else None
    start = filing.receipt_date - _HISTORY_LOOKBACK
    stock_bars = lake.market_window(security.instrument_id, start, as_of.date(), as_of)
    index_bars: tuple[IndexBar, ...] = ()
    if security.market in ("KOSPI", "KOSDAQ"):
        index_bars = index_store.window(cast("Literal['KOSPI', 'KOSDAQ']", security.market), start, as_of.date(), as_of)
    versions = [
        version
        for rcept_no in event.rcept_nos
        if (version := catalog.get_filing_asof(rcept_no, as_of)) is not None
    ]
    confound, _ = verified_event_confound(
        catalog, catalog.db_path.parent, lake, event.event_id, filing.corp_code,
        filing.receipt_date, frozenset(event.rcept_nos), as_of, policy,
    )
    study = study_buyback(event, versions, filing, stock_bars, index_bars, confound, as_of, policy)
    purpose = next((fact.value_text for fact in parsed.facts if fact.field == "ACQ_PPS"), None)
    return EventOutcome(
        event_id=event.event_id,
        receipt_date=filing.receipt_date,
        market=security.market,
        purpose_class=classify_purpose(purpose),
        amount_ratio=ratio,
        confound=confound,
        status=study.status,
        horizon_car=dict(study.horizon_car),
        intraday_excess=study.intraday_excess,
    )


def _summarize(values: Sequence[float]) -> tuple[float, float, float | None, float | None]:
    mean = statistics.fmean(values)
    median = statistics.median(values)
    if len(values) < 2:
        return mean, median, None, None
    stdev = statistics.stdev(values)
    t_stat = mean / (stdev / math.sqrt(len(values))) if stdev > 0 else None
    return mean, median, stdev, t_stat


def aggregate_outcomes(
    outcomes: Sequence[EventOutcome],
    as_of: datetime,
    policy_version: str,
    index_manifest_hash: str,
    linked_total: int,
) -> AggregateReport:
    """Summarize horizon CARs overall and by year, market, purpose, size bucket and confound state.

    Only events that carry a CAR for a horizon enter that horizon's statistics. Groups are pure partitions of the
    events, so the same event appears once per dimension. The t-statistic treats events as independent and does
    not adjust for cross-sectional correlation in clustered event dates; it is descriptive, not a significance claim.
    """
    dimensions: dict[str, Callable[[EventOutcome], str]] = {
        "all": lambda _: "all",
        "year": lambda o: str(o.receipt_date.year),
        "market": lambda o: o.market,
        "purpose": lambda o: o.purpose_class,
        "size": lambda o: ratio_bucket(o.amount_ratio),
        "confound": lambda o: o.confound,
    }
    stats: list[GroupStat] = []
    for dimension, keyer in dimensions.items():
        groups: dict[str, list[EventOutcome]] = {}
        for outcome in outcomes:
            groups.setdefault(keyer(outcome), []).append(outcome)
        for key in sorted(groups):
            for horizon in HORIZONS:
                values = [
                    float(car)
                    for item in groups[key]
                    if (car := item.horizon_car.get(horizon)) is not None
                ]
                if not values:
                    continue
                mean, median, stdev, t_stat = _summarize(values)
                stats.append(GroupStat(dimension, key, horizon, len(values), mean, median, stdev, t_stat))
    by_status: dict[str, int] = {}
    by_confound: dict[str, int] = {}
    for outcome in outcomes:
        by_status[outcome.status] = by_status.get(outcome.status, 0) + 1
        by_confound[outcome.confound] = by_confound.get(outcome.confound, 0) + 1
    coverage = Coverage(
        linked_total=linked_total,
        total=len(outcomes),
        with_any_car=sum(1 for o in outcomes if any(v is not None for v in o.horizon_car.values())),
        by_status=dict(sorted(by_status.items())),
        by_confound=dict(sorted(by_confound.items())),
    )
    return AggregateReport(as_of, policy_version, index_manifest_hash, coverage, tuple(stats))


def render_report_markdown(report: AggregateReport) -> str:
    """Render the aggregate as Markdown tables without recomputing any figure."""
    lines = [
        "# 자사주 취득 이벤트 종합 (descriptive)",
        "",
        f"- as_of: {report.as_of.isoformat()}",
        f"- study policy: {report.policy_version}",
        f"- index manifest: {report.index_manifest_hash}",
        f"- events: linked {report.coverage.linked_total}, measured {report.coverage.total}"
        f" (unresolved {report.coverage.linked_total - report.coverage.total}), CAR available {report.coverage.with_any_car}",
        "- CAR는 시장모형 초과수익 합계(소수)이며 인과 효과가 아니다. t값은 이벤트 독립을 가정한 참고값이다.",
        "",
        "## Coverage",
        "",
        "| status | n |",
        "|---|---:|",
        *[f"| {k} | {v} |" for k, v in report.coverage.by_status.items()],
        "",
        "| confound | n |",
        "|---|---:|",
        *[f"| {k} | {v} |" for k, v in report.coverage.by_confound.items()],
    ]
    for dimension in dict.fromkeys(item.dimension for item in report.stats):
        lines += ["", f"## {dimension}", "", "| key | h | n | mean | median | t |", "|---|---:|---:|---:|---:|---:|"]
        for item in report.stats:
            if item.dimension != dimension:
                continue
            t_text = "-" if item.t_stat is None else f"{item.t_stat:.2f}"
            lines.append(
                f"| {item.key} | {item.horizon} | {item.n} | {item.mean * 100:+.2f}% | {item.median * 100:+.2f}% | {t_text} |"
            )
    return "\n".join(lines) + "\n"


__all__ = [
    "AggregateReport",
    "EventOutcome",
    "GroupStat",
    "aggregate_outcomes",
    "classify_purpose",
    "collect_outcomes",
    "ratio_bucket",
    "render_report_markdown",
]
