"""Invariant guards for KRX index range collection and checkpoints."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from src.data.catalog import Catalog
from src.data.krx_ingest import collect_index_range
from src.integrations.krx_index import KrxSourceError

SESSION = date(2024, 6, 27)
FRIDAY = date(2024, 6, 28)
SATURDAY = date(2024, 6, 29)


def _row(name: str, open_price: str, close_price: str, day: str, cls: str) -> dict[str, str]:
    return {
        "BAS_DD": day,
        "IDX_CLSS": cls,
        "IDX_NM": name,
        "OPNPRC_IDX": open_price,
        "CLSPRC_IDX": close_price,
    }


def _payload(market: str, day: date, headline: str) -> bytes:
    return json.dumps(
        {
            "OutBlock_1": [
                _row(f"{headline} (외국주포함)", "", "", day.strftime("%Y%m%d"), market),
                _row(headline, "2767.62", "2784.06", day.strftime("%Y%m%d"), market),
            ]
        }
    ).encode("utf-8")


class _FakeClient:
    def __init__(
        self,
        heads: tuple[str, str] = ("코스피", "KOSPI"),
        missing: set[date] | None = None,
        failing: set[date] | None = None,
        invalid: set[date] | None = None,
    ) -> None:
        self._heads = heads
        self._missing = missing or set()
        self._failing = failing or set()
        self._invalid = invalid or set()
        self.calls: list[date] = []

    def fetch_day(self, market: str, session: date) -> bytes:
        self.calls.append(session)
        if session in self._missing:
            raise KrxSourceError("NO_DATA", False, "krx index day unavailable")
        if session in self._failing:
            raise KrxSourceError("TRANSPORT", True, "krx index transport failure")
        if session in self._invalid:
            return b'{"OutBlock_1": []}'
        headline, cls = self._heads
        assert market == cls
        return _payload(market, session, headline)


def _catalog(tmp_path: Path) -> tuple[Catalog, Path]:
    root = tmp_path / "data"
    return Catalog(root / "catalog.sqlite"), root


def test_range_registers_bars_and_skips_missing_days(tmp_path: Path) -> None:
    """Trading days register validated bars while unavailable days count as missing."""
    catalog, root = _catalog(tmp_path)
    client = _FakeClient(missing={SATURDAY})
    summary = collect_index_range(client, catalog, "KOSPI", SESSION, SATURDAY, root, "snap-1")
    assert summary.snapshot_id == "snap-1"
    assert summary.market == "KOSPI"
    assert summary.requested_sessions == 3
    assert summary.bars_registered == 2
    assert summary.missing_sessions == 1
    assert summary.failed_sessions == 0
    assert summary.checkpoint_cursor == FRIDAY.isoformat()
    assert catalog.find_artifact("krx", "index", "KOSPI:20240627", "snap-1") is not None
    assert catalog.find_artifact("krx", "index", "KOSPI:20240629", "snap-1") is None
    assert (root / "raw/krx/snap-1/KOSPI-20240627.json").is_file()


def test_failure_halts_before_checkpoint(tmp_path: Path) -> None:
    """A transport failure stops the range without advancing past it; resume completes."""
    catalog, root = _catalog(tmp_path)
    client = _FakeClient(failing={FRIDAY})
    first = collect_index_range(client, catalog, "KOSPI", SESSION, SATURDAY, root, "snap-1")
    assert first.bars_registered == 1
    assert first.failed_sessions == 1
    assert first.checkpoint_cursor == SESSION.isoformat()
    client._failing.clear()
    second = collect_index_range(client, catalog, "KOSPI", SESSION, SATURDAY, root, "snap-1")
    assert second.bars_registered == 2
    assert second.failed_sessions == 0
    assert second.checkpoint_cursor == SATURDAY.isoformat()
    assert client.calls.count(SESSION) == 1


def test_invalid_bar_halts_without_checkpoint(tmp_path: Path) -> None:
    """A registered raw with an invalid bar never advances the checkpoint."""
    catalog, root = _catalog(tmp_path)
    client = _FakeClient(invalid={SESSION})
    summary = collect_index_range(client, catalog, "KOSPI", SESSION, SESSION, root, "snap-1")
    assert summary.bars_registered == 0
    assert summary.failed_sessions == 1
    assert summary.checkpoint_cursor == ""


def test_corrupt_cursor_restarts_range(tmp_path: Path) -> None:
    """An unreadable checkpoint cursor restarts collection from the first session."""
    catalog, root = _catalog(tmp_path)
    catalog.save_checkpoint("krx_index", f"KOSPI:{SESSION.isoformat()}:{SESSION.isoformat()}", "snap-1", "garbage")
    summary = collect_index_range(_FakeClient(), catalog, "KOSPI", SESSION, SESSION, root, "snap-1")
    assert summary.bars_registered == 1
    assert summary.checkpoint_cursor == SESSION.isoformat()


def test_collection_contract_guards(tmp_path: Path) -> None:
    """Bad markets, empty ranges and unsafe snapshot ids fail closed."""
    catalog, root = _catalog(tmp_path)
    client = _FakeClient()
    with pytest.raises(ValueError, match="market"):
        collect_index_range(client, catalog, "NYSE", SESSION, SESSION, root, "snap-1")
    with pytest.raises(ValueError, match="range"):
        collect_index_range(client, catalog, "KOSPI", SATURDAY, SESSION, root, "snap-1")
    with pytest.raises(ValueError, match="snapshot"):
        collect_index_range(client, catalog, "KOSPI", SESSION, SESSION, root, "../evil")
    kosdaq = _FakeClient(heads=("코스닥", "KOSDAQ"))
    summary = collect_index_range(kosdaq, catalog, "KOSDAQ", SESSION, SESSION, root, "snap-1")
    assert summary.bars_registered == 1
    assert summary.failed_sessions == 0
