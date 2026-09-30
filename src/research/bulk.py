"""Bulk publication of evidence-backed memos for every linked event."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from src.data.catalog import Catalog
from src.data.event_store import EventStore
from src.data.financial_evidence import FinancialEvidence
from src.data.index_store import IndexStore
from src.data.local_lake import LocalLake
from src.research.analogue_proof import build_analogue_proof
from src.research.context import ResearchUnavailable, build_research_context
from src.research.event_study import StudyPolicy
from src.research.memo import build_baseline_memo
from src.research.publication import proof_reference, publish_research_run

_KST = ZoneInfo("Asia/Seoul")


@dataclass(frozen=True, slots=True)
class BulkSummary:
    """Auditable outcome of one bulk publication; every linked event lands in exactly one bucket."""

    linked: int
    published: int
    unavailable: dict[str, int] = field(default_factory=dict)
    failed: tuple[str, ...] = ()
    status_counts: dict[str, int] = field(default_factory=dict)


def publish_all_memos(
    data_root: Path,
    catalog: Catalog,
    event_store: EventStore,
    lake: LocalLake,
    financial: FinancialEvidence,
    index_store: IndexStore,
    as_of: datetime,
    policy: StudyPolicy,
    limit: int | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> BulkSummary:
    """Build and publish one memo per linked event through the verified publication path.

    Run ids are ``<active receipt>-<as_of stamp>``, so a rerun with the same instant is idempotent and a new
    instant never overwrites an earlier run. One event's failure is recorded and does not stop the others.
    """
    events = event_store.list_prior_events(as_of)
    selected = events if limit is None else events[:limit]
    stamp = as_of.astimezone(_KST).strftime("%Y%m%d-%H%M%S%z").replace("+", "p")
    published = 0
    unavailable: dict[str, int] = {}
    failed: list[str] = []
    status_counts: dict[str, int] = {}
    for position, (_link, original) in enumerate(selected, start=1):
        try:
            context = build_research_context(
                catalog, event_store, lake, financial, index_store, original.rcept_no, as_of, policy
            )
            run_id = f"{context.active_rcept_no}-{stamp}"
            run_dir = data_root / "reports" / context.event.event_id / run_id
            proof = build_analogue_proof(context)
            analogue_ref = proof_reference(run_dir, proof) if proof is not None else None
            memo = build_baseline_memo(context, analogue_ref=analogue_ref)
            manifest = {
                "event_id": context.event.event_id,
                "index_manifest_hash": index_store.manifest.manifest_hash,
                "manifest_hash": memo.manifest_hash,
                "proof_sha256": proof.sha256 if proof is not None else None,
                "run_id": run_id,
            }
            publish_research_run(data_root, run_id, memo, proof, manifest)
        except ResearchUnavailable as exc:
            unavailable[exc.reason_code] = unavailable.get(exc.reason_code, 0) + 1
        except (ValueError, OSError) as exc:
            failed.append(f"{original.rcept_no}: {exc}")
        else:
            published += 1
            for status in memo.statuses or ("OK",):
                status_counts[status] = status_counts.get(status, 0) + 1
        if progress is not None and (position % 25 == 0 or position == len(selected)):
            progress(position, len(selected))
    return BulkSummary(len(selected), published, dict(sorted(unavailable.items())), tuple(failed), dict(sorted(status_counts.items())))


__all__ = ["BulkSummary", "publish_all_memos"]
