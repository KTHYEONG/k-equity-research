"""Validated point-in-time reads over project-local imported datasets."""

from __future__ import annotations

import hashlib
import json
from bisect import bisect_left, bisect_right
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from zoneinfo import ZoneInfo

import polars as pl

from src.data.imports import ImportManifest
from src.data.local_paths import checked_local_path

PANEL_DATASET_ID = "market_panel_from_20220707_v1"
UNIVERSE_DATASET_ID = "ordinary_universe_from_20220707_v1"
FACTS_DATASET_ID = "financial_facts_from_20220101_v1"
FACTS_V2_DATASET_ID = "financial_facts_from_20220101_v2"
SecurityStatus = Literal["OK", "AMBIGUOUS", "MISSING", "MISSING_LOCAL"]

ORDINARY_SHARE_KIND = "보통주"

_CHUNK_SIZE = 1024 * 1024

_BAR_COLUMNS = (
    "instrument_id",
    "session",
    "market",
    "open",
    "close",
    "market_cap",
    "listed_shares",
    "trading_value",
    "ret_price",
    "price_state",
    "gap_before",
    "share_factor",
    "available_at",
    "source_hash",
)

_UNIVERSE_COLUMNS = (
    "instrument_id",
    "ticker",
    "market",
    "source_security_id",
    "share_kind",
    "session",
    "available_at",
)


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(_CHUNK_SIZE), b""):
            digest.update(block)
    return digest.hexdigest()


def _require_aware(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")


def _opt_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _opt_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _hint_matches(path: Path, key: str, wanted: str) -> bool:
    for part in path.parts:
        if part.startswith(key):
            return part[len(key):] == wanted
    return True


def _year_in_window(path: Path, start: date, end: date) -> bool:
    for part in path.parts:
        if part.startswith("year="):
            year = part[len("year="):]
            return year.isdigit() and start.year <= int(year) <= end.year
    return True


def _has_columns(path: Path, columns: tuple[str, ...]) -> bool:
    names = pl.scan_parquet(str(path)).collect_schema().names()
    return all(column in names for column in columns)


def _asof_literal(files: list[str], as_of: datetime) -> datetime:
    dtype = pl.scan_parquet(files[0]).collect_schema()["available_at"]
    zone = dtype.time_zone if isinstance(dtype, pl.Datetime) else None
    return as_of.astimezone(ZoneInfo(zone or "UTC"))


@dataclass(frozen=True, slots=True)
class SecurityMatch:
    """One historical ordinary-share identity verdict."""

    instrument_id: str
    ticker: str
    market: str
    source_security_id: str
    session: date
    status: SecurityStatus


@dataclass(frozen=True, slots=True)
class MarketBar:
    """One source-backed local market bar with explicit nullable values."""

    instrument_id: str
    session: date
    open: int | None
    close: int | None
    market_cap: int | None
    listed_shares: int | None
    trading_value: int | None
    ret_price: float | None
    price_state: str
    gap_before: bool
    share_factor: float | None
    available_at: datetime
    source_hash: str


def _to_bar(row: dict[str, Any]) -> MarketBar | None:
    if (
        row["price_state"] is None
        or row["gap_before"] is None
        or row["available_at"] is None
        or row["source_hash"] is None
    ):
        return None
    return MarketBar(
        instrument_id=str(row["instrument_id"]),
        session=row["session"],
        open=_opt_int(row["open"]),
        close=_opt_int(row["close"]),
        market_cap=_opt_int(row["market_cap"]),
        listed_shares=_opt_int(row["listed_shares"]),
        trading_value=_opt_int(row["trading_value"]),
        ret_price=_opt_float(row["ret_price"]),
        price_state=str(row["price_state"]),
        gap_before=bool(row["gap_before"]),
        share_factor=_opt_float(row["share_factor"]),
        available_at=row["available_at"],
        source_hash=str(row["source_hash"]),
    )


_INSTRUMENT_CACHE_SIZE = 512
_UNIVERSE_CACHE_SIZE = 8192


class LocalLake:
    """Validated reads over this repository's imported market and universe parts."""

    def __init__(self, data_root: Path, manifests: Mapping[str, ImportManifest]) -> None:
        if data_root.is_symlink() or not data_root.is_dir():
            raise ValueError(f"missing project data root: {data_root}")
        self._data_root = data_root
        self._parts: dict[str, tuple[Path, ...]] = {}
        for dataset_id, manifest in manifests.items():
            self._parts[dataset_id] = self._verify_dataset(data_root, dataset_id, manifest)
        self._universe_cache: OrderedDict[tuple[date, str, str], tuple[dict[str, Any], ...]] = OrderedDict()
        self._name_cache: dict[str, dict[str, str]] = {}
        self._zones: dict[str, str | None] = {}
        self._instrument_cache: OrderedDict[str, tuple[tuple[Path, dict[str, Any]], ...]] = OrderedDict()
        self._sessions = self._load_sessions()

    def _verify_dataset(
        self, data_root: Path, dataset_id: str, manifest: ImportManifest
    ) -> tuple[Path, ...]:
        if not dataset_id or "/" in dataset_id or dataset_id != manifest.dataset_id:
            raise ValueError(f"unregistered local dataset: {dataset_id!r}")
        verified: list[Path] = []
        for part in manifest.parts:
            text = part.relative_path.as_posix()
            if not text or text == "." or part.relative_path.is_absolute() or ".." in part.relative_path.parts:
                raise ValueError(f"unsafe local part: {text!r}")
            target = checked_local_path(data_root, PurePosixPath("imports") / dataset_id / text)
            if target.is_symlink() or not target.is_file():
                raise ValueError(f"missing local part: {text!r}")
            if target.stat().st_size != part.byte_length or _sha256_of(target) != part.sha256.lower():
                raise ValueError(f"hash mismatch for local part: {text!r}")
            try:
                pl.scan_parquet(str(target)).collect_schema()
            except Exception as exc:
                raise ValueError(f"invalid local part: {text!r}") from exc
            verified.append(target)
        return tuple(verified)

    def _load_sessions(self) -> tuple[date, ...]:
        found: set[date] = set()
        for dataset_id in (PANEL_DATASET_ID, UNIVERSE_DATASET_ID):
            for path in self._parts.get(dataset_id, ()):
                if not _has_columns(path, ("session",)):
                    continue
                for row in pl.scan_parquet(str(path)).select("session").collect().to_dicts():
                    found.add(row["session"])
        return tuple(sorted(found))

    def dataset_parts(self, dataset_id: str) -> tuple[Path, ...]:
        """Return verified local part paths for one registered dataset."""
        try:
            return self._parts[dataset_id]
        except KeyError:
            raise ValueError(f"dataset not registered locally: {dataset_id}") from None

    def financial_parts(self) -> tuple[Path, ...]:
        """Prefer the corrected immutable index when it is registered locally."""
        return self.dataset_parts(FACTS_V2_DATASET_ID if FACTS_V2_DATASET_ID in self._parts else FACTS_DATASET_ID)

    def previous_session(self, before: date) -> date | None:
        """Return the last validated local KRX session strictly before the date; return None when coverage is insufficient."""
        index = bisect_left(self._sessions, before)
        if index == 0:
            return None
        return self._sessions[index - 1]

    def next_session(self, after: date) -> date | None:
        """Return the first validated local KRX session strictly after the date. Return None when calendar coverage is insufficient; never infer a trading day from weekdays alone."""
        index = bisect_right(self._sessions, after)
        if index >= len(self._sessions):
            return None
        return self._sessions[index]

    def sessions_between(self, start: date, end: date) -> tuple[date, ...]:
        """Return verified retained exchange sessions within inclusive dates.

        The session calendar comes from the active local market dataset, not
        weekdays or an external project. Raise if the active import is invalid.
        """
        if start > end:
            raise ValueError("session window must not be empty")
        return tuple(session for session in self._sessions if start <= session <= end)

    def _universe_lookup(self, session: date, column: str, value: str) -> tuple[dict[str, Any], ...]:
        """Return one session's universe rows for a ticker or ISIN, unfiltered by availability, scanning parquet once.

        Keyed by the identifier rather than by session because prior-event lookups touch one row of a large
        session. The point-in-time availability filter is applied per call by the caller.
        """
        key = (session, column, value)
        cached = self._universe_cache.get(key)
        if cached is not None:
            self._universe_cache.move_to_end(key)
            return cached
        files = [
            path
            for path in self._parts[UNIVERSE_DATASET_ID]
            if _hint_matches(path, "session=", session.isoformat()) and _has_columns(path, _UNIVERSE_COLUMNS)
        ]
        rows: tuple[dict[str, Any], ...] = ()
        if files:
            columns = list(_UNIVERSE_COLUMNS)
            if all(_has_columns(path, ("source_hash",)) for path in files):
                columns.append("source_hash")
            rows = tuple(
                pl.scan_parquet([str(path) for path in files])
                .filter((pl.col("session") == session) & (pl.col(column) == value))
                .select(columns)
                .collect()
                .to_dicts()
            )
        self._universe_cache[key] = rows
        if len(self._universe_cache) > _UNIVERSE_CACHE_SIZE:
            self._universe_cache.popitem(last=False)
        return rows

    def _universe_rows(self, session: date, ticker: str, as_of: datetime) -> list[dict[str, Any]]:
        rows = self._universe_lookup(session, "ticker", ticker)
        if not rows:
            return []
        cutoff = as_of.astimezone(ZoneInfo(self._available_at_zone(UNIVERSE_DATASET_ID) or "UTC"))
        return [row for row in rows if row["available_at"] <= cutoff]

    def security_name(self, source_security_id: str, session: date, as_of: datetime) -> tuple[str, str] | None:
        """Return the listed short name and its verified source-payload hash for one security on one session.

        The name comes from the KRX security-master payload cited by that session's universe row, so it is the name
        in force then, not today's. Returns None unless exactly one universe row and one payload record match and the
        payload bytes hash to the cited digest.
        """
        _require_aware(as_of, "as_of")
        cutoff = as_of.astimezone(ZoneInfo(self._available_at_zone(UNIVERSE_DATASET_ID) or "UTC"))
        cited = {
            str(row["source_hash"]).lower()
            for row in self._universe_lookup(session, "source_security_id", source_security_id)
            if row["available_at"] <= cutoff
            and row.get("source_hash")
        }
        if len(cited) != 1:
            return None
        digest = next(iter(cited))
        names = self._security_master_names(digest)
        name = names.get(source_security_id)
        return None if not name else (name, digest)

    def _security_master_names(self, digest: str) -> dict[str, str]:
        cached = self._name_cache.get(digest)
        if cached is not None:
            return cached
        names: dict[str, str] = {}
        target = checked_local_path(self._data_root, PurePosixPath("raw/imported/security_master") / digest / "payload.json")
        try:
            raw = target.read_bytes()
        except OSError:
            raw = b""
        if raw and hashlib.sha256(raw).hexdigest() == digest:
            counts: dict[str, int] = {}
            for record in json.loads(raw.decode("utf-8")).get("records", []):
                code, abbreviation = str(record.get("ISU_CD", "")), str(record.get("ISU_ABBRV", "")).strip()
                counts[code] = counts.get(code, 0) + 1
                names[code] = abbreviation
            names = {code: name for code, name in names.items() if counts[code] == 1}
            self._name_cache[digest] = names
        return names

    def _instrument_rows(self, instrument_id: str) -> tuple[tuple[Path, dict[str, Any]], ...]:
        """Return every retained panel row of one instrument with its source part, reading parquet once per instrument.

        Rows are immutable imported data; session and availability filters are applied per call by the caller so
        point-in-time semantics never depend on cache state.
        """
        cached = self._instrument_cache.get(instrument_id)
        if cached is not None:
            self._instrument_cache.move_to_end(instrument_id)
            return cached
        files = [str(path) for path in self._parts[PANEL_DATASET_ID] if _has_columns(path, _BAR_COLUMNS)]
        rows: tuple[tuple[Path, dict[str, Any]], ...] = ()
        if files:
            frame = (
                pl.scan_parquet(files, include_file_paths="_part")
                .filter(pl.col("instrument_id") == instrument_id)
                .select([*_BAR_COLUMNS, "_part"])
                .collect()
            )
            rows = tuple((Path(str(row.pop("_part"))), row) for row in frame.to_dicts())
        self._instrument_cache[instrument_id] = rows
        if len(self._instrument_cache) > _INSTRUMENT_CACHE_SIZE:
            self._instrument_cache.popitem(last=False)
        return rows

    def _panel_rows(
        self, instrument_id: str, start: date, end: date, as_of: datetime
    ) -> list[dict[str, Any]]:
        rows = self._instrument_rows(instrument_id)
        if not rows:
            return []
        zone = self._available_at_zone()
        cutoff = as_of.astimezone(ZoneInfo(zone or "UTC"))
        # The partition year in a part's path is authoritative, exactly as in a pruned scan.
        return [
            row
            for part, row in rows
            if _year_in_window(part, start, end) and start <= row["session"] <= end and row["available_at"] <= cutoff
        ]

    def _available_at_zone(self, dataset_id: str = PANEL_DATASET_ID) -> str | None:
        """Return the timezone of ``available_at`` for a dataset; identical across parts, so read the first once."""
        if dataset_id not in self._zones:
            columns = _BAR_COLUMNS if dataset_id == PANEL_DATASET_ID else _UNIVERSE_COLUMNS
            first = next(path for path in self._parts[dataset_id] if _has_columns(path, columns))
            dtype = pl.scan_parquet(str(first)).collect_schema()["available_at"]
            self._zones[dataset_id] = dtype.time_zone if isinstance(dtype, pl.Datetime) else None
        return self._zones[dataset_id]

    def resolve_security(self, stock_code: str, session: date, as_of: datetime) -> SecurityMatch:
        """Verify one historical ordinary-share identity against the local universe and market panel; return an ambiguity status instead of guessing."""
        if not stock_code:
            raise ValueError("stock code must be non-empty")
        _require_aware(as_of, "as_of")
        if UNIVERSE_DATASET_ID not in self._parts:
            return SecurityMatch("", stock_code, "", "", session, "MISSING_LOCAL")
        candidates = self._universe_rows(session, stock_code, as_of)
        if len(candidates) != 1:
            return SecurityMatch(
                "", stock_code, "", "", session, "AMBIGUOUS" if len(candidates) > 1 else "MISSING"
            )
        candidate = candidates[0]
        if candidate["share_kind"] != ORDINARY_SHARE_KIND:
            return SecurityMatch("", stock_code, "", "", session, "AMBIGUOUS")
        bars = self._panel_rows(str(candidate["instrument_id"]), session, session, as_of)
        if len(bars) != 1:
            return SecurityMatch(
                "", stock_code, "", "", session, "AMBIGUOUS" if len(bars) > 1 else "MISSING"
            )
        if bars[0]["market"] != candidate["market"]:
            return SecurityMatch("", stock_code, "", "", session, "AMBIGUOUS")
        return SecurityMatch(
            str(candidate["instrument_id"]),
            stock_code,
            str(candidate["market"]),
            str(candidate["source_security_id"]),
            session,
            "OK",
        )

    def market_bar(self, instrument_id: str, session: date, as_of: datetime) -> MarketBar | None:
        """Read one source-backed local bar only after its batch availability. Preserve raw OHLC, state, share factor and source hash; return None for unavailable or ambiguous rows."""
        if not instrument_id:
            raise ValueError("instrument id must be non-empty")
        _require_aware(as_of, "as_of")
        if PANEL_DATASET_ID not in self._parts:
            return None
        rows = self._panel_rows(instrument_id, session, session, as_of)
        if len(rows) != 1:
            return None
        return _to_bar(rows[0])

    def market_window(
        self, instrument_id: str, start: date, end: date, as_of: datetime
    ) -> tuple[MarketBar, ...]:
        """Read only eligible local bars in an inclusive session window, preserving gaps and source states."""
        if not instrument_id:
            raise ValueError("instrument id must be non-empty")
        _require_aware(as_of, "as_of")
        if start > end:
            raise ValueError("session window must not be empty")
        if PANEL_DATASET_ID not in self._parts:
            return ()
        grouped: dict[date, list[dict[str, Any]]] = {}
        for row in self._panel_rows(instrument_id, start, end, as_of):
            grouped.setdefault(row["session"], []).append(row)
        bars: list[MarketBar] = []
        for session in sorted(grouped):
            if len(grouped[session]) != 1:
                continue
            bar = _to_bar(grouped[session][0])
            if bar is None:
                continue
            bars.append(bar)
        return tuple(bars)

    def market_universe(
        self, start: date, end: date, as_of: datetime
    ) -> Mapping[str, tuple[MarketBar, ...]]:
        """Project local market columns for a bounded session window without loading the whole historical panel."""
        _require_aware(as_of, "as_of")
        if start > end:
            raise ValueError("session window must not be empty")
        if PANEL_DATASET_ID not in self._parts:
            return {}
        files = [
            str(path)
            for path in self._parts[PANEL_DATASET_ID]
            if _year_in_window(path, start, end) and _has_columns(path, _BAR_COLUMNS)
        ]
        if not files:
            return {}
        cutoff = _asof_literal(files, as_of)
        grouped: dict[tuple[str, date], list[dict[str, Any]]] = {}
        rows = (
            pl.scan_parquet(files)
            .filter(
                (pl.col("session") >= start)
                & (pl.col("session") <= end)
                & (pl.col("available_at") <= cutoff)
            )
            .select(list(_BAR_COLUMNS))
            .collect()
            .to_dicts()
        )
        for row in rows:
            grouped.setdefault((str(row["instrument_id"]), row["session"]), []).append(row)
        ordered: dict[str, list[MarketBar]] = {}
        for instrument_id, session in sorted(grouped):
            if len(grouped[(instrument_id, session)]) != 1:
                continue
            bar = _to_bar(grouped[(instrument_id, session)][0])
            if bar is None:
                continue
            ordered.setdefault(instrument_id, []).append(bar)
        return {instrument_id: tuple(bars) for instrument_id, bars in ordered.items()}
