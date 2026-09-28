"""Project-owned collection of official OpenDART financial statements."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Any

import polars as pl

from src.data.catalog import Catalog
from src.data.local_paths import checked_local_path
from src.integrations.dart import DartClient, FinancialStatementRequest

_SOURCE = "dart"
_ENDPOINT = "financial-statement"
_FINANCIAL_DIR = "financial"
_VALID_SJ_DIVS = frozenset({"BS", "IS", "CIS", "CF", "SCE"})
_NON_ACCOUNT = "-표준계정코드 미사용-"

_FACT_COLUMNS = (
    "corp_code",
    "bsns_year",
    "reprt_code",
    "fs_div",
    "rcept_no",
    "sj_div",
    "account_id",
    "ord",
    "currency",
    "thstrm_amount",
    "value",
    "available_at",
    "source_hash",
    "snapshot_id",
)


@dataclass(frozen=True, slots=True)
class FinancialCollectionSummary:
    """Describe an immutable project-owned financial snapshot and rows."""

    artifact_sha256: str
    artifact_path: Path
    row_count: int
    observed_at: datetime


def _require_aware(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")


def _check_snapshot(snapshot_id: str) -> None:
    if not snapshot_id or "/" in snapshot_id or snapshot_id in {".", ".."} or ".." in snapshot_id:
        raise ValueError("snapshot id must be non-empty")


def _request_key(request: FinancialStatementRequest) -> str:
    return f"{request.corp_code.strip()}:{request.bsns_year:04d}:{request.reprt_code.strip()}:{str(request.fs_div).strip()}"


def _parse_amount(value: Any) -> Decimal | None:
    text = str(value).replace(",", "").strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def _row_key(item: dict[str, Any]) -> tuple[str, str, str, str] | None:
    rcept_no = str(item.get("rcept_no", "")).strip()
    sj_div = str(item.get("sj_div", "")).strip()
    account_id = str(item.get("account_id", "")).strip()
    ord_code = str(item.get("ord", "")).strip()
    if not rcept_no or not sj_div or not account_id or not ord_code:
        return None
    return (rcept_no, sj_div, account_id, ord_code)


def _normalize_rows(
    payload: bytes,
    request: FinancialStatementRequest,
    observed_at: datetime,
    source_hash: str,
    snapshot_id: str,
) -> list[dict[str, Any]]:
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("dart financial schema failure") from exc
    if not isinstance(document, dict):
        raise ValueError("dart financial schema failure")
    if str(document.get("status", "")).strip() != "000":
        raise ValueError("dart financial schema failure")
    items = document.get("list")
    if not isinstance(items, list):
        raise ValueError("dart financial schema failure")
    counts: dict[tuple[str, str, str, str], int] = {}
    for item in items:
        if isinstance(item, dict):
            key = _row_key(item)
            if key is not None:
                counts[key] = counts.get(key, 0) + 1
    rows: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        key = _row_key(item)
        if key is None or counts.get(key, 0) != 1:
            continue
        rcept_no, sj_div, account_id, ord_code = key
        if sj_div not in _VALID_SJ_DIVS:
            continue
        if account_id == _NON_ACCOUNT:
            continue
        currency = str(item.get("currency", "")).strip()
        if not currency:
            continue
        amount = _parse_amount(item.get("thstrm_amount"))
        if amount is None:
            continue
        item_corp = str(item.get("corp_code", "") or "").strip()
        if item_corp and item_corp != request.corp_code.strip():
            continue
        item_year = str(item.get("bsns_year", "") or "").strip()
        if item_year and item_year != f"{request.bsns_year:04d}":
            continue
        item_reprt = str(item.get("reprt_code", "") or "").strip()
        if item_reprt and item_reprt != request.reprt_code.strip():
            continue
        rows.append(
            {
                "corp_code": request.corp_code.strip(),
                "bsns_year": request.bsns_year,
                "reprt_code": request.reprt_code.strip(),
                "fs_div": str(request.fs_div).strip(),
                "rcept_no": rcept_no,
                "sj_div": sj_div,
                "account_id": account_id,
                "ord": ord_code,
                "currency": currency,
                "thstrm_amount": str(item.get("thstrm_amount")).strip(),
                "value": float(amount),
                "available_at": observed_at,
                "source_hash": source_hash.lower(),
                "snapshot_id": snapshot_id,
            }
        )
    rows.sort(key=lambda row: (row["rcept_no"], row["sj_div"], row["account_id"], row["ord"]))
    return rows


def _write_rows(data_root: Path, snapshot_id: str, request_key: str, rows: list[dict[str, Any]]) -> None:
    relative = PurePosixPath(f"{_FINANCIAL_DIR}/{snapshot_id}/{request_key.replace(':', '-')}.parquet")
    target = checked_local_path(data_root, relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    if rows:
        frame = pl.DataFrame(
            {
                "corp_code": [row["corp_code"] for row in rows],
                "bsns_year": [row["bsns_year"] for row in rows],
                "reprt_code": [row["reprt_code"] for row in rows],
                "fs_div": [row["fs_div"] for row in rows],
                "rcept_no": [row["rcept_no"] for row in rows],
                "sj_div": [row["sj_div"] for row in rows],
                "account_id": [row["account_id"] for row in rows],
                "ord": [row["ord"] for row in rows],
                "currency": [row["currency"] for row in rows],
                "thstrm_amount": [row["thstrm_amount"] for row in rows],
                "value": [row["value"] for row in rows],
                "available_at": [row["available_at"] for row in rows],
                "source_hash": [row["source_hash"] for row in rows],
                "snapshot_id": [row["snapshot_id"] for row in rows],
            }
        )
    else:
        frame = pl.DataFrame(
            {
                "corp_code": pl.Series([], dtype=pl.String),
                "bsns_year": pl.Series([], dtype=pl.Int64),
                "reprt_code": pl.Series([], dtype=pl.String),
                "fs_div": pl.Series([], dtype=pl.String),
                "rcept_no": pl.Series([], dtype=pl.String),
                "sj_div": pl.Series([], dtype=pl.String),
                "account_id": pl.Series([], dtype=pl.String),
                "ord": pl.Series([], dtype=pl.String),
                "currency": pl.Series([], dtype=pl.String),
                "thstrm_amount": pl.Series([], dtype=pl.String),
                "value": pl.Series([], dtype=pl.Float64),
                "available_at": pl.Series([], dtype=pl.Datetime(time_zone="UTC")),
                "source_hash": pl.Series([], dtype=pl.String),
                "snapshot_id": pl.Series([], dtype=pl.String),
            }
        )
    partial = target.with_name(target.name + ".partial")
    frame.write_parquet(partial)
    os.replace(partial, target)


def collect_financial_snapshot(
    client: DartClient,
    catalog: Catalog,
    request: FinancialStatementRequest,
    data_root: Path,
    snapshot_id: str,
    observed_at: datetime,
) -> FinancialCollectionSummary:
    """Register raw OpenDART evidence and normalize verifiable fact rows.

    Store the exact response under this project, record its SHA-256 and
    retrieval time, and retain receipt and account identity for each row.
    Fail closed on ambiguous unit, currency, account, or receipt mapping.
    A current-view row is available no earlier than its first verified
    observation unless immutable receipt evidence establishes earlier time.
    """
    _require_aware(observed_at, "observed_at")
    _check_snapshot(snapshot_id)
    data_root.mkdir(parents=True, exist_ok=True)
    key = _request_key(request)
    payload = client.fetch_financial_statement(request)
    raw_relative = PurePosixPath(f"raw/dart/financial/{snapshot_id}/{key.replace(':', '-')}.json")
    with catalog.transaction():
        registered = catalog.register_artifact(
            source=_SOURCE,
            endpoint=_ENDPOINT,
            request_key=key,
            snapshot_id=snapshot_id,
            raw_bytes=payload,
            retrieved_at=observed_at,
            local_relative_path=raw_relative,
        )
        rows = _normalize_rows(payload, request, observed_at, registered, snapshot_id)
        _write_rows(data_root, snapshot_id, key, rows)
    artifact_path = checked_local_path(data_root, raw_relative)
    return FinancialCollectionSummary(
        artifact_sha256=registered,
        artifact_path=artifact_path,
        row_count=len(rows),
        observed_at=observed_at,
    )


__all__ = ["FinancialCollectionSummary", "collect_financial_snapshot"]
