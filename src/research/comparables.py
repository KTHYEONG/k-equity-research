"""Pre-filing peer and analogue selection without look-ahead."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from src.core.revisions import EventLink
from src.data.local_lake import MarketBar, SecurityMatch
from src.research.materiality import MaterialityResult

_FEATURE_SESSIONS = 20
_MIN_OUTCOMES = 5
_TRADABLE_STATE = "tradable"


@dataclass(frozen=True, slots=True)
class PastEventProfile:
    """Frozen verified prior buyback event assembled only from prior research."""

    event_id: str
    receipt_date: date
    features_available_at: datetime
    outcome_available_at: datetime | None
    market: str
    prior_market_cap: Decimal
    planned_amount_ratio: Decimal | None
    first_safe_intraday_excess: Decimal | None


@dataclass(frozen=True, slots=True)
class ComparableSet:
    """Frozen observable peer and analogue selection for one target event."""

    event_id: str
    as_of: datetime
    feature_end_session: date
    peer_ids: tuple[str, ...]
    analogue_event_ids: tuple[str, ...]
    analogue_intraday_excess: tuple[Decimal, ...]
    analogue_quantiles: Mapping[str, Decimal | None]
    exclusions: Mapping[str, str]
    status: str


def _complete_bar(bar: MarketBar) -> bool:
    return (
        bar.price_state == _TRADABLE_STATE
        and bar.market_cap is not None
        and bar.market_cap > 0
        and bar.trading_value is not None
        and bar.trading_value >= 0
    )


def _decimal_median(values: Sequence[int]) -> Decimal:
    ordered = sorted(values)
    count = len(ordered)
    middle = count // 2
    if count % 2 == 1:
        return Decimal(ordered[middle])
    return (Decimal(ordered[middle - 1]) + Decimal(ordered[middle])) / Decimal(2)


def _quintiles(levels: Sequence[tuple[str, Decimal]]) -> dict[str, int]:
    ordered = sorted(levels, key=lambda item: (item[1], item[0]))
    count = len(ordered)
    return {instrument_id: (rank * 5) // count for rank, (instrument_id, _) in enumerate(ordered)}


def _nearest_rank(values: Sequence[Decimal], probability: Decimal) -> Decimal:
    ordered = sorted(values)
    raw_rank = int((probability * len(ordered)).to_integral_value(rounding="ROUND_CEILING"))
    rank = max(1, min(len(ordered), raw_rank))
    return ordered[rank - 1]


def select_comparables(
    target: SecurityMatch,
    target_event: EventLink,
    filing_date: date,
    target_materiality: MaterialityResult,
    universe: Mapping[str, Sequence[MarketBar]],
    past_events: Sequence[PastEventProfile],
    as_of: datetime,
) -> ComparableSet:
    """Select observable same-market securities and prior buyback events using pre-filing size and liquidity only. Return selection dates, exclusions and low-sample status; never backdate a current industry label."""
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as-of instant must be timezone-aware")
    exclusions: dict[str, str] = {}
    sizes: dict[str, Decimal] = {}
    liquidities: dict[str, Decimal] = {}
    for instrument_id, bars in universe.items():
        prior = sorted((bar for bar in bars if bar.session < filing_date), key=lambda bar: bar.session)
        sessions = {bar.session for bar in prior}
        if len(sessions) != len(prior):
            exclusions[instrument_id] = "AMBIGUOUS_SESSIONS"
            continue
        complete = [bar for bar in prior if _complete_bar(bar)]
        if len(complete) < _FEATURE_SESSIONS:
            exclusions[instrument_id] = "INSUFFICIENT_HISTORY"
            continue
        window = complete[-_FEATURE_SESSIONS:]
        median_cap = _decimal_median([bar.market_cap for bar in window if bar.market_cap is not None])
        median_value = _decimal_median([bar.trading_value for bar in window if bar.trading_value is not None])
        if median_cap <= 0 or median_value <= 0:
            exclusions[instrument_id] = "NONPOSITIVE_FEATURE"
            continue
        sizes[instrument_id] = median_cap
        liquidities[instrument_id] = median_value
    if target.instrument_id not in sizes:
        exclusions[target.instrument_id] = "TARGET_NO_FEATURES"
    peers: tuple[str, ...] = ()
    if target.instrument_id in sizes:
        size_bands = _quintiles(list(sizes.items()))
        liquidity_bands = _quintiles(list(liquidities.items()))
        peers = tuple(
            sorted(
                instrument_id
                for instrument_id in sizes
                if instrument_id != target.instrument_id
                and size_bands[instrument_id] == size_bands[target.instrument_id]
                and liquidity_bands[instrument_id] == liquidity_bands[target.instrument_id]
            )
        )
        for instrument_id in sorted(set(sizes) - set(peers) - {target.instrument_id}):
            exclusions.setdefault(instrument_id, "OUTSIDE_QUINTILE")

    latest: date | None = None
    for bars in universe.values():
        for bar in bars:
            if bar.session < filing_date and (latest is None or bar.session > latest):
                latest = bar.session
    feature_end = latest if latest is not None else filing_date - timedelta(days=1)

    seen: dict[str, PastEventProfile] = {}
    for profile in past_events:
        current = seen.get(profile.event_id)
        if current is None or (profile.receipt_date, profile.event_id) < (current.receipt_date, current.event_id):
            seen[profile.event_id] = profile
    target_ratio = target_materiality.amount_to_market_cap
    candidates: list[PastEventProfile] = []
    for event_id in sorted(seen):
        profile = seen[event_id]
        if event_id == target_event.event_id:
            continue
        if profile.receipt_date >= filing_date:
            exclusions[event_id] = "FUTURE_EVENT"
            continue
        if profile.market != target.market:
            exclusions[event_id] = "DIFFERENT_MARKET"
            continue
        if profile.features_available_at.date() >= filing_date:
            exclusions[event_id] = "FEATURES_NOT_YET_KNOWN"
            continue
        if profile.prior_market_cap <= 0:
            exclusions[event_id] = "MISSING_SIZE"
            continue
        if target_ratio is None or profile.planned_amount_ratio is None:
            exclusions[event_id] = "MISSING_RATIO"
            continue
        candidates.append(profile)
    analogues: list[PastEventProfile] = []
    if target.instrument_id in sizes and target_ratio is not None and candidates:
        size_levels = [(target.instrument_id, sizes[target.instrument_id])]
        size_levels.extend((profile.event_id, profile.prior_market_cap) for profile in candidates)
        ratio_levels = [(target.instrument_id, target_ratio)]
        ratio_levels.extend(
            (profile.event_id, profile.planned_amount_ratio)
            for profile in candidates
            if profile.planned_amount_ratio is not None
        )
        size_bands = _quintiles(size_levels)
        ratio_bands = _quintiles(ratio_levels)
        analogues = [
            profile
            for profile in candidates
            if size_bands.get(profile.event_id) == size_bands.get(target.instrument_id)
            and ratio_bands.get(profile.event_id) == ratio_bands.get(target.instrument_id)
        ]
        for profile in candidates:
            if profile not in analogues:
                exclusions.setdefault(profile.event_id, "OUTSIDE_QUINTILE")
    elif candidates:
        for profile in candidates:
            exclusions.setdefault(profile.event_id, "OUTSIDE_QUINTILE")
    analogues.sort(key=lambda profile: profile.event_id)
    outcomes = tuple(
        profile.first_safe_intraday_excess
        for profile in analogues
        if profile.first_safe_intraday_excess is not None
        and profile.outcome_available_at is not None
        and profile.outcome_available_at.date() < filing_date
    )
    if len(outcomes) >= _MIN_OUTCOMES:
        quantiles: dict[str, Decimal | None] = {
            "p25": _nearest_rank(outcomes, Decimal("0.25")),
            "median": _decimal_median_quantile(outcomes),
            "p75": _nearest_rank(outcomes, Decimal("0.75")),
        }
    else:
        quantiles = {"p25": None, "median": None, "p75": None}
    flags: list[str] = []
    if len(outcomes) < _MIN_OUTCOMES:
        flags.append("LOW_SAMPLE")
    if not peers:
        flags.append("EMPTY_PEERS")
    if not analogues:
        flags.append("NO_ANALOGUES")
    return ComparableSet(
        event_id=target_event.event_id,
        as_of=as_of,
        feature_end_session=feature_end,
        peer_ids=peers,
        analogue_event_ids=tuple(profile.event_id for profile in analogues),
        analogue_intraday_excess=outcomes,
        analogue_quantiles=quantiles,
        exclusions=exclusions,
        status="OK" if not flags else ";".join(flags),
    )


def _decimal_median_quantile(values: Sequence[Decimal]) -> Decimal:
    ordered = sorted(values)
    count = len(ordered)
    middle = count // 2
    if count % 2 == 1:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / Decimal(2)


__all__ = ["ComparableSet", "PastEventProfile", "select_comparables"]
