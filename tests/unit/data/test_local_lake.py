"""Invariant guards for project-local point-in-time market reads."""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo

import polars as pl
import pytest

from src.data.imports import ImportManifest, ImportPart
from src.data.local_lake import PANEL_DATASET_ID, UNIVERSE_DATASET_ID, LocalLake

KST = ZoneInfo("Asia/Seoul")
MON = date(2024, 6, 24)
TUE = date(2024, 6, 25)
WED = date(2024, 6, 26)
THU = date(2024, 6, 27)
FRI = date(2024, 6, 28)
BATCH = datetime(2024, 6, 25, 18, 0, tzinfo=KST)


def _sha_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _register(data_root: Path, dataset_id: str, frames: dict[str, pl.DataFrame]) -> ImportManifest:
    parts = []
    for relative, frame in frames.items():
        target = data_root / "imports" / dataset_id / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        frame.write_parquet(target)
        parts.append(
            ImportPart(
                relative_path=PurePosixPath(relative),
                sha256=_sha_of(target),
                byte_length=target.stat().st_size,
            )
        )
    return ImportManifest(
        dataset_id=dataset_id,
        source_manifest_sha256="0" * 64,
        imported_at=datetime.now(UTC),
        parts=tuple(parts),
    )


def _panel_rows() -> list[dict[str, object]]:
    return [
        {
            "instrument_id": "KRX:005930",
            "session": day,
            "market": "KOSPI",
            "ticker": "005930",
            "open": 70000,
            "close": 71000,
            "market_cap": 420000000000000,
            "listed_shares": 5900000000,
            "trading_value": 300000000000,
            "ret_price": 0.01,
            "price_state": "tradable",
            "gap_before": False,
            "share_factor": 1.0,
            "available_at": datetime(day.year, day.month, day.day, 18, 0, tzinfo=KST),
            "source_hash": "a" * 64,
        }
        for day in (MON, TUE, WED, THU, FRI)
    ]


def _universe_rows() -> list[dict[str, object]]:
    return [
        {
            "instrument_id": "KRX:005930",
            "ticker": "005930",
            "market": "KOSPI",
            "source_security_id": "KR7005930003",
            "share_kind": "보통주",
            "session": day,
            "available_at": datetime(day.year, day.month, day.day, 15, 30, tzinfo=KST),
        }
        for day in (MON, TUE, WED, THU, FRI)
    ]


def _standard_lake(data_root: Path) -> LocalLake:
    panel = _register(
        data_root,
        PANEL_DATASET_ID,
        {
            "year=2024/part.parquet": pl.DataFrame(_panel_rows()),
            "instrument_exits.parquet": pl.DataFrame(
                [{"instrument_id": "KRX:000000", "last_session": MON, "exit_kind": "halted_exit"}]
            ),
        },
    )
    universe = _register(
        data_root,
        UNIVERSE_DATASET_ID,
        {f"session={day.isoformat()}/part.parquet": pl.DataFrame([row]) for day, row in zip((MON, TUE, WED, THU, FRI), _universe_rows(), strict=True)},
    )
    return LocalLake(data_root, {PANEL_DATASET_ID: panel, UNIVERSE_DATASET_ID: universe})


def test_missing_local_datasets_yield_explicit_status(tmp_path: Path) -> None:
    """Absent local parts surface as missing-local status instead of external reads."""
    data_root = tmp_path / "data"
    data_root.mkdir(parents=True)
    empty = LocalLake(data_root, {})
    assert empty.previous_session(TUE) is None
    assert empty.next_session(TUE) is None
    assert empty.market_bar("KRX:005930", TUE, BATCH) is None
    assert empty.market_window("KRX:005930", MON, FRI, BATCH) == ()
    assert empty.market_universe(MON, FRI, BATCH) == {}
    panel_only = LocalLake(data_root, {PANEL_DATASET_ID: _register(data_root, PANEL_DATASET_ID, {})})
    assert panel_only.market_universe(MON, FRI, BATCH) == {}
    match = panel_only.resolve_security("005930", TUE, BATCH)
    assert match.status == "MISSING_LOCAL"
    assert match.instrument_id == ""


def test_construction_rejects_unverified_parts(tmp_path: Path) -> None:
    """Symlink, missing, tampered or unsafe local parts fail closed."""
    data_root = tmp_path / "data"
    manifest = _register(data_root, PANEL_DATASET_ID, {"year=2024/part.parquet": pl.DataFrame(_panel_rows())})
    target = data_root / "imports" / PANEL_DATASET_ID / "year=2024/part.parquet"
    target.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="hash mismatch"):
        LocalLake(data_root, {PANEL_DATASET_ID: manifest})
    forged = ImportManifest(
        dataset_id=PANEL_DATASET_ID,
        source_manifest_sha256="0" * 64,
        imported_at=datetime.now(UTC),
        parts=(ImportPart(PurePosixPath("year=2024/absent.parquet"), "0" * 64, 10),),
    )
    with pytest.raises(ValueError, match="missing local part"):
        LocalLake(data_root, {PANEL_DATASET_ID: forged})
    link_dir = data_root / "imports" / PANEL_DATASET_ID / "year=2025"
    link_dir.mkdir(parents=True, exist_ok=True)
    origin = tmp_path / "origin.parquet"
    origin.write_bytes(b"origin")
    link = link_dir / "part.parquet"
    link.symlink_to(origin)
    linked = ImportManifest(
        dataset_id=PANEL_DATASET_ID,
        source_manifest_sha256="0" * 64,
        imported_at=datetime.now(UTC),
        parts=(ImportPart(PurePosixPath("year=2025/part.parquet"), _sha_of(origin), origin.stat().st_size),),
    )
    with pytest.raises(ValueError, match="symlinked local path"):
        LocalLake(data_root, {PANEL_DATASET_ID: linked})
    unsafe = ImportManifest(
        dataset_id=PANEL_DATASET_ID,
        source_manifest_sha256="0" * 64,
        imported_at=datetime.now(UTC),
        parts=(ImportPart(PurePosixPath("../escape.parquet"), "0" * 64, 10),),
    )
    with pytest.raises(ValueError, match="unsafe local part"):
        LocalLake(data_root, {PANEL_DATASET_ID: unsafe})
    with pytest.raises(ValueError, match="unregistered local dataset"):
        LocalLake(data_root, {"other_id": manifest})
    with pytest.raises(ValueError, match="missing project data root"):
        LocalLake(tmp_path / "absent-root", {})


def test_bar_withheld_until_batch_availability(tmp_path: Path) -> None:
    """A bar available at 18:00 is unavailable at 09:00 the same day."""
    lake = _standard_lake(tmp_path / "data")
    morning = datetime(2024, 6, 25, 9, 0, tzinfo=KST)
    assert lake.market_bar("KRX:005930", TUE, morning) is None
    bar = lake.market_bar("KRX:005930", TUE, BATCH)
    assert bar is not None
    assert bar.open == 70000
    assert bar.close == 71000
    assert bar.source_hash == "a" * 64
    assert bar.available_at == BATCH
    with pytest.raises(ValueError, match="timezone"):
        lake.market_bar("KRX:005930", TUE, datetime(2024, 6, 25, 9, 0))
    with pytest.raises(ValueError, match="non-empty"):
        lake.market_bar("", TUE, BATCH)


def test_historical_identity_ambiguity(tmp_path: Path) -> None:
    """Duplicate candidates or changed markets resolve as ambiguous, never guessed."""
    lake = _standard_lake(tmp_path / "data")
    match = lake.resolve_security("005930", TUE, BATCH)
    assert match.status == "OK"
    assert match.instrument_id == "KRX:005930"
    assert match.market == "KOSPI"
    assert match.source_security_id == "KR7005930003"
    assert lake.resolve_security("999999", TUE, BATCH).status == "MISSING"
    assert lake.resolve_security("005930", date(2024, 7, 1), BATCH).status == "MISSING"
    with pytest.raises(ValueError, match="timezone"):
        lake.resolve_security("005930", TUE, datetime(2024, 6, 25, 9, 0))
    with pytest.raises(ValueError, match="non-empty"):
        lake.resolve_security("", TUE, BATCH)


def test_duplicate_and_changed_identity_is_ambiguous(tmp_path: Path) -> None:
    """Duplicate tickers, preferred shares and market drift never pick a first row."""
    data_root = tmp_path / "data"
    rows = _universe_rows()
    dup = [dict(rows[1]), dict(rows[1])]
    dup[1]["instrument_id"] = "KRX:005931"
    dup[1]["source_security_id"] = "KR7005931003"
    universe = _register(
        data_root, UNIVERSE_DATASET_ID, {"universe/part.parquet": pl.DataFrame(dup)}
    )
    bars = [row for row in _panel_rows() if row["session"] == TUE]
    conflict = [dict(bars[0]), dict(bars[0])]
    conflict[1]["instrument_id"] = "KRX:005931"
    panel = _register(data_root, PANEL_DATASET_ID, {"year=2024/part.parquet": pl.DataFrame(conflict)})
    lake = LocalLake(data_root, {PANEL_DATASET_ID: panel, UNIVERSE_DATASET_ID: universe})
    assert lake.resolve_security("005930", TUE, BATCH).status == "AMBIGUOUS"

    preferred = [dict(rows[1])]
    preferred[0]["share_kind"] = "신형우선주"
    universe_pref = _register(
        data_root, UNIVERSE_DATASET_ID, {"universe/part.parquet": pl.DataFrame(preferred)}
    )
    lake_pref = LocalLake(data_root, {PANEL_DATASET_ID: panel, UNIVERSE_DATASET_ID: universe_pref})
    assert lake_pref.resolve_security("005930", TUE, BATCH).status == "AMBIGUOUS"

    drifted = [dict(rows[1])]
    drifted[0]["market"] = "KOSDAQ"
    universe_drift = _register(
        data_root, UNIVERSE_DATASET_ID, {"universe/part.parquet": pl.DataFrame(drifted)}
    )
    single_bar = _register(
        data_root, PANEL_DATASET_ID, {"year=2024/part.parquet": pl.DataFrame([bars[0]])}
    )
    lake_drift = LocalLake(data_root, {PANEL_DATASET_ID: single_bar, UNIVERSE_DATASET_ID: universe_drift})
    assert lake_drift.resolve_security("005930", TUE, BATCH).status == "AMBIGUOUS"

    universe_only = _register(
        data_root, UNIVERSE_DATASET_ID, {"universe/part.parquet": pl.DataFrame([rows[1]])}
    )
    empty_panel = _register(data_root, PANEL_DATASET_ID, {"year=2023/part.parquet": pl.DataFrame(_panel_rows())})
    lake_nobar = LocalLake(data_root, {PANEL_DATASET_ID: empty_panel, UNIVERSE_DATASET_ID: universe_only})
    assert lake_nobar.resolve_security("005930", TUE, BATCH).status == "MISSING"

    dup_bars = [dict(bars[0]), dict(bars[0])]
    panel_dup = _register(data_root, PANEL_DATASET_ID, {"year=2024/part.parquet": pl.DataFrame(dup_bars)})
    lake_dupbar = LocalLake(data_root, {PANEL_DATASET_ID: panel_dup, UNIVERSE_DATASET_ID: universe_only})
    assert lake_dupbar.resolve_security("005930", TUE, BATCH).status == "AMBIGUOUS"
    assert lake_dupbar.market_bar("KRX:005930", TUE, BATCH) is None


def test_prior_session_is_strictly_before_receipt(tmp_path: Path) -> None:
    """Pre-disclosure denominators use completed sessions strictly before receipt day."""
    lake = _standard_lake(tmp_path / "data")
    assert lake.previous_session(TUE) == MON
    assert lake.next_session(TUE) == WED
    assert lake.previous_session(MON) is None
    assert lake.next_session(FRI) is None


def test_halt_gap_and_nulls_preserved(tmp_path: Path) -> None:
    """Halted, discontinuous and incomplete bars stay visible for downstream refusal."""
    data_root = tmp_path / "data"
    rows = _panel_rows()
    halted = [dict(row) for row in rows if row["session"] in (TUE, WED)]
    halted[0]["price_state"] = "invalid"
    halted[0]["gap_before"] = True
    halted[1]["open"] = None
    halted[1]["close"] = None
    broken = dict(halted[0])
    broken["session"] = THU
    broken["price_state"] = None
    panel = _register(data_root, PANEL_DATASET_ID, {"year=2024/part.parquet": pl.DataFrame([*halted, broken])})
    lake = LocalLake(data_root, {PANEL_DATASET_ID: panel})
    bar = lake.market_bar("KRX:005930", TUE, BATCH)
    assert bar is not None
    assert bar.price_state == "invalid"
    assert bar.gap_before is True
    assert bar.open == 70000
    gapped = lake.market_bar("KRX:005930", WED, datetime(2024, 6, 26, 18, 0, tzinfo=KST))
    assert gapped is not None
    assert gapped.open is None
    assert gapped.close is None
    assert lake.market_bar("KRX:005930", THU, datetime(2024, 6, 27, 18, 0, tzinfo=KST)) is None
    window = lake.market_window("KRX:005930", TUE, THU, datetime(2024, 6, 27, 18, 0, tzinfo=KST))
    assert [bar.session for bar in window] == [TUE, WED]


def test_window_preserves_gaps_and_skips_duplicates(tmp_path: Path) -> None:
    """Windows keep gaps, skip ambiguous sessions and stay within imported years."""
    lake = _standard_lake(tmp_path / "data")
    late = datetime(2024, 6, 26, 18, 0, tzinfo=KST)
    bars = lake.market_window("KRX:005930", MON, WED, late)
    assert [bar.session for bar in bars] == [MON, TUE, WED]
    assert lake.market_window("KRX:005930", date(2025, 1, 6), date(2025, 1, 10), BATCH) == ()
    with pytest.raises(ValueError, match="empty"):
        lake.market_window("KRX:005930", WED, MON, BATCH)
    with pytest.raises(ValueError, match="non-empty"):
        lake.market_window("", MON, WED, BATCH)
    with pytest.raises(ValueError, match="timezone"):
        lake.market_window("KRX:005930", MON, WED, datetime(2024, 6, 25, 9, 0))


def test_window_gap_and_duplicate_session(tmp_path: Path) -> None:
    """A missing session stays a gap while a duplicated session is skipped."""
    data_root = tmp_path / "data"
    rows = [row for row in _panel_rows() if row["session"] in (MON, TUE)]
    dup = [dict(rows[0]), dict(rows[0])]
    frames = {"year=2024/part.parquet": pl.DataFrame([*rows, *dup])}
    panel = _register(data_root, PANEL_DATASET_ID, frames)
    lake = LocalLake(data_root, {PANEL_DATASET_ID: panel})
    bars = lake.market_window("KRX:005930", MON, WED, BATCH)
    assert [bar.session for bar in bars] == [TUE]


def test_universe_projects_bounded_window(tmp_path: Path) -> None:
    """Universe reads project needed columns for the requested window only."""
    data_root = tmp_path / "data"
    first = [dict(row, instrument_id="KRX:000001", ticker="000001") for row in _panel_rows()]
    second = [dict(row, instrument_id="KRX:000002", ticker="000002") for row in _panel_rows()]
    future = dict(second[2])
    future["available_at"] = datetime(2024, 6, 27, 18, 0, tzinfo=KST)
    second[2] = future
    dup_first = [dict(first[0]), dict(first[0])]
    broken_second = dict(second[1])
    broken_second["session"] = WED
    broken_second["price_state"] = None
    panel = _register(
        data_root,
        PANEL_DATASET_ID,
        {"year=2024/part.parquet": pl.DataFrame([*first[1:], *second, *dup_first, broken_second])},
    )
    lake = LocalLake(data_root, {PANEL_DATASET_ID: panel})
    as_of = datetime(2024, 6, 26, 18, 0, tzinfo=KST)
    result = lake.market_universe(MON, WED, as_of)
    assert set(result) == {"KRX:000001", "KRX:000002"}
    assert [bar.session for bar in result["KRX:000001"]] == [TUE, WED]
    assert [bar.session for bar in result["KRX:000002"]] == [MON, TUE]
    assert result["KRX:000001"][0].market_cap == 420000000000000
    with pytest.raises(ValueError, match="empty"):
        lake.market_universe(WED, MON, as_of)
    with pytest.raises(ValueError, match="timezone"):
        lake.market_universe(MON, WED, datetime(2024, 6, 26, 9, 0))
