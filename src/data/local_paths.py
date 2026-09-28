"""Validate paths that must remain inside one project-local data root."""

from __future__ import annotations

from pathlib import Path, PurePosixPath


def checked_local_path(data_root: Path, relative_path: PurePosixPath) -> Path:
    """Return a path inside data_root, rejecting traversal and symlinks in every component."""
    text = relative_path.as_posix()
    if not text or text == "." or relative_path.is_absolute() or ".." in relative_path.parts:
        raise ValueError(f"unsafe local path: {text!r}")
    for ancestor in (data_root.absolute(), *data_root.absolute().parents):
        if ancestor.is_symlink():
            raise ValueError(f"symlinked data root: {ancestor}")
    root = data_root.resolve()
    candidate = root
    for part in relative_path.parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise ValueError(f"symlinked local path: {relative_path}")
    resolved = candidate.resolve(strict=False)
    if root not in resolved.parents:  # pragma: no cover - protects against a path swap after component checks
        raise ValueError(f"path outside data root: {relative_path}")
    return candidate


def checked_data_path(data_root: Path, path: Path) -> Path:
    """Validate a path already expressed relative to or below data_root."""
    root = data_root.absolute()
    target = path.absolute()
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"path outside data root: {path}") from exc
    return checked_local_path(data_root, PurePosixPath(relative.as_posix()))
