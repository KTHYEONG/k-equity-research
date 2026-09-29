"""Deterministic extraction of version-specific buyback facts from DART receipt ZIPs."""

from __future__ import annotations

import hashlib
import io
import re
import zipfile
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import PurePosixPath
from typing import Literal
from xml.etree import ElementTree as ET

from src.data.catalog import FilingVersion

_DATE_RE = re.compile(r"^[0-9]{8}$")
_FIRST_SUBMISSION_LABEL = re.compile(r"정정대상\s*공시서류의\s*최초제출일")
_FIRST_SUBMISSION_RE = re.compile(
    r"(?<!\d)(\d{4})\s*(?:년|월|[.\-/])\s*(\d{1,2})\s*(?:월|[.\-/])\s*(\d{1,2})\s*일?"
)
_SHORT_SUBMISSION_RE = re.compile(r"[`'](\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})")

_EXPECTED_SECTION = "자기주식 취득 결정"
_XML_AMPERSAND_RE = re.compile(r"&(?!amp;|lt;|gt;|quot;|apos;|#\d+;|#x[0-9A-Fa-f]+;)")
_XML_CDATA_RE = re.compile(r"(<!\[CDATA\[.*?\]\]>|<!--.*?-->|<\?.*?\?>)", re.DOTALL)


def _parse_receipt_xml(payload: bytes) -> ET.Element:
    """Parse DART XML, retrying after escaping literal ampersands outside CDATA/comments."""
    try:
        document = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("malformed receipt document") from exc
    try:
        return ET.fromstring(document)  # noqa: S314 - source XML is authenticated, hash-registered, and bounded by DocumentLimits
    except ET.ParseError:
        parts = _XML_CDATA_RE.split(document)
        repaired = "".join(
            part if index % 2 else _XML_AMPERSAND_RE.sub("&amp;", part)
            for index, part in enumerate(parts)
        )
        try:
            return ET.fromstring(repaired)  # noqa: S314 - source XML is authenticated, hash-registered, and bounded by DocumentLimits
        except ET.ParseError as exc:
            raise ValueError("malformed receipt document") from exc


@dataclass(frozen=True, slots=True)
class EvidenceLocation:
    """Frozen source coordinate for one extracted fact."""

    rcept_no: str
    document_hash: str
    member_name: str
    source_kind: Literal["ACODE", "AUNIT"]
    source_key: str
    section: str | None
    table: str | None
    cell: str | None


@dataclass(frozen=True, slots=True)
class DocumentLimits:
    """Explicit per-receipt ZIP safety budget."""

    max_members: int = 8
    max_uncompressed_bytes: int = 33554432


@dataclass(frozen=True, slots=True)
class BuybackFact:
    """Frozen normalized fact anchored to one receipt document."""

    field: str
    value_decimal: Decimal | None
    value_text: str | None
    unit: str | None
    evidence: EvidenceLocation
    status: Literal["VERIFIED", "UNVERIFIED", "NOT_APPLICABLE"]


@dataclass(frozen=True, slots=True)
class ParsedBuyback:
    """Frozen version-specific parse result for one receipt."""

    rcept_no: str
    corp_code: str
    first_submission_date: date | None
    facts: tuple[BuybackFact, ...]
    document_hash: str
    parse_status: str


@dataclass(frozen=True, slots=True)
class _Target:
    kind: Literal["ACODE", "AUNIT"]
    value_kind: Literal["quantity", "amount", "text", "date"]
    unit: str | None
    table: str
    labels: tuple[str, ...]


_TARGETS: dict[str, _Target] = {
    "ACQ_OSTK": _Target("ACODE", "quantity", "shares", "TBL_ACQ_STK", ("취득예정주식", "보통주식")),
    "ACQ_OSTK_PRC": _Target("ACODE", "amount", "KRW", "TBL_ACQ_STK", ("취득예정금액", "보통주식")),
    "ACQ_PPS": _Target("ACODE", "text", None, "TBL_ACQ_STK", ("취득목적",)),
    "ACQ_BGN": _Target("AUNIT", "date", None, "TBL_ACQ_STK", ("취득예상기간", "시작일")),
    "ACQ_END": _Target("AUNIT", "date", None, "TBL_ACQ_STK", ("취득예상기간", "종료일")),
    "BUY_OSTK_LMT": _Target("ACODE", "quantity", "shares", "TBL_ACQ_STK", ("1일 매수 주문수량 한도", "보통주식")),
}


def _element_text(element: ET.Element) -> str:
    return "".join(element.itertext()).strip()


def _cell_label(row: ET.Element) -> str:
    parts = [_element_text(child) for child in row if child.tag == "TD"]
    return " / ".join(part for part in parts if part)


def _row_label_with_span(row: ET.Element, parents: dict[ET.Element, ET.Element]) -> str:
    """Combine one row's labels with row-spanning labels from the preceding row."""
    own = _cell_label(row)
    container = parents.get(row)
    assert container is not None
    siblings = [child for child in container if child.tag == "TR"]
    position = siblings.index(row)
    if position == 0:
        return own
    spanned = [
        _element_text(cell)
        for cell in siblings[position - 1]
        if cell.tag == "TD" and cell.get("ROWSPAN") is not None
    ]
    spanned = [text for text in spanned if text]
    if not spanned:
        return own
    return " / ".join([*spanned, own] if own else spanned)


def _parse_decimal(raw: str) -> Decimal | None:
    text = raw.strip().replace(",", "")
    if not text or text == "-":
        return None
    if not re.fullmatch(r"[0-9]+(\.[0-9]+)?", text):
        return None
    return Decimal(text)


def _iso_from_unit_value(raw: str) -> str | None:
    text = raw.strip()
    if not _DATE_RE.match(text):
        return None
    try:
        parsed = date(int(text[0:4]), int(text[4:6]), int(text[6:8]))
    except ValueError:
        return None
    return parsed.isoformat()


def _first_submission_date(root: ET.Element, filing: FilingVersion) -> date | None:
    if not filing.correction_flag and not filing.withdrawal_flag:
        return filing.receipt_date
    found: set[date] = set()
    for element in root.iter():
        if element.tag not in {"P", "TR"}:
            continue
        content = _element_text(element)
        label = _FIRST_SUBMISSION_LABEL.search(content)
        if label is None:
            continue
        value = content[label.end():]
        match = _FIRST_SUBMISSION_RE.search(value)
        short = _SHORT_SUBMISSION_RE.search(value) if match is None else None
        if match is None and short is None:
            continue
        try:
            if match is not None:
                found.add(date(*(int(group) for group in match.groups())))
            else:
                assert short is not None
                found.add(date(2000 + int(short[1]), int(short[2]), int(short[3])))
        except ValueError:
            return None
    if len(found) != 1:
        return None
    first = next(iter(found))
    return first if first <= filing.receipt_date else None


def _select_member(raw_zip: bytes, limits: DocumentLimits) -> tuple[str, bytes, str]:
    if not raw_zip:
        raise ValueError("empty receipt archive")
    document_hash = hashlib.sha256(raw_zip).hexdigest()
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw_zip))
    except zipfile.BadZipFile as exc:
        raise ValueError("malformed receipt archive") from exc
    with archive:
        infos = archive.infolist()
        if len(infos) > limits.max_members:
            raise ValueError("receipt archive exceeds member budget")
        names = [info.filename for info in infos]
        if len(set(names)) != len(names):
            raise ValueError("duplicate member names")
        total = 0
        for info in infos:
            name = info.filename
            if not name or name.startswith(("/", "\\")) or "\\" in name:
                raise ValueError("unsafe member path")
            if ".." in PurePosixPath(name).parts:
                raise ValueError("unsafe member path")
            if info.is_dir():
                raise ValueError("unexpected directory member")
            total += info.file_size
            if total > limits.max_uncompressed_bytes:
                raise ValueError("receipt archive exceeds expansion budget")
        xml_names = [name for name in names if name.lower().endswith(".xml")]
        if len(xml_names) != 1:
            raise ValueError("receipt must contain one unambiguous XML member")
        member_name = xml_names[0]
        payload = archive.read(member_name)
    return member_name, payload, document_hash


def parse_buyback_document(
    filing: FilingVersion, raw_zip: bytes, limits: DocumentLimits
) -> ParsedBuyback:
    """Extract version-specific buyback facts and evidence coordinates from one DART receipt ZIP. Preserve missing, ambiguous and nonnumeric fields as unverified; raise ValueError for unsafe ZIP or malformed document."""
    if not filing.rcept_no:
        raise ValueError("filing receipt number must be non-empty")
    member_name, payload, document_hash = _select_member(raw_zip, limits)
    root = _parse_receipt_xml(payload)  # noqa: S314 - authenticated DART receipt bytes are hash-registered locally and bounded by DocumentLimits
    parents: dict[ET.Element, ET.Element] = {}
    for parent in root.iter():
        for child in parent:
            parents[child] = parent
    groups: dict[str, list[tuple[ET.Element, str, str | None, str, str]]] = {}
    for element in root.iter():
        if element.tag == "TE" and element.get("ACODE"):
            key = str(element.get("ACODE"))
            target = _TARGETS.get(key)
            if target is None or target.kind != "ACODE":
                continue
            section: str | None = None
            table: str | None = None
            node: ET.Element | None = element
            while node is not None:
                if node.tag == "TABLE-GROUP" and table is None:
                    table = node.get("ACLASS")
                if node.tag == "SECTION-1" and section is None:
                    title = node.find("TITLE")
                    section = _element_text(title) if title is not None else ""
                node = parents.get(node)
            groups.setdefault(key, []).append((element, _element_text(element), None, section or "", table or ""))
        elif element.tag == "TU" and element.get("AUNIT"):
            key = str(element.get("AUNIT"))
            target = _TARGETS.get(key)
            if target is None or target.kind != "AUNIT":
                continue
            section = None
            table = None
            node = element
            while node is not None:
                if node.tag == "TABLE-GROUP" and table is None:
                    table = node.get("ACLASS")
                if node.tag == "SECTION-1" and section is None:
                    title = node.find("TITLE")
                    section = _element_text(title) if title is not None else ""
                parent_next: ET.Element | None = parents.get(node)
                node = parent_next
            groups.setdefault(key, []).append(
                (element, _element_text(element), element.get("AUNITVALUE"), section or "", table or "")
            )
    facts: list[BuybackFact] = []
    for field in sorted(groups):
        target = _TARGETS[field]
        occurrences = groups[field]
        duplicated = len(occurrences) > 1
        ambiguous = duplicated
        element, raw_text, unit_value, section, table = occurrences[0]
        row = parents.get(element)
        while row is not None and row.tag != "TR":
            row = parents.get(row)
        cell = _row_label_with_span(row, parents) if row is not None else ""
        evidence = EvidenceLocation(
            rcept_no=filing.rcept_no,
            document_hash=document_hash,
            member_name=member_name,
            source_kind=target.kind,
            source_key=field,
            section=section or None,
            table=table or None,
            cell=cell or None,
        )
        consistent = (
            _EXPECTED_SECTION in section
            and table == target.table
            and all(fragment in cell for fragment in target.labels)
        )
        if target.value_kind in ("quantity", "amount"):
            value = _parse_decimal(raw_text)
            if value is not None and consistent and not ambiguous and not duplicated:
                facts.append(BuybackFact(field, value, raw_text.strip(), target.unit, evidence, "VERIFIED"))
            else:
                kept = raw_text.strip() or None
                facts.append(BuybackFact(field, None, kept, target.unit, evidence, "UNVERIFIED"))
        elif target.value_kind == "text":
            text_ok = bool(raw_text.strip()) and raw_text.strip() != "-"
            if text_ok and consistent and not duplicated:
                facts.append(BuybackFact(field, None, raw_text.strip(), None, evidence, "VERIFIED"))
            else:
                facts.append(BuybackFact(field, None, raw_text.strip() or None, None, evidence, "UNVERIFIED"))
        else:
            iso = _iso_from_unit_value(str(unit_value or ""))
            if iso is not None and consistent and not duplicated:
                facts.append(BuybackFact(field, None, iso, None, evidence, "VERIFIED"))
            else:
                fallback = _iso_from_unit_value(str(unit_value or "")) or (raw_text.strip() or None)
                facts.append(BuybackFact(field, None, fallback, None, evidence, "UNVERIFIED"))
    return ParsedBuyback(
        rcept_no=filing.rcept_no,
        corp_code=filing.corp_code,
        first_submission_date=_first_submission_date(root, filing),
        facts=tuple(facts),
        document_hash=document_hash,
        parse_status="OK",
    )


__all__ = [
    "BuybackFact",
    "DocumentLimits",
    "EvidenceLocation",
    "ParsedBuyback",
    "parse_buyback_document",
]
