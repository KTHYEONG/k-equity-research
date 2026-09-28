"""Invariant guards for session-bounded KRX index collection."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path, PurePosixPath

import polars as pl
import pytest

from src.data.catalog import Catalog
from src.data.imports import ImportManifest, ImportPart
from src.data.index_store import IndexManifest, merge_index_manifest
from src.data.krx_ingest import collect_index_sessions
from src.data.local_lake import PANEL_DATASET_ID, LocalLake
from src.integrations.krx_index import KrxSourceError

S1 = date(2024, 6, 24)
S2 = date(2024, 6, 25)
S3 = date(2024, 6, 26)
S4 = date(2024, 6, 27)
OUTSIDE = date(2024, 6, 20)
HEADLINES = {"KOSPI": "코스피", "KOSDAQ": "코스닥"}


def _sha_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _payload(market: str, day: date, close: str = "2784.06") -> bytes:
    return json.dumps(
        {
            "OutBlock_1": [
                {
                    "BAS_DD": day.strftime("%Y%m%d"),
                    "IDX_CLSS": market,
                    "IDX_NM": f"{HEADLINES[market]} (외국주포함)",
                    "OPNPRC_IDX": "",
                    "CLSPRC_IDX": "",
                },
                {
                    "BAS_DD": day.strftime("%Y%m%d"),
                    "IDX_CLSS": market,
                    "IDX_NM": HEADLINES[market],
                    "OPNPRC_IDX": "2767.62",
                    "CLSPRC_IDX": close,
                },
            ]
        }
    ).encode("utf-8")


class _FakeClient:
    def __init__(
        self,
        missing: set[date] | None = None,
        failing: set[date] | None = None,
        invalid: set[date] | None = None,
        shifted: set[date] | None = None,
        alt: set[date] | None = None,
    ) -> None:
        self._missing = missing or set()
        self._failing = failing or set()
        self._invalid = invalid or set()
        self._shifted = shifted or set()
        self._alt = alt or set()
        self.calls: list[tuple[str, date]] = []

    def fetch_day(self, market: str, session: date) -> bytes:
        self.calls.append((market, session))
        if session in self._missing:
            raise KrxSourceError("NO_DATA", False, "krx index day unavailable")
        if session in self._failing:
            raise KrxSourceError("TRANSPORT", True, "krx index transport failure")
        if session in self._invalid:
            return b'{"OutBlock_1": []}'
        if session in self._shifted:
            return _payload(market, session + timedelta(days=1))
        if session in self._alt:
            return _payload(market, session, close="1000.00")
        assert market in HEADLINES
        return _payload(market, session)


def _lake(data_root: Path, sessions: list[date]) -> LocalLake:
    target = data_root / "imports" / PANEL_DATASET_ID / "year=2024/part.parquet"
    target.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(
        {"session": sessions, "instrument_id": ["KRX:005930"] * len(sessions)},
    ).write_parquet(target)
    manifest = ImportManifest(
        PANEL_DATASET_ID,
        "0" * 64,
        datetime.now(UTC),
        (ImportPart(PurePosixPath("year=2024/part.parquet"), _sha_of(target), target.stat().st_size),),
    )
    return LocalLake(data_root, {PANEL_DATASET_ID: manifest})


def _write_lake_manifests(data_root: Path, sessions: list[date]) -> None:
    target = data_root / "imports" / PANEL_DATASET_ID / "year=2024/part.parquet"
    target.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(
        {"session": sessions, "instrument_id": ["KRX:005930"] * len(sessions)},
    ).write_parquet(target)
    payload = {
        "dataset_id": PANEL_DATASET_ID,
        "source_manifest_sha256": "0" * 64,
        "imported_at": datetime.now(UTC).isoformat(),
        "parts": [
            {"path": "year=2024/part.parquet", "sha256": _sha_of(target), "bytes": target.stat().st_size},
        ],
    }
    (data_root / "imports" / PANEL_DATASET_ID / "manifest.json").write_text(
        json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )


def _previous(
    data_root: Path, catalog: Catalog, keys: list[tuple[str, date]], snapshot: str = "seed"
) -> IndexManifest:
    accepted = []
    for market, session in keys:
        raw = _payload(market, session)
        digest = catalog.register_artifact(
            source="krx",
            endpoint="index",
            request_key=f"{market}:{session:%Y%m%d}",
            snapshot_id=snapshot,
            raw_bytes=raw,
            retrieved_at=datetime(2024, 6, 28, 12, 0, tzinfo=UTC),
            local_relative_path=PurePosixPath(f"raw/krx/{snapshot}/{market}-{session:%Y%m%d}.json"),
        )
        accepted.append((market, session, digest))
    return merge_index_manifest(None, accepted, data_root)


def _catalog(tmp_path: Path) -> tuple[Catalog, Path]:
    root = tmp_path / "data"
    return Catalog(root / "catalog.sqlite"), root


def _manifest_files(root: Path) -> list[Path]:
    manifest_dir = root / "krx" / "manifests"
    if not manifest_dir.is_dir():
        return []
    return sorted(manifest_dir.glob("*.json"))


def test_pilot_reuse_only_missing_keys_trigger_requests(tmp_path: Path) -> None:
    """Verified prior bars are reused; only missing market/session keys are fetched and the manifest stays cumulative."""
    catalog, root = _catalog(tmp_path)
    lake = _lake(root, [S1, S2, S3, S4])
    previous = _previous(root, catalog, [("KOSPI", S1), ("KOSDAQ", S1), ("KOSPI", OUTSIDE)])
    client = _FakeClient()
    summary = collect_index_sessions(client, catalog, lake, previous, S1, S4, root, "snap-1")
    assert summary.snapshot_id == "snap-1"
    assert summary.expected_keys == 8
    assert summary.reused_keys == 2
    assert summary.fetched_keys == 6
    assert summary.unresolved_keys == ()
    assert len(client.calls) == 6
    assert ("KOSPI", S1) not in client.calls
    assert ("KOSDAQ", S1) not in client.calls
    assert len(summary.manifest.entries) == 9
    assert summary.manifest.entries[("KOSPI", OUTSIDE)] == previous.entries[("KOSPI", OUTSIDE)]
    assert summary.manifest.entries[("KOSPI", S1)] == previous.entries[("KOSPI", S1)]
    assert summary.manifest_hash == summary.manifest.manifest_hash
    assert (root / "krx" / "manifests" / f"{summary.manifest_hash}.json").is_file()


def test_holiday_exclusion_no_requests_for_non_sessions(tmp_path: Path) -> None:
    """Calendar days without a retained market session never trigger a provider call."""
    catalog, root = _catalog(tmp_path)
    lake = _lake(root, [S1, S3])
    client = _FakeClient()
    summary = collect_index_sessions(client, catalog, lake, None, S1, S3, root, "snap-1")
    assert summary.expected_keys == 4
    assert summary.fetched_keys == 4
    assert {session for _, session in client.calls} == {S1, S3}
    assert len(summary.manifest.entries) == 4


def test_provider_gap_fails_without_manifest(tmp_path: Path) -> None:
    """A NO_DATA session fails the run with that key unresolved and no cumulative manifest published."""
    catalog, root = _catalog(tmp_path)
    lake = _lake(root, [S1, S2])
    client = _FakeClient(missing={S2})
    with pytest.raises(ValueError, match="unresolved index keys: KOSDAQ:2024-06-25, KOSPI:2024-06-25"):
        collect_index_sessions(client, catalog, lake, None, S1, S2, root, "snap-1")
    assert _manifest_files(root) == []


def test_conflicting_duplicate_fails_without_replacement(tmp_path: Path) -> None:
    """A refetched payload that disagrees with an accepted bar fails instead of replacing it."""
    catalog, root = _catalog(tmp_path)
    lake = _lake(root, [S1])
    before = _previous(root, catalog, [("KOSPI", S1)])
    (root / "raw" / "krx" / "seed" / "KOSPI-20240624.json").unlink()
    published = _manifest_files(root)
    client = _FakeClient(alt={S1})
    with pytest.raises(ValueError, match="conflicting index raw"):
        collect_index_sessions(client, catalog, lake, before, S1, S1, root, "snap-1")
    assert _manifest_files(root) == published


def test_interrupted_resume_reuses_completed_keys(tmp_path: Path) -> None:
    """Keys registered by a partial previous attempt are reused; only unresolved keys are requested."""
    catalog, root = _catalog(tmp_path)
    lake = _lake(root, [S1, S2])
    raw = _payload("KOSPI", S1)
    catalog.register_artifact(
        source="krx",
        endpoint="index",
        request_key=f"KOSPI:{S1:%Y%m%d}",
        snapshot_id="snap-1",
        raw_bytes=raw,
        retrieved_at=datetime(2024, 6, 28, 12, 0, tzinfo=UTC),
        local_relative_path=PurePosixPath(f"raw/krx/snap-1/KOSPI-{S1:%Y%m%d}.json"),
    )
    client = _FakeClient()
    summary = collect_index_sessions(client, catalog, lake, None, S1, S2, root, "snap-1")
    assert summary.expected_keys == 4
    assert summary.reused_keys == 1
    assert summary.fetched_keys == 3
    assert ("KOSPI", S1) not in client.calls
    assert len(summary.manifest.entries) == 4


def test_tampered_prior_payload_is_refetched(tmp_path: Path) -> None:
    """A prior bar whose registered bytes no longer match its hash is refetched, never trusted."""
    catalog, root = _catalog(tmp_path)
    lake = _lake(root, [S1])
    previous = _previous(root, catalog, [("KOSPI", S1)])
    (root / "raw" / "krx" / "seed" / f"KOSPI-{S1:%Y%m%d}.json").write_bytes(_payload("KOSPI", S1, close="1000.00"))
    client = _FakeClient()
    summary = collect_index_sessions(client, catalog, lake, previous, S1, S1, root, "snap-1")
    assert ("KOSPI", S1) in client.calls
    assert summary.reused_keys == 0
    assert summary.fetched_keys == 2
    assert summary.manifest.entries[("KOSPI", S1)] == previous.entries[("KOSPI", S1)]


def test_collection_contract_guards(tmp_path: Path) -> None:
    """Unsafe snapshots and inverted bounds fail before any remote read."""
    catalog, root = _catalog(tmp_path)
    lake = _lake(root, [S1])
    client = _FakeClient()
    with pytest.raises(ValueError, match="snapshot"):
        collect_index_sessions(client, catalog, lake, None, S1, S1, root, "../evil")
    with pytest.raises(ValueError, match="empty"):
        collect_index_sessions(client, catalog, lake, None, S2, S1, root, "snap-1")
    assert client.calls == []


def test_malformed_and_mismatched_payloads_stay_unresolved(tmp_path: Path) -> None:
    """Unparseable bars and wrong-session payloads fail the run without publishing a manifest."""
    catalog, root = _catalog(tmp_path)
    lake = _lake(root, [S1])
    with pytest.raises(ValueError, match="unresolved index keys"):
        collect_index_sessions(_FakeClient(invalid={S1}), catalog, lake, None, S1, S1, root, "snap-a")
    assert _manifest_files(root) == []
    with pytest.raises(ValueError, match="KOSPI:2024-06-24"):
        collect_index_sessions(_FakeClient(shifted={S1}), catalog, lake, None, S1, S1, root, "snap-b")
    assert _manifest_files(root) == []


def test_failed_catalog_registration_stays_unresolved(tmp_path: Path) -> None:
    """A local path clash blocks registration, leaving the key unresolved and the manifest unpublished."""
    catalog, root = _catalog(tmp_path)
    lake = _lake(root, [S1])
    target = root / "raw" / "krx" / "snap-1" / f"KOSPI-{S1:%Y%m%d}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"stray bytes")
    with pytest.raises(ValueError, match="unresolved index keys"):
        collect_index_sessions(_FakeClient(), catalog, lake, None, S1, S1, root, "snap-1")
    assert _manifest_files(root) == []


def test_backfill_index_command_reports_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The backfill-index command prints runtime expected, reused, fetched, and unresolved counts."""
    from src.cli.main import main

    data_root = tmp_path / "data"
    _write_lake_manifests(data_root, [S1, S2])
    monkeypatch.setenv("KRX_OPENAPI_KEY", "test-key")

    class _CLIClient:
        def __init__(self, api_key: str, http_client: object) -> None:
            assert api_key == "test-key"

        def fetch_day(self, market: str, session: date) -> bytes:
            return _payload(market, session)

    monkeypatch.setattr("src.integrations.krx_index.KrxIndexClient", _CLIClient)
    assert (
        main(
            [
                "data",
                "backfill-index",
                "--start",
                S1.isoformat(),
                "--end",
                S2.isoformat(),
                "--data-root",
                str(data_root),
            ]
        )
        == 0
    )
    document = json.loads(capsys.readouterr().out.strip())
    assert document["expected_keys"] == 4
    assert document["reused_keys"] == 0
    assert document["fetched_keys"] == 4
    assert document["unresolved_keys"] == []
    manifest_path = Path(document["manifest"])
    assert manifest_path.is_file()
    assert len(json.loads(manifest_path.read_text(encoding="utf-8"))["entries"]) == 4


def test_backfill_index_requires_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Backfill without provider credentials fails closed before any lake or catalog work."""
    from src.cli.main import main

    data_root = tmp_path / "data"
    data_root.mkdir(parents=True)
    monkeypatch.delenv("KRX_OPENAPI_KEY", raising=False)
    monkeypatch.delenv("KRX_API_KEY", raising=False)
    assert main(["data", "backfill-index", "--start", S1.isoformat(), "--end", S2.isoformat(), "--data-root", str(data_root)]) == 2
    assert "KRX API key" in capsys.readouterr().err
