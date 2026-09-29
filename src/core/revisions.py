"""Conservative links between original and corrected buyback filing versions."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from src.core.buyback_document import BuybackFact, ParsedBuyback
from src.data.catalog import FilingVersion

_PREFIX_RE = re.compile(r"^\s*\[[^\]]*\]\s*")
_WS_RE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class EventLink:
    """Frozen grouping of receipts that establish one buyback event."""

    event_id: str
    rcept_nos: tuple[str, ...]
    status: Literal["LINKED", "UNRESOLVED_LINK", "WITHDRAWN"]


@dataclass(frozen=True, slots=True)
class FactChange:
    """Frozen verified same-field change between two linked versions."""

    field: str
    before: BuybackFact
    after: BuybackFact


def _form_key(report_name: str) -> str:
    text = report_name
    while True:
        stripped = _PREFIX_RE.sub("", text, count=1)
        if stripped == text:
            return _WS_RE.sub("", text)
        text = stripped


def _event_id(corp_code: str, root_rcept_no: str) -> str:
    return f"buyback:{corp_code}:{root_rcept_no}"


def link_buyback_versions(
    filings: Sequence[FilingVersion], parsed: Mapping[str, ParsedBuyback],
    verified_parents: Mapping[str, str] | None = None,
) -> tuple[EventLink, ...]:
    """Group filing receipts only when company, report form, initial submission date and document references establish one event. Preserve ambiguous same-day reports separately for review."""
    index: dict[str, FilingVersion] = {}
    for filing in filings:
        if filing.rcept_no and filing.rcept_no not in index:
            index[filing.rcept_no] = filing

    verified_parents = verified_parents or {}
    inferred_parents: dict[str, str] = {}
    for filing in index.values():
        if not filing.correction_flag or filing.parent_rcept_no is not None or filing.rcept_no in verified_parents:
            continue
        first_date = parsed.get(filing.rcept_no)
        if first_date is None or first_date.first_submission_date is None:
            continue
        candidates = [
            original.rcept_no
            for original in index.values()
            if not original.correction_flag
            and not original.withdrawal_flag
            and original.corp_code == filing.corp_code
            and _form_key(original.report_name) == _form_key(filing.report_name)
            and original.receipt_date == first_date.first_submission_date
            and parsed.get(original.rcept_no) is not None
            and parsed[original.rcept_no].first_submission_date == original.receipt_date
        ]
        if len(candidates) == 1:
            inferred_parents[filing.rcept_no] = candidates[0]

    def _root(rcept_no: str) -> str:
        seen = {rcept_no}
        current = rcept_no
        while True:
            filing = index[current]
            parent = filing.parent_rcept_no or verified_parents.get(current) or inferred_parents.get(current)
            if parent is None:
                return current
            if parent not in index or parent in seen:
                return current
            seen.add(parent)
            current = parent

    groups: dict[str, list[FilingVersion]] = {}
    for filing in index.values():
        groups.setdefault(_root(filing.rcept_no), []).append(filing)

    parentless_keys: dict[tuple[str, str, str], list[str]] = {}
    for filing in index.values():
        if not filing.parent_rcept_no and not filing.correction_flag and not filing.withdrawal_flag:
            key = (filing.corp_code, _form_key(filing.report_name), filing.receipt_date.isoformat())
            parentless_keys.setdefault(key, []).append(filing.rcept_no)

    links: list[EventLink] = []
    emitted: set[str] = set()
    for root_no in sorted(groups):
        members = sorted(groups[root_no], key=lambda item: (item.receipt_date.isoformat(), item.rcept_no))
        root = index[root_no]
        ordered_nos = tuple(item.rcept_no for item in members)
        event = _event_id(root.corp_code, root_no)
        root_key = (root.corp_code, _form_key(root.report_name), root.receipt_date.isoformat())
        if len(parentless_keys.get(root_key, [])) > 1:
            involved = {item.rcept_no for item in members} | set(parentless_keys[root_key])
            for rcept_no in sorted(involved):
                if rcept_no in emitted:
                    continue
                member = index.get(rcept_no)
                corp = member.corp_code if member is not None else root.corp_code
                links.append(EventLink(_event_id(corp, rcept_no), (rcept_no,), "UNRESOLVED_LINK"))
                emitted.add(rcept_no)
            continue
        unresolved = False
        if root.rcept_no not in parsed or parsed[root.rcept_no].first_submission_date is None:
            unresolved = True
        if root.correction_flag or root.withdrawal_flag:
            unresolved = True
        for item in members:
            member_parsed = parsed.get(item.rcept_no)
            has_verified_parent = verified_parents.get(item.rcept_no) == root.rcept_no
            if member_parsed is None or (not has_verified_parent and member_parsed.first_submission_date != root.receipt_date):
                unresolved = True
            if item.corp_code != root.corp_code:
                unresolved = True
            if _form_key(item.report_name) != _form_key(root.report_name):
                unresolved = True
            if item.parent_rcept_no is not None and item.parent_rcept_no not in index:
                unresolved = True
            if item.withdrawal_flag and item.parent_rcept_no not in index:
                unresolved = True
        if unresolved:
            links.append(EventLink(event, ordered_nos, "UNRESOLVED_LINK"))
            continue
        if any(item.withdrawal_flag for item in members):
            links.append(EventLink(event, ordered_nos, "WITHDRAWN"))
            continue
        links.append(EventLink(event, ordered_nos, "LINKED"))
    links.sort(key=lambda link: link.event_id)
    return tuple(links)


def diff_verified_facts(before: ParsedBuyback, after: ParsedBuyback) -> tuple[FactChange, ...]:
    """Describe only verified same-field changes between two linked source versions; absence or unverified values do not imply a change to zero."""
    before_by_field = {fact.field: fact for fact in before.facts}
    after_by_field = {fact.field: fact for fact in after.facts}
    changes: list[FactChange] = []
    for field in sorted(set(before_by_field) & set(after_by_field)):
        old = before_by_field[field]
        new = after_by_field[field]
        if old.status != "VERIFIED" or new.status != "VERIFIED":
            continue
        if old.unit != new.unit:
            continue
        if old.value_decimal is not None or new.value_decimal is not None:
            if old.value_decimal is None or new.value_decimal is None:
                continue
            if old.value_decimal != new.value_decimal:
                changes.append(FactChange(field, old, new))
        elif (old.value_text or "") != (new.value_text or ""):
            if not old.value_text or not new.value_text:
                continue
            changes.append(FactChange(field, old, new))
    return tuple(changes)


__all__ = ["EventLink", "FactChange", "diff_verified_facts", "link_buyback_versions"]
