"""Official KRX daily index collection over injected HTTP."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from zoneinfo import ZoneInfo

import httpx

Market = Literal["KOSPI", "KOSDAQ"]

_BASE_URL = "https://data-dbg.krx.co.kr/svc/apis"
_ENDPOINTS: dict[str, str] = {"KOSPI": "idx/kospi_dd_trd", "KOSDAQ": "idx/kosdaq_dd_trd"}
_HEADLINE: dict[str, str] = {"KOSPI": "코스피", "KOSDAQ": "코스닥"}

_MAX_ATTEMPTS = 3
_RETRYABLE_HTTP = frozenset({429, 500, 502, 503, 504})
_AUTH_HTTP = frozenset({401, 403})
_HEXDIGITS = frozenset("0123456789abcdefABCDEF")
_KST = ZoneInfo("Asia/Seoul")


class KrxSourceError(Exception):
    """Typed boundary failure for KRX authorization, transport and schema errors."""

    def __init__(self, status: str, retryable: bool, message: str = "") -> None:
        self.status = status
        self.retryable = retryable
        super().__init__(message or status)


@dataclass(frozen=True, slots=True)
class IndexBar:
    """One official daily headline index bar with conservative batch availability."""

    market: Market
    session: date
    open: Decimal
    close: Decimal
    source_hash: str
    batch_available_at: datetime


def _is_hex64(value: str) -> bool:
    return len(value) == 64 and all(char in _HEXDIGITS for char in value)


def _batch_available_at(session: date) -> datetime:
    return datetime(session.year, session.month, session.day, 18, 0, 0, tzinfo=_KST)


def _decimal_or_raise(value: Any, label: str) -> Decimal:
    text = str(value).strip().replace(",", "")
    if not text:
        raise ValueError(f"blank index value: {label}")
    try:
        amount = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"nonnumeric index value: {label}") from exc
    if amount <= 0:
        raise ValueError(f"nonpositive index value: {label}")
    return amount


class KrxIndexClient:
    """Authenticated official KRX index client with bounded retry."""

    def __init__(self, api_key: str, http_client: httpx.Client) -> None:
        if not api_key:
            raise ValueError("api key must be non-empty")
        self._api_key = api_key
        self._client = http_client

    def __repr__(self) -> str:
        return "KrxIndexClient(<redacted>)"

    def fetch_day(self, market: Market, session: date) -> bytes:
        """Fetch exact official daily index response bytes for one market and session. Raise a typed source error on rejected authorization, malformed response or unavailable day; do not substitute self-built baskets."""
        endpoint = _ENDPOINTS.get(market)
        if endpoint is None:
            raise ValueError("market must be KOSPI or KOSDAQ")
        url = f"{_BASE_URL}/{endpoint}"
        params = {"basDd": session.strftime("%Y%m%d")}
        attempts = 0
        while True:
            attempts += 1
            try:
                response = self._client.get(url, params=params, headers={"AUTH_KEY": self._api_key})
            except httpx.HTTPError:
                if attempts >= _MAX_ATTEMPTS:
                    raise KrxSourceError("TRANSPORT", True, "krx index transport failure") from None
                continue
            if response.status_code in _RETRYABLE_HTTP:
                if attempts >= _MAX_ATTEMPTS:
                    raise KrxSourceError("TRANSPORT", True, "krx index transport failure")
                continue
            if response.status_code in _AUTH_HTTP:
                raise KrxSourceError("AUTH", False, "krx index authorization failure")
            if response.status_code != 200:
                raise KrxSourceError("TRANSPORT", False, "krx index transport failure")
            raw = bytes(response.content)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                raise KrxSourceError("SCHEMA", False, "krx index schema failure") from None
            if not isinstance(payload, dict):
                raise KrxSourceError("SCHEMA", False, "krx index schema failure")
            rows = payload.get("OutBlock_1")
            if not isinstance(rows, list) or not rows:
                raise KrxSourceError("NO_DATA", False, "krx index day unavailable")
            return raw


def parse_index_day(raw_bytes: bytes, market: Market, session: date, raw_hash: str) -> IndexBar:
    """Select the one exact headline index row and validate positive open/close before research use. Raise ValueError on missing, duplicate or blank headline rows."""
    if market not in _HEADLINE:
        raise ValueError("market must be KOSPI or KOSDAQ")
    if not _is_hex64(raw_hash):
        raise ValueError("invalid raw hash")
    try:
        payload = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("malformed index response") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("OutBlock_1"), list):
        raise ValueError("malformed index response")
    wanted = session.strftime("%Y%m%d")
    headline = _HEADLINE[market]
    matches = [
        row
        for row in payload["OutBlock_1"]
        if isinstance(row, dict)
        and str(row.get("BAS_DD", "")).strip() == wanted
        and str(row.get("IDX_CLSS", "")).strip() == market
        and str(row.get("IDX_NM", "")).strip() == headline
    ]
    if len(matches) != 1:
        raise ValueError("missing or duplicate headline index row")
    row = matches[0]
    return IndexBar(
        market=market,
        session=session,
        open=_decimal_or_raise(row.get("OPNPRC_IDX"), "open"),
        close=_decimal_or_raise(row.get("CLSPRC_IDX"), "close"),
        source_hash=raw_hash.lower(),
        batch_available_at=_batch_available_at(session),
    )


__all__ = ["IndexBar", "KrxIndexClient", "KrxSourceError", "parse_index_day"]
