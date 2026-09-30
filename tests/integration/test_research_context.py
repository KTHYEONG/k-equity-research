"""Vertical-slice guards for receipt-specific research context over local data."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo

import polars as pl
import pytest

from src.core.buyback_document import BuybackFact, EvidenceLocation, ParsedBuyback
from src.data.catalog import Catalog, FilingVersion
from src.data.event_store import EventStore
from src.data.financial_evidence import FinancialEvidence
from src.data.imports import ImportManifest, ImportPart
from src.data.index_store import IndexStore, merge_index_manifest
from src.data.local_lake import FACTS_DATASET_ID, PANEL_DATASET_ID, UNIVERSE_DATASET_ID, LocalLake
from src.research.context import ResearchUnavailable, build_research_context
from src.research.event_study import StudyPolicy

KST = ZoneInfo("Asia/Seoul")
SESSIONS = [date(2024, 5, 30) + timedelta(days=offset) for offset in range(30)]
FILING_DATE = SESSIONS[25]
SAFE = SESSIONS[26]
RCEPT = "20240624000001"
POLICY = StudyPolicy(estimation_start=-10, estimation_end=-2, min_pairs=5, horizons=(1,))


def _sha_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _manifest(data_root: Path, dataset_id: str, frames: dict[str, pl.DataFrame]) -> ImportManifest:
    parts = []
    for relative, frame in frames.items():
        target = data_root / "imports" / dataset_id / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        frame.write_parquet(target)
        parts.append(ImportPart(PurePosixPath(relative), _sha_of(target), target.stat().st_size))
    return ImportManifest(dataset_id, "0" * 64, datetime.now(UTC), tuple(parts))


def _panel_rows() -> list[dict[str, object]]:
    rows = [
        {
            "instrument_id": "KRX:000001",
            "session": session,
            "market": "KOSPI",
            "ticker": "000001",
            "open": 70000 + position,
            "close": 71000 + position,
            "market_cap": 700_000_000_000,
            "listed_shares": 10_000_000,
            "trading_value": 5_000_000_000,
            "ret_price": 0.02 if position % 2 == 0 else -0.01,
            "price_state": "tradable",
            "gap_before": False,
            "share_factor": 1.0,
            "available_at": datetime(session.year, session.month, session.day, 18, 0, tzinfo=KST),
            "source_hash": hashlib.sha256(f"panel:{session.isoformat()}".encode()).hexdigest(),
        }
        for position, session in enumerate(SESSIONS)
    ]
    rows[19]["market_cap"] = 0
    return rows


def _universe_rows() -> list[dict[str, object]]:
    return [
        {
            "instrument_id": "KRX:000001",
            "ticker": "000001",
            "market": "KOSPI",
            "source_security_id": "KR7000001001",
            "share_kind": "보통주",
            "session": session,
            "available_at": datetime(session.year, session.month, session.day, 15, 30, tzinfo=KST),
        }
        for session in SESSIONS
    ]


def _index_payload(session: date, close: Decimal) -> bytes:
    return json.dumps(
        {
            "OutBlock_1": [
                {
                    "BAS_DD": session.strftime("%Y%m%d"),
                    "IDX_CLSS": "KOSPI",
                    "IDX_NM": "코스피",
                    "OPNPRC_IDX": str(close - 1),
                    "CLSPRC_IDX": str(close),
                }
            ]
        }
    ).encode("utf-8")


def _filing(rcept_no: str, stock_code: str, raw_hash: str) -> FilingVersion:
    return FilingVersion(
        rcept_no=rcept_no,
        corp_code="01386916",
        receipt_date=FILING_DATE,
        report_name="주요사항보고서(자기주식취득결정)",
        stock_code=stock_code,
        raw_hash=raw_hash,
        first_observed_at=datetime(2024, 6, 24, 9, 0, tzinfo=KST),
        knowledge_available_at=datetime(2024, 6, 24, 18, 0, tzinfo=KST),
        availability_mode="HISTORICAL_BACKFILL",
        correction_flag=False,
        withdrawal_flag=False,
        parent_rcept_no=None,
        link_status="ORIGINAL",
        time_precision="DATE_ONLY",
    )


def _parsed(rcept_no: str, document_hash: str, first_date: date | None = None) -> ParsedBuyback:
    def fact(field: str, value: Decimal, unit: str) -> BuybackFact:
        return BuybackFact(
            field=field,
            value_decimal=value,
            value_text=str(value),
            unit=unit,
            evidence=EvidenceLocation(rcept_no, document_hash, "report.xml", "ACODE", field, "s", "t", "c"),
            status="VERIFIED",
        )

    return ParsedBuyback(
        rcept_no=rcept_no,
        corp_code="01386916",
        first_submission_date=first_date or FILING_DATE,
        facts=(fact("ACQ_OSTK_PRC", Decimal(14_000_000_000), "KRW"), fact("ACQ_OSTK", Decimal(200_000), "shares")),
        document_hash=document_hash,
        parse_status="OK",
    )


@pytest.fixture
def rig(tmp_path: Path) -> dict[str, object]:
    data_root = tmp_path / "data"
    panel = _manifest(data_root, PANEL_DATASET_ID, {"year=2024/part.parquet": pl.DataFrame(_panel_rows())})
    universe_frames = {
        f"session={session.isoformat()}/part.parquet": pl.DataFrame([row])
        for session, row in zip(SESSIONS, _universe_rows(), strict=True)
    }
    universe = _manifest(data_root, UNIVERSE_DATASET_ID, universe_frames)
    lake = LocalLake(data_root, {PANEL_DATASET_ID: panel, UNIVERSE_DATASET_ID: universe})
    catalog = Catalog(data_root / "catalog.sqlite")
    dart_raw = b'{"rcept": "20240624000001"}'
    dart_hash = catalog.register_artifact(
        source="dart",
        endpoint="document",
        request_key=RCEPT,
        snapshot_id="dart-snap",
        raw_bytes=dart_raw,
        retrieved_at=datetime(2026, 9, 24, 12, 0, tzinfo=KST),
        local_relative_path=PurePosixPath("raw/dart/20240624000001.zip"),
    )
    accepted = []
    for position, session in enumerate(SESSIONS[15:29], start=15):
        raw = _index_payload(session, Decimal(2700 + position * 3))
        digest = catalog.register_artifact(
            source="krx",
            endpoint="index",
            request_key=f"KOSPI:{session.strftime('%Y%m%d')}",
            snapshot_id="krx-snap",
            raw_bytes=raw,
            retrieved_at=datetime(2026, 9, 24, 12, 0, tzinfo=KST),
            local_relative_path=PurePosixPath(f"raw/krx/KOSPI-{session.strftime('%Y%m%d')}.json"),
        )
        accepted.append(("KOSPI", session, digest))
    manifest = merge_index_manifest(None, accepted, data_root)
    index_store = IndexStore(catalog, data_root, manifest)
    event_store = EventStore(catalog)
    filing = _filing(RCEPT, "000001", dart_hash)
    catalog.upsert_filing(filing)
    event_store.store_parsed_batch([filing], [_parsed(RCEPT, dart_hash)])
    prior_raw = b'{"rcept": "20240620000003"}'
    prior_hash = catalog.register_artifact(
        source="dart",
        endpoint="document",
        request_key="20240620000003",
        snapshot_id="dart-snap",
        raw_bytes=prior_raw,
        retrieved_at=datetime(2026, 9, 24, 12, 0, tzinfo=KST),
        local_relative_path=PurePosixPath("raw/dart/20240620000003.zip"),
    )
    prior_filing = FilingVersion(
        rcept_no="20240620000003",
        corp_code="01386916",
        receipt_date=SESSIONS[20],
        report_name="주요사항보고서(자기주식취득결정)",
        stock_code="000001",
        raw_hash=prior_hash,
        first_observed_at=datetime(2024, 6, 19, 9, 0, tzinfo=KST),
        knowledge_available_at=datetime(2024, 6, 19, 18, 0, tzinfo=KST),
        availability_mode="HISTORICAL_BACKFILL",
        correction_flag=False,
        withdrawal_flag=False,
        parent_rcept_no=None,
        link_status="ORIGINAL",
        time_precision="DATE_ONLY",
    )
    catalog.upsert_filing(prior_filing)
    event_store.store_parsed_batch([prior_filing], [_parsed("20240620000003", prior_hash, SESSIONS[20])])

    def _extra(
        rcept_no: str, stock_code: str, receipt: date, knowledge: datetime, raw: bytes = b"{}",
    ) -> None:
        digest = catalog.register_artifact(
            source="dart",
            endpoint="document",
            request_key=rcept_no,
            snapshot_id="dart-snap",
            raw_bytes=raw,
            retrieved_at=datetime(2026, 9, 24, 12, 0, tzinfo=KST),
            local_relative_path=PurePosixPath(f"raw/dart/{rcept_no}.zip"),
        )
        extra_filing = FilingVersion(
            rcept_no=rcept_no,
            corp_code="01386916",
            receipt_date=receipt,
            report_name="주요사항보고서(자기주식취득결정)",
            stock_code=stock_code,
            raw_hash=digest,
            first_observed_at=knowledge,
            knowledge_available_at=knowledge,
            availability_mode="HISTORICAL_BACKFILL",
            correction_flag=False,
            withdrawal_flag=False,
            parent_rcept_no=None,
            link_status="ORIGINAL",
            time_precision="DATE_ONLY",
        )
        catalog.upsert_filing(extra_filing)
        event_store.store_parsed_batch([extra_filing], [_parsed(rcept_no, digest, receipt)])

    _extra("20240619000004", "000001", SESSIONS[0], datetime(2024, 5, 30, 18, 0, tzinfo=KST), b'{"r":4}')
    _extra("20240619000005", "888888", SESSIONS[18], datetime(2024, 6, 17, 18, 0, tzinfo=KST), b'{"r":5}')
    _extra("20240624000006", "000001", SESSIONS[24], datetime(2024, 6, 26, 18, 0, tzinfo=KST), b'{"r":6}')
    _extra("20240621000007", "000001", SESSIONS[21], datetime(2024, 6, 20, 18, 0, tzinfo=KST), b'{"r":7}')
    financial = FinancialEvidence(data_root, lake)
    return {
        "catalog": catalog,
        "event_store": event_store,
        "lake": lake,
        "financial": financial,
        "index_store": index_store,
        "dart_hash": dart_hash,
        "manifest": manifest,
    }


def test_self_contained_slice_uses_only_local_data(rig: dict[str, object]) -> None:
    """Imported 2024 files and local raw evidence back every context field."""
    as_of = datetime(2024, 6, 29, 18, 0, tzinfo=KST)
    context = build_research_context(
        rig["catalog"],  # type: ignore[arg-type]
        rig["event_store"],  # type: ignore[arg-type]
        rig["lake"],  # type: ignore[arg-type]
        rig["financial"],  # type: ignore[arg-type]
        rig["index_store"],  # type: ignore[arg-type]
        RCEPT,
        as_of,
        POLICY,
    )
    assert context.anchor_rcept_no == RCEPT
    assert context.active_rcept_no == RCEPT
    assert context.materiality.amount_to_market_cap == Decimal(14_000_000_000) / Decimal(700_000_000_000)
    assert context.materiality.shares_to_listed_shares == Decimal(200_000) / Decimal(10_000_000)
    assert context.study.first_safe_session == SAFE
    assert context.study.intraday_excess is not None
    assert context.index_manifest_hash == rig["manifest"].manifest_hash
    assert rig["dart_hash"] in context.source_hashes
    assert context.artifact_paths[rig["dart_hash"]] == PurePosixPath("raw/dart/20240624000001.zip")
    assert {"dart-snap", "krx-snap"} <= set(context.snapshot_ids)
    assert context.financial_facts == ()


def test_context_includes_local_bronze_verified_financial_fact(rig: dict[str, object]) -> None:
    catalog = rig["catalog"]
    assert isinstance(catalog, Catalog)
    data_root = catalog.db_path.parent
    record = {
        "account_id": "ifrs-full_Assets",
        "sj_div": "BS",
        "ord": "7",
        "rcept_no": "20240514001363",
        "filing_id": "20240514001363",
        "corp_code": "01386916",
        "fact": "assets",
        "fiscal_period": "2024Q1",
        "consolidated": True,
        "currency": "KRW",
        "unit": "KRW",
        "thstrm_amount": "4021576839000",
    }
    payload = json.dumps({"records": [record]}, sort_keys=True).encode()
    digest = hashlib.sha256(payload).hexdigest()
    evidence_dir = data_root / "imports" / "financial_evidence" / digest
    evidence_dir.mkdir(parents=True)
    (evidence_dir / "payload.json").write_bytes(payload)
    (evidence_dir / "receipt.json").write_text(json.dumps({"content_hash": digest}), encoding="utf-8")
    facts = _manifest(
        data_root,
        FACTS_DATASET_ID,
        {"part-00000.parquet": pl.DataFrame([{
            "dart_corp_code": "01386916",
            "filing_id": "20240514001363",
            "fact": "assets",
            "fiscal_period": "2024Q1",
            "consolidated": True,
            "available_at": datetime(2024, 5, 15, tzinfo=UTC),
            "value": 4021576839000.0,
            "unit": "KRW",
            "source_hash": digest,
        }])},
    )
    manifests = {FACTS_DATASET_ID: facts}
    for dataset_id in (PANEL_DATASET_ID, UNIVERSE_DATASET_ID):
        base = data_root / "imports" / dataset_id
        parts = tuple(
            ImportPart(PurePosixPath(path.relative_to(base).as_posix()), _sha_of(path), path.stat().st_size)
            for path in sorted(base.rglob("*.parquet"))
        )
        manifests[dataset_id] = ImportManifest(dataset_id, "0" * 64, datetime.now(UTC), parts)
    lake = LocalLake(data_root, manifests)
    context = build_research_context(
        catalog,
        rig["event_store"],  # type: ignore[arg-type]
        lake,
        FinancialEvidence(data_root, lake),
        rig["index_store"],  # type: ignore[arg-type]
        RCEPT,
        datetime(2024, 6, 29, 18, 0, tzinfo=KST),
        POLICY,
    )
    assert len(context.financial_facts) == 1
    assert context.financial_facts[0].value == Decimal("4021576839000")
    assert context.financial_facts[0].source_hash == digest


def test_unresolved_identity_publishes_nothing(rig: dict[str, object]) -> None:
    """A ticker without a unique prior-session ordinary share raises."""
    catalog = rig["catalog"]
    assert isinstance(catalog, Catalog)
    raw = b'{"rcept": "20240624000002"}'
    digest = catalog.register_artifact(
        source="dart",
        endpoint="document",
        request_key="20240624000002",
        snapshot_id="dart-snap",
        raw_bytes=raw,
        retrieved_at=datetime(2026, 9, 24, 12, 0, tzinfo=KST),
        local_relative_path=PurePosixPath("raw/dart/20240624000002.zip"),
    )
    filing = _filing("20240624000002", "999999", digest)
    catalog.upsert_filing(filing)
    store = rig["event_store"]
    assert isinstance(store, EventStore)
    store.store_parsed_batch([filing], [_parsed("20240624000002", digest)])
    with pytest.raises(ResearchUnavailable):
        build_research_context(
            rig["catalog"],  # type: ignore[arg-type]
            rig["event_store"],  # type: ignore[arg-type]
            rig["lake"],  # type: ignore[arg-type]
            rig["financial"],  # type: ignore[arg-type]
            rig["index_store"],  # type: ignore[arg-type]
            "20240624000002",
            datetime(2024, 6, 29, 18, 0, tzinfo=KST),
            POLICY,
        )


def test_future_outcome_pending(rig: dict[str, object]) -> None:
    """Before the next-session close, filing facts are available but outcome returns wait."""
    context = build_research_context(
        rig["catalog"],  # type: ignore[arg-type]
        rig["event_store"],  # type: ignore[arg-type]
        rig["lake"],  # type: ignore[arg-type]
        rig["financial"],  # type: ignore[arg-type]
        rig["index_store"],  # type: ignore[arg-type]
        RCEPT,
        datetime(SAFE.year, SAFE.month, SAFE.day, 9, 0, tzinfo=KST),
        POLICY,
    )
    assert context.parsed.rcept_no == RCEPT
    assert context.study.first_safe_session == SAFE
    assert context.study.intraday_excess is None
    assert "PENDING" in context.study.reasons


def test_prior_outcome_keeps_own_source_hashes(rig: dict[str, object]) -> None:
    """A historical profile retains its own filing, denominator, and first-safe hashes."""
    from datetime import time

    from src.research.context import _profile_prior_event

    catalog = rig["catalog"]
    event_store = rig["event_store"]
    lake = rig["lake"]
    index_store = rig["index_store"]
    assert isinstance(catalog, Catalog)
    as_of = datetime(2024, 6, 29, 18, 0, tzinfo=KST)
    cap = datetime.combine(FILING_DATE, time.min).replace(tzinfo=KST)
    prior_as_of = min(as_of, cap)
    pairs = event_store.list_prior_events(as_of)  # type: ignore[union-attr]
    target = next((link, original) for link, original in pairs if original.rcept_no == "20240621000007")
    link, original = target
    profile = _profile_prior_event(
        catalog, event_store, lake, index_store, link, original, prior_as_of, POLICY  # type: ignore[arg-type]
    )
    assert profile is not None
    prior_filing = catalog.get_filing_asof("20240621000007", prior_as_of)
    assert prior_filing is not None
    assert prior_filing.raw_hash in profile.source_hashes
    assert len(profile.source_hashes) == len(set(profile.source_hashes))
    assert tuple(sorted(profile.source_hashes)) == profile.source_hashes
    if profile.first_safe_intraday_excess is not None:
        assert profile.outcome_available_at is not None
        assert len(profile.source_hashes) >= 4
    target_panel_hash = hashlib.sha256(f"panel:{SAFE.isoformat()}".encode()).hexdigest()
    assert target_panel_hash not in profile.source_hashes


def test_context_covers_selection_hashes_with_own_paths(rig: dict[str, object]) -> None:
    """Selected and ranking-only candidate hashes each appear with their own catalog paths."""
    context = build_research_context(
        rig["catalog"],  # type: ignore[arg-type]
        rig["event_store"],  # type: ignore[arg-type]
        rig["lake"],  # type: ignore[arg-type]
        rig["financial"],  # type: ignore[arg-type]
        rig["index_store"],  # type: ignore[arg-type]
        RCEPT,
        datetime(2024, 6, 29, 18, 0, tzinfo=KST),
        POLICY,
    )
    for digest in context.comparables.selection_source_hashes:
        assert digest in context.source_hashes
    for observation in context.comparables.analogue_observations:
        for digest in observation.source_hashes:
            assert digest in context.source_hashes
    catalog = rig["catalog"]
    assert isinstance(catalog, Catalog)
    for digest, path in context.artifact_paths.items():
        assert catalog.get_artifact_path(digest) == path
    assert rig["dart_hash"] in context.artifact_paths


def test_unavailable_first_safe_pair_withholds_outcome(rig: dict[str, object]) -> None:
    """A missing prior index bar withholds the evidenced outcome instead of substituting hashes."""
    from datetime import time

    from src.research.context import _profile_prior_event

    catalog = rig["catalog"]
    assert isinstance(catalog, Catalog)
    data_root = catalog.db_path.parent
    manifest = rig["manifest"]
    prior_safe = SESSIONS[22]
    kept = [(market, session, digest) for (market, session), digest in manifest.entries.items() if session != prior_safe]  # type: ignore[union-attr]
    pruned = merge_index_manifest(None, kept, data_root)
    pruned_store = IndexStore(catalog, data_root, pruned)
    as_of = datetime(2024, 6, 29, 18, 0, tzinfo=KST)
    cap = datetime.combine(FILING_DATE, time.min).replace(tzinfo=KST)
    prior_as_of = min(as_of, cap)
    event_store = rig["event_store"]
    pairs = event_store.list_prior_events(as_of)  # type: ignore[union-attr]
    target = next((link, original) for link, original in pairs if original.rcept_no == "20240621000007")
    link, original = target
    profile = _profile_prior_event(
        catalog, event_store, rig["lake"], pruned_store, link, original, prior_as_of, POLICY  # type: ignore[arg-type]
    )
    assert profile is not None
    assert profile.first_safe_intraday_excess is None
    assert profile.outcome_available_at is None


def test_aggregate_outcomes_match_full_context_and_omit_unresolved(rig: dict[str, object]) -> None:
    """The lean aggregate path reproduces the context study and drops only unresolved securities."""
    from src.research.aggregate import collect_outcomes

    as_of = datetime(2024, 6, 29, 18, 0, tzinfo=KST)
    catalog, event_store = rig["catalog"], rig["event_store"]
    seen: list[tuple[int, int]] = []
    outcomes = collect_outcomes(
        catalog, event_store, rig["lake"], rig["index_store"], as_of, POLICY,  # type: ignore[arg-type]
        lambda done, total: seen.append((done, total)),
    )
    linked = event_store.list_prior_events(as_of)  # type: ignore[attr-defined]
    assert len(outcomes) < len(linked)
    assert seen
    assert seen[-1] == (len(linked), len(linked))
    by_id = {outcome.event_id: outcome for outcome in outcomes}
    context = build_research_context(
        catalog, event_store, rig["lake"], rig["financial"], rig["index_store"], RCEPT, as_of, POLICY,  # type: ignore[arg-type]
    )
    measured = by_id[context.event.event_id]
    assert measured.horizon_car == dict(context.study.horizon_car)
    assert measured.intraday_excess == context.study.intraday_excess
    assert measured.status == context.study.status
    assert measured.amount_ratio == context.materiality.amount_to_market_cap
    assert measured.market == "KOSPI"
