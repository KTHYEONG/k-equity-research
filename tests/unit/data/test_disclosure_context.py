"""Provider gaps cannot masquerade as complete disclosure-context coverage."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath
from unittest.mock import Mock

import pytest

from src.data.catalog import Catalog, FilingVersion
from src.data.disclosure_context import collect_event_disclosure_context, get_event_coverage
from src.data.event_store import EventStore
from src.data.local_lake import LocalLake
from src.integrations.dart import DartListPage, DartListRow, DartSourceError
from src.research.event_study import StudyPolicy

_CORP = "01386916"
_RECEIPT = "20240626000207"
_DAY = date(2024, 6, 26)
_AS_OF = datetime(2024, 6, 29, tzinfo=UTC)
_SNAPSHOT = "context-test"
_WINDOW = f"disclosure:{_CORP}:2024-06-27:2024-06-28"


class _ListClient:
    def __init__(self, fail_page: int | None) -> None:
        self.fail_page = fail_page
        self.calls: list[int] = []

    def list_reports(self, start: date, end: date, page: int, corp_code: str | None = None) -> DartListPage:
        assert (start, end) == (date(2024, 6, 27), date(2024, 6, 28))
        assert corp_code == _CORP
        self.calls.append(page)
        if page == self.fail_page:
            raise DartSourceError("020", True, "quota reached")
        return DartListPage(
            page_no=page,
            page_count=2,
            total_count=101,
            raw_bytes=json.dumps({"page": page}).encode(),
            rows=(),
        )


class _DocumentClient:
    def __init__(self) -> None:
        self.fail_document = True
        self.pages: list[int] = []

    def list_reports(self, start: date, end: date, page: int, corp_code: str | None = None) -> DartListPage:
        assert (start, end, page) == (date(2024, 6, 27), date(2024, 6, 28), 1)
        assert corp_code == _CORP
        self.pages.append(page)
        row = DartListRow(
            rcept_no="20240627000001",
            corp_code=_CORP,
            stock_code="361610",
            corp_cls="Y",
            report_name="기타 공시",
            rcept_date=date(2024, 6, 27),
            rm="",
        )
        return DartListPage(1, 1, 1, b'{"page":1}', (row,))

    def document_zip(self, rcept_no: str) -> bytes:
        assert rcept_no == "20240627000001"
        if self.fail_document:
            raise DartSourceError("transport", True, "document unavailable")
        return b"original context document"


def _rig(tmp_path: Path) -> tuple[Path, Catalog, EventStore, LocalLake]:
    root = tmp_path / "data"
    catalog = Catalog(root / "catalog.sqlite")
    raw = b"original buyback document"
    digest = hashlib.sha256(raw).hexdigest()
    catalog.register_artifact(
        "dart", "document", _RECEIPT, "original", raw, datetime(2024, 6, 27, tzinfo=UTC),
        PurePosixPath(f"raw/dart/original/doc-{_RECEIPT}.zip"),
    )
    catalog.upsert_filing(
        FilingVersion(
            rcept_no=_RECEIPT,
            corp_code=_CORP,
            receipt_date=_DAY,
            report_name="주요사항보고서(자기주식취득결정)",
            stock_code="361610",
            raw_hash=digest,
            first_observed_at=datetime(2024, 6, 27, tzinfo=UTC),
            knowledge_available_at=datetime(2024, 6, 27, tzinfo=UTC),
            availability_mode="HISTORICAL_BACKFILL",
            correction_flag=False,
            withdrawal_flag=False,
            parent_rcept_no=None,
            link_status="ORIGINAL",
            time_precision="DATE_ONLY",
        )
    )
    store = EventStore(catalog)
    store._conn.execute(  # noqa: SLF001
        "INSERT INTO event_link (event_id, rcept_nos, status) VALUES (?, ?, ?)",
        ("event-1", json.dumps([_RECEIPT]), "LINKED"),
    )
    store._conn.commit()  # noqa: SLF001
    lake = Mock(spec=LocalLake)
    lake.next_session.side_effect = {
        _DAY: date(2024, 6, 27),
        date(2024, 6, 27): date(2024, 6, 28),
    }.get
    return root, catalog, store, lake


@pytest.mark.parametrize("fail_page", [1, 2])
def test_list_failure_is_incomplete_and_resume_reaches_terminal_page(tmp_path: Path, fail_page: int) -> None:
    root, catalog, store, lake = _rig(tmp_path)
    client = _ListClient(fail_page)
    policy = StudyPolicy(estimation_start=0, horizons=(1,))

    with pytest.raises(ValueError, match=f"incomplete DART list.*page {fail_page}: 020"):
        collect_event_disclosure_context(client, catalog, store, lake, policy, root, _AS_OF, _SNAPSHOT)
    assert catalog.load_checkpoint("disclosure-context", _WINDOW, _SNAPSHOT) == (
        "page-1" if fail_page == 2 else None
    )

    client.fail_page = None
    summary = collect_event_disclosure_context(client, catalog, store, lake, policy, root, _AS_OF, _SNAPSHOT)
    assert summary.issuer_windows == 1
    assert summary.pages_verified == 2
    assert summary.missing_receipts == ()
    assert catalog.load_checkpoint("disclosure-context", _WINDOW, _SNAPSHOT) == "complete:2"
    assert client.calls[-2:] == [1, 2]


def test_missing_document_blocks_completion_until_retry(tmp_path: Path) -> None:
    root, catalog, store, lake = _rig(tmp_path)
    client = _DocumentClient()
    policy = StudyPolicy(estimation_start=0, horizons=(1,))

    with pytest.raises(ValueError, match="incomplete DART documents"):
        collect_event_disclosure_context(client, catalog, store, lake, policy, root, _AS_OF, _SNAPSHOT)
    assert catalog.load_checkpoint("disclosure-context", _WINDOW, _SNAPSHOT) == "page-1"
    assert catalog.find_artifact("dart", "document", "20240627000001", _SNAPSHOT) is None

    client.fail_document = False
    summary = collect_event_disclosure_context(client, catalog, store, lake, policy, root, _AS_OF, _SNAPSHOT)
    assert summary.documents_verified == 1
    assert summary.missing_receipts == ()
    assert catalog.load_checkpoint("disclosure-context", _WINDOW, _SNAPSHOT) == "complete:1"
    assert get_event_coverage(catalog, "event-1", _SNAPSHOT)[0].document_ok
    assert client.pages == [1, 1]


def test_completed_window_fails_when_document_disappears(tmp_path: Path) -> None:
    root, catalog, store, lake = _rig(tmp_path)
    client = _DocumentClient()
    client.fail_document = False
    policy = StudyPolicy(estimation_start=0, horizons=(1,))
    collect_event_disclosure_context(client, catalog, store, lake, policy, root, _AS_OF, _SNAPSHOT)
    artifact = catalog.find_artifact("dart", "document", "20240627000001", _SNAPSHOT)
    assert artifact is not None
    (root / artifact.local_relative_path).unlink()

    client.fail_document = True
    with pytest.raises(ValueError, match="incomplete DART documents"):
        collect_event_disclosure_context(client, catalog, store, lake, policy, root, _AS_OF, _SNAPSHOT)
    assert client.pages == [1]
