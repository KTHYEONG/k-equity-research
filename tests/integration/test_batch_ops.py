"""Invariant scenarios for recoverable daily batch and atomic publication."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo

import polars as pl
import pytest

from src.cli.batch import BatchPolicy, run_daily_batch
from src.core.buyback_document import BuybackFact, EvidenceLocation, ParsedBuyback
from src.data.catalog import Catalog, FilingVersion
from src.data.event_store import EventStore
from src.data.imports import ImportManifest, ImportPart
from src.data.index_store import load_index_manifest, merge_index_manifest
from src.data.local_lake import PANEL_DATASET_ID, UNIVERSE_DATASET_ID
from src.integrations.dart import DartSourceError
from src.integrations.krx_index import KrxSourceError

KST = ZoneInfo("Asia/Seoul")
SESSIONS = [date(2024, 5, 30) + timedelta(days=offset) for offset in range(30)]
FILING_DATE = SESSIONS[25]
SAFE = SESSIONS[26]
RCEPT = "20240624000001"


@pytest.fixture(autouse=True)
def _isolate_source_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("OPENDART_API_KEY", "DART_API_KEY", "KRX_OPENAPI_KEY", "KRX_API_KEY"):
        monkeypatch.delenv(name, raising=False)


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
    manifest = ImportManifest(dataset_id, "0" * 64, datetime.now(UTC), tuple(parts))
    return manifest


def _write_import_manifest(data_root: Path, manifest: ImportManifest) -> None:
    target = data_root / "imports" / manifest.dataset_id / "manifest.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "dataset_id": manifest.dataset_id,
        "source_manifest_sha256": manifest.source_manifest_sha256,
        "imported_at": manifest.imported_at.isoformat(),
        "parts": [
            {"path": part.relative_path.as_posix(), "sha256": part.sha256, "bytes": part.byte_length}
            for part in manifest.parts
        ],
    }
    target.write_bytes((json.dumps(payload, sort_keys=True, indent=2) + "\n").encode())


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


def _parsed(rcept_no: str, document_hash: str) -> ParsedBuyback:
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
        first_submission_date=FILING_DATE,
        facts=(fact("ACQ_OSTK_PRC", Decimal(14_000_000_000), "KRW"), fact("ACQ_OSTK", Decimal(200_000), "shares")),
        document_hash=document_hash,
        parse_status="OK",
    )


def _seed_base(data_root: Path) -> Catalog:
    panel = _manifest(data_root, PANEL_DATASET_ID, {"year=2024/part.parquet": pl.DataFrame(_panel_rows())})
    universe_frames = {
        f"session={session.isoformat()}/part.parquet": pl.DataFrame([row])
        for session, row in zip(SESSIONS, _universe_rows(), strict=True)
    }
    universe = _manifest(data_root, UNIVERSE_DATASET_ID, universe_frames)
    _write_import_manifest(data_root, panel)
    _write_import_manifest(data_root, universe)
    catalog = Catalog(data_root / "catalog.sqlite")
    dart_raw = b'{"rcept": "20240624000001"}'
    dart_hash = catalog.register_artifact(
        source="dart",
        endpoint="document",
        request_key=RCEPT,
        snapshot_id="seed",
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
            snapshot_id="seed",
            raw_bytes=raw,
            retrieved_at=datetime(2026, 9, 24, 12, 0, tzinfo=KST),
            local_relative_path=PurePosixPath(f"raw/krx/KOSPI-{session.strftime('%Y%m%d')}.json"),
        )
        accepted.append(("KOSPI", session, digest))
    merge_index_manifest(None, accepted, data_root)
    store = EventStore(catalog)
    filing = _filing(RCEPT, "000001", dart_hash)
    catalog.upsert_filing(filing)
    store.store_parsed_batch([filing], [_parsed(RCEPT, dart_hash)])
    return catalog


def _policy() -> BatchPolicy:
    return BatchPolicy(
        dart_start=date(2024, 6, 1),
        dart_end=date(2024, 6, 30),
        recheck_days=365,
        publish_time_kst=time(18, 30),
        max_attempts=3,
    )


class _FailingDartClient:
    """Fails on page 2 on first use, then serves both pages."""

    def __init__(self) -> None:
        self.calls = 0

    def list_major_reports(self, start: date, end: date, page: int):  # type: ignore[no-untyped-def]
        from src.integrations.dart import DartListPage

        self.calls += 1
        if page == 2 and self.calls <= 2:
            raise DartSourceError("TRANSPORT", True, "boom")
        raw = json.dumps(
            {"status": "000", "message": "ok", "page_no": page, "page_count": 2, "total_count": 0, "list": []}
        ).encode()
        return DartListPage(page_no=page, page_count=2, total_count=0, raw_bytes=raw, rows=())

    def document_zip(self, rcept_no: str) -> bytes:
        raise AssertionError("no candidates expected")


class _FailingKrxClient:
    def fetch_day(self, market: str, session: date) -> bytes:  # type: ignore[no-untyped-def]
        raise KrxSourceError("NO_DATA", False, "unavailable")


class _BrokenModel:
    def generate_json(self, messages, schema_name):  # type: ignore[no-untyped-def]
        raise ConnectionError("model down")


def test_interrupted_page_resumes_without_partial_memo(tmp_path: Path) -> None:

    data_root = tmp_path / "data"
    _seed_base(data_root)
    as_of = datetime(2024, 6, 29, 18, 0, tzinfo=KST)
    dart = _FailingDartClient()
    first = run_daily_batch(_policy(), data_root, as_of, dart_client=dart, krx_client=_FailingKrxClient())
    assert first.memos_published == 0
    assert any("DART_WINDOW_INCOMPLETE" in failure for failure in first.failures)
    assert not list(data_root.rglob("*.partial"))
    second = run_daily_batch(_policy(), data_root, as_of, dart_client=dart, krx_client=_FailingKrxClient())
    assert second.run_id != first.run_id
    assert second.memos_published >= 1
    assert not list(data_root.rglob("*.partial"))


def test_late_correction_preserves_earlier_report(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    catalog = _seed_base(data_root)
    as_of_first = datetime(2024, 6, 29, 18, 0, tzinfo=KST)
    first = run_daily_batch(_policy(), data_root, as_of_first)
    first_dirs = sorted((data_root / "reports").rglob("manifest.json"))
    assert first_dirs
    first_payloads = [path.read_bytes() for path in first_dirs]
    first_memos = {path: path.read_bytes() for path in (data_root / "reports").rglob("memo.json")}

    correction_no = "20240626000009"
    raw = b'{"rcept": "correction"}'
    digest = catalog.register_artifact(
        source="dart",
        endpoint="document",
        request_key=correction_no,
        snapshot_id="late",
        raw_bytes=raw,
        retrieved_at=datetime(2024, 6, 26, 19, 0, tzinfo=KST),
        local_relative_path=PurePosixPath(f"raw/dart/{correction_no}.zip"),
    )
    correction = FilingVersion(
        rcept_no=correction_no,
        corp_code="01386916",
        receipt_date=date(2024, 6, 26),
        report_name="주요사항보고서(자기주식취득결정)",
        stock_code="000001",
        raw_hash=digest,
        first_observed_at=datetime(2024, 6, 26, 19, 0, tzinfo=KST),
        knowledge_available_at=datetime(2024, 6, 26, 19, 0, tzinfo=KST),
        availability_mode="LIVE",
        correction_flag=True,
        withdrawal_flag=False,
        parent_rcept_no=RCEPT,
        link_status="CORRECTION",
        time_precision="DATE_ONLY",
    )
    catalog.upsert_filing(correction)
    EventStore(catalog).store_parsed_batch([correction], [_parsed(correction_no, digest)])

    as_of_second = as_of_first
    second = run_daily_batch(_policy(), data_root, as_of_second)
    assert second.run_id != first.run_id
    for payload in first_payloads:
        assert payload in [path.read_bytes() for path in sorted((data_root / "reports").rglob("manifest.json"))]
    assert all(path.read_bytes() == payload for path, payload in first_memos.items())
    assert second.memos_published >= 1
    repeated = run_daily_batch(_policy(), data_root, as_of_second)
    assert repeated.run_id == second.run_id
    assert all(path.read_bytes() == payload for path, payload in first_memos.items())


def test_model_outage_publishes_baseline(tmp_path: Path) -> None:
    from src.agent.workflow import AgentPolicy

    data_root = tmp_path / "data"
    _seed_base(data_root)
    as_of = datetime(2024, 6, 29, 18, 0, tzinfo=KST)
    summary = run_daily_batch(
        _policy(),
        data_root,
        as_of,
        agent_mode=True,
        agent_model=_BrokenModel(),  # type: ignore[arg-type]
        agent_policy=AgentPolicy(3, 30.0, "v1"),
    )
    assert summary.memos_published >= 1
    assert any("AGENT" in failure for failure in summary.failures)
    memos = [json.loads(path.read_bytes().decode()) for path in data_root.rglob("memo.json")]
    assert memos
    assert all("AGENT" in " ".join(memo["statuses"]) or memo["statuses"] for memo in memos)


def test_source_independence_uses_only_local_data(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    data_root = tmp_path / "data"
    _seed_base(data_root)
    monkeypatch.setenv("DART_API_KEY", "")
    monkeypatch.setenv("KRX_API_KEY", "")
    other = tmp_path / "other-project"
    other.mkdir()
    (other / "secret.parquet").write_bytes(b"external")
    as_of = datetime(2024, 6, 29, 18, 0, tzinfo=KST)
    summary = run_daily_batch(_policy(), data_root, as_of)
    assert summary.memos_published >= 1
    assert summary.failures == () or all("AGENT" not in failure for failure in summary.failures)
    batch_manifest = data_root / "batch" / summary.run_id / "manifest.json"
    assert batch_manifest.is_file()
    assert hashlib.sha256(batch_manifest.read_bytes()).hexdigest() == summary.manifest_hash


def test_disjoint_historical_index_manifests_form_valid_pinned_union(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    _seed_base(data_root)
    extra_day = date(2024, 6, 30)
    raw = _index_payload(extra_day, Decimal("2800"))
    catalog = Catalog(data_root / "catalog.sqlite")
    digest = catalog.register_artifact(
        source="krx",
        endpoint="index",
        request_key="KOSPI:20240630",
        snapshot_id="extra",
        raw_bytes=raw,
        retrieved_at=datetime(2024, 6, 30, 18, 0, tzinfo=KST),
        local_relative_path=PurePosixPath("raw/krx/KOSPI-20240630.json"),
    )
    merge_index_manifest(None, [("KOSPI", extra_day, digest)], data_root)
    summary = run_daily_batch(_policy(), data_root, datetime(2024, 6, 30, 18, 0, tzinfo=KST))
    batch = json.loads((data_root / "batch" / summary.run_id / "manifest.json").read_text())
    pinned = load_index_manifest(data_root, batch["index_manifest_hash"])
    assert pinned.entries[("KOSPI", extra_day)] == digest
    assert ("KOSPI", SESSIONS[15]) in pinned.entries
