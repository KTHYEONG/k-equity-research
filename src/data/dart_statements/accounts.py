"""One fact vocabulary for every DART account label and element id.

The tables below are the single place where a statement label or an XBRL
element id becomes a standardized fact name.  Every DART source (XBRL,
filing document) maps through here so the same account never means two
different things depending on where it was read.
"""
from __future__ import annotations

import re
from typing import Final

MAPPING_VERSION: Final = "dart-fact-map-v1"

_LOSS_ONLY_TO_FACT: Final = {
    "영업손실": "operating_profit",
    "당기순손실": "net_income",
    "분기순손실": "net_income",
    "반기순손실": "net_income",
    "연결당기순손실": "net_income",
    "분기연결순손실": "net_income",
    "반기연결순손실": "net_income",
}

_SUPPORT_TO_LINE: Final = {
    "매출원가": "cost_of_sales",
    "판매비와관리비": "sga",
    "판매비및관리비": "sga",
}

_LABEL_TO_FACT: dict[str, str] = {
    "매출액": "sales",
    "매출": "sales",
    "영업수익": "sales",
    "수익": "sales",
    "매출총이익": "gross_profit",
    "매출총손익": "gross_profit",
    "영업이익": "operating_profit",
    "영업손익": "operating_profit",
    "당기순이익": "net_income",
    "당기순손익": "net_income",
    "분기순이익": "net_income",
    "분기순손익": "net_income",
    "반기순이익": "net_income",
    "지배기업소유주지분순이익": "net_income",
    "자산총계": "assets",
    "자산총액": "assets",
    "자산계": "assets",
    "자본총계": "equity",
    "자본총액": "equity",
    "자본계": "equity",
    "부채총계": "debt",
    "부채총액": "debt",
    "부채계": "debt",
    "현금및현금성자산": "cash",
    "기말현금및현금성자산": "cash",
    "영업활동현금흐름": "operating_cash_flow",
    "영업활동으로인한현금흐름": "operating_cash_flow",
    "영업활동으로인한순현금흐름": "operating_cash_flow",
    "유형자산취득": "capex",
    "자본적지출": "capex",
    "설비투자": "capex",
    "유형자산의취득": "capex",
    "유무형자산취득": "capex",
}

_ID_TO_FACT: dict[str, str] = {
    "ifrs-full_Revenue": "sales",
    "ifrs-full_RevenueFromContractsWithCustomers": "sales",
    "ifrs-full_SalesRevenue": "sales",
    "ifrs-full_GrossProfit": "gross_profit",
    "ifrs-full_OperatingProfit": "operating_profit",
    "ifrs-full_OperatingIncome": "operating_profit",
    "ifrs-full_ProfitLoss": "net_income",
    "ifrs-full_ProfitLossAttributableToOwnersOfParent": "net_income",
    "ifrs-full_ProfitLossAttributableToOwners": "net_income",
    "ifrs-full_ComprehensiveIncome": "net_income",
    "ifrs-full_Assets": "assets",
    "ifrs-full_Equity": "equity",
    "ifrs-full_EquityAttributableToOwnersOfParent": "equity",
    "ifrs-full_Liabilities": "debt",
    "ifrs-full_CashAndCashEquivalents": "cash",
    "ifrs-full_CashFlowsFromOperatingActivities": "operating_cash_flow",
    "ifrs-full_CashFlowsFromUsedInOperatingActivities": "operating_cash_flow",
    "ifrs-full_NetCashFlowsFromOperatingActivities": "operating_cash_flow",
    "ifrs-full_PaymentsToAcquirePropertyPlantAndEquipment": "capex",
    "ifrs-full_PaymentsToAcquireIntangibleAssets": "capex",
    "dart_OperatingIncome": "operating_profit",
    "dart_Revenue": "sales",
}

# Leading row enumerators: roman numeral, arabic digit or Korean syllable, always
# followed by "." or ")" so that "자산총계" keeps its first syllable.
_ENUMERATOR_RE = re.compile(r"^[\(\[]?(?:[IVXLCDMⅠ-Ⅻ]+|\d+|[가-하])[.\)]")  # noqa: RUF001
_NOTE_REFERENCE_RE = re.compile(r"\(주석[^)]*\)$")


def map_standardized_account(*, account_id: str = "", account_nm: str = "") -> str | None:
    """Map an XBRL element id or a statement label to its standardized fact name."""
    aid = (account_id or "").strip()
    if aid and aid in _ID_TO_FACT:
        return _ID_TO_FACT[aid]
    label = re.sub(r"\([^)]*\)", "", (account_nm or "")).replace(" ", "").strip()
    if label and label in _LABEL_TO_FACT:
        return _LABEL_TO_FACT[label]
    return None


def map_loss_only_account(*, account_nm: str) -> str | None:
    """Map a loss-only income label to the fact it reports.

    Labels such as ``영업손실`` or ``당기순손실`` exist only for periods with a loss, but filings print
    their amount either in brackets or as a bare positive magnitude, so the label alone does not fix
    the sign. The mapping is kept apart from the standard vocabulary because XBRL and standard-API rows
    never carry these labels.

    Args:
        account_nm: A label already reduced by ``normalize_statement_label``.

    Returns:
        ``operating_profit`` or ``net_income``; None for every other label.
    """
    return _LOSS_ONLY_TO_FACT.get(account_nm or "")


def map_income_support_account(*, account_nm: str) -> str | None:
    """Map a label to an income-statement support line used only for consistency checks.

    Args:
        account_nm: A label already reduced by ``normalize_statement_label``.

    Returns:
        ``cost_of_sales`` (매출원가) or ``sga`` (판매비와관리비, including the ``판매비및관리비``
        spelling); None for every other label.
    """
    return _SUPPORT_TO_LINE.get(account_nm or "")


def _balanced_brackets(label: str) -> bool:
    """Check that every ``()`` and ``[]`` pair in the label is closed and nested."""
    depth_paren = 0
    depth_brack = 0
    for char in label:
        if char == "(":
            depth_paren += 1
        elif char == ")":
            depth_paren -= 1
            if depth_paren < 0:
                return False
        elif char == "[":
            depth_brack += 1
        elif char == "]":
            depth_brack -= 1
            if depth_brack < 0:
                return False
    return depth_paren == 0 and depth_brack == 0


def _unwrap_whole_label(label: str) -> str:
    """Strip brackets only when one pair wraps the whole remaining label."""
    while len(label) >= 2:
        opener, closer = label[0], label[-1]
        if (opener, closer) not in (("(", ")"), ("[", "]")):
            break
        depth = 0
        wraps_whole = True
        for index, char in enumerate(label):
            if char == opener:
                depth += 1
            elif char == closer:
                depth -= 1
            if depth == 0 and index < len(label) - 1:
                wraps_whole = False
                break
        if not wraps_whole or depth != 0:
            break
        label = label[1:-1]
    return label


def normalize_statement_label(raw: str) -> str:
    """Reduce one source row label to its bare account name.

    Removes whitespace, a trailing note reference such as ``(주석 29)``, and a leading enumerator
    (``I.``, ``1.``, ``(1)``, ``가.``, including roman-numeral forms). Brackets are removed only when they wrap the whole
    remaining label or are left unpaired by the enumerator removal; a closed parenthetical that is part
    of the account name (``수익(매출액)``) is preserved so vocabulary lookup can resolve it.

    Args:
        raw: The label cell as printed in the statement.

    Returns:
        The label without spacing, notes and enumerators, with balanced brackets intact.
    """
    label = "".join(raw.split())
    label = _NOTE_REFERENCE_RE.sub("", label)
    while True:
        stripped = _ENUMERATOR_RE.sub("", label, count=1)
        if stripped == label:
            break
        label = stripped
    label = _unwrap_whole_label(label)
    if not _balanced_brackets(label):
        edge_stripped = label.strip("()[]")
        label = edge_stripped if edge_stripped and _balanced_brackets(edge_stripped) else re.sub(r"[()\[\]]", "", label)
    return label
