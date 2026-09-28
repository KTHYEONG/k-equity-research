"""Planned buyback size relative to pre-filing market scale."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from src.core.buyback_document import ParsedBuyback
from src.data.local_lake import MarketBar

_AMOUNT_FIELD = "ACQ_OSTK_PRC"
_SHARES_FIELD = "ACQ_OSTK"
_TRADABLE_STATE = "tradable"


@dataclass(frozen=True, slots=True)
class MaterialityResult:
    """Frozen planned-size ratios anchored to one pre-filing session."""

    amount_to_market_cap: Decimal | None
    shares_to_listed_shares: Decimal | None
    source_hashes: tuple[str, ...]
    status: str


def _verified_decimal(parsed: ParsedBuyback, field: str, unit: str) -> Decimal | None:
    for fact in parsed.facts:
        if fact.field != field or fact.status != "VERIFIED" or fact.unit != unit:
            continue
        if fact.value_decimal is None:
            return None
        return fact.value_decimal
    return None


def calculate_materiality(parsed: ParsedBuyback, filing_date: date, prior_bar: MarketBar) -> MaterialityResult:
    """Relate verified planned KRW spend and common-share quantity to the last completed pre-filing market cap and listed shares. Return unavailable ratios with reasons when source units, dates or denominators cannot be trusted."""
    if prior_bar.session >= filing_date:
        raise ValueError("prior bar must come from a session strictly before the filing date")
    reasons: list[str] = []
    amount_ratio: Decimal | None = None
    share_ratio: Decimal | None = None
    source_hashes: tuple[str, ...] = ()
    if not prior_bar.instrument_id or prior_bar.price_state != _TRADABLE_STATE:
        reasons.append("INVALID_PRIOR_BAR")
    else:
        amount = _verified_decimal(parsed, _AMOUNT_FIELD, "KRW")
        if amount is None:
            reasons.append("AMOUNT_UNAVAILABLE")
        elif prior_bar.market_cap is None or prior_bar.market_cap <= 0:
            reasons.append("MARKET_CAP_UNAVAILABLE")
        else:
            amount_ratio = amount / Decimal(prior_bar.market_cap)
        shares = _verified_decimal(parsed, _SHARES_FIELD, "shares")
        if shares is None:
            reasons.append("SHARES_UNAVAILABLE")
        elif prior_bar.listed_shares is None or prior_bar.listed_shares <= 0:
            reasons.append("LISTED_SHARES_UNAVAILABLE")
        else:
            share_ratio = shares / Decimal(prior_bar.listed_shares)
    if amount_ratio is not None and share_ratio is not None:
        status = "OK"
    else:
        status = ";".join(reasons) if reasons else "UNAVAILABLE"
    if amount_ratio is not None or share_ratio is not None:
        source_hashes = (parsed.document_hash, prior_bar.source_hash)
    else:
        source_hashes = ()
    return MaterialityResult(
        amount_to_market_cap=amount_ratio,
        shares_to_listed_shares=share_ratio,
        source_hashes=source_hashes,
        status=status,
    )


__all__ = ["MaterialityResult", "calculate_materiality"]
