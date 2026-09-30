"""Invariant guards for receipt-specific research context assembly."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src.core.buyback_document import BuybackFact, EvidenceLocation, ParsedBuyback
from src.core.revisions import EventLink
from src.data.catalog import FilingVersion
from src.data.index_store import IndexManifest
from src.data.local_lake import MarketBar, SecurityMatch
from src.integrations.krx_index import IndexBar
from src.research.context import ResearchUnavailable, build_research_context
from src.research.event_study import StudyPolicy

KST = ZoneInfo("Asia/Seoul")
BASE = date(2024, 6, 10)
SESSIONS = [BASE + timedelta(days=offset) for offset in range(14)]
FILING_DATE = SESSIONS[10]
PRIOR = SESSIONS[9]
SAFE = SESSIONS[11]
POLICY = StudyPolicy(estimation_start=-8, estimation_end=-2, min_pairs=3, horizons=(1,))
DOC_HASH = "d" * 64


def _filing(receipt: date = FILING_DATE) -> FilingVersion:
    return FilingVersion(
        rcept_no="20240620000001",
        corp_code="01386916",
        receipt_date=receipt,
        report_name="주요사항보고서(자기주식취득결정)",
        stock_code="000001",
        raw_hash="a" * 64,
        first_observed_at=datetime(2024, 6, 20, 9, 0, tzinfo=KST),
        knowledge_available_at=datetime(2024, 6, 20, 18, 0, tzinfo=KST),
        availability_mode="HISTORICAL_BACKFILL",
        correction_flag=False,
        withdrawal_flag=False,
        parent_rcept_no=None,
        link_status="ORIGINAL",
        time_precision="DATE_ONLY",
    )


def _parsed() -> ParsedBuyback:
    def fact(field: str, value: Decimal, unit: str) -> BuybackFact:
        return BuybackFact(
            field=field,
            value_decimal=value,
            value_text=str(value),
            unit=unit,
            evidence=EvidenceLocation("20240620000001", DOC_HASH, "report.xml", "ACODE", field, "s", "t", "c"),
            status="VERIFIED",
        )

    return ParsedBuyback(
        rcept_no="20240620000001",
        corp_code="01386916",
        first_submission_date=FILING_DATE,
        facts=(fact("ACQ_OSTK_PRC", Decimal(1_000_000_000), "KRW"), fact("ACQ_OSTK", Decimal(10_000), "shares")),
        document_hash=DOC_HASH,
        parse_status="OK",
    )


def _event() -> EventLink:
    return EventLink("buyback:01386916:20240620000001", ("20240620000001",), "LINKED")


def _match(status: str = "OK") -> SecurityMatch:
    return SecurityMatch("KRX:000001", "000001", "KOSPI", "KR7000001001", PRIOR, status)  # type: ignore[arg-type]


def _stock(session: date, close: int = 71000) -> MarketBar:
    return MarketBar(
        instrument_id="KRX:000001",
        session=session,
        open=70000,
        close=close,
        market_cap=700_000_000_000,
        listed_shares=10_000_000,
        trading_value=5_000_000_000,
        ret_price=0.01,
        price_state="tradable",
        gap_before=False,
        share_factor=1.0,
        available_at=datetime(session.year, session.month, session.day, 18, 0, tzinfo=KST),
        source_hash="s" * 64,
    )


def _index(session: date) -> IndexBar:
    level = Decimal(2700 + (session - BASE).days * 3)
    return IndexBar(
        market="KOSPI",
        session=session,
        open=level - 1,
        close=level,
        source_hash="i" * 64,
        batch_available_at=datetime(session.year, session.month, session.day, 18, 0, tzinfo=KST),
    )


class _Catalog:
    def __init__(self, db_path: Path, receipt: date = FILING_DATE) -> None:
        self._db_path = db_path
        self._receipt = receipt

    @property
    def db_path(self) -> Path:
        return self._db_path

    def get_filing_asof(self, rcept_no: str, as_of: datetime) -> FilingVersion | None:
        filing = _filing(self._receipt)
        if rcept_no != filing.rcept_no or as_of < filing.knowledge_available_at:
            return None
        return filing

    def get_artifact_path(self, sha256: str) -> None:
        del sha256
        return None


class _EventStore:
    def __init__(self, resolved: tuple[EventLink, ParsedBuyback] | None) -> None:
        self._resolved = resolved

    def get_event_asof(self, anchor: str, as_of: datetime) -> tuple[EventLink, ParsedBuyback] | None:
        del anchor, as_of
        return self._resolved

    def get_receipt(self, rcept_no: str) -> ParsedBuyback | None:
        del rcept_no
        return None

    def list_prior_events(self, as_of: datetime) -> tuple[()]:
        del as_of
        return ()


class _Lake:
    def __init__(
        self, match: SecurityMatch, bar_none: bool = False, name: tuple[str, str] | None = None
    ) -> None:
        self._match = match
        self._bar_none = bar_none
        self._name = name

    def security_name(self, source_security_id: str, session: date, as_of: datetime) -> tuple[str, str] | None:
        del source_security_id, session, as_of
        return self._name

    def previous_session(self, before: date) -> date | None:
        earlier = [session for session in SESSIONS if session < before]
        return earlier[-1] if earlier else None

    def next_session(self, after: date) -> date | None:
        later = [session for session in SESSIONS if session > after]
        return later[0] if later else None

    def resolve_security(self, stock_code: str, session: date, as_of: datetime) -> SecurityMatch:
        del stock_code, session, as_of
        return self._match

    def market_bar(self, instrument_id: str, session: date, as_of: datetime) -> MarketBar | None:
        del instrument_id
        if self._bar_none or session not in SESSIONS:
            return None
        bar = _stock(session)
        return bar if bar.available_at <= as_of else None

    def market_window(self, instrument_id: str, start: date, end: date, as_of: datetime) -> tuple[MarketBar, ...]:
        del instrument_id
        return tuple(
            bar
            for session in SESSIONS
            if start <= session <= end and (bar := _stock(session)).available_at <= as_of
        )

    def market_universe(self, start: date, end: date, as_of: datetime) -> dict[str, tuple[MarketBar, ...]]:
        return {"KRX:000001": self.market_window("KRX:000001", start, end, as_of)}


class _Financial:
    def facts_asof(self, corp_code: str, as_of: datetime, names: frozenset[str]) -> tuple[()]:
        del corp_code, as_of, names
        return ()


class _IndexStore:
    @property
    def manifest(self) -> IndexManifest:
        return IndexManifest(entries={}, manifest_hash="m" * 64)

    def window(self, market: str, start: date, end: date, as_of: datetime) -> tuple[IndexBar, ...]:
        del market
        return tuple(
            bar
            for session in SESSIONS
            if start <= session <= end and (bar := _index(session)).batch_available_at <= as_of
        )


def _context(
    as_of: datetime,
    tmp_path: Path,
    match: SecurityMatch | None = None,
    receipt: date = FILING_DATE,
    bar_none: bool = False,
    empty_event: bool = False,
    db_name: str = "catalog.sqlite",
) -> object:
    store = _EventStore(None if empty_event else (_event(), _parsed()))
    return build_research_context(
        _Catalog(tmp_path / db_name, receipt),  # type: ignore[arg-type]
        store,  # type: ignore[arg-type]
        _Lake(match or _match(), bar_none),  # type: ignore[arg-type]
        _Financial(),  # type: ignore[arg-type]
        _IndexStore(),  # type: ignore[arg-type]
        "20240620000001",
        as_of,
        POLICY,
    )


def test_request_validation(tmp_path: Path) -> None:
    """Empty receipts and naive instants fail before any local read."""
    late = datetime(2024, 6, 24, 18, 0, tzinfo=KST)
    with pytest.raises(ValueError, match="non-empty"):
        build_research_context(
            _Catalog(tmp_path / "catalog.sqlite"),  # type: ignore[arg-type]
            _EventStore((_event(), _parsed())),  # type: ignore[arg-type]
            _Lake(_match()),  # type: ignore[arg-type]
            _Financial(),  # type: ignore[arg-type]
            _IndexStore(),  # type: ignore[arg-type]
            "",
            late,
            POLICY,
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        _context(datetime(2024, 6, 24, 18, 0), tmp_path)


def test_filing_not_yet_known(tmp_path: Path) -> None:
    """An as-of before the knowledge boundary publishes no filing view."""
    with pytest.raises(ResearchUnavailable, match="FILING_NOT_YET_KNOWN"):
        _context(datetime(FILING_DATE.year, FILING_DATE.month, FILING_DATE.day, 9, 0, tzinfo=KST), tmp_path)


def test_no_prior_session(tmp_path: Path) -> None:
    """A filing on the first known session has no pre-filing denominator session."""
    with pytest.raises(ResearchUnavailable, match="NO_PRIOR_SESSION"):
        _context(datetime(2024, 6, 24, 18, 0, tzinfo=KST), tmp_path, receipt=SESSIONS[0])


def test_missing_prior_bar_withholds_materiality(tmp_path: Path) -> None:
    """A missing prior bar withholds materiality while the study still runs."""
    context = _context(datetime(2024, 6, 24, 18, 0, tzinfo=KST), tmp_path, bar_none=True)
    assert context.materiality.status == "NO_PRIOR_BAR"  # type: ignore[attr-defined]
    assert context.study.first_safe_session == SAFE  # type: ignore[attr-defined]


def test_snapshot_failure_degrades_to_empty(tmp_path: Path) -> None:
    """An unreadable catalog snapshot listing degrades to empty instead of aborting."""
    context = _context(
        datetime(2024, 6, 24, 18, 0, tzinfo=KST), tmp_path, db_name="no-such-dir/catalog.sqlite"
    )
    assert context.snapshot_ids == ()  # type: ignore[attr-defined]


def _link_db(path: Path, rows: list[tuple[str, str, str]]) -> None:
    import sqlite3

    connection = sqlite3.connect(str(path))
    try:
        connection.execute("CREATE TABLE event_link (event_id TEXT PRIMARY KEY, rcept_nos TEXT NOT NULL, status TEXT NOT NULL)")
        connection.executemany("INSERT INTO event_link VALUES (?, ?, ?)", rows)
        connection.commit()
    finally:
        connection.close()


def test_withdrawn_event_withholds_active_claims(tmp_path: Path) -> None:
    """A withdrawn event keeps history but withholds materiality and return claims."""
    import json

    db_path = tmp_path / "withdrawn.sqlite"
    _link_db(
        db_path,
        [
            ("bad-row", "[[[", "LINKED"),
            (_event().event_id, json.dumps(["20240620000001"]), "WITHDRAWN"),
        ],
    )

    class _ReceiptStore(_EventStore):
        def get_receipt(self, rcept_no: str) -> ParsedBuyback | None:
            return _parsed() if rcept_no == "20240620000001" else None

    context = build_research_context(
        _Catalog(db_path),  # type: ignore[arg-type]
        _ReceiptStore(None),  # type: ignore[arg-type]
        _Lake(_match()),  # type: ignore[arg-type]
        _Financial(),  # type: ignore[arg-type]
        _IndexStore(),  # type: ignore[arg-type]
        "20240620000001",
        datetime(2024, 6, 24, 18, 0, tzinfo=KST),
        POLICY,
    )
    assert context.event.status == "WITHDRAWN"  # type: ignore[attr-defined]
    assert context.materiality.status == "WITHDRAWN"  # type: ignore[attr-defined]
    assert context.study.status == "WITHDRAWN"  # type: ignore[attr-defined]


def test_withdrawn_without_parsed_facts_aborts(tmp_path: Path) -> None:
    """A withdrawn event without stored parsed facts cannot build a view."""
    import json

    db_path = tmp_path / "withdrawn-bare.sqlite"
    _link_db(db_path, [(_event().event_id, json.dumps(["20240620000001"]), "WITHDRAWN")])
    with pytest.raises(ResearchUnavailable, match="FILING_NOT_YET_KNOWN"):
        build_research_context(
            _Catalog(db_path),  # type: ignore[arg-type]
            _EventStore(None),  # type: ignore[arg-type]
            _Lake(_match()),  # type: ignore[arg-type]
            _Financial(),  # type: ignore[arg-type]
            _IndexStore(),  # type: ignore[arg-type]
            "20240620000001",
            datetime(2024, 6, 24, 18, 0, tzinfo=KST),
            POLICY,
        )


def test_unresolved_identity_publishes_nothing(tmp_path: Path) -> None:
    """A ticker without a unique prior-session ordinary share raises instead of publishing."""
    with pytest.raises(ResearchUnavailable, match="SECURITY"):
        _context(datetime(2024, 6, 24, 18, 0, tzinfo=KST), tmp_path, _match("AMBIGUOUS"))


def test_unusable_link_rows_publish_nothing(tmp_path: Path) -> None:
    """Unknown link statuses and missing tables resolve to no published context."""
    import json

    bogus = tmp_path / "bogus.sqlite"
    _link_db(bogus, [(_event().event_id, json.dumps(["20240620000001"]), "BOGUS")])
    with pytest.raises(ResearchUnavailable, match="UNRESOLVED_EVENT"):
        _context(datetime(2024, 6, 24, 18, 0, tzinfo=KST), tmp_path, empty_event=True, db_name="bogus.sqlite")
    with pytest.raises(ResearchUnavailable, match="UNRESOLVED_EVENT"):
        _context(datetime(2024, 6, 24, 18, 0, tzinfo=KST), tmp_path, empty_event=True, db_name="fresh.sqlite")
    _link_db(tmp_path / "empty.sqlite", [])
    with pytest.raises(ResearchUnavailable, match="UNRESOLVED_EVENT"):
        _context(datetime(2024, 6, 24, 18, 0, tzinfo=KST), tmp_path, empty_event=True, db_name="empty.sqlite")
    _link_db(tmp_path / "open.sqlite", [(_event().event_id, json.dumps(["20240620000001"]), "UNRESOLVED_LINK")])
    with pytest.raises(ResearchUnavailable, match="UNRESOLVED_EVENT"):
        _context(datetime(2024, 6, 24, 18, 0, tzinfo=KST), tmp_path, empty_event=True, db_name="open.sqlite")
    with pytest.raises(ResearchUnavailable, match="UNRESOLVED_EVENT"):
        _context(
            datetime(2024, 6, 24, 18, 0, tzinfo=KST), tmp_path, empty_event=True, db_name="no-such-dir/c.sqlite"
        )


def test_last_session_keeps_unknown_safe_session(tmp_path: Path) -> None:
    """A filing on the last known session reports no safe session instead of pending forever."""
    context = _context(datetime(2024, 6, 24, 18, 0, tzinfo=KST), tmp_path, receipt=SESSIONS[-1])
    assert context.study.first_safe_session is None  # type: ignore[attr-defined]
    assert context.study.status == "NO_SAFE_SESSION"  # type: ignore[attr-defined]


def test_future_outcome_pending_keeps_filing_facts(tmp_path: Path) -> None:
    """Before the safe-session close, facts are available while outcome returns stay pending."""
    context = _context(datetime(SAFE.year, SAFE.month, SAFE.day, 9, 0, tzinfo=KST), tmp_path)
    assert context.parsed.rcept_no == "20240620000001"  # type: ignore[attr-defined]
    assert context.study.first_safe_session == SAFE  # type: ignore[attr-defined]
    assert context.study.intraday_excess is None  # type: ignore[attr-defined]
    assert "PENDING" in context.study.reasons  # type: ignore[attr-defined]


def test_index_window_carries_only_eligible_bars(tmp_path: Path) -> None:
    """Index bars whose batch availability is after as-of never enter the context."""
    early = _context(datetime(2024, 6, 22, 18, 0, tzinfo=KST), tmp_path)
    late = _context(datetime(2024, 6, 24, 18, 0, tzinfo=KST), tmp_path)
    assert len(early.index_bars) < len(late.index_bars)  # type: ignore[attr-defined]
    assert all(bar.batch_available_at <= datetime(2024, 6, 22, 18, 0, tzinfo=KST) for bar in early.index_bars)  # type: ignore[attr-defined]


def test_company_name_and_its_source_hash_travel_with_the_context(tmp_path: Path) -> None:
    store = _EventStore((_event(), _parsed()))
    args = (
        _Catalog(tmp_path / "catalog.sqlite", FILING_DATE),  # type: ignore[arg-type]
        store,  # type: ignore[arg-type]
        _Lake(_match(), False, ("테스트전자", "n" * 64)),  # type: ignore[arg-type]
        _Financial(),  # type: ignore[arg-type]
        _IndexStore(),  # type: ignore[arg-type]
        "20240620000001",
        datetime(2024, 6, 24, 18, 0, tzinfo=KST),
        POLICY,
    )
    context = build_research_context(*args)
    assert context.company_name == "테스트전자"
    assert context.company_name_source_hash == "n" * 64
    assert "n" * 64 in context.source_hashes
