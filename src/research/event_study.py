"""Descriptive post-filing price-path measurement over validated sessions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from src.core.revisions import EventLink
from src.data.catalog import FilingVersion
from src.data.local_lake import MarketBar
from src.integrations.krx_index import IndexBar

ConfoundCheck = Literal["KNOWN_CLEAR", "KNOWN_CONFOUNDED", "INCOMPLETE"]

_TRADABLE_STATE = "tradable"


@dataclass(frozen=True, slots=True)
class StudyPolicy:
    """Explicit versioned event-study configuration."""

    estimation_start: int = -120
    estimation_end: int = -21
    min_pairs: int = 60
    horizons: tuple[int, ...] = (1, 5, 20)
    version: str = "v1"


@dataclass(frozen=True, slots=True)
class StudyResult:
    """Frozen descriptive outcome for one filing version's price path."""

    event_id: str
    active_rcept_no: str
    as_of: datetime
    first_safe_session: date | None
    intraday_excess: Decimal | None
    model_alpha: Decimal | None
    model_beta: Decimal | None
    horizon_car: Mapping[int, Decimal | None]
    status: str
    reasons: tuple[str, ...]
    evidence_hashes: tuple[str, ...]
    policy_version: str
    omitted_sessions: int


def _index_sessions(stock: Sequence[MarketBar], index: Sequence[IndexBar]) -> dict[date, int]:
    ordered = sorted({bar.session for bar in stock} | {bar.session for bar in index})
    return {session: rank for rank, session in enumerate(ordered)}


def _dedupe_stock(stock: Sequence[MarketBar]) -> dict[date, MarketBar | None]:
    grouped: dict[date, list[MarketBar]] = {}
    for bar in stock:
        grouped.setdefault(bar.session, []).append(bar)
    return {session: bars[0] if len(bars) == 1 else None for session, bars in grouped.items()}


def _dedupe_index(index: Sequence[IndexBar]) -> dict[date, IndexBar | None]:
    grouped: dict[date, list[IndexBar]] = {}
    for bar in index:
        grouped.setdefault(bar.session, []).append(bar)
    return {session: bars[0] if len(bars) == 1 else None for session, bars in grouped.items()}


def _is_ambiguous(versions: Sequence[FilingVersion], filing: FilingVersion, safe: date) -> bool:
    for version in versions:
        if version.rcept_no == filing.rcept_no or version.time_precision != "DATE_ONLY":
            continue
        if filing.receipt_date < version.receipt_date <= safe:
            return True
    return False


def _intraday_excess(stock_bar: MarketBar, index_bar: IndexBar) -> Decimal:
    stock_ret = Decimal(stock_bar.close or 0) / Decimal(stock_bar.open or 0) - Decimal(1)
    index_ret = index_bar.close / index_bar.open - Decimal(1)
    return stock_ret - index_ret


def _valid_intraday_stock(bar: MarketBar | None, as_of: datetime) -> bool:
    return (
        bar is not None
        and bar.available_at <= as_of
        and bar.price_state == _TRADABLE_STATE
        and bar.open is not None
        and bar.close is not None
        and bar.open > 0
        and bar.close > 0
    )


def _valid_intraday_index(bar: IndexBar | None, as_of: datetime) -> bool:
    return bar is not None and bar.batch_available_at <= as_of and bar.open > 0 and bar.close > 0


def _paired_returns(
    session: date,
    previous: date | None,
    stock_by: Mapping[date, MarketBar | None],
    index_by: Mapping[date, IndexBar | None],
    as_of: datetime,
) -> tuple[Decimal, Decimal, tuple[str, str]] | None:
    stock_bar = stock_by.get(session)
    if (
        stock_bar is None
        or stock_bar.available_at > as_of
        or stock_bar.price_state != _TRADABLE_STATE
        or stock_bar.gap_before
        or stock_bar.ret_price is None
        or stock_bar.open is None
        or stock_bar.close is None
        or stock_bar.open <= 0
        or stock_bar.close <= 0
        or stock_bar.share_factor is None
    ):
        return None
    prior_stock = stock_by.get(previous) if previous is not None else None
    if prior_stock is None or prior_stock.share_factor is None or prior_stock.share_factor != stock_bar.share_factor:
        return None
    index_bar = index_by.get(session)
    prior_index = index_by.get(previous) if previous is not None else None
    if (
        index_bar is None
        or prior_index is None
        or index_bar.batch_available_at > as_of
        or index_bar.close <= 0
        or prior_index.close <= 0
    ):
        return None
    stock_ret = Decimal(str(stock_bar.ret_price))
    index_ret = index_bar.close / prior_index.close - Decimal(1)
    return (stock_ret, index_ret, (stock_bar.source_hash, index_bar.source_hash))


def _fit_market_model(pairs: Sequence[tuple[Decimal, Decimal]]) -> tuple[Decimal, Decimal] | None:
    count = len(pairs)
    mean_stock = sum((item[0] for item in pairs), Decimal(0)) / count
    mean_index = sum((item[1] for item in pairs), Decimal(0)) / count
    variance = sum(((item[1] - mean_index) ** 2 for item in pairs), Decimal(0))
    if variance == 0:
        return None
    covariance = sum(((item[0] - mean_stock) * (item[1] - mean_index) for item in pairs), Decimal(0))
    beta = covariance / variance
    return (mean_stock - beta * mean_index, beta)


def study_buyback(
    event: EventLink,
    versions: Sequence[FilingVersion],
    filing: FilingVersion,
    stock: Sequence[MarketBar],
    index: Sequence[IndexBar],
    confound_check: ConfoundCheck,
    as_of: datetime,
    policy: StudyPolicy,
) -> StudyResult:
    """Measure the observable post-filing price path using only eligible sessions, validated prices and pre-event calibration. Return explicit refusal reasons for temporal ambiguity, missing data, halts, corporate actions or confounding; never label a descriptive return as a causal disclosure effect."""
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as-of instant must be timezone-aware")
    if confound_check not in ("KNOWN_CLEAR", "KNOWN_CONFOUNDED", "INCOMPLETE"):
        raise ValueError("unknown confound check state")
    reasons: list[str] = []
    evidence: list[str] = []
    omitted = 0
    if confound_check == "KNOWN_CONFOUNDED":
        reasons.append("CONFOUNDED")
    elif confound_check == "INCOMPLETE":
        reasons.append("CONFOUND_CHECK_INCOMPLETE")

    session_rank = _index_sessions(stock, index)
    ordered = sorted(session_rank)
    stock_by = _dedupe_stock(stock)
    index_by = _dedupe_index(index)
    later = [session for session in ordered if session > filing.receipt_date]
    if not later:
        reasons.append("NO_SAFE_SESSION")
        return StudyResult(
            event_id=event.event_id,
            active_rcept_no=filing.rcept_no,
            as_of=as_of,
            first_safe_session=None,
            intraday_excess=None,
            model_alpha=None,
            model_beta=None,
            horizon_car=dict.fromkeys(policy.horizons, None),
            status="NO_SAFE_SESSION",
            reasons=tuple(reasons),
            evidence_hashes=(),
            policy_version=policy.version,
            omitted_sessions=0,
        )
    safe = later[0]
    safe_rank = session_rank[safe]
    previous_of: dict[date, date | None] = {
        session: ordered[rank - 1] if rank > 0 else None for session, rank in session_rank.items()
    }

    ambiguous = _is_ambiguous(versions, filing, safe)
    intraday: Decimal | None = None
    if ambiguous:
        reasons.append("TIME_AMBIGUOUS")
    else:
        stock_bar = stock_by.get(safe)
        index_bar = index_by.get(safe)
        if (
            stock_bar is not None
            and index_bar is not None
            and _valid_intraday_stock(stock_bar, as_of)
            and _valid_intraday_index(index_bar, as_of)
        ):
            intraday = _intraday_excess(stock_bar, index_bar)
            evidence.extend((stock_bar.source_hash, index_bar.source_hash))
        elif as_of.date() <= safe:
            reasons.append("PENDING")
        elif stock_bar is None or index_bar is None:
            reasons.append("MISSING_S0_BAR")
            omitted += 1
        else:
            reasons.append("HALTED_S0_BAR")
            omitted += 1

    estimation = [
        session
        for session, rank in session_rank.items()
        if policy.estimation_start <= rank - safe_rank <= policy.estimation_end
    ]
    pairs: list[tuple[Decimal, Decimal]] = []
    for session in sorted(estimation):
        paired = _paired_returns(session, previous_of[session], stock_by, index_by, as_of)
        if paired is None:
            omitted += 1
            continue
        pairs.append((paired[0], paired[1]))
        evidence.extend(paired[2])
    alpha: Decimal | None = None
    beta: Decimal | None = None
    if not pairs or len(pairs) < policy.min_pairs:
        reasons.append("INSUFFICIENT_PAIRS")
    else:
        fitted = _fit_market_model(pairs)
        if fitted is None:
            reasons.append("ZERO_MARKET_VARIANCE")
        else:
            alpha, beta = fitted

    horizon_car: dict[int, Decimal | None] = {}
    for horizon in policy.horizons:
        following = [session for session in ordered if session > safe][:horizon]
        if len(following) < horizon:
            reasons.append(f"PENDING_H{horizon}")
            horizon_car[horizon] = None
            continue
        if alpha is None or beta is None:
            reasons.append(f"NOT_ESTIMABLE_H{horizon}")
            horizon_car[horizon] = None
            continue
        abnormal: list[Decimal] = []
        refused: str | None = None
        for session in following:
            paired = _paired_returns(session, previous_of[session], stock_by, index_by, as_of)
            if paired is None:
                stock_candidate = stock_by.get(session)
                index_candidate = index_by.get(session)
                if as_of.date() <= session or (
                    (stock_candidate is not None
                    and stock_candidate.available_at > as_of)
                    or (index_candidate is not None
                    and index_candidate.batch_available_at > as_of)
                ):
                    refused = f"PENDING_H{horizon}"
                else:
                    refused = f"CORPORATE_ACTION_BREAK_H{horizon}"
                    omitted += 1
                break
            abnormal.append(paired[0] - (alpha + beta * paired[1]))
            evidence.extend(paired[2])
        if refused is not None:
            reasons.append(refused)
            horizon_car[horizon] = None
        else:
            horizon_car[horizon] = sum(abnormal, Decimal(0))

    status = "OK"
    for candidate in (
        "NO_SAFE_SESSION",
        "TIME_AMBIGUOUS",
        "PENDING",
        *[reason for reason in reasons if reason.startswith("PENDING_H")],
        "INSUFFICIENT_PAIRS",
        "ZERO_MARKET_VARIANCE",
        "MISSING_S0_BAR",
        "HALTED_S0_BAR",
        *[reason for reason in reasons if reason.startswith("CORPORATE_ACTION_BREAK_")],
        *[reason for reason in reasons if reason.startswith("NOT_ESTIMABLE_")],
        "CONFOUNDED",
        "CONFOUND_CHECK_INCOMPLETE",
    ):
        if candidate in reasons:
            status = candidate
            break
    return StudyResult(
        event_id=event.event_id,
        active_rcept_no=filing.rcept_no,
        as_of=as_of,
        first_safe_session=safe,
        intraday_excess=intraday,
        model_alpha=alpha,
        model_beta=beta,
        horizon_car=horizon_car,
        status=status,
        reasons=tuple(reasons),
        evidence_hashes=tuple(sorted(set(evidence))),
        policy_version=policy.version,
        omitted_sessions=omitted,
    )


__all__ = ["StudyPolicy", "StudyResult", "study_buyback"]
