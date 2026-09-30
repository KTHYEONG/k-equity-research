"""Point-in-time research context assembly."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import PurePosixPath
from typing import Literal, cast
from zoneinfo import ZoneInfo

from src.core.buyback_document import ParsedBuyback
from src.core.revisions import EventLink
from src.data.catalog import Catalog, FilingVersion
from src.data.disclosure_context import verified_event_confound
from src.data.event_store import EventStore
from src.data.financial_evidence import FinancialEvidence, VerifiedFinancialFact
from src.data.index_store import IndexStore
from src.data.local_lake import LocalLake, MarketBar, SecurityMatch
from src.integrations.krx_index import IndexBar
from src.research.comparables import ComparableSet, PastEventProfile, select_comparables
from src.research.event_study import StudyPolicy, StudyResult, study_buyback
from src.research.materiality import MaterialityResult, calculate_materiality

_KST = ZoneInfo("Asia/Seoul")
_HISTORY_LOOKBACK = timedelta(days=500)
_FEATURE_SESSIONS = 20
_FINANCIAL_FACTS = frozenset(
    {"assets", "capex", "cash", "debt", "equity", "gross_profit", "net_income", "operating_cash_flow", "operating_profit", "sales"}
)


class ResearchUnavailable(Exception):  # noqa: N818 - spec-mandated boundary name
    """Typed boundary failure for missing local inputs or unresolved identity."""

    def __init__(self, reason_code: str, message: str = "") -> None:
        self.reason_code = reason_code
        super().__init__(f"{reason_code}: {message}" if message else reason_code)


@dataclass(frozen=True, slots=True)
class ResearchContext:
    """Frozen source-backed research view for one filing receipt."""

    anchor_rcept_no: str
    active_rcept_no: str
    event: EventLink
    filing: FilingVersion
    parsed: ParsedBuyback
    security: SecurityMatch
    financial_facts: tuple[VerifiedFinancialFact, ...]
    stock_bars: tuple[MarketBar, ...]
    index_bars: tuple[IndexBar, ...]
    materiality: MaterialityResult
    study: StudyResult
    comparables: ComparableSet
    as_of: datetime
    index_manifest_hash: str
    snapshot_ids: tuple[str, ...]
    source_hashes: tuple[str, ...]
    artifact_paths: Mapping[str, PurePosixPath]
    confounding_receipts: tuple[tuple[str, str, str], ...] = ()
    company_name: str | None = None
    company_name_source_hash: str | None = None


def _require_aware(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")


def _read_link(catalog: Catalog, anchor_rcept_no: str) -> EventLink | None:
    try:
        connection = sqlite3.connect(str(catalog.db_path))
    except sqlite3.Error:
        return None
    try:
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute("SELECT event_id, rcept_nos, status FROM event_link").fetchall()
        except sqlite3.Error:
            return None
        for row in rows:
            try:
                numbers = tuple(str(value) for value in json.loads(str(row["rcept_nos"])))
            except ValueError:
                continue
            if anchor_rcept_no in numbers:
                status = str(row["status"])
                if status not in ("LINKED", "UNRESOLVED_LINK", "WITHDRAWN"):
                    return None
                return EventLink(
                    event_id=str(row["event_id"]),
                    rcept_nos=numbers,
                    status=cast("Literal['LINKED', 'UNRESOLVED_LINK', 'WITHDRAWN']", status),
                )
        return None
    finally:
        connection.close()


def _snapshot_ids(catalog: Catalog, source_hashes: tuple[str, ...]) -> tuple[str, ...]:
    try:
        connection = sqlite3.connect(str(catalog.db_path))
    except sqlite3.Error:
        return ()
    try:
        try:
            snapshots: set[str] = set()
            for start in range(0, len(source_hashes), 500):
                chunk = source_hashes[start : start + 500]
                placeholders = ",".join("?" for _ in chunk)
                rows = connection.execute(
                    f"SELECT sha256, MIN(snapshot_id) FROM raw_artifact WHERE sha256 IN ({placeholders}) GROUP BY sha256",  # noqa: S608 - placeholders are generated only from the fixed chunk length
                    chunk,
                ).fetchall()
                snapshots.update(str(row[1]) for row in rows if row[1])
        except sqlite3.Error:
            return ()
        return tuple(sorted(snapshots))
    finally:
        connection.close()


def _eligible_versions(catalog: Catalog, event: EventLink, as_of: datetime) -> list[FilingVersion]:
    versions: list[FilingVersion] = []
    for rcept_no in event.rcept_nos:
        filing = catalog.get_filing_asof(rcept_no, as_of)
        if filing is not None:
            versions.append(filing)
    return versions


def _prior_sessions(lake: LocalLake, filing_date: date, count: int) -> list[date]:
    sessions: list[date] = []
    cursor = filing_date
    for _ in range(count):
        previous = lake.previous_session(cursor)
        if previous is None:
            break
        sessions.append(previous)
        cursor = previous
    return sessions


def _withheld_study(event: EventLink, filing: FilingVersion, as_of: datetime, policy: StudyPolicy) -> StudyResult:
    return StudyResult(
        event_id=event.event_id,
        active_rcept_no=filing.rcept_no,
        as_of=as_of,
        first_safe_session=None,
        intraday_excess=None,
        model_alpha=None,
        model_beta=None,
        horizon_car=dict.fromkeys(policy.horizons, None),
        status="WITHDRAWN",
        reasons=("WITHDRAWN",),
        evidence_hashes=(),
        policy_version=policy.version,
        omitted_sessions=0,
    )


def _pending_when_session_known(
    lake: LocalLake,
    event: EventLink,
    filing: FilingVersion,
    as_of: datetime,
    policy: StudyPolicy,
    study: StudyResult,
) -> StudyResult:
    """Keep a validated next session as pending when outcome bars are not yet available."""
    coming = lake.next_session(filing.receipt_date)
    if coming is None or as_of.date() > coming:
        return study
    return StudyResult(
        event_id=event.event_id,
        active_rcept_no=filing.rcept_no,
        as_of=as_of,
        first_safe_session=coming,
        intraday_excess=None,
        model_alpha=None,
        model_beta=None,
        horizon_car=dict.fromkeys(policy.horizons, None),
        status="PENDING",
        reasons=("PENDING", "CONFOUND_CHECK_INCOMPLETE"),
        evidence_hashes=(),
        policy_version=policy.version,
        omitted_sessions=study.omitted_sessions,
    )


def _build_past_profiles(
    catalog: Catalog,
    event_store: EventStore,
    lake: LocalLake,
    index_store: IndexStore,
    target_event_id: str,
    filing_date: date,
    as_of: datetime,
    policy: StudyPolicy,
) -> list[PastEventProfile]:
    cap = datetime.combine(filing_date, time.min).replace(tzinfo=_KST)
    prior_as_of = min(as_of, cap)
    profiles: list[PastEventProfile] = []
    for link, original in event_store.list_prior_events(as_of):
        if link.event_id == target_event_id or original.receipt_date >= filing_date:
            continue
        profile = _profile_prior_event(
            catalog, event_store, lake, index_store, link, original, prior_as_of, policy
        )
        if profile is not None:
            profiles.append(profile)
    return profiles


def _profile_prior_event(
    catalog: Catalog,
    event_store: EventStore,
    lake: LocalLake,
    index_store: IndexStore,
    link: EventLink,
    original: FilingVersion,
    prior_as_of: datetime,
    policy: StudyPolicy,
) -> PastEventProfile | None:
    """Build one historical profile from information observable before the target filing and retain exact filing, denominator, and first-safe-bar hashes."""
    resolved = event_store.get_event_asof(original.rcept_no, prior_as_of)
    if resolved is None:
        return None
    active_link, active_parsed = resolved
    prior_filing = catalog.get_filing_asof(active_parsed.rcept_no, prior_as_of)
    assert prior_filing is not None
    prior_session = lake.previous_session(original.receipt_date)
    if prior_session is None:
        return None
    match = lake.resolve_security(original.stock_code, prior_session, prior_as_of)
    if match.status != "OK":
        return None
    prior_bar = lake.market_bar(match.instrument_id, prior_session, prior_as_of)
    assert prior_bar is not None
    if prior_bar.market_cap is None or prior_bar.market_cap <= 0:
        return None
    materiality = calculate_materiality(active_parsed, prior_filing.receipt_date, prior_bar)
    market = match.market if match.market in ("KOSPI", "KOSDAQ") else None
    start = original.receipt_date - _HISTORY_LOOKBACK
    end = prior_as_of.date()
    stock_window: tuple[MarketBar, ...] = ()
    index_window: tuple[IndexBar, ...] = ()
    if start <= end:
        stock_window = lake.market_window(match.instrument_id, start, end, prior_as_of)
        if market is not None:
            index_window = index_store.window(
                cast("Literal['KOSPI', 'KOSDAQ']", market),
                start,
                end,
                prior_as_of,
            )
    versions = _eligible_versions(catalog, active_link, prior_as_of)
    study = study_buyback(
        active_link, versions, prior_filing, stock_window, index_window, "INCOMPLETE", prior_as_of, policy
    )
    outcome_at: datetime | None = None
    intraday: Decimal | None = study.intraday_excess
    if study.first_safe_session is not None and intraday is not None:
        for bar in stock_window:
            if bar.session == study.first_safe_session and bar.available_at <= prior_as_of:
                outcome_at = bar.available_at
                break
    profile_source_hashes: set[str] = {prior_filing.raw_hash, active_parsed.document_hash, prior_bar.source_hash}
    if intraday is not None and outcome_at is not None and study.first_safe_session is not None:
        stock_hash: str | None = None
        index_hash: str | None = None
        for stock_bar in stock_window:
            if stock_bar.session == study.first_safe_session:
                stock_hash = stock_bar.source_hash
                break
        for index_bar in index_window:
            if index_bar.session == study.first_safe_session:
                index_hash = index_bar.source_hash
                break
        if stock_hash and index_hash:
            profile_source_hashes.add(stock_hash)
            profile_source_hashes.add(index_hash)
    if outcome_at is None:
        intraday = None
    return PastEventProfile(
        event_id=link.event_id,
        receipt_date=original.receipt_date,
        features_available_at=prior_filing.knowledge_available_at,
        outcome_available_at=outcome_at,
        market=match.market,
        prior_market_cap=Decimal(prior_bar.market_cap),
        planned_amount_ratio=materiality.amount_to_market_cap,
        first_safe_intraday_excess=intraday,
        source_hashes=tuple(sorted(profile_source_hashes)),
    )


def build_research_context(
    catalog: Catalog,
    event_store: EventStore,
    lake: LocalLake,
    financial: FinancialEvidence,
    index_store: IndexStore,
    rcept_no: str,
    as_of: datetime,
    policy: StudyPolicy,
) -> ResearchContext:
    """Assemble a point-in-time research view whose source inventory includes the inputs of every published analogue selection."""
    if not rcept_no:
        raise ValueError("receipt number must be non-empty")
    _require_aware(as_of, "as_of")
    resolved = event_store.get_event_asof(rcept_no, as_of)
    withdrawn = False
    if resolved is not None:
        event, parsed = resolved
    else:
        link = _read_link(catalog, rcept_no)
        anchor_filing = catalog.get_filing_asof(rcept_no, as_of)
        if link is None or anchor_filing is None:
            raise ResearchUnavailable(
                "UNKNOWN_RECEIPT" if anchor_filing is None else "UNRESOLVED_EVENT",
                f"no resolved event for receipt {rcept_no}",
            )
        if link.status != "WITHDRAWN":
            raise ResearchUnavailable("UNRESOLVED_EVENT", f"no resolved event for receipt {rcept_no}")
        versions = _eligible_versions(catalog, link, as_of)
        versions.sort(key=lambda filing: (filing.receipt_date.isoformat(), filing.rcept_no))
        active_no = versions[-1].rcept_no
        loaded = event_store.get_receipt(active_no)
        if loaded is None:
            raise ResearchUnavailable("FILING_NOT_YET_KNOWN", f"no parsed facts for receipt {active_no}")
        event, parsed = link, loaded
        withdrawn = True
    filing = catalog.get_filing_asof(parsed.rcept_no, as_of)
    if filing is None:
        raise ResearchUnavailable("FILING_NOT_YET_KNOWN", f"no eligible filing for receipt {parsed.rcept_no}")
    prior_session = lake.previous_session(filing.receipt_date)
    if prior_session is None:
        raise ResearchUnavailable("NO_PRIOR_SESSION", "no validated session before the filing date")
    security = lake.resolve_security(filing.stock_code, prior_session, as_of)
    if security.status != "OK":
        raise ResearchUnavailable(
            f"SECURITY_{security.status}", f"unresolved security for ticker {filing.stock_code}"
        )
    named = lake.security_name(security.source_security_id, prior_session, as_of)
    prior_bar = lake.market_bar(security.instrument_id, prior_session, as_of)
    if withdrawn:
        materiality = MaterialityResult(None, None, (), "WITHDRAWN")
    elif prior_bar is None:
        materiality = MaterialityResult(None, None, (), "NO_PRIOR_BAR")
    else:
        materiality = calculate_materiality(parsed, filing.receipt_date, prior_bar)

    financial_facts = financial.facts_asof(filing.corp_code, as_of, _FINANCIAL_FACTS)

    start = filing.receipt_date - _HISTORY_LOOKBACK
    end = as_of.date()
    stock_bars: tuple[MarketBar, ...] = ()
    index_bars: tuple[IndexBar, ...] = ()
    if start <= end:
        stock_bars = lake.market_window(security.instrument_id, start, end, as_of)
        if security.market in ("KOSPI", "KOSDAQ"):
            index_bars = index_store.window(
                cast("Literal['KOSPI', 'KOSDAQ']", security.market),
                start,
                end,
                as_of,
            )
    if withdrawn:
        study = _withheld_study(event, filing, as_of, policy)
        confounding_receipts: tuple[tuple[str, str, str], ...] = ()
    else:
        versions = _eligible_versions(catalog, event, as_of)
        confound_check, confounding_receipts = verified_event_confound(
            catalog, catalog.db_path.parent, lake, event.event_id, filing.corp_code,
            filing.receipt_date, frozenset(event.rcept_nos), as_of, policy,
        )
        study = study_buyback(
            event, versions, filing, stock_bars, index_bars, confound_check, as_of, policy
        )
        if study.first_safe_session is None:
            study = _pending_when_session_known(lake, event, filing, as_of, policy, study)

    feature_sessions = _prior_sessions(lake, filing.receipt_date, _FEATURE_SESSIONS)
    universe: Mapping[str, Sequence[MarketBar]] = {}
    if feature_sessions:
        universe = lake.market_universe(feature_sessions[-1], feature_sessions[0], as_of)
    past_profiles = _build_past_profiles(
        catalog, event_store, lake, index_store, event.event_id, filing.receipt_date, as_of, policy
    )
    comparables = select_comparables(
        security, event, filing.receipt_date, materiality, universe, past_profiles, as_of
    )

    hashes: set[str] = {parsed.document_hash, filing.raw_hash}
    if prior_bar is not None:
        hashes.add(prior_bar.source_hash)
    hashes.update(fact.source_hash for fact in financial_facts)
    if named is not None:
        hashes.add(named[1])
    hashes.update(bar.source_hash for bar in stock_bars)
    hashes.update(bar.source_hash for bar in index_bars)
    hashes.update(study.evidence_hashes)
    hashes.update(digest for _, _, digest in confounding_receipts)
    hashes.update(comparables.selection_source_hashes)
    source_hashes = tuple(sorted(hashes))
    artifact_paths = {
        digest: path
        for digest in source_hashes
        if (path := catalog.get_artifact_path(digest)) is not None
    }
    return ResearchContext(
        anchor_rcept_no=rcept_no,
        active_rcept_no=parsed.rcept_no,
        event=event,
        filing=filing,
        parsed=parsed,
        security=security,
        financial_facts=financial_facts,
        stock_bars=stock_bars,
        index_bars=index_bars,
        materiality=materiality,
        study=study,
        comparables=comparables,
        as_of=as_of,
        index_manifest_hash=index_store.manifest.manifest_hash,
        snapshot_ids=_snapshot_ids(catalog, source_hashes),
        source_hashes=source_hashes,
        artifact_paths=artifact_paths,
        confounding_receipts=confounding_receipts,
        company_name=named[0] if named is not None else None,
        company_name_source_hash=named[1] if named is not None else None,
    )


__all__ = ["ResearchContext", "ResearchUnavailable", "build_research_context"]
