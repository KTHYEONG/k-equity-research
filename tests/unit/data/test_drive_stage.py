"""Invariant guards for quant-lake staging and archive extraction."""

from __future__ import annotations

import hashlib
import io
import os
import tarfile
from pathlib import Path, PurePosixPath

import pytest

from src.data.drive_stage import ArchiveLimits, stage_drive_file, stage_selected_tar_members


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_tar(path: Path, files: dict[str, bytes]) -> None:
    with tarfile.open(path, mode="w:gz") as archive:
        for name in sorted(files):
            data = files[name]
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mtime = 0
            archive.addfile(info, io.BytesIO(data))


def _write_tar_with_symlink(path: Path, link_name: str, target: str, regular: dict[str, bytes]) -> None:
    with tarfile.open(path, mode="w:gz") as archive:
        for name in sorted(regular):
            data = regular[name]
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mtime = 0
            archive.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo(name=link_name)
        link.type = tarfile.SYMTYPE
        link.linkname = target
        link.mtime = 0
        archive.addfile(link)


def _write_tar_with_duplicate(path: Path, name: str, first: bytes, second: bytes) -> None:
    with tarfile.open(path, mode="w:gz") as archive:
        for data in (first, second):
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mtime = 0
            archive.addfile(info, io.BytesIO(data))


def _install_fake_rclone(bin_dir: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "rclone"
    script.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))


def test_selected_restore_only_requested_member(tmp_path: Path) -> None:
    """Only the requested verified member appears in project-local staging."""
    archive = tmp_path / "bronze_financial_facts.tar.gz"
    first = b'{"fact": 1}'
    second = b'{"fact": 2}'
    _write_tar(
        archive,
        {
            "bronze/financial_facts/hash1/payload.json": first,
            "bronze/financial_facts/hash2/payload.json": second,
        },
    )
    data_root = tmp_path / "data"
    (staged,) = stage_selected_tar_members(
        archive,
        {PurePosixPath("bronze/financial_facts/hash1/payload.json"): _digest(first)},
        data_root,
    )
    assert staged.read_bytes() == first
    assert data_root.resolve() in staged.resolve().parents
    assert not (data_root / "staging" / "bronze/financial_facts/hash2/payload.json").exists()


def test_extract_is_idempotent(tmp_path: Path) -> None:
    """Extracting the same verified member twice yields the same staged path."""
    archive = tmp_path / "bronze.tar.gz"
    payload = b"evidence-bytes"
    _write_tar(archive, {"bronze/a/payload.json": payload})
    data_root = tmp_path / "data"
    wanted = {PurePosixPath("bronze/a/payload.json"): _digest(payload)}
    first = stage_selected_tar_members(archive, wanted, data_root)
    second = stage_selected_tar_members(archive, wanted, data_root)
    assert first == second
    assert first[0].read_bytes() == payload


def test_unsafe_archive_members_rejected(tmp_path: Path) -> None:
    """Traversal, absolute and symlink members never escape staging."""
    data_root = tmp_path / "data"
    traversal = tmp_path / "traversal.tar.gz"
    _write_tar(traversal, {"../escape.json": b"evil"})
    with pytest.raises(ValueError, match="unsafe"):
        stage_selected_tar_members(
            traversal, {PurePosixPath("../escape.json"): _digest(b"evil")}, data_root
        )
    absolute = tmp_path / "absolute.tar.gz"
    _write_tar(absolute, {"/tmp/escape.json": b"evil"})  # noqa: S108
    with pytest.raises(ValueError, match="unsafe"):
        stage_selected_tar_members(
            absolute, {PurePosixPath("/tmp/escape.json"): _digest(b"evil")}, data_root  # noqa: S108
        )
    linked = tmp_path / "linked.tar.gz"
    _write_tar_with_symlink(
        linked, "bronze/a/payload.json", "/etc/passwd", {"bronze/a/receipt.json": b"{}"}
    )
    with pytest.raises(ValueError, match="non-regular"):
        stage_selected_tar_members(
            linked, {PurePosixPath("bronze/a/payload.json"): _digest(b"evil")}, data_root
        )
    staging = data_root / "staging"
    leftovers = list(staging.rglob("*")) if staging.exists() else []
    assert [path for path in leftovers if path.is_file() and "partial" not in path.name] == []


def test_member_selection_errors(tmp_path: Path) -> None:
    """Absent, duplicated, corrupt and over-budget selections fail."""
    archive = tmp_path / "bronze.tar.gz"
    payload = b"evidence-bytes"
    _write_tar(archive, {"bronze/a/payload.json": payload})
    data_root = tmp_path / "data"
    with pytest.raises(ValueError, match="empty member selection"):
        stage_selected_tar_members(archive, {}, data_root)
    with pytest.raises(ValueError, match="missing archive"):
        stage_selected_tar_members(tmp_path / "absent.tar.gz", {PurePosixPath("x"): "0" * 64}, data_root)
    with pytest.raises(ValueError, match="absent from archive"):
        stage_selected_tar_members(
            archive, {PurePosixPath("bronze/a/ghost.json"): _digest(b"x")}, data_root
        )
    with pytest.raises(ValueError, match="invalid member checksum"):
        stage_selected_tar_members(archive, {PurePosixPath("bronze/a/payload.json"): "zz"}, data_root)
    with pytest.raises(ValueError, match="checksum mismatch"):
        stage_selected_tar_members(
            archive, {PurePosixPath("bronze/a/payload.json"): _digest(b"other")}, data_root
        )
    with pytest.raises(ValueError, match="invalid extraction budget"):
        stage_selected_tar_members(
            archive,
            {PurePosixPath("bronze/a/payload.json"): _digest(payload)},
            data_root,
            ArchiveLimits(max_member_bytes=0, max_selected_bytes=10),
        )
    with pytest.raises(ValueError, match="exceeds extraction budget"):
        stage_selected_tar_members(
            archive,
            {PurePosixPath("bronze/a/payload.json"): _digest(payload)},
            data_root,
            ArchiveLimits(max_member_bytes=2, max_selected_bytes=1024),
        )
    two = tmp_path / "two.tar.gz"
    _write_tar(two, {"bronze/a/1.json": b"11", "bronze/a/2.json": b"22"})
    with pytest.raises(ValueError, match="exceeds extraction budget"):
        stage_selected_tar_members(
            two,
            {
                PurePosixPath("bronze/a/1.json"): _digest(b"11"),
                PurePosixPath("bronze/a/2.json"): _digest(b"22"),
            },
            data_root,
            ArchiveLimits(max_member_bytes=16, max_selected_bytes=3),
        )
    dup = tmp_path / "dup.tar.gz"
    _write_tar_with_duplicate(dup, "bronze/a/payload.json", b"one", b"two")
    with pytest.raises(ValueError, match="duplicate member"):
        stage_selected_tar_members(
            dup, {PurePosixPath("bronze/a/payload.json"): _digest(b"one")}, data_root
        )


def test_conflicting_staged_member_rejected(tmp_path: Path) -> None:
    """Existing staged bytes are never overwritten by a conflicting member."""
    archive = tmp_path / "bronze.tar.gz"
    payload = b"evidence-bytes"
    _write_tar(archive, {"bronze/a/payload.json": payload})
    data_root = tmp_path / "data"
    wanted = {PurePosixPath("bronze/a/payload.json"): _digest(payload)}
    (staged,) = stage_selected_tar_members(archive, wanted, data_root)
    staged.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="conflicting staged member"):
        stage_selected_tar_members(archive, wanted, data_root)
    assert staged.read_bytes() == b"tampered"
    staged.unlink()
    link_target = tmp_path / "outside.txt"
    link_target.write_bytes(b"outside")
    staged.symlink_to(link_target)
    with pytest.raises(ValueError, match="symlinked local path"):
        stage_selected_tar_members(archive, wanted, data_root)


def test_stage_drive_file_success_and_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A fake rclone copy is hash-verified, staged locally and stable on repeat."""
    fixture = tmp_path / "remote-bytes.tar.gz"
    fixture.write_bytes(b"archive-bytes")
    monkeypatch.setenv("FIXTURE", str(fixture))
    _install_fake_rclone(tmp_path / "bin", monkeypatch, '/bin/cp "$FIXTURE" "$3"')
    data_root = tmp_path / "data"
    first = stage_drive_file("gdrive:quant-lake/bronze_financial_facts.tar.gz", _digest(b"archive-bytes"), data_root)
    assert first.read_bytes() == b"archive-bytes"
    assert data_root.resolve() in first.resolve().parents
    second = stage_drive_file("gdrive:quant-lake/bronze_financial_facts.tar.gz", _digest(b"archive-bytes"), data_root)
    assert second == first


def test_stage_drive_file_rejects_bad_requests(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-lake URIs, bad checksums and conflicting destinations fail."""
    fixture = tmp_path / "remote-bytes.tar.gz"
    fixture.write_bytes(b"archive-bytes")
    monkeypatch.setenv("FIXTURE", str(fixture))
    _install_fake_rclone(tmp_path / "bin", monkeypatch, '/bin/cp "$FIXTURE" "$3"')
    data_root = tmp_path / "data"
    with pytest.raises(ValueError, match="remote uri"):
        stage_drive_file("s3://bucket/archive.tar.gz", _digest(b"archive-bytes"), data_root)
    with pytest.raises(ValueError, match="remote uri"):
        stage_drive_file("gdrive:quant-lake/", _digest(b"archive-bytes"), data_root)
    with pytest.raises(ValueError, match="remote uri"):
        stage_drive_file("gdrive:quant-lake/../evil.tar.gz", _digest(b"archive-bytes"), data_root)
    with pytest.raises(ValueError, match="invalid expected checksum"):
        stage_drive_file("gdrive:quant-lake/a.tar.gz", "zz", data_root)
    with pytest.raises(ValueError, match="checksum mismatch"):
        stage_drive_file("gdrive:quant-lake/a.tar.gz", _digest(b"other-bytes"), data_root)
    assert not (data_root / "staging" / "a.tar.gz").exists()
    conflict = data_root / "staging" / "conflict.tar.gz"
    conflict.parent.mkdir(parents=True, exist_ok=True)
    conflict.write_bytes(b"stale")
    with pytest.raises(ValueError, match="conflicting staged archive"):
        stage_drive_file("gdrive:quant-lake/conflict.tar.gz", _digest(b"archive-bytes"), data_root)
    conflict.unlink()
    conflict.symlink_to(fixture)
    with pytest.raises(ValueError, match="symlinked local path"):
        stage_drive_file("gdrive:quant-lake/conflict.tar.gz", _digest(b"archive-bytes"), data_root)


def test_stage_drive_file_remote_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing rclone, remote errors and empty downloads expose no complete archive."""
    data_root = tmp_path / "data"
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    (tmp_path / "empty-bin").mkdir(parents=True, exist_ok=True)
    with pytest.raises(OSError, match="rclone"):
        stage_drive_file("gdrive:quant-lake/a.tar.gz", _digest(b"x"), data_root)
    fixture = tmp_path / "remote-bytes.tar.gz"
    fixture.write_bytes(b"archive-bytes")
    monkeypatch.setenv("FIXTURE", str(fixture))
    failing = tmp_path / "failing-bin"
    _install_fake_rclone(failing, monkeypatch, '/bin/cp "$FIXTURE" "$3"\nexit 1')
    with pytest.raises(OSError, match="remote download failed"):
        stage_drive_file("gdrive:quant-lake/a.tar.gz", _digest(b"archive-bytes"), data_root)
    assert not (data_root / "staging" / "a.tar.gz").exists()
    silent = tmp_path / "silent-bin"
    _install_fake_rclone(silent, monkeypatch, "exit 0")
    with pytest.raises(OSError, match="remote download failed"):
        stage_drive_file("gdrive:quant-lake/a.tar.gz", _digest(b"archive-bytes"), data_root)
    assert not (data_root / "staging" / "a.tar.gz").exists()
