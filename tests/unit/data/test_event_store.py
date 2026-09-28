"""Invariant guards for versioned buyback fact storage and as-of event reads."""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo

import pytest

from src.core.buyback_document import BuybackFact, EvidenceLocation, ParsedBuyback
from src.data.catalog import Catalog, FilingVersion
from src.data.event_store import EventStore

KST = ZoneInfo("Asia/Seoul")
FORM = "주요사항보고서(자기주식취득결정)"
CORR_FORM = "[기재정정]" + FORM


def _open(tmp_path: Path) -> tuple[Catalog, EventStore]:
    catalog = Catalog(tmp_path / "data" / "catalog.sqlite")
    return catalog, EventStore(catalog)


def _evidence(rcept_no: str, doc_hash: str, key: str = "BUY_OSTK_LMT") -> EvidenceLocation:
    return EvidenceLocation(
        rcept_no=rcept_no,
        document_hash=doc_hash,
        member_name=f"{rcept_no}.xml",
        source_kind="ACODE",
        source_key=key,
        section="자기주식 취득 결정",
        table="TBL_ACQ_LMT",
        cell="10. 1일 매수 주문수량 한도 / 보통주식",
    )


def _parsed(rcept_no: str, limit: str, day: date) -> ParsedBuyback:
    doc_hash = hashlib.sha256(rcept_no.encode()).hexdigest()
    return ParsedBuyback(
        rcept_no=rcept_no,
        corp_code="01386916",
        first_submission_date=day,
        facts=(
            BuybackFact(
                field="BUY_OSTK_LMT",
                value_decimal=Decimal(limit.replace(",", "")),
                value_text=limit,
                unit="shares",
                evidence=_evidence(rcept_no, doc_hash),
                status="VERIFIED",
            ),
        ),
        document_hash=doc_hash,
        parse_status="OK",
    )


def _register(catalog: Catalog, rcept_no: str, day: date, parent: str | None = None) -> FilingVersion:
    raw = (rcept_no + "-bytes").encode()
    catalog.register_artifact(
        source="dart",
        endpoint="document",
        request_key=rcept_no,
        snapshot_id="snap-1",
        raw_bytes=raw,
        retrieved_at=datetime(2024, 6, 27, 9, 0, tzinfo=KST),
        local_relative_path=PurePosixPath(f"raw/dart/snap-1/doc-{rcept_no}.zip"),
    )
    available = datetime(2024, 6, 27, 9, 0, tzinfo=KST) if parent is None else datetime(2024, 6, 28, 9, 0, tzinfo=KST)
    filing = FilingVersion(
        rcept_no=rcept_no,
        corp_code="01386916",
        receipt_date=day,
        report_name=CORR_FORM if parent else FORM,
        stock_code="361610",
        raw_hash=hashlib.sha256(raw).hexdigest(),
        first_observed_at=datetime(2024, 6, 27, 9, 0, tzinfo=KST),
        knowledge_available_at=available,
        availability_mode="HISTORICAL_BACKFILL",
        correction_flag=parent is not None,
        withdrawal_flag=False,
        parent_rcept_no=parent,
        link_status="ORIGINAL",
        time_precision="DATE_ONLY",
    )
    catalog.upsert_filing(filing)
    return filing


def test_version_specific_retrieval_switches_only_after_eligibility(tmp_path: Path) -> None:
    """Original facts never change; the active event version advances only at its boundary."""
    catalog, store = _open(tmp_path)
    original = _register(catalog, "20240626000207", date(2024, 6, 26))
    correction = _register(catalog, "20240626000369", date(2024, 6, 26), parent="20240626000207")
    parsed_orig = _parsed("20240626000207", "84,775", date(2024, 6, 26))
    parsed_corr = _parsed("20240626000369", "84,795", date(2024, 6, 26))
    store.store_parsed_batch([original, correction], [parsed_orig, parsed_corr])
    store.store_parsed_batch([original, correction], [parsed_orig, parsed_corr])

    kept = store.get_receipt("20240626000207")
    assert kept is not None
    assert kept.document_hash == parsed_orig.document_hash
    assert kept.facts[0].value_decimal == Decimal("84775")

    before = datetime(2024, 6, 27, 9, 0, tzinfo=KST)
    resolved_before = store.get_event_asof("20240626000207", before)
    assert resolved_before is not None
    link_before, active_before = resolved_before
    assert active_before.rcept_no == "20240626000207"
    assert set(link_before.rcept_nos) == {"20240626000207", "20240626000369"}
    assert store.get_event_asof("20240626000369", before) is None
    assert store.get_event_asof("20240626000207", datetime(2024, 6, 1, 9, 0, tzinfo=KST)) is None
    assert store.list_prior_events(datetime(2024, 6, 1, 9, 0, tzinfo=KST)) == ()

    after = datetime(2024, 6, 28, 9, 0, tzinfo=KST)
    resolved = store.get_event_asof("20240626000207", after)
    assert resolved is not None
    assert resolved[1].rcept_no == "20240626000369"
    assert resolved[1].facts[0].value_decimal == Decimal("84795")

    events_before = store.list_prior_events(before)
    assert len(events_before) == 1
    assert events_before[0][1].rcept_no == "20240626000207"
    events_after = store.list_prior_events(after)
    assert len(events_after) == 1

    assert store.get_receipt("missing") is None
    assert store.get_event_asof("missing", after) is None
    conflicting = ParsedBuyback(
        rcept_no=parsed_orig.rcept_no,
        corp_code=parsed_orig.corp_code,
        first_submission_date=parsed_orig.first_submission_date,
        facts=parsed_orig.facts,
        document_hash="0" * 64,
        parse_status="OK",
    )
    try:
        store.store_parsed_batch([original], [conflicting])
        raise AssertionError("expected conflict")
    except ValueError:
        pass
    assert store.get_receipt("missing") is None
    assert store.get_event_asof("missing", after) is None


def test_unresolved_event_withheld_while_raw_receipts_stay_auditable(tmp_path: Path) -> None:
    """Ambiguous same-day filings return no merged event but keep raw receipts."""
    catalog, store = _open(tmp_path)
    first = _register(catalog, "20240626000207", date(2024, 6, 26))
    second = _register(catalog, "20240626000208", date(2024, 6, 26))
    store.store_parsed_batch(
        [first, second],
        [_parsed("20240626000207", "84,775", date(2024, 6, 26)), _parsed("20240626000208", "84,775", date(2024, 6, 26))],
    )
    as_of = datetime(2024, 6, 28, 9, 0, tzinfo=KST)
    assert store.get_event_asof("20240626000207", as_of) is None
    assert store.list_prior_events(as_of) == ()
    assert store.get_receipt("20240626000207") is not None
    assert store.get_receipt("20240626000208") is not None


def test_withdrawn_event_inactive_while_original_replayable(tmp_path: Path) -> None:
    """After withdrawal eligibility the event is inactive but the original persists."""
    catalog, store = _open(tmp_path)
    original = _register(catalog, "20240626000207", date(2024, 6, 26))
    raw = b"20240626000999-bytes"
    catalog.register_artifact(
        source="dart",
        endpoint="document",
        request_key="20240626000999",
        snapshot_id="snap-1",
        raw_bytes=raw,
        retrieved_at=datetime(2024, 6, 27, 9, 0, tzinfo=KST),
        local_relative_path=PurePosixPath("raw/dart/snap-1/doc-20240626000999.zip"),
    )
    withdrawal = FilingVersion(
        rcept_no="20240626000999",
        corp_code="01386916",
        receipt_date=date(2024, 6, 29),
        report_name=FORM,
        stock_code="361610",
        raw_hash=hashlib.sha256(raw).hexdigest(),
        first_observed_at=datetime(2024, 6, 29, 9, 0, tzinfo=KST),
        knowledge_available_at=datetime(2024, 6, 30, 9, 0, tzinfo=KST),
        availability_mode="HISTORICAL_BACKFILL",
        correction_flag=False,
        withdrawal_flag=True,
        parent_rcept_no="20240626000207",
        link_status="ORIGINAL",
        time_precision="DATE_ONLY",
    )
    catalog.upsert_filing(withdrawal)
    parsed_orig = _parsed("20240626000207", "84,775", date(2024, 6, 26))
    doc_hash = hashlib.sha256(b"20240626000999").hexdigest()
    parsed_wd = ParsedBuyback(
        rcept_no="20240626000999",
        corp_code="01386916",
        first_submission_date=date(2024, 6, 29),
        facts=(
            BuybackFact(
                field="BUY_OSTK_LMT",
                value_decimal=Decimal("84775"),
                value_text="84,775",
                unit="shares",
                evidence=_evidence("20240626000999", doc_hash),
                status="VERIFIED",
            ),
        ),
        document_hash=doc_hash,
        parse_status="OK",
    )
    store.store_parsed_batch([original, withdrawal], [parsed_orig, parsed_wd])
    before = datetime(2024, 6, 28, 9, 0, tzinfo=KST)
    assert store.get_event_asof("20240626000207", before) is None
    after = datetime(2024, 6, 30, 9, 0, tzinfo=KST)
    assert store.get_event_asof("20240626000207", after) is None
    assert store.list_prior_events(after) == ()
    assert store.get_receipt("20240626000207") is not None


def test_store_guards_and_clock_validation(tmp_path: Path) -> None:
    """Orphan rows are skipped, schema drift fails, and naive clocks are rejected."""
    catalog, store = _open(tmp_path)
    EventStore(catalog)
    store._conn.execute(  # noqa: SLF001
        "INSERT INTO buyback_receipt (rcept_no, corp_code, first_submission_date, document_hash, parse_status) VALUES (?, ?, ?, ?, ?)",
        ("orphan-1", "01386916", None, "0" * 64, "OK"),
    )
    store._conn.commit()
    original = _register(catalog, "20240626000207", date(2024, 6, 26))
    store.store_parsed_batch([original], [_parsed("20240626000207", "84,775", date(2024, 6, 26))])
    assert store.get_receipt("orphan-1") is not None
    with pytest.raises(ValueError, match="without filing"):
        store.store_parsed_batch([original], [_parsed("20240626000999", "1", date(2024, 6, 26))])
    with pytest.raises(ValueError, match="timezone"):
        store.get_event_asof("20240626000207", datetime(2024, 6, 28, 9, 0))
    with pytest.raises(ValueError, match="timezone"):
        store.list_prior_events(datetime(2024, 6, 28, 9, 0))
    import sqlite3

    conn = sqlite3.connect(str(tmp_path / "data" / "catalog.sqlite"))
    conn.execute("UPDATE event_schema_version SET version=999")
    conn.commit()
    conn.close()
    with pytest.raises(ValueError, match="unsupported event store"):
        EventStore(catalog)


def test_collection_wires_parsing_and_event_links(tmp_path: Path) -> None:
    """Collected ZIPs parse into receipt facts and conservative links at the anchor."""
    from src.core.buyback_document import DocumentLimits
    from src.data.dart_ingest import collect_buyback_window
    from src.integrations.dart import DartListPage, DartListRow

    repo_root = Path(__file__).resolve().parents[3]
    raw_zip = (repo_root / "data/probe_dart/20240626000207_document.zip").read_bytes()

    class _Lake:
        def next_session(self, after: date) -> date | None:
            del after
            return date(2024, 6, 27)

        def previous_session(self, before: date) -> date | None:
            del before
            return date(2024, 6, 25)

    class _Client:
        def list_major_reports(self, start: date, end: date, page: int) -> DartListPage:
            del start, end, page
            row = DartListRow(
                rcept_no="20240626000207",
                corp_code="01386916",
                stock_code="361610",
                corp_cls="Y",
                report_name=FORM,
                rcept_date=date(2024, 6, 26),
                rm="",
            )
            return DartListPage(page_no=1, page_count=1, total_count=1, raw_bytes=b"{}", rows=(row,))

        def document_zip(self, rcept_no: str) -> bytes:
            assert rcept_no == "20240626000207"
            return raw_zip

    catalog, store = _open(tmp_path)
    root = tmp_path / "data"
    summary = collect_buyback_window(
        _Client(),  # type: ignore[arg-type]
        catalog,
        _Lake(),  # type: ignore[arg-type]
        date(2024, 6, 26),
        date(2024, 6, 26),
        root,
        "snap-wire",
        "HISTORICAL_BACKFILL",
        DocumentLimits(),
        store,
    )
    assert summary.documents_registered == 1
    receipt = store.get_receipt("20240626000207")
    assert receipt is not None
    limits = {fact.field: fact for fact in receipt.facts}["BUY_OSTK_LMT"]
    assert limits.value_decimal == Decimal("84775")
    resolved = store.get_event_asof("20240626000207", datetime(2024, 6, 28, 9, 0, tzinfo=KST))
    assert resolved is not None
    assert resolved[1].rcept_no == "20240626000207"


def test_batch_without_catalog_filing_keeps_receipt_but_no_link(tmp_path: Path) -> None:
    """Parsed receipts without a catalog filing stay retrievable without event output."""
    catalog, store = _open(tmp_path)
    orphan_filing = FilingVersion(
        rcept_no="20240626000207",
        corp_code="01386916",
        receipt_date=date(2024, 6, 26),
        report_name=FORM,
        stock_code="361610",
        raw_hash="b" * 64,
        first_observed_at=datetime(2024, 6, 27, 9, 0, tzinfo=KST),
        knowledge_available_at=datetime(2024, 6, 27, 9, 0, tzinfo=KST),
        availability_mode="HISTORICAL_BACKFILL",
        correction_flag=False,
        withdrawal_flag=False,
        parent_rcept_no=None,
        link_status="ORIGINAL",
        time_precision="DATE_ONLY",
    )
    store.store_parsed_batch([orphan_filing], [_parsed("20240626000207", "84,775", date(2024, 6, 26))])
    assert store.get_receipt("20240626000207") is not None
    assert store.get_event_asof("20240626000207", datetime(2024, 6, 28, 9, 0, tzinfo=KST)) is None
    assert store.list_prior_events(datetime(2024, 6, 28, 9, 0, tzinfo=KST)) == ()
