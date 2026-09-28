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
        return records

    def _verify_row(self, row: dict[str, Any]) -> VerifiedFinancialFact | None:
        source_hash = str(row.get("source_hash") or "")
        records = self._load_records(source_hash)
        if records is None:
            return None
        filing_id = str(row.get("filing_id") or "")
        corp_code = str(row.get("dart_corp_code") or "")
        fact = str(row.get("fact") or "")
        fiscal_period = str(row.get("fiscal_period") or "")
        consolidated = bool(row.get("consolidated"))
        matches = [
            record
            for record in records
            if isinstance(record, dict)
            and record.get("filing_id") == filing_id
            and record.get("corp_code") == corp_code
            and record.get("fact") == fact
            and record.get("fiscal_period") == fiscal_period
            and bool(record.get("consolidated")) == consolidated
        ]
        if len(matches) != 1:
            return None
        record = matches[0]
        unit = str(row.get("unit") or "")
        if not unit or record.get("currency") != unit or record.get("unit") != unit:
            return None
        if (
            not record.get("account_id")
            or not record.get("sj_div")
            or record.get("rcept_no") != filing_id
            or record.get("ord") is None
        ):
            return None
        try:
            amount = Decimal(str(record.get("thstrm_amount")).replace(",", "").strip())
        except (InvalidOperation, ValueError):
            return None
        hint = row.get("value")
        if hint is not None and abs(float(amount) - float(hint)) > max(1.0, 1e-6 * abs(float(hint))):
            return None
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
            evidence_key=f"financial_evidence/{normalized}#{record.get('ord')}",
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
        for row in rows:
            fact = self._verify_row(row)
            if fact is not None:
                verified.append(fact)
        verified.sort(key=lambda item: (item.filing_id, item.fiscal_period, item.fact, item.consolidated))
        return tuple(verified)
