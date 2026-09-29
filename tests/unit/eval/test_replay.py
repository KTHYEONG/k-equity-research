"""Invariant guards for frozen replay and stratified evaluation."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo

import polars as pl
import pytest

from src.agent.workflow import AgentPolicy
from src.data.catalog import Catalog, FilingVersion
from src.data.event_store import EventStore
from src.data.imports import ImportManifest, ImportPart
from src.data.index_store import merge_index_manifest
from src.data.local_lake import PANEL_DATASET_ID, UNIVERSE_DATASET_ID
from src.core.buyback_document import BuybackFact, EvidenceLocation, ParsedBuyback
from src.eval.replay import (
    ReplayCase,
    evaluate_cases,
    load_cases,
    render_report_markdown,
    replay_case,
    report_to_dict,
)
from src.research.context import build_research_context
from src.research.analogue_proof import AnalogueProof
from src.research.event_study import StudyPolicy
from src.eval.replay import _detect_future_leak as detect_future_leak

KST = ZoneInfo("Asia/Seoul")
SESSIONS = [date(2024, 6, 10) + timedelta(days=offset) for offset in range(8)]
RCEPT = "20240620000001"
CORRECTION = "20240626000369"
KNOWLEDGE = datetime(2024, 6, 24, 18, 0, tzinfo=KST)
AS_OF = datetime(2024, 6, 25, 18, 0, tzinfo=KST)
POLICY = StudyPolicy(estimation_start=-10, estimation_end=-2, min_pairs=5, horizons=(1,))


def _sha_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _persist_dataset(data_root: Path, dataset_id: str, frame: pl.DataFrame) -> None:
    relative = "part.parquet"
    target = data_root / "imports" / dataset_id / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    frame.write_parquet(target)
    document = {
        "dataset_id": dataset_id,
        "source_manifest_sha256": "0" * 64,
        "imported_at": datetime(2024, 6, 1, 12, 0, tzinfo=KST).isoformat(),
        "parts": [{"path": relative, "sha256": _sha_of(target), "bytes": target.stat().st_size}],
    }
    (target.parent / "manifest.json").write_text(json.dumps(document) + "\n", encoding="utf-8")


def _panel_frame() -> pl.DataFrame:
    return pl.DataFrame(
        [
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
    )


def _universe_frame() -> pl.DataFrame:
    return pl.DataFrame(
        [
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
    )


def _parsed(rcept_no: str, document_hash: str, amount: Decimal, first_date: date) -> ParsedBuyback:
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
        first_submission_date=first_date,
        facts=(fact("ACQ_OSTK_PRC", amount, "KRW"), fact("ACQ_OSTK", Decimal(200_000), "shares")),
        document_hash=document_hash,
        parse_status="OK",
    )


def _filing(
    rcept_no: str,
    receipt_date: date,
    knowledge: datetime,
    raw_hash: str,
    parent: str | None = None,
) -> FilingVersion:
    return FilingVersion(
        rcept_no=rcept_no,
        corp_code="01386916",
        receipt_date=receipt_date,
        report_name="주요사항보고서(자기주식취득결정)",
        stock_code="000001",
        raw_hash=raw_hash,
        first_observed_at=knowledge,
        knowledge_available_at=knowledge,
        availability_mode="HISTORICAL_BACKFILL",
        correction_flag=parent is not None,
        withdrawal_flag=False,
        parent_rcept_no=parent,
        link_status="ORIGINAL",
        time_precision="DATE_ONLY",
    )


def _add_filing(
    catalog: Catalog,
    store: EventStore,
    rcept_no: str,
    receipt_date: date,
    knowledge: datetime,
    amount: Decimal,
    raw: bytes,
    parent: str | None = None,
) -> str:
    digest = catalog.register_artifact(
        source="dart",
        endpoint="document",
        request_key=rcept_no,
        snapshot_id="dart-snap",
        raw_bytes=raw,
        retrieved_at=datetime(2026, 9, 24, 12, 0, tzinfo=KST),
        local_relative_path=PurePosixPath(f"raw/dart/{rcept_no}.zip"),
    )
    filing = _filing(rcept_no, receipt_date, knowledge, digest, parent)
    catalog.upsert_filing(filing)
    parent_filing = catalog.get_filing_asof(parent, datetime.max.replace(tzinfo=KST)) if parent else None
    first_date = parent_filing.receipt_date if parent_filing is not None else receipt_date
    store.store_parsed_batch([filing], [_parsed(rcept_no, digest, amount, first_date)])
    return digest


def _rig(tmp_path: Path, with_index: bool = True) -> dict[str, object]:
    data_root = tmp_path / "data"
    _persist_dataset(data_root, PANEL_DATASET_ID, _panel_frame())
    _persist_dataset(data_root, UNIVERSE_DATASET_ID, _universe_frame())
    catalog = Catalog(data_root / "catalog.sqlite")
    if with_index:
        accepted = []
        for position, session in enumerate(SESSIONS):
            raw = json.dumps(
                {
                    "OutBlock_1": [
                        {
                            "BAS_DD": session.strftime("%Y%m%d"),
                            "IDX_CLSS": "KOSPI",
                            "IDX_NM": "코스피",
                            "OPNPRC_IDX": str(2700 + position * 3 - 1),
                            "CLSPRC_IDX": str(2700 + position * 3),
                        }
                    ]
                }
            ).encode("utf-8")
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
        merge_index_manifest(None, accepted, data_root)
    store = EventStore(catalog)
    dart_hash = _add_filing(catalog, store, RCEPT, SESSIONS[5], KNOWLEDGE, Decimal(14_000_000_000), b'{"r":1}')
    return {"data_root": data_root, "dart_hash": dart_hash}


def _case(
    data_root: Path,
    rcept_no: str = RCEPT,
    as_of: datetime = AS_OF,
    hashes: tuple[str, ...] | None = None,
    facts: dict[str, str | None] | None = None,
    cohort: str = "development",
    snapshot_id: str = "dart-snap",
    index_manifest_hash: str | None = None,
) -> ReplayCase:
    rig_hashes = hashes if hashes is not None else ()
    resolved_facts = facts if facts is not None else {"planned_amount_krw": "14000000000", "planned_shares": "200000"}
    names = sorted((data_root / "krx" / "manifests").glob("*.json"))
    pinned_index = index_manifest_hash or (names[0].stem if names else "0" * 64)
    return ReplayCase(
        case_id="case-1",
        rcept_no=rcept_no,
        as_of=as_of,
        snapshot_id=snapshot_id,
        index_manifest_hash=pinned_index,
        expected_source_hashes=rig_hashes,
        expected_facts=resolved_facts,
        expected_citations={"planned_amount_krw": "filing-fact-acq_ostk_prc"},
        cohort=cohort,
    )


def test_changed_source_hash_fails_before_memo(tmp_path: Path) -> None:
    """A pinned hash that is unknown or whose bytes changed fails before memo generation."""
    rig = _rig(tmp_path)
    data_root = rig["data_root"]
    assert isinstance(data_root, Path)
    dart_hash = rig["dart_hash"]
    assert isinstance(dart_hash, str)
    stale = replay_case(_case(data_root, hashes=(dart_hash, "f" * 64)), data_root)
    assert stale.status == "SOURCE_CHANGED"
    assert stale.fact_matches == 0
    assert stale.manifest_hash == ""
    target = data_root / f"raw/dart/{RCEPT}.zip"
    target.write_bytes(b"tampered")
    tampered = replay_case(_case(data_root, hashes=(dart_hash,)), data_root)
    assert tampered.status == "SOURCE_CHANGED"
    target.unlink()
    missing = replay_case(_case(data_root, hashes=(dart_hash,)), data_root)
    assert missing.status == "SOURCE_CHANGED"


def test_replay_rejects_proof_without_analogue_citation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Replay refuses a proof that the generated memo cannot cite."""
    rig = _rig(tmp_path)
    data_root = rig["data_root"]
    assert isinstance(data_root, Path)
    invalid = AnalogueProof(payload=b"invalid proof", sha256="0" * 64, input_hashes=())
    monkeypatch.setattr("src.eval.replay.build_analogue_proof", lambda context: invalid)
    result = replay_case(_case(data_root), data_root)
    assert result.status == "SOURCE_CHANGED"


def test_case_before_correction_keeps_earlier_facts(tmp_path: Path) -> None:
    """An as-of before a correction never sees later facts and no leak is counted."""
    rig = _rig(tmp_path)
    data_root = rig["data_root"]
    assert isinstance(data_root, Path)
    catalog = Catalog(data_root / "catalog.sqlite")
    store = EventStore(catalog)
    _add_filing(
        catalog,
        store,
        CORRECTION,
        SESSIONS[6],
        datetime(2024, 6, 30, 18, 0, tzinfo=KST),
        Decimal(15_000_000_000),
        b'{"r":2}',
        parent=RCEPT,
    )
    dart_hash = rig["dart_hash"]
    assert isinstance(dart_hash, str)
    result = replay_case(_case(data_root, hashes=(dart_hash,)), data_root)
    assert result.status == "OK"
    assert result.fact_matches == 2
    assert result.fact_errors == 0
    report = evaluate_cases([_case(data_root, hashes=(dart_hash,))], data_root)
    assert report.future_leak_count == 0
    assert report.case_count == 1


def test_event_versions_never_split_across_cohorts(tmp_path: Path) -> None:
    """An original and its 2026 correction stay in one cohort instead of splitting."""
    data_root = tmp_path / "data"
    _persist_dataset(data_root, PANEL_DATASET_ID, _panel_frame())
    _persist_dataset(data_root, UNIVERSE_DATASET_ID, _universe_frame())
    merge_index_manifest(None, [], data_root)
    catalog = Catalog(data_root / "catalog.sqlite")
    store = EventStore(catalog)
    original_hash = _add_filing(
        catalog,
        store,
        RCEPT,
        date(2025, 6, 20),
        datetime(2025, 6, 24, 18, 0, tzinfo=KST),
        Decimal(14_000_000_000),
        b'{"r":1}',
    )
    _add_filing(
        catalog,
        store,
        CORRECTION,
        date(2026, 1, 5),
        datetime(2026, 6, 30, 18, 0, tzinfo=KST),
        Decimal(15_000_000_000),
        b'{"r":2}',
        parent=RCEPT,
    )
    original = _case(data_root, 
        as_of=datetime(2025, 6, 25, 18, 0, tzinfo=KST),
        hashes=(original_hash,),
        cohort="development",
    )
    correction = dataclasses.replace(
        _case(data_root, 
            rcept_no=CORRECTION,
            as_of=datetime(2026, 7, 1, 18, 0, tzinfo=KST),
            hashes=(original_hash,),
            facts={"planned_amount_krw": "15000000000", "planned_shares": "200000"},
            cohort="holdout",
        ),
        case_id="case-2",
    )
    report = evaluate_cases([original, correction], data_root)
    assert report.cohort_counts == {"holdout": 2}
    assert report.cohort_counts.get("development", 0) == 0


def test_verified_and_refused_filings_use_separate_denominators(tmp_path: Path) -> None:
    """Precision counts compared facts while coverage counts every expected fact."""
    rig = _rig(tmp_path)
    data_root = rig["data_root"]
    assert isinstance(data_root, Path)
    dart_hash = rig["dart_hash"]
    assert isinstance(dart_hash, str)
    refused = dataclasses.replace(
        _case(data_root, rcept_no="99999999999999", hashes=(), facts={"planned_amount_krw": "1"}),
        case_id="case-2",
    )
    report = evaluate_cases([_case(data_root, hashes=(dart_hash,)), refused], data_root)
    assert report.parser_precision_den == 2
    assert report.parser_precision == 1.0
    assert report.parser_coverage_den == 3
    assert report.parser_coverage == pytest.approx(2 / 3)
    assert report.refusal_num == 1
    assert report.refusal_rate == pytest.approx(1 / 2)


def test_changed_policy_creates_new_run_identity(tmp_path: Path) -> None:
    """A different prompt version yields a different run identity for identical cases."""
    rig = _rig(tmp_path)
    data_root = rig["data_root"]
    assert isinstance(data_root, Path)
    dart_hash = rig["dart_hash"]
    assert isinstance(dart_hash, str)
    case = _case(data_root, hashes=(dart_hash,))

    class _SlowModel:
        def generate_json(self, messages: object, schema_name: str) -> dict[str, object]:
            del messages, schema_name
            raise TimeoutError("slow")

    first = evaluate_cases([case], data_root)
    repeat = evaluate_cases([case], data_root)
    assert first.run_id == repeat.run_id
    policy = AgentPolicy(max_tool_calls=3, model_timeout_seconds=5.0, prompt_version="v2")
    other = evaluate_cases([case], data_root, _SlowModel(), policy)  # type: ignore[arg-type]
    assert other.run_id != first.run_id
    assert other.run_id.startswith("eval-")


def test_report_serialization_keeps_exact_denominators(tmp_path: Path) -> None:
    """Report JSON and Markdown carry exact numerators and denominators."""
    rig = _rig(tmp_path)
    data_root = rig["data_root"]
    assert isinstance(data_root, Path)
    dart_hash = rig["dart_hash"]
    assert isinstance(dart_hash, str)
    report = evaluate_cases([_case(data_root, hashes=(dart_hash,))], data_root)
    document = report_to_dict(report)
    assert document["parser_precision_den"] == 2
    assert document["parser_coverage_den"] == 2
    assert document["citation_precision_den"] == 1
    assert document["run_id"] == report.run_id
    markdown = render_report_markdown(report)
    assert report.run_id in markdown
    assert "(2/2)" in markdown
    assert "(1/1)" in markdown


def test_mismatched_values_count_separate_errors(tmp_path: Path) -> None:
    """Wrong fact values and unknown citation IDs count as errors, not silence."""
    rig = _rig(tmp_path)
    data_root = rig["data_root"]
    assert isinstance(data_root, Path)
    dart_hash = rig["dart_hash"]
    assert isinstance(dart_hash, str)
    case = _case(data_root, 
        hashes=(dart_hash,),
        facts={"planned_amount_krw": "14000000000", "planned_shares": "WRONG"},
    )
    case = dataclasses.replace(
        case, expected_citations={"planned_amount_krw": "filing-fact-acq_ostk_prc", "planned_shares": "missing-id"}
    )
    result = replay_case(case, data_root)
    assert result.status == "OK"
    assert result.fact_matches == 1
    assert result.fact_errors == 1
    assert result.citation_matches == 1
    assert result.citation_errors == 1


def test_empty_evaluation_reports_zero_rates(tmp_path: Path) -> None:
    """No cases yields zero rates with an explicit empty-cohort marker."""
    data_root = tmp_path / "data"
    data_root.mkdir(parents=True, exist_ok=True)
    report = evaluate_cases([], data_root)
    assert report.case_count == 0
    assert report.parser_precision == 0.0
    assert report.parser_coverage == 0.0
    assert "- none" in render_report_markdown(report)


def test_snapshot_pin_mismatch_rejects_replay(tmp_path: Path) -> None:
    """A case pinned to another snapshot cannot replay against this data root."""
    rig = _rig(tmp_path)
    data_root = rig["data_root"]
    assert isinstance(data_root, Path)
    dart_hash = rig["dart_hash"]
    assert isinstance(dart_hash, str)
    result = replay_case(_case(data_root, hashes=(dart_hash,), snapshot_id="other-snap"), data_root)
    assert result.status == "SOURCE_CHANGED"


def test_index_manifest_pin_survives_unrelated_new_manifest(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    data_root = rig["data_root"]
    assert isinstance(data_root, Path)
    dart_hash = rig["dart_hash"]
    assert isinstance(dart_hash, str)
    case = _case(data_root, hashes=(dart_hash,))
    original = replay_case(case, data_root)
    assert original.status == "OK"
    for number in range(1, 100):
        other = merge_index_manifest(
            None, [("KOSPI", SESSIONS[0], f"{number:064x}")], data_root
        )
        if other.manifest_hash < case.index_manifest_hash:
            break
    assert other.manifest_hash < case.index_manifest_hash
    repeated = replay_case(case, data_root)
    assert repeated.status == "OK"
    assert repeated.manifest_hash == original.manifest_hash
    report = evaluate_cases([case], data_root)
    assert case.index_manifest_hash in report.source_manifest_hashes
    assert other.manifest_hash not in report.source_manifest_hashes


def test_missing_index_manifest_hash_rejects_case(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    data_root = rig["data_root"]
    assert isinstance(data_root, Path)
    case = dataclasses.replace(_case(data_root), index_manifest_hash="f" * 64)
    result = replay_case(case, data_root)
    assert result.status == "SOURCE_CHANGED"
    assert result.fact_matches == 0


def test_missing_pinned_index_artifact_rejects_replay(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    data_root = rig["data_root"]
    assert isinstance(data_root, Path)
    case = _case(data_root)
    raw = data_root / "raw" / "krx" / f"KOSPI-{SESSIONS[0]:%Y%m%d}.json"
    raw.unlink()
    result = replay_case(case, data_root)
    assert result.status == "SOURCE_CHANGED"


def test_symlinked_raw_parent_rejects_replay(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    data_root = rig["data_root"]
    assert isinstance(data_root, Path)
    dart_hash = rig["dart_hash"]
    assert isinstance(dart_hash, str)
    case = _case(data_root, hashes=(dart_hash,))
    raw_dir = data_root / "raw" / "dart"
    outside = tmp_path / "outside_raw"
    raw_dir.rename(outside)
    raw_dir.symlink_to(outside, target_is_directory=True)
    result = replay_case(case, data_root)
    assert result.status == "SOURCE_CHANGED"


def test_naive_case_instant_rejected(tmp_path: Path) -> None:
    """A naive case as-of fails before any local read."""
    rig = _rig(tmp_path)
    data_root = rig["data_root"]
    assert isinstance(data_root, Path)
    case = dataclasses.replace(_case(data_root), as_of=datetime(2024, 6, 25, 18, 0))
    with pytest.raises(ValueError, match="timezone-aware"):
        replay_case(case, data_root)


def test_invalid_import_manifest_rejected(tmp_path: Path) -> None:
    """A corrupt local import manifest fails loudly instead of replaying."""
    rig = _rig(tmp_path)
    data_root = rig["data_root"]
    assert isinstance(data_root, Path)
    (data_root / "imports" / PANEL_DATASET_ID / "manifest.json").write_text('{"bogus": 1}\n', encoding="utf-8")
    (data_root / "imports" / "0-stray").mkdir(parents=True, exist_ok=True)
    with pytest.raises(ValueError, match="invalid local manifest"):
        replay_case(_case(data_root), data_root)


def test_missing_pinned_index_manifest_rejects_replay(tmp_path: Path) -> None:
    """A replay cannot substitute another index version when its pinned file is absent."""
    rig = _rig(tmp_path, with_index=False)
    data_root = rig["data_root"]
    assert isinstance(data_root, Path)
    dart_hash = rig["dart_hash"]
    assert isinstance(dart_hash, str)
    result = replay_case(_case(data_root, hashes=(dart_hash,)), data_root)
    assert result.status == "SOURCE_CHANGED"
    assert result.fact_matches == 0


def test_leak_canary_flags_future_knowledge(tmp_path: Path) -> None:
    """A filing known only after as-of is future data by definition."""
    rig = _rig(tmp_path)
    data_root = rig["data_root"]
    assert isinstance(data_root, Path)
    catalog = Catalog(data_root / "catalog.sqlite")
    store = EventStore(catalog)
    lake, financial, index_store = _rig_stack(data_root)
    context = build_research_context(catalog, store, lake, financial, index_store, RCEPT, AS_OF, POLICY)
    assert detect_future_leak(context, _case(data_root)) is False
    future_filing = dataclasses.replace(
        context.filing, knowledge_available_at=datetime(2024, 7, 1, 18, 0, tzinfo=KST)
    )
    future_context = dataclasses.replace(context, filing=future_filing)
    assert detect_future_leak(future_context, _case(data_root)) is True


def _rig_stack(data_root: Path) -> tuple[object, object, object]:
    from src.data.financial_evidence import FinancialEvidence
    from src.data.index_store import IndexStore, load_index_manifest
    from src.data.local_lake import LocalLake

    manifests: dict[str, ImportManifest] = {}
    for child in sorted((data_root / "imports").iterdir()):
        document = json.loads((child / "manifest.json").read_bytes().decode("utf-8"))
        manifests[str(document["dataset_id"])] = ImportManifest(
            dataset_id=str(document["dataset_id"]),
            source_manifest_sha256=str(document["source_manifest_sha256"]),
            imported_at=datetime.fromisoformat(str(document["imported_at"])),
            parts=tuple(
                ImportPart(
                    relative_path=PurePosixPath(str(item["path"])),
                    sha256=str(item["sha256"]),
                    byte_length=int(item["bytes"]),
                )
                for item in document["parts"]
            ),
        )
    lake = LocalLake(data_root, manifests)
    names = sorted((data_root / "krx" / "manifests").glob("*.json"))
    manifest = load_index_manifest(data_root, names[0].name[:-len(".json")])
    return (
        lake,
        FinancialEvidence(data_root, lake),
        IndexStore(Catalog(data_root / "catalog.sqlite"), data_root, manifest),
    )


def test_reviewed_labels_load_from_versioned_file(tmp_path: Path) -> None:
    """A reviewed labels file parses into pinned replay cases."""
    entry = {
        "case_id": "case-1",
        "rcept_no": RCEPT,
        "as_of": AS_OF.isoformat(),
        "snapshot_id": "dart-snap",
        "index_manifest_hash": "a" * 64,
        "expected_source_hashes": ["a" * 64],
        "expected_facts": {"planned_amount_krw": "14000000000"},
        "expected_citations": {"planned_amount_krw": "filing-fact-acq_ostk_prc"},
        "cohort": "development",
    }
    path = tmp_path / "cases.json"
    path.write_text(json.dumps([entry]), encoding="utf-8")
    (cases,) = load_cases(path)
    assert cases.rcept_no == RCEPT
    assert cases.index_manifest_hash == "a" * 64
    assert cases.expected_citations["planned_amount_krw"] == "filing-fact-acq_ostk_prc"
    without_index = dict(entry)
    without_index.pop("index_manifest_hash")
    path.write_text(json.dumps([without_index]), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid eval case entry"):
        load_cases(path)


def test_malformed_labels_rejected(tmp_path: Path) -> None:
    """Unreadable or misshapen labels never become silent replay cases."""
    bad_json = tmp_path / "bad.json"
    bad_json.write_text("{oops", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid eval cases file"):
        load_cases(bad_json)
    not_list = tmp_path / "not-list.json"
    not_list.write_text(json.dumps({"case_id": "x"}), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid eval cases file"):
        load_cases(not_list)
    not_dict = tmp_path / "not-dict.json"
    not_dict.write_text(json.dumps(["oops"]), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid eval case entry"):
        load_cases(not_dict)
    missing_key = tmp_path / "missing-key.json"
    missing_key.write_text(json.dumps([{"case_id": "x"}]), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid eval case entry"):
        load_cases(missing_key)
    bad_index = tmp_path / "bad-index.json"
    bad_index.write_text(
        json.dumps([{"case_id": "x", "rcept_no": RCEPT, "as_of": AS_OF.isoformat(), "snapshot_id": "dart-snap", "index_manifest_hash": "bad", "expected_source_hashes": [], "expected_facts": {}, "expected_citations": {}, "cohort": "development"}]),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="invalid eval case entry"):
        load_cases(bad_index)
    naive = tmp_path / "naive.json"
    naive.write_text(
        json.dumps(
            [
                {
                    "case_id": "x",
                    "rcept_no": RCEPT,
                    "as_of": "2024-06-25T18:00:00",
                    "snapshot_id": "dart-snap",
                    "index_manifest_hash": "a" * 64,
                    "expected_source_hashes": [],
                    "expected_facts": {},
                    "expected_citations": {},
                    "cohort": "development",
                }
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="invalid eval case entry"):
        load_cases(naive)
    empty_id = tmp_path / "empty-id.json"
    empty_id.write_text(
        json.dumps(
            [
                {
                    "case_id": "",
                    "rcept_no": RCEPT,
                    "as_of": AS_OF.isoformat(),
                    "snapshot_id": "dart-snap",
                    "index_manifest_hash": "a" * 64,
                    "expected_source_hashes": [],
                    "expected_facts": {},
                    "expected_citations": {},
                    "cohort": "development",
                }
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="invalid eval case entry"):
        load_cases(empty_id)


def test_agent_run_marks_tool_outcome(tmp_path: Path) -> None:
    """An agent replay records tool validity without changing scored facts."""
    rig = _rig(tmp_path)
    data_root = rig["data_root"]
    assert isinstance(data_root, Path)
    dart_hash = rig["dart_hash"]
    assert isinstance(dart_hash, str)

    class _QuietModel:
        def generate_json(self, messages: object, schema_name: str) -> dict[str, object]:
            del messages, schema_name
            return {"tool_calls": [], "claims": []}

    policy = AgentPolicy(max_tool_calls=3, model_timeout_seconds=5.0, prompt_version="v1")
    result = replay_case(_case(data_root, hashes=(dart_hash,)), data_root, _QuietModel(), policy)  # type: ignore[arg-type]
    assert result.status == "OK"
    assert result.tool_valid is True
    assert result.fact_matches == 2


def test_stale_memo_bytes_fail_evidence_validation(tmp_path: Path) -> None:
    """A cited filing whose bytes changed after registration fails before fact scoring."""
    rig = _rig(tmp_path)
    data_root = rig["data_root"]
    assert isinstance(data_root, Path)
    target = data_root / f"raw/dart/{RCEPT}.zip"
    target.write_bytes(b"tampered")
    result = replay_case(_case(data_root, hashes=()), data_root)
    assert result.status == "SOURCE_CHANGED"
    assert result.manifest_hash == ""
