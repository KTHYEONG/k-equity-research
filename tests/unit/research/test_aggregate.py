"""Invariant guards for cross-event aggregation."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from src.research.aggregate import (
    EventOutcome,
    aggregate_outcomes,
    classify_purpose,
    ratio_bucket,
    render_report_markdown,
)

AS_OF = datetime(2026, 9, 29, 18, 32, tzinfo=ZoneInfo("Asia/Seoul"))


def _outcome(idx: int, car5: str | None, *, year: int = 2024, market: str = "KOSPI", ratio: str | None = "0.02") -> EventOutcome:
    return EventOutcome(
        event_id=f"buyback:{idx}",
        receipt_date=date(year, 3, 1),
        market=market,
        purpose_class="OTHER",
        amount_ratio=Decimal(ratio) if ratio is not None else None,
        confound="INCOMPLETE",
        status="CONFOUND_CHECK_INCOMPLETE",
        horizon_car={1: None, 5: Decimal(car5) if car5 is not None else None, 20: None},
        intraday_excess=None,
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (None, "UNAVAILABLE"),
        ("", "UNAVAILABLE"),
        ("이익 소각", "CANCELLATION"),
        ("임직원 보상 및 소각", "CANCELLATION"),
        ("임직원 성과급 지급", "COMPENSATION"),
        ("Stock Grant 부여", "COMPENSATION"),
        ("주가 안정", "OTHER"),
    ],
)
def test_purpose_classification(text: str | None, expected: str) -> None:
    assert classify_purpose(text) == expected


@pytest.mark.parametrize(
    ("ratio", "expected"),
    [
        (None, "UNAVAILABLE"),
        (Decimal("0.0009"), "<0.1%"),
        (Decimal("0.001"), "0.1-1%"),
        (Decimal("0.01"), "1-3%"),
        (Decimal("0.03"), ">=3%"),
    ],
)
def test_ratio_bucket_boundaries(ratio: Decimal | None, expected: str) -> None:
    assert ratio_bucket(ratio) == expected


def test_group_partition_and_denominators() -> None:
    rows = [_outcome(1, "0.02"), _outcome(2, "0.04", market="KOSDAQ"), _outcome(3, None, year=2025)]
    report = aggregate_outcomes(rows, AS_OF, "v1", "h" * 64, linked_total=4)
    assert report.coverage.linked_total == 4
    assert report.coverage.total == 3
    assert report.coverage.with_any_car == 2
    for dimension in ("all", "year", "market", "size", "confound"):
        counted = sum(s.n for s in report.stats if s.dimension == dimension and s.horizon == 5)
        assert counted == 2


def test_statistics_match_hand_computation() -> None:
    rows = [_outcome(1, "0.01"), _outcome(2, "0.03")]
    overall = next(s for s in aggregate_outcomes(rows, AS_OF, "v1", "h", 2).stats if s.dimension == "all" and s.horizon == 5)
    assert overall.mean == pytest.approx(0.02)
    assert overall.median == pytest.approx(0.02)
    assert overall.stdev == pytest.approx(0.0141421356)
    assert overall.t_stat == pytest.approx(2.0)


def test_single_observation_and_zero_variance_have_no_t_stat() -> None:
    single = aggregate_outcomes([_outcome(1, "0.01")], AS_OF, "v1", "h", 1).stats
    assert single[0].stdev is None
    assert single[0].t_stat is None
    flat = aggregate_outcomes([_outcome(1, "0.01"), _outcome(2, "0.01")], AS_OF, "v1", "h", 2).stats
    assert flat[0].t_stat is None


def test_empty_input_and_render() -> None:
    report = aggregate_outcomes([], AS_OF, "v1", "h", 0)
    assert report.stats == ()
    assert "linked 0, measured 0" in render_report_markdown(report)


def test_render_lists_each_group_with_percent_and_missing_t() -> None:
    rows = [_outcome(1, "0.01"), _outcome(2, "0.03", year=2025), _outcome(3, "-0.02", year=2025)]
    text = render_report_markdown(aggregate_outcomes(rows, AS_OF, "v1", "h" * 64, linked_total=3))
    assert "## year" in text
    assert "| 2024 | 5 | 1 | +1.00% | +1.00% | - |" in text
    assert "| 2025 | 5 | 2 | +0.50% | +0.50% |" in text
