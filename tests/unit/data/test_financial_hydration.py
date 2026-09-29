"""Invariant guards for event-scoped financial evidence hydration."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath

import polars as pl
import pytest

from src.core.buyback_document import ParsedBuyback
from src.data.catalog import Catalog, FilingVersion
from src.data.event_store import EventStore
from src.data.financial_evidence import FinancialEvidence
from src.data.financial_hydration import HydrationSummary, hydrate_event_financial_evidence
from src.data.imports import ImportManifest, ImportPart
from src.data.local_lake import FACTS_DATASET_ID, LocalLake
from src.integrations.dart import FinancialStatementRequest

CORP_A = "01386916"
CORP_W = "01111111"
CORP_ODD = "CORP0001"
CANDIDATE = "00999999"
FORM = "주요사항보고서(자기주식취득결정)"
AS_OF_2024 = datetime(2024, 6, 4, tzinfo=UTC)
AS_OF_2023 = datetime(2023, 5, 16, tzinfo=UTC)
OBSERVED_2026 = datetime(2026, 3, 2, tzinfo=UTC)


def _sha_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _record(
    filing_id: str,
    corp: str,
    amount: str,
    fiscal: str = "2024Q1",
    consolidated: bool = True,
    hint: float | None = None,
) -> dict[str, object]:
    return {
        "account_id": "ifrs-full_Assets",
        "consolidated": consolidated,
        "corp_code": corp,
        "currency": "KRW",
        "fact": "assets",
        "filing_id": filing_id,
        "fiscal_period": fiscal,
        "ord": "7",
        "rcept_no": filing_id,
        "sj_div": "BS",
        "thstrm_amount": amount,
        "unit": "KRW",
        "value": hint if hint is not None else float(amount),
    }


def _store_evidence(data_root: Path, records: list[dict[str, object]]) -> str:
    raw = json.dumps({"records": records}, sort_keys=True).encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    base = data_root / "imports" / "financial_evidence" / digest
    base.mkdir(parents=True, exist_ok=True)
    (base / "payload.json").write_bytes(raw)
    (base / "receipt.json").write_text(json.dumps({"content_hash": digest}), encoding="utf-8")
    return digest


def _index_rows(entries: list[dict[str, object]]) -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "dart_corp_code": entry["corp"],
                "filing_id": entry["filing"],
                "fact": "assets",
                "fiscal_period": entry["fiscal"],
                "consolidated": entry.get("consolidated", True),
                "available_at": entry["available_at"],
                "value": entry["value"],
                "unit": "KRW",
                "source_hash": entry["source_hash"],
            }
            for entry in entries
        ]
    )


def _lake(data_root: Path, frame: pl.DataFrame) -> LocalLake:
    target = data_root / "imports" / FACTS_DATASET_ID / "part-00000.parquet"
    target.parent.mkdir(parents=True, exist_ok=True)
    frame.write_parquet(target)
    manifest = ImportManifest(
        dataset_id=FACTS_DATASET_ID,
        source_manifest_sha256="0" * 64,
        imported_at=datetime.now(UTC),
        parts=(ImportPart(PurePosixPath("part-00000.parquet"), _sha_of(target), target.stat().st_size),),
    )
    return LocalLake(data_root, {FACTS_DATASET_ID: manifest})


def _seed_event(
    catalog: Catalog, store: EventStore, corp: str, rcept: str, receipt_day: date, known_at: datetime
) -> None:
    raw = b"PK\x03\x04" + rcept.encode()
    digest = catalog.register_artifact(
        "dart", "document", rcept, "seed", raw, known_at, PurePosixPath(f"raw/dart/seed/doc-{rcept}.zip")
    )
    filing = FilingVersion(
        rcept,
        corp,
        receipt_day,
        FORM,
        "361610",
        digest,
        known_at,
        known_at,
        "HISTORICAL_BACKFILL",
        False,
        False,
        None,
        "ORIGINAL",
        "DATE_ONLY",
    )
    catalog.upsert_filing(filing)
    store.store_parsed_batch(
        [filing], [ParsedBuyback(rcept, corp, receipt_day, (), digest, "COMPLETE")]
    )


def _stack(
    tmp_path: Path, events: list[tuple[str, str, date, datetime]], entries: list[dict[str, object]]
) -> tuple[Catalog, EventStore, FinancialEvidence, Path]:
    root = tmp_path / "data"
    catalog = Catalog(root / "catalog.sqlite")
    store = EventStore(catalog)
    for corp, rcept, receipt_day, known_at in events:
        _seed_event(catalog, store, corp, rcept, receipt_day, known_at)
    payload_groups: dict[str, list[dict[str, object]]] = {}
    for record in [entry.pop("_record", None) for entry in entries]:
        if record is not None:
            payload_groups.setdefault(str(record["filing_id"]), []).append(record)
    digests = {filing: _store_evidence(root, records) for filing, records in payload_groups.items()}
    for entry in entries:
        if entry.get("source_hash") is None and str(entry["filing"]) in digests:
            entry["source_hash"] = digests[str(entry["filing"])]
        if entry.get("source_hash") is None:
            entry["source_hash"] = ""
    lake = _lake(root, _index_rows(entries))
    return catalog, store, FinancialEvidence(root, lake), root


def _entry(
    corp: str,
    filing: str,
    fiscal: str,
    available_at: datetime,
    value: float,
    record_amount: str | None = None,
    record_hint: float | None = None,
    consolidated: bool = True,
    source_hash: str | None = None,
) -> dict[str, object]:
    entry: dict[str, object] = {
        "corp": corp,
        "filing": filing,
        "fiscal": fiscal,
        "available_at": available_at,
        "value": value,
        "consolidated": consolidated,
        "source_hash": source_hash,
    }
    if record_amount is not None:
        entry["_record"] = _record(filing, corp, record_amount, fiscal, consolidated, record_hint)
    return entry


def test_issuer_scope_excludes_drive_candidate(tmp_path: Path) -> None:
    """A candidate absent from the verified event store creates no requirement."""
    candidate_record = _record("20240101000009", CANDIDATE, "7000", "2024Q1", True)
    catalog, store, financial, root = _stack(
        tmp_path,
        [(CORP_A, "20240531000001", date(2024, 5, 31), datetime(2024, 6, 3, tzinfo=UTC))],
        [
            _entry(CORP_A, "20240531000001", "2024Q1", datetime(2024, 6, 3, tzinfo=UTC), 1000.0, "1000"),
            {
                "corp": CANDIDATE,
                "filing": "20240101000009",
                "fiscal": "2024Q1",
                "available_at": datetime(2024, 6, 3, tzinfo=UTC),
                "value": 7000.0,
                "consolidated": True,
                "source_hash": None,
                "_record": candidate_record,
            },
        ],
    )
    summary = hydrate_event_financial_evidence(catalog, store, financial, root, AS_OF_2024)
    assert summary.event_corp_count == 1
    assert summary.required_hash_count == 1
    assert summary.verified_hash_count == 1
    assert summary.missing_hashes == ()
    assert summary.missing_requests == ()


def test_missing_and_mismatched_payload_reported_without_fact(tmp_path: Path) -> None:
    """Unresolvable hashes are reported as gaps with collector requests; no fact is emitted."""
    catalog, store, financial, root = _stack(
        tmp_path,
        [
            (CORP_A, "20240531000001", date(2024, 5, 31), datetime(2024, 6, 3, tzinfo=UTC)),
            (CORP_ODD, "20240401000001", date(2024, 4, 1), datetime(2024, 4, 2, tzinfo=UTC)),
        ],
        [
            _entry(CORP_A, "20240531000001", "2024Q1", datetime(2024, 6, 3, tzinfo=UTC), 1000.0, "1000"),
            _entry(
                CORP_A,
                "20240531000002",
                "2024Q1",
                datetime(2024, 6, 3, tzinfo=UTC),
                1000.0,
                consolidated=True,
                source_hash="c" * 64,
            ),
            _entry(
                CORP_A,
                "20240531000003",
                "2024Q2",
                datetime(2024, 6, 3, tzinfo=UTC),
                999999.0,
                "5000",
                None,
                False,
            ),
            _entry(CORP_A, "20240531000004", "2024Q3", datetime(2024, 6, 3, tzinfo=UTC), 100.0, source_hash=""),
            _entry(
                CORP_A,
                "20240531000005",
                "FY2024",
                datetime(2024, 6, 3, tzinfo=UTC),
                100.0,
                source_hash="d" * 64,
            ),
            _entry(
                CORP_A,
                "20240531000006",
                "2010Q1",
                datetime(2024, 6, 3, tzinfo=UTC),
                100.0,
                source_hash="e" * 64,
            ),
            _entry(
                CORP_ODD,
                "20240401000001",
                "2024Q1",
                datetime(2024, 4, 2, tzinfo=UTC),
                100.0,
                source_hash="f" * 64,
            ),
        ],
    )
    summary = hydrate_event_financial_evidence(catalog, store, financial, root, AS_OF_2024)
    assert summary.event_corp_count == 2
    assert summary.required_hash_count == 6
    assert summary.verified_hash_count == 1
    assert len(summary.missing_hashes) == 4
    assert len(summary.unverified_hashes) == 1
    assert tuple(sorted(summary.missing_hashes)) == summary.missing_hashes
    assert summary.missing_requests == (
        FinancialStatementRequest(CORP_A, 2024, "11012", "OFS"),
        FinancialStatementRequest(CORP_A, 2024, "11013", "CFS"),
        FinancialStatementRequest(CORP_A, 2024, "11014", "CFS"),
    )
    verified = financial.facts_asof(CORP_A, AS_OF_2024, frozenset({"assets"}))
    assert [fact.filing_id for fact in verified] == ["20240531000001"]
    assert financial.facts_asof(CORP_ODD, AS_OF_2024, frozenset({"assets"})) == ()


def test_shared_evidence_hash_is_incomplete_when_one_fact_mismatches(tmp_path: Path) -> None:
    """One verified row cannot make another mismatched row under the same hash safe."""
    catalog, store, financial, root = _stack(
        tmp_path,
        [(CORP_A, "20240531000001", date(2024, 5, 31), datetime(2024, 6, 3, tzinfo=UTC))],
        [
            _entry(CORP_A, "20240531000001", "2024Q1", datetime(2024, 6, 3, tzinfo=UTC), 1000.0, "1000"),
            _entry(CORP_A, "20240531000001", "2024Q1", datetime(2024, 6, 3, tzinfo=UTC), 9000.0, "2000"),
        ],
    )
    summary = hydrate_event_financial_evidence(catalog, store, financial, root, AS_OF_2024)
    assert summary.required_hash_count == 1
    assert summary.verified_hash_count == 0
    assert summary.missing_hashes == ()
    assert len(summary.unverified_hashes) == 1
    assert summary.missing_requests == (FinancialStatementRequest(CORP_A, 2024, "11013", "CFS"),)


def test_early_analogue_issuer_in_scope(tmp_path: Path) -> None:
    """A verified 2023 event issuer contributes its 2022+ evidence alongside 2024 targets."""
    catalog, store, financial, root = _stack(
        tmp_path,
        [
            (CORP_W, "20230515000001", date(2023, 5, 15), datetime(2023, 5, 16, tzinfo=UTC)),
            (CORP_A, "20240531000001", date(2024, 5, 31), datetime(2024, 6, 3, tzinfo=UTC)),
        ],
        [
            _entry(CORP_W, "20230515000001", "2023Q1", datetime(2023, 5, 16, tzinfo=UTC), 2000.0, "2000"),
            _entry(CORP_A, "20240531000001", "2024Q1", datetime(2024, 6, 3, tzinfo=UTC), 1000.0, "1000"),
        ],
    )
    summary = hydrate_event_financial_evidence(catalog, store, financial, root, AS_OF_2024)
    assert summary.event_corp_count == 2
    assert summary.required_hash_count == 2
    assert summary.verified_hash_count == 2
    assert summary.missing_hashes == ()
    early = hydrate_event_financial_evidence(catalog, store, financial, root, AS_OF_2023)
    assert early.event_corp_count == 1
    assert early.required_hash_count == 1
    assert early.verified_hash_count == 1


def test_no_retroactive_repair(tmp_path: Path) -> None:
    """A 2026 current-view collection cannot close a 2024 point-in-time gap."""
    from src.data.financial_ingest import collect_financial_snapshot

    catalog, store, financial, root = _stack(
        tmp_path,
        [(CORP_A, "20240531000001", date(2024, 5, 31), datetime(2024, 6, 3, tzinfo=UTC))],
        [
            _entry(
                CORP_A,
                "20240304000001",
                "2024Q1",
                datetime(2024, 5, 15, tzinfo=UTC),
                1000.0,
                source_hash="c" * 64,
            )
        ],
    )
    before = hydrate_event_financial_evidence(catalog, store, financial, root, AS_OF_2024)
    assert before.verified_hash_count == 0
    assert len(before.missing_hashes) == 1

    request = FinancialStatementRequest(CORP_A, 2024, "11013", "CFS")
    payload = json.dumps(
        {
            "status": "000",
            "message": "ok",
            "list": [
                {
                    "rcept_no": "20240304000001",
                    "reprt_code": "11013",
                    "bsns_year": "2024",
                    "corp_code": CORP_A,
                    "sj_div": "BS",
                    "account_id": "ifrs-full_Assets",
                    "ord": "1",
                    "currency": "KRW",
                    "thstrm_amount": "1000",
                }
            ],
        }
    ).encode()

    class _Client:
        def fetch_financial_statement(self, wanted: FinancialStatementRequest) -> bytes:
            assert wanted == request
            return payload

    collection = collect_financial_snapshot(
        _Client(),  # type: ignore[arg-type]
        catalog,
        request,
        root,
        "snap-2026",
        OBSERVED_2026,
    )
    assert collection.row_count == 1
    after = hydrate_event_financial_evidence(catalog, store, financial, root, AS_OF_2024)
    assert after == before
    assert financial.facts_asof(CORP_A, AS_OF_2024, frozenset({"assets"})) == ()


def test_deterministic_retry(tmp_path: Path) -> None:
    """Unchanged stores produce identical issuer, hash, and request inventories."""
    catalog, store, financial, root = _stack(
        tmp_path,
        [(CORP_A, "20240531000001", date(2024, 5, 31), datetime(2024, 6, 3, tzinfo=UTC))],
        [
            _entry(
                CORP_A,
                "20240531000002",
                "2024Q1",
                datetime(2024, 6, 3, tzinfo=UTC),
                1000.0,
                source_hash="c" * 64,
            )
        ],
    )
    first = hydrate_event_financial_evidence(catalog, store, financial, root, AS_OF_2024)
    second = hydrate_event_financial_evidence(catalog, store, financial, root, AS_OF_2024)
    assert first == second
    assert isinstance(first, HydrationSummary)


def test_empty_and_guarded_inputs(tmp_path: Path) -> None:
    """No events audit empty; naive clocks, foreign roots, and absent indexes fail closed."""
    root = tmp_path / "data"
    catalog = Catalog(root / "catalog.sqlite")
    store = EventStore(catalog)
    lake = LocalLake(root, {})
    financial = FinancialEvidence(root, lake)
    assert hydrate_event_financial_evidence(catalog, store, financial, root, AS_OF_2024) == HydrationSummary(
        0, 0, 0, (), ()
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        hydrate_event_financial_evidence(catalog, store, financial, root, datetime(2024, 6, 4))
    with pytest.raises(ValueError, match="one project data root"):
        hydrate_event_financial_evidence(catalog, store, financial, tmp_path / "other", AS_OF_2024)
    _seed_event(catalog, store, CORP_A, "20240531000001", date(2024, 5, 31), datetime(2024, 6, 3, tzinfo=UTC))
    assert hydrate_event_financial_evidence(catalog, store, financial, root, AS_OF_2024) == HydrationSummary(
        1, 0, 0, (), ()
    )
    hollow = LocalLake(
        root,
        {
            FACTS_DATASET_ID: ImportManifest(
                dataset_id=FACTS_DATASET_ID,
                source_manifest_sha256="0" * 64,
                imported_at=datetime.now(UTC),
                parts=(),
            )
        },
    )
    hollow_financial = FinancialEvidence(root, hollow)
    assert hydrate_event_financial_evidence(
        catalog, store, hollow_financial, root, AS_OF_2024
    ) == HydrationSummary(1, 0, 0, (), ())


def test_audit_financial_cli_reports_gaps(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The audit-financial command emits counts and a project-local gap report."""
    from src.cli.main import main

    root = tmp_path / "data"
    catalog = Catalog(root / "catalog.sqlite")
    store = EventStore(catalog)
    _seed_event(catalog, store, CORP_A, "20240531000001", date(2024, 5, 31), datetime(2024, 6, 3, tzinfo=UTC))
    frame = _index_rows(
        [
            {
                "corp": CORP_A,
                "filing": "20240531000002",
                "fiscal": "2024Q1",
                "available_at": datetime(2024, 6, 3, tzinfo=UTC),
                "value": 1000.0,
                "consolidated": True,
                "source_hash": "c" * 64,
            }
        ]
    )
    target = root / "imports" / FACTS_DATASET_ID / "part-00000.parquet"
    target.parent.mkdir(parents=True, exist_ok=True)
    frame.write_parquet(target)
    manifest = {
        "dataset_id": FACTS_DATASET_ID,
        "source_manifest_sha256": "0" * 64,
        "imported_at": datetime.now(UTC).isoformat(),
        "parts": [{"path": "part-00000.parquet", "sha256": _sha_of(target), "bytes": target.stat().st_size}],
    }
    (root / "imports" / FACTS_DATASET_ID / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    assert (
        main(
            [
                "data",
                "audit-financial",
                "--as-of",
                "2024-06-04T00:00:00+00:00",
                "--data-root",
                str(root),
            ]
        )
        == 0
    )
    document = json.loads(capsys.readouterr().out.strip())
    assert document["event_corp_count"] == 1
    assert document["required_hash_count"] == 1
    assert document["verified_hash_count"] == 0
    assert document["missing_hashes"] == ["c" * 64]
    report = Path(document["report"])
    assert report.is_file()
    stored = json.loads(report.read_text(encoding="utf-8"))
    assert stored["missing_requests"] == [
        {"corp_code": CORP_A, "bsns_year": 2024, "reprt_code": "11013", "fs_div": "CFS"}
    ]
    assert main(["data", "audit-financial", "--as-of", "2024-06-04", "--data-root", str(root)]) == 2
