"""Point-in-time balance-sheet snapshot and deterministic buyback affordability ratios."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from src.data.financial_evidence import VerifiedFinancialFact

_BALANCE_FACTS = ("assets", "cash", "debt", "equity")


@dataclass(frozen=True, slots=True)
class FinancialSnapshot:
    """One filing's balance sheet on a single basis; never blends filings or consolidated/separate statements."""

    fiscal_period: str
    consolidated: bool
    filing_id: str
    available_at: datetime
    facts: Mapping[str, VerifiedFinancialFact]
    ratios: Mapping[str, Decimal]

    @property
    def basis(self) -> str:
        return "consolidated" if self.consolidated else "separate"


def build_financial_snapshot(
    facts: Sequence[VerifiedFinancialFact],
    knowledge_at: datetime,
    planned_amount_krw: Decimal | None,
) -> FinancialSnapshot | None:
    """Select the latest complete KRW balance sheet observable at ``knowledge_at`` and derive ratios from it.

    ``debt`` is total liabilities. Balance-sheet items are point-in-time, so no year-to-date ambiguity arises;
    income and cash-flow items are deliberately excluded. Ratios with a non-positive denominator are omitted.
    Returns None when no filing carries all four items on one basis (fail-closed).
    """
    groups: dict[tuple[str, str, bool], dict[str, VerifiedFinancialFact]] = {}
    for fact in facts:
        if fact.fact in _BALANCE_FACTS and fact.unit == "KRW" and fact.available_at <= knowledge_at:
            groups.setdefault((fact.filing_id, fact.fiscal_period, fact.consolidated), {})[fact.fact] = fact
    complete = {key: items for key, items in groups.items() if len(items) == len(_BALANCE_FACTS)}
    if not complete:
        return None
    key = max(complete, key=lambda k: (complete[k]["cash"].available_at, k[2], k[1], k[0]))
    items = complete[key]
    cash, equity, assets, liabilities = (items[name].value for name in ("cash", "equity", "assets", "debt"))
    ratios: dict[str, Decimal] = {}
    if assets > 0:
        ratios["cash_to_assets"] = cash / assets
    if equity > 0:
        ratios["liabilities_to_equity"] = liabilities / equity
        if planned_amount_krw is not None:
            ratios["amount_to_equity"] = planned_amount_krw / equity
    if cash > 0 and planned_amount_krw is not None:
        ratios["amount_to_cash"] = planned_amount_krw / cash
    return FinancialSnapshot(key[1], key[2], key[0], items["cash"].available_at, items, ratios)


__all__ = ["FinancialSnapshot", "build_financial_snapshot"]
