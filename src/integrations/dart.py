"""OpenDART list and immutable document collection over injected HTTP."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx

_LIST_URL = "https://opendart.fss.or.kr/api/list.json"
_DOCUMENT_URL = "https://opendart.fss.or.kr/api/document.xml"
_DETAIL_URL = "https://opendart.fss.or.kr/api/buybackDetail.json"

_MAX_ATTEMPTS = 3
_PAGE_SIZE = 100
_AUTH_STATUSES = frozenset({"100", "101", "102", "110", "111", "112"})
_RETRYABLE_HTTP = frozenset({429, 500, 502, 503, 504})


class DartSourceError(Exception):
    """Typed boundary failure for DART authorization, quota, transport and schema errors."""

    def __init__(self, status: str, retryable: bool, message: str = "") -> None:
        self.status = status
        self.retryable = retryable
        super().__init__(message or status)


@dataclass(frozen=True, slots=True)
class DartListRow:
    """One OpenDART list row as returned by the source API."""

    rcept_no: str
    corp_code: str
    stock_code: str
    corp_cls: str
    report_name: str
    rcept_date: date
    rm: str


@dataclass(frozen=True, slots=True)
class DartListPage:
    """One complete OpenDART list page with preserved response bytes."""

    page_no: int
    page_count: int
    total_count: int
    raw_bytes: bytes
    rows: tuple[DartListRow, ...]


def _safe_message(message: str) -> str:
    return message[:500]


def _coerce_int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(label)
    try:
        result = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(label) from exc
    return result


def _parse_receipt_date(value: Any) -> date:
    text = str(value).strip()
    if len(text) != 8 or not text.isdigit():
        raise ValueError("invalid receipt date")
    return date(int(text[0:4]), int(text[4:6]), int(text[6:8]))


def _build_row(item: Any) -> DartListRow:
    if not isinstance(item, dict):
        raise ValueError("invalid row")
    rcept_no = str(item.get("rcept_no", "")).strip()
    corp_code = str(item.get("corp_code", "")).strip()
    stock_code = str(item.get("stock_code", "") or "").strip()
    corp_cls = str(item.get("corp_cls", "") or "").strip()
    report_name = str(item.get("report_nm", "") or "").strip()
    rm = str(item.get("rm", "") or "").strip()
    if not rcept_no or not corp_code or not report_name:
        raise ValueError("invalid row")
    rcept_date = _parse_receipt_date(item.get("rcept_dt", ""))
    return DartListRow(
        rcept_no=rcept_no,
        corp_code=corp_code,
        stock_code=stock_code,
        corp_cls=corp_cls,
        report_name=report_name,
        rcept_date=rcept_date,
        rm=rm,
    )


class DartClient:
    """Authenticated OpenDART collection client with bounded retry."""

    def __init__(self, api_key: str, http_client: httpx.Client) -> None:
        if not api_key:
            raise ValueError("api key must be non-empty")
        self._api_key = api_key
        self._client = http_client

    def __repr__(self) -> str:
        return "DartClient(<redacted>)"

    def list_major_reports(self, start: date, end: date, page: int) -> DartListPage:
        """Fetch one OpenDART B-report page with last_reprt_at=N and 100 rows per page. Preserve response bytes and page metadata; raise a typed source error on API status or schema failure without treating an incomplete page as success."""
        if page < 1:
            raise ValueError("page must be >= 1")
        if start > end:
            raise ValueError("collection window must not be empty")
        params = {
            "crtfc_key": self._api_key,
            "bgn_de": start.strftime("%Y%m%d"),
            "end_de": end.strftime("%Y%m%d"),
            "last_reprt_at": "N",
            "pblntf_ty": "B",
            "page_no": str(page),
            "page_count": str(_PAGE_SIZE),
        }
        attempts = 0
        while True:
            attempts += 1
            try:
                response = self._client.get(_LIST_URL, params=params)
            except httpx.HTTPError:
                if attempts >= _MAX_ATTEMPTS:
                    raise DartSourceError("TRANSPORT", True, "dart list transport failure") from None
                continue
            if response.status_code in _RETRYABLE_HTTP:
                if attempts >= _MAX_ATTEMPTS:
                    raise DartSourceError("TRANSPORT", True, "dart list transport failure")
                continue
            if response.status_code != 200:
                raise DartSourceError("TRANSPORT", False, "dart list transport failure")
            raw = bytes(response.content)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                raise DartSourceError("SCHEMA", False, "dart list schema failure") from None
            if not isinstance(payload, dict):
                raise DartSourceError("SCHEMA", False, "dart list schema failure")
            status = str(payload.get("status", "")).strip()
            message = _safe_message(str(payload.get("message", "")))
            if status == "010":
                return DartListPage(page_no=page, page_count=0, total_count=0, raw_bytes=raw, rows=())
            if status == "020":
                if attempts >= _MAX_ATTEMPTS:
                    raise DartSourceError(status, True, message or "dart quota exceeded")
                continue
            if status != "000":
                if status in _AUTH_STATUSES:
                    raise DartSourceError(status, False, message or "dart authorization failure")
                raise DartSourceError(status, True, message or "dart request failure")
            try:
                page_no = _coerce_int(payload.get("page_no"), "page_no")
                page_count = _coerce_int(payload.get("page_count"), "page_count")
                total_count = _coerce_int(payload.get("total_count"), "total_count")
                items = payload.get("list")
                if not isinstance(items, list):
                    raise ValueError("list")
                if page_no != page:
                    raise ValueError("page_no")
                if len(items) > _PAGE_SIZE:
                    raise ValueError("page size")
                rows = tuple(_build_row(item) for item in items)
            except ValueError:
                raise DartSourceError("SCHEMA", False, "dart list schema failure") from None
            return DartListPage(
                page_no=page_no, page_count=page_count, total_count=total_count, raw_bytes=raw, rows=rows
            )

    def document_zip(self, rcept_no: str) -> bytes:
        """Fetch the immutable source document for one receipt number. Return exact ZIP bytes; reject error payloads and non-ZIP responses."""
        key = rcept_no.strip()
        if not key:
            raise ValueError("receipt number must be non-empty")
        params = {"crtfc_key": self._api_key, "rcept_no": key}
        attempts = 0
        while True:
            attempts += 1
            try:
                response = self._client.get(_DOCUMENT_URL, params=params)
            except httpx.HTTPError:
                if attempts >= _MAX_ATTEMPTS:
                    raise DartSourceError("TRANSPORT", True, "dart document transport failure") from None
                continue
            if response.status_code in _RETRYABLE_HTTP:
                if attempts >= _MAX_ATTEMPTS:
                    raise DartSourceError("TRANSPORT", True, "dart document transport failure")
                continue
            if response.status_code != 200:
                raise DartSourceError("TRANSPORT", False, "dart document transport failure")
            raw = bytes(response.content)
            if len(raw) < 4 or not raw.startswith(b"PK"):
                raise DartSourceError("SCHEMA", False, "dart document schema failure")
            return raw

    def current_buyback_details(self, corp_code: str, start: date, end: date) -> bytes:
        """Fetch the current structured buyback view for cross-checking only; it cannot reconstruct prior corrected receipts."""
        code = corp_code.strip()
        if not code:
            raise ValueError("corp code must be non-empty")
        if start > end:
            raise ValueError("collection window must not be empty")
        params = {
            "crtfc_key": self._api_key,
            "corp_code": code,
            "bgn_de": start.strftime("%Y%m%d"),
            "end_de": end.strftime("%Y%m%d"),
        }
        attempts = 0
        while True:
            attempts += 1
            try:
                response = self._client.get(_DETAIL_URL, params=params)
            except httpx.HTTPError:
                if attempts >= _MAX_ATTEMPTS:
                    raise DartSourceError("TRANSPORT", True, "dart detail transport failure") from None
                continue
            if response.status_code in _RETRYABLE_HTTP:
                if attempts >= _MAX_ATTEMPTS:
                    raise DartSourceError("TRANSPORT", True, "dart detail transport failure")
                continue
            if response.status_code != 200:
                raise DartSourceError("TRANSPORT", False, "dart detail transport failure")
            return bytes(response.content)


__all__ = [
    "DartClient",
    "DartListPage",
    "DartListRow",
    "DartSourceError",
]
