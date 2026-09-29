"""Command-line interface for k-equity-research."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import date, datetime
from pathlib import Path, PurePosixPath

from src.data.catalog import Catalog
from src.data.drive_stage import ArchiveLimits, stage_drive_file, stage_selected_tar_members
from src.data.imports import import_dataset, import_financial_evidence

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_LIMITS = ArchiveLimits()


def project_data_root() -> Path:
    """Return this repository's project-local data directory."""
    return PROJECT_ROOT / "data"


def open_catalog(data_root: Path) -> Catalog:
    """Construct the shared local catalog after validating the project data root."""
    catalog = Catalog(data_root / "catalog.sqlite")
    return catalog


def _resolve_data_root(override: str | None) -> Path:
    root = Path(override) if override else project_data_root()
    resolved = root.resolve()
    if resolved != PROJECT_ROOT.resolve() and PROJECT_ROOT.resolve() not in resolved.parents:
        raise ValueError(f"data root must stay inside this repository: {root}")
    return resolved


def _add_data_root(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--data-root", default=None, help="Project-local data root (defaults to ./data).")


def build_parser() -> argparse.ArgumentParser:
    """Create the equity-research argument parser."""
    parser = argparse.ArgumentParser(prog="equity-research")
    subparsers = parser.add_subparsers(dest="command", required=True)
    data = subparsers.add_parser("data", help="Project-local data operations.")
    data_sub = data.add_subparsers(dest="data_command", required=True)
    importer = data_sub.add_parser("import", help="Import verified source partitions.")
    importer.add_argument("--source-dataset", required=True)
    importer.add_argument("--parts", nargs="+", required=True)
    _add_data_root(importer)
    evidence = data_sub.add_parser("import-evidence", help="Import cited financial evidence.")
    evidence.add_argument("--source-receipt-dir", required=True)
    evidence.add_argument("--source-hash", required=True)
    _add_data_root(evidence)
    lineage = data_sub.add_parser("register-lineage", help="Register imported source payloads in the local catalog.")
    lineage.add_argument("--dataset-id", required=True)
    lineage.add_argument("--kind", required=True, choices=("daily_market", "security_master"))
    lineage.add_argument("--source-root", required=True, help="Source Bronze root used only during this import.")
    _add_data_root(lineage)
    retain = data_sub.add_parser("retain-build", help="Build verified time-scoped research datasets.")
    _add_data_root(retain)
    financial_v2 = data_sub.add_parser("publish-financial-v2", help="Publish source-currency-corrected financial index.")
    _add_data_root(financial_v2)
    backfill = data_sub.add_parser("backfill-index", help="Backfill official KOSPI/KOSDAQ bars for retained sessions.")
    backfill.add_argument("--start", required=True, help="Retained interval start YYYY-MM-DD.")
    backfill.add_argument("--end", required=True, help="Retained interval end YYYY-MM-DD.")
    _add_data_root(backfill)
    dart_backfill = data_sub.add_parser(
        "backfill-dart", help="Backfill official buyback disclosures by calendar month."
    )
    dart_backfill.add_argument("--start", required=True, help="History start YYYY-MM-DD (2023-01-01 or later).")
    dart_backfill.add_argument("--end", required=True, help="History end YYYY-MM-DD (inclusive).")
    _add_data_root(dart_backfill)
    disclosure = data_sub.add_parser(
        "collect-disclosure-context", help="Collect all-category DART evidence around accepted event windows."
    )
    disclosure.add_argument("--as-of", required=True, help="Timezone-aware instant, e.g. 2024-06-28T09:00:00+09:00.")
    disclosure.add_argument("--event-id", default=None, help="Limit collection to one accepted event.")
    _add_data_root(disclosure)
    financial = data_sub.add_parser("collect-financial", help="Collect one official OpenDART financial statement.")
    financial.add_argument("--corp-code", required=True, help="8-digit OpenDART corp code.")
    financial.add_argument("--year", required=True, type=int, help="Business year, e.g. 2024.")
    financial.add_argument("--report-code", required=True, help="Report code: 11011, 11012, 11013, or 11014.")
    financial.add_argument("--fs-div", required=True, choices=("CFS", "OFS"))
    financial.add_argument("--snapshot-id", default=None, help="Collection snapshot id (defaults per request).")
    _add_data_root(financial)
    audit = data_sub.add_parser("audit-financial", help="Audit event-corp financial evidence and report gaps.")
    audit.add_argument("--as-of", required=True, help="Timezone-aware instant, e.g. 2024-06-28T09:00:00+09:00.")
    _add_data_root(audit)
    cleanup = data_sub.add_parser("retain-cleanup", help="Plan or apply referentially safe local retirement.")
    cleanup.add_argument("--retire-run-id", action="append", default=[], help="Research run ID to retire.")
    cleanup.add_argument("--apply", action="store_true", help="Execute a previously displayed plan.")
    cleanup.add_argument("--plan-digest", default=None, help="Approved plan digest required with --apply.")
    _add_data_root(cleanup)
    staged = data_sub.add_parser("stage-drive", help="Stage one quant-lake object.")
    staged.add_argument("--remote-uri", required=True)
    staged.add_argument("--expected-sha256", required=True)
    _add_data_root(staged)
    extracted = data_sub.add_parser("extract-archive", help="Extract verified archive members.")
    extracted.add_argument("--archive", required=True)
    extracted.add_argument("--member", action="append", default=[], help="member=sha256 entries.")
    extracted.add_argument("--max-member-bytes", type=int, default=_DEFAULT_LIMITS.max_member_bytes)
    extracted.add_argument("--max-selected-bytes", type=int, default=_DEFAULT_LIMITS.max_selected_bytes)
    _add_data_root(extracted)
    research = subparsers.add_parser("research", help="Source-backed research operations.")
    research_sub = research.add_subparsers(dest="research_command", required=True)
    memo = research_sub.add_parser("memo", help="Assemble one receipt-specific research view.")
    memo.add_argument("--rcept-no", required=True)
    memo.add_argument("--as-of", required=True, help="Timezone-aware instant, e.g. 2024-06-28T09:00:00+09:00.")
    memo.add_argument("--index-manifest", required=True)
    memo.add_argument(
        "--agent", action="store_true", help="Draft narrative with the local model; baseline stays the fallback."
    )
    memo.add_argument("--agent-base-url", default="http://127.0.0.1:8080")
    memo.add_argument("--agent-model", default="local-7b-q4")
    memo.add_argument("--agent-timeout-seconds", type=float, default=30.0)
    memo.add_argument("--agent-max-calls", type=int, default=3)
    memo.add_argument("--agent-prompt-version", default="v1")
    _add_data_root(memo)
    repair = research_sub.add_parser("repair-memo", help="Rebuild one historical memo with verified analogue evidence.")
    repair.add_argument("--run-id", required=True)
    repair.add_argument("--index-manifest", required=True)
    _add_data_root(repair)
    evaluation = subparsers.add_parser("eval", help="Frozen replay and stratified evaluation.")
    eval_sub = evaluation.add_subparsers(dest="eval_command", required=True)
    replay = eval_sub.add_parser("replay", help="Replay pinned cases and aggregate stratified quality.")
    replay.add_argument("--cases", required=True, help="Local reviewed labels file, e.g. data/eval/cases.json.")
    replay.add_argument(
        "--agent", action="store_true", help="Replay with the local model; baseline stays the fallback."
    )
    replay.add_argument("--agent-base-url", default="http://127.0.0.1:8080")
    replay.add_argument("--agent-model", default="local-7b-q4")
    replay.add_argument("--agent-timeout-seconds", type=float, default=30.0)
    replay.add_argument("--agent-max-calls", type=int, default=3)
    replay.add_argument("--agent-prompt-version", default="v1")
    _add_data_root(replay)
    batch = subparsers.add_parser("batch", help="Recoverable daily batch operations.")
    batch_sub = batch.add_subparsers(dest="batch_command", required=True)
    daily = batch_sub.add_parser("daily", help="Collect new receipts/index days and publish memos atomically.")
    daily.add_argument("--dart-start", required=True, help="Primary DART window start YYYY-MM-DD.")
    daily.add_argument("--dart-end", required=True, help="Primary DART window end YYYY-MM-DD.")
    daily.add_argument("--as-of", required=True, help="Timezone-aware instant, e.g. 2024-06-28T18:30:00+09:00.")
    daily.add_argument("--recheck-days", type=int, default=365)
    daily.add_argument("--publish-time", default="18:30", help="Daily KST orchestration time HH:MM.")
    daily.add_argument("--max-attempts", type=int, default=3)
    daily.add_argument(
        "--agent", action="store_true", help="Draft narrative with the local model; baseline stays the fallback."
    )
    daily.add_argument("--agent-base-url", default="http://127.0.0.1:8080")
    daily.add_argument("--agent-model", default="local-7b-q4")
    daily.add_argument("--agent-timeout-seconds", type=float, default=30.0)
    daily.add_argument("--agent-max-calls", type=int, default=3)
    daily.add_argument("--agent-prompt-version", default="v1")
    _add_data_root(daily)
    return parser


def _run_import(args: argparse.Namespace) -> int:
    root = _resolve_data_root(args.data_root)
    manifest = import_dataset(
        Path(args.source_dataset),
        [PurePosixPath(part) for part in args.parts],
        root,
    )
    sys.stdout.write(
        json.dumps(
            {
                "manifest": str(root / "imports" / manifest.dataset_id / "manifest.json"),
                "dataset_id": manifest.dataset_id,
                "parts": len(manifest.parts),
            }
        )
        + "\n"
    )
    return 0


def _run_import_evidence(args: argparse.Namespace) -> int:
    payload, receipt = import_financial_evidence(
        Path(args.source_receipt_dir), args.source_hash, _resolve_data_root(args.data_root)
    )
    sys.stdout.write(json.dumps({"payload": str(payload), "receipt": str(receipt)}) + "\n")
    return 0


def _run_register_lineage(args: argparse.Namespace) -> int:
    from src.data.lineage_import import register_imported_lineage

    count = register_imported_lineage(
        _resolve_data_root(args.data_root),
        args.dataset_id,
        args.kind,
        Path(args.source_root) if args.source_root else None,
    )
    sys.stdout.write(json.dumps({"dataset_id": args.dataset_id, "source_hashes": count}) + "\n")
    return 0


def _run_retain_build(args: argparse.Namespace) -> int:
    from src.data.retention import materialize_research_scope

    summary = materialize_research_scope(data_root=_resolve_data_root(args.data_root))
    sys.stdout.write(
        json.dumps(
            {
                "dataset_ids": list(summary.dataset_ids),
                "source_rows": dict(summary.source_rows),
                "retained_rows": dict(summary.retained_rows),
                "source_bytes": dict(summary.source_bytes),
                "retained_bytes": dict(summary.retained_bytes),
                "provenance": str(summary.provenance_path),
            }
        )
        + "\n"
    )
    return 0


def _run_publish_financial_v2(args: argparse.Namespace) -> int:
    from src.data.financial_index_v2 import publish_financial_index_v2

    result = publish_financial_index_v2(_resolve_data_root(args.data_root))
    sys.stdout.write(json.dumps({
        "rows": result.row_count,
        "corrected_rows": result.corrected_rows,
        "corrected_hashes": result.corrected_hashes,
        "currencies": list(result.currencies),
        "manifest": str(result.manifest_path),
    }) + "\n")
    return 0


def _run_backfill_index(args: argparse.Namespace) -> int:
    import os as _os

    import httpx

    from src.cli.batch import _load_previous_manifest, _read_import_manifests
    from src.data.krx_ingest import collect_index_sessions
    from src.data.local_lake import LocalLake
    from src.integrations.krx_index import KrxIndexClient

    api_key = _os.environ.get("KRX_OPENAPI_KEY") or _os.environ.get("KRX_API_KEY", "")
    if not api_key:
        raise ValueError("KRX API key must be set (KRX_OPENAPI_KEY)")
    root = _resolve_data_root(args.data_root)
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    snapshot_id = f"index-backfill-{start.isoformat()}-{end.isoformat()}"
    catalog = open_catalog(root)
    lake = LocalLake(root, _read_import_manifests(root))
    previous = _load_previous_manifest(root)
    http_client = httpx.Client(timeout=30.0)
    try:
        summary = collect_index_sessions(
            KrxIndexClient(api_key, http_client), catalog, lake, previous, start, end, root, snapshot_id
        )
    finally:
        http_client.close()
    sys.stdout.write(
        json.dumps(
            {
                "expected_keys": summary.expected_keys,
                "reused_keys": summary.reused_keys,
                "fetched_keys": summary.fetched_keys,
                "unresolved_keys": list(summary.unresolved_keys),
                "manifest_hash": summary.manifest_hash,
                "manifest": str(root / "krx" / "manifests" / f"{summary.manifest_hash}.json"),
            }
        )
        + "\n"
    )
    return 0


def _run_backfill_dart(args: argparse.Namespace) -> int:
    import os as _os

    import httpx

    from src.cli.batch import _read_import_manifests
    from src.core.buyback_document import DocumentLimits
    from src.data.dart_ingest import collect_buyback_history
    from src.data.event_store import EventStore
    from src.data.local_lake import LocalLake
    from src.integrations.dart import DartClient

    api_key = _os.environ.get("OPENDART_API_KEY") or _os.environ.get("DART_API_KEY", "")
    if not api_key:
        raise ValueError("DART API key must be set (OPENDART_API_KEY)")
    root = _resolve_data_root(args.data_root)
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    snapshot_id = f"dart-backfill-{start.isoformat()}-{end.isoformat()}"
    catalog = open_catalog(root)
    lake = LocalLake(root, _read_import_manifests(root))
    event_store = EventStore(catalog)
    http_client = httpx.Client(timeout=30.0)
    try:
        client = DartClient(api_key, http_client)

        class _DisclosureTypeClient:
            """Limit historical listing to one verified buyback-bearing DART type."""

            def __init__(self, pblntf_ty: str) -> None:
                self.pblntf_ty = pblntf_ty

            def list_major_reports(self, window_start: date, window_end: date, page: int) -> object:
                return client.list_reports(window_start, window_end, page, pblntf_ty=self.pblntf_ty)

            def document_zip(self, rcept_no: str) -> bytes:
                return client.document_zip(rcept_no)

            def current_buyback_details(self, corp_code: str, window_start: date, window_end: date) -> bytes:
                return client.current_buyback_details(corp_code, window_start, window_end)

        summaries = {
            pblntf_ty: collect_buyback_history(
                _DisclosureTypeClient(pblntf_ty),  # type: ignore[arg-type]
                catalog,
                lake,
                start,
                end,
                root,
                f"{snapshot_id}-{pblntf_ty}",
                DocumentLimits(),
                event_store=event_store,
            )
            for pblntf_ty in ("B", "E")
        }
    finally:
        http_client.close()
    summary = {
        field: sum(getattr(item, field) for item in summaries.values())
        for field in (
            "documents_fetched",
            "documents_reused",
            "events_accepted",
            "list_pages_fetched",
            "list_pages_reused",
            "windows_complete",
        )
    }
    sys.stdout.write(
        json.dumps(
            {
                **summary,
                "disclosure_types": ["B", "E"],
                "snapshot_ids": {kind: f"{snapshot_id}-{kind}" for kind in summaries},
            }
        )
        + "\n"
    )
    return 0


def _run_collect_disclosure_context(args: argparse.Namespace) -> int:
    import os as _os

    import httpx

    from src.cli.batch import _read_import_manifests
    from src.data.disclosure_context import collect_event_disclosure_context
    from src.data.event_store import EventStore
    from src.data.local_lake import LocalLake
    from src.integrations.dart import DartClient
    from src.research.event_study import StudyPolicy

    api_key = _os.environ.get("OPENDART_API_KEY") or _os.environ.get("DART_API_KEY", "")
    if not api_key:
        raise ValueError("DART API key must be set (OPENDART_API_KEY)")
    root = _resolve_data_root(args.data_root)
    as_of = datetime.fromisoformat(args.as_of)
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as-of instant must be timezone-aware")
    from zoneinfo import ZoneInfo

    stamp = as_of.astimezone(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d-%H%M%S%z").replace("+", "p")
    snapshot_id = f"disclosure-context-{stamp}"
    catalog = open_catalog(root)
    lake = LocalLake(root, _read_import_manifests(root))
    event_store = EventStore(catalog)
    http_client = httpx.Client(timeout=30.0)
    try:
        summary = collect_event_disclosure_context(
            DartClient(api_key, http_client),
            catalog,
            event_store,
            lake,
            StudyPolicy(),
            root,
            as_of,
            snapshot_id,
            args.event_id,
        )
    finally:
        http_client.close()
    sys.stdout.write(
        json.dumps(
            {
                "documents_verified": summary.documents_verified,
                "issuer_windows": summary.issuer_windows,
                "missing_receipts": list(summary.missing_receipts),
                "pages_verified": summary.pages_verified,
                "receipts_discovered": summary.receipts_discovered,
                "snapshot_id": snapshot_id,
            }
        )
        + "\n"
    )
    return 0


def _run_collect_financial(args: argparse.Namespace) -> int:
    import os as _os
    from datetime import UTC

    import httpx

    from src.data.financial_ingest import collect_financial_snapshot
    from src.integrations.dart import DartClient, FinancialStatementRequest

    api_key = _os.environ.get("OPENDART_API_KEY") or _os.environ.get("DART_API_KEY", "")
    if not api_key:
        raise ValueError("DART API key must be set (OPENDART_API_KEY)")
    root = _resolve_data_root(args.data_root)
    request = FinancialStatementRequest(
        corp_code=args.corp_code,
        bsns_year=int(args.year),
        reprt_code=args.report_code,
        fs_div=args.fs_div,
    )
    snapshot_id = args.snapshot_id or (
        f"financial-{request.corp_code.strip()}-{request.bsns_year:04d}"
        f"-{request.reprt_code.strip()}-{str(request.fs_div).strip()}"
    )
    observed_at = datetime.now(UTC)
    catalog = open_catalog(root)
    http_client = httpx.Client(timeout=30.0)
    try:
        summary = collect_financial_snapshot(
            DartClient(api_key, http_client), catalog, request, root, snapshot_id, observed_at
        )
    finally:
        http_client.close()
    sys.stdout.write(
        json.dumps(
            {
                "artifact_sha256": summary.artifact_sha256,
                "artifact_path": str(summary.artifact_path),
                "row_count": summary.row_count,
                "observed_at": summary.observed_at.isoformat(),
            }
        )
        + "\n"
    )
    return 0


def _run_audit_financial(args: argparse.Namespace) -> int:
    from zoneinfo import ZoneInfo

    from src.cli.batch import _read_import_manifests
    from src.data.event_store import EventStore
    from src.data.financial_evidence import FinancialEvidence
    from src.data.financial_hydration import hydrate_event_financial_evidence
    from src.data.local_lake import LocalLake

    root = _resolve_data_root(args.data_root)
    as_of = datetime.fromisoformat(args.as_of)
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as-of instant must be timezone-aware")
    catalog = open_catalog(root)
    lake = LocalLake(root, _read_import_manifests(root))
    financial = FinancialEvidence(root, lake)
    event_store = EventStore(catalog)
    summary = hydrate_event_financial_evidence(catalog, event_store, financial, root, as_of)
    stamp = as_of.astimezone(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d-%H%M%S%z").replace("+", "p")
    report = {
        "as_of": as_of.isoformat(),
        "event_corp_count": summary.event_corp_count,
        "required_hash_count": summary.required_hash_count,
        "verified_hash_count": summary.verified_hash_count,
        "missing_hashes": list(summary.missing_hashes),
        "unverified_hashes": list(summary.unverified_hashes),
        "missing_requests": [
            {
                "corp_code": request.corp_code,
                "bsns_year": request.bsns_year,
                "reprt_code": request.reprt_code,
                "fs_div": request.fs_div,
            }
            for request in summary.missing_requests
        ],
    }
    payload = (json.dumps(report, sort_keys=True, indent=2) + "\n").encode("utf-8")
    relative = PurePosixPath(f"financial_hydration/gap-{stamp}.json")
    _atomic_write_bytes(root, root / relative.as_posix(), payload)
    sys.stdout.write(
        json.dumps(
            {
                "event_corp_count": summary.event_corp_count,
                "required_hash_count": summary.required_hash_count,
                "verified_hash_count": summary.verified_hash_count,
                "missing_hashes": list(summary.missing_hashes),
                "unverified_hashes": list(summary.unverified_hashes),
                "report": str(root / relative.as_posix()),
            }
        )
        + "\n"
    )
    return 0


def _run_retain_cleanup(args: argparse.Namespace) -> int:
    from src.data.event_store import EventStore
    from src.data.retention_cleanup import execute_local_retirement, plan_local_retirement

    root = _resolve_data_root(args.data_root)
    retired = frozenset(str(run_id) for run_id in args.retire_run_id)
    catalog = open_catalog(root)
    event_store = EventStore(catalog)
    plan = plan_local_retirement(root, catalog, event_store, retired)
    body = {
        "obsolete_import_parts": len(plan.obsolete_import_parts),
        "obsolete_raw_artifacts": len(plan.obsolete_raw_artifacts),
        "obsolete_financial_evidence": len(plan.obsolete_financial_evidence),
        "blocking_run_ids": list(plan.blocking_run_ids),
        "retained_bytes": plan.retained_bytes,
        "reclaimable_bytes": plan.reclaimable_bytes,
        "plan_digest": plan.plan_digest,
    }
    if not args.apply:
        sys.stdout.write(json.dumps(body) + "\n")
        return 0
    if not args.plan_digest:
        raise ValueError("--plan-digest is required with --apply")
    sys.stdout.write(json.dumps(body) + "\n")
    if args.plan_digest != plan.plan_digest:
        raise ValueError("plan digest changed since approval")
    execute_local_retirement(plan, root, catalog, retired)
    return 0


def _run_stage_drive(args: argparse.Namespace) -> int:
    staged = stage_drive_file(args.remote_uri, args.expected_sha256, _resolve_data_root(args.data_root))
    sys.stdout.write(json.dumps({"staged": str(staged)}) + "\n")
    return 0


def _run_extract_archive(args: argparse.Namespace) -> int:
    members: dict[PurePosixPath, str] = {}
    for entry in args.member:
        name, separator, digest = entry.partition("=")
        if not separator or not name or not digest:
            raise ValueError(f"invalid member entry: {entry!r}")
        members[PurePosixPath(name)] = digest
    limits = ArchiveLimits(max_member_bytes=args.max_member_bytes, max_selected_bytes=args.max_selected_bytes)
    parts = stage_selected_tar_members(Path(args.archive), members, _resolve_data_root(args.data_root), limits)
    sys.stdout.write(json.dumps({"members": len(parts), "paths": [str(path) for path in parts]}) + "\n")
    return 0


def _atomic_write_bytes(data_root: Path, target: Path, payload: bytes) -> None:
    """Write one local file atomically without exposing a partial report."""
    from src.data.local_paths import checked_data_path

    target = checked_data_path(data_root, target)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise ValueError(f"refusing symlinked report destination: {target}")
    partial = checked_data_path(data_root, target.with_name(target.name + ".partial"))
    partial.write_bytes(payload)
    import os

    os.replace(partial, target)


def _run_research_memo(args: argparse.Namespace) -> int:
    from zoneinfo import ZoneInfo

    from src.data.event_store import EventStore
    from src.data.financial_evidence import FinancialEvidence
    from src.data.imports import ImportManifest, ImportPart
    from src.data.index_store import IndexStore, load_index_manifest
    from src.data.local_lake import LocalLake
    from src.data.local_paths import checked_data_path, checked_local_path
    from src.research.analogue_proof import build_analogue_proof
    from src.research.context import ResearchUnavailable, build_research_context
    from src.research.event_study import StudyPolicy
    from src.research.memo import build_baseline_memo
    from src.research.publication import proof_reference, publish_research_run

    root = _resolve_data_root(args.data_root)
    catalog = open_catalog(root)
    manifests: dict[str, ImportManifest] = {}
    imports_root = checked_local_path(root, PurePosixPath("imports"))
    if imports_root.is_dir():
        for child in sorted(imports_root.iterdir()):
            manifest_path = checked_data_path(root, child / "manifest.json")
            if manifest_path.is_symlink() or not manifest_path.is_file():
                continue
            try:
                document = json.loads(manifest_path.read_bytes().decode("utf-8"))
                manifests[str(document["dataset_id"])] = ImportManifest(
                    dataset_id=str(document["dataset_id"]),
                    source_manifest_sha256=str(document["source_manifest_sha256"]),
                    imported_at=datetime.fromisoformat(str(document["imported_at"])),
                    parts=tuple(
                        ImportPart(
                            relative_path=PurePosixPath(str(item["path"])),
                            sha256=str(item["sha256"]),
                            byte_length=int(item["bytes"]),
                        )
                        for item in document["parts"]
                    ),
                )
            except (ValueError, KeyError, TypeError) as exc:
                raise ValueError(f"invalid local manifest: {manifest_path}") from exc
    lake = LocalLake(root, manifests)
    financial = FinancialEvidence(root, lake)
    event_store = EventStore(catalog)
    manifest = load_index_manifest(root, args.index_manifest)
    index_store = IndexStore(catalog, root, manifest)
    as_of = datetime.fromisoformat(args.as_of)
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as-of instant must be timezone-aware")
    try:
        context = build_research_context(
            catalog, event_store, lake, financial, index_store, args.rcept_no, as_of, StudyPolicy()
        )
    except ResearchUnavailable as exc:
        sys.stderr.write(f"unavailable: {exc.reason_code}\n")
        return 3
    proof = build_analogue_proof(context)
    stamp = as_of.astimezone(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d-%H%M%S%z").replace("+", "p")
    run_id = f"{context.active_rcept_no}-{stamp}"
    relative_dir = PurePosixPath(f"reports/{context.event.event_id}/{run_id}")
    run_dir = root / relative_dir.as_posix()
    analogue_ref = proof_reference(run_dir, proof) if proof is not None else None
    memo = build_baseline_memo(context, analogue_ref=analogue_ref)
    if args.agent:
        import httpx

        from src.agent.local_model import LlamaCppClient
        from src.agent.workflow import AgentPolicy, AgentRunner

        http_client = httpx.Client()
        try:
            model_client = LlamaCppClient(
                args.agent_base_url, args.agent_model, args.agent_timeout_seconds, http_client
            )
            agent_policy = AgentPolicy(args.agent_max_calls, args.agent_timeout_seconds, args.agent_prompt_version)
            memo = AgentRunner().run(context, memo, model_client, agent_policy)
        finally:
            http_client.close()
    run_manifest = {
        "event_id": context.event.event_id,
        "index_manifest_hash": manifest.manifest_hash,
        "manifest_hash": memo.manifest_hash,
        "proof_sha256": proof.sha256 if proof is not None else None,
        "run_id": run_id,
    }
    published = publish_research_run(root, run_id, memo, proof, run_manifest)
    sys.stdout.write(
        json.dumps(
            {
                "anchor_rcept_no": context.anchor_rcept_no,
                "active_rcept_no": context.active_rcept_no,
                "analogue_proof": proof.sha256 if proof is not None else None,
                "manifest_hash": published.memo.manifest_hash,
                "markdown": str(root / published.report_dir.as_posix() / "memo.md"),
                "materiality": context.materiality.status,
                "memo": str(root / published.report_dir.as_posix() / "memo.json"),
                "run_id": run_id,
                "study": context.study.status,
                "comparables": context.comparables.status,
            }
        )
        + "\n"
    )
    return 0


def _run_research_repair_memo(args: argparse.Namespace) -> int:
    from src.research.repair import repair_memo_evidence

    root = _resolve_data_root(args.data_root)
    summary = repair_memo_evidence(root, args.run_id, args.index_manifest)
    sys.stdout.write(
        json.dumps(
            {
                "old_run_id": summary.old_run_id,
                "new_run_id": summary.new_run_id,
                "run_id": summary.new_run_id,
                "proof_sha256": summary.proof_sha256,
                "analogue_proof": summary.proof_sha256,
                "manifest_hash": summary.manifest_hash,
            }
        )
        + "\n"
    )
    return 0


def _run_eval_replay(args: argparse.Namespace) -> int:
    from src.eval.replay import evaluate_cases, load_cases, render_report_markdown, report_to_dict

    root = _resolve_data_root(args.data_root)
    from src.data.local_paths import checked_data_path

    cases = load_cases(checked_data_path(root, Path(args.cases)))
    if args.agent:
        import httpx

        from src.agent.local_model import LlamaCppClient
        from src.agent.workflow import AgentPolicy

        http_client = httpx.Client()
        try:
            model_client = LlamaCppClient(
                args.agent_base_url, args.agent_model, args.agent_timeout_seconds, http_client
            )
            agent_policy = AgentPolicy(args.agent_max_calls, args.agent_timeout_seconds, args.agent_prompt_version)
            report = evaluate_cases(cases, root, model_client, agent_policy)
        finally:
            http_client.close()
    else:
        report = evaluate_cases(cases, root)
    run_dir = root / "eval" / "runs" / report.run_id
    report_payload = (json.dumps(report_to_dict(report), sort_keys=True, indent=2) + "\n").encode("utf-8")
    markdown_payload = render_report_markdown(report).encode("utf-8")
    _atomic_write_bytes(root, run_dir / "report.json", report_payload)
    _atomic_write_bytes(root, run_dir / "report.md", markdown_payload)
    sys.stdout.write(
        json.dumps(
            {
                "case_count": report.case_count,
                "markdown": str(run_dir / "report.md"),
                "report": str(run_dir / "report.json"),
                "run_id": report.run_id,
            }
        )
        + "\n"
    )
    return 0


def _run_batch_daily(args: argparse.Namespace) -> int:
    from datetime import date, time

    from src.cli.batch import BatchPolicy, run_daily_batch

    root = _resolve_data_root(args.data_root)
    dart_start = date.fromisoformat(args.dart_start)
    dart_end = date.fromisoformat(args.dart_end)
    hour_text, _, minute_text = args.publish_time.partition(":")
    publish_time = time(int(hour_text), int(minute_text) if minute_text else 0)
    # Development-plan defaults are wired here, not hidden in domain code:
    # daily 18:30 KST orchestration, recent-year recheck, bounded retry budget.
    batch_policy = BatchPolicy(
        dart_start=dart_start,
        dart_end=dart_end,
        recheck_days=args.recheck_days,
        publish_time_kst=publish_time,
        max_attempts=args.max_attempts,
    )
    as_of = datetime.fromisoformat(args.as_of)
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as-of instant must be timezone-aware")
    project_data_root = root
    if args.agent:
        import httpx

        from src.agent.local_model import LlamaCppClient
        from src.agent.workflow import AgentPolicy

        http_client = httpx.Client()
        try:
            model_client = LlamaCppClient(
                args.agent_base_url, args.agent_model, args.agent_timeout_seconds, http_client
            )
            agent_policy = AgentPolicy(args.agent_max_calls, args.agent_timeout_seconds, args.agent_prompt_version)
            summary = run_daily_batch(
                batch_policy,
                project_data_root,
                as_of,
                agent_mode=args.agent,
                agent_model=model_client,
                agent_policy=agent_policy,
            )
        finally:
            http_client.close()
    else:
        summary = run_daily_batch(batch_policy, project_data_root, as_of, agent_mode=args.agent)
    sys.stdout.write(
        json.dumps(
            {
                "artifacts_registered": summary.artifacts_registered,
                "events_linked": summary.events_linked,
                "failures": list(summary.failures),
                "manifest_hash": summary.manifest_hash,
                "memos_published": summary.memos_published,
                "memos_withheld": summary.memos_withheld,
                "run_id": summary.run_id,
            }
        )
        + "\n"
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run the equity-research CLI and return a process exit code."""
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        open_catalog(_resolve_data_root(getattr(args, "data_root", None)))
        if args.command == "data" and args.data_command == "import":
            return _run_import(args)
        if args.command == "data" and args.data_command == "import-evidence":
            return _run_import_evidence(args)
        if args.command == "data" and args.data_command == "register-lineage":
            return _run_register_lineage(args)
        if args.command == "data" and args.data_command == "retain-build":
            return _run_retain_build(args)
        if args.command == "data" and args.data_command == "publish-financial-v2":
            return _run_publish_financial_v2(args)
        if args.command == "data" and args.data_command == "backfill-index":
            return _run_backfill_index(args)
        if args.command == "data" and args.data_command == "backfill-dart":
            return _run_backfill_dart(args)
        if args.command == "data" and args.data_command == "collect-disclosure-context":
            return _run_collect_disclosure_context(args)
        if args.command == "data" and args.data_command == "collect-financial":
            return _run_collect_financial(args)
        if args.command == "data" and args.data_command == "audit-financial":
            return _run_audit_financial(args)
        if args.command == "data" and args.data_command == "retain-cleanup":
            return _run_retain_cleanup(args)
        if args.command == "data" and args.data_command == "stage-drive":
            return _run_stage_drive(args)
        if args.command == "data" and args.data_command == "extract-archive":
            return _run_extract_archive(args)
        if args.command == "research" and args.research_command == "memo":
            return _run_research_memo(args)
        if args.command == "research" and args.research_command == "repair-memo":
            return _run_research_repair_memo(args)
        if args.command == "eval" and args.eval_command == "replay":
            return _run_eval_replay(args)
        if args.command == "batch" and args.batch_command == "daily":
            return _run_batch_daily(args)
    except (ValueError, OSError) as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2
    parser.error("unsupported command")
    return 2


if __name__ == "__main__":
    sys.exit(main())
