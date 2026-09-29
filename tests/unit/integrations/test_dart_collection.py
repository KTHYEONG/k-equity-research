"""Invariant guards for DART collection and immutable storage."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from src.core.buyback_document import DocumentLimits
from src.data.catalog import Catalog
from src.data.dart_ingest import collect_buyback_window
from src.data.event_store import EventStore
from src.integrations.dart import DartClient, DartListPage, DartListRow, DartSourceError

FORM = "주요사항보고서(자기주식취득결정)"
API_KEY = "SECRET-KEY-123"


def _row(rcept_no: str, report: str = FORM, cls: str = "Y", stock: str = "005930", rm: str = "") -> DartListRow:
    return DartListRow(
        rcept_no=rcept_no,
        corp_code="00123456",
        stock_code=stock,
        corp_cls=cls,
        report_name=report,
        rcept_date=date(2024, 5, 31),
        rm=rm,
    )


class _Lake:
    def __init__(self, missing: bool = False) -> None:
        self._missing = missing

    def next_session(self, after: date) -> date | None:
        del after
        return None if self._missing else date(2024, 6, 3)

    def previous_session(self, before: date) -> date | None:
        del before
        return None if self._missing else date(2024, 5, 30)

    def resolve_security(self, *args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("not used")


class _FakeClient:
    def __init__(
        self,
        pages: dict[int, DartListPage],
        zips: dict[str, bytes],
        fail_lists: set[int] | None = None,
        fail_zips: set[str] | None = None,
    ) -> None:
        self._pages = pages
        self._zips = zips
        self.fail_lists = fail_lists or set()
        self.fail_zips = fail_zips or set()

    def list_major_reports(self, start: date, end: date, page: int) -> DartListPage:
        del start, end
        if page in self.fail_lists:
            raise DartSourceError("020", True, "quota exceeded")
        return self._pages[page]

    def document_zip(self, rcept_no: str) -> bytes:
        if rcept_no in self.fail_zips:
            raise DartSourceError("TRANSPORT", True, "transport failure")
        return self._zips[rcept_no]

    def current_buyback_details(self, corp_code: str, start: date, end: date) -> bytes:
        del corp_code, start, end
        return b'{"only": "correction"}'


def _page(page_no: int, rows: list[DartListRow], count: int = 2) -> DartListPage:
    raw = json.dumps({"page": page_no}).encode()
    return DartListPage(page_no=page_no, page_count=count, total_count=len(rows), raw_bytes=raw, rows=tuple(rows))


def _catalog(tmp_path: Path) -> tuple[Catalog, Path]:
    root = tmp_path / "data"
    return Catalog(root / "catalog.sqlite"), root


def _zip_bytes(tag: bytes) -> bytes:
    return b"PK\x03\x04" + tag


def test_all_category_list_uses_issuer_filter_without_disclosure_type() -> None:
    """An issuer query must retain regular and buyback reports in one response."""
    requests: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "status": "000",
                "message": "정상",
                "page_no": 1,
                "total_page": 1,
                "total_count": 2,
                "list": [
                    {
                        "rcept_no": "20240627000001",
                        "corp_code": "00123456",
                        "stock_code": "005930",
                        "corp_cls": "Y",
                        "report_nm": "사업보고서",
                        "rcept_dt": "20240627",
                        "rm": "",
                    },
                    {
                        "rcept_no": "20240627000002",
                        "corp_code": "00123456",
                        "stock_code": "005930",
                        "corp_cls": "Y",
                        "report_nm": FORM,
                        "rcept_dt": "20240627",
                        "rm": "",
                    },
                ],
            },
        )

    client = DartClient(API_KEY, httpx.Client(transport=httpx.MockTransport(_handler)))
    page = client.list_reports(date(2024, 6, 27), date(2024, 6, 28), 1, corp_code="00123456")
    assert {row.report_name for row in page.rows} == {"사업보고서", FORM}
    assert requests[0].url.path == "/api/list.json"
    assert requests[0].url.params["corp_code"] == "00123456"
    assert requests[0].url.params["last_reprt_at"] == "N"
    assert "pblntf_ty" not in requests[0].url.params

    buyback_page = client.list_major_reports(date(2024, 6, 27), date(2024, 6, 28), 1)
    assert len(buyback_page.rows) == 2
    assert "corp_code" not in requests[1].url.params
    assert "pblntf_ty" not in requests[1].url.params

    filtered_page = client.list_reports(date(2024, 6, 27), date(2024, 6, 28), 1, pblntf_ty="E")
    assert len(filtered_page.rows) == 2
    assert requests[2].url.params["pblntf_ty"] == "E"


def test_issuer_list_rejects_provider_identity_mismatch() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "status": "000",
                "page_no": 1,
                "total_page": 1,
                "total_count": 1,
                "list": [
                    {
                        "rcept_no": "20240627000001",
                        "corp_code": "99999999",
                        "stock_code": "005930",
                        "corp_cls": "Y",
                        "report_nm": "사업보고서",
                        "rcept_dt": "20240627",
                        "rm": "",
                    }
                ],
            },
        )

    client = DartClient(API_KEY, httpx.Client(transport=httpx.MockTransport(_handler)))
    with pytest.raises(DartSourceError) as error:
        client.list_reports(date(2024, 6, 27), date(2024, 6, 28), 1, corp_code="00123456")
    assert error.value.status == "SCHEMA"
    with pytest.raises(ValueError, match="8-digit"):
        client.list_reports(date(2024, 6, 27), date(2024, 6, 28), 1, corp_code="short")
    with pytest.raises(ValueError, match="disclosure type"):
        client.list_reports(date(2024, 6, 27), date(2024, 6, 28), 1, pblntf_ty="Z")


def test_all_pages_retained(tmp_path: Path) -> None:
    """Both page payloads and both receipt ZIPs are locally registered."""
    catalog, root = _catalog(tmp_path)
    pages = {
        1: _page(1, [_row("20240531000001")]),
        2: _page(2, [_row("20240603000002", report="[정정]" + FORM, rm="정")]),
    }
    zips = {"20240531000001": _zip_bytes(b"one"), "20240603000002": _zip_bytes(b"two")}
    summary = collect_buyback_window(
        _FakeClient(pages, zips),  # type: ignore[arg-type]
        catalog,
        _Lake(),  # type: ignore[arg-type]
        date(2024, 5, 30),
        date(2024, 6, 4),
        root,
        "snap-1",
        "HISTORICAL_BACKFILL",
    )
    assert summary.pages == 2
    assert summary.candidates == 2
    assert summary.listed_candidates == 2
    assert summary.excluded_candidates == 0
    assert summary.documents_registered == 2
    assert summary.failed_receipts == 0
    assert summary.checkpoint_cursor == "page-2"
    assert catalog.find_artifact("dart", "list", "2024-05-30:2024-06-04:page-1", "snap-1") is not None
    assert catalog.find_artifact("dart", "list", "2024-05-30:2024-06-04:page-2", "snap-1") is not None
    assert catalog.find_artifact("dart", "document", "20240531000001", "snap-1") is not None
    assert catalog.find_artifact("dart", "document", "20240603000002", "snap-1") is not None
    assert (root / "raw/dart/snap-1/doc-20240531000001.zip").read_bytes() == _zip_bytes(b"one")


def test_resume_without_gaps(tmp_path: Path) -> None:
    """Page one is idempotent and page two is collected before checkpoint completion."""
    catalog, root = _catalog(tmp_path)
    pages = {1: _page(1, [_row("20240531000001")]), 2: _page(2, [_row("20240603000002")])}
    zips = {"20240531000001": _zip_bytes(b"one"), "20240603000002": _zip_bytes(b"two")}
    failing = _FakeClient(pages, zips, fail_lists={2})
    first = collect_buyback_window(
        failing,  # type: ignore[arg-type]
        catalog,
        _Lake(),  # type: ignore[arg-type]
        date(2024, 5, 30),
        date(2024, 6, 4),
        root,
        "snap-1",
        "HISTORICAL_BACKFILL",
    )
    assert first.pages == 1
    assert first.failed_receipts == 1
    assert first.checkpoint_cursor == "page-1"
    failing.fail_lists.clear()
    second = collect_buyback_window(
        failing,  # type: ignore[arg-type]
        catalog,
        _Lake(),  # type: ignore[arg-type]
        date(2024, 5, 30),
        date(2024, 6, 4),
        root,
        "snap-1",
        "HISTORICAL_BACKFILL",
    )
    assert second.pages == 1
    assert second.checkpoint_cursor == "page-2"
    assert catalog.find_artifact("dart", "document", "20240603000002", "snap-1") is not None


def test_list_failure_on_first_page_is_not_success(tmp_path: Path) -> None:
    catalog, root = _catalog(tmp_path)
    client = _FakeClient({}, {}, fail_lists={1})
    result = collect_buyback_window(
        client,  # type: ignore[arg-type]
        catalog,
        _Lake(),  # type: ignore[arg-type]
        date(2024, 5, 30),
        date(2024, 6, 4),
        root,
        "snap-1",
        "LIVE",
    )
    assert result.pages == 0
    assert result.failed_receipts == 1
    assert result.checkpoint_cursor == ""
    assert catalog.load_checkpoint("dart", "2024-05-30:2024-06-04", "snap-1") is None


def test_collected_correction_links_to_unique_original(tmp_path: Path) -> None:
    class _JuneLake(_Lake):
        def next_session(self, after: date) -> date:
            assert after == date(2024, 6, 26)
            return date(2024, 6, 27)

    original_no = "20240626000207"
    correction_no = "20240626000369"
    root = tmp_path / "data"
    catalog = Catalog(root / "catalog.sqlite")
    store = EventStore(catalog)
    probe = Path("data/raw/dart/pilot-202406-20260928")
    original = DartListRow(original_no, "01386916", "361610", "Y", FORM, date(2024, 6, 26), "정")
    correction = DartListRow(
        correction_no, "01386916", "361610", "Y", "[기재정정]" + FORM, date(2024, 6, 26), "정"
    )
    pages = {1: _page(1, [original]), 2: _page(2, [correction])}
    zips = {
        original_no: (probe / f"doc-{original_no}.zip").read_bytes(),
        correction_no: (probe / f"doc-{correction_no}.zip").read_bytes(),
    }
    summary = collect_buyback_window(
        _FakeClient(pages, zips),  # type: ignore[arg-type]
        catalog,
        _JuneLake(),  # type: ignore[arg-type]
        date(2024, 6, 26),
        date(2024, 6, 26),
        root,
        "snap-1",
        "HISTORICAL_BACKFILL",
        DocumentLimits(),
        store,
    )
    assert summary.failed_receipts == 0
    assert summary.documents_registered == 2
    filing = catalog.get_filing_asof(correction_no, datetime(2026, 1, 1, tzinfo=ZoneInfo("Asia/Seoul")))
    assert filing is not None
    assert filing.correction_flag
    links = store.list_prior_events(datetime(2026, 1, 1, tzinfo=ZoneInfo("Asia/Seoul")))
    assert len(links) == 1
    assert links[0][0].rcept_nos == (original_no, correction_no)
    assert links[0][0].status == "LINKED"


def test_retrospective_flag_keeps_original(tmp_path: Path) -> None:
    """Original stored without importing future correction contents."""
    catalog, root = _catalog(tmp_path)
    pages = {1: _page(1, [_row("20240531000001", rm="정")], count=1)}
    zips = {"20240531000001": _zip_bytes(b"original-only")}
    summary = collect_buyback_window(
        _FakeClient(pages, zips),  # type: ignore[arg-type]
        catalog,
        _Lake(),  # type: ignore[arg-type]
        date(2024, 5, 30),
        date(2024, 6, 4),
        root,
        "snap-1",
        "HISTORICAL_BACKFILL",
    )
    assert summary.documents_registered == 1
    filing = catalog.get_filing_asof("20240531000001", datetime(2026, 1, 1, tzinfo=ZoneInfo("Asia/Seoul")))
    assert filing is not None
    assert filing.correction_flag is False
    assert filing.raw_hash == hashlib.sha256(_zip_bytes(b"original-only")).hexdigest()
    assert filing.report_name == FORM


def test_current_detail_omission_preserves_original(tmp_path: Path) -> None:
    """Original ZIP remains required even when structured details omit it."""
    catalog, root = _catalog(tmp_path)
    pages = {1: _page(1, [_row("20240531000001")], count=1)}
    client = _FakeClient(pages, {"20240531000001": _zip_bytes(b"orig")})
    summary = collect_buyback_window(
        client,  # type: ignore[arg-type]
        catalog,
        _Lake(),  # type: ignore[arg-type]
        date(2024, 5, 30),
        date(2024, 6, 4),
        root,
        "snap-1",
        "HISTORICAL_BACKFILL",
    )
    assert summary.documents_registered == 1
    detail = client.current_buyback_details("00123456", date(2024, 5, 30), date(2024, 6, 4))
    assert b"20240531000001" not in detail
    assert catalog.find_artifact("dart", "document", "20240531000001", "snap-1") is not None


def test_credential_isolation(tmp_path: Path) -> None:
    """API key is absent from metadata, logs and errors."""
    seen: dict[str, object] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        payload = {
            "status": "000",
            "message": "정상",
            "page_no": 1,
            "page_count": 1,
            "total_page": 1,
            "total_count": 1,
            "list": [
                {
                    "rcept_no": "20240531000001",
                    "corp_code": "00123456",
                    "stock_code": "005930",
                    "corp_cls": "Y",
                    "report_nm": FORM,
                    "rcept_dt": "20240531",
                    "rm": "",
                }
            ],
        }
        return httpx.Response(200, json=payload)

    http = httpx.Client(transport=httpx.MockTransport(_handler))
    client = DartClient(API_KEY, http)
    page = client.list_major_reports(date(2024, 5, 30), date(2024, 6, 4), 1)
    assert page.rows[0].rcept_no == "20240531000001"
    assert API_KEY not in repr(client)
    assert API_KEY not in page.raw_bytes.decode("utf-8")
    catalog, root = _catalog(tmp_path)
    summary = collect_buyback_window(
        _FakeClient({1: page}, {"20240531000001": _zip_bytes(b"x")}),  # type: ignore[arg-type]
        catalog,
        _Lake(),  # type: ignore[arg-type]
        date(2024, 5, 30),
        date(2024, 6, 4),
        root,
        "snap-1",
        "HISTORICAL_BACKFILL",
    )
    assert summary.pages == 1
    artifact = catalog.find_artifact("dart", "list", "2024-05-30:2024-06-04:page-1", "snap-1")
    assert artifact is not None
    assert API_KEY not in artifact.request_key
    assert API_KEY not in artifact.local_relative_path.as_posix()

    def _fail_handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json={"status": "100", "message": "invalid key"})

    bad = DartClient(API_KEY, httpx.Client(transport=httpx.MockTransport(_fail_handler)))
    with pytest.raises(DartSourceError) as exc_info:
        bad.list_major_reports(date(2024, 5, 30), date(2024, 6, 4), 1)
    assert API_KEY not in str(exc_info.value)
    assert exc_info.value.status == "100"
    assert exc_info.value.retryable is False


def test_title_and_listing_filters(tmp_path: Path) -> None:
    """Trust-contract forms and unlisted rows are excluded but countable."""
    catalog, root = _catalog(tmp_path)
    pages = {
        1: _page(
            1,
            [
                _row("20240531000001"),
                _row("20240531000002", report="주요사항보고서(자기주식취득 신탁계약 체결결정)"),
                _row("20240531000003", cls="N"),
                _row("20240531000004", stock="00000"),
                _row("20240531000005", report="[기재정정] 주요사항보고서(자기주식취득결정) ", cls="K"),
                _row("20240531000006", report="[첨부정정] 주요사항보고서(자기주식취득결정) ", cls="K"),
            ],
            count=1,
        )
    }
    zips = {"20240531000001": _zip_bytes(b"a"), "20240531000005": _zip_bytes(b"b")}
    summary = collect_buyback_window(
        _FakeClient(pages, zips),  # type: ignore[arg-type]
        catalog,
        _Lake(),  # type: ignore[arg-type]
        date(2024, 5, 30),
        date(2024, 6, 4),
        root,
        "snap-1",
        "HISTORICAL_BACKFILL",
    )
    assert summary.candidates == 4
    assert summary.listed_candidates == 2
    assert summary.excluded_candidates == 2
    assert summary.documents_registered == 2


def test_pending_calendar_and_failures(tmp_path: Path) -> None:
    """Missing sessions retain raw rows; receipt failures stop before checkpoint."""
    catalog, root = _catalog(tmp_path)
    pages = {1: _page(1, [_row("20240531000001")], count=1)}
    summary = collect_buyback_window(
        _FakeClient(pages, {"20240531000001": _zip_bytes(b"a")}),  # type: ignore[arg-type]
        catalog,
        _Lake(missing=True),  # type: ignore[arg-type]
        date(2024, 5, 30),
        date(2024, 6, 4),
        root,
        "snap-1",
        "HISTORICAL_BACKFILL",
    )
    assert summary.documents_registered == 0
    assert summary.checkpoint_cursor == "page-1"
    assert catalog.find_artifact("dart", "document", "20240531000001", "snap-1") is not None

    catalog2, root2 = _catalog(tmp_path / "second")
    pages2 = {1: _page(1, [_row("20240531000001")], count=1)}
    summary2 = collect_buyback_window(
        _FakeClient(pages2, {}, fail_zips={"20240531000001"}),  # type: ignore[arg-type]
        catalog2,
        _Lake(),  # type: ignore[arg-type]
        date(2024, 5, 30),
        date(2024, 6, 4),
        root2,
        "snap-1",
        "HISTORICAL_BACKFILL",
    )
    assert summary2.failed_receipts == 1
    assert summary2.checkpoint_cursor == ""
    assert summary2.pages == 0


def _mock_client(handler: object) -> DartClient:
    return DartClient(API_KEY, httpx.Client(transport=httpx.MockTransport(handler)))  # type: ignore[arg-type]


def test_dart_client_boundaries() -> None:
    """Transport, schema and document guards fail closed with retry."""
    calls = {"n": 0}

    def _flaky(request: httpx.Request) -> httpx.Response:
        del request
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("down")
        return httpx.Response(
            200,
            json={
                "status": "000",
                "message": "ok",
                "page_no": 1,
                "page_count": 100,
                "total_page": 2,
                "total_count": 0,
                "list": [],
            },
        )

    page = _mock_client(_flaky).list_major_reports(date(2024, 5, 30), date(2024, 6, 4), 1)
    assert page.total_count == 0
    assert page.page_count == 2

    def _empty_ok(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json={"status": "010", "message": "no data"})

    assert _mock_client(_empty_ok).list_major_reports(date(2024, 5, 30), date(2024, 6, 4), 1).rows == ()

    def _quota(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json={"status": "020", "message": "limited"})

    with pytest.raises(DartSourceError, match=r"limited|020|quota"):
        _mock_client(_quota).list_major_reports(date(2024, 5, 30), date(2024, 6, 4), 1)

    def _bad_schema(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json={"status": "000", "page_no": 2, "page_count": 1, "total_page": 1, "total_count": 0, "list": []})

    with pytest.raises(DartSourceError) as schema_info:
        _mock_client(_bad_schema).list_major_reports(date(2024, 5, 30), date(2024, 6, 4), 1)
    assert schema_info.value.status == "SCHEMA"

    def _bad_row(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={"status": "000", "page_no": 1, "page_count": 1, "total_page": 1, "total_count": 1, "list": [{"rcept_no": ""}]},
        )

    with pytest.raises(DartSourceError) as row_info:
        _mock_client(_bad_row).list_major_reports(date(2024, 5, 30), date(2024, 6, 4), 1)
    assert row_info.value.status == "SCHEMA"

    def _not_json(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, content=b"not-json{")

    with pytest.raises(DartSourceError) as json_info:
        _mock_client(_not_json).list_major_reports(date(2024, 5, 30), date(2024, 6, 4), 1)
    assert json_info.value.status == "SCHEMA"

    def _doc(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, content=b"PK\x03\x04data")

    assert _mock_client(_doc).document_zip("20240531000001") == b"PK\x03\x04data"

    def _doc_error(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, content=b'{"status":"013"}')

    with pytest.raises(DartSourceError) as doc_info:
        _mock_client(_doc_error).document_zip("20240531000001")
    assert doc_info.value.status == "SCHEMA"

    def _detail(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, content=b'{"detail":1}')

    assert _mock_client(_detail).current_buyback_details("00123456", date(2024, 5, 30), date(2024, 6, 4)) == b'{"detail":1}'

    client = _mock_client(_detail)
    with pytest.raises(ValueError, match="must"):
        client.list_major_reports(date(2024, 6, 4), date(2024, 5, 30), 1)
    with pytest.raises(ValueError, match="must"):
        client.list_major_reports(date(2024, 5, 30), date(2024, 6, 4), 0)
    with pytest.raises(ValueError, match="must"):
        client.document_zip("  ")
    with pytest.raises(ValueError, match="must"):
        DartClient("", httpx.Client(transport=httpx.MockTransport(_detail)))


def test_transport_exhaustion_and_status() -> None:
    """Persistent transport and HTTP failures raise typed retryable errors."""

    def _down(request: httpx.Request) -> httpx.Response:
        del request
        raise httpx.ConnectError("down")

    with pytest.raises(DartSourceError) as list_info:
        _mock_client(_down).list_major_reports(date(2024, 5, 30), date(2024, 6, 4), 1)
    assert list_info.value.status == "TRANSPORT"
    assert list_info.value.retryable is True
    with pytest.raises(DartSourceError) as zip_info:
        _mock_client(_down).document_zip("20240531000001")
    assert zip_info.value.retryable is True
    with pytest.raises(DartSourceError) as detail_info:
        _mock_client(_down).current_buyback_details("00123456", date(2024, 5, 30), date(2024, 6, 4))
    assert detail_info.value.retryable is True

    def _busy(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(503, content=b"busy")

    with pytest.raises(DartSourceError) as busy_info:
        _mock_client(_busy).list_major_reports(date(2024, 5, 30), date(2024, 6, 4), 1)
    assert busy_info.value.retryable is True

    def _denied(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(404, content=b"nope")

    with pytest.raises(DartSourceError) as denied_info:
        _mock_client(_denied).document_zip("20240531000001")
    assert denied_info.value.retryable is False
