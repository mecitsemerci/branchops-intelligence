"""Pull source adapter importing incidents from CSV/TSV, XLSX and JSONL files.

Each data row becomes one ``IncidentEvent``. ``field_mapping`` maps canonical names to column headers:
snapshot attributes (summary, description, status, ...) go to ``fields``, ``updated_at`` sets
``source_updated_at`` and every other name (branch_code, application, ...) goes to ``mapped_fields``.

Header matching: an exact header match wins; otherwise headers are compared *folded* — trimmed, inner
whitespace collapsed, Turkish letters transliterated (ç ğ ı İ ö ş ü -> c g i i o s u), other
diacritics stripped and case-folded — so ``"Sube Kodu"`` matches ``"ŞUBE  KODU"`` and ``"Öncelik"``
matches ``"ONCELIK"``.

Identity: with the ``id_column`` option a non-empty cell is the external key (whitespace replaced by "-").
Rows without an id are *content-addressed*: the key is ``sha256-`` + 32 hex chars of a hash over the
row's non-empty (header, value) pairs, so re-importing an identical row is a no-op while an edited row
becomes a new record.

Empty cells are omitted (they never erase known values). Masking is not applied here: events are stored
in the inbox as envelopes and masked by the worker through :meth:`CsvImportAdapter.normalize`.

Options (``SourceSettings.options``): ``id_column``, ``delimiter`` (CSV; default sniffed among , ; TAB),
``encoding`` (default ``utf-8-sig``), ``sheet`` (XLSX; default the first sheet), ``watch_directory``
(for :meth:`~CsvImportAdapter.fetch`), ``batch`` (default the file stem; also the default ``group_key``),
``datetime_formats`` (strptime formats tried after ISO 8601) and ``timezone`` for naive date/times
(default ``Europe/Istanbul``).
"""

from __future__ import annotations

import csv
import hashlib
import importlib
import io
import json
import re
import unicodedata
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from datetime import UTC, date, datetime, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from branchops.application.interfaces.source_adapter import (
    SourcePayloadError,
    TextTransform,
    decode_event_envelope,
)
from branchops.config.settings import SourceSettings
from branchops.domain.entities.incident import EXTERNAL_KEY_RE, IncidentEvent
from branchops.domain.enums import IncidentEventType

CSV_SUFFIXES = frozenset({".csv", ".tsv", ".txt"})
XLSX_SUFFIXES = frozenset({".xlsx", ".xlsm"})
JSONL_SUFFIXES = frozenset({".jsonl", ".ndjson"})
SUPPORTED_SUFFIXES = CSV_SUFFIXES | XLSX_SUFFIXES | JSONL_SUFFIXES

DEFAULT_DATETIME_FORMATS: tuple[str, ...] = (
    "%d.%m.%Y %H:%M:%S",
    "%d.%m.%Y %H:%M",
    "%d.%m.%Y",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
    "%d/%m/%Y",
)
DEFAULT_TIMEZONE = "Europe/Istanbul"
UPDATED_AT = "updated_at"
TEXT_ATTRIBUTES = frozenset(
    {"external_id", "group_key", "summary", "description", "status", "issue_type", "priority", "reporter"}
)
LIST_ATTRIBUTES = frozenset({"labels", "components"})
DATETIME_ATTRIBUTES = frozenset({"created_at"})
SNAPSHOT_ATTRIBUTES = TEXT_ATTRIBUTES | LIST_ATTRIBUTES | DATETIME_ATTRIBUTES

_TURKISH_FOLD = str.maketrans("çÇğĞıİöÖşŞüÜ", "cCgGiioOsSuU")
_WS_RE = re.compile(r"\s+")
_LIST_SPLIT_RE = re.compile(r"[;,]")
_SNIFF_CANDIDATES = (";", ",", "\t")

Row = Mapping[str, Any]


def fold_header(header: str) -> str:
    """Tolerant comparison form of a column header (see module docstring)."""
    text = _WS_RE.sub(" ", str(header).strip()).translate(_TURKISH_FOLD)
    text = "".join(ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch))
    return text.casefold()


def resolve_header(wanted: str, headers: Iterable[str]) -> str | None:
    """Exact header match first, then the first header with the same folded form."""
    header_list = list(headers)
    if wanted in header_list:
        return wanted
    folded = fold_header(wanted)
    return next((h for h in header_list if fold_header(h) == folded), None)


def cell_text(value: Any) -> str:
    """Stripped text of a cell (CSV string, XLSX number/date, JSON scalar or list)."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        # XLSX stores numeric codes (branch codes, ids) as floats.
        return str(int(value))
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, list):
        return ", ".join(t for t in (cell_text(v) for v in value) if t)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value).strip()


def parse_cell_datetime(value: Any, formats: Sequence[str], tz: tzinfo) -> datetime | None:
    """ISO 8601 first, then ``formats``; naive results are interpreted in ``tz``."""
    parsed: datetime | None = None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime(value.year, value.month, value.day)
    else:
        text = cell_text(value)
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            for fmt in formats:
                try:
                    parsed = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
    if parsed is None:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=tz)


def row_digest_key(row: Row) -> str:
    pairs = sorted((str(h).strip(), cell_text(v)) for h, v in row.items() if cell_text(v))
    canonical = json.dumps(pairs, ensure_ascii=False, separators=(",", ":"))
    return "sha256-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def _split_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [t for t in (cell_text(v) for v in value) if t]
    return [p.strip() for p in _LIST_SPLIT_RE.split(cell_text(value)) if p.strip()]


class CsvImportAdapter:
    """``PullSourceAdapter`` for CSV/TSV, XLSX and JSONL incident exports."""

    def __init__(self, settings: SourceSettings) -> None:
        self.source_id = settings.id
        self.poll_interval_seconds = settings.poll_interval_seconds
        self._field_mapping = dict(settings.field_mapping)
        options = settings.options
        self._id_column: str | None = options.get("id_column") or None
        self._delimiter: str | None = options.get("delimiter") or None
        self._encoding: str = options.get("encoding") or "utf-8-sig"
        self._sheet: str | None = options.get("sheet") or None
        watch = options.get("watch_directory")
        self._watch_directory: Path | None = Path(watch) if watch else None
        self._batch: str | None = options.get("batch") or None
        self._datetime_formats: tuple[str, ...] = tuple(
            options.get("datetime_formats") or DEFAULT_DATETIME_FORMATS
        )
        self._tz = ZoneInfo(options.get("timezone") or DEFAULT_TIMEZONE)

    # -- PullSourceAdapter -------------------------------------------------------------------------------

    def normalize(self, payload: Mapping[str, Any], transform: TextTransform | None = None) -> IncidentEvent:
        return decode_event_envelope(self.source_id, payload, transform)

    def fetch(self, since: datetime | None, limit: int) -> Iterator[IncidentEvent]:
        """Events from files in ``watch_directory`` modified after ``since``, oldest file first.

        Every event carries ``event_timestamp`` = the file mtime (UTC), so a checkpoint taken from the fetched
        events skips the file next time. Files are never split: ``limit`` is honoured at file granularity
        (a later file is only deferred when its mtime is strictly newer than the last one emitted), because
        a partially emitted file would otherwise be skipped by the mtime checkpoint.
        """
        if self._watch_directory is None or limit <= 0:
            return
        candidates: list[tuple[datetime, Path]] = []
        for path in self._watch_directory.iterdir():
            if not path.is_file() or path.suffix.lower() not in SUPPORTED_SUFFIXES:
                continue
            if path.name.startswith((".", "~$")):  # hidden files, Excel lock files
                continue
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
            if since is None or mtime > since:
                candidates.append((mtime, path))
        candidates.sort()
        emitted = 0
        last_mtime: datetime | None = None
        for mtime, path in candidates:
            if emitted >= limit and last_mtime is not None and mtime > last_mtime:
                return
            for event in self._read(path, is_known=None, timestamp=mtime):
                emitted += 1
                yield event
            last_mtime = mtime

    # -- import ------------------------------------------------------------------------------------------

    def read_file(
        self,
        path: Path,
        *,
        is_known: Callable[[str], bool] | None = None,
        imported_at: datetime | None = None,
    ) -> Iterator[IncidentEvent]:
        """Events for every non-empty data row of ``path`` (dispatched by suffix).

        ``event_timestamp`` is ``imported_at`` (default: now) so that re-imports of edited rows win the
        per-attribute version check. Raises :class:`SourcePayloadError` with file/row context.
        """
        timestamp = imported_at or datetime.now(UTC)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        return self._read(Path(path), is_known=is_known, timestamp=timestamp)

    def _read(
        self, path: Path, *, is_known: Callable[[str], bool] | None, timestamp: datetime
    ) -> Iterator[IncidentEvent]:
        suffix = path.suffix.lower()
        if suffix in CSV_SUFFIXES:
            rows = self._csv_rows(path)
        elif suffix in XLSX_SUFFIXES:
            rows = self._xlsx_rows(path)
        elif suffix in JSONL_SUFFIXES:
            rows = self._jsonl_rows(path)
        else:
            raise SourcePayloadError(f"{path.name}: unsupported file type {suffix or '<none>'!r}")
        kind = "csv" if suffix in CSV_SUFFIXES else "xlsx" if suffix in XLSX_SUFFIXES else "jsonl"
        batch = self._batch or path.stem
        for row_number, row in rows:
            if not any(cell_text(v) for v in row.values()):
                continue
            yield self._row_event(
                row,
                where=f"{path.name} row {row_number}",
                raw_event_name=f"{kind}_import",
                batch=batch,
                timestamp=timestamp,
                is_known=is_known,
            )

    def _row_event(
        self,
        row: Row,
        *,
        where: str,
        raw_event_name: str,
        batch: str,
        timestamp: datetime,
        is_known: Callable[[str], bool] | None,
    ) -> IncidentEvent:
        headers = list(row)
        external_key: str | None = None
        if self._id_column:
            id_header = resolve_header(self._id_column, headers)
            if id_header is None:
                raise SourcePayloadError(f"{where}: id column {self._id_column!r} not found")
            id_value = _WS_RE.sub("-", cell_text(row[id_header]))
            if id_value:
                if not EXTERNAL_KEY_RE.match(id_value):
                    raise SourcePayloadError(f"{where}: invalid id {id_value[:40]!r} in {id_header!r}")
                external_key = id_value
        known = external_key is not None and is_known is not None and is_known(external_key)
        if external_key is None:
            external_key = row_digest_key(row)

        fields: dict[str, Any] = {}
        mapped: dict[str, str] = {}
        source_updated_at: datetime | None = None
        for canonical, wanted in self._field_mapping.items():
            header = resolve_header(wanted, headers)
            if header is None:
                continue
            value = row[header]
            if not cell_text(value):
                continue
            if canonical == UPDATED_AT:
                source_updated_at = parse_cell_datetime(value, self._datetime_formats, self._tz)
            elif canonical in DATETIME_ATTRIBUTES:
                parsed = parse_cell_datetime(value, self._datetime_formats, self._tz)
                if parsed is not None:
                    fields[canonical] = parsed
            elif canonical in LIST_ATTRIBUTES:
                fields[canonical] = _split_list(value)
            elif canonical in TEXT_ATTRIBUTES:
                fields[canonical] = cell_text(value)
            else:
                mapped[canonical] = cell_text(value)
        group_key = fields.setdefault("group_key", batch)

        return IncidentEvent(
            source=self.source_id,
            event_type=IncidentEventType.RECORD_UPDATED if known else IncidentEventType.RECORD_CREATED,
            raw_event_name=raw_event_name,
            external_key=external_key,
            external_id=fields.get("external_id"),
            group_key=group_key,
            event_timestamp=timestamp,
            source_updated_at=source_updated_at,
            fields=fields,
            mapped_fields=mapped,
        )

    # -- readers (yield (row number, header -> value)) ---------------------------------------------------

    def _csv_rows(self, path: Path) -> Iterator[tuple[int, Row]]:
        try:
            text = path.read_text(encoding=self._encoding)
        except UnicodeDecodeError as exc:
            raise SourcePayloadError(f"{path.name}: not decodable as {self._encoding}: {exc}") from exc
        delimiter = self._delimiter or ("\t" if path.suffix.lower() == ".tsv" else sniff_delimiter(text))
        reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
        try:
            headers: list[str] | None = None
            for record in reader:
                if headers is None:
                    if any(c.strip() for c in record):
                        headers = [c.strip() for c in record]
                    continue
                yield reader.line_num, _zip_row(headers, record)
        except csv.Error as exc:
            raise SourcePayloadError(f"{path.name} line {reader.line_num}: {exc}") from exc

    def _xlsx_rows(self, path: Path) -> Iterator[tuple[int, Row]]:
        try:
            openpyxl: Any = importlib.import_module("openpyxl")
        except ImportError as exc:
            raise SourcePayloadError(
                "XLSX import requires the optional dependency 'openpyxl' "
                "(pip install 'branchops-intelligence[xlsx]')"
            ) from exc
        try:
            workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
        except Exception as exc:  # openpyxl raises zipfile/XML/KeyError variants for broken files
            raise SourcePayloadError(f"{path.name}: cannot open workbook: {exc}") from exc
        try:
            if self._sheet:
                if self._sheet not in workbook.sheetnames:
                    raise SourcePayloadError(f"{path.name}: sheet {self._sheet!r} not found")
                sheet = workbook[self._sheet]
            else:
                sheet = workbook.worksheets[0]
            headers: list[str] | None = None
            for number, values in enumerate(sheet.iter_rows(values_only=True), start=1):
                cells = list(values)
                if headers is None:
                    if any(cell_text(c) for c in cells):
                        headers = [cell_text(c) for c in cells]
                    continue
                yield number, _zip_row(headers, cells)
        finally:
            workbook.close()

    def _jsonl_rows(self, path: Path) -> Iterator[tuple[int, Row]]:
        try:
            lines = path.read_text(encoding=self._encoding).splitlines()
        except UnicodeDecodeError as exc:
            raise SourcePayloadError(f"{path.name}: not decodable as {self._encoding}: {exc}") from exc
        for number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SourcePayloadError(f"{path.name} line {number}: invalid JSON: {exc.msg}") from exc
            if not isinstance(record, dict):
                raise SourcePayloadError(f"{path.name} line {number}: expected a JSON object")
            yield number, {str(k): v for k, v in record.items()}


def _zip_row(headers: Sequence[str], cells: Sequence[Any]) -> dict[str, Any]:
    row: dict[str, Any] = {}
    for idx, header in enumerate(headers):
        if header and header not in row:  # unnamed and duplicate columns are ignored
            row[header] = cells[idx] if idx < len(cells) else None
    return row


def sniff_delimiter(text: str) -> str:
    """Most frequent candidate in the header line, then ``csv.Sniffer``, then ","."""
    first = next((line for line in text.splitlines() if line.strip()), "")
    counts = {d: first.count(d) for d in _SNIFF_CANDIDATES}
    best = max(counts, key=lambda d: counts[d])
    if counts[best] > 0 and list(counts.values()).count(counts[best]) == 1:
        return best
    try:
        return csv.Sniffer().sniff(text[:65536], delimiters="".join(_SNIFF_CANDIDATES)).delimiter
    except csv.Error:
        return ","
