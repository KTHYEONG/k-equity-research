"""Invariant guards for official KRX index fetch, parse and local replay."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo

import httpx
import polars as pl
import pytest

from src.data.catalog import Catalog
from src.data.imports import ImportManifest, ImportPart
from src.data.index_store import IndexStore, merge_index_manifest
from src.data.krx_ingest import collect_index_sessions
from src.data.local_lake import PANEL_DATASET_ID, LocalLake
from src.integrations.krx_index import KrxIndexClient, KrxSourceError, parse_index_day

KST = ZoneInfo("Asia/Seoul")
SESSION = date(2024, 6, 27)
API_KEY = "SECRET-KEY-123"
PROBE = Path(__file__).resolve().parents[3] / "data" / "probe_krx"


def _row(name: str, open_price: str, close_price: str, day: str = "20240627", cls: str = "KOSPI") -> dict[str, str]:
    return {
        "BAS_DD": day,
        "IDX_CLSS": cls,
        "IDX_NM": name,
        "OPNPRC_IDX": open_price,
        "CLSPRC_IDX": close_price,
    }


def _payload(rows: list[dict[str, str]]) -> bytes:
    return json.dumps({"OutBlock_1": rows}).encode("utf-8")


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def test_exact_headline_row_selected() -> None:
    """A later exact 코스피 row wins over a blank first row and similarly named rows."""
    raw = _payload(
        [
            _row("코스피 (외국주포함)", "", ""),
            _row("코스피 200", "378.98", "382.27"),
            _row("코스피", "2767.62", "2784.06"),
        ]
    )
    bar = parse_index_day(raw, "KOSPI", SESSION, _digest(raw))
    assert bar.market == "KOSPI"
    assert bar.session == SESSION
    assert bar.open == Decimal("2767.62")
    assert bar.close == Decimal("2784.06")
    assert bar.source_hash == _digest(raw)
    assert bar.batch_available_at == datetime(2024, 6, 27, 18, 0, tzinfo=KST)


def test_blank_headline_open_produces_no_bar() -> None:
    """Blank, nonpositive, nonnumeric or mismatched headline values never form a bar."""
    cases = [
        [_row("코스피", "", "2784.06")],
        [_row("코스피", "2767.62", "")],
        [_row("코스피", "0", "2784.06")],
        [_row("코스피", "-1.5", "2784.06")],
        [_row("코스피", "abc", "2784.06")],
        [_row("코스피", "2767.62", "2784.06", day="20240628")],
        [_row("코스피", "2767.62", "2784.06", cls="KOSDAQ")],
        [_row("코스닥", "841.12", "838.65")],
        [],
        [_row("코스피", "2767.62", "2784.06"), _row("코스피", "2767.62", "2784.06")],
    ]
    for rows in cases:
        raw = _payload(rows)
        with pytest.raises(ValueError, match=r"index (value|row)"):
            parse_index_day(raw, "KOSPI", SESSION, _digest(raw))


def test_parse_contract_guards() -> None:
    """Unknown markets, bad hashes and malformed payloads fail closed."""
    raw = _payload([_row("코스피", "2767.62", "2784.06")])
    with pytest.raises(ValueError, match="market"):
        parse_index_day(raw, "NYSE", SESSION, _digest(raw))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="raw hash"):
        parse_index_day(raw, "KOSPI", SESSION, "not-a-hash")
    with pytest.raises(ValueError, match="malformed"):
        parse_index_day(b"not-json{", "KOSPI", SESSION, _digest(b"not-json{"))
    with pytest.raises(ValueError, match="malformed"):
        parse_index_day(b'{"other": []}', "KOSPI", SESSION, _digest(b'{"other": []}'))


def _mock_client(handler: object) -> KrxIndexClient:
    return KrxIndexClient(API_KEY, httpx.Client(transport=httpx.MockTransport(handler)))  # type: ignore[arg-type]


def test_fetch_day_boundaries() -> None:
    """Authorization, transport, schema and unavailable-day failures are typed."""
    seen: dict[str, object] = {}

    def _ok(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["params"] = dict(request.url.params)
        seen["auth"] = request.headers.get("AUTH_KEY")
        return httpx.Response(200, json={"OutBlock_1": [_row("코스피", "2767.62", "2784.06")]})

    raw = _mock_client(_ok).fetch_day("KOSPI", SESSION)
    assert json.loads(raw.decode("utf-8"))["OutBlock_1"][0]["IDX_NM"] == "코스피"
    assert seen["params"]["basDd"] == "20240627"  # type: ignore[index]
    assert "serviceKey" not in seen["params"]  # type: ignore[operator]
    assert seen["auth"] == API_KEY
    assert str(seen["url"]).startswith("https://data-dbg.krx.co.kr/svc/apis/idx/")
    assert "kospi_dd_trd" in str(seen["url"])
    assert _mock_client(_ok).fetch_day("KOSDAQ", SESSION) is not None

    def _empty(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json={"OutBlock_1": []})

    with pytest.raises(KrxSourceError) as empty_info:
        _mock_client(_empty).fetch_day("KOSPI", SESSION)
    assert empty_info.value.status == "NO_DATA"
    assert empty_info.value.retryable is False

    def _denied(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(403, content=b"denied")

    with pytest.raises(KrxSourceError) as auth_info:
        _mock_client(_denied).fetch_day("KOSPI", SESSION)
    assert auth_info.value.status == "AUTH"
    assert auth_info.value.retryable is False

    def _missing_page(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(404, content=b"nope")

    with pytest.raises(KrxSourceError) as denied_info:
        _mock_client(_missing_page).fetch_day("KOSPI", SESSION)
    assert denied_info.value.status == "TRANSPORT"
    assert denied_info.value.retryable is False

    def _list_body(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json=["not", "a", "dict"])

    with pytest.raises(KrxSourceError) as list_info:
        _mock_client(_list_body).fetch_day("KOSPI", SESSION)
    assert list_info.value.status == "SCHEMA"

    def _busy(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(503, content=b"busy")

    with pytest.raises(KrxSourceError) as busy_info:
        _mock_client(_busy).fetch_day("KOSPI", SESSION)
    assert busy_info.value.retryable is True

    def _garbage(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, content=b"not-json{")

    with pytest.raises(KrxSourceError) as schema_info:
        _mock_client(_garbage).fetch_day("KOSPI", SESSION)
    assert schema_info.value.status == "SCHEMA"

    client = _mock_client(_ok)
    with pytest.raises(ValueError, match="market"):
        client.fetch_day("NYSE", SESSION)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="api key"):
        KrxIndexClient("", httpx.Client(transport=httpx.MockTransport(_ok)))
    assert API_KEY not in repr(client)


def test_fetch_day_retries_then_recovers_or_exhausts() -> None:
    """Transient transport and busy responses retry with bounds before success or typed failure."""
    calls = {"n": 0}

    def _flaky(request: httpx.Request) -> httpx.Response:
        del request
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("down")
        if calls["n"] == 2:
            return httpx.Response(503, content=b"busy")
        return httpx.Response(200, json={"OutBlock_1": [_row("코스피", "2767.62", "2784.06")]})

    raw = _mock_client(_flaky).fetch_day("KOSPI", SESSION)
    assert json.loads(raw.decode("utf-8"))["OutBlock_1"][0]["IDX_NM"] == "코스피"
    assert calls["n"] == 3

    def _down(request: httpx.Request) -> httpx.Response:
        del request
        raise httpx.ConnectError("down")

    with pytest.raises(KrxSourceError) as down_info:
        _mock_client(_down).fetch_day("KOSPI", SESSION)
    assert down_info.value.status == "TRANSPORT"
    assert down_info.value.retryable is True


def test_credential_exclusion_from_cache_and_errors() -> None:
    """The API key never reaches cached bytes, catalog metadata or error text."""
    probe = (PROBE / "kospi_20240627.json").read_bytes()

    def _ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=probe)

    client = _mock_client(_ok)
    raw = client.fetch_day("KOSPI", SESSION)
    assert API_KEY not in raw.decode("utf-8")

    def _denied(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json={"status": "100", "message": "invalid key"})

    with pytest.raises(KrxSourceError) as exc_info:
        _mock_client(_denied).fetch_day("KOSPI", SESSION)
    assert API_KEY not in str(exc_info.value)
    assert API_KEY not in repr(_mock_client(_denied))


def test_project_local_replay_without_api_access(tmp_path: Path) -> None:
    """Cached raw responses replay the same bar and hash with no live lookup."""
    catalog = Catalog(tmp_path / "data" / "catalog.sqlite")
    kospi_probe = (PROBE / "kospi_20240627.json").read_bytes()
    kosdaq_probe = (PROBE / "kosdaq_20240627.json").read_bytes()

    class _Client:
        def fetch_day(self, market: str, session: date) -> bytes:
            assert session == SESSION
            return {"KOSPI": kospi_probe, "KOSDAQ": kosdaq_probe}[market]

    lake_root = tmp_path / "data"
    panel_path = lake_root / "imports" / PANEL_DATASET_ID / "year=2024/part.parquet"
    panel_path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({"session": [SESSION]}).write_parquet(panel_path)
    with panel_path.open("rb") as handle:
        panel_digest = hashlib.sha256(handle.read()).hexdigest()
    lake = LocalLake(
        lake_root,
        {
            PANEL_DATASET_ID: ImportManifest(
                PANEL_DATASET_ID,
                "0" * 64,
                datetime.now(tz=ZoneInfo("UTC")),
                (ImportPart(PurePosixPath("year=2024/part.parquet"), panel_digest, panel_path.stat().st_size),),
            )
        },
    )
    summary = collect_index_sessions(
        _Client(),  # type: ignore[arg-type]
        catalog,
        lake,
        None,
        SESSION,
        SESSION,
        tmp_path / "data",
        "snap-1",
    )
    assert summary.expected_keys == 2
    assert summary.fetched_keys == 2
    assert summary.unresolved_keys == ()
    expected_hash = _digest(kospi_probe)
    artifact = catalog.find_artifact("krx", "index", "KOSPI:20240627", "snap-1")
    assert artifact is not None
    assert artifact.sha256 == expected_hash

    manifest = merge_index_manifest(None, [("KOSPI", SESSION, expected_hash)], tmp_path / "data")
    store = IndexStore(catalog, tmp_path / "data", manifest)
    bars = store.window("KOSPI", SESSION, SESSION, datetime(2024, 6, 28, 9, 0, tzinfo=KST))
    assert len(bars) == 1
    assert bars[0].open == Decimal("2767.62")
    assert bars[0].close == Decimal("2784.06")
    assert bars[0].source_hash == expected_hash
    assert catalog.get_artifact_path(expected_hash) == PurePosixPath("raw/krx/snap-1/KOSPI-20240627.json")
