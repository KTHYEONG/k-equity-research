"""Invariant guards for official OpenDART financial statement fetch."""

from __future__ import annotations

import json

import httpx
import pytest

from src.integrations.dart import DartClient, DartSourceError, FinancialStatementRequest

API_KEY = "SECRET-KEY-123"
REQUEST = FinancialStatementRequest(corp_code="00126380", bsns_year=2018, reprt_code="11011", fs_div="OFS")


def _success_payload() -> bytes:
    return json.dumps(
        {
            "status": "000",
            "message": "정상",
            "list": [
                {
                    "rcept_no": "20190401004781",
                    "reprt_code": "11011",
                    "bsns_year": "2018",
                    "corp_code": "00126380",
                    "sj_div": "BS",
                    "account_id": "ifrs-full_Assets",
                    "ord": "1",
                    "currency": "KRW",
                    "thstrm_amount": "1000",
                }
            ],
        }
    ).encode("utf-8")


def test_canonical_request_forwards_exact_parameters() -> None:
    """Valid corp, year, report, and basis reach the official endpoint and raw bytes return."""
    raw = _success_payload()
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update({str(key): str(value) for key, value in request.url.params.items()})
        return httpx.Response(200, content=raw)

    client = DartClient(API_KEY, httpx.Client(transport=httpx.MockTransport(handler)))
    assert client.fetch_financial_statement(REQUEST) == raw
    assert seen["corp_code"] == "00126380"
    assert seen["bsns_year"] == "2018"
    assert seen["reprt_code"] == "11011"
    assert seen["fs_div"] == "OFS"
    assert seen["crtfc_key"] == API_KEY
    assert API_KEY not in repr(client)


def test_quota_and_authorization_surface_as_failures() -> None:
    """Quota or authorization statuses raise and can never read as zero facts."""

    def quota_handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json={"status": "020", "message": "quota exceeded"})

    quota = DartClient(API_KEY, httpx.Client(transport=httpx.MockTransport(quota_handler)))
    with pytest.raises(DartSourceError) as quota_info:
        quota.fetch_financial_statement(REQUEST)
    assert quota_info.value.status == "020"
    assert quota_info.value.retryable is True

    def auth_handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json={"status": "100", "message": "invalid value"})

    auth = DartClient(API_KEY, httpx.Client(transport=httpx.MockTransport(auth_handler)))
    with pytest.raises(DartSourceError) as auth_info:
        auth.fetch_financial_statement(REQUEST)
    assert auth_info.value.retryable is False


def test_no_data_is_not_an_empty_statement() -> None:
    """A 013 response raises instead of returning bytes that look like zero rows."""

    def empty_handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json={"status": "013", "message": "no data"})

    client = DartClient(API_KEY, httpx.Client(transport=httpx.MockTransport(empty_handler)))
    with pytest.raises(DartSourceError) as exc_info:
        client.fetch_financial_statement(REQUEST)
    assert exc_info.value.status == "013"
    assert exc_info.value.retryable is False


def test_invalid_arguments_rejected_before_transport() -> None:
    def fail_handler(request: httpx.Request) -> httpx.Response:
        del request
        raise AssertionError("transport must not be called")

    client = DartClient(API_KEY, httpx.Client(transport=httpx.MockTransport(fail_handler)))
    with pytest.raises(ValueError, match="corp code"):
        client.fetch_financial_statement(
            FinancialStatementRequest(corp_code="123", bsns_year=2018, reprt_code="11011", fs_div="OFS")
        )
    with pytest.raises(ValueError, match="business year"):
        client.fetch_financial_statement(
            FinancialStatementRequest(corp_code="00126380", bsns_year=2010, reprt_code="11011", fs_div="OFS")
        )
    with pytest.raises(ValueError, match="report code"):
        client.fetch_financial_statement(
            FinancialStatementRequest(corp_code="00126380", bsns_year=2018, reprt_code="99999", fs_div="OFS")
        )


def test_transport_retry_then_success() -> None:
    """A transient transport failure retries with the same parameters and returns raw bytes."""
    raw = _success_payload()
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            raise httpx.ConnectError("boom")
        assert request.url.params["corp_code"] == "00126380"
        return httpx.Response(200, content=raw)

    client = DartClient(API_KEY, httpx.Client(transport=httpx.MockTransport(handler)))
    assert client.fetch_financial_statement(REQUEST) == raw
    assert calls["count"] == 2


def test_retryable_http_then_success() -> None:
    """A 503 response retries and a later official payload is returned unmodified."""
    raw = _success_payload()
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(503, content=b"busy")
        return httpx.Response(200, content=raw)

    client = DartClient(API_KEY, httpx.Client(transport=httpx.MockTransport(handler)))
    assert client.fetch_financial_statement(REQUEST) == raw


def test_malformed_and_unknown_statuses_fail_closed() -> None:
    """Non-JSON bytes, empty statuses, and unknown provider codes raise without empty rows."""

    def broken_handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, content=b"not json")

    broken = DartClient(API_KEY, httpx.Client(transport=httpx.MockTransport(broken_handler)))
    with pytest.raises(DartSourceError) as broken_info:
        broken.fetch_financial_statement(REQUEST)
    assert broken_info.value.status == "SCHEMA"

    def unknown_handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json={"status": "800", "message": "maintenance"})

    unknown = DartClient(API_KEY, httpx.Client(transport=httpx.MockTransport(unknown_handler)))
    with pytest.raises(DartSourceError) as unknown_info:
        unknown.fetch_financial_statement(REQUEST)
    assert unknown_info.value.status == "800"
    assert unknown_info.value.retryable is True


def test_retry_budget_and_schema_guards() -> None:
    """Persistent transport, HTTP, and schema failures raise with typed statuses."""
    bad_basis = FinancialStatementRequest(corp_code="00126380", bsns_year=2018, reprt_code="11011", fs_div="X")  # type: ignore[arg-type]
    dummy = DartClient(
        API_KEY, httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})))
    )
    with pytest.raises(ValueError, match="fs division"):
        dummy.fetch_financial_statement(bad_basis)

    def dead_handler(request: httpx.Request) -> httpx.Response:
        del request
        raise httpx.ConnectError("down")

    dead = DartClient(API_KEY, httpx.Client(transport=httpx.MockTransport(dead_handler)))
    with pytest.raises(DartSourceError) as dead_info:
        dead.fetch_financial_statement(REQUEST)
    assert dead_info.value.status == "TRANSPORT"
    assert dead_info.value.retryable is True

    def busy_handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(503, content=b"busy")

    busy = DartClient(API_KEY, httpx.Client(transport=httpx.MockTransport(busy_handler)))
    with pytest.raises(DartSourceError) as busy_info:
        busy.fetch_financial_statement(REQUEST)
    assert busy_info.value.status == "TRANSPORT"

    def missing_handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(404, content=b"missing")

    missing = DartClient(API_KEY, httpx.Client(transport=httpx.MockTransport(missing_handler)))
    with pytest.raises(DartSourceError) as missing_info:
        missing.fetch_financial_statement(REQUEST)
    assert missing_info.value.retryable is False

    def list_handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, content=b"[1, 2]")

    listed = DartClient(API_KEY, httpx.Client(transport=httpx.MockTransport(list_handler)))
    with pytest.raises(DartSourceError) as listed_info:
        listed.fetch_financial_statement(REQUEST)
    assert listed_info.value.status == "SCHEMA"

    def statusless_handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json={"list": []})

    statusless = DartClient(API_KEY, httpx.Client(transport=httpx.MockTransport(statusless_handler)))
    with pytest.raises(DartSourceError) as statusless_info:
        statusless.fetch_financial_statement(REQUEST)
    assert statusless_info.value.status == "SCHEMA"
