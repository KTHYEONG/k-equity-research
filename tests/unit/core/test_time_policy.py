"""Invariant guards for conservative filing availability."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from src.core.time_policy import is_known, knowledge_available_at

KST = ZoneInfo("Asia/Seoul")


def test_date_only_boundary_uses_next_session_open() -> None:
    """Friday filing becomes eligible at Monday 09:00 KST, not Friday open."""
    receipt = date(2024, 5, 31)
    monday = date(2024, 6, 3)
    observed = datetime(2026, 1, 5, 12, 0, tzinfo=KST)
    available = knowledge_available_at(receipt, monday, observed, "HISTORICAL_BACKFILL")
    assert available == datetime(2024, 6, 3, 9, 0, tzinfo=KST)
    assert available != datetime(2024, 5, 31, 9, 0, tzinfo=KST)


def test_historical_retrieval_keeps_policy_boundary() -> None:
    """2024 receipt downloaded in 2026 replays at its 2024 next-session boundary."""
    receipt = date(2024, 5, 31)
    monday = date(2024, 6, 3)
    observed_2026 = datetime(2026, 2, 10, 15, 30, tzinfo=KST)
    available = knowledge_available_at(receipt, monday, observed_2026, "HISTORICAL_BACKFILL")
    assert available == datetime(2024, 6, 3, 9, 0, tzinfo=KST)
    assert available != observed_2026
    assert is_known(available, datetime(2024, 6, 3, 9, 0, tzinfo=KST))
    assert not is_known(available, datetime(2024, 6, 2, 12, 0, tzinfo=KST))


def test_live_observation_uses_first_batch() -> None:
    """Same-day 18:30 batch is usable at 18:30 while safe price session stays next day."""
    receipt = date(2024, 6, 3)
    next_session = date(2024, 6, 4)
    batch = datetime(2024, 6, 3, 18, 30, tzinfo=KST)
    live = knowledge_available_at(receipt, next_session, batch, "LIVE")
    assert live == batch
    assert is_known(live, batch)
    assert not is_known(live, datetime(2024, 6, 3, 18, 29, tzinfo=KST))
    policy = knowledge_available_at(receipt, next_session, batch, "HISTORICAL_BACKFILL")
    assert policy == datetime(2024, 6, 4, 9, 0, tzinfo=KST)


def test_invalid_clocks_raise() -> None:
    """Naive observation or non-later session fails closed."""
    with pytest.raises(ValueError, match="later"):
        knowledge_available_at(
            date(2024, 6, 3), date(2024, 6, 3), datetime(2024, 6, 3, 9, 0, tzinfo=KST), "LIVE"
        )
    with pytest.raises(ValueError, match="timezone"):
        knowledge_available_at(date(2024, 6, 3), date(2024, 6, 4), datetime(2024, 6, 4, 9, 0), "LIVE")
    with pytest.raises(ValueError, match="timezone"):
        is_known(datetime(2024, 6, 4, 9, 0), datetime(2024, 6, 4, 9, 0, tzinfo=KST))
    with pytest.raises(ValueError, match="timezone"):
        is_known(
            datetime(2024, 6, 4, 9, 0, tzinfo=KST), datetime(2024, 6, 4, 9, 0),
        )
