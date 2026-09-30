"""Version-checked SQLite storage for normalized buyback facts and event links."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal, cast

from src.core.buyback_document import BuybackFact, EvidenceLocation, ParsedBuyback
from src.core.revisions import EventLink, link_buyback_versions
from src.data.catalog import Catalog, FilingVersion

_SCHEMA_VERSION = 1


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


class EventStore:
    """SQLite-backed durable identity for versioned buyback facts and event links."""

    def __init__(self, catalog: Catalog) -> None:
        self._catalog = catalog
        self._db_path: Path = catalog.db_path
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS buyback_receipt (
                rcept_no TEXT PRIMARY KEY,
                corp_code TEXT NOT NULL,
                first_submission_date TEXT,
                document_hash TEXT NOT NULL,
                parse_status TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS buyback_fact (
                rcept_no TEXT NOT NULL,
                field TEXT NOT NULL,
                value_decimal TEXT,
                value_text TEXT,
                unit TEXT,
                status TEXT NOT NULL,
                member_name TEXT NOT NULL,
                source_kind TEXT NOT NULL,
                source_key TEXT NOT NULL,
                section TEXT,
                fact_table TEXT,
                cell TEXT,
                document_hash TEXT NOT NULL,
                PRIMARY KEY (rcept_no, field)
            );
            CREATE TABLE IF NOT EXISTS event_link (
                event_id TEXT PRIMARY KEY,
                rcept_nos TEXT NOT NULL,
                status TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS buyback_viewer_parent (
                correction_rcept_no TEXT PRIMARY KEY,
                original_rcept_no TEXT NOT NULL,
                viewer_hash TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS event_schema_version (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            """
        )
        row = self._conn.execute("SELECT version FROM event_schema_version").fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO event_schema_version (version, applied_at) VALUES (?, ?)",
                (_SCHEMA_VERSION, datetime.now().astimezone().isoformat()),
            )
            self._conn.commit()
        elif int(row["version"]) != _SCHEMA_VERSION:
            raise ValueError(f"unsupported event store schema version: {row['version']}")
        else:
            self._conn.commit()

    def _load_parsed(self, rcept_no: str) -> ParsedBuyback | None:
        receipt = self._conn.execute("SELECT * FROM buyback_receipt WHERE rcept_no=?", (rcept_no,)).fetchone()
        if receipt is None:
            return None
        rows = self._conn.execute(
            "SELECT * FROM buyback_fact WHERE rcept_no=? ORDER BY field", (rcept_no,)
        ).fetchall()
        facts: list[BuybackFact] = []
        for item in rows:
            raw_decimal = item["value_decimal"]
            kind = cast("Literal['ACODE', 'AUNIT']", str(item["source_kind"]))
            status = cast("Literal['VERIFIED', 'UNVERIFIED', 'NOT_APPLICABLE']", str(item["status"]))
            facts.append(
                BuybackFact(
                    field=str(item["field"]),
                    value_decimal=Decimal(str(raw_decimal)) if raw_decimal is not None else None,
                    value_text=str(item["value_text"]) if item["value_text"] is not None else None,
                    unit=str(item["unit"]) if item["unit"] is not None else None,
                    evidence=EvidenceLocation(
                        rcept_no=rcept_no,
                        document_hash=str(item["document_hash"]),
                        member_name=str(item["member_name"]),
                        source_kind=kind,
                        source_key=str(item["source_key"]),
                        section=str(item["section"]) if item["section"] is not None else None,
                        table=str(item["fact_table"]) if item["fact_table"] is not None else None,
                        cell=str(item["cell"]) if item["cell"] is not None else None,
                    ),
                    status=status,
                )
            )
        first_date = receipt["first_submission_date"]
        return ParsedBuyback(
            rcept_no=rcept_no,
            corp_code=str(receipt["corp_code"]),
            first_submission_date=date.fromisoformat(str(first_date)) if first_date is not None else None,
            facts=tuple(facts),
            document_hash=str(receipt["document_hash"]),
            parse_status=str(receipt["parse_status"]),
        )

    def _all_filings(self) -> list[FilingVersion]:
        rows = self._conn.execute("SELECT rcept_no FROM buyback_receipt").fetchall()
        filings: list[FilingVersion] = []
        for item in rows:
            rcept_no = str(item["rcept_no"])
            direct = self._conn.execute("SELECT * FROM filing_version WHERE rcept_no=?", (rcept_no,)).fetchone()
            if direct is None:
                continue
            parent = direct["parent_rcept_no"]
            filings.append(
                FilingVersion(
                    rcept_no=str(direct["rcept_no"]),
                    corp_code=str(direct["corp_code"]),
                    receipt_date=date.fromisoformat(str(direct["receipt_date"])),
                    report_name=str(direct["report_name"]),
                    stock_code=str(direct["stock_code"]),
                    raw_hash=str(direct["raw_hash"]),
                    first_observed_at=_parse_dt(str(direct["first_observed_at"])),
                    knowledge_available_at=_parse_dt(str(direct["knowledge_available_at"])),
                    availability_mode=str(direct["availability_mode"]),  # type: ignore[arg-type]
                    correction_flag=bool(int(direct["correction_flag"])),
                    withdrawal_flag=bool(int(direct["withdrawal_flag"])),
                    parent_rcept_no=str(parent) if parent is not None else None,
                    link_status=str(direct["link_status"]),
                    time_precision=str(direct["time_precision"]),  # type: ignore[arg-type]
                )
            )
        return filings

    def _all_parsed(self) -> dict[str, ParsedBuyback]:
        rows = self._conn.execute("SELECT rcept_no FROM buyback_receipt").fetchall()
        result: dict[str, ParsedBuyback] = {}
        for item in rows:
            loaded = self._load_parsed(str(item["rcept_no"]))
            if loaded is not None:
                result[loaded.rcept_no] = loaded
        return result

    def _refresh_links(self) -> None:
        filings = self._all_filings()
        parsed = self._all_parsed()
        if not filings:
            return
        parents = {
            str(row["correction_rcept_no"]): str(row["original_rcept_no"])
            for row in self._conn.execute("SELECT correction_rcept_no, original_rcept_no FROM buyback_viewer_parent")
        }
        links = link_buyback_versions(filings, parsed, parents)
        self._conn.execute("DELETE FROM event_link")
        for link in links:
            self._conn.execute(
                "INSERT INTO event_link (event_id, rcept_nos, status) VALUES (?, ?, ?)",
                (link.event_id, json.dumps(list(link.rcept_nos)), link.status),
            )

    def store_parsed_batch(
        self, filings: Sequence[FilingVersion], parsed: Sequence[ParsedBuyback]
    ) -> None:
        """Commit verified receipt facts and conservative event links together, preserving immutable source hashes."""
        filing_map: Mapping[str, FilingVersion] = {filing.rcept_no: filing for filing in filings}
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            for item in parsed:
                if item.rcept_no not in filing_map:
                    raise ValueError("parsed receipt without filing version")
                stored = self._conn.execute(
                    "SELECT document_hash FROM buyback_receipt WHERE rcept_no=?", (item.rcept_no,)
                ).fetchone()
                if stored is not None and str(stored["document_hash"]) != item.document_hash:
                    raise ValueError("conflicting same-receipt bytes")
                first_date = item.first_submission_date.isoformat() if item.first_submission_date else None
                self._conn.execute(
                    "INSERT INTO buyback_receipt (rcept_no, corp_code, first_submission_date, document_hash, parse_status) VALUES (?, ?, ?, ?, ?) ON CONFLICT (rcept_no) DO UPDATE SET corp_code=excluded.corp_code, first_submission_date=excluded.first_submission_date, document_hash=excluded.document_hash, parse_status=excluded.parse_status",
                    (item.rcept_no, item.corp_code, first_date, item.document_hash, item.parse_status),
                )
                self._conn.execute("DELETE FROM buyback_fact WHERE rcept_no=?", (item.rcept_no,))
                for fact in item.facts:
                    self._conn.execute(
                        "INSERT INTO buyback_fact (rcept_no, field, value_decimal, value_text, unit, status, member_name, source_kind, source_key, section, fact_table, cell, document_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            item.rcept_no,
                            fact.field,
                            str(fact.value_decimal) if fact.value_decimal is not None else None,
                            fact.value_text,
                            fact.unit,
                            fact.status,
                            fact.evidence.member_name,
                            fact.evidence.source_kind,
                            fact.evidence.source_key,
                            fact.evidence.section,
                            fact.evidence.table,
                            fact.evidence.cell,
                            fact.evidence.document_hash,
                        ),
                    )
            self._refresh_links()
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise

    def register_viewer_parent(self, correction_no: str, original_no: str, viewer_html: bytes, viewer_hash: str) -> None:
        """Link a correction only when its archived official DART family selector names exactly one original."""
        import hashlib

        if hashlib.sha256(viewer_html).hexdigest() != viewer_hash:
            raise ValueError("viewer hash mismatch")
        artifact = self._catalog.get_artifact_path(viewer_hash)
        if artifact is None or (self._db_path.parent / artifact).read_bytes() != viewer_html:
            raise ValueError("viewer source is not registered locally")
        source = self._conn.execute(
            "SELECT 1 FROM raw_artifact WHERE sha256=? AND source='dart' AND endpoint='viewer' AND request_key=?",
            (viewer_hash, correction_no),
        ).fetchone()
        if source is None:
            raise ValueError("viewer source identity mismatch")
        try:
            html = viewer_html.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("invalid viewer encoding") from exc
        family = set(re.findall(r'<option value="rcpNo=(\d{14})" title="([^"]+)"', html))
        ids = {number for number, _ in family}
        if ids != {correction_no, original_no} or len(family) != 2:
            raise ValueError("ambiguous DART family selector")
        filings = {filing.rcept_no: filing for filing in self._all_filings()}
        correction = filings.get(correction_no)
        original = filings.get(original_no)
        if correction is None or original is None or not correction.correction_flag or original.correction_flag:
            raise ValueError("viewer parent is not an original/correction pair")
        if correction.corp_code != original.corp_code or correction.receipt_date < original.receipt_date:
            raise ValueError("viewer parent company or chronology mismatch")
        if correction.report_name.split("]")[-1].strip() != original.report_name.split("]")[-1].strip():
            raise ValueError("viewer parent form mismatch")
        if {title.replace(" ", "") for _, title in family} != {"주요사항보고서(자기주식취득결정)"}:
            raise ValueError("viewer parent form is not buyback decision")
        existing = self._conn.execute(
            "SELECT original_rcept_no, viewer_hash FROM buyback_viewer_parent WHERE correction_rcept_no=?", (correction_no,)
        ).fetchone()
        if existing is not None and (str(existing["original_rcept_no"]), str(existing["viewer_hash"])) != (original_no, viewer_hash):
            raise ValueError("conflicting viewer parent")
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            self._conn.execute(
                "INSERT OR IGNORE INTO buyback_viewer_parent VALUES (?, ?, ?)",
                (correction_no, original_no, viewer_hash),
            )
            self._refresh_links()
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise

    def get_receipt(self, rcept_no: str) -> ParsedBuyback | None:
        """Return immutable facts for exactly one receipt number, independent of later corrections."""
        return self._load_parsed(rcept_no)

    def get_event_asof(self, anchor_rcept_no: str, as_of: datetime) -> tuple[EventLink, ParsedBuyback] | None:
        """Resolve an anchor receipt to its verified event and latest eligible linked version at as_of. Return None for ambiguous links; never include a correction before its knowledge boundary."""
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as-of instant must be timezone-aware")
        # instr() narrows the scan in SQLite; the JSON check below keeps the match exact (no substring hits).
        row = self._conn.execute(
            "SELECT * FROM event_link WHERE instr(rcept_nos, ?) > 0", (f'"{anchor_rcept_no}"',)
        ).fetchall()
        target: EventLink | None = None
        for item in row:
            nos = tuple(str(value) for value in json.loads(str(item["rcept_nos"])))
            if anchor_rcept_no in nos:
                status = cast("Literal['LINKED', 'UNRESOLVED_LINK', 'WITHDRAWN']", str(item["status"]))
                target = EventLink(event_id=str(item["event_id"]), rcept_nos=nos, status=status)
                break
        if target is None or target.status != "LINKED":
            return None
        eligible: list[tuple[datetime, str]] = []
        for rcept_no in target.rcept_nos:
            filing = self._catalog.get_filing_asof(rcept_no, as_of)
            if filing is not None:
                eligible.append((filing.knowledge_available_at, rcept_no))
        if not eligible:
            return None
        if anchor_rcept_no not in [rcept_no for _, rcept_no in eligible]:
            anchor_filing = self._catalog.get_filing_asof(anchor_rcept_no, as_of)
            if anchor_filing is None:
                return None
        eligible.sort()
        active_no = eligible[-1][1]
        active = self._load_parsed(active_no)
        assert active is not None
        return (target, active)

    def list_prior_events(self, as_of: datetime) -> tuple[tuple[EventLink, FilingVersion], ...]:
        """List distinct verified buyback events and their eligible original filings known by the requested instant."""
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as-of instant must be timezone-aware")
        rows = self._conn.execute("SELECT * FROM event_link ORDER BY event_id").fetchall()
        result: list[tuple[EventLink, FilingVersion]] = []
        for item in rows:
            status = str(item["status"])
            if status != "LINKED":
                continue
            nos = tuple(str(value) for value in json.loads(str(item["rcept_nos"])))
            link = EventLink(event_id=str(item["event_id"]), rcept_nos=nos, status="LINKED")
            candidates: list[FilingVersion] = []
            for rcept_no in nos:
                filing = self._catalog.get_filing_asof(rcept_no, as_of)
                if filing is not None:
                    candidates.append(filing)
            if not candidates:
                continue
            candidates.sort(key=lambda filing: (filing.receipt_date.isoformat(), filing.rcept_no))
            result.append((link, candidates[0]))
        result.sort(key=lambda pair: (pair[1].receipt_date.isoformat(), pair[1].rcept_no))
        return tuple(result)


__all__ = ["EventStore"]
