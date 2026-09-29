"""Offline guards for the project-local DART financial document parser."""

from __future__ import annotations

import io
import zipfile

from src.data.dart_statements.document_statements import parse_filing_document


def _archive(assets: str = "1,000") -> bytes:
    rows = [
        ["과목", "제 35 기", "제 34 기"],
        ["자산총계", assets, "900"],
        ["부채총계", "400", "350"],
        ["자본총계", "600", "550"],
        ["현금및현금성자산", "100", "90"],
        ["이익잉여금", "50", "40"],
    ]
    table = "<TABLE>" + "".join(
        "<TR>" + "".join(f"<TD>{cell}</TD>" for cell in row) + "</TR>" for row in rows
    ) + "</TABLE>"
    section = (
        '<TITLE ATOC="Y" AASSOCNOTE="D-0-3-2-0">연결재무제표</TITLE>'
        '<P>재무상태표</P><P>(단위 : 원)</P><P>2019년 12월 31일 현재</P>' + table
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("20200101000001.xml", section)
    return buffer.getvalue()


def test_balanced_statement_yields_source_amounts() -> None:
    result = parse_filing_document(_archive(), reprt_code="11011", biz_year="2019")
    assert result.statements is not None
    assert result.statements.checks == ("bs_balance",)
    assert {item.fact: item.value for item in result.statements.facts} == {
        "assets": 1000, "debt": 400, "equity": 600, "cash": 100,
    }


def test_unbalanced_statement_withholds_all_facts() -> None:
    assert parse_filing_document(_archive("1,001"), reprt_code="11011", biz_year="2019").statements is None
