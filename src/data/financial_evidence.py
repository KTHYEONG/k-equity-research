"""Primary-source financial facts verified against local Bronze evidence."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl

from src.data.local_lake import FACTS_DATASET_ID, LocalLake
from src.data.local_paths import checked_local_path

_EVIDENCE_DIRNAME = "financial_evidence"
_HEXDIGITS = frozenset("0123456789abcdefABCDEF")
_CHUNK_SIZE = 1024 * 1024
_PREFERRED_STATEMENTS: dict[str, tuple[str, ...]] = {
    "assets": ("BS",),
    "debt": ("BS",),
    "equity": ("BS",),
    "cash": ("BS", "CF"),
    "sales": ("IS", "CIS"),
    "gross_profit": ("IS", "CIS"),
    "operating_profit": ("IS", "CIS"),
    "net_income": ("IS", "CIS"),
    "operating_cash_flow": ("CF",),
    "capex": ("CF",),
}
_PREFERRED_ACCOUNTS: dict[str, str] = {
    "assets": "Assets",
    "debt": "Liabilities",
    "equity": "Equity",
    "cash": "CashAndCashEquivalents",
    "sales": "Revenue",
    "net_income": "ProfitLoss",
}

_FACT_COLUMNS = (
    "dart_corp_code",
    "filing_id",
    "fact",
    "fiscal_period",
    "consolidated",
    "available_at",
    "value",
    "unit",
    "source_hash",
)


def _is_hex64(value: str) -> bool:
    return len(value) == 64 and all(char in _HEXDIGITS for char in value)


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(_CHUNK_SIZE), b""):
            digest.update(block)
    return digest.hexdigest()


def _require_aware(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")


def _asof_literal(files: list[str], as_of: datetime) -> datetime:
    dtype = pl.scan_parquet(files[0]).collect_schema()["available_at"]
    zone = dtype.time_zone if isinstance(dtype, pl.Datetime) else None
    return as_of.astimezone(ZoneInfo(zone or "UTC"))


@dataclass(frozen=True, slots=True)
class VerifiedFinancialFact:
    """One Bronze-verified financial amount for a single filing and basis."""

    corp_code: str
    filing_id: str
    fact: str
    fiscal_period: str
    consolidated: bool
    value: Decimal
    unit: str
    available_at: datetime
    source_hash: str
    evidence_key: str


class FinancialEvidence:
    """Verified reads over the imported financial index and local Bronze payloads."""

    def __init__(self, data_root: Path, lake: LocalLake) -> None:
        if data_root.is_symlink() or not data_root.is_dir():
            raise ValueError(f"missing project data root: {data_root}")
        self._data_root = data_root
        self._lake = lake

    def _load_records(self, source_hash: str) -> list[Any] | None:
        if not _is_hex64(source_hash):
            return None
        normalized = source_hash.lower()
        try:
            base = PurePosixPath("imports") / _EVIDENCE_DIRNAME / normalized
            payload = checked_local_path(self._data_root, base / "payload.json")
            receipt = checked_local_path(self._data_root, base / "receipt.json")
        except ValueError:
            return None
        if payload.is_symlink() or not payload.is_file() or receipt.is_symlink() or not receipt.is_file():
            return None
        if _sha256_of(payload) != normalized:
            return None
        try:
            records = json.loads(payload.read_bytes().decode("utf-8"))["records"]
            receipt_doc = json.loads(receipt.read_bytes().decode("utf-8"))
        except (ValueError, KeyError, TypeError):
            return None
        if not isinstance(records, list) or not isinstance(receipt_doc, dict):
            return None
        if receipt_doc.get("content_hash") != normalized:
            return None
        return records

    def _verify_row(self, row: dict[str, Any], records: list[Any] | None = None) -> VerifiedFinancialFact | None:
        source_hash = str(row.get("source_hash") or "")
        if records is None:
            records = self._load_records(source_hash)
        if records is None:
            return None
        filing_id = str(row.get("filing_id") or "")
        corp_code = str(row.get("dart_corp_code") or "")
        fact = str(row.get("fact") or "")
        fiscal_period = str(row.get("fiscal_period") or "")
        consolidated = bool(row.get("consolidated"))
        matches = [
            (index, record)
            for index, record in enumerate(records)
            if isinstance(record, dict)
            and record.get("filing_id") == filing_id
            and record.get("corp_code") == corp_code
            and record.get("fact") == fact
            and record.get("fiscal_period") == fiscal_period
            and bool(record.get("consolidated")) == consolidated
        ]
        unit = str(row.get("unit") or "")
        if not unit:
            return None
        hint = row.get("value")
        candidates: list[tuple[int, dict[str, Any], Decimal]] = []
        for index, record in matches:
            if record.get("currency") != unit or record.get("unit") != unit:
                continue
            if (
                not record.get("account_id")
                or not record.get("sj_div")
                or record.get("rcept_no") != filing_id
                or record.get("ord") is None
            ):
                continue
            try:
                amount = Decimal(str(record.get("thstrm_amount")).replace(",", "").strip())
            except (InvalidOperation, ValueError):
                continue
            if not amount.is_finite() or (hint is not None and float(amount) != float(hint)):
                continue
            candidates.append((index, record, amount))
        if not candidates or len({amount for _, _, amount in candidates}) != 1:
            return None
        preferred = _PREFERRED_STATEMENTS.get(fact, ())

        def rank(item: tuple[int, dict[str, Any], Decimal]) -> tuple[int, int, int, str, str, int]:
            index, record, _ = item
            statement = str(record["sj_div"])
            account = str(record["account_id"])
            account_name = account.rsplit("_", 1)[-1]
            return (
                preferred.index(statement) if statement in preferred else len(preferred),
                0 if str(record.get("account_detail") or "-") == "-" else 1,
                0 if account_name == _PREFERRED_ACCOUNTS.get(fact) else 1,
                account,
                str(record["ord"]),
                index,
            )
        record_index, record, amount = min(candidates, key=rank)
        normalized = source_hash.lower()
        return VerifiedFinancialFact(
            corp_code=corp_code,
            filing_id=filing_id,
            fact=fact,
            fiscal_period=fiscal_period,
            consolidated=consolidated,
            value=amount,
            unit=unit,
            available_at=row["available_at"],
            source_hash=normalized,
            evidence_key=(
                f"financial_evidence/{normalized}#row={record_index};statement={record['sj_div']};"
                f"account={record['account_id']};ord={record['ord']}"
            ),
        )

    def facts_asof(
        self, corp_code: str, as_of: datetime, fact_names: frozenset[str]
    ) -> tuple[VerifiedFinancialFact, ...]:
        """Return only filing versions eligible at as_of with exact Decimal values verified against local Bronze amount strings. Preserve filing, fiscal period, consolidation basis and source hash; quarantine missing or ambiguous raw evidence."""
        if not corp_code:
            raise ValueError("corp code must be non-empty")
        _require_aware(as_of, "as_of")
        if not fact_names:
            return ()
        try:
            parts = self._lake.dataset_parts(FACTS_DATASET_ID)
        except ValueError:
            return ()
        if not parts:
            return ()
        files = [str(path) for path in parts]
        cutoff = _asof_literal(files, as_of)
        rows = (
            pl.scan_parquet(files)
            .filter(
                (pl.col("dart_corp_code") == corp_code)
                & (pl.col("fact").is_in(list(fact_names)))
                & (pl.col("available_at") <= cutoff)
            )
            .select(list(_FACT_COLUMNS))
            .collect()
            .to_dicts()
        )
        verified: list[VerifiedFinancialFact] = []
        record_cache: dict[str, list[Any] | None] = {}
        for row in rows:
            digest = str(row.get("source_hash") or "")
            if digest not in record_cache:
                record_cache[digest] = self._load_records(digest)
            records = record_cache[digest]
            fact = self._verify_row(row, records) if records is not None else None
            if fact is not None:
                verified.append(fact)
        verified.sort(key=lambda item: (item.filing_id, item.fiscal_period, item.fact, item.consolidated))
        return tuple(verified)
