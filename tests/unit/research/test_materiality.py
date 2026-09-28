"""Invariant guards for planned-size materiality ratios."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from src.core.buyback_document import BuybackFact, EvidenceLocation, ParsedBuyback
from src.data.local_lake import MarketBar
from src.research.materiality import MaterialityResult, calculate_materiality

KST = ZoneInfo("Asia/Seoul")
FILING_DATE = date(2024, 6, 25)
PRIOR_SESSION = date(2024, 6, 24)
DOC_HASH = "d" * 64
BAR_HASH = "b" * 64


def _evidence(field: str) -> EvidenceLocation:
    return EvidenceLocation(
        rcept_no="20240624000001",
        document_hash=DOC_HASH,
        member_name="report.xml",
        source_kind="ACODE",
        source_key=field,
        section="자기주식 취득 결정",
        table="TBL_ACQ_STK",
        cell="취득예정금액 / 보통주식",
    )


def _fact(field: str, value: Decimal | None, unit: str, status: str = "VERIFIED") -> BuybackFact:
    return BuybackFact(
        field=field,
        value_decimal=value,
        value_text=str(value) if value is not None else None,
        unit=unit,
        evidence=_evidence(field),
        status=status,  # type: ignore[arg-type]
    )


def _parsed(amount: Decimal | None, shares: Decimal | None) -> ParsedBuyback:
    return ParsedBuyback(
        rcept_no="20240624000001",
        corp_code="01386916",
        first_submission_date=FILING_DATE,
        facts=(
            _fact("ACQ_OSTK_PRC", amount, "KRW"),
            _fact("ACQ_OSTK", shares, "shares"),
        ),
        document_hash=DOC_HASH,
        parse_status="OK",
    )


def _bar(session: date = PRIOR_SESSION, market_cap: int | None = 500_000_000_000, listed: int | None = 50_000_000) -> MarketBar:
    return MarketBar(
        instrument_id="KRX:000001",
        session=session,
        open=70000,
        close=71000,
        market_cap=market_cap,
        listed_shares=listed,
        trading_value=300_000_000,
        ret_price=0.01,
        price_state="tradable",
        gap_before=False,
        share_factor=1.0,
        available_at=datetime(2024, 6, 24, 18, 0, tzinfo=KST),
        source_hash=BAR_HASH,
    )


def test_exact_ratios_use_decimal_division_with_both_hashes() -> None:
    """Verified KRW amount and share count divide prior denominators with both source hashes."""
    result = calculate_materiality(_parsed(Decimal(10_000_000_000), Decimal(100_000)), FILING_DATE, _bar())
    assert isinstance(result, MaterialityResult)
    assert result.amount_to_market_cap == Decimal(10_000_000_000) / Decimal(500_000_000_000)
    assert result.shares_to_listed_shares == Decimal(100_000) / Decimal(50_000_000)
    assert result.source_hashes == (DOC_HASH, BAR_HASH)
    assert result.status == "OK"


def test_zero_listed_shares_withholds_only_share_ratio() -> None:
    """Zero listed shares null the share ratio with a reason while the amount ratio survives."""
    result = calculate_materiality(_parsed(Decimal(10_000_000_000), Decimal(100_000)), FILING_DATE, _bar(listed=0))
    assert result.shares_to_listed_shares is None
    assert result.amount_to_market_cap == Decimal(10_000_000_000) / Decimal(500_000_000_000)
    assert "LISTED_SHARES_UNAVAILABLE" in result.status


def test_halted_prior_bar_withholds_both_ratios() -> None:
    """A halted prior bar withholds both ratios with an explicit reason."""
    halted = _bar()
    bar = MarketBar(
        instrument_id=halted.instrument_id,
        session=halted.session,
        open=halted.open,
        close=halted.close,
        market_cap=halted.market_cap,
        listed_shares=halted.listed_shares,
        trading_value=halted.trading_value,
        ret_price=halted.ret_price,
        price_state="halted",
        gap_before=halted.gap_before,
        share_factor=halted.share_factor,
        available_at=halted.available_at,
        source_hash=halted.source_hash,
    )
    result = calculate_materiality(_parsed(Decimal(10_000_000_000), Decimal(100_000)), FILING_DATE, bar)
    assert result.amount_to_market_cap is None
    assert result.shares_to_listed_shares is None
    assert result.source_hashes == ()
    assert result.status == "INVALID_PRIOR_BAR"


def test_unverified_amount_and_missing_cap_withhold_amount_ratio() -> None:
    """Unverified or unit-unknown numerators and missing denominators withhold only their ratio."""
    parsed = ParsedBuyback(
        rcept_no="20240624000001",
        corp_code="01386916",
        first_submission_date=FILING_DATE,
        facts=(
            _fact("ACQ_PPS", None, "KRW"),
            _fact("ACQ_OSTK_PRC", None, "KRW"),
            _fact("ACQ_OSTK", Decimal(100_000), "shares"),
        ),
        document_hash=DOC_HASH,
        parse_status="OK",
    )
    result = calculate_materiality(parsed, FILING_DATE, _bar())
    assert result.amount_to_market_cap is None
    assert result.shares_to_listed_shares == Decimal(100_000) / Decimal(50_000_000)
    assert "AMOUNT_UNAVAILABLE" in result.status
    assert result.source_hashes == (DOC_HASH, BAR_HASH)


def test_missing_market_cap_keeps_share_ratio() -> None:
    """A missing market-cap denominator withholds the amount ratio while shares survive."""
    result = calculate_materiality(
        _parsed(Decimal(10_000_000_000), Decimal(100_000)), FILING_DATE, _bar(market_cap=None)
    )
    assert result.amount_to_market_cap is None
    assert result.shares_to_listed_shares == Decimal(100_000) / Decimal(50_000_000)
    assert "MARKET_CAP_UNAVAILABLE" in result.status


def test_absent_share_fact_withholds_share_ratio() -> None:
    """A missing share-quantity fact withholds the share ratio while the amount survives."""
    parsed = ParsedBuyback(
        rcept_no="20240624000001",
        corp_code="01386916",
        first_submission_date=FILING_DATE,
        facts=(_fact("ACQ_OSTK_PRC", Decimal(10_000_000_000), "KRW"),),
        document_hash=DOC_HASH,
        parse_status="OK",
    )
    result = calculate_materiality(parsed, FILING_DATE, _bar())
    assert result.amount_to_market_cap == Decimal(10_000_000_000) / Decimal(500_000_000_000)
    assert result.shares_to_listed_shares is None
    assert "SHARES_UNAVAILABLE" in result.status


def test_receipt_day_close_is_rejected_as_denominator() -> None:
    """A receipt-day bar can never serve as the pre-filing denominator."""
    with pytest.raises(ValueError, match="strictly before"):
        calculate_materiality(_parsed(Decimal(10_000_000_000), Decimal(100_000)), FILING_DATE, _bar(session=FILING_DATE))
