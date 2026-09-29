"""Invariant guards for Bronze-verified financial facts."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo

import polars as pl
import pytest

from src.data.financial_evidence import FinancialEvidence
from src.data.imports import ImportManifest, ImportPart
from src.data.local_lake import FACTS_DATASET_ID, LocalLake

KST = ZoneInfo("Asia/Seoul")
PUBLISHED = datetime(2024, 5, 14, 0, 0, tzinfo=UTC)
ELIGIBLE = datetime(2024, 5, 15, 0, 0, tzinfo=UTC)


def _sha_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _record(
    filing_id: str, fact: str, amount: str, consolidated: bool = True, hint: float | None = None
) -> dict[str, object]:
    value = hint if hint is not None else float(amount)
    return {
        "account_detail": "-",
        "account_id": "ifrs-full_Assets",
        "account_nm": "자산총계",
        "company_id": "01386916",
        "consolidated": consolidated,
        "corp_code": "01386916",
        "currency": "KRW",
        "fact": fact,
        "filing_id": filing_id,
        "fiscal_period": "2024Q1",
        "frmtrm_amount": "4083815083000",
        "mapping_version": "dart-fact-map-v1",
        "ord": "7",
        "rcept_no": filing_id,
        "restatement_id": "r0",
        "sj_div": "BS",
        "sj_nm": "재무상태표",
        "thstrm_amount": amount,
        "ticker": "361610",
        "unit": "KRW",
        "value": value,
    }


def _store_evidence(data_root: Path, payload: dict[str, object]) -> str:
    raw = json.dumps(payload, sort_keys=True).encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    base = data_root / "imports" / "financial_evidence" / digest
    base.mkdir(parents=True, exist_ok=True)
    (base / "payload.json").write_bytes(raw)
    (base / "receipt.json").write_text(json.dumps({"content_hash": digest}), encoding="utf-8")
    return digest


def _index_rows(rows: list[dict[str, object]]) -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "company_id": "01386916",
                "dart_corp_code": "01386916",
                "ticker": "361610",
                "fiscal_period": "2024Q1",
                "filing_id": row["filing_id"],
                "fact": row["fact"],
                "published_at": PUBLISHED,
                "consolidated": row.get("consolidated", True),
                "restatement_id": "r0",
                "source_hash": row["source_hash"],
                "source_kind": "opendart_standard",
                "mapping_version": "dart-fact-map-v1",
                "raw_document_hash": None,
                "available_at": row["available_at"],
                "value": row["value"],
                "unit": "KRW",
            }
            for row in rows
        ]
    )


def _lake_with_facts(data_root: Path, frame: pl.DataFrame) -> LocalLake:
    target = data_root / "imports" / FACTS_DATASET_ID / "part-00000.parquet"
    target.parent.mkdir(parents=True, exist_ok=True)
    frame.write_parquet(target)
    manifest = ImportManifest(
        dataset_id=FACTS_DATASET_ID,
        source_manifest_sha256="0" * 64,
        imported_at=datetime.now(UTC),
        parts=(
            ImportPart(
                relative_path=PurePosixPath("part-00000.parquet"),
                sha256=_sha_of(target),
                byte_length=target.stat().st_size,
            ),
        ),
    )
    return LocalLake(data_root, {FACTS_DATASET_ID: manifest})


def _setup(
    tmp_path: Path, records: list[dict[str, object]], entries: list[dict[str, object]]
) -> FinancialEvidence:
    data_root = tmp_path / "data"
    payloads: dict[str, list[dict[str, object]]] = {}
    for record in records:
        payloads.setdefault(str(record["filing_id"]), []).append(record)
    index_entries = []
    for entry in entries:
        digest = _store_evidence(data_root, {"records": payloads[str(entry["filing_id"])]})
        index_entries.append({**entry, "source_hash": digest})
    lake = _lake_with_facts(data_root, _index_rows(index_entries))
    return FinancialEvidence(data_root, lake)


def test_exact_amount_from_raw_string(tmp_path: Path) -> None:
    """Returned Decimal equals the Bronze integer string exactly, not the float hint."""
    evidence = _setup(
        tmp_path,
        [_record("20240514001363", "assets", "4021576839000")],
        [{"filing_id": "20240514001363", "fact": "assets", "available_at": ELIGIBLE, "value": 4021576839000.0}],
    )
    as_of = datetime(2024, 5, 16, 0, 0, tzinfo=KST)
    facts = evidence.facts_asof("01386916", as_of, frozenset({"assets"}))
    assert len(facts) == 1
    fact = facts[0]
    assert fact.value == Decimal("4021576839000")
    assert isinstance(fact.value, Decimal)
    assert fact.unit == "KRW"
    assert fact.filing_id == "20240514001363"
    assert fact.fiscal_period == "2024Q1"
    assert fact.consolidated is True
    assert fact.available_at == ELIGIBLE
    assert fact.evidence_key.startswith("financial_evidence/")
    assert evidence.facts_asof("01386916", datetime(2024, 5, 14, 0, 0, tzinfo=UTC), frozenset({"assets"})) == ()


def test_missing_payload_quarantines_fact(tmp_path: Path) -> None:
    """A fact row without its local payload yields no verified numeric fact."""
    data_root = tmp_path / "data"
    lake = _lake_with_facts(
        data_root,
        _index_rows(
            [
                {
                    "filing_id": "20240514001363",
                    "fact": "assets",
                    "available_at": ELIGIBLE,
                    "value": 1.0,
                    "source_hash": "b" * 64,
                }
            ]
        ),
    )
    evidence = FinancialEvidence(data_root, lake)
    assert evidence.facts_asof("01386916", datetime(2024, 6, 1, tzinfo=KST), frozenset({"assets"})) == ()


def test_tampered_payload_quarantines_fact(tmp_path: Path) -> None:
    """A payload whose bytes no longer match the cited hash is excluded."""
    evidence = _setup(
        tmp_path,
        [_record("20240514001363", "assets", "4021576839000")],
        [{"filing_id": "20240514001363", "fact": "assets", "available_at": ELIGIBLE, "value": 4021576839000.0}],
    )
    payload = tmp_path / "data" / "imports" / "financial_evidence"
    for base in payload.iterdir():
        (base / "payload.json").write_bytes(b'{"records": []}')
    assert evidence.facts_asof("01386916", datetime(2024, 6, 1, tzinfo=KST), frozenset({"assets"})) == ()


def test_restatement_does_not_replace_original(tmp_path: Path) -> None:
    """Before the restatement only the original eligible version is used."""
    original = _record("20240514001363", "assets", "4021576839000")
    restated = _record("20240603000002", "assets", "4021576840000")
    evidence = _setup(
        tmp_path,
        [original, restated],
        [
            {"filing_id": "20240514001363", "fact": "assets", "available_at": ELIGIBLE, "value": 4021576839000.0},
            {
                "filing_id": "20240603000002",
                "fact": "assets",
                "available_at": datetime(2024, 6, 5, tzinfo=UTC),
                "value": 4021576840000.0,
            },
        ],
    )
    early = evidence.facts_asof("01386916", datetime(2024, 5, 20, tzinfo=UTC), frozenset({"assets"}))
    assert [fact.filing_id for fact in early] == ["20240514001363"]
    late = evidence.facts_asof("01386916", datetime(2024, 6, 6, tzinfo=UTC), frozenset({"assets"}))
    assert [fact.filing_id for fact in late] == ["20240514001363", "20240603000002"]
    assert late[0].value == Decimal("4021576839000")


def test_basis_isolation(tmp_path: Path) -> None:
    """CFS and OFS records for one period remain distinct and unblended."""
    evidence = _setup(
        tmp_path,
        [_record("20240514001363", "assets", "4021576839000", True), _record("20240514001363", "assets", "3000000000000", False)],
        [
            {"filing_id": "20240514001363", "fact": "assets", "available_at": ELIGIBLE, "value": 4021576839000.0, "consolidated": True},
            {"filing_id": "20240514001363", "fact": "assets", "available_at": ELIGIBLE, "value": 3000000000000.0, "consolidated": False},
        ],
    )
    facts = evidence.facts_asof("01386916", datetime(2024, 6, 1, tzinfo=KST), frozenset({"assets"}))
    assert len(facts) == 2
    by_basis = {fact.consolidated: fact.value for fact in facts}
    assert by_basis == {True: Decimal("4021576839000"), False: Decimal("3000000000000")}


def test_quarantine_reasons(tmp_path: Path) -> None:
    """Identical source rows verify one value; unit, provenance and amount conflicts stay excluded."""
    good = _record("20240514001363", "assets", "4021576839000")
    dup = _record("20240514001363", "assets", "4021576839000")
    wrong_unit = _record("20240514001364", "assets", "100")
    wrong_unit["currency"] = "USD"
    no_account = _record("20240514001365", "assets", "100")
    no_account["account_id"] = ""
    bad_amount = _record("20240514001366", "assets", "not-a-number", hint=100.0)
    conflict = _record("20240514001367", "assets", "100")
    evidence = _setup(
        tmp_path,
        [good, dup, wrong_unit, no_account, bad_amount, conflict],
        [
            {"filing_id": "20240514001363", "fact": "assets", "available_at": ELIGIBLE, "value": 4021576839000.0},
            {"filing_id": "20240514001364", "fact": "assets", "available_at": ELIGIBLE, "value": 100.0},
            {"filing_id": "20240514001365", "fact": "assets", "available_at": ELIGIBLE, "value": 100.0},
            {"filing_id": "20240514001366", "fact": "assets", "available_at": ELIGIBLE, "value": 100.0},
            {"filing_id": "20240514001367", "fact": "assets", "available_at": ELIGIBLE, "value": 999999.0},
        ],
    )
    facts = evidence.facts_asof("01386916", datetime(2024, 6, 1, tzinfo=KST), frozenset({"assets"}))
    assert len(facts) == 1
    assert facts[0].filing_id == "20240514001363"


def test_same_fact_different_amounts_never_selects_a_nearby_account(tmp_path: Path) -> None:
    first = _record("20240514001363", "equity", "1000000")
    second = _record("20240514001363", "equity", "1000001")
    second["account_id"] = "ifrs-full_EquityAttributableToOwnersOfParent"
    evidence = _setup(
        tmp_path, [first, second],
        [{"filing_id": "20240514001363", "fact": "equity", "available_at": ELIGIBLE, "value": 1000000.0}],
    )
    facts = evidence.facts_asof("01386916", datetime(2024, 6, 1, tzinfo=KST), frozenset({"equity"}))
    assert len(facts) == 1
    assert facts[0].value == Decimal("1000000")


def test_repeated_amount_prefers_statement_coordinate(tmp_path: Path) -> None:
    balance = _record("20240514001363", "cash", "1000")
    cash_flow = _record("20240514001363", "cash", "1000")
    cash_flow["sj_div"] = "CF"
    cash_flow["account_id"] = "dart_CashAndCashEquivalentsAtEndOfPeriodCf"
    evidence = _setup(
        tmp_path, [cash_flow, balance],
        [{"filing_id": "20240514001363", "fact": "cash", "available_at": ELIGIBLE, "value": 1000.0}],
    )
    facts = evidence.facts_asof("01386916", datetime(2024, 6, 1, tzinfo=KST), frozenset({"cash"}))
    assert len(facts) == 1
    assert "#row=1;statement=BS;" in facts[0].evidence_key


def test_net_income_prefers_profit_loss_over_comprehensive_income(tmp_path: Path) -> None:
    comprehensive = _record("20240514001363", "net_income", "1000")
    comprehensive["sj_div"] = "CIS"
    comprehensive["account_id"] = "ifrs-full_ComprehensiveIncome"
    profit = _record("20240514001363", "net_income", "1000")
    profit["sj_div"] = "CIS"
    profit["account_id"] = "ifrs-full_ProfitLoss"
    evidence = _setup(
        tmp_path, [comprehensive, profit],
        [{"filing_id": "20240514001363", "fact": "net_income", "available_at": ELIGIBLE, "value": 1000.0}],
    )
    facts = evidence.facts_asof("01386916", datetime(2024, 6, 1, tzinfo=KST), frozenset({"net_income"}))
    assert len(facts) == 1
    assert "account=ifrs-full_ProfitLoss;" in facts[0].evidence_key


def test_invalid_index_hash_and_corrupt_payload(tmp_path: Path) -> None:
    """Non-hex hashes and unparseable payloads quarantine without raising."""
    data_root = tmp_path / "data"
    lake = _lake_with_facts(
        data_root,
        _index_rows(
            [{"filing_id": "20240514001363", "fact": "assets", "available_at": ELIGIBLE, "value": 1.0, "source_hash": "not-a-hash"}]
        ),
    )
    evidence = FinancialEvidence(data_root, lake)
    assert evidence.facts_asof("01386916", datetime(2024, 6, 1, tzinfo=KST), frozenset({"assets"})) == ()
    raw = b"not json"
    digest = hashlib.sha256(raw).hexdigest()
    base = data_root / "imports" / "financial_evidence" / digest
    base.mkdir(parents=True, exist_ok=True)
    (base / "payload.json").write_bytes(raw)
    (base / "receipt.json").write_text("{}", encoding="utf-8")
    lake_broken = _lake_with_facts(
        data_root,
        _index_rows(
            [{"filing_id": "20240514001363", "fact": "assets", "available_at": ELIGIBLE, "value": 1.0, "source_hash": digest}]
        ),
    )
    broken = FinancialEvidence(data_root, lake_broken)
    assert broken.facts_asof("01386916", datetime(2024, 6, 1, tzinfo=KST), frozenset({"assets"})) == ()
    digest_shapeless = _store_evidence(data_root, {"records": {"fact": 1}})
    lake_shapeless = _lake_with_facts(
        data_root,
        _index_rows(
            [{"filing_id": "20240514001363", "fact": "assets", "available_at": ELIGIBLE, "value": 1.0, "source_hash": digest_shapeless}]
        ),
    )
    shapeless = FinancialEvidence(data_root, lake_shapeless)
    assert shapeless.facts_asof("01386916", datetime(2024, 6, 1, tzinfo=KST), frozenset({"assets"})) == ()


def test_request_guards(tmp_path: Path) -> None:
    """Empty identities, naive clocks and absent indexes fail closed."""
    data_root = tmp_path / "data"
    data_root.mkdir(parents=True, exist_ok=True)
    lake = LocalLake(data_root, {})
    evidence = FinancialEvidence(data_root, lake)
    assert evidence.facts_asof("01386916", datetime(2024, 6, 1, tzinfo=KST), frozenset()) == ()
    assert evidence.facts_asof("01386916", datetime(2024, 6, 1, tzinfo=KST), frozenset({"assets"})) == ()
    with pytest.raises(ValueError, match="non-empty"):
        evidence.facts_asof("", datetime(2024, 6, 1, tzinfo=KST), frozenset({"assets"}))
    with pytest.raises(ValueError, match="timezone"):
        evidence.facts_asof("01386916", datetime(2024, 6, 1), frozenset({"assets"}))
    with pytest.raises(ValueError, match="missing project data root"):
        FinancialEvidence(tmp_path / "absent-root", lake)


def test_empty_parts_and_corrupt_index(tmp_path: Path) -> None:
    """An empty facts registration reads nothing; a corrupt index raises."""
    data_root = tmp_path / "data"
    data_root.mkdir(parents=True, exist_ok=True)
    hollow = ImportManifest(
        dataset_id=FACTS_DATASET_ID,
        source_manifest_sha256="0" * 64,
        imported_at=datetime.now(UTC),
        parts=(),
    )
    lake = LocalLake(data_root, {FACTS_DATASET_ID: hollow})
    evidence = FinancialEvidence(data_root, lake)
    assert evidence.facts_asof("01386916", datetime(2024, 6, 1, tzinfo=KST), frozenset({"assets"})) == ()
    target = data_root / "imports" / FACTS_DATASET_ID / "part-00000.parquet"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"not parquet")
    forged = ImportManifest(
        dataset_id=FACTS_DATASET_ID,
        source_manifest_sha256="0" * 64,
        imported_at=datetime.now(UTC),
        parts=(ImportPart(PurePosixPath("part-00000.parquet"), _sha_of(target), target.stat().st_size),),
    )
    with pytest.raises(ValueError, match="invalid local part"):
        LocalLake(data_root, {FACTS_DATASET_ID: forged})
