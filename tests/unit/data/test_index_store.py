"""Invariant guards for pinned cumulative index manifests and as-of replay."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo

import pytest

from src.data.catalog import Catalog
from src.data.index_store import IndexStore, load_index_manifest, merge_index_manifest

KST = ZoneInfo("Asia/Seoul")
SESSION = date(2024, 6, 27)
NEXT = date(2024, 6, 28)
RETRIEVED_2026 = datetime(2026, 9, 24, 12, 0, tzinfo=KST)


def _payload(headline: str, market: str, day: date) -> bytes:
    return json.dumps(
        {
            "OutBlock_1": [
                {
                    "BAS_DD": day.strftime("%Y%m%d"),
                    "IDX_CLSS": market,
                    "IDX_NM": f"{headline} (외국주포함)",
                    "OPNPRC_IDX": "",
                    "CLSPRC_IDX": "",
                },
                {
                    "BAS_DD": day.strftime("%Y%m%d"),
                    "IDX_CLSS": market,
                    "IDX_NM": headline,
                    "OPNPRC_IDX": "2767.62",
                    "CLSPRC_IDX": "2784.06",
                },
            ]
        }
    ).encode("utf-8")


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _registered(
    catalog: Catalog, market: str, day: date, headline: str, retrieved_at: datetime = RETRIEVED_2026
) -> str:
    raw = _payload(headline, market, day)
    return catalog.register_artifact(
        source="krx",
        endpoint="index",
        request_key=f"{market}:{day:%Y%m%d}",
        snapshot_id="snap-1",
        raw_bytes=raw,
        retrieved_at=retrieved_at,
        local_relative_path=PurePosixPath(f"raw/krx/snap-1/{market}-{day:%Y%m%d}.json"),
    )


def _open(tmp_path: Path) -> tuple[Catalog, Path]:
    root = tmp_path / "data"
    return Catalog(root / "catalog.sqlite"), root


def test_historical_replay_uses_policy_not_retrieval_time(tmp_path: Path) -> None:
    """A 2024 bar collected in 2026 is eligible after the 18:00 policy boundary."""
    catalog, root = _open(tmp_path)
    digest = _registered(catalog, "KOSPI", SESSION, "코스피")
    manifest = merge_index_manifest(None, [("KOSPI", SESSION, digest)], root)
    store = IndexStore(catalog, root, manifest)
    assert store.manifest.manifest_hash == manifest.manifest_hash

    eligible = datetime(2024, 6, 27, 18, 0, tzinfo=KST)
    bars = store.window("KOSPI", SESSION, SESSION, eligible)
    assert len(bars) == 1
    assert bars[0].batch_available_at == eligible
    assert bars[0].source_hash == digest

    assert store.window("KOSPI", SESSION, SESSION, datetime(2024, 6, 27, 17, 59, tzinfo=KST)) == ()
    assert store.window("KOSDAQ", SESSION, SESSION, eligible) == ()
    assert store.window("KOSPI", NEXT, NEXT, eligible) == ()

    artifact = catalog.find_artifact("krx", "index", "KOSPI:20240627", "snap-1")
    assert artifact is not None
    assert artifact.retrieved_at.year == 2026


def test_raw_mutation_returns_no_bar(tmp_path: Path) -> None:
    """Changed cached bytes never produce an index bar."""
    catalog, root = _open(tmp_path)
    digest = _registered(catalog, "KOSPI", SESSION, "코스피")
    manifest = merge_index_manifest(None, [("KOSPI", SESSION, digest)], root)
    store = IndexStore(catalog, root, manifest)
    (root / "raw/krx/snap-1/KOSPI-20240627.json").write_bytes(b'{"OutBlock_1": []}')
    assert store.window("KOSPI", SESSION, SESSION, datetime(2024, 6, 28, 9, 0, tzinfo=KST)) == ()


def test_unreadable_sessions_stay_absent(tmp_path: Path) -> None:
    """Unregistered, vanished or unparsable sessions never surface as bars."""
    catalog, root = _open(tmp_path)
    as_of = datetime(2024, 6, 28, 9, 0, tzinfo=KST)
    ghost = merge_index_manifest(None, [("KOSPI", SESSION, "d" * 64)], root)
    assert IndexStore(catalog, root, ghost).window("KOSPI", SESSION, SESSION, as_of) == ()

    digest = _registered(catalog, "KOSPI", SESSION, "코스피")
    vanished = merge_index_manifest(None, [("KOSPI", SESSION, digest)], root)
    (root / "raw/krx/snap-1/KOSPI-20240627.json").unlink()
    assert IndexStore(catalog, root, vanished).window("KOSPI", SESSION, SESSION, as_of) == ()

    catalog.register_artifact(
        source="krx",
        endpoint="index",
        request_key="KOSPI:20240628",
        snapshot_id="snap-1",
        raw_bytes=b'{"OutBlock_1": []}',
        retrieved_at=RETRIEVED_2026,
        local_relative_path=PurePosixPath("raw/krx/snap-1/KOSPI-20240628.json"),
    )
    empty_hash = hashlib.sha256(b'{"OutBlock_1": []}').hexdigest()
    empty = merge_index_manifest(None, [("KOSPI", NEXT, empty_hash)], root)
    assert IndexStore(catalog, root, empty).window("KOSPI", NEXT, NEXT, as_of) == ()


def test_cumulative_days_preserve_old_manifest(tmp_path: Path) -> None:
    """A new day extends the manifest to 121 hashes without touching prior bytes."""
    catalog, root = _open(tmp_path)
    accepted: list[tuple[str, date, str]] = []
    first = date(2024, 1, 2)
    for offset in range(120):
        day = first + timedelta(days=offset)
        accepted.append(("KOSPI", day, "a" * 63 + f"{offset % 10:x}"))
    old = merge_index_manifest(None, accepted, root)
    assert len(old.entries) == 120
    old_path = root / "krx" / "manifests" / f"{old.manifest_hash}.json"
    old_bytes = old_path.read_bytes()

    extra_day = first + timedelta(days=120)
    new = merge_index_manifest(old, [("KOSPI", extra_day, "b" * 64)], root)
    assert len(new.entries) == 121
    assert new.entries[("KOSPI", extra_day)] == "b" * 64
    assert new.manifest_hash != old.manifest_hash
    assert old_path.read_bytes() == old_bytes
    assert load_index_manifest(root, old.manifest_hash).entries == old.entries
    assert load_index_manifest(root, new.manifest_hash).entries == new.entries
    assert merge_index_manifest(old, [], root).manifest_hash == old.manifest_hash


def test_revised_day_requires_explicit_replacement(tmp_path: Path) -> None:
    """Changed same-day bytes conflict by default and fork only with replacement policy."""
    catalog, root = _open(tmp_path)
    first = merge_index_manifest(None, [("KOSPI", SESSION, "a" * 64)], root)
    with pytest.raises(ValueError, match="conflicting index raw"):
        merge_index_manifest(first, [("KOSPI", SESSION, "b" * 64)], root)
    forked = merge_index_manifest(first, [("KOSPI", SESSION, "b" * 64)], root, replace_existing=True)
    assert forked.entries[("KOSPI", SESSION)] == "b" * 64
    assert forked.manifest_hash != first.manifest_hash
    assert load_index_manifest(root, first.manifest_hash).entries == first.entries
    assert load_index_manifest(root, forked.manifest_hash).entries == forked.entries


def test_manifest_and_window_guards(tmp_path: Path) -> None:
    """Bad hashes, markets, clocks and windows fail closed without remote fallback."""
    catalog, root = _open(tmp_path)
    with pytest.raises(ValueError, match="manifest hash"):
        load_index_manifest(root, "not-a-hash")
    with pytest.raises(ValueError, match="missing index manifest"):
        load_index_manifest(root, "a" * 64)
    manifest = merge_index_manifest(None, [], root)
    assert manifest.entries == {}
    store = IndexStore(catalog, root, manifest)
    with pytest.raises(ValueError, match="market"):
        store.window("NYSE", SESSION, SESSION, datetime(2024, 6, 28, 9, 0, tzinfo=KST))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="window"):
        store.window("KOSPI", NEXT, SESSION, datetime(2024, 6, 28, 9, 0, tzinfo=KST))
    with pytest.raises(ValueError, match="timezone"):
        store.window("KOSPI", SESSION, SESSION, datetime(2024, 6, 28, 9, 0))
    with pytest.raises(ValueError, match="data root"):
        IndexStore(catalog, root / "absent", manifest)
    with pytest.raises(ValueError, match="market"):
        merge_index_manifest(None, [("NYSE", SESSION, "a" * 64)], root)
    with pytest.raises(ValueError, match="raw hash"):
        merge_index_manifest(None, [("KOSPI", SESSION, "xyz")], root)
    with pytest.raises(ValueError, match="session"):
        merge_index_manifest(None, [("KOSPI", "2024-06-27", "a" * 64)], root)  # type: ignore[list-item]


def _self_named_manifest(root: Path, document: object) -> str:
    raw = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    target = root / "krx" / "manifests" / f"{digest}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)
    return digest


def test_corrupt_manifest_files_fail_closed(tmp_path: Path) -> None:
    """Tampered, malformed or structurally invalid manifest files never load."""
    catalog, root = _open(tmp_path)
    digest = _registered(catalog, "KOSPI", SESSION, "코스피")
    manifest = merge_index_manifest(None, [("KOSPI", SESSION, digest)], root)
    target = root / "krx" / "manifests" / f"{manifest.manifest_hash}.json"
    target.write_bytes(b'{"OutBlock_1": []}')
    with pytest.raises(ValueError, match="hash mismatch"):
        load_index_manifest(root, manifest.manifest_hash)

    bad_json = _self_named_manifest(root, "not-a-mapping")
    with pytest.raises(ValueError, match="invalid index manifest"):
        load_index_manifest(root, bad_json)
    bad_version = _self_named_manifest(root, {"version": 999, "entries": []})
    with pytest.raises(ValueError, match="invalid index manifest"):
        load_index_manifest(root, bad_version)
    nach = _self_named_manifest(
        root,
        {
            "version": 1,
            "entries": [
                {"market": "KOSPI", "session": SESSION.isoformat(), "sha256": "a" * 64},
                {"market": "KOSPI", "session": SESSION.isoformat(), "sha256": "a" * 64},
            ],
        },
    )
    with pytest.raises(ValueError, match="invalid index manifest"):
        load_index_manifest(root, nach)
    bad_entry = _self_named_manifest(
        root,
        {"version": 1, "entries": [{"market": "NYSE", "session": SESSION.isoformat(), "sha256": "a" * 64}]},
    )
    with pytest.raises(ValueError, match="invalid index manifest"):
        load_index_manifest(root, bad_entry)

    manifest_b = merge_index_manifest(None, [("KOSPI", SESSION, "b" * 64)], root)
    other = root / "krx" / "manifests" / f"{manifest_b.manifest_hash}.json"
    other.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="conflicting index manifest file"):
        merge_index_manifest(None, [("KOSPI", SESSION, "b" * 64)], root)

    link_dir = root / "krx" / "manifests"
    probe = merge_index_manifest(None, [("KOSPI", SESSION, "c" * 64)], tmp_path / "probe-data")
    link_target = link_dir / f"{probe.manifest_hash}.json"
    link_dir.mkdir(parents=True, exist_ok=True)
    origin = link_dir / "origin.json"
    origin.write_bytes(b"origin")
    if link_target.exists() or link_target.is_symlink():
        link_target.unlink()
    link_target.symlink_to(origin)
    with pytest.raises(ValueError, match="symlink"):
        merge_index_manifest(None, [("KOSPI", SESSION, "c" * 64)], root)
