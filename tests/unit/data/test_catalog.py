"""Invariant guards for the durable local catalog."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo

import pytest

from src.data.catalog import Catalog, FilingVersion

KST = ZoneInfo("Asia/Seoul")


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _open(tmp_path: Path) -> Catalog:
    return Catalog(tmp_path / "data" / "catalog.sqlite")


def _artifact_kwargs(path: str = "raw/dart/page-1.json") -> dict[str, object]:
    return {
        "source": "dart",
        "endpoint": "list",
        "request_key": "2024-window-p1",
        "snapshot_id": "snap-2024",
        "raw_bytes": b'{"page": 1}',
        "retrieved_at": datetime(2024, 6, 3, 18, 30, tzinfo=KST),
        "local_relative_path": PurePosixPath(path),
    }


def _filing(rcept_no: str, raw_hash: str, parent: str | None = None) -> FilingVersion:
    return FilingVersion(
        rcept_no=rcept_no,
        corp_code="00123456",
        receipt_date=date(2024, 5, 31),
        report_name="report",
        stock_code="005930",
        raw_hash=raw_hash,
        first_observed_at=datetime(2026, 2, 10, 15, 30, tzinfo=KST),
        knowledge_available_at=datetime(2024, 6, 3, 9, 0, tzinfo=KST),
        availability_mode="HISTORICAL_BACKFILL",
        correction_flag=parent is not None,
        withdrawal_flag=False,
        parent_rcept_no=parent,
        link_status="UNRESOLVED_LINK" if parent is not None else "ORIGINAL",
        time_precision="DATE_ONLY",
    )


def test_immutable_bytes_conflict_keeps_original(tmp_path: Path) -> None:
    """Different bytes at a registered path conflict without touching the original."""
    catalog = _open(tmp_path)
    kwargs = _artifact_kwargs()
    first = catalog.register_artifact(**kwargs)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="conflicting bytes"):
        catalog.register_artifact(**{**kwargs, "raw_bytes": b'{"page": 2}'})  # type: ignore[arg-type]
    found = catalog.find_artifact("dart", "list", "2024-window-p1", "snap-2024")
    assert found is not None
    assert found.sha256 == first
    assert (tmp_path / "data" / "raw/dart/page-1.json").read_bytes() == b'{"page": 1}'
    assert catalog.get_artifact_path(first) == PurePosixPath("raw/dart/page-1.json")
    assert catalog.get_artifact_path("0" * 64) is None
    assert catalog.find_artifact("dart", "list", "missing", "snap-2024") is None


def test_same_request_same_content_is_idempotent(tmp_path: Path) -> None:
    """Repeating one request snapshot with identical bytes returns the same hash."""
    catalog = _open(tmp_path)
    kwargs = _artifact_kwargs()
    first = catalog.register_artifact(**kwargs)  # type: ignore[arg-type]
    second = catalog.register_artifact(**kwargs)  # type: ignore[arg-type]
    assert first == second == _digest(b'{"page": 1}')
    reopened = Catalog(tmp_path / "data" / "catalog.sqlite")
    assert reopened.load_checkpoint("dart", "2024-window", "snap-2024") is None
    assert reopened.get_artifact_path(first) == PurePosixPath("raw/dart/page-1.json")
    with pytest.raises(ValueError, match="conflicting bytes"):
        reopened.register_artifact(
            **{**kwargs, "local_relative_path": PurePosixPath("raw/dart/other-path.json")},
        )  # type: ignore[arg-type]


def test_past_replay_keeps_original_available(tmp_path: Path) -> None:
    """Original stays accessible before a later correction becomes eligible."""
    catalog = _open(tmp_path)
    raw_original = catalog.register_artifact(**_artifact_kwargs("raw/dart/orig.zip"))  # type: ignore[arg-type]
    raw_fix = catalog.register_artifact(
        **{**_artifact_kwargs("raw/dart/fix.zip"), "request_key": "2024-window-fix", "raw_bytes": b"fix-bytes"},
    )  # type: ignore[arg-type]
    catalog.upsert_filing(_filing("20240531000001", raw_original))
    catalog.upsert_filing(
        replace(
            _filing("20240603000002", raw_fix),
            parent_rcept_no="20240531000001",
            knowledge_available_at=datetime(2024, 6, 5, 9, 0, tzinfo=KST),
        )
    )
    before_fix = datetime(2024, 6, 4, 9, 0, tzinfo=KST)
    assert catalog.get_filing_asof("20240531000001", before_fix) is not None
    assert catalog.get_filing_asof("20240603000002", before_fix) is None
    after_fix = datetime(2024, 6, 5, 9, 0, tzinfo=KST)
    assert catalog.get_filing_asof("20240603000002", after_fix) is not None
    assert catalog.get_filing_asof("20240603000002", after_fix).parent_rcept_no == "20240531000001"  # type: ignore[union-attr]
    assert catalog.get_filing_asof("missing", after_fix) is None
    catalog.upsert_filing(_filing("20240531000001", raw_original))
    with pytest.raises(ValueError, match="conflicting receipt"):
        catalog.upsert_filing(_filing("20240531000001", raw_fix))
    with pytest.raises(ValueError, match="timezone"):
        catalog.get_filing_asof("20240531000001", datetime(2024, 6, 4, 9, 0))


def test_atomic_checkpoint_rolls_back_failed_batch(tmp_path: Path) -> None:
    """A failed page batch leaves the cursor at the prior committed page."""
    catalog = _open(tmp_path)
    catalog.save_checkpoint("dart", "2024-window", "snap-2024", "page-1")
    assert catalog.load_checkpoint("dart", "2024-window", "snap-2024") == "page-1"
    assert catalog.load_checkpoint("dart", "2024-window", "missing") is None
    catalog.save_checkpoint("dart", "2024-window", "snap-2024", "page-1")

    def _failing_batch() -> None:
        with catalog.transaction():
            catalog.register_artifact(**_artifact_kwargs("raw/dart/page-2.json"))  # type: ignore[arg-type]
            catalog.save_checkpoint("dart", "2024-window", "snap-2024", "page-2")
            catalog.register_artifact(
                **{**_artifact_kwargs("raw/dart/page-2.json"), "raw_bytes": b"changed"},
            )  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="conflicting bytes"):
        _failing_batch()
    assert catalog.load_checkpoint("dart", "2024-window", "snap-2024") == "page-1"
    with catalog.transaction(), catalog.transaction():
        catalog.save_checkpoint("dart", "2024-window", "snap-2024", "page-2")
    assert catalog.load_checkpoint("dart", "2024-window", "snap-2024") == "page-2"


def test_local_path_only_rejects_external(tmp_path: Path) -> None:
    """Absolute external paths are rejected for artifacts and research runs."""
    catalog = _open(tmp_path)
    with pytest.raises(ValueError, match="unsafe local path"):
        catalog.register_artifact(**{**_artifact_kwargs(), "local_relative_path": PurePosixPath("/etc/passwd")})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unsafe local path"):
        catalog.register_artifact(**{**_artifact_kwargs(), "local_relative_path": PurePosixPath("../escape.json")})  # type: ignore[arg-type]
    payload = tmp_path / "data" / "runs" / "manifest.json"
    payload.parent.mkdir(parents=True, exist_ok=True)
    payload.write_bytes(b'{"run": 1}')
    manifest = _digest(b'{"run": 1}')
    catalog.register_research_run("run-1", manifest, "COMPLETE", PurePosixPath("runs/manifest.json"))
    catalog.register_research_run("run-1", manifest, "COMPLETE", PurePosixPath("runs/manifest.json"))
    with pytest.raises(ValueError, match="conflicting research run"):
        catalog.register_research_run("run-1", manifest, "FAILED", PurePosixPath("runs/manifest.json"))
    with pytest.raises(ValueError, match="unsafe local path"):
        catalog.register_research_run("run-2", manifest, "COMPLETE", PurePosixPath("/external/evil.json"))
    with pytest.raises(ValueError, match="missing or changed"):
        catalog.register_research_run("run-3", manifest, "COMPLETE", PurePosixPath("runs/absent.json"))
    with pytest.raises(ValueError, match="invalid manifest hash"):
        catalog.register_research_run("run-4", "not-a-hash", "COMPLETE", PurePosixPath("runs/manifest.json"))
    with pytest.raises(ValueError, match="non-empty"):
        catalog.register_research_run("", manifest, "COMPLETE", PurePosixPath("runs/manifest.json"))


def test_validation_guards(tmp_path: Path) -> None:
    """Empty identities, naive clocks, unknown hashes and schema drift fail closed."""
    catalog = _open(tmp_path)
    assert catalog.db_path.name == "catalog.sqlite"
    with pytest.raises(ValueError, match="non-empty"):
        catalog.register_artifact(**{**_artifact_kwargs(), "source": ""})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="timezone"):
        catalog.register_artifact(**{**_artifact_kwargs(), "retrieved_at": datetime(2024, 6, 3, 9, 0)})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="non-empty"):
        catalog.save_checkpoint("", "w", "s", "c")
    with pytest.raises(ValueError, match="missing registered artifact"):
        catalog.upsert_filing(_filing("20240531000009", "a" * 64))
    raw = catalog.register_artifact(**_artifact_kwargs("raw/dart/nine.zip"))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="non-empty"):
        catalog.upsert_filing(_filing("", raw))
    with pytest.raises(ValueError, match="link status"):
        catalog.upsert_filing(replace(_filing("20240531000010", raw), link_status=""))
    with pytest.raises(ValueError, match="invalid artifact hash"):
        catalog.upsert_filing(_filing("20240531000011", "not-a-hash"))
    naive = replace(_filing("20240531000012", raw), first_observed_at=datetime(2024, 6, 3, 9, 0))
    with pytest.raises(ValueError, match="timezone"):
        catalog.upsert_filing(naive)
    naive_knowledge = replace(
        _filing("20240531000013", raw), knowledge_available_at=datetime(2024, 6, 3, 9, 0)
    )
    with pytest.raises(ValueError, match="timezone"):
        catalog.upsert_filing(naive_knowledge)
    (tmp_path / "data" / "raw/dart/nine.zip").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="missing or changed"):
        catalog.upsert_filing(_filing("20240531000014", raw))
    import sqlite3

    conn = sqlite3.connect(str(tmp_path / "data" / "catalog.sqlite"))
    conn.execute("UPDATE schema_version SET version=999")
    conn.commit()
    conn.close()
    with pytest.raises(ValueError, match="unsupported catalog"):
        Catalog(tmp_path / "data" / "catalog.sqlite")


def test_destination_edge_cases(tmp_path: Path) -> None:
    """Symlinks, directories and vanished files at artifact paths fail closed."""
    catalog = _open(tmp_path)
    data_root = tmp_path / "data"
    target = data_root / "raw/dart/link.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    origin = data_root / "raw/dart/origin.json"
    origin.write_bytes(b"origin")
    if target.exists() or target.is_symlink():
        target.unlink()
    target.symlink_to(origin)
    with pytest.raises(ValueError, match="symlink"):
        catalog.register_artifact(**_artifact_kwargs("raw/dart/link.json"))  # type: ignore[arg-type]
    target.unlink()
    directory = tmp_path / "data" / "raw/dart/taken.json"
    directory.mkdir(parents=True, exist_ok=True)
    with pytest.raises(ValueError, match="conflicting bytes"):
        catalog.register_artifact(**_artifact_kwargs("raw/dart/taken.json"))  # type: ignore[arg-type]
    file_path = tmp_path / "data" / "raw/dart/vanish.json"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_bytes(b"vanish")
    catalog.register_artifact(
        **{**_artifact_kwargs("raw/dart/vanish.json"), "raw_bytes": b"vanish"},
    )  # type: ignore[arg-type]
    file_path.unlink()
    with pytest.raises(ValueError, match="missing or changed"):
        catalog.register_artifact(
            **{**_artifact_kwargs("raw/dart/vanish.json"), "raw_bytes": b"vanish"},
        )  # type: ignore[arg-type]
    clash_path = tmp_path / "data" / "raw/dart/clash.json"
    clash_path.write_bytes(b"other-content")
    with pytest.raises(ValueError, match="conflicting bytes"):
        catalog.register_artifact(
            **{**_artifact_kwargs("raw/dart/clash.json"), "request_key": "clash-key"},
        )  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="conflicting bytes"):
        catalog.register_artifact(
            **{**_artifact_kwargs("raw/dart/vanish.json"), "request_key": "other-key", "raw_bytes": b"other"},
        )  # type: ignore[arg-type]


def test_research_run_lookup_returns_record(tmp_path: Path) -> None:
    """Registered run records round-trip through the read-only lookup."""
    catalog = _open(tmp_path)
    assert catalog.get_research_run("missing") is None
    payload = tmp_path / "data" / "runs" / "manifest.json"
    payload.parent.mkdir(parents=True, exist_ok=True)
    payload.write_bytes(b'{"run": 1}')
    manifest = _digest(b'{"run": 1}')
    catalog.register_research_run("run-1", manifest, "COMPLETE", PurePosixPath("runs/manifest.json"))
    record = catalog.get_research_run("run-1")
    assert record is not None
    assert record.run_id == "run-1"
    assert record.manifest_hash == manifest
    assert record.status == "COMPLETE"
    assert record.local_path == PurePosixPath("runs/manifest.json")
