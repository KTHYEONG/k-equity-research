"""Local incremental batch and recoverable publication."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path, PurePosixPath
from typing import Any
from zoneinfo import ZoneInfo

from src.agent.local_model import DEFAULT_AGENT_MODEL, DEFAULT_AGENT_TIMEOUT_SECONDS
from src.agent.workflow import AGENT_SCHEMAS, DEFAULT_PROMPT_VERSION, AgentPolicy, AgentRunner, LocalModelClient
from src.core.buyback_document import DocumentLimits
from src.data.catalog import Catalog
from src.data.event_store import EventStore
from src.data.financial_evidence import FinancialEvidence
from src.data.imports import ImportManifest, ImportPart
from src.data.index_store import IndexManifest, IndexStore, load_index_manifest, merge_index_manifest
from src.data.local_lake import LocalLake
from src.data.local_paths import checked_data_path, checked_local_path
from src.integrations.dart import api_keys_from_environ
from src.research.analogue_proof import build_analogue_proof
from src.research.context import ResearchUnavailable, build_research_context
from src.research.event_study import StudyPolicy
from src.research.memo import build_baseline_memo
from src.research.publication import proof_reference, publish_research_run

KST = ZoneInfo("Asia/Seoul")

_DEFAULT_RECHECK_DAYS = 365
_DEFAULT_PUBLISH_TIME = time(18, 30)
_DEFAULT_MAX_ATTEMPTS = 3


@dataclass(frozen=True, slots=True)
class BatchPolicy:
    """Explicit configuration for one recoverable daily batch."""

    dart_start: date
    dart_end: date
    recheck_days: int
    publish_time_kst: time
    max_attempts: int

    def __post_init__(self) -> None:
        if self.dart_start > self.dart_end:
            raise ValueError("collection window must not be empty")
        if self.recheck_days < 0:
            raise ValueError("recheck days must be non-negative")
        if self.max_attempts < 1:
            raise ValueError("max attempts must be positive")


@dataclass(frozen=True, slots=True)
class BatchSummary:
    """Auditable outcome for one daily batch run."""

    run_id: str
    collection_windows: tuple[str, ...]
    artifacts_registered: int
    events_linked: int
    memos_published: int
    memos_withheld: int
    failures: tuple[str, ...]
    manifest_hash: str


def _policy_fingerprint(policy: BatchPolicy) -> str:
    payload = {
        "dart_end": policy.dart_end.isoformat(),
        "dart_start": policy.dart_start.isoformat(),
        "max_attempts": policy.max_attempts,
        "publish_time_kst": policy.publish_time_kst.isoformat(),
        "recheck_days": policy.recheck_days,
    }
    raw = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _run_id(policy: BatchPolicy, as_of: datetime) -> str:
    kst = as_of.astimezone(KST)
    stamp = kst.strftime("%Y%m%d")
    return f"batch-{stamp}-{_policy_fingerprint(policy)[:8]}"


def _collection_snapshot(data_root: Path, base_run_id: str) -> str:
    sequence = 1
    while True:
        candidate = base_run_id if sequence == 1 else f"{base_run_id}-r{sequence}"
        marker = checked_local_path(data_root, PurePosixPath("collections") / candidate / "complete.json")
        if not marker.is_file():
            return candidate
        sequence += 1


def _source_run_id(
    base_run_id: str,
    catalog: Catalog,
    manifests: dict[str, ImportManifest],
    index_manifest: IndexManifest,
    as_of: datetime,
    agent_mode: bool,
) -> str:
    from src.research.memo import CODE_REVISION

    study_policy = StudyPolicy()
    filings = catalog._conn.execute(  # noqa: SLF001
        "SELECT rcept_no, raw_hash, knowledge_available_at FROM filing_version ORDER BY rcept_no"
    ).fetchall()
    eligible = [
        (str(row["rcept_no"]), str(row["raw_hash"]), str(row["knowledge_available_at"]))
        for row in filings
        if datetime.fromisoformat(str(row["knowledge_available_at"])) <= as_of
    ]
    payload = {
        "agent_mode": agent_mode,
        "as_of": as_of.isoformat(),
        "code_revision": CODE_REVISION,
        "filings": eligible,
        "imports": [
            (dataset_id, manifest.source_manifest_sha256, [(part.relative_path.as_posix(), part.sha256) for part in manifest.parts])
            for dataset_id, manifest in sorted(manifests.items())
        ],
        "index_manifest_hash": index_manifest.manifest_hash,
        "study_policy": {
            "estimation_start": study_policy.estimation_start,
            "estimation_end": study_policy.estimation_end,
            "min_pairs": study_policy.min_pairs,
            "horizons": study_policy.horizons,
            "version": study_policy.version,
        },
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return f"{base_run_id}-{digest[:16]}"


def _require_aware(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")


def _read_import_manifests(data_root: Path) -> dict[str, ImportManifest]:
    manifests: dict[str, ImportManifest] = {}
    imports_root = checked_local_path(data_root, PurePosixPath("imports"))
    if not imports_root.is_dir():
        return manifests
    for child in sorted(imports_root.iterdir()):
        manifest_path = checked_data_path(data_root, child / "manifest.json")
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
    return manifests


def _load_previous_manifest(data_root: Path) -> IndexManifest | None:
    manifest_dir = checked_local_path(data_root, PurePosixPath("krx/manifests"))
    if not manifest_dir.is_dir():
        return None
    names = sorted(
        path.name for path in manifest_dir.glob("*.json") if path.is_file() and not path.is_symlink()
    )
    if not names:
        return None
    combined: dict[tuple[str, date], str] = {}
    for name in names:
        manifest = load_index_manifest(data_root, name[: -len(".json")])
        for key, digest in manifest.entries.items():
            if key in combined and combined[key] != digest:
                raise ValueError(f"conflicting historical index manifests for {key[0]} {key[1]}")
            combined[key] = digest
    if not combined:
        return None
    return merge_index_manifest(None, [(market, session, digest) for (market, session), digest in combined.items()], data_root)


def _artifact_count(catalog: Catalog) -> int:
    rows = catalog._conn.execute("SELECT COUNT(*) AS n FROM raw_artifact").fetchone()  # noqa: SLF001
    return int(rows["n"])


def _atomic_write_bytes(data_root: Path, target: Path, payload: bytes) -> None:
    target = checked_data_path(data_root, target)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise ValueError(f"refusing symlinked report destination: {target}")
    if target.is_file():
        if target.read_bytes() != payload:
            raise ValueError(f"refusing to overwrite immutable report: {target}")
        return
    partial = checked_data_path(data_root, target.with_name(target.name + ".partial"))
    try:
        partial.write_bytes(payload)
        os.link(partial, target)
        partial.unlink()
    except FileExistsError as exc:
        if target.is_file() and target.read_bytes() == payload:
            partial.unlink()
            return
        raise ValueError(f"refusing to overwrite immutable report: {target}") from exc
    except BaseException:
        try:
            if partial.is_symlink() or partial.is_file():
                partial.unlink()
        except OSError:
            pass
        raise


def _try_dart_client() -> Any | None:
    import os as _os

    key = api_keys_from_environ(_os.environ)
    if not key:
        return None
    try:
        import httpx

        from src.integrations.dart import DartClient

        return DartClient(key, httpx.Client(timeout=30.0))
    except ValueError:
        return None


def _try_krx_client() -> Any | None:
    import os as _os

    key = _os.environ.get("KRX_OPENAPI_KEY") or _os.environ.get("KRX_API_KEY", "")
    if not key:
        return None
    try:
        import httpx

        from src.integrations.krx_index import KrxIndexClient

        return KrxIndexClient(key, httpx.Client(timeout=30.0))
    except ValueError:
        return None


def _try_agent_clients() -> tuple[LocalModelClient | None, AgentPolicy | None]:
    import os as _os

    base_url = _os.environ.get("AGENT_BASE_URL", "")
    if not base_url:
        return None, None
    try:
        import httpx

        from src.agent.local_model import LlamaCppClient

        model_name = _os.environ.get("AGENT_MODEL", DEFAULT_AGENT_MODEL)
        client: LocalModelClient = LlamaCppClient(
            base_url, model_name, DEFAULT_AGENT_TIMEOUT_SECONDS, httpx.Client(), schemas=AGENT_SCHEMAS
        )
        return client, AgentPolicy(3, DEFAULT_AGENT_TIMEOUT_SECONDS, DEFAULT_PROMPT_VERSION, model_id=model_name)
    except ValueError:
        return None, None


def _agent_fallback_statuses(memo: Any, policy_version: str, trace: tuple[str, ...], status: str) -> Any:
    from src.research.memo import ResearchMemo

    statuses = tuple(sorted(set(memo.statuses) | {status, f"AGENT_PROMPT:{policy_version}"} | set(trace)))
    return ResearchMemo(
        event_id=memo.event_id,
        anchor_rcept_no=memo.anchor_rcept_no,
        active_rcept_no=memo.active_rcept_no,
        as_of=memo.as_of,
        facts=memo.facts,
        metrics=memo.metrics,
        claims=memo.claims,
        evidence=memo.evidence,
        statuses=statuses,
        manifest_hash=memo.manifest_hash,
    )


def next_daily_run(now: datetime, publish_time_kst: time) -> datetime:
    """Return the next local 18:30 KST batch instant after now for an optional local scheduler."""
    _require_aware(now, "now")
    kst_now = now.astimezone(KST)
    candidate = datetime.combine(kst_now.date(), publish_time_kst, tzinfo=KST)
    if candidate <= kst_now:
        candidate = datetime.combine(kst_now.date() + timedelta(days=1), publish_time_kst, tzinfo=KST)
    return candidate


def run_daily_batch(
    policy: BatchPolicy,
    data_root: Path,
    as_of: datetime,
    agent_mode: bool = False,
    *,
    dart_client: Any | None = None,
    krx_client: Any | None = None,
    agent_model: LocalModelClient | None = None,
    agent_policy: AgentPolicy | None = None,
) -> BatchSummary:
    """Collect newly observable DART receipts and KRX index days, normalize eligible events, build local research memos and publish complete runs atomically. Resume from durable checkpoints after source or model failure without duplicate events or partial reports."""
    _require_aware(as_of, "as_of")
    data_root.mkdir(parents=True, exist_ok=True)
    if not data_root.is_dir():
        raise ValueError(f"missing project data root: {data_root}")

    base_run_id = _run_id(policy, as_of)
    snapshot_id = _collection_snapshot(data_root, base_run_id)
    failures: list[str] = []

    recheck_start = policy.dart_end - timedelta(days=policy.recheck_days) if policy.recheck_days else policy.dart_end
    if recheck_start < policy.dart_start:
        recheck_start = policy.dart_start
    kst_now = as_of.astimezone(KST)
    krx_end = kst_now.date()
    krx_start = min(policy.dart_start, recheck_start)

    windows: list[str] = [f"dart:{policy.dart_start.isoformat()}:{policy.dart_end.isoformat()}"]
    if (recheck_start, policy.dart_end) != (policy.dart_start, policy.dart_end):
        windows.append(f"dart-recheck:{recheck_start.isoformat()}:{policy.dart_end.isoformat()}")
    if krx_end >= krx_start:
        windows.append(f"krx:{krx_start.isoformat()}:{krx_end.isoformat()}")

    catalog = Catalog(data_root / "catalog.sqlite")
    event_store = EventStore(catalog)
    before = _artifact_count(catalog)

    # Stage 1: raw registration (DART pages + receipt ZIPs, idempotent via checkpoints).
    resolved_dart = dart_client if dart_client is not None else _try_dart_client()
    if resolved_dart is not None:
        from src.data.dart_ingest import collect_buyback_window

        manifests = _read_import_manifests(data_root)
        try:
            lake_for_collect = LocalLake(data_root, manifests)
        except ValueError as exc:
            failures.append(f"DART_LAKE_UNAVAILABLE:{exc}")
            lake_for_collect = None
        if lake_for_collect is not None:
            for wstart, wend in [(policy.dart_start, policy.dart_end), (recheck_start, policy.dart_end)]:
                if wstart > wend:
                    continue
                try:
                    dart_summary = collect_buyback_window(
                        resolved_dart,
                        catalog,
                        lake_for_collect,
                        wstart,
                        wend,
                        data_root,
                        snapshot_id,
                        "LIVE",
                        DocumentLimits(),
                        event_store,
                    )
                except (ValueError, OSError) as exc:
                    failures.append(f"DART_WINDOW_FAILED:{wstart.isoformat()}:{exc}")
                    break
                if dart_summary.failed_receipts:
                    failures.append(f"DART_WINDOW_INCOMPLETE:{wstart.isoformat()}:{wend.isoformat()}")
                    break

    # Stage 2: session-bounded KRX collection, merged into a new cumulative manifest.
    previous = _load_previous_manifest(data_root)
    resolved_krx = krx_client if krx_client is not None else _try_krx_client()
    krx_manifests = _read_import_manifests(data_root)
    try:
        krx_lake = LocalLake(data_root, krx_manifests)
    except ValueError as exc:
        failures.append(f"KRX_WINDOW_FAILED:{exc}")
        index_manifest = merge_index_manifest(previous, [], data_root)
    else:
        if resolved_krx is not None and krx_end >= krx_start:
            from src.data.krx_ingest import collect_index_sessions

            try:
                krx_summary = collect_index_sessions(
                    resolved_krx, catalog, krx_lake, previous, krx_start, krx_end, data_root, snapshot_id
                )
            except (ValueError, OSError) as exc:
                failures.append(f"KRX_WINDOW_FAILED:{exc}")
                index_manifest = merge_index_manifest(previous, [], data_root)
            else:
                index_manifest = krx_summary.manifest
        else:
            index_manifest = merge_index_manifest(previous, [], data_root)

    # Stages 3-6: verified parse/link (durable in catalog/event store) -> PIT analysis ->
    # baseline/optional AI -> validation -> atomic publication.
    manifests = _read_import_manifests(data_root)
    try:
        lake = LocalLake(data_root, manifests)
    except ValueError as exc:
        failures.append(f"LAKE_UNAVAILABLE:{exc}")
        lake = LocalLake(data_root, {})
    collection_failed = bool(failures)
    if collection_failed:
        failure_payload = json.dumps(
            {"failures": sorted(failures), "snapshot_id": snapshot_id, "artifacts_registered": _artifact_count(catalog) - before},
            sort_keys=True,
        ).encode()
        run_id = f"{snapshot_id}-failed-{hashlib.sha256(failure_payload).hexdigest()[:12]}"
    else:
        completion = checked_local_path(data_root, PurePosixPath("collections") / snapshot_id / "complete.json")
        _atomic_write_bytes(data_root, completion, b'{"status":"COMPLETE"}\n')
        run_id = _source_run_id(base_run_id, catalog, manifests, index_manifest, as_of, agent_mode)
        if agent_mode:
            run_id = f"{run_id}-a{hashlib.sha256(snapshot_id.encode()).hexdigest()[:8]}"
        existing_batch = checked_local_path(data_root, PurePosixPath("batch") / run_id / "manifest.json")
        if existing_batch.is_file():
            raw = existing_batch.read_bytes()
            document = json.loads(raw.decode("utf-8"))
            if document.get("run_id") != run_id or document.get("failures"):
                raise ValueError(f"conflicting existing batch run: {run_id}")
            return BatchSummary(
                run_id=run_id,
                collection_windows=tuple(str(item) for item in document["collection_windows"]),
                artifacts_registered=int(document["artifacts_registered"]),
                events_linked=int(document["events_linked"]),
                memos_published=int(document["memos_published"]),
                memos_withheld=int(document["memos_withheld"]),
                failures=(),
                manifest_hash=hashlib.sha256(raw).hexdigest(),
            )
    financial = FinancialEvidence(data_root, lake)
    index_store = IndexStore(catalog, data_root, index_manifest)
    study_policy = StudyPolicy()

    try:
        prior_events = event_store.list_prior_events(as_of)
    except (ValueError, OSError) as exc:
        failures.append(f"EVENT_LINK_FAILED:{exc}")
        prior_events = ()

    resolved_agent = agent_model
    resolved_policy = agent_policy
    if agent_mode and (resolved_agent is None or resolved_policy is None):
        env_model, env_policy = _try_agent_clients()
        if resolved_agent is None:
            resolved_agent = env_model
        if resolved_policy is None:
            resolved_policy = env_policy

    published = 0
    withheld = 0
    event_hashes: list[str] = []
    for _link, original in sorted(
        prior_events if not collection_failed else (),
        key=lambda pair: (pair[1].receipt_date.isoformat(), pair[1].rcept_no),
    ):
        anchor = original.rcept_no
        try:
            context = build_research_context(
                catalog, event_store, lake, financial, index_store, anchor, as_of, study_policy
            )
        except ResearchUnavailable:
            withheld += 1
            continue
        except (ValueError, OSError):
            withheld += 1
            failures.append(f"MEMO_WITHHELD:{anchor}")
            continue
        proof = build_analogue_proof(context)
        memo_run_id = f"{context.event.event_id}-{snapshot_id}"
        relative_dir = PurePosixPath(f"reports/{context.event.event_id}/{memo_run_id}")
        analogue_ref = (
            proof_reference(data_root / relative_dir.as_posix(), proof) if proof is not None else None
        )
        baseline = build_baseline_memo(context, analogue_ref=analogue_ref)
        memo = baseline
        if agent_mode:
            if resolved_agent is None or resolved_policy is None:
                memo = _agent_fallback_statuses(baseline, DEFAULT_PROMPT_VERSION, (), "AGENT_UNAVAILABLE")
                if f"AGENT_UNAVAILABLE:{anchor}" not in failures:
                    failures.append(f"AGENT_UNAVAILABLE:{anchor}")
            else:
                memo = AgentRunner().run(context, baseline, resolved_agent, resolved_policy)
                if "AGENT_UNAVAILABLE" in memo.statuses or "AGENT_REJECTED" in memo.statuses:
                    failures.append(f"AGENT_FALLBACK:{anchor}")
        try:
            run_manifest = {
                "batch_run_id": run_id,
                "collection_snapshot_id": snapshot_id,
                "event_id": context.event.event_id,
                "index_manifest_hash": index_manifest.manifest_hash,
                "manifest_hash": memo.manifest_hash,
                "memo_run_id": memo_run_id,
                "proof_sha256": proof.sha256 if proof is not None else None,
                "run_id": snapshot_id,
            }
            publish_research_run(data_root, memo_run_id, memo, proof, run_manifest)
        except (ValueError, OSError):
            withheld += 1
            failures.append(f"PUBLISH_FAILED:{anchor}")
            continue
        published += 1
        event_hashes.append(memo.manifest_hash)

    artifacts_registered = _artifact_count(catalog) - before
    try:
        events_linked = len(event_store.list_prior_events(as_of))
    except (ValueError, OSError):
        events_linked = 0

    batch_manifest = {
        "artifacts_registered": artifacts_registered,
        "as_of": as_of.isoformat(),
        "collection_windows": sorted(windows),
        "events_linked": events_linked,
        "failures": sorted(set(failures)),
        "index_manifest_hash": index_manifest.manifest_hash,
        "memos_published": published,
        "memos_withheld": withheld,
        "memo_manifest_hashes": sorted(event_hashes),
        "policy": {
            "dart_end": policy.dart_end.isoformat(),
            "dart_start": policy.dart_start.isoformat(),
            "max_attempts": policy.max_attempts,
            "publish_time_kst": policy.publish_time_kst.isoformat(),
            "recheck_days": policy.recheck_days,
        },
        "run_id": run_id,
    }
    batch_payload = (json.dumps(batch_manifest, sort_keys=True, indent=2) + "\n").encode("utf-8")
    batch_path = data_root / "batch" / run_id / "manifest.json"
    _atomic_write_bytes(data_root, batch_path, batch_payload)
    manifest_hash = hashlib.sha256(batch_payload).hexdigest()

    return BatchSummary(
        run_id=run_id,
        collection_windows=tuple(windows),
        artifacts_registered=artifacts_registered,
        events_linked=events_linked,
        memos_published=published,
        memos_withheld=withheld,
        failures=tuple(sorted(set(failures))),
        manifest_hash=manifest_hash,
    )


__all__ = [
    "BatchPolicy",
    "BatchSummary",
    "next_daily_run",
    "run_daily_batch",
]
