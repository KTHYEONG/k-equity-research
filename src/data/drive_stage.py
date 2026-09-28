"""Project-local staging of explicitly requested quant-lake objects."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tarfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from src.data.local_paths import checked_data_path, checked_local_path

_REMOTE_PREFIX = "gdrive:quant-lake/"
_STAGING_DIRNAME = "staging"
_HEXDIGITS = frozenset("0123456789abcdefABCDEF")
_CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True, slots=True)
class ArchiveLimits:
    """Explicit extraction budget independent of archive compressed size."""

    max_member_bytes: int = 268435456
    max_selected_bytes: int = 1073741824


def _is_hex64(value: str) -> bool:
    return len(value) == 64 and all(char in _HEXDIGITS for char in value)


def _check_member_name(value: PurePosixPath) -> None:
    text = value.as_posix()
    if not text or text == "." or value.is_absolute() or ".." in value.parts:
        raise ValueError(f"unsafe archive member: {text!r}")


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(_CHUNK_SIZE), b""):
            digest.update(block)
    return digest.hexdigest()


def stage_drive_file(remote_uri: str, expected_sha256: str, data_root: Path) -> Path:
    """Download one explicitly requested quant-lake object into project-local staging with rclone copyto and verify its known SHA-256. Return only a local path; raise on missing rclone, remote failure or checksum mismatch."""
    if not remote_uri.startswith(_REMOTE_PREFIX) or ".." in PurePosixPath(remote_uri[len(_REMOTE_PREFIX):]).parts:
        raise ValueError(f"refusing remote uri: {remote_uri!r}")
    remainder = remote_uri[len(_REMOTE_PREFIX):]
    if not remainder or remainder.endswith("/"):
        raise ValueError(f"refusing remote uri: {remote_uri!r}")
    normalized = expected_sha256.lower()
    if not _is_hex64(normalized):
        raise ValueError("invalid expected checksum")
    data_root.mkdir(parents=True, exist_ok=True)
    staging = checked_local_path(data_root, PurePosixPath(_STAGING_DIRNAME))
    staging.mkdir(parents=True, exist_ok=True)
    destination = checked_local_path(staging, PurePosixPath(remainder))
    if destination.is_file() and _sha256_of(destination) == normalized:
        return destination
    if destination.exists():
        raise ValueError("conflicting staged archive already present")
    rclone = shutil.which("rclone")
    if rclone is None:
        raise OSError("rclone executable not available")
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = checked_data_path(data_root, destination.with_name(destination.name + ".partial"))
    completed = subprocess.run(  # noqa: S603
        [rclone, "copyto", remote_uri, str(partial)],
        shell=False,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if completed.returncode != 0 or not partial.is_file():
        if partial.is_symlink() or partial.exists():
            partial.unlink(missing_ok=True)
        raise OSError("remote download failed")
    if _sha256_of(partial) != normalized:
        partial.unlink(missing_ok=True)
        raise ValueError("downloaded archive checksum mismatch")
    os.replace(partial, destination)
    return destination


def stage_selected_tar_members(
    archive_path: Path,
    expected_members: Mapping[PurePosixPath, str],
    data_root: Path,
    limits: ArchiveLimits | None = None,
) -> tuple[Path, ...]:
    """Extract only requested verified members from a local quant-lake tar.gz archive into project-local staging. Reject traversal, links, duplicate members and per-member hash mismatches."""
    if archive_path.is_symlink() or not archive_path.is_file():
        raise ValueError(f"missing archive: {archive_path}")
    if not expected_members:
        raise ValueError("empty member selection")
    active = limits if limits is not None else ArchiveLimits()
    if active.max_member_bytes <= 0 or active.max_selected_bytes <= 0:
        raise ValueError("invalid extraction budget")
    wanted: dict[str, str] = {}
    for name, checksum in expected_members.items():
        member_name = PurePosixPath(str(name))
        _check_member_name(member_name)
        normalized = checksum.lower()
        if not _is_hex64(normalized):
            raise ValueError(f"invalid member checksum: {member_name.as_posix()!r}")
        key = member_name.as_posix()
        wanted[key] = normalized
    data_root.mkdir(parents=True, exist_ok=True)
    staging = checked_local_path(data_root, PurePosixPath(_STAGING_DIRNAME))
    staging.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, mode="r:gz") as archive:
        infos = archive.getmembers()
    matches: dict[str, list[tarfile.TarInfo]] = {}
    for info in infos:
        if info.name in wanted:
            matches.setdefault(info.name, []).append(info)
    staged: list[Path] = []
    total_bytes = 0
    for key in wanted:
        candidates = matches.get(key, [])
        if not candidates:
            raise ValueError(f"member absent from archive: {key!r}")
        if len(candidates) > 1:
            raise ValueError(f"duplicate member in archive: {key!r}")
        info = candidates[0]
        if not info.isreg():
            raise ValueError(f"refusing non-regular member: {key!r}")
        if info.size < 0 or info.size > active.max_member_bytes:
            raise ValueError(f"member exceeds extraction budget: {key!r}")
        total_bytes += info.size
        if total_bytes > active.max_selected_bytes:
            raise ValueError("selection exceeds extraction budget")
    for key in wanted:
        info = matches[key][0]
        destination = checked_local_path(staging, PurePosixPath(key))
        if destination.is_file() and _sha256_of(destination) == wanted[key]:
            staged.append(destination)
            continue
        if destination.exists():
            raise ValueError(f"conflicting staged member already present: {key!r}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = checked_data_path(data_root, destination.with_name(destination.name + ".partial"))
        digest = hashlib.sha256()
        written = 0
        with tarfile.open(archive_path, mode="r:gz") as archive:
            member = archive.extractfile(info)
            assert member is not None
            with member, partial.open("wb") as output:
                while True:
                    block = member.read(_CHUNK_SIZE)
                    if not block:
                        break
                    digest.update(block)
                    written += len(block)
                    output.write(block)
        if written != info.size or digest.hexdigest() != wanted[key]:
            partial.unlink(missing_ok=True)
            raise ValueError(f"member checksum mismatch: {key!r}")
        os.replace(partial, destination)
        staged.append(destination)
    return tuple(staged)
