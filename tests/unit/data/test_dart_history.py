"""Historical DART windows resume from durable pages without losing receipt evidence."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from src.data.catalog import Catalog
from src.data.dart_ingest import collect_buyback_history, collect_buyback_window
from src.integrations.dart import DartListPage, DartListRow, DartSourceError

_START = date(2024, 5, 30)
_END = date(2024, 5, 31)
_RECEIPT = "20240530000001"
_WINDOW = "2024-05-30:2024-05-31"
_FORM = "주요사항보고서(자기주식취득결정)"


class _Lake:
    def next_session(self, after: date) -> date:
        assert after == _START
        return date(2024, 6, 3)


class _Client:
    def __init__(self, fail_page: int | None = None) -> None:
        self.fail_page = fail_page
        self.pages: list[int] = []
        self.documents: list[str] = []

    def list_major_reports(self, start: date, end: date, page: int) -> DartListPage:
        assert (start, end) == (_START, _END)
        self.pages.append(page)
        if page == self.fail_page:
            raise DartSourceError("020", True, "quota exceeded")
        assert page in (1, 2), "a completed month must not request page three"
        rows = (
            (DartListRow(_RECEIPT, "00123456", "005930", "Y", _FORM, _START, ""),)
            if page == 1
            else ()
        )
        payload = {
            "status": "000",
            "page_no": page,
            "total_page": 2,
            "total_count": 1,
            "list": [
                {
                    "rcept_no": row.rcept_no,
                    "corp_code": row.corp_code,
                    "stock_code": row.stock_code,
                    "corp_cls": row.corp_cls,
                    "report_nm": row.report_name,
                    "rcept_dt": row.rcept_date.strftime("%Y%m%d"),
                    "rm": row.rm,
                }
                for row in rows
            ],
        }
        return DartListPage(page, 2, 1, json.dumps(payload).encode(), rows)

    def document_zip(self, rcept_no: str) -> bytes:
        self.documents.append(rcept_no)
        assert rcept_no == _RECEIPT
        return b"PK\x03\x04immutable document"


def test_historical_retry_rechecks_last_page_and_keeps_all_receipts(tmp_path: Path) -> None:
    root = tmp_path / "data"
    catalog = Catalog(root / "catalog.sqlite")
    client = _Client(fail_page=2)
    lake = _Lake()

    with pytest.raises(ValueError, match="incomplete source evidence"):
        collect_buyback_history(client, catalog, lake, _START, _END, root, "snapshot")  # type: ignore[arg-type]
    assert catalog.load_checkpoint("dart", _WINDOW, "snapshot") == "page-1"

    client.fail_page = None
    summary = collect_buyback_history(client, catalog, lake, _START, _END, root, "snapshot")  # type: ignore[arg-type]
    assert summary.windows_complete == 1
    assert catalog.load_checkpoint("dart-backfill-complete", _WINDOW, "snapshot") == f"complete:2:{_RECEIPT}"
    assert client.pages == [1, 2, 1, 2]
    assert catalog.find_artifact("dart", "document", _RECEIPT, "snapshot") is not None

    calls = list(client.pages)
    collect_buyback_history(client, catalog, lake, _START, _END, root, "snapshot")  # type: ignore[arg-type]
    assert client.pages == calls


def test_terminal_page_checkpoint_without_completion_is_replayed(tmp_path: Path) -> None:
    root = tmp_path / "data"
    catalog = Catalog(root / "catalog.sqlite")
    client = _Client()
    lake = _Lake()
    partial = collect_buyback_window(
        client, catalog, lake, _START, _END, root, "snapshot", "HISTORICAL_BACKFILL"  # type: ignore[arg-type]
    )
    assert partial.checkpoint_cursor == "page-2"
    assert catalog.load_checkpoint("dart-backfill-complete", _WINDOW, "snapshot") is None

    summary = collect_buyback_history(client, catalog, lake, _START, _END, root, "snapshot")  # type: ignore[arg-type]
    assert summary.windows_complete == 1
    assert client.pages == [1, 2, 2]
    assert catalog.load_checkpoint("dart-backfill-complete", _WINDOW, "snapshot") == f"complete:2:{_RECEIPT}"


def test_corrupt_completed_document_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "data"
    catalog = Catalog(root / "catalog.sqlite")
    client = _Client()
    lake = _Lake()
    collect_buyback_history(client, catalog, lake, _START, _END, root, "snapshot")  # type: ignore[arg-type]
    artifact = catalog.find_artifact("dart", "document", _RECEIPT, "snapshot")
    assert artifact is not None
    (root / artifact.local_relative_path).unlink()

    with pytest.raises(ValueError, match="invalid completed DART window"):
        collect_buyback_history(client, catalog, lake, _START, _END, root, "snapshot")  # type: ignore[arg-type]
    assert client.pages == [1, 2]


def test_empty_completion_marker_cannot_skip_source_query(tmp_path: Path) -> None:
    root = tmp_path / "data"
    catalog = Catalog(root / "catalog.sqlite")
    catalog.save_checkpoint("dart-backfill-complete", _WINDOW, "snapshot", "complete:0:")
    client = _Client()
    with pytest.raises(ValueError, match="invalid completed DART window"):
        collect_buyback_history(client, catalog, _Lake(), _START, _END, root, "snapshot")  # type: ignore[arg-type]
    assert client.pages == []
