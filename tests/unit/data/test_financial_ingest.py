"""Invariant guards for project-owned financial snapshot collection."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import polars as pl
import pytest

from src.data.catalog import Catalog
from src.data.financial_ingest import collect_financial_snapshot
from src.integrations.dart import FinancialStatementRequest

CORP = "00126380"
REQUEST = FinancialStatementRequest(corp_code=CORP, bsns_year=2024, reprt_code="11011", fs_div="CFS")
OBSERVED_2026 = datetime(2026, 3, 2, tzinfo=UTC)
AS_OF_2024 = datetime(2024, 12, 31, tzinfo=UTC)


def _item(rcept: str, account: str, amount: str, ord_code: str = "1", currency: str = "KRW") -> dict[str, str]:
    return {
        "rcept_no": rcept,
        "reprt_code": "11011",
        "bsns_year": "2024",
        "corp_code": CORP,
        "sj_div": "BS",
        "account_id": account,
        "ord": ord_code,
        "currency": currency,
        "thstrm_amount": amount,
    }


def _payload(items: list[dict[str, str]]) -> bytes:
    return json.dumps({"status": "000", "message": "정상", "list": items}).encode("utf-8")


class _Client:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def fetch_financial_statement(self, request: FinancialStatementRequest) -> bytes:
        assert request == REQUEST
        return self._payload


def _catalog(tmp_path: Path) -> tuple[Catalog, Path]:
    root = tmp_path / "data"
    return Catalog(root / "catalog.sqlite"), root


def _rows_for(root: Path, snapshot: str) -> pl.DataFrame:
    frames = list((root / "financial" / snapshot).glob("*.parquet"))
    assert len(frames) == 1
    return pl.read_parquet(frames[0])


def test_first_observation_unavailable_to_earlier_backtest(tmp_path: Path) -> None:
    """A 2026 retrieval for a 2024 filing cannot appear in a 2024 point-in-time query."""
    catalog, root = _catalog(tmp_path)
    summary = collect_financial_snapshot(
        _Client(_payload([_item("20250301000001", "ifrs-full_Assets", "1,000")])),  # type: ignore[arg-type]
        catalog,
        REQUEST,
        root,
        "snap-1",
        OBSERVED_2026,
    )
    assert summary.row_count == 1
    assert summary.artifact_path.is_file()
    assert hashlib.sha256(summary.artifact_path.read_bytes()).hexdigest() == summary.artifact_sha256
    frame = _rows_for(root, "snap-1")
    assert frame["available_at"][0] >= OBSERVED_2026
    assert frame.filter(pl.col("available_at") <= AS_OF_2024).height == 0


def test_corrected_amount_keeps_both_hashes_with_own_times(tmp_path: Path) -> None:
    """Two snapshots preserve both raw hashes and the newer amount carries the later time."""
    catalog, root = _catalog(tmp_path)
    first = collect_financial_snapshot(
        _Client(_payload([_item("20250301000001", "ifrs-full_Assets", "1000")])),  # type: ignore[arg-type]
        catalog,
        REQUEST,
        root,
        "snap-1",
        OBSERVED_2026,
    )
    later = OBSERVED_2026.replace(day=3)
    second = collect_financial_snapshot(
        _Client(_payload([_item("20250301000001", "ifrs-full_Assets", "2000")])),  # type: ignore[arg-type]
        catalog,
        REQUEST,
        root,
        "snap-2",
        later,
    )
    assert first.artifact_sha256 != second.artifact_sha256
    assert catalog.find_artifact("dart", "financial-statement", f"{CORP}:2024:11011:CFS", "snap-1") is not None
    assert catalog.find_artifact("dart", "financial-statement", f"{CORP}:2024:11011:CFS", "snap-2") is not None
    assert pl.read_parquet(next((root / "financial" / "snap-2").glob("*.parquet")))["value"][0] == 2000.0
    assert pl.read_parquet(next((root / "financial" / "snap-2").glob("*.parquet")))["available_at"][0] == later


def test_ambiguous_account_keeps_raw_without_named_fact(tmp_path: Path) -> None:
    """Rows without a verifiable mapping or unit keep raw evidence but emit no fact."""
    catalog, root = _catalog(tmp_path)
    items = [
        _item("20250301000001", "ifrs-full_Assets", "1000"),
        _item("20250301000001", "", "1000", ord_code="2"),
        _item("20250301000001", "ifrs-full_Assets", "1000", ord_code="3", currency=""),
        _item("20250301000001", "ifrs-full_Assets", "not-a-number", ord_code="4"),
        _item("20250301000001", "-표준계정코드 미사용-", "1000", ord_code="5"),
        _item("20250301000001", "ifrs-full_Assets", "", ord_code="6"),
    ]
    summary = collect_financial_snapshot(
        _Client(_payload(items)),  # type: ignore[arg-type]
        catalog,
        REQUEST,
        root,
        "snap-1",
        OBSERVED_2026,
    )
    assert summary.row_count == 1
    assert summary.artifact_path.is_file()
    frame = _rows_for(root, "snap-1")
    assert frame["account_id"].to_list() == ["ifrs-full_Assets"]
    assert frame["currency"].to_list() == ["KRW"]


def test_artifact_conflict_fails_without_mutation(tmp_path: Path) -> None:
    """Changed bytes under the same request and snapshot conflict without overwriting."""
    catalog, root = _catalog(tmp_path)
    first = collect_financial_snapshot(
        _Client(_payload([_item("20250301000001", "ifrs-full_Assets", "1000")])),  # type: ignore[arg-type]
        catalog,
        REQUEST,
        root,
        "snap-1",
        OBSERVED_2026,
    )
    before = first.artifact_path.read_bytes()
    with pytest.raises(ValueError, match="conflicting bytes"):
        collect_financial_snapshot(
            _Client(_payload([_item("20250301000001", "ifrs-full_Assets", "9999")])),  # type: ignore[arg-type]
            catalog,
            REQUEST,
            root,
            "snap-1",
            OBSERVED_2026,
        )
    assert first.artifact_path.read_bytes() == before
    stored = catalog.find_artifact("dart", "financial-statement", f"{CORP}:2024:11011:CFS", "snap-1")
    assert stored is not None
    assert stored.sha256 == first.artifact_sha256


def test_request_guards_fail_closed(tmp_path: Path) -> None:
    """Naive clocks, unsafe snapshots, and malformed payloads raise without rows."""
    catalog, root = _catalog(tmp_path)
    with pytest.raises(ValueError, match="timezone-aware"):
        collect_financial_snapshot(
            _Client(_payload([])),  # type: ignore[arg-type]
            catalog,
            REQUEST,
            root,
            "snap-1",
            datetime(2026, 3, 2),
        )
    with pytest.raises(ValueError, match="snapshot id"):
        collect_financial_snapshot(
            _Client(_payload([])),  # type: ignore[arg-type]
            catalog,
            REQUEST,
            root,
            "../escape",
            OBSERVED_2026,
        )
    with pytest.raises(ValueError, match="schema failure"):
        collect_financial_snapshot(
            _Client(b"not json"),  # type: ignore[arg-type]
            catalog,
            REQUEST,
            root,
            "snap-1",
            OBSERVED_2026,
        )


def test_collect_financial_cli_prints_hash_rows_and_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The collect-financial command prints artifact hash, row count, and observation time."""
    from src.cli.main import main

    data_root = tmp_path / "data"
    raw = _payload([_item("20250301000001", "ifrs-full_Assets", "1000")])
    monkeypatch.setenv("OPENDART_API_KEY", "test-key")

    class _CLIClient:
        def __init__(self, api_key: str, http_client: object) -> None:
            assert api_key == "test-key"

        def fetch_financial_statement(self, request: FinancialStatementRequest) -> bytes:
            assert request == REQUEST
            return raw

    monkeypatch.setattr("src.integrations.dart.DartClient", _CLIClient)
    assert (
        main(
            [
                "data",
                "collect-financial",
                "--corp-code",
                CORP,
                "--year",
                "2024",
                "--report-code",
                "11011",
                "--fs-div",
                "CFS",
                "--data-root",
                str(data_root),
            ]
        )
        == 0
    )
    document = json.loads(capsys.readouterr().out.strip())
    assert len(document["artifact_sha256"]) == 64
    assert document["row_count"] == 1
    assert datetime.fromisoformat(document["observed_at"]).tzinfo is not None


def test_collect_financial_cli_requires_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The collect-financial command fails closed without provider credentials."""
    from src.cli.main import main

    monkeypatch.delenv("OPENDART_API_KEY", raising=False)
    monkeypatch.delenv("DART_API_KEY", raising=False)
    assert (
        main(
            [
                "data",
                "collect-financial",
                "--corp-code",
                CORP,
                "--year",
                "2024",
                "--report-code",
                "11011",
                "--fs-div",
                "CFS",
                "--data-root",
                str(tmp_path / "data"),
            ]
        )
        == 2
    )
    assert "DART API key" in capsys.readouterr().err


def test_duplicates_and_mismatched_identity_suppressed(tmp_path: Path) -> None:
    """Duplicated keys, unknown statements, and cross-request rows emit no facts."""
    catalog, root = _catalog(tmp_path)
    dup = _item("20250301000001", "ifrs-full_Assets", "1000")
    unknown_sj = _item("20250301000001", "ifrs-full_Assets", "1000", ord_code="2")
    unknown_sj["sj_div"] = "XX"
    foreign = _item("20250301000001", "ifrs-full_Assets", "1000", ord_code="3")
    foreign["corp_code"] = "00999999"
    wrong_year = _item("20250301000001", "ifrs-full_Assets", "1000", ord_code="4")
    wrong_year["bsns_year"] = "2023"
    wrong_reprt = _item("20250301000001", "ifrs-full_Assets", "1000", ord_code="5")
    wrong_reprt["reprt_code"] = "11012"
    summary = collect_financial_snapshot(
        _Client(_payload([dup, dict(dup), unknown_sj, foreign, wrong_year, wrong_reprt, "junk"])),  # type: ignore[arg-type]
        catalog,
        REQUEST,
        root,
        "snap-1",
        OBSERVED_2026,
    )
    assert summary.row_count == 0
    assert summary.artifact_path.is_file()


def test_malformed_snapshot_payloads_raise(tmp_path: Path) -> None:
    """Non-object payloads, off-status views, and non-list bodies raise without rows."""
    catalog, root = _catalog(tmp_path)
    for index, raw in enumerate(
        (
            b"[1, 2]",
            json.dumps({"status": "013", "message": "no data"}).encode(),
            json.dumps({"status": "000", "message": "ok", "list": {}}).encode(),
        )
    ):
        with pytest.raises(ValueError, match="schema failure"):
            collect_financial_snapshot(
                _Client(raw),  # type: ignore[arg-type]
                catalog,
                REQUEST,
                root,
                f"snap-bad-{index}",
                OBSERVED_2026,
            )
