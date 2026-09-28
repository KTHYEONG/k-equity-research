"""Invariant guards for project-local verified import."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath

import pytest

from src.data.imports import import_dataset, import_financial_evidence


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_source_dataset(
    base: Path,
    dataset_id: str,
    files: dict[str, bytes],
    declared_sizes: dict[str, int] | None = None,
) -> Path:
    source = base / "upstream" / dataset_id
    partitions = []
    for name in sorted(files):
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(files[name])
        declared = len(files[name]) if declared_sizes is None else declared_sizes[name]
        partitions.append({"path": name, "sha256": _digest(files[name]), "bytes": declared})
    manifest = {"dataset_id": dataset_id, "partitions": partitions}
    (source / "manifest.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    return source


def _manifest_sha(source: Path) -> str:
    return hashlib.sha256((source / "manifest.json").read_bytes()).hexdigest()


def test_independent_copy_survives_source_removal(tmp_path: Path) -> None:
    """Local SHA matches while inode differs; reads succeed after source removal."""
    payload = b"parquet-bytes-2023" * 64
    source = _write_source_dataset(tmp_path, "market_panel_hash", {"year=2023/part.parquet": payload})
    data_root = tmp_path / "data"
    manifest = import_dataset(source, [PurePosixPath("year=2023/part.parquet")], data_root)
    local = data_root / "imports" / "market_panel_hash" / "year=2023" / "part.parquet"
    assert local.read_bytes() == payload
    assert _digest(local.read_bytes()) == manifest.parts[0].sha256
    assert not os.path.samestat((source / "year=2023/part.parquet").stat(), local.stat())
    assert manifest.imported_at.tzinfo is not None
    for child in sorted(source.rglob("*")):
        if child.is_file() and not child.is_symlink():
            child.unlink()
    assert local.read_bytes() == payload


def test_partial_selection_registers_only_selected(tmp_path: Path) -> None:
    """Local manifest names exactly the selected part plus the origin manifest hash."""
    source = _write_source_dataset(
        tmp_path,
        "market_panel_hash",
        {"year=2023/part.parquet": b"a" * 32, "year=2024/part.parquet": b"b" * 32},
    )
    manifest = import_dataset(source, [PurePosixPath("year=2023/part.parquet")], tmp_path / "data")
    assert [part.relative_path.as_posix() for part in manifest.parts] == ["year=2023/part.parquet"]
    assert manifest.source_manifest_sha256 == _manifest_sha(source)
    stored = json.loads((tmp_path / "data" / "imports" / "market_panel_hash" / "manifest.json").read_text())
    assert [item["path"] for item in stored["parts"]] == ["year=2023/part.parquet"]
    assert stored["source_manifest_sha256"] == _manifest_sha(source)
    assert "upstream" not in json.dumps(stored)
    assert not (tmp_path / "data" / "imports" / "market_panel_hash" / "year=2024" / "part.parquet").exists()


def test_tampered_source_fails_without_complete_dataset(tmp_path: Path) -> None:
    """Hash mismatch aborts registration and leaves no complete local dataset."""
    source = _write_source_dataset(tmp_path, "facts_hash", {"part-00000.parquet": b"real"})
    (source / "part-00000.parquet").write_bytes(b"fake")
    with pytest.raises(ValueError, match="hash mismatch"):
        import_dataset(source, [PurePosixPath("part-00000.parquet")], tmp_path / "data")
    dest_root = tmp_path / "data" / "imports" / "facts_hash"
    assert not (dest_root / "manifest.json").exists()
    assert not (dest_root / "part-00000.parquet").exists()
    assert list(dest_root.rglob("*.partial")) == []


def test_declared_size_mismatch_fails(tmp_path: Path) -> None:
    """A source file disagreeing with its manifest size is rejected."""
    source = _write_source_dataset(
        tmp_path, "facts_hash", {"part-00000.parquet": b"real"}, declared_sizes={"part-00000.parquet": 999}
    )
    with pytest.raises(ValueError, match="size mismatch"):
        import_dataset(source, [PurePosixPath("part-00000.parquet")], tmp_path / "data")


def test_invalid_selections_fail_closed(tmp_path: Path) -> None:
    """Empty, duplicate, unknown and unsafe selections are rejected."""
    source = _write_source_dataset(tmp_path, "facts_hash", {"part-00000.parquet": b"real"})
    data_root = tmp_path / "data"
    with pytest.raises(ValueError, match="empty selection"):
        import_dataset(source, [], data_root)
    with pytest.raises(ValueError, match="duplicate"):
        import_dataset(
            source,
            [PurePosixPath("part-00000.parquet"), PurePosixPath("part-00000.parquet")],
            data_root,
        )
    with pytest.raises(ValueError, match="not listed"):
        import_dataset(source, [PurePosixPath("missing.parquet")], data_root)
    with pytest.raises(ValueError, match="unsafe"):
        import_dataset(source, [PurePosixPath("../escape.parquet")], data_root)
    with pytest.raises(ValueError, match="unsafe"):
        import_dataset(source, [PurePosixPath("/absolute.parquet")], data_root)


def test_invalid_source_manifests_fail(tmp_path: Path) -> None:
    """Malformed manifests, bad identities and unreadable parts fail before copying."""
    data_root = tmp_path / "data"
    missing = tmp_path / "upstream" / "absent"
    missing.mkdir(parents=True)
    with pytest.raises(ValueError, match="missing source manifest"):
        import_dataset(missing, [PurePosixPath("a.parquet")], data_root)
    corrupt = tmp_path / "upstream" / "corrupt"
    corrupt.mkdir(parents=True)
    (corrupt / "manifest.json").write_text("not json", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid source manifest"):
        import_dataset(corrupt, [PurePosixPath("a.parquet")], data_root)
    bad_id = tmp_path / "upstream" / "badid"
    bad_id.mkdir(parents=True)
    (bad_id / "manifest.json").write_text(
        json.dumps({"dataset_id": "../evil", "partitions": [{"path": "a.parquet", "sha256": "0" * 64}]}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="invalid dataset id"):
        import_dataset(bad_id, [PurePosixPath("a.parquet")], data_root)
    non_str_id = tmp_path / "upstream" / "nonstr"
    non_str_id.mkdir(parents=True)
    (non_str_id / "manifest.json").write_text(
        json.dumps({"dataset_id": 7, "partitions": [{"path": "a.parquet", "sha256": "0" * 64}]}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="invalid source manifest"):
        import_dataset(non_str_id, [PurePosixPath("a.parquet")], data_root)
    empty_parts = tmp_path / "upstream" / "emptyparts"
    empty_parts.mkdir(parents=True)
    (empty_parts / "manifest.json").write_text(
        json.dumps({"dataset_id": "ok_id", "partitions": []}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="invalid source manifest"):
        import_dataset(empty_parts, [PurePosixPath("a.parquet")], data_root)
    dup_parts = tmp_path / "upstream" / "dupparts"
    dup_parts.mkdir(parents=True)
    (dup_parts / "a.parquet").write_bytes(b"x")
    entry = {"path": "a.parquet", "sha256": _digest(b"x"), "bytes": 1}
    (dup_parts / "manifest.json").write_text(
        json.dumps({"dataset_id": "ok_id", "partitions": [entry, entry]}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="invalid source manifest"):
        import_dataset(dup_parts, [PurePosixPath("a.parquet")], data_root)
    bad_size = tmp_path / "upstream" / "badsize"
    bad_size.mkdir(parents=True)
    (bad_size / "a.parquet").write_bytes(b"x")
    (bad_size / "manifest.json").write_text(
        json.dumps(
            {"dataset_id": "ok_id", "partitions": [{"path": "a.parquet", "sha256": _digest(b"x"), "bytes": -1}]}
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="invalid source manifest"):
        import_dataset(bad_size, [PurePosixPath("a.parquet")], data_root)
    traversal = tmp_path / "upstream" / "traversal"
    traversal.mkdir(parents=True)
    (traversal / "manifest.json").write_text(
        json.dumps(
            {"dataset_id": "ok_id", "partitions": [{"path": "../a.parquet", "sha256": "0" * 64, "bytes": 1}]}
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unsafe"):
        import_dataset(traversal, [PurePosixPath("a.parquet")], data_root)
    ghost = _write_source_dataset(tmp_path, "ghost_id", {"a.parquet": b"x"})
    (ghost / "a.parquet").unlink()
    with pytest.raises(ValueError, match="invalid source part"):
        import_dataset(ghost, [PurePosixPath("a.parquet")], data_root)
    for label, partitions in {
        "non-dict entry": ["not-a-dict"],
        "non-string fields": [{"path": 7, "sha256": "0" * 64}],
        "non-hex digest": [{"path": "a.parquet", "sha256": "zz"}],
    }.items():
        broken = tmp_path / "upstream" / f"broken-{label.split()[0]}"
        broken.mkdir(parents=True, exist_ok=True)
        (broken / "a.parquet").write_bytes(b"x")
        (broken / "manifest.json").write_text(
            json.dumps({"dataset_id": "ok_id", "partitions": partitions}), encoding="utf-8"
        )
        with pytest.raises(ValueError, match="invalid source manifest"):
            import_dataset(broken, [PurePosixPath("a.parquet")], data_root)


def test_symlinked_sources_rejected(tmp_path: Path) -> None:
    """Symlinked dataset roots, manifests parts and destinations are refused."""
    real = _write_source_dataset(tmp_path, "facts_hash", {"a.parquet": b"x"})
    linked = tmp_path / "linked_dataset"
    linked.symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError, match="invalid source dataset"):
        import_dataset(linked, [PurePosixPath("a.parquet")], tmp_path / "data")
    link_part = tmp_path / "upstream" / "linkpart"
    link_part.mkdir(parents=True)
    (tmp_path / "real_payload.parquet").write_bytes(b"x")
    (link_part / "a.parquet").symlink_to(tmp_path / "real_payload.parquet")
    (link_part / "manifest.json").write_text(
        json.dumps({"dataset_id": "link_id", "partitions": [{"path": "a.parquet", "sha256": _digest(b"x")}]}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="symlinked local path"):
        import_dataset(link_part, [PurePosixPath("a.parquet")], tmp_path / "data")


def test_symlinked_import_parent_cannot_copy_outside_data_root(tmp_path: Path) -> None:
    source = _write_source_dataset(tmp_path, "facts_hash", {"part.parquet": b"payload"})
    data_root = tmp_path / "data"
    outside = tmp_path / "outside"
    (data_root / "imports").mkdir(parents=True)
    outside.mkdir()
    (data_root / "imports" / "facts_hash").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinked local path"):
        import_dataset(source, [PurePosixPath("part.parquet")], data_root)
    assert not (outside / "part.parquet").exists()


def test_hardlinked_and_existing_outputs_rejected(tmp_path: Path) -> None:
    """Pre-existing hardlinked, symlinked or stale destinations fail without overwrite."""
    source = _write_source_dataset(tmp_path, "facts_hash", {"a.parquet": b"payload"})
    data_root = tmp_path / "data"
    dest = data_root / "imports" / "facts_hash" / "a.parquet"
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.link(source / "a.parquet", dest)
    with pytest.raises(ValueError, match="hardlinked"):
        import_dataset(source, [PurePosixPath("a.parquet")], data_root)
    dest.unlink()
    dest.symlink_to(source / "a.parquet")
    with pytest.raises(ValueError, match="symlinked local path"):
        import_dataset(source, [PurePosixPath("a.parquet")], data_root)
    dest.unlink()
    dest.write_bytes(b"stale")
    with pytest.raises(ValueError, match="refusing to overwrite"):
        import_dataset(source, [PurePosixPath("a.parquet")], data_root)


def test_repeat_import_idempotent_and_conflict_fails(tmp_path: Path) -> None:
    """Identical bytes import twice; changed bytes fail while originals stay intact."""
    source = _write_source_dataset(tmp_path, "facts_hash", {"a.parquet": b"v1"})
    data_root = tmp_path / "data"
    first = import_dataset(source, [PurePosixPath("a.parquet")], data_root)
    second = import_dataset(source, [PurePosixPath("a.parquet")], data_root)
    assert second == first
    local = data_root / "imports" / "facts_hash" / "a.parquet"
    local.unlink()
    repaired = import_dataset(source, [PurePosixPath("a.parquet")], data_root)
    assert local.read_bytes() == b"v1"
    assert repaired.source_manifest_sha256 == first.source_manifest_sha256
    local.write_bytes(b"tampered-local")
    with pytest.raises(ValueError, match="refusing to overwrite"):
        import_dataset(source, [PurePosixPath("a.parquet")], data_root)
    local.write_bytes(b"v1")
    manifest_text = (data_root / "imports" / "facts_hash" / "manifest.json").read_text()
    (source / "a.parquet").write_bytes(b"v222")
    (source / "manifest.json").write_text(
        json.dumps(
            {
                "dataset_id": "facts_hash",
                "partitions": [{"path": "a.parquet", "sha256": _digest(b"v222"), "bytes": 4}],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="conflicting import"):
        import_dataset(source, [PurePosixPath("a.parquet")], data_root)
    assert local.read_bytes() == b"v1"
    assert (data_root / "imports" / "facts_hash" / "manifest.json").read_text() == manifest_text


def test_corrupt_local_manifest_fails(tmp_path: Path) -> None:
    """An unreadable local manifest blocks registration instead of being trusted."""
    source = _write_source_dataset(tmp_path, "facts_hash", {"a.parquet": b"v1"})
    data_root = tmp_path / "data"
    dest_root = data_root / "imports" / "facts_hash"
    dest_root.mkdir(parents=True, exist_ok=True)
    (dest_root / "manifest.json").write_text("broken", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid local manifest"):
        import_dataset(source, [PurePosixPath("a.parquet")], data_root)


def _write_receipt_dir(base: Path, payload: bytes, receipt: bytes, name: str = "payload.json") -> Path:
    receipt_dir = base / "receipts" / _digest(payload)
    receipt_dir.mkdir(parents=True, exist_ok=True)
    (receipt_dir / name).write_bytes(payload)
    (receipt_dir / "receipt.json").write_bytes(receipt)
    return receipt_dir


def test_evidence_import_is_stable(tmp_path: Path) -> None:
    """Equal payload hash yields one stable local pair on repeated imports."""
    payload = b'{"fact": 1}'
    receipt = b'{"retrieved_at": "2026-09-19T07:06:37+00:00"}'
    receipt_dir = _write_receipt_dir(tmp_path, payload, receipt)
    data_root = tmp_path / "data"
    first_payload, first_receipt = import_financial_evidence(receipt_dir, _digest(payload), data_root)
    second_payload, second_receipt = import_financial_evidence(receipt_dir, _digest(payload), data_root)
    assert (first_payload, first_receipt) == (second_payload, second_receipt)
    assert first_payload.read_bytes() == payload
    assert first_receipt.read_bytes() == receipt
    assert data_root.resolve() in first_payload.resolve().parents
    assert data_root.resolve() in first_receipt.resolve().parents
    assert receipt_dir.resolve() not in first_payload.resolve().parents


def test_evidence_rejects_mismatch_and_missing(tmp_path: Path) -> None:
    """Bad hashes, tampered payloads and absent files fail closed."""
    payload = b'{"fact": 1}'
    receipt_dir = _write_receipt_dir(tmp_path, payload, b'{"ok": true}')
    data_root = tmp_path / "data"
    with pytest.raises(ValueError, match="invalid source hash"):
        import_financial_evidence(receipt_dir, "xyz", data_root)
    with pytest.raises(ValueError, match="hash mismatch"):
        import_financial_evidence(receipt_dir, _digest(b"other"), data_root)
    (receipt_dir / "payload.json").write_bytes(b"changed")
    with pytest.raises(ValueError, match="hash mismatch"):
        import_financial_evidence(receipt_dir, _digest(payload), data_root)
    (receipt_dir / "payload.json").write_bytes(payload)
    (receipt_dir / "receipt.json").unlink()
    with pytest.raises(ValueError, match="missing evidence"):
        import_financial_evidence(receipt_dir, _digest(payload), data_root)
    payloadless = tmp_path / "receipts" / "payloadless"
    payloadless.mkdir(parents=True, exist_ok=True)
    (payloadless / "receipt.json").write_bytes(b'{"ok": true}')
    with pytest.raises(ValueError, match="missing evidence"):
        import_financial_evidence(payloadless, _digest(payload), data_root)
    with pytest.raises(ValueError, match="missing evidence"):
        import_financial_evidence(tmp_path / "receipts" / "absent", _digest(payload), data_root)


def test_evidence_conflict_and_zip_variant(tmp_path: Path) -> None:
    """Conflicting local evidence is kept; zipped payloads import under their own name."""
    payload = b'{"fact": 2}'
    receipt_dir = _write_receipt_dir(tmp_path, payload, b'{"ok": true}')
    data_root = tmp_path / "data"
    stored_payload, stored_receipt = import_financial_evidence(receipt_dir, _digest(payload), data_root)
    stored_payload.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="conflicting evidence"):
        import_financial_evidence(receipt_dir, _digest(payload), data_root)
    stored_payload.write_bytes(payload)
    stored_receipt.unlink()
    with pytest.raises(ValueError, match="conflicting evidence"):
        import_financial_evidence(receipt_dir, _digest(payload), data_root)
    zip_payload = b"PK-fake-zip-bytes"
    zip_dir = _write_receipt_dir(tmp_path, zip_payload, b'{"ok": "zip"}', name="payload.zip")
    zip_stored, _ = import_financial_evidence(zip_dir, _digest(zip_payload), data_root)
    assert zip_stored.name == "payload.zip"
    assert zip_stored.read_bytes() == zip_payload
