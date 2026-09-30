"""Invariant guards for the point-in-time balance-sheet snapshot."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from src.data.financial_evidence import VerifiedFinancialFact
from src.research.financial_snapshot import build_financial_snapshot

KNOWN = datetime(2024, 6, 20, 9, 0, tzinfo=UTC)


def _fact(
    name: str,
    value: str,
    *,
    filing: str = "F1",
    period: str = "2024Q1",
    consolidated: bool = True,
    available: datetime = datetime(2024, 5, 20, tzinfo=UTC),
    unit: str = "KRW",
) -> VerifiedFinancialFact:
    return VerifiedFinancialFact(
        corp_code="00000001", filing_id=filing, fact=name, fiscal_period=period, consolidated=consolidated,
        value=Decimal(value), unit=unit, available_at=available, source_hash=(name[0] * 64), evidence_key=f"k-{name}",
    )


def _sheet(**kwargs: object) -> list[VerifiedFinancialFact]:
    values = {"assets": "1000", "cash": "200", "debt": "400", "equity": "600"}
    return [_fact(name, value, **kwargs) for name, value in values.items()]  # type: ignore[arg-type]


def test_ratios_are_exact_decimal_quotients() -> None:
    snapshot = build_financial_snapshot(_sheet(), KNOWN, Decimal(50))
    assert snapshot is not None
    assert snapshot.ratios == {
        "cash_to_assets": Decimal("0.2"),
        "liabilities_to_equity": Decimal(400) / Decimal(600),
        "amount_to_equity": Decimal(50) / Decimal(600),
        "amount_to_cash": Decimal("0.25"),
    }
    assert snapshot.basis == "consolidated"


def test_filing_after_knowledge_time_is_invisible() -> None:
    later = datetime(2024, 6, 21, tzinfo=UTC)
    facts = _sheet() + _sheet(filing="F2", period="2024Q2", available=later)
    snapshot = build_financial_snapshot(facts, KNOWN, None)
    assert snapshot is not None
    assert (snapshot.filing_id, snapshot.fiscal_period) == ("F1", "2024Q1")
    at_boundary = build_financial_snapshot(facts, later, None)
    assert at_boundary is not None
    assert at_boundary.filing_id == "F2"


def test_same_filing_prefers_consolidated_and_never_blends_bases() -> None:
    separate = _sheet(consolidated=False)
    mixed = [f for f in _sheet() if f.fact != "equity"] + [_fact("equity", "1", consolidated=False)]
    snapshot = build_financial_snapshot(separate + _sheet(), KNOWN, None)
    assert snapshot is not None
    assert snapshot.consolidated is True
    assert build_financial_snapshot(mixed, KNOWN, None) is None


def test_incomplete_or_foreign_currency_balance_sheet_fails_closed() -> None:
    assert build_financial_snapshot([f for f in _sheet() if f.fact != "cash"], KNOWN, None) is None
    assert build_financial_snapshot(_sheet(unit="USD"), KNOWN, Decimal(1)) is None
    assert build_financial_snapshot([], KNOWN, None) is None


def test_non_positive_denominators_omit_only_dependent_ratios() -> None:
    facts = [_fact("assets", "1000"), _fact("cash", "0"), _fact("debt", "400"), _fact("equity", "-5")]
    snapshot = build_financial_snapshot(facts, KNOWN, Decimal(10))
    assert snapshot is not None
    assert snapshot.ratios == {"cash_to_assets": Decimal(0)}
    no_assets = build_financial_snapshot(
        [_fact("assets", "0"), _fact("cash", "5"), _fact("debt", "1"), _fact("equity", "2")], KNOWN, None
    )
    assert no_assets is not None
    assert set(no_assets.ratios) == {"liabilities_to_equity"}
