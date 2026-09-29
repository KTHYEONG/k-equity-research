"""Single shared DART HTML table decoder and row extractor."""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Literal

_CELL_TAGS = frozenset({"TD", "TH", "TE", "TU"})
_PARAGRAPH_TAGS = frozenset({"P", "TITLE"})


@dataclass(frozen=True, slots=True)
class DocumentBlock:
    """One top-level element of a DART document body, in document order."""

    kind: Literal["text", "table"]
    text: str  # normalized whitespace; "" for tables
    grid: tuple[tuple[str, ...], ...]  # expanded cell grid; () for text


def decode_member(raw: bytes) -> str | None:
    """Decode one archive member (BOM, UTF-8, CP949)."""
    if raw.startswith(b"\xef\xbb\xbf"):
        try:
            return raw[3:].decode("utf-8")
        except UnicodeDecodeError:
            return None
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        try:
            return raw.decode("utf-16")
        except UnicodeDecodeError:
            return None
    head = raw[:1024].decode("ascii", errors="ignore")
    match = re.search(r"encoding\s*=\s*['\"]([^'\"]+)['\"]", head)
    if match:
        enc = match.group(1).strip().lower().replace("_", "-")
        if enc in ("utf8", "utf-8"):
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError:
                pass
        if enc in ("cp949", "euc-kr", "ks-c-5601", "windows-949", "uhc"):
            try:
                codec = "cp949" if enc in ("cp949", "windows-949", "uhc") else "euc-kr"
                return raw.decode(codec)
            except (UnicodeDecodeError, LookupError):
                pass
    for codec in ("utf-8", "cp949"):
        try:
            return raw.decode(codec).lstrip("\ufeff")
        except UnicodeDecodeError:
            continue
    for codec in ("euc-kr",):
        try:
            return raw.decode(codec).lstrip("\ufeff")
        except UnicodeDecodeError:
            continue
    return None


class _SharedTableParser(HTMLParser):
    """Tolerant table reader for DART's non-XML forms."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._current_table: list[list[str]] | None = None
        self._current_row: list[str] | None = None
        self._current_cell: list[str] | None = None
        self.table_index = -1
        self.indexed: dict[int, list[list[str]]] = {}

    def handle_starttag(self, tag: str, _attrs: list[tuple[str, str | None]]) -> None:
        name = tag.upper()
        if name == "TABLE":
            self.table_index += 1
            self._current_table = []
            self.tables.append(self._current_table)
            self.indexed[self.table_index] = self._current_table
        elif name == "TR":
            self._current_row = []
        elif name in {"TD", "TH"} and self._current_row is not None:
            self._current_cell = []

    def handle_data(self, data: str) -> None:
        if self._current_cell is not None:
            self._current_cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        name = tag.upper()
        if name in {"TD", "TH"} and self._current_row is not None and self._current_cell is not None:
            self._current_row.append(" ".join("".join(self._current_cell).split()))
            self._current_cell = None
        elif name == "TR" and self._current_row is not None:
            if self._current_table is None:
                self._current_table = []
                self.tables.append(self._current_table)
            self._current_table.append(self._current_row)
            self.indexed.setdefault(self.table_index, self._current_table)
            self._current_row = None


def extract_tables(text: str) -> list[list[list[str]]]:
    """Extract non-empty tables from one decoded member."""
    parser = _SharedTableParser()
    parser.feed(text)
    return [table for table in parser.tables if table]


def _normalized(text: str) -> str:
    """Collapse ``&cr;`` and every whitespace run into single spaces."""
    return " ".join(text.replace("&cr;", " ").split())


def _span(attrs: list[tuple[str, str | None]], name: str) -> int:
    """Read a COLSPAN/ROWSPAN attribute, defaulting to one cell."""
    for key, value in attrs:
        if key.upper() == name and value and value.strip().isdecimal():
            return max(1, int(value.strip()))
    return 1


class _DocumentBlockParser(HTMLParser):
    """Tolerant reader emitting paragraphs and expanded table grids in document order."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[DocumentBlock] = []
        self._table: list[list[str]] | None = None
        self._nested = 0
        self._cells: dict[int, str] | None = None
        self._carry: dict[int, tuple[str, int]] = {}
        self._cell: list[str] | None = None
        self._cell_span = 1
        self._cell_rowspan = 1
        self._paragraph: list[str] = []

    def _flush_paragraph(self) -> None:
        text = _normalized("".join(self._paragraph))
        self._paragraph = []
        if text:
            self.blocks.append(DocumentBlock(kind="text", text=text, grid=()))

    def _open_row(self) -> None:
        """Start a row, first restating every cell carried down by ROWSPAN."""
        fills: dict[int, str] = {}
        carry: dict[int, tuple[str, int]] = {}
        for column, (text, left) in self._carry.items():
            fills[column] = text
            if left > 1:
                carry[column] = (text, left - 1)
        self._carry = carry
        self._cells = fills

    def _close_cell(self) -> None:
        if self._cell is None:
            return
        text = _normalized("".join(self._cell))
        self._cell = None
        cells = self._cells if self._cells is not None else {}
        self._cells = cells
        column = 0
        for _ in range(self._cell_span):
            while column in cells:
                column += 1
            cells[column] = text
            if self._cell_rowspan > 1:
                self._carry[column] = (text, self._cell_rowspan - 1)
            column += 1

    def _close_row(self) -> None:
        self._close_cell()
        if self._cells is None or self._table is None:
            return
        width = max(self._cells, default=-1) + 1
        self._table.append([self._cells.get(index, "") for index in range(width)])
        self._cells = None

    def _emit_table(self) -> None:
        rows = self._table or []
        self._table = None
        self._cells = None
        self._carry = {}
        if not rows:
            return
        width = max(len(row) for row in rows)
        grid = tuple(tuple(row) + ("",) * (width - len(row)) for row in rows)
        self.blocks.append(DocumentBlock(kind="table", text="", grid=grid))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        name = tag.upper()
        if name == "TABLE":
            if self._table is None:
                self._flush_paragraph()
                self._table = []
                self._carry = {}
            else:
                self._nested += 1
            return
        if self._nested:
            return
        if name == "TR":
            if self._table is not None:
                self._close_row()
                self._open_row()
        elif name in _CELL_TAGS:
            if self._table is None:
                return
            if self._cell is not None:
                self._close_cell()
            if self._cells is None:
                self._open_row()
            self._cell = []
            self._cell_span = _span(attrs, "COLSPAN")
            self._cell_rowspan = _span(attrs, "ROWSPAN")
        elif name in _PARAGRAPH_TAGS and self._table is None:
            self._flush_paragraph()

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)
        elif self._table is None:
            self._paragraph.append(data)

    def handle_endtag(self, tag: str) -> None:
        name = tag.upper()
        if name == "TABLE":
            if self._table is None:
                return
            if self._nested:
                self._nested -= 1
                return
            self._close_row()
            self._emit_table()
        elif self._nested:
            return
        elif name in _CELL_TAGS:
            if self._table is not None:
                self._close_cell()
        elif name == "TR":
            if self._table is not None:
                self._close_row()
        elif name in _PARAGRAPH_TAGS and self._table is None:
            self._flush_paragraph()

    def finish(self) -> None:
        """Close any table or paragraph left open at the end of input."""
        if self._table is not None:
            self._close_row()
            self._emit_table()
        self._flush_paragraph()


def read_blocks(markup: str) -> tuple[DocumentBlock, ...]:
    """Read paragraphs, titles and top-level tables of one DART document fragment in order.

    Tables are returned as rectangular grids in which every ``TD``, ``TH``,
    ``TE`` and ``TU`` cell is placed at its visual position: a cell spanning
    ``COLSPAN`` columns or ``ROWSPAN`` rows is repeated in each covered slot.
    Header rows therefore align with body rows, which is what makes the
    current-period column identifiable. ``&cr;`` and runs of whitespace
    become single spaces. Nested tables are flattened into their parent's cell text.
    """
    parser = _DocumentBlockParser()
    parser.feed(markup)
    parser.close()
    parser.finish()
    return tuple(parser.blocks)
