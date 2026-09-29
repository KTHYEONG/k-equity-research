"""Invariant guards for versioned buyback document parsing."""

from __future__ import annotations

import hashlib
import io
import zipfile
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src.core.buyback_document import DocumentLimits, parse_buyback_document
from src.data.catalog import FilingVersion

KST = ZoneInfo("Asia/Seoul")
ORIG = "20240626000207"
CORR = "20240626000369"
PROBE = Path("data/probe_dart")


def _filing(rcept_no: str, raw: bytes, receipt_day: date = date(2024, 6, 26)) -> FilingVersion:
    observed = datetime(2024, 6, 27, 9, 0, tzinfo=KST)
    return FilingVersion(
        rcept_no=rcept_no,
        corp_code="01386916",
        receipt_date=receipt_day,
        report_name="주요사항보고서(자기주식취득결정)",
        stock_code="361610",
        raw_hash=hashlib.sha256(raw).hexdigest(),
        first_observed_at=observed,
        knowledge_available_at=datetime(2024, 6, 27, 9, 0, tzinfo=KST),
        availability_mode="HISTORICAL_BACKFILL",
        correction_flag=False,
        withdrawal_flag=False,
        parent_rcept_no=None,
        link_status="ORIGINAL",
        time_precision="DATE_ONLY",
    )


def _fact_map(parsed):  # type: ignore[no-untyped-def]
    return {fact.field: fact for fact in parsed.facts}


def _row(label: str, sub: str, code: str, value: str) -> str:
    return (
        '<TR ACOPY="N" ADELETE="N">'
        f'<TD ROWSPAN="2" COLSPAN="2" VALIGN="MIDDLE" WIDTH="267" HEIGHT="60">{label}</TD>'
        f'<TD ALIGN="CENTER" WIDTH="101" HEIGHT="30">{sub}</TD>'
        f'<TE COLSPAN="3" ALIGN="RIGHT" WIDTH="241" HEIGHT="30" ACODE="{code}">{value}</TE>'
        "</TR>"
    )


def _date_row(label: str, sub: str, key: str, stamp: str, display: str) -> str:
    return (
        '<TR ACOPY="N" ADELETE="N">'
        f'<TD ROWSPAN="2" COLSPAN="2" VALIGN="MIDDLE" WIDTH="267" HEIGHT="60">{label}</TD>'
        f'<TD ALIGN="CENTER" WIDTH="101" HEIGHT="30">{sub}</TD>'
        f'<TU COLSPAN="3" ALIGN="CENTER" WIDTH="241" HEIGHT="30" AUNIT="{key}" AUNITVALUE="{stamp}">{display}</TU>'
        "</TR>"
    )


def _wrap(rows: str) -> bytes:
    xml = (
        '<?xml version="1.0" encoding="utf-8"?><DOCUMENT>'
        '<COMPANY-NAME AREGCIK="01386916">에스케이아이이테크놀로지(주)</COMPANY-NAME>'
        '<BODY><SECTION-1 ACLASS="MANDATORY" APARTSOURCE="SOURCE">'
        '<TITLE>자기주식 취득 결정</TITLE>'
        f'<TABLE-GROUP ACLASS="TBL_ACQ_STK"><TABLE><TBODY>{rows}</TBODY></TABLE></TABLE-GROUP>'
        "</SECTION-1></BODY></DOCUMENT>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("20240626000207.xml", xml.encode("utf-8"))
    return buffer.getvalue()


def _zip_with_names(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def test_original_and_correction_keep_own_hashes_and_limits() -> None:
    """Distinct document hashes and BUY_OSTK_LMT values stay attached to their receipts."""
    raw_orig = (PROBE / f"{ORIG}_document.zip").read_bytes()
    raw_corr = (PROBE / f"{CORR}_document.zip").read_bytes()
    parsed_orig = parse_buyback_document(_filing(ORIG, raw_orig), raw_orig, DocumentLimits())
    parsed_corr = parse_buyback_document(_filing(CORR, raw_corr), raw_corr, DocumentLimits())
    assert parsed_orig.document_hash == hashlib.sha256(raw_orig).hexdigest()
    assert parsed_corr.document_hash == hashlib.sha256(raw_corr).hexdigest()
    assert parsed_orig.document_hash != parsed_corr.document_hash
    facts_orig = _fact_map(parsed_orig)
    facts_corr = _fact_map(parsed_corr)
    assert facts_orig["BUY_OSTK_LMT"].value_decimal == Decimal("84775")
    assert facts_corr["BUY_OSTK_LMT"].value_decimal == Decimal("84795")
    assert facts_orig["BUY_OSTK_LMT"].status == "VERIFIED"
    assert facts_corr["BUY_OSTK_LMT"].status == "VERIFIED"
    assert facts_orig["BUY_OSTK_LMT"].evidence.rcept_no == ORIG
    assert facts_corr["BUY_OSTK_LMT"].evidence.rcept_no == CORR
    assert facts_orig["BUY_OSTK_LMT"].evidence.document_hash == parsed_orig.document_hash
    assert facts_corr["BUY_OSTK_LMT"].evidence.document_hash == parsed_corr.document_hash
    assert facts_orig["ACQ_OSTK"].value_decimal == Decimal("3652")
    assert facts_corr["ACQ_OSTK"].value_decimal == Decimal("3652")


def test_correction_reads_first_submission_date_from_document() -> None:
    raw = (PROBE / f"{CORR}_document.zip").read_bytes()
    filing = replace(_filing(CORR, raw, date(2024, 6, 28)), correction_flag=True)
    parsed = parse_buyback_document(filing, raw, DocumentLimits())
    assert parsed.first_submission_date == date(2024, 6, 26)


@pytest.mark.parametrize("value", ["2024-06-26", "2024.6.26", "2024년 06월 26일", "`24.6.26"])
def test_correction_reads_first_submission_date_from_table(value: str) -> None:
    xml = (
        '<?xml version="1.0" encoding="utf-8"?><DOCUMENT><BODY><TABLE><TR>'
        f'<TD>정정대상 공시서류의 최초제출일</TD><TD>{value}</TD>'
        '</TR></TABLE></BODY></DOCUMENT>'
    )
    raw = _zip_with_names({"correction.xml": xml.encode("utf-8")})
    filing = replace(_filing(CORR, raw, date(2024, 6, 28)), correction_flag=True)
    assert parse_buyback_document(filing, raw, DocumentLimits()).first_submission_date == date(2024, 6, 26)


def test_correction_without_first_submission_date_stays_unresolved() -> None:
    raw = _wrap(_row("1. 취득예정주식(주)", "보통주식", "ACQ_OSTK", "100"))
    filing = replace(_filing(CORR, raw, date(2024, 6, 28)), correction_flag=True)
    parsed = parse_buyback_document(filing, raw, DocumentLimits())
    assert parsed.first_submission_date is None


def test_invalid_correction_first_submission_date_stays_unresolved() -> None:
    raw = _wrap(_row("1. 취득예정주식(주)", "보통주식", "ACQ_OSTK", "100"))
    xml = (
        '<?xml version="1.0" encoding="utf-8"?><DOCUMENT><BODY>'
        '<P>2. 정정대상 공시서류의 최초제출일 : 2024년 02월 31일</P>'
        '</BODY></DOCUMENT>'
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("correction.xml", xml)
    filing = replace(_filing(CORR, raw, date(2024, 6, 28)), correction_flag=True)
    parsed = parse_buyback_document(filing, buffer.getvalue(), DocumentLimits())
    assert parsed.first_submission_date is None


def test_dash_value_never_becomes_verified_zero() -> None:
    """A '-' amount stays unverified without a numeric zero."""
    raw = _wrap(_row("1. 취득예정주식(주)", "보통주식", "ACQ_OSTK", "-"))
    parsed = parse_buyback_document(_filing(ORIG, raw), raw, DocumentLimits())
    fact = _fact_map(parsed)["ACQ_OSTK"]
    assert fact.status == "UNVERIFIED"
    assert fact.value_decimal is None
    assert fact.value_decimal != Decimal(0)


def test_date_coordinates_preserve_iso_and_evidence() -> None:
    """AUNIT planned dates keep ISO values with receipt-specific coordinates."""
    raw = (PROBE / f"{ORIG}_document.zip").read_bytes()
    parsed = parse_buyback_document(_filing(ORIG, raw), raw, DocumentLimits())
    facts = _fact_map(parsed)
    assert facts["ACQ_BGN"].value_text == "2024-06-27"
    assert facts["ACQ_END"].value_text == "2024-07-03"
    assert facts["ACQ_BGN"].status == "VERIFIED"
    assert facts["ACQ_END"].status == "VERIFIED"
    assert facts["ACQ_BGN"].evidence.source_kind == "AUNIT"
    assert facts["ACQ_BGN"].evidence.source_key == "ACQ_BGN"
    assert facts["ACQ_BGN"].evidence.rcept_no == ORIG
    assert facts["ACQ_BGN"].evidence.document_hash == parsed.document_hash
    assert facts["ACQ_PPS"].status == "VERIFIED"
    assert facts["ACQ_PPS"].value_text is not None
    assert "Stock Grant" in facts["ACQ_PPS"].value_text  # type: ignore[operator]


def test_duplicate_amount_codes_stay_unverified() -> None:
    """Conflicting duplicate amount ACODEs cannot verify an amount."""
    rows = _row("2. 취득예정금액(원)", "보통주식", "ACQ_OSTK_PRC", "159,957,600") + _row(
        "2. 취득예정금액(원)", "보통주식", "ACQ_OSTK_PRC", "159,957,601"
    )
    raw = _wrap(rows)
    parsed = parse_buyback_document(_filing(ORIG, raw), raw, DocumentLimits())
    fact = _fact_map(parsed)["ACQ_OSTK_PRC"]
    assert fact.status == "UNVERIFIED"
    assert fact.value_decimal is None


def test_unsafe_archives_rejected_before_extraction() -> None:
    """Traversal, ambiguous members and expansion over budget fail closed."""
    raw = (PROBE / f"{ORIG}_document.zip").read_bytes()
    filing = _filing(ORIG, raw)
    traversal = _zip_with_names({"../evil.xml": b"<DOCUMENT/>"})
    with pytest.raises(ValueError, match="unsafe"):
        parse_buyback_document(filing, traversal, DocumentLimits())
    ambiguous = _zip_with_names({"a.xml": b"<DOCUMENT/>", "b.xml": b"<DOCUMENT/>"})
    with pytest.raises(ValueError, match="unambiguous"):
        parse_buyback_document(filing, ambiguous, DocumentLimits())
    with pytest.raises(ValueError, match="expansion"):
        parse_buyback_document(filing, raw, DocumentLimits(max_uncompressed_bytes=10))
    with pytest.raises(ValueError, match="malformed"):
        parse_buyback_document(filing, b"PK\x03\x04not-a-zip", DocumentLimits())


def test_label_mismatch_stays_unverified() -> None:
    """A mapped key under a conflicting label is not a verified fact."""
    raw = _wrap(_row("2. 취득예정금액(원)", "기타주식", "ACQ_OSTK_PRC", "159,957,600"))
    parsed = parse_buyback_document(_filing(ORIG, raw), raw, DocumentLimits())
    fact = _fact_map(parsed)["ACQ_OSTK_PRC"]
    assert fact.status == "UNVERIFIED"
    assert fact.value_decimal is None


def test_blank_and_garbled_numbers_stay_unverified() -> None:
    """Blank or nonnumeric quantities never verify."""
    for raw_value in ["", "   ", "12a,34"]:
        raw = _wrap(_row("1. 취득예정주식(주)", "보통주식", "ACQ_OSTK", raw_value))
        parsed = parse_buyback_document(_filing(ORIG, raw), raw, DocumentLimits())
        fact = _fact_map(parsed)["ACQ_OSTK"]
        assert fact.status == "UNVERIFIED"
        assert fact.value_decimal is None


def test_malformed_date_coordinates_stay_unverified() -> None:
    """Date units without a valid YYYYMMDD stamp keep display text unverified."""
    for stamp in ["not-a-date", "20240230"]:
        rows = _date_row("3. 취득예상기간", "시작일", "ACQ_BGN", stamp, "2024년 06월 27일")
        raw = _wrap(rows)
        parsed = parse_buyback_document(_filing(ORIG, raw), raw, DocumentLimits())
        fact = _fact_map(parsed)["ACQ_BGN"]
        assert fact.status == "UNVERIFIED"
        assert fact.value_decimal is None
        assert fact.value_text == "2024년 06월 27일"


def test_dash_purpose_stays_unverified() -> None:
    """A '-' stated purpose is preserved without verification."""
    rows = (
        '<TR ACOPY="N" ADELETE="N">'
        '<TD COLSPAN="3" WIDTH="368" HEIGHT="76">5. 취득목적</TD>'
        '<TE COLSPAN="3" WIDTH="241" HEIGHT="76" ACODE="ACQ_PPS" VALIGN="MIDDLE">-</TE>'
        "</TR>"
    )
    raw = _wrap(rows)
    parsed = parse_buyback_document(_filing(ORIG, raw), raw, DocumentLimits())
    fact = _fact_map(parsed)["ACQ_PPS"]
    assert fact.status == "UNVERIFIED"
    assert fact.value_decimal is None


def test_nested_value_elements_resolve_row_coordinates() -> None:
    """Values wrapped in inline markup still resolve to their row labels."""
    rows = (
        '<TR ACOPY="N" ADELETE="N">'
        '<TD ROWSPAN="2" COLSPAN="2" VALIGN="MIDDLE" WIDTH="267" HEIGHT="60">1. 취득예정주식(주)</TD>'
        '<TD ALIGN="CENTER" WIDTH="101" HEIGHT="30">보통주식</TD>'
        '<TD COLSPAN="3" WIDTH="241" HEIGHT="30"><P><TE ACODE="ACQ_OSTK">3,652</TE></P></TD>'
        "</TR>"
    )
    raw = _wrap(rows)
    parsed = parse_buyback_document(_filing(ORIG, raw), raw, DocumentLimits())
    fact = _fact_map(parsed)["ACQ_OSTK"]
    assert fact.status == "VERIFIED"
    assert fact.value_decimal == Decimal("3652")
    assert fact.evidence.cell is not None
    assert "취득예정주식" in fact.evidence.cell


def test_literal_ampersand_in_dart_text_is_recovered_without_changing_source_hash() -> None:
    """DART receipts sometimes contain unescaped ampersands in company names."""
    malformed = _wrap(_row("취득예정주식(주)", "보통주식", "ACQ_OSTK", "3,652")).replace(
        b"</COMPANY-NAME>", b" & SECURITIES</COMPANY-NAME>"
    )
    parsed = parse_buyback_document(_filing(ORIG, malformed), malformed, DocumentLimits())
    assert parsed.document_hash == hashlib.sha256(malformed).hexdigest()
    assert _fact_map(parsed)["ACQ_OSTK"].value_decimal == Decimal("3652")


def test_archive_guards_reject_malformed_inputs() -> None:
    """Empty, mislabeled, over-budget and malformed archives fail closed."""
    filing = _filing(ORIG, b"PK\x03\x04x")
    with pytest.raises(ValueError, match="empty"):
        parse_buyback_document(filing, b"", DocumentLimits())
    with pytest.raises(ValueError, match="non-empty"):
        parse_buyback_document(replace(filing, rcept_no=""), b"PK\x03\x04x", DocumentLimits())
    broken = _zip_with_names({"20240626000207.xml": b"<DOCUMENT><unclosed>"})
    with pytest.raises(ValueError, match="malformed receipt document"):
        parse_buyback_document(filing, broken, DocumentLimits())
    garbage = b"not-a-zip-at-all"
    with pytest.raises(ValueError, match="malformed receipt archive"):
        parse_buyback_document(filing, garbage, DocumentLimits())
    real = (PROBE / f"{ORIG}_document.zip").read_bytes()
    with pytest.raises(ValueError, match="member budget"):
        parse_buyback_document(filing, real, DocumentLimits(max_members=0))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("dup.xml", b"<DOCUMENT/>")
        archive.writestr("dup.xml", b"<DOCUMENT/>")
    with pytest.raises(ValueError, match="duplicate member"):
        parse_buyback_document(filing, buffer.getvalue(), DocumentLimits())
    backslash = _zip_with_names({"sub\\doc.xml": b"<DOCUMENT/>"})
    with pytest.raises(ValueError, match="unsafe member"):
        parse_buyback_document(filing, backslash, DocumentLimits())
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("nested-dir/", b"")
        archive.writestr("20240626000207.xml", b"<DOCUMENT/>")
    with pytest.raises(ValueError, match="directory member"):
        parse_buyback_document(filing, buffer.getvalue(), DocumentLimits())
