"""Invariant guards for descriptive buyback event studies."""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from src.core.revisions import EventLink
from src.data.catalog import FilingVersion
from src.data.local_lake import MarketBar
from src.integrations.krx_index import IndexBar
from src.research.event_study import StudyPolicy, study_buyback

KST = ZoneInfo("Asia/Seoul")
BASE = date(2024, 6, 3)
SESSIONS = [BASE + timedelta(days=offset) for offset in range(16)]
FILING_DATE = SESSIONS[10]
SAFE = SESSIONS[11]
POLICY = StudyPolicy(estimation_start=-10, estimation_end=-2, min_pairs=5, horizons=(1,))


def _filing(rcept_no: str = "20240610000001", receipt: date = FILING_DATE) -> FilingVersion:
    observed = datetime(2024, 6, 11, 9, 0, tzinfo=KST)
    return FilingVersion(
        rcept_no=rcept_no,
        corp_code="01386916",
        receipt_date=receipt,
        report_name="주요사항보고서(자기주식취득결정)",
        stock_code="000001",
        raw_hash="a" * 64,
        first_observed_at=observed,
        knowledge_available_at=datetime(2024, 6, 11, 9, 0, tzinfo=KST),
        availability_mode="HISTORICAL_BACKFILL",
        correction_flag=False,
        withdrawal_flag=False,
        parent_rcept_no=None,
        link_status="ORIGINAL",
        time_precision="DATE_ONLY",
    )


def _event() -> EventLink:
    return EventLink("buyback:01386916:20240610000001", ("20240610000001",), "LINKED")


def _stock_hash(session: date) -> str:
    return hashlib.sha256(f"stock:{session.isoformat()}".encode()).hexdigest()


def _index_hash(session: date) -> str:
    return hashlib.sha256(f"index:{session.isoformat()}".encode()).hexdigest()


def _stock_bar(
    session: date,
    open_price: int,
    close_price: int,
    ret: float,
    share_factor: float = 1.0,
    gap: bool = False,
    state: str = "tradable",
) -> MarketBar:
    return MarketBar(
        instrument_id="KRX:000001",
        session=session,
        open=open_price,
        close=close_price,
        market_cap=700_000_000_000,
        listed_shares=10_000_000,
        trading_value=5_000_000_000,
        ret_price=ret,
        price_state=state,
        gap_before=gap,
        share_factor=share_factor,
        available_at=datetime(session.year, session.month, session.day, 18, 0, tzinfo=KST),
        source_hash=_stock_hash(session),
    )


def _index_bar(session: date, open_price: str, close_price: str) -> IndexBar:
    return IndexBar(
        market="KOSPI",
        session=session,
        open=Decimal(open_price),
        close=Decimal(close_price),
        source_hash=_index_hash(session),
        batch_available_at=datetime(session.year, session.month, session.day, 18, 0, tzinfo=KST),
    )


def _estimation_stock() -> list[MarketBar]:
    bars = []
    for position, session in enumerate(SESSIONS[:11]):
        ret = 0.02 if position % 2 == 0 else -0.01
        bars.append(_stock_bar(session, 70000 + position, 70500 + position, ret))
    return bars


def _estimation_index() -> list[IndexBar]:
    bars = []
    for position, session in enumerate(SESSIONS[:11]):
        level = Decimal(2700 + position * 3 + (position % 2))
        bars.append(_index_bar(session, str(level - 1), str(level)))
    return bars


def _series() -> tuple[list[MarketBar], list[IndexBar]]:
    stock = _estimation_stock()
    index = _estimation_index()
    stock.append(_stock_bar(SAFE, 44500, 43850, -0.0146))
    index.append(_index_bar(SAFE, "2767.62", "2784.06"))
    stock.append(_stock_bar(SESSIONS[12], 44000, 44100, 0.0023))
    index.append(_index_bar(SESSIONS[12], "2785.10", "2790.44"))
    return stock, index


def test_hand_calculated_intraday_excess() -> None:
    """44,500/43,850 stock against 2,767.62/2,784.06 KOSPI yields about -2.05pp with hashes."""
    stock, index = _series()
    as_of = datetime(2024, 6, 20, 18, 0, tzinfo=KST)
    result = study_buyback(_event(), [_filing()], _filing(), stock, index, "KNOWN_CLEAR", as_of, POLICY)
    assert result.first_safe_session == SAFE
    assert result.intraday_excess is not None
    assert abs(result.intraday_excess - Decimal("-0.0205")) < Decimal("0.001")
    assert _stock_hash(SAFE) in result.evidence_hashes
    assert _index_hash(SAFE) in result.evidence_hashes
    assert result.status == "OK"


def test_future_outcome_stays_pending() -> None:
    """An as-of before the safe-session close never reads the future bar."""
    stock, index = _series()
    as_of = datetime(SAFE.year, SAFE.month, SAFE.day, 9, 0, tzinfo=KST)
    result = study_buyback(_event(), [_filing()], _filing(), stock, index, "KNOWN_CLEAR", as_of, POLICY)
    assert result.first_safe_session == SAFE
    assert result.intraday_excess is None
    assert "PENDING" in result.reasons


def test_next_session_correction_withholds_first_day() -> None:
    """A date-only correction dated on the safe session makes first-day timing ambiguous."""
    stock, index = _series()
    correction = _filing("20240611000002", SAFE)
    as_of = datetime(2024, 6, 20, 18, 0, tzinfo=KST)
    result = study_buyback(_event(), [_filing(), correction], _filing(), stock, index, "KNOWN_CLEAR", as_of, POLICY)
    assert result.intraday_excess is None
    assert "TIME_AMBIGUOUS" in result.reasons


def test_insufficient_pairs_and_flat_market_are_not_estimable() -> None:
    """Too few pairs or zero market variance leave the market model unestimated."""
    stock, index = _series()
    as_of = datetime(2024, 6, 20, 18, 0, tzinfo=KST)
    strict = StudyPolicy(estimation_start=-10, estimation_end=-2, min_pairs=60, horizons=(1,))
    thin = study_buyback(_event(), [_filing()], _filing(), stock, index, "KNOWN_CLEAR", as_of, strict)
    assert thin.model_alpha is None
    assert thin.model_beta is None
    assert "INSUFFICIENT_PAIRS" in thin.reasons
    flat_index = [_index_bar(session, "2700.00", "2727.00") for session in SESSIONS[:13]]
    flat = study_buyback(_event(), [_filing()], _filing(), stock, flat_index, "KNOWN_CLEAR", as_of, POLICY)
    assert flat.model_alpha is None
    assert flat.model_beta is None
    assert "ZERO_MARKET_VARIANCE" in flat.reasons


def test_input_validation_rejects_naive_instant_and_unknown_confound() -> None:
    """Naive as-of instants and unknown confound states fail closed."""
    stock, index = _series()
    as_of = datetime(2024, 6, 20, 18, 0, tzinfo=KST)
    with pytest.raises(ValueError, match="timezone-aware"):
        study_buyback(_event(), [_filing()], _filing(), stock, index, "KNOWN_CLEAR", datetime(2024, 6, 20, 18, 0), POLICY)
    with pytest.raises(ValueError, match="confound"):
        study_buyback(_event(), [_filing()], _filing(), stock, index, "UNKNOWN", as_of, POLICY)  # type: ignore[arg-type]


def test_no_later_session_leaves_safe_session_unknown() -> None:
    """A filing on the last known session reports no safe session instead of inventing one."""
    stock, index = _series()
    late_filing = _filing("20240610000001", SESSIONS[-1])
    as_of = datetime(2024, 6, 20, 18, 0, tzinfo=KST)
    result = study_buyback(_event(), [late_filing], late_filing, stock, index, "KNOWN_CLEAR", as_of, POLICY)
    assert result.first_safe_session is None
    assert result.intraday_excess is None
    assert result.status == "NO_SAFE_SESSION"


def test_missing_and_halted_safe_bars_are_distinguished() -> None:
    """Absent safe bars and halted safe bars produce distinct refusal reasons."""
    stock, index = _series()
    as_of = datetime(2024, 6, 20, 18, 0, tzinfo=KST)
    gapped = [bar for bar in stock if bar.session != SAFE]
    missing = study_buyback(_event(), [_filing()], _filing(), gapped, index, "KNOWN_CLEAR", as_of, POLICY)
    assert missing.intraday_excess is None
    assert "MISSING_S0_BAR" in missing.reasons
    halted_bar = _stock_bar(SAFE, 44500, 43850, -0.0146, state="halted")
    halted = [bar for bar in stock if bar.session != SAFE] + [halted_bar]
    refused = study_buyback(_event(), [_filing()], _filing(), halted, index, "KNOWN_CLEAR", as_of, POLICY)
    assert refused.intraday_excess is None
    assert "HALTED_S0_BAR" in refused.reasons


def test_gapped_and_missing_estimation_pairs_are_omitted() -> None:
    """Discontinuous or index-missing estimation sessions are omitted, never filled."""
    stock, index = _series()
    gapped = _stock_bar(SESSIONS[3], 70003, 70503, 0.02, gap=True)
    stock = [bar for bar in stock if bar.session != SESSIONS[3]] + [gapped]
    index = [bar for bar in index if bar.session != SESSIONS[5]]
    as_of = datetime(2024, 6, 20, 18, 0, tzinfo=KST)
    result = study_buyback(_event(), [_filing()], _filing(), stock, index, "KNOWN_CLEAR", as_of, POLICY)
    assert result.model_alpha is not None
    assert result.omitted_sessions >= 2
    assert result.status == "OK"


def test_short_future_series_yields_pending_horizon() -> None:
    """A horizon reaching beyond known sessions stays pending, never zero."""
    stock, index = _series()
    wide = StudyPolicy(estimation_start=-10, estimation_end=-2, min_pairs=5, horizons=(5,))
    as_of = datetime(2024, 6, 20, 18, 0, tzinfo=KST)
    result = study_buyback(_event(), [_filing()], _filing(), stock, index, "KNOWN_CLEAR", as_of, wide)
    assert result.horizon_car[5] is None
    assert "PENDING_H5" in result.reasons


def test_confounded_window_is_labeled_not_hidden() -> None:
    """Known same-window filings label the study as confounded."""
    stock, index = _series()
    as_of = datetime(2024, 6, 20, 18, 0, tzinfo=KST)
    result = study_buyback(_event(), [_filing()], _filing(), stock, index, "KNOWN_CONFOUNDED", as_of, POLICY)
    assert result.intraday_excess is not None
    assert result.status == "CONFOUNDED"


def test_share_factor_break_refuses_affected_horizon() -> None:
    """An unvalidated share-factor gap refuses the horizon instead of smoothing it."""
    stock, index = _series()
    broken = _stock_bar(SESSIONS[12], 44000, 44100, 0.0023, share_factor=2.0)
    stock = [bar for bar in stock if bar.session != SESSIONS[12]] + [broken]
    as_of = datetime(2024, 6, 20, 18, 0, tzinfo=KST)
    result = study_buyback(_event(), [_filing()], _filing(), stock, index, "KNOWN_CLEAR", as_of, POLICY)
    assert result.horizon_car[1] is None
    assert any(reason.startswith("CORPORATE_ACTION_BREAK_") for reason in result.reasons)
