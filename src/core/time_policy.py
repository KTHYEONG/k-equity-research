"""Conservative availability boundary for date-only disclosures."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal
from zoneinfo import ZoneInfo

TimePrecision = Literal["DATE_ONLY", "OBSERVED_INSTANT"]

_KST = ZoneInfo("Asia/Seoul")


def knowledge_available_at(
    receipt_date: date,
    next_session: date,
    first_observed_at: datetime,
    mode: Literal["HISTORICAL_BACKFILL", "LIVE"],
) -> datetime:
    """Return the conservative information boundary for one filing. Historical immutable receipts use the next validated session open; live collection uses first actual observation without backdating. Raise ValueError for non-later sessions or naive observation timestamps."""
    if next_session <= receipt_date:
        raise ValueError("next session must be later than receipt date")
    if first_observed_at.tzinfo is None or first_observed_at.utcoffset() is None:
        raise ValueError("first observation must be timezone-aware")
    if mode == "HISTORICAL_BACKFILL":
        return datetime(
            next_session.year, next_session.month, next_session.day, 9, 0, 0, tzinfo=_KST
        )
    return first_observed_at


def is_known(available_at: datetime, as_of: datetime) -> bool:
    """Compare timezone-aware instants without treating filing date, retrieval date or fiscal period as interchangeable evidence of availability. Raise ValueError for naive instants."""
    if available_at.tzinfo is None or available_at.utcoffset() is None:
        raise ValueError("availability instant must be timezone-aware")
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as-of instant must be timezone-aware")
    return as_of >= available_at
