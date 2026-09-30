"""Frozen replay and stratified evaluation over pinned local inputs."""

from __future__ import annotations

import hashlib
import json
import resource
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath

from src.agent.workflow import AgentPolicy, AgentRunner, LocalModelClient
from src.data.catalog import Catalog
from src.data.event_store import EventStore
from src.data.financial_evidence import FinancialEvidence
from src.data.imports import ImportManifest, ImportPart
from src.data.index_store import IndexStore, load_index_manifest
from src.data.local_lake import LocalLake
from src.data.local_paths import checked_local_path
from src.research.analogue_proof import build_analogue_proof
from src.research.context import ResearchContext, ResearchUnavailable, build_research_context
from src.research.event_study import StudyPolicy
from src.research.memo import build_baseline_memo
from src.research.publication import proof_reference, validate_memo_evidence

CODE_REVISION = "eval-replay-v2"
_HOLDOUT_YEAR = 2026
_CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True, slots=True)
class ReplayCase:
    """Frozen reviewed replay label pinned to exact local inputs."""

    case_id: str
    rcept_no: str
    as_of: datetime
    snapshot_id: str
    index_manifest_hash: str
    expected_source_hashes: tuple[str, ...]
    expected_facts: Mapping[str, str | None]
    expected_citations: Mapping[str, str]
    cohort: str


@dataclass(frozen=True, slots=True)
class ReplayResult:
    """Frozen separate fact, citation, tool and resource outcomes for one case."""

    case_id: str
    manifest_hash: str
    fact_matches: int
    fact_errors: int
    citation_matches: int
    citation_errors: int
    tool_valid: bool
    status: str
    elapsed_ms: int
    peak_rss_mib: float
    agent_status: str = ""
    agent_reason: str = ""
    agent_claim_count: int = 0


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    """Frozen aggregate quality with explicit denominators beside every rate."""

    run_id: str
    case_count: int
    cohort_counts: Mapping[str, int]
    parser_precision: float
    parser_precision_num: int
    parser_precision_den: int
    parser_coverage: float
    parser_coverage_num: int
    parser_coverage_den: int
    citation_precision: float
    citation_precision_num: int
    citation_precision_den: int
    tool_valid_rate: float
    tool_valid_num: int
    tool_valid_den: int
    future_leak_count: int
    refusal_rate: float
    refusal_num: int
    refusal_den: int
    latency_ms: float
    peak_rss_mib: float
    source_manifest_hashes: tuple[str, ...]
    agent_ok_num: int = 0
    agent_rejected_num: int = 0
    agent_unavailable_num: int = 0
    agent_den: int = 0
    agent_reason_counts: Mapping[str, int] = field(default_factory=dict)
    agent_claims_per_ok: float = 0.0
    agent_model_id: str = ""


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(_CHUNK_SIZE), b""):
            digest.update(block)
    return digest.hexdigest()


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _peak_rss_mib() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def _rate(num: int, den: int) -> float:
    return num / den if den else 0.0


def _artifact_intact(catalog: Catalog, data_root: Path, digest: str) -> bool:
    try:
        path = catalog.get_artifact_path(digest)
        if path is None:
            return False
        target = checked_local_path(data_root, path)
    except ValueError:
        return False
    if target.is_symlink() or not target.is_file():
        return False
    return _sha256_of(target) == digest.lower()


def _read_import_manifests(data_root: Path) -> dict[str, ImportManifest]:
    manifests: dict[str, ImportManifest] = {}
    imports_root = checked_local_path(data_root, PurePosixPath("imports"))
    if not imports_root.is_dir():
        return manifests
    for child in sorted(imports_root.iterdir()):
        manifest_path = checked_local_path(data_root, PurePosixPath("imports") / child.name / "manifest.json")
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


def _source_manifest_hashes(data_root: Path, cases: Sequence[ReplayCase]) -> tuple[str, ...]:
    manifests = _read_import_manifests(data_root)
    return tuple(
        sorted({item.source_manifest_sha256 for item in manifests.values()} | {case.index_manifest_hash for case in cases})
    )


def _detect_future_leak(context: ResearchContext, case: ReplayCase) -> bool:
    if context.filing.knowledge_available_at > case.as_of:
        return True
    return any(fact.available_at > case.as_of for fact in context.financial_facts)


def _empty_result(case_id: str, status: str, started: float) -> ReplayResult:
    return ReplayResult(case_id, "", 0, 0, 0, 0, False, status, _elapsed_ms(started), _peak_rss_mib())


def _agent_outcome(statuses: Sequence[str]) -> tuple[str, str]:
    reason = ""
    for status in statuses:
        if status.startswith("AGENT_REASON:"):
            reason = status.split(":", 1)[1]
            break
    if "AGENT_OK" in statuses:
        return "AGENT_OK", ""
    if "AGENT_REJECTED" in statuses:
        return "AGENT_REJECTED", reason
    if "AGENT_UNAVAILABLE" in statuses:
        return "AGENT_UNAVAILABLE", reason
    return "", ""


def _replay(
    case: ReplayCase,
    data_root: Path,
    model: LocalModelClient | None,
    agent_policy: AgentPolicy | None,
) -> tuple[ReplayResult, str | None]:
    started = time.perf_counter()
    if case.as_of.tzinfo is None or case.as_of.utcoffset() is None:
        raise ValueError("case as_of must be timezone-aware")
    catalog = Catalog(data_root / "catalog.sqlite")
    for digest in case.expected_source_hashes:
        if not _artifact_intact(catalog, data_root, digest):
            return _empty_result(case.case_id, "SOURCE_CHANGED", started), None
    lake = LocalLake(data_root, _read_import_manifests(data_root))
    financial = FinancialEvidence(data_root, lake)
    event_store = EventStore(catalog)
    try:
        pinned_index = load_index_manifest(data_root, case.index_manifest_hash)
    except ValueError:
        return _empty_result(case.case_id, "SOURCE_CHANGED", started), None
    if any(not _artifact_intact(catalog, data_root, digest) for digest in set(pinned_index.entries.values())):
        return _empty_result(case.case_id, "SOURCE_CHANGED", started), None
    index_store = IndexStore(catalog, data_root, pinned_index)
    try:
        context = build_research_context(
            catalog, event_store, lake, financial, index_store, case.rcept_no, case.as_of, StudyPolicy()
        )
    except ResearchUnavailable:
        return _empty_result(case.case_id, "REFUSED", started), None
    if case.snapshot_id not in context.snapshot_ids:
        return _empty_result(case.case_id, "SOURCE_CHANGED", started), None
    try:
        proof = build_analogue_proof(context)
        proof_ref = (
            proof_reference(data_root / "reports" / context.event.event_id / f"eval-{case.case_id}", proof)
            if proof is not None
            else None
        )
        baseline = build_baseline_memo(context, analogue_ref=proof_ref)
        if proof is not None and not any(ref.id == "tool-analogues" for ref in baseline.evidence):
            return _empty_result(case.case_id, "SOURCE_CHANGED", started), None
        validate_memo_evidence(data_root, baseline, staged_proof=proof)
    except ValueError:
        return _empty_result(case.case_id, "SOURCE_CHANGED", started), None
    if model is not None and agent_policy is not None:
        memo = AgentRunner().run(context, baseline, model, agent_policy)
        tool_valid = "AGENT_REJECTED" not in memo.statuses
        agent_status, agent_reason = _agent_outcome(memo.statuses)
        agent_claim_count = len(memo.claims) - len(baseline.claims)
    else:
        memo = baseline
        tool_valid = True
        agent_status, agent_reason, agent_claim_count = "", "", 0
    leak = _detect_future_leak(context, case)
    fact_matches = 0
    fact_errors = 0
    for key, wanted in case.expected_facts.items():
        if memo.facts.get(key) == wanted:
            fact_matches += 1
        else:
            fact_errors += 1
    evidence_ids = {item.id for item in memo.evidence}
    citation_matches = 0
    citation_errors = 0
    for wanted_id in case.expected_citations.values():
        if wanted_id in evidence_ids:
            citation_matches += 1
        else:
            citation_errors += 1
    status = "FUTURE_LEAK" if leak else "OK"
    return (
        ReplayResult(
            case.case_id,
            memo.manifest_hash,
            fact_matches,
            fact_errors,
            citation_matches,
            citation_errors,
            tool_valid,
            status,
            _elapsed_ms(started),
            _peak_rss_mib(),
            agent_status,
            agent_reason,
            agent_claim_count,
        ),
        memo.event_id,
    )


def replay_case(
    case: ReplayCase,
    data_root: Path,
    model: LocalModelClient | None = None,
    agent_policy: AgentPolicy | None = None,
) -> ReplayResult:
    """Rebuild one historical memo from pinned local inputs and report separate fact, citation, tool and resource outcomes. Reject changed source hashes and future data rather than silently refreshing a case."""
    result, _ = _replay(case, data_root, model, agent_policy)
    return result


def evaluate_cases(
    cases: Sequence[ReplayCase],
    data_root: Path,
    model: LocalModelClient | None = None,
    agent_policy: AgentPolicy | None = None,
) -> EvaluationReport:
    """Aggregate overall and stratified quality with explicit denominators, coverage and refusal rates. Keep 2026 holdout events and all versions of one event outside 2024-2025 development cohorts.

    When a model is supplied the report separates narrative outcomes (`AGENT_OK`, `AGENT_REJECTED`, `AGENT_UNAVAILABLE`) and rejection reasons from tool validity, and the run identity includes the model id.
    """
    replayed = [_replay(case, data_root, model, agent_policy) for case in cases]
    results = [result for result, _ in replayed]
    by_event: dict[str, list[int]] = {}
    for index, item in enumerate(replayed):
        if item[1] is not None:
            by_event.setdefault(item[1], []).append(index)
    effective = [case.cohort for case in cases]
    for members in by_event.values():
        if any(cases[i].cohort == "holdout" or cases[i].as_of.year >= _HOLDOUT_YEAR for i in members):
            for i in members:
                effective[i] = "holdout"
    cohort_counts: dict[str, int] = {}
    for cohort in effective:
        cohort_counts[cohort] = cohort_counts.get(cohort, 0) + 1
    fact_matches = sum(result.fact_matches for result in results)
    fact_errors = sum(result.fact_errors for result in results)
    compared = fact_matches + fact_errors
    expected_total = sum(len(case.expected_facts) for case in cases)
    citation_matches = sum(result.citation_matches for result in results)
    citation_errors = sum(result.citation_errors for result in results)
    cited = citation_matches + citation_errors
    known = [index for index, item in enumerate(replayed) if item[1] is not None]
    tool_num = sum(1 for index in known if results[index].tool_valid)
    refusal_num = sum(1 for result in results if result.status == "REFUSED")
    agent_results = [result for result in results if result.agent_status]
    agent_ok_num = sum(1 for result in agent_results if result.agent_status == "AGENT_OK")
    agent_rejected_num = sum(1 for result in agent_results if result.agent_status == "AGENT_REJECTED")
    agent_unavailable_num = sum(1 for result in agent_results if result.agent_status == "AGENT_UNAVAILABLE")
    agent_den = len(agent_results)
    reason_counts: dict[str, int] = {}
    for result in agent_results:
        if result.agent_reason:
            reason_counts[result.agent_reason] = reason_counts.get(result.agent_reason, 0) + 1
    ok_claims = sum(result.agent_claim_count for result in agent_results if result.agent_status == "AGENT_OK")
    agent_claims_per_ok = ok_claims / agent_ok_num if agent_ok_num else 0.0
    agent_model_id = agent_policy.model_id if model is not None and agent_policy is not None else ""
    source_manifest_hashes = _source_manifest_hashes(data_root, cases)
    fingerprint = {
        "agent": agent_policy.prompt_version if model is not None and agent_policy is not None else "baseline",
        "model_id": agent_model_id,
        "cases": sorted(
            (case.case_id, case.rcept_no, case.as_of.isoformat(), case.snapshot_id, case.index_manifest_hash)
            for case in cases
        ),
        "code": CODE_REVISION,
        "expected_hashes": sorted({digest for case in cases for digest in case.expected_source_hashes}),
        "sources": sorted(source_manifest_hashes),
        "study_policy": StudyPolicy().version,
    }
    run_id = "eval-" + hashlib.sha256((json.dumps(fingerprint, sort_keys=True, separators=(",", ":")) + "\n").encode()).hexdigest()[:16]
    latency_ms = sum(result.elapsed_ms for result in results) / len(results) if results else 0.0
    peak_rss_mib = max((result.peak_rss_mib for result in results), default=0.0)
    return EvaluationReport(
        run_id=run_id,
        case_count=len(results),
        cohort_counts=cohort_counts,
        parser_precision=_rate(fact_matches, compared),
        parser_precision_num=fact_matches,
        parser_precision_den=compared,
        parser_coverage=_rate(compared, expected_total),
        parser_coverage_num=compared,
        parser_coverage_den=expected_total,
        citation_precision=_rate(citation_matches, cited),
        citation_precision_num=citation_matches,
        citation_precision_den=cited,
        tool_valid_rate=_rate(tool_num, len(known)),
        tool_valid_num=tool_num,
        tool_valid_den=len(known),
        future_leak_count=sum(1 for result in results if result.status == "FUTURE_LEAK"),
        refusal_rate=_rate(refusal_num, len(results)),
        refusal_num=refusal_num,
        refusal_den=len(results),
        latency_ms=latency_ms,
        peak_rss_mib=peak_rss_mib,
        source_manifest_hashes=source_manifest_hashes,
        agent_ok_num=agent_ok_num,
        agent_rejected_num=agent_rejected_num,
        agent_unavailable_num=agent_unavailable_num,
        agent_den=agent_den,
        agent_reason_counts=dict(sorted(reason_counts.items())),
        agent_claims_per_ok=agent_claims_per_ok,
        agent_model_id=agent_model_id,
    )


def _parse_case(entry: object, path: Path) -> ReplayCase:
    if not isinstance(entry, dict):
        raise ValueError(f"invalid eval case entry: {path}")
    try:
        as_of = datetime.fromisoformat(str(entry["as_of"]))
        case = ReplayCase(
            case_id=str(entry["case_id"]),
            rcept_no=str(entry["rcept_no"]),
            as_of=as_of,
            snapshot_id=str(entry["snapshot_id"]),
            index_manifest_hash=str(entry["index_manifest_hash"]),
            expected_source_hashes=tuple(str(item) for item in entry["expected_source_hashes"]),
            expected_facts=dict(entry["expected_facts"]),
            expected_citations=dict(entry["expected_citations"]),
            cohort=str(entry["cohort"]),
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise ValueError(f"invalid eval case entry: {path}") from exc
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError(f"invalid eval case entry: {path}")
    if not case.case_id or not case.rcept_no or not case.cohort:
        raise ValueError(f"invalid eval case entry: {path}")
    if len(case.index_manifest_hash) != 64 or any(char not in "0123456789abcdefABCDEF" for char in case.index_manifest_hash):
        raise ValueError(f"invalid eval case entry: {path}")
    return case


def load_cases(path: Path) -> tuple[ReplayCase, ...]:
    """Load reviewed replay labels from a local versioned file without inventing cases."""
    try:
        document = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"invalid eval cases file: {path}") from exc
    if not isinstance(document, list):
        raise ValueError(f"invalid eval cases file: {path}")
    return tuple(_parse_case(entry, path) for entry in document)


def report_to_dict(report: EvaluationReport) -> dict[str, object]:
    """Return a stable JSON-ready mapping for one evaluation report with exact denominators."""
    return {
        "agent_claims_per_ok": report.agent_claims_per_ok,
        "agent_den": report.agent_den,
        "agent_model_id": report.agent_model_id,
        "agent_ok_num": report.agent_ok_num,
        "agent_reason_counts": {key: report.agent_reason_counts[key] for key in sorted(report.agent_reason_counts)},
        "agent_rejected_num": report.agent_rejected_num,
        "agent_unavailable_num": report.agent_unavailable_num,
        "case_count": report.case_count,
        "citation_precision": report.citation_precision,
        "citation_precision_den": report.citation_precision_den,
        "citation_precision_num": report.citation_precision_num,
        "cohort_counts": {key: report.cohort_counts[key] for key in sorted(report.cohort_counts)},
        "future_leak_count": report.future_leak_count,
        "latency_ms": report.latency_ms,
        "parser_coverage": report.parser_coverage,
        "parser_coverage_den": report.parser_coverage_den,
        "parser_coverage_num": report.parser_coverage_num,
        "parser_precision": report.parser_precision,
        "parser_precision_den": report.parser_precision_den,
        "parser_precision_num": report.parser_precision_num,
        "peak_rss_mib": report.peak_rss_mib,
        "refusal_den": report.refusal_den,
        "refusal_num": report.refusal_num,
        "refusal_rate": report.refusal_rate,
        "run_id": report.run_id,
        "source_manifest_hashes": list(report.source_manifest_hashes),
        "tool_valid_den": report.tool_valid_den,
        "tool_valid_num": report.tool_valid_num,
        "tool_valid_rate": report.tool_valid_rate,
    }


def render_report_markdown(report: EvaluationReport) -> str:
    """Render one evaluation report for human review without recomputing any figure."""
    lines = [
        f"# Evaluation {report.run_id}",
        "",
        f"Cases: {report.case_count}",
        "",
        "## Cohorts",
        "",
    ]
    if report.cohort_counts:
        lines.extend(f"- {key}: {report.cohort_counts[key]}" for key in sorted(report.cohort_counts))
    else:
        lines.append("- none")
    lines.extend(
        (
            "",
            "## Quality",
            "",
            f"- parser_precision: {report.parser_precision} ({report.parser_precision_num}/{report.parser_precision_den})",
            f"- parser_coverage: {report.parser_coverage} ({report.parser_coverage_num}/{report.parser_coverage_den})",
            f"- citation_precision: {report.citation_precision} ({report.citation_precision_num}/{report.citation_precision_den})",
            f"- tool_valid_rate: {report.tool_valid_rate} ({report.tool_valid_num}/{report.tool_valid_den})",
            f"- future_leak_count: {report.future_leak_count}",
            f"- refusal_rate: {report.refusal_rate} ({report.refusal_num}/{report.refusal_den})",
            "",
            "## Agent",
            "",
            f"- model_id: {report.agent_model_id or 'none'}",
            f"- agent_ok: {report.agent_ok_num}/{report.agent_den}",
            f"- agent_rejected: {report.agent_rejected_num}/{report.agent_den}",
            f"- agent_unavailable: {report.agent_unavailable_num}/{report.agent_den}",
            f"- agent_claims_per_ok: {report.agent_claims_per_ok}",
            (
                "- agent_reasons: none"
                if not report.agent_reason_counts
                else "- agent_reasons:\n"
                + "\n".join(
                    f"  - {key}: {report.agent_reason_counts[key]}" for key in sorted(report.agent_reason_counts)
                )
            ),
            "",
            "## Resources",
            "",
            f"- latency_ms: {report.latency_ms}",
            f"- peak_rss_mib: {report.peak_rss_mib}",
            f"- source_manifest_hashes: {', '.join(report.source_manifest_hashes)}",
            "",
        )
    )
    return "\n".join(lines)


__all__ = [
    "CODE_REVISION",
    "EvaluationReport",
    "ReplayCase",
    "ReplayResult",
    "evaluate_cases",
    "load_cases",
    "render_report_markdown",
    "replay_case",
    "report_to_dict",
]
