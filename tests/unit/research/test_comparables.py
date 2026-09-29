"""Invariant guards for pre-filing peer and analogue selection."""

from __future__ import annotations

import inspect
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from src.core.revisions import EventLink
from src.data.local_lake import MarketBar, SecurityMatch
from src.research.comparables import PastEventProfile, select_comparables
from src.research.materiality import MaterialityResult

KST = ZoneInfo("Asia/Seoul")
FILING_DATE = date(2024, 6, 25)
PRIOR_SESSIONS = [date(2024, 5, 27) + timedelta(days=offset) for offset in range(28)]
AS_OF = datetime(2024, 6, 28, 18, 0, tzinfo=KST)


def _bar(session: date, cap: int, value: int) -> MarketBar:
    return MarketBar(
        instrument_id="",
        session=session,
        open=100,
        close=101,
        market_cap=cap,
        listed_shares=1_000_000,
        trading_value=value,
        ret_price=0.01,
        price_state="tradable",
        gap_before=False,
        share_factor=1.0,
        available_at=datetime(session.year, session.month, session.day, 18, 0, tzinfo=KST),
        source_hash="c" * 64,
    )


def _universe(sizes: dict[str, int], extra_tuesday: bool = False) -> dict[str, tuple[MarketBar, ...]]:
    universe: dict[str, tuple[MarketBar, ...]] = {}
    for instrument_id, size in sizes.items():
        bars = [MarketBar(
            instrument_id=instrument_id,
            session=session,
            open=100,
            close=101,
            market_cap=size,
            listed_shares=1_000_000,
            trading_value=size // 100,
            ret_price=0.01,
            price_state="tradable",
            gap_before=False,
            share_factor=1.0,
            available_at=datetime(session.year, session.month, session.day, 18, 0, tzinfo=KST),
            source_hash="e" * 64,
        ) for session in PRIOR_SESSIONS if session < FILING_DATE]
        if extra_tuesday:
            bars.append(
                MarketBar(
                    instrument_id=instrument_id,
                    session=FILING_DATE,
                    open=100,
                    close=200,
                    market_cap=size * 10,
                    listed_shares=1_000_000,
                    trading_value=size,
                    ret_price=0.9,
                    price_state="tradable",
                    gap_before=False,
                    share_factor=1.0,
                    available_at=datetime(2024, 6, 25, 18, 0, tzinfo=KST),
                    source_hash="f" * 64,
                )
            )
        universe[instrument_id] = tuple(bars)
    return universe


def _target(instrument_id: str = "KRX:000001") -> SecurityMatch:
    return SecurityMatch(instrument_id, "000001", "KOSPI", "KR7000001001", FILING_DATE - timedelta(days=1), "OK")


def _link() -> EventLink:
    return EventLink("buyback:01386916:anchor", ("anchor",), "LINKED")


def _materiality(ratio: Decimal | None = Decimal("0.05")) -> MaterialityResult:
    return MaterialityResult(ratio, Decimal("0.02"), ("d" * 64, "b" * 64), "OK")


def test_filing_day_move_cannot_change_peer_set() -> None:
    """A large filing-Tuesday move leaves peer selection identical to prior-only features."""
    sizes = {f"KRX:{number:06d}": (number + 1) * 1_000_000_000_000 for number in range(1, 11)}
    plain = select_comparables(_target("KRX:000001"), _link(), FILING_DATE, _materiality(), _universe(sizes), (), AS_OF)
    moved = select_comparables(_target("KRX:000001"), _link(), FILING_DATE, _materiality(), _universe(sizes, True), (), AS_OF)
    assert plain.peer_ids == moved.peer_ids
    assert "KRX:000001" not in moved.peer_ids
    assert moved.feature_end_session < FILING_DATE


def test_selection_uses_no_industry_label() -> None:
    """Peer selection accepts no current industry input by contract."""
    parameters = inspect.signature(select_comparables).parameters
    assert not any("indust" in name or "sector" in name for name in parameters)


def _prior(
    event_id: str,
    excess: Decimal | None = None,
    outcome_at: datetime | None = None,
) -> PastEventProfile:
    return PastEventProfile(
        event_id=event_id,
        receipt_date=date(2024, 5, 10),
        features_available_at=datetime(2024, 5, 11, 9, 0, tzinfo=KST),
        outcome_available_at=outcome_at,
        market="KOSPI",
        prior_market_cap=Decimal(5_000_000_000_000),
        planned_amount_ratio=Decimal("0.05"),
        first_safe_intraday_excess=excess,
    )


def test_duplicate_revision_counts_once() -> None:
    """Original and correction profiles of one past event yield a single analogue."""
    sizes = {"M:T": 5_000_000_000_000, **{f"z:{number:02d}": 5_000_000_000_000 for number in range(1, 9)}}
    late = datetime(2024, 5, 14, 18, 0, tzinfo=KST)
    profiles = [
        _prior("buyback:111:orig", Decimal("0.01"), late),
        _prior("buyback:111:orig", Decimal("0.012"), late),
        *(_prior(f"z:{number:02d}") for number in range(1, 9)),
    ]
    result = select_comparables(_target("M:T"), _link(), FILING_DATE, _materiality(), _universe(sizes), profiles, AS_OF)
    assert result.analogue_event_ids.count("buyback:111:orig") == 1


def test_input_validation_and_median_helpers() -> None:
    """Naive instants fail closed and median helpers cover odd and even counts."""
    from src.research.comparables import _decimal_median, _decimal_median_quantile

    sizes = {f"KRX:{number:06d}": 5_000_000_000_000 for number in range(1, 6)}
    with pytest.raises(ValueError, match="timezone-aware"):
        select_comparables(
            _target(), _link(), FILING_DATE, _materiality(), _universe(sizes), (),
            datetime(2024, 6, 28, 18, 0),
        )
    assert _decimal_median([3, 1, 2]) == Decimal(2)
    assert _decimal_median([4, 1, 3, 2]) == Decimal("2.5")
    assert _decimal_median_quantile([Decimal(3), Decimal(1), Decimal(2)]) == Decimal(2)
    assert _decimal_median_quantile([Decimal(4), Decimal(1), Decimal(3), Decimal(2)]) == Decimal("2.5")


def test_duplicate_sessions_and_zero_value_are_excluded() -> None:
    """Ambiguous sessions and zero medians exclude a security with reasons."""
    sizes = {f"KRX:{number:06d}": 5_000_000_000_000 for number in range(1, 6)}
    universe = _universe(sizes)
    doubled = universe["KRX:000002"] + universe["KRX:000002"][:1]
    universe["KRX:000002"] = doubled
    flat = tuple(
        MarketBar(
            instrument_id="KRX:000003",
            session=bar.session,
            open=bar.open,
            close=bar.close,
            market_cap=bar.market_cap,
            listed_shares=bar.listed_shares,
            trading_value=0,
            ret_price=bar.ret_price,
            price_state=bar.price_state,
            gap_before=bar.gap_before,
            share_factor=bar.share_factor,
            available_at=bar.available_at,
            source_hash=bar.source_hash,
        )
        for bar in universe["KRX:000003"]
    )
    universe["KRX:000003"] = flat
    result = select_comparables(_target("KRX:000001"), _link(), FILING_DATE, _materiality(), universe, (), AS_OF)
    assert result.exclusions.get("KRX:000002") == "AMBIGUOUS_SESSIONS"
    assert result.exclusions.get("KRX:000003") == "NONPOSITIVE_FEATURE"


def test_analogue_filters_log_every_exclusion() -> None:
    """Future, foreign-market, unknowable, unsized and ratio-less priors are logged."""
    sizes = {f"KRX:{number:06d}": 5_000_000_000_000 for number in range(1, 6)}
    base = _prior("buyback:base", Decimal("0.01"), datetime(2024, 6, 20, 18, 0, tzinfo=KST))
    profiles = [
        PastEventProfile("buyback:01386916:anchor", base.receipt_date, base.features_available_at,
                         base.outcome_available_at, base.market, base.prior_market_cap,
                         base.planned_amount_ratio, base.first_safe_intraday_excess),
        PastEventProfile("buyback:future", FILING_DATE, base.features_available_at,
                         base.outcome_available_at, base.market, base.prior_market_cap,
                         base.planned_amount_ratio, base.first_safe_intraday_excess),
        PastEventProfile("buyback:other", base.receipt_date, base.features_available_at,
                         base.outcome_available_at, "KOSDAQ", base.prior_market_cap,
                         base.planned_amount_ratio, base.first_safe_intraday_excess),
        PastEventProfile("buyback:late", base.receipt_date, AS_OF,
                         base.outcome_available_at, base.market, base.prior_market_cap,
                         base.planned_amount_ratio, base.first_safe_intraday_excess),
        PastEventProfile("buyback:nosize", base.receipt_date, base.features_available_at,
                         base.outcome_available_at, base.market, Decimal(0),
                         base.planned_amount_ratio, base.first_safe_intraday_excess),
        PastEventProfile("buyback:noratio", base.receipt_date, base.features_available_at,
                         base.outcome_available_at, base.market, base.prior_market_cap,
                         None, base.first_safe_intraday_excess),
    ]
    result = select_comparables(_target("KRX:000001"), _link(), FILING_DATE, _materiality(), _universe(sizes), profiles, AS_OF)
    assert "buyback:01386916:anchor" not in result.analogue_event_ids
    assert result.exclusions.get("buyback:future") == "FUTURE_EVENT"
    assert result.exclusions.get("buyback:other") == "DIFFERENT_MARKET"
    assert result.exclusions.get("buyback:late") == "FEATURES_NOT_YET_KNOWN"
    assert result.exclusions.get("buyback:nosize") == "MISSING_SIZE"
    assert result.exclusions.get("buyback:noratio") == "MISSING_RATIO"


def test_missing_target_features_and_ratios_stay_explicit() -> None:
    """A target absent from the universe cannot anchor peers or ratio analogues."""
    sizes = {f"KRX:{number:06d}": 5_000_000_000_000 for number in range(2, 7)}
    profiles = [_prior("buyback:only", Decimal("0.01"), datetime(2024, 6, 20, 18, 0, tzinfo=KST))]
    result = select_comparables(_target("KRX:000001"), _link(), FILING_DATE, _materiality(), _universe(sizes), profiles, AS_OF)
    assert result.peer_ids == ()
    assert result.exclusions.get("KRX:000001") == "TARGET_NO_FEATURES"
    assert result.exclusions.get("buyback:only") == "OUTSIDE_QUINTILE"
    assert "NO_ANALOGUES" in result.status


def test_new_listing_is_excluded_with_reason() -> None:
    """Fewer than 20 prior sessions produce an explicit exclusion reason."""
    sizes = {f"KRX:{number:06d}": 5_000_000_000_000 for number in range(1, 6)}
    universe = _universe(sizes)
    newcomer = tuple(_bar(session, 5_000_000_000_000, 50_000_000_000) for session in PRIOR_SESSIONS[-5:] if session < FILING_DATE)
    newcomer = tuple(
        MarketBar(
            instrument_id="KRX:999999",
            session=bar.session,
            open=bar.open,
            close=bar.close,
            market_cap=bar.market_cap,
            listed_shares=bar.listed_shares,
            trading_value=bar.trading_value,
            ret_price=bar.ret_price,
            price_state=bar.price_state,
            gap_before=bar.gap_before,
            share_factor=bar.share_factor,
            available_at=bar.available_at,
            source_hash=bar.source_hash,
        )
        for bar in newcomer
    )
    universe["KRX:999999"] = newcomer
    result = select_comparables(_target("KRX:000001"), _link(), FILING_DATE, _materiality(), universe, (), AS_OF)
    assert result.exclusions.get("KRX:999999") == "INSUFFICIENT_HISTORY"
    assert "KRX:999999" not in result.peer_ids


def test_analogue_distribution_uses_only_earlier_outcomes() -> None:
    """Six earlier outcomes define quantiles while a filing-date outcome is excluded."""
    sizes = {"AAA": 1_000_000_000_000, **{f"EVT:{number:04d}": 1_000_000_000_000 for number in range(1, 36)}}
    universe = _universe(sizes)
    profiles = []
    for number in range(1, 36):
        early = number <= 6
        outcome_at = (
            datetime(2024, 6, 20, 18, 0, tzinfo=KST)
            if early
            else (datetime(2024, 6, 25, 9, 0, tzinfo=KST) if number == 7 else None)
        )
        profiles.append(
            PastEventProfile(
                event_id=f"EVT:{number:04d}",
                receipt_date=date(2024, 6, 10),
                features_available_at=datetime(2024, 6, 11, 9, 0, tzinfo=KST),
                outcome_available_at=outcome_at,
                market="KOSPI",
                prior_market_cap=Decimal(1_000_000_000_000),
                planned_amount_ratio=Decimal("0.05"),
                first_safe_intraday_excess=Decimal(f"0.0{number}") if number <= 7 else None,
            )
        )
    result = select_comparables(_target("AAA"), _link(), FILING_DATE, _materiality(), universe, profiles, AS_OF)
    assert len(result.analogue_intraday_excess) == 6
    assert result.analogue_quantiles["median"] == Decimal("0.035")
    assert "LOW_SAMPLE" not in result.status


def _hex_tag(number: int) -> str:
    return format(number, "064x")


def _lined_prior(event_id: str, number: int, excess: Decimal | None, outcome_at: datetime | None) -> PastEventProfile:
    return PastEventProfile(
        event_id=event_id,
        receipt_date=date(2024, 5, 10),
        features_available_at=datetime(2024, 5, 11, 9, 0, tzinfo=KST),
        outcome_available_at=outcome_at,
        market="KOSPI",
        prior_market_cap=Decimal(5_000_000_000_000),
        planned_amount_ratio=Decimal("0.05"),
        first_safe_intraday_excess=excess,
        source_hashes=(_hex_tag(number), _hex_tag(number + 1000), _hex_tag(number + 2000), _hex_tag(number + 3000)),
    )


def test_observation_pairing_keeps_unavailable_selection() -> None:
    """Observed pairs match eligible excess entries while the unavailable selection stays listed."""
    sizes = {"AAA": 1_000_000_000_000, **{f"EVT:{number:04d}": 1_000_000_000_000 for number in range(1, 36)}}
    universe = _universe(sizes)
    late = datetime(2024, 5, 14, 18, 0, tzinfo=KST)
    profiles: list[PastEventProfile] = []
    for number in range(1, 36):
        if number <= 6:
            profiles.append(_lined_prior(f"EVT:{number:04d}", number, Decimal(f"0.0{number}"), late))
        elif number == 7:
            profiles.append(_lined_prior(f"EVT:{number:04d}", number, None, None))
        else:
            profiles.append(_lined_prior(f"EVT:{number:04d}", number, None, None))
    result = select_comparables(_target("AAA"), _link(), FILING_DATE, _materiality(), universe, profiles, AS_OF)
    assert len(result.analogue_observations) == len(result.analogue_intraday_excess) == 6
    for observation, amount in zip(result.analogue_observations, result.analogue_intraday_excess, strict=True):
        assert observation.intraday_excess == amount
        assert isinstance(observation.intraday_excess, Decimal)
    assert "EVT:0007" in result.analogue_event_ids
    assert all(obs.event_id != "EVT:0007" for obs in result.analogue_observations)


def test_selection_lineage_covers_features_and_candidates() -> None:
    """Sorted lineage contains every used target feature and candidate hash exactly once."""
    sizes = {f"KRX:{number:06d}": 5_000_000_000_000 for number in range(1, 6)}
    universe = _universe(sizes)
    target_bars: list[MarketBar] = []
    for position, bar in enumerate(universe["KRX:000001"]):
        target_bars.append(
            MarketBar(
                instrument_id=bar.instrument_id,
                session=bar.session,
                open=bar.open,
                close=bar.close,
                market_cap=bar.market_cap,
                listed_shares=bar.listed_shares,
                trading_value=bar.trading_value,
                ret_price=bar.ret_price,
                price_state=bar.price_state,
                gap_before=bar.gap_before,
                share_factor=bar.share_factor,
                available_at=bar.available_at,
                source_hash=_hex_tag(5000 + position),
            )
        )
    universe["KRX:000001"] = tuple(target_bars)
    profiles = [_lined_prior(f"cand:{number:02d}", 7000 + number, None, None) for number in range(1, 4)]
    materiality = _materiality()
    result = select_comparables(_target("KRX:000001"), _link(), FILING_DATE, materiality, universe, profiles, AS_OF)
    used_features = {bar.source_hash for bar in target_bars[-20:]}
    expected = set(used_features) | set(materiality.source_hashes)
    for profile in profiles:
        expected.update(profile.source_hashes)
    assert result.selection_source_hashes == tuple(sorted(expected))
    assert len(result.selection_source_hashes) == len(set(result.selection_source_hashes))


def test_future_outcome_excluded_from_distribution() -> None:
    """A prior outcome available on or after filing stays out of the observed distribution."""
    sizes = {"AAA": 1_000_000_000_000, **{f"EVT:{number:04d}": 1_000_000_000_000 for number in range(1, 36)}}
    universe = _universe(sizes)
    profiles = []
    for number in range(1, 36):
        if number <= 6:
            outcome_at: datetime | None = datetime(2024, 6, 20, 18, 0, tzinfo=KST)
            excess: Decimal | None = Decimal(f"0.0{number}")
        elif number == 7:
            outcome_at = datetime(2024, 6, 25, 9, 0, tzinfo=KST)
            excess = Decimal("0.07")
        else:
            outcome_at = None
            excess = None
        profiles.append(
            PastEventProfile(
                event_id=f"EVT:{number:04d}",
                receipt_date=date(2024, 6, 10),
                features_available_at=datetime(2024, 6, 11, 9, 0, tzinfo=KST),
                outcome_available_at=outcome_at,
                market="KOSPI",
                prior_market_cap=Decimal(1_000_000_000_000),
                planned_amount_ratio=Decimal("0.05"),
                first_safe_intraday_excess=excess,
                source_hashes=(_hex_tag(number), _hex_tag(number + 1000), _hex_tag(number + 2000), _hex_tag(number + 3000)),
            )
        )
    result = select_comparables(_target("AAA"), _link(), FILING_DATE, _materiality(), universe, profiles, AS_OF)
    assert "EVT:0007" in result.analogue_event_ids
    assert [obs.event_id for obs in result.analogue_observations] == [f"EVT:{number:04d}" for number in range(1, 7)]
    assert Decimal("0.07") not in result.analogue_intraday_excess
    assert all(_hex_tag(7) not in obs.source_hashes for obs in result.analogue_observations)


def test_lineage_preserves_established_quantiles() -> None:
    """Eight-outcome lineage selection keeps IDs, order, quantiles, and status unchanged."""
    sizes = {"AAA": 1_000_000_000_000, **{f"EVT:{number:04d}": 1_000_000_000_000 for number in range(1, 42)}}
    universe = _universe(sizes)
    bare: list[PastEventProfile] = []
    lined: list[PastEventProfile] = []
    for number in range(1, 42):
        has_outcome = number <= 8
        outcome_at = datetime(2024, 6, 20, 18, 0, tzinfo=KST) if has_outcome else None
        excess = Decimal(f"0.0{number}") if has_outcome else None
        bare.append(
            PastEventProfile(
                event_id=f"EVT:{number:04d}",
                receipt_date=date(2024, 6, 10),
                features_available_at=datetime(2024, 6, 11, 9, 0, tzinfo=KST),
                outcome_available_at=outcome_at,
                market="KOSPI",
                prior_market_cap=Decimal(1_000_000_000_000),
                planned_amount_ratio=Decimal("0.05"),
                first_safe_intraday_excess=excess,
            )
        )
        lined.append(
            PastEventProfile(
                event_id=f"EVT:{number:04d}",
                receipt_date=date(2024, 6, 10),
                features_available_at=datetime(2024, 6, 11, 9, 0, tzinfo=KST),
                outcome_available_at=outcome_at,
                market="KOSPI",
                prior_market_cap=Decimal(1_000_000_000_000),
                planned_amount_ratio=Decimal("0.05"),
                first_safe_intraday_excess=excess,
                source_hashes=(_hex_tag(number), _hex_tag(number + 1000), _hex_tag(number + 2000), _hex_tag(number + 3000)),
            )
        )
    plain = select_comparables(_target("AAA"), _link(), FILING_DATE, _materiality(), universe, bare, AS_OF)
    proved = select_comparables(_target("AAA"), _link(), FILING_DATE, _materiality(), universe, lined, AS_OF)
    assert len(proved.analogue_intraday_excess) == 8
    assert proved.analogue_event_ids == plain.analogue_event_ids
    assert proved.analogue_intraday_excess == plain.analogue_intraday_excess
    assert proved.analogue_quantiles == plain.analogue_quantiles
    assert proved.status == plain.status
    assert [obs.event_id for obs in proved.analogue_observations] == [
        f"EVT:{number:04d}" for number in range(1, 9)
    ]
    assert proved.analogue_quantiles["p25"] == Decimal("0.02")
    assert proved.analogue_quantiles["median"] == Decimal("0.045")
    assert proved.analogue_quantiles["p75"] == Decimal("0.06")
