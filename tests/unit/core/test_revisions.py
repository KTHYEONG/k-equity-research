"""Invariant guards for buyback version links and verified fact diffs."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from src.core.buyback_document import BuybackFact, EvidenceLocation, ParsedBuyback
from src.core.revisions import diff_verified_facts, link_buyback_versions
from src.data.catalog import FilingVersion

KST = ZoneInfo("Asia/Seoul")
FORM = "주요사항보고서(자기주식취득결정)"


def _filing(
    rcept_no: str,
    receipt_day: date = date(2024, 6, 26),
    parent: str | None = None,
    corp: str = "01386916",
    report: str = FORM,
    withdrawal: bool = False,
) -> FilingVersion:
    observed = datetime(2024, 6, 27, 9, 0, tzinfo=KST)
    return FilingVersion(
        rcept_no=rcept_no,
        corp_code=corp,
        receipt_date=receipt_day,
        report_name=report,
        stock_code="361610",
        raw_hash="a" * 64,
        first_observed_at=observed,
        knowledge_available_at=datetime(2024, 6, 27, 9, 0, tzinfo=KST),
        availability_mode="HISTORICAL_BACKFILL",
        correction_flag=parent is not None,
        withdrawal_flag=withdrawal,
        parent_rcept_no=parent,
        link_status="ORIGINAL",
        time_precision="DATE_ONLY",
    )


def _evidence(rcept_no: str, key: str) -> EvidenceLocation:
    return EvidenceLocation(
        rcept_no=rcept_no,
        document_hash="h-" + rcept_no,
        member_name=f"{rcept_no}.xml",
        source_kind="ACODE",
        source_key=key,
        section="자기주식 취득 결정",
        table="TBL_ACQ_LMT",
        cell="10. 1일 매수 주문수량 한도 / 보통주식",
    )


def _fact(rcept_no: str, field: str, value: str, status: str = "VERIFIED") -> BuybackFact:
    return BuybackFact(
        field=field,
        value_decimal=Decimal(value.replace(",", "")) if status == "VERIFIED" else None,
        value_text=value,
        unit="shares",
        evidence=_evidence(rcept_no, field),
        status=status,  # type: ignore[arg-type]
    )


def _parsed(rcept_no: str, limit: str, status: str = "VERIFIED") -> ParsedBuyback:
    return ParsedBuyback(
        rcept_no=rcept_no,
        corp_code="01386916",
        first_submission_date=date(2024, 6, 26),
        facts=(_fact(rcept_no, "BUY_OSTK_LMT", limit, status),),
        document_hash="h-" + rcept_no,
        parse_status="OK",
    )


def test_changed_verified_field_reports_both_coordinates() -> None:
    """Exactly the changed verified field appears with before and after evidence."""
    before = _parsed("20240626000207", "84,775")
    after = _parsed("20240626000369", "84,795")
    changes = diff_verified_facts(before, after)
    assert len(changes) == 1
    assert changes[0].field == "BUY_OSTK_LMT"
    assert changes[0].before.value_decimal == Decimal("84775")
    assert changes[0].after.value_decimal == Decimal("84795")
    assert changes[0].before.evidence.rcept_no == "20240626000207"
    assert changes[0].after.evidence.rcept_no == "20240626000369"


def test_unverified_absence_never_implies_change_to_zero() -> None:
    """Missing or unverified values do not produce a change record."""
    before = _parsed("20240626000207", "84,775")
    after = _parsed("20240626000369", "-", status="UNVERIFIED")
    assert diff_verified_facts(before, after) == ()
    lonely = ParsedBuyback(
        rcept_no="20240626000369",
        corp_code="01386916",
        first_submission_date=date(2024, 6, 26),
        facts=(),
        document_hash="h-x",
        parse_status="OK",
    )
    assert diff_verified_facts(before, lonely) == ()


def test_ambiguous_same_day_filings_stay_unresolved() -> None:
    """Two plausible originals and their correction never merge automatically."""
    first = _filing("20240626000207")
    second = _filing("20240626000208")
    correction = _filing("20240626000369", parent="20240626000207")
    parsed = {
        "20240626000207": _parsed("20240626000207", "84,775"),
        "20240626000208": _parsed("20240626000208", "84,775"),
        "20240626000369": _parsed("20240626000369", "84,795"),
    }
    links = link_buyback_versions([first, second, correction], parsed)
    assert all(link.status == "UNRESOLVED_LINK" for link in links)
    assert not any(len(link.rcept_nos) > 1 and link.status == "LINKED" for link in links)


def test_correction_remains_own_filing_with_stable_event() -> None:
    """A linked correction keeps its receipt while the event id follows the original."""
    original = _filing("20240626000207")
    correction = _filing("20240626000369", parent="20240626000207", report="[기재정정]" + FORM)
    other = _filing("20240628000242", receipt_day=date(2024, 6, 28), corp="00155948")
    parsed = {
        "20240626000207": _parsed("20240626000207", "84,775"),
        "20240626000369": _parsed("20240626000369", "84,795"),
        "20240628000242": ParsedBuyback(
            rcept_no="20240628000242",
            corp_code="00155948",
            first_submission_date=date(2024, 6, 28),
            facts=(_fact("20240628000242", "BUY_OSTK_LMT", "10,000"),),
            document_hash="h-20240628000242",
            parse_status="OK",
        ),
    }
    links = {link.event_id: link for link in link_buyback_versions([original, correction, other], parsed)}
    event = links["buyback:01386916:20240626000207"]
    assert event.status == "LINKED"
    assert set(event.rcept_nos) == {"20240626000207", "20240626000369"}
    foreign = links["buyback:00155948:20240628000242"]
    assert foreign.status == "LINKED"
    assert foreign.rcept_nos == ("20240628000242",)


def test_unique_original_is_inferred_from_correction_first_date() -> None:
    original = _filing("20240626000207")
    correction = _filing(
        "20240628000369", receipt_day=date(2024, 6, 28), report="[기재정정]" + FORM
    )
    correction = replace(correction, correction_flag=True, link_status="CORRECTION")
    links = link_buyback_versions(
        [original, correction],
        {original.rcept_no: _parsed(original.rcept_no, "84,775"), correction.rcept_no: _parsed(correction.rcept_no, "84,795")},
    )
    assert len(links) == 1
    assert links[0].status == "LINKED"
    assert links[0].rcept_nos == (original.rcept_no, correction.rcept_no)


def test_correction_without_first_date_does_not_attach_to_original() -> None:
    original = _filing("20240626000207")
    correction = replace(_filing("20240628000369", date(2024, 6, 28)), correction_flag=True)
    parsed_correction = replace(_parsed(correction.rcept_no, "84,795"), first_submission_date=None)
    links = link_buyback_versions(
        [original, correction],
        {original.rcept_no: _parsed(original.rcept_no, "84,775"), correction.rcept_no: parsed_correction},
    )
    assert len(links) == 2
    assert any(link.rcept_nos == (correction.rcept_no,) and link.status == "UNRESOLVED_LINK" for link in links)


def test_withdrawal_keeps_history_without_active_link() -> None:
    """A verified withdrawal marks the event withdrawn while versions persist."""
    original = _filing("20240626000207")
    withdrawal = _filing("20240626000999", parent="20240626000207", withdrawal=True)
    parsed = {
        "20240626000207": _parsed("20240626000207", "84,775"),
        "20240626000999": _parsed("20240626000999", "84,775"),
    }
    links = link_buyback_versions([original, withdrawal], parsed)
    assert len(links) == 1
    assert links[0].status == "WITHDRAWN"
    assert set(links[0].rcept_nos) == {"20240626000207", "20240626000999"}


def test_conflicting_company_or_missing_date_cannot_link() -> None:
    """Different companies and missing initial dates produce unresolved links."""
    original = _filing("20240626000207")
    foreign = _filing("20240626000207x", parent="20240626000207", corp="00155948")
    parsed = {
        "20240626000207": _parsed("20240626000207", "84,775"),
        "20240626000207x": ParsedBuyback(
            rcept_no="20240626000207x",
            corp_code="00155948",
            first_submission_date=None,
            facts=(),
            document_hash="h-y",
            parse_status="OK",
        ),
    }
    links = link_buyback_versions([original, foreign], parsed)
    assert len(links) == 1
    assert links[0].status == "UNRESOLVED_LINK"


def test_dangling_reference_cannot_link() -> None:
    """A correction pointing outside the known set stays unresolved."""
    original = _filing("20240626000207")
    dangling = _filing("20240626000369", parent="20240621999999")
    parsed = {
        "20240626000207": _parsed("20240626000207", "84,775"),
        "20240626000369": _parsed("20240626000369", "84,795"),
    }
    links = link_buyback_versions([original, dangling], parsed)
    assert {link.event_id for link in links} == {
        "buyback:01386916:20240626000207",
        "buyback:01386916:20240626000369",
    }
    assert all(link.status != "LINKED" or link.rcept_nos == ("20240626000207",) for link in links)


def test_reference_cycle_never_merges() -> None:
    """Mutual parent references resolve without merging receipts."""
    first = _filing("20240626000207", parent="20240626000208")
    second = _filing("20240626000208", parent="20240626000207")
    parsed = {
        "20240626000207": _parsed("20240626000207", "84,775"),
        "20240626000208": _parsed("20240626000208", "84,775"),
    }
    links = link_buyback_versions([first, second], parsed)
    assert all(len(link.rcept_nos) == 1 for link in links)


def test_missing_root_parse_or_form_mismatch_cannot_link() -> None:
    """Unknown root documents and conflicting forms stay unresolved."""
    original = _filing("20240626000207")
    correction = _filing("20240626000369", parent="20240626000207")
    links = link_buyback_versions([original, correction], {"20240626000369": _parsed("20240626000369", "84,795")})
    assert len(links) == 1
    assert links[0].status == "UNRESOLVED_LINK"
    renamed = _filing("20240626000369", parent="20240626000207", report="주요사항보고서(유상증자결정)")
    parsed = {
        "20240626000207": _parsed("20240626000207", "84,775"),
        "20240626000369": _parsed("20240626000369", "84,795"),
    }
    links = link_buyback_versions([original, renamed], parsed)
    assert len(links) == 1
    assert links[0].status == "UNRESOLVED_LINK"
    dangling_withdrawal = _filing("20240626000999", parent="20240621999999", withdrawal=True)
    parsed_withdrawal = {
        "20240626000207": _parsed("20240626000207", "84,775"),
        "20240626000999": _parsed("20240626000999", "84,775"),
    }
    links = link_buyback_versions([original, dangling_withdrawal], parsed_withdrawal)
    assert all(link.status != "LINKED" or link.rcept_nos == ("20240626000207",) for link in links)


def _text_fact(rcept_no: str, value: str) -> BuybackFact:
    return BuybackFact(
        field="ACQ_PPS",
        value_decimal=None,
        value_text=value,
        unit=None,
        evidence=_evidence(rcept_no, "ACQ_PPS"),
        status="VERIFIED",
    )


def test_text_change_and_unit_mismatch_rules() -> None:
    """Verified text changes diff; incomparable units or half-numeric pairs never do."""
    before = ParsedBuyback(
        rcept_no="20240626000207",
        corp_code="01386916",
        first_submission_date=date(2024, 6, 26),
        facts=(_text_fact("20240626000207", "Stock Grant 부여"),),
        document_hash="h-a",
        parse_status="OK",
    )
    after = ParsedBuyback(
        rcept_no="20240626000369",
        corp_code="01386916",
        first_submission_date=date(2024, 6, 26),
        facts=(_text_fact("20240626000369", "Stock Grant 지급"),),
        document_hash="h-b",
        parse_status="OK",
    )
    changes = diff_verified_facts(before, after)
    assert len(changes) == 1
    assert changes[0].field == "ACQ_PPS"
    assert changes[0].before.value_text == "Stock Grant 부여"
    assert changes[0].after.value_text == "Stock Grant 지급"

    numeric = _parsed("20240626000207", "84,775")
    renumbered = ParsedBuyback(
        rcept_no="20240626000369",
        corp_code="01386916",
        first_submission_date=date(2024, 6, 26),
        facts=(
            BuybackFact(
                field="BUY_OSTK_LMT",
                value_decimal=None,
                value_text="84,795",
                unit="shares",
                evidence=_evidence("20240626000369", "BUY_OSTK_LMT"),
                status="VERIFIED",
            ),
        ),
        document_hash="h-c",
        parse_status="OK",
    )
    assert diff_verified_facts(numeric, renumbered) == ()
    other_unit = ParsedBuyback(
        rcept_no="20240626000369",
        corp_code="01386916",
        first_submission_date=date(2024, 6, 26),
        facts=(
            BuybackFact(
                field="BUY_OSTK_LMT",
                value_decimal=Decimal("84795"),
                value_text="84,795",
                unit="contracts",
                evidence=_evidence("20240626000369", "BUY_OSTK_LMT"),
                status="VERIFIED",
            ),
        ),
        document_hash="h-d",
        parse_status="OK",
    )
    assert diff_verified_facts(numeric, other_unit) == ()
    emptied = ParsedBuyback(
        rcept_no="20240626000369",
        corp_code="01386916",
        first_submission_date=date(2024, 6, 26),
        facts=(_text_fact("20240626000369", ""),),
        document_hash="h-e",
        parse_status="OK",
    )
    assert diff_verified_facts(before, emptied) == ()
