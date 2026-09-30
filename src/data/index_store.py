"""Pinned cumulative manifests and validated on-demand reads of KRX index bars."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Literal

from src.data.catalog import Catalog
from src.data.local_paths import checked_data_path, checked_local_path
from src.integrations.krx_index import IndexBar, parse_index_day

_MANIFEST_DIRNAME = "krx/manifests"
_MANIFEST_VERSION = 1
_MARKETS = ("KOSPI", "KOSDAQ")
_HEXDIGITS = frozenset("0123456789abcdefABCDEF")


@dataclass(frozen=True, slots=True)
class IndexManifest:
    """Approved local raw index hashes keyed by market and session."""

    entries: Mapping[tuple[str, date], str]
    manifest_hash: str


def _is_hex64(value: str) -> bool:
    return len(value) == 64 and all(char in _HEXDIGITS for char in value)


def _manifest_bytes(entries: Mapping[tuple[str, date], str]) -> bytes:
    ordered = sorted(entries.items(), key=lambda item: (item[0][0], item[0][1].isoformat()))
    payload = {
        "version": _MANIFEST_VERSION,
        "entries": [
            {"market": market, "session": session.isoformat(), "sha256": digest}
            for (market, session), digest in ordered
        ],
    }
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _manifest_path(data_root: Path, manifest_hash: str) -> Path:
    return checked_local_path(data_root, PurePosixPath(_MANIFEST_DIRNAME) / f"{manifest_hash.lower()}.json")


def _checked_entry(market: str, session: date, sha256: str) -> tuple[tuple[str, date], str]:
    if market not in _MARKETS:
        raise ValueError(f"unsupported index market: {market!r}")
    if not isinstance(session, date) or isinstance(session, datetime):
        raise ValueError(f"invalid index session: {session!r}")
    if not _is_hex64(sha256):
        raise ValueError(f"invalid index raw hash for {(market, session.isoformat())!r}")
    return ((market, session), sha256.lower())


def merge_index_manifest(
    previous: IndexManifest | None,
    accepted: Sequence[tuple[str, date, str]],
    data_root: Path,
    replace_existing: bool = False,
) -> IndexManifest:
    """Persist a new immutable cumulative index manifest from previously approved days and validated new artifact hashes. Preserve earlier manifest bytes; require explicit replacement policy for changed same-day data."""
    combined: dict[tuple[str, date], str] = dict(previous.entries) if previous is not None else {}
    for market, session, sha256 in accepted:
        key, digest = _checked_entry(market, session, sha256)
        if key in combined and combined[key] != digest and not replace_existing:
            raise ValueError(f"conflicting index raw for {key[0]} {key[1].isoformat()}")
        combined[key] = digest
    raw = _manifest_bytes(combined)
    manifest = IndexManifest(entries=combined, manifest_hash=hashlib.sha256(raw).hexdigest())
    target = _manifest_path(data_root, manifest.manifest_hash)
    if target.is_file():
        if hashlib.sha256(target.read_bytes()).hexdigest() != manifest.manifest_hash:
            raise ValueError(f"conflicting index manifest file: {target}")
        return manifest
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = checked_data_path(data_root, target.with_name(target.name + ".partial"))
    partial.write_bytes(raw)
    os.replace(partial, target)
    return manifest


def load_index_manifest(data_root: Path, manifest_hash: str) -> IndexManifest:
    """Load and hash-verify one pinned project-local index manifest without falling back to current or remote data."""
    if not _is_hex64(manifest_hash):
        raise ValueError("invalid index manifest hash")
    target = _manifest_path(data_root, manifest_hash)
    if target.is_symlink() or not target.is_file():
        raise ValueError(f"missing index manifest: {manifest_hash.lower()}")
    raw = target.read_bytes()
    if hashlib.sha256(raw).hexdigest() != manifest_hash.lower():
        raise ValueError(f"hash mismatch for index manifest: {manifest_hash.lower()}")
    try:
        document = json.loads(raw.decode("utf-8"))
        items = document["entries"]
        version = document.get("version", _MANIFEST_VERSION)
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ValueError(f"invalid index manifest: {manifest_hash.lower()}") from exc
    if version != _MANIFEST_VERSION or not isinstance(items, list):
        raise ValueError(f"invalid index manifest: {manifest_hash.lower()}")
    entries: dict[tuple[str, date], str] = {}
    try:
        for item in items:
            key, digest = _checked_entry(str(item["market"]), date.fromisoformat(str(item["session"])), str(item["sha256"]))
            if key in entries:
                raise ValueError("duplicate index manifest entry")
            entries[key] = digest
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"invalid index manifest: {manifest_hash.lower()}") from exc
    return IndexManifest(entries=entries, manifest_hash=manifest_hash.lower())


class IndexStore:
    """Validated on-demand reads of official index bars pinned by one local manifest."""

    def __init__(self, catalog: Catalog, data_root: Path, manifest: IndexManifest) -> None:
        if data_root.is_symlink() or not data_root.is_dir():
            raise ValueError(f"missing project data root: {data_root}")
        self._catalog = catalog
        self._data_root = data_root
        self._manifest = manifest
        self._bars: dict[tuple[str, date, str], IndexBar] = {}

    @property
    def manifest(self) -> IndexManifest:
        """Return the pinned cumulative index manifest."""
        return self._manifest

    def _read_bar(self, market: Literal["KOSPI", "KOSDAQ"], session: date, sha256: str) -> IndexBar | None:
        """Read and hash-verify one pinned bar once; only successes are cached so a repaired file is picked up."""
        key = (market, session, sha256)
        bar = self._bars.get(key)
        if bar is None:
            bar = self._load_bar(market, session, sha256)
            if bar is not None:
                self._bars[key] = bar
        return bar

    def _load_bar(self, market: Literal["KOSPI", "KOSDAQ"], session: date, sha256: str) -> IndexBar | None:
        relative = self._catalog.get_artifact_path(sha256)
        if relative is None:
            return None
        target = checked_local_path(self._data_root, relative)
        try:
            raw = target.read_bytes()
        except OSError:
            return None
        if hashlib.sha256(raw).hexdigest() != sha256.lower():
            return None
        try:
            return parse_index_day(raw, market, session, sha256)
        except ValueError:
            return None

    def window(
        self, market: Literal["KOSPI", "KOSDAQ"], start: date, end: date, as_of: datetime
    ) -> tuple[IndexBar, ...]:
        """Return only locally cached official index sessions whose conservative batch availability is no later than as_of. Missing or invalid sessions stay absent for downstream explicit refusal."""
        if market not in _MARKETS:
            raise ValueError("market must be KOSPI or KOSDAQ")
        if start > end:
            raise ValueError("index window must not be empty")
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as-of instant must be timezone-aware")
        bars: list[IndexBar] = []
        session = start
        while session <= end:
            digest = self._manifest.entries.get((market, session))
            if digest is not None:
                bar = self._read_bar(market, session, digest)
                if bar is not None and bar.batch_available_at <= as_of:
                    bars.append(bar)
            session += timedelta(days=1)
        return tuple(bars)


__all__ = ["IndexBar", "IndexManifest", "IndexStore", "load_index_manifest", "merge_index_manifest"]
