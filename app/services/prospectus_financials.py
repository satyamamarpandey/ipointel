from __future__ import annotations
"""Point-in-time US pre-IPO financials from the prospectus itself (A-001).

The final prospectus (424B4) and the registration statement (S-1/F-1) print a
"Summary (Consolidated) Financial Data" section: audited annual figures for
the last two or three fiscal years, often beside interim columns and pro
forma columns. These values were public on the prospectus filing date, so
they may enter a leakage-safe model dataset with
  availability_rule  "prospectus_filing"
  available_at       the EDGAR "FILED AS OF DATE" of the submission.

Parsing works on the HTML table grid (colspans expanded) rather than on
flattened text, so each number is mapped to the column group (annual vs
interim vs pro forma) and fiscal year printed above it. Only annual,
historical, USD columns are used. Anything ambiguous yields no value:
missing stays missing.
"""
import html as html_lib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from sqlalchemy import select
from sqlalchemy.orm import Session
from ..models import IPO, FeatureObservation

SOURCE_NAME = "SEC prospectus summary financial data"
PROVENANCE_SOURCE = "SEC prospectus (summary financial data table)"
RULE_PROSPECTUS = "prospectus_filing"
CONFIDENCE_EXACT_PERIOD = 0.95
CONFIDENCE_YEAR_ONLY = 0.85
SECTION_MAX_TABLES = 8
FIELDS = ("revenue_m", "revenue_prev_m", "revenue_2y_ago_m", "net_income_m", "cfo_m")
IPO_COLUMNS = {"revenue_m": "revenue_m", "net_income_m": "net_income_m", "cfo_m": "cfo_m"}

_HEADING = re.compile(r"^\s*summary\b[\w ,&()\-]{0,90}?\bfinancial\b[\w ,&()\-]{0,40}?\b(?:data|information|highlights)\b", re.I)
_SECTION_END = re.compile(r"^\s*risk factors\s*$", re.I)
_YEAR = re.compile(r"\b(19[89]\d|20[0-4]\d)\b")
_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august",
           "september", "october", "november", "december")
_MONTH_DAY = re.compile(r"\b(" + "|".join(_MONTHS) + r")\s+(\d{1,2})\b", re.I)
_ANNUAL = re.compile(r"\b(?:fiscal )?years?\b[^|]{0,30}\bended\b|\bfiscal year\b|\byear ended\b|\btwelve months ended\b", re.I)
_INTERIM = re.compile(r"\b(?:three|six|nine|3|6|9)\s*(?:-\s*)?months?\b|\bquarters? ended\b|\binterim\b", re.I)
_PRO_FORMA = re.compile(r"pro\s*forma|as adjusted", re.I)
_NON_USD = re.compile(r"\bRMB\b|\bHK\$|\bNT\$|\bS\$|\bA\$|\bC\$|€|£|¥|\bINR\b|\bRs\.?\b|\bEUR\b|\bGBP\b|\bJPY\b|\bMYR\b|\bRM\b|\bSGD\b|\bHKD\b|\bNIS\b", re.I)
_USD_COL = re.compile(r"\bUS\$|\bUSD\b|\bU\.S\. ?\$", re.I)
_SCALE_MARKERS = re.compile(
    r"(?P<k>in\s+(?:(?:us|u\.s\.|usd)?\s*\$?\s*)?thousands|thousands\s+of\s+(?:u\.?s\.?\s*)?(?:dollars|\$|usd)"
    r"|(?<![\d,])(?:us\$|usd|\$)\s*['’]?\s*000(?![\d,])|\(\s*000s?\s*\)|\bin\s+000s?\b|['’]000\b)"
    r"|(?P<m>in\s+(?:(?:us|u\.s\.|usd)?\s*\$?\s*)?millions|millions\s+of\s+(?:u\.?s\.?\s*)?(?:dollars|\$|usd)|\(\s*\$?\s*(?:in\s+)?millions\s*\))",
    re.I)
_NUMBER = re.compile(r"^\(?\s*-?\$?\s*\(?\s*(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?\s*\)?$")

# First text cell of a row -> field. Order matters: the first matching row wins.
_ROW_PATTERNS = {
    "revenue": re.compile(r"^(?:total )?(?:net )?(?:operating )?(?:revenues?|sales)(?:,? net)?(?: from [\w ]+)?$|^total (?:net )?revenues?(?: and other income)?$|^revenues?,? net$", re.I),
    "net_income": re.compile(r"^net (?:\(?loss\)?|\(?income\)?|\(?loss\)? ?(?:/ ?)?\(?income\)?|\(?income\)? ?(?:/ ?)?\(?loss\)?)(?: and comprehensive (?:\(?loss\)?|\(?income\)?)(?: ?\(?(?:loss|income)\)?)?)?$", re.I),
    "cfo": re.compile(r"^net cash (?:\(?(?:provided by|used in|from|generated from)\)?[ /]*(?:\(?(?:provided by|used in|from|generated from)\)?)?) ?operating activities$", re.I),
}


_CFO_CAPTION = re.compile(r"^net cash (?:\(?(?:provided by|used in|from)\)?[ /]*){1,2}:?$", re.I)


@dataclass
class Cell:
    col: int
    span: int
    text: str


@dataclass
class Table:
    rows: list[list[Cell]] = field(default_factory=list)
    preceding_text: str = ""


def _int_attr(v: str | None) -> int:
    try:
        return max(1, min(50, int(str(v or "1").strip().rstrip(";"))))
    except ValueError:
        return 1


class _GridParser(HTMLParser):
    """Stream of ("text", str) and ("table", Table) events. Nested tables are
    flattened into the outer one."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.events: list[tuple[str, object]] = []
        self._depth = 0
        self._table: Table | None = None
        self._row: list[Cell] | None = None
        self._cell: list[str] | None = None
        self._span = 1
        self._rowspan = 1
        self._col = 0
        self._occupied: dict[int, int] = {}  # grid column -> later rows still covered by a rowspan
        self._row_occupied: set[int] = set()
        self._text: list[str] = []
        self._recent = ""

    def _skip_occupied(self) -> None:
        while self._col in self._row_occupied:
            self._col += 1

    def _flush_text(self) -> None:
        t = re.sub(r"\s+", " ", "".join(self._text)).strip()
        self._text = []
        if t:
            self.events.append(("text", t))
            self._recent = (self._recent + " " + t)[-1500:]

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            if self._depth == 0:
                self._flush_text()
                self._table = Table(preceding_text=self._recent)
                self._occupied = {}
            self._depth += 1
        elif self._depth and tag == "tr":
            self._row, self._col = [], 0
            # Columns covered by a rowspan from an earlier row, for this row only.
            self._row_occupied = {k for k, v in self._occupied.items() if v > 0}
            self._occupied = {k: v - 1 for k, v in self._occupied.items() if v > 1}
            self._skip_occupied()
        elif self._depth and tag in ("td", "th") and self._row is not None:
            self._cell = []
            a = dict(attrs)
            self._span = _int_attr(a.get("colspan"))
            self._rowspan = _int_attr(a.get("rowspan"))
        elif self._depth and self._cell is not None and tag in ("br", "p", "div"):
            self._cell.append(" ")
        elif not self._depth and tag in ("p", "div", "br", "h1", "h2", "h3", "h4", "li"):
            self._flush_text()

    def handle_endtag(self, tag):
        if self._depth and tag in ("td", "th") and self._cell is not None and self._row is not None:
            text = re.sub(r"\s+", " ", "".join(self._cell)).replace("​", "").strip()
            self._row.append(Cell(self._col, self._span, text))
            if self._rowspan > 1:
                for k in range(self._col, self._col + self._span):
                    self._occupied[k] = self._rowspan - 1
            self._col += self._span
            self._cell = None
            self._skip_occupied()
        elif self._depth and tag == "tr" and self._row is not None and self._table is not None:
            if any(c.text for c in self._row):
                self._table.rows.append(self._row)
            self._row = None

        elif tag == "table" and self._depth:
            self._depth -= 1
            if self._depth == 0 and self._table is not None:
                self.events.append(("table", self._table))
                self._table = None
        elif not self._depth and tag in ("p", "div", "h1", "h2", "h3", "h4", "li"):
            self._flush_text()

    def handle_data(self, data):
        if self._depth:
            if self._cell is not None:
                self._cell.append(data)
        else:
            self._text.append(data)

    def close(self):
        super().close()
        self._flush_text()


def first_document(raw: str) -> str:
    """The prospectus is the first <DOCUMENT> of an EDGAR .txt submission."""
    end = raw.find("</DOCUMENT>")
    return raw if end < 0 else raw[:end]


def filed_date(raw: str) -> str | None:
    m = re.search(r"FILED AS OF DATE:\s*(\d{4})(\d{2})(\d{2})", raw[:5000])
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


def submission_type(raw: str) -> str | None:
    m = re.search(r"CONFORMED SUBMISSION TYPE:\s*(\S+)", raw[:5000])
    return m.group(1) if m else None


_STATEMENT_HEADING = re.compile(
    r"^\s*(?:(?:audited|unaudited|consolidated|combined|carve-out|condensed)\s+)*statements?\s+of\s+"
    r"(?:(?:consolidated\s+)?operations|income|(?:comprehensive\s+)?(?:loss|income)|operations\s+and\s+comprehensive\s+(?:loss|income)"
    r"|comprehensive\s+(?:loss|income)|cash\s+flows)\b", re.I)


def parse_events(doc_html: str) -> list[tuple[str, object]]:
    p = _GridParser()
    p.feed(doc_html)
    p.close()
    return p.events


def section_tables(events: list[tuple[str, object]], heading: re.Pattern, per_heading: int) -> list[Table]:
    """Tables following each heading that sits outside a table (the table
    of contents is a table), at most per_heading per heading, stopping at
    "Risk Factors" or the next heading of the same kind. Document order;
    the caller takes the first that yields values."""
    out: list[Table] = []
    for i, (kind, val) in enumerate(events):
        if kind != "text" or len(val) > 160 or not heading.search(val):
            continue
        taken = 0
        for kind2, val2 in events[i + 1:]:
            if kind2 == "text" and (_SECTION_END.search(val2) or (len(val2) <= 160 and heading.search(val2))):
                break
            if kind2 == "table":
                out.append(val2)
                taken += 1
                if taken >= per_heading:
                    break
        if len(out) >= SECTION_MAX_TABLES * 4:
            break
    return out


def is_blank_check(raw: str) -> bool:
    """EDGAR header SIC 6770 ("BLANK CHECKS"): a SPAC with no operating history."""
    return bool(re.search(r"STANDARD INDUSTRIAL CLASSIFICATION:[^\n]*\[6770\]", raw[:6000]))


def summary_tables(doc_html: str) -> list[Table]:
    return section_tables(parse_events(doc_html), _HEADING, SECTION_MAX_TABLES)


def _number(text: str) -> float | None:
    t = text.replace("$", "").replace("US", "").replace(" ", "").replace("—", "").replace("–", "")
    if t in ("", "-", "—"):
        return None
    m = _NUMBER.match(t)
    if not m:
        return None
    v = float(m.group(1).replace(",", "") + ("." + m.group(2) if m.group(2) else ""))
    return -v if "(" in t or t.startswith("-") else v


def _scale_marker(text: str) -> float | None:
    """Multiplier of the LAST unit marker in text (nearest to the table)."""
    last = None
    for m in _SCALE_MARKERS.finditer(text):
        last = 1e3 if m.group("k") else 1e6
    return last


def _table_scale(table: Table, start: int) -> float | None:
    """The table's own unit note (header rows and the first labels), else
    the nearest note in the text just above it; None when neither says."""
    own = " ".join(c.text for r in table.rows[:start + 3] for c in r[:1] + (r if table.rows.index(r) < start else []))
    return _scale_marker(own) or _scale_marker(table.preceding_text[-1500:])


@dataclass(frozen=True)
class Column:
    col: int
    year: int
    annual: bool
    pro_forma: bool
    month_day: tuple[int, int] | None
    usd: bool


def _covering(cells: list[Cell], col: int) -> Cell | None:
    for c in cells:
        if c.col <= col < c.col + c.span and c.text:
            return c
    return None


_CAPTION_ANNUAL = re.compile(r"\b(?:fiscal )?years? ended\s+(" + "|".join(_MONTHS) + r")\s+(\d{1,2})", re.I)
_CAPTION_INTERIM = re.compile(r"\b(?:months|periods?|quarters?) ended\s+(" + "|".join(_MONTHS) + r")\s+(\d{1,2})", re.I)


def _annual_from_caption(context: str, month_day: tuple[int, int] | None) -> bool:
    """Column headers without a period label: use the caption above the
    table. Annual only when the column's month/day is the caption's
    year-end date and not also its interim date; unknown fails closed."""
    def md(m):
        return (_MONTHS.index(m.group(1).lower()) + 1, int(m.group(2))) if m else None
    annual_md, interim_md = md(_CAPTION_ANNUAL.search(context)), md(_CAPTION_INTERIM.search(context))
    if annual_md is None and interim_md is None:
        return bool(_ANNUAL.search(context)) and not _INTERIM.search(context)
    if annual_md is not None and interim_md is None and month_day is None:
        return True
    if month_day is None or annual_md is None:
        return False
    return month_day == annual_md and month_day != interim_md


def _header_end(table: Table) -> int | None:
    """Index of the first data row: a text label followed by at least one
    number that is not a bare year."""
    for i, row in enumerate(table.rows):
        nums = [c for c in row[1:] if _number(c.text) is not None and not _YEAR.fullmatch(c.text.strip())]
        if row and row[0].text and not _YEAR.search(row[0].text) and nums:
            return i
    return None


def _columns(table: Table) -> tuple[list[Column], int]:
    """One column per grid position that carries numbers in the data rows,
    described by every header cell printed above it (group label, date,
    year, currency, audit status). A position whose header names no single
    fiscal year is dropped. Returns (columns, first data row index)."""
    start = _header_end(table)
    if start is None or start == 0:
        return [], len(table.rows)
    header, data = table.rows[:start], table.rows[start:]
    positions = sorted({c.col for r in data for c in r[1:] if _number(c.text) is not None})
    context = table.preceding_text[-400:]
    cols: list[Column] = []
    for p in positions:
        cover = [x for r in header for x in [_covering(r, p)] if x is not None]
        years = {int(y) for x in cover for y in _YEAR.findall(x.text) if len(x.text) <= 60}
        if len(years) != 1:
            continue
        year = years.pop()
        above = " | ".join(x.text for x in cover)
        md = None
        for x in cover:
            md = md or _MONTH_DAY.search(x.text)
        month_day = (_MONTHS.index(md.group(1).lower()) + 1, int(md.group(2))) if md else None
        if _ANNUAL.search(above) or _INTERIM.search(above):
            annual = bool(_ANNUAL.search(above)) and not _INTERIM.search(above)
        else:
            annual = _annual_from_caption(context, month_day)
        usd = not _NON_USD.search(above) or bool(_USD_COL.search(above))
        cols.append(Column(p, year, annual, bool(_PRO_FORMA.search(above)), month_day, usd))
    return cols, start


def _values(row: list[Cell], cols: list[Column]) -> dict[int, float]:
    """{column grid position: value} for the numbers in a data row. Exact
    position first, else a column one grid cell away."""
    out: dict[int, float] = {}
    positions = {c.col for c in cols}
    for cell in row[1:]:
        v = _number(cell.text)
        if v is None:
            continue
        target = cell.col if cell.col in positions else next((p for p in (cell.col - 1, cell.col + 1) if p in positions), None)
        if target is not None and target not in out:
            out[target] = v
    return out


def _period_end(c: Column) -> str:
    if c.month_day:
        m, d = c.month_day
        return f"{c.year:04d}-{m:02d}-{d:02d}"
    return f"{c.year:04d}-12-31"


def _row_label(row: list[Cell]) -> str:
    label = row[0].text if row and row[0].text else ""
    label = re.sub(r"\((?:note\s*)?\d+\)|\[\d\]|\*+|†|:$", "", label, flags=re.I).strip()
    label = re.sub(r"\s*[,–—-]\s*(?:from )?related part(?:y|ies)$", "", label, flags=re.I)
    return re.sub(r"\s+", " ", label)


def _annual_pairs(row: list[Cell], cols: list[Column], usable: list[Column], mult: float) -> list[tuple[Column, float]]:
    vals = _values(row, cols)
    pairs = [(c, vals[c.col] * mult) for c in usable if c.col in vals]
    # One value per fiscal year; if a year repeats (restated columns), keep the first printed.
    seen, uniq = set(), []
    for c, v in sorted(pairs, key=lambda cv: -cv[0].year):
        if c.year not in seen:
            seen.add(c.year)
            uniq.append((c, v))
    return uniq


def parse_table(table: Table, default_scale: float = 1.0) -> tuple[dict[str, list[tuple[Column, float]]], float | None]:
    """({"revenue"|"net_income"|"cfo": [(annual column, value in USD), ...]}
    newest first, the table's own unit multiplier or None). Only annual,
    historical, USD columns. A table without its own unit note uses
    default_scale (the note of an earlier table in the same section)."""
    cols, start = _columns(table)
    own_scale = _table_scale(table, start) if cols else None
    usable = [c for c in cols if c.annual and not c.pro_forma and c.usd]
    if not usable:
        return {}, own_scale
    mult = own_scale or default_scale
    found: dict[str, list[tuple[Column, float]]] = {}
    # Label-only caption rows ("Revenues") can sit between the last header
    # row that prints a year and the first numeric row.
    last_year_row = max((i for i, r in enumerate(table.rows[:start]) if any(_YEAR.search(c.text) for c in r[1:])), default=-1)
    data = table.rows[last_year_row + 1:]
    prev_label = ""
    for i, row in enumerate(data):
        label = _row_label(row)
        if "cfo" not in found and _CFO_CAPTION.match(prev_label) and re.match(r"^operating activities$", label, re.I):
            uniq = _annual_pairs(row, cols, usable, mult)
            if uniq:
                found["cfo"] = uniq
        prev_label = label
        for key, rx in _ROW_PATTERNS.items():
            if key in found or not rx.match(label):
                continue
            uniq = _annual_pairs(row, cols, usable, mult)
            if not uniq and key == "revenue":
                # "Revenues" as a group caption: the total is the next row
                # that is unlabeled or starts with "total".
                for nxt in data[i + 1:i + 9]:
                    nl = _row_label(nxt).lower()
                    if "expense" in nl or "cost" in nl:
                        break  # the revenue block ended without a total
                    if nl == "" or re.match(r"^total\b.*\b(?:revenues?|sales)\b", nl):
                        uniq = _annual_pairs(nxt, cols, usable, mult)
                        if uniq:
                            break
            if uniq:
                found[key] = uniq
    return found, own_scale


SECTION_SUMMARY = "summary financial data"
SECTION_STATEMENTS = "audited financial statements"


def extract(raw_submission: str) -> dict:
    """Observations keyed by feature name (same shape as xbrl_financials),
    or {} when nothing yields annual USD figures. Pure: no I/O.
    The summary section is read first; the audited statements (F-pages)
    fill any field it lacks (smaller reporting companies may omit the
    summary)."""
    filed = filed_date(raw_submission)
    if filed is None:
        return {}
    form = submission_type(raw_submission) or ""
    events = parse_events(first_document(raw_submission))
    merged: dict[str, tuple[list[tuple[Column, float]], str]] = {}
    for section, tables in ((SECTION_SUMMARY, section_tables(events, _HEADING, SECTION_MAX_TABLES)),
                            (SECTION_STATEMENTS, section_tables(events, _STATEMENT_HEADING, 1))):
        section_scale = 1.0
        for table in tables:
            found, own = parse_table(table, section_scale)
            section_scale = own or section_scale
            for key, vals in found.items():
                merged.setdefault(key, (vals, section))
            if {"revenue", "net_income", "cfo"} <= merged.keys():
                break
    out: dict[str, dict] = {}

    def ob(c: Column, v: float, concept: str, section: str) -> dict:
        return {"value": v / 1e6, "period_end": _period_end(c), "period_start": "", "source_form": form,
                "filed": filed, "available_at": filed, "availability_rule": RULE_PROSPECTUS,
                "confidence": CONFIDENCE_EXACT_PERIOD if c.month_day else CONFIDENCE_YEAR_ONLY,
                "concept": concept, "section": section, "unit": "USD_m", "fiscal_year": c.year}

    rev, rev_section = merged.get("revenue") or ([], "")
    for name, idx in (("revenue_m", 0), ("revenue_prev_m", 1), ("revenue_2y_ago_m", 2)):
        if len(rev) > idx and rev[idx][1] >= 0:
            out[name] = ob(rev[idx][0], rev[idx][1], "revenue", rev_section)
    for key, name, concept in (("net_income", "net_income_m", "net income (loss)"), ("cfo", "cfo_m", "net cash from operating activities")):
        if merged.get(key):
            vals, section = merged[key]
            out[name] = ob(vals[0][0], vals[0][1], concept, section)
    # All latest-year fields must describe the same fiscal period: anchored on
    # revenue when present, else on the latest period found.
    latest_keys = [k for k in ("revenue_m", "net_income_m", "cfo_m") if k in out]
    if latest_keys:
        anchor = out["revenue_m"]["period_end"] if "revenue_m" in out else max(out[k]["period_end"] for k in latest_keys)
        for k in latest_keys:
            if out[k]["period_end"] != anchor:
                del out[k]
    if "revenue_m" not in out:
        out.pop("revenue_prev_m", None)
        out.pop("revenue_2y_ago_m", None)
    return out


def apply_observations(db: Session, ipo: IPO, obs: dict, source_url: str) -> dict:
    """Upsert FeatureObservation rows; fill IPO display columns only when
    empty. Never overwrites another source's value."""
    from .pipeline import add_provenance  # local import: pipeline imports scoring which imports models
    filled: list[str] = []
    for name, o in obs.items():
        existing = db.scalar(select(FeatureObservation).where(
            FeatureObservation.ipo_id == ipo.id, FeatureObservation.field_name == name,
            FeatureObservation.period_end == o["period_end"], FeatureObservation.source_name == SOURCE_NAME))
        if existing is None:
            existing = FeatureObservation(ipo_id=ipo.id, field_name=name, source_name=SOURCE_NAME, period_end=o["period_end"])
            db.add(existing)
        existing.value = o["value"]
        existing.unit = o["unit"]
        existing.source_url = source_url
        existing.source_tier = 1
        existing.source_form = o["source_form"]
        existing.period_start = ""
        existing.available_at = o["available_at"]
        existing.availability_rule = o["availability_rule"]
        existing.confidence = o["confidence"]
        existing.observed_at = datetime.now(timezone.utc)
        existing.raw = {k: v for k, v in o.items() if k != "value"}
        col = IPO_COLUMNS.get(name)
        if col and getattr(ipo, col) is None:
            setattr(ipo, col, o["value"])
            add_provenance(db, ipo, col, o["value"], PROVENANCE_SOURCE, source_url, 1)
            filled.append(col)
    if filled:
        ipo.updated_at = datetime.now(timezone.utc)
    return {"observations": len(obs), "columns_filled": filled}


def decode_entities(text: str) -> str:
    return html_lib.unescape(text)


AGREEMENT_TOLERANCE = 0.01


def agreement_with_xbrl(db: Session, xbrl_source: str = "SEC XBRL companyfacts") -> dict:
    """Evidence for A-001. Per field: IPOs with both sources, how often the
    values for the same fiscal period agree within 1%, and how often the
    XBRL comparative's latest period is LATER than anything the prospectus
    printed (a figure that did not exist at the IPO)."""
    rows = db.scalars(select(FeatureObservation).where(
        FeatureObservation.source_name.in_([SOURCE_NAME, xbrl_source]),
        FeatureObservation.field_name.in_(["revenue_m", "net_income_m", "cfo_m"]))).all()
    by: dict[tuple[int, str], dict[str, list[FeatureObservation]]] = {}
    for o in rows:
        by.setdefault((o.ipo_id, o.field_name), {}).setdefault(o.source_name, []).append(o)
    out: dict[str, dict] = {}
    for (_, fname), src in by.items():
        p, x = src.get(SOURCE_NAME), src.get(xbrl_source)
        if not p or not x:
            continue
        s = out.setdefault(fname, {"both_sources": 0, "same_period": 0, "agree_within_1pct": 0, "xbrl_period_after_prospectus": 0})
        s["both_sources"] += 1
        p_latest = max(o.period_end for o in p)
        if max(o.period_end for o in x) > p_latest:
            s["xbrl_period_after_prospectus"] += 1
        pv = {o.period_end: o.value for o in p if o.value is not None}
        for o in x:
            if o.period_end in pv and o.value is not None:
                s["same_period"] += 1
                a, b = pv[o.period_end], o.value
                if abs(a - b) <= AGREEMENT_TOLERANCE * max(abs(a), abs(b), 1e-9):
                    s["agree_within_1pct"] += 1
    return out
