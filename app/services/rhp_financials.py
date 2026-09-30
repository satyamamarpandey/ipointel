from __future__ import annotations
"""Point-in-time India pre-IPO financials from the Red Herring Prospectus (A-002).

NSE archives every issue's RHP at nsearchives.nseindia.com/content/ipo/RHP_<SYMBOL>.zip.
The RHP is filed with the Registrar of Companies and published before the
issue opens, so its restated figures may enter a leakage-safe dataset with
  availability_rule  "prospectus_filing"
  available_at       the issue open date (never later than listing).

Extraction reads the PDF text (pypdf) of the first pages, finds the first
page that prints both a "Revenue from operations" row and a restated
profit row with at least two numbers each, and maps the numbers to the
fiscal-year columns named on that page. Only full fiscal years are used
(stub periods such as "three months ended June 30" are dropped). Values are
stored in INR millions (unit "INR_m"). Anything ambiguous yields nothing.
"""
import io
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session
from ..models import IPO, FeatureObservation
from .identity import normalize_date
from .net_safety import validate_outbound_url

SOURCE_NAME = "NSE RHP restated financial information"
RULE_PROSPECTUS = "prospectus_filing"
PROVENANCE_SOURCE = "NSE RHP (restated financial information)"
DISPLAY_COLUMNS = ("revenue_m", "revenue_prev_m", "revenue_2y_ago_m", "net_income_m")
RHP_URL = "https://nsearchives.nseindia.com/content/ipo/RHP_{symbol}.zip"
_ALLOWED_HOSTS = {"nsearchives.nseindia.com"}
_UA = {"User-Agent": "Mozilla/5.0 IPOIntelligence/2.0", "Referer": "https://www.nseindia.com/"}
MAX_ZIP_BYTES = 80_000_000
SCAN_PAGES = 220
CONFIDENCE = 0.9

_NUM = re.compile(r"\(?-?\d[\d,]*(?:\.\d+)?\)?")
_REVENUE = re.compile(r"^\s*(?:[ivx]+\.?\s+|\d+\.?\s+)?(?:total\s+)?revenue\s+from\s+operations?\b", re.I)
_PROFIT = re.compile(
    r"^\s*(?:[ivx]+\.?\s+|\d+\.?\s+)?(?:restated\s+)?(?:net\s+)?profit\s*/?\s*(?:\(loss\)\s*)?"
    r"(?:after\s+tax|for\s+the\s+(?:year|period)(?:\s*/\s*(?:year|period))?)\b"
    r"|^\s*(?:[ivx]+\.?\s+|\d+\.?\s+)?restated\s+(?:net\s+)?profit(?:\s*/\s*\(loss\))?(?:\s+after\s+tax)?(?:\s+for\s+the\s+(?:year|period))?\b", re.I)
_EXCLUDE = re.compile(r"segment|%|margin|per\s+share|from\s+(?:india|government|non)|share\s+of|before", re.I)
_UNIT = (
    (re.compile(r"(?:₹|rs\.?|inr)\s*(?:in\s+)?crores?|in\s+(?:₹\s*)?crores?", re.I), 10.0),
    (re.compile(r"(?:₹|rs\.?|inr)\s*(?:in\s+)?lakhs?|in\s+(?:₹\s*)?lakhs?|(?:₹|rs\.?|inr)\s*(?:in\s+)?lacs?|in\s+(?:₹\s*)?lacs?", re.I), 0.1),
    (re.compile(r"(?:₹|rs\.?|inr)\s*(?:in\s+)?millions?|in\s+(?:₹\s*)?millions?|(?:₹|rs\.?|inr)\s*(?:in\s+)?mn\b", re.I), 1.0),
)
_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december")
# Full or abbreviated month name ("Sep", "Sept.", "March").
_MONTH_RX = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?"
_COL_LABEL = re.compile(
    r"(?P<fiscal>(?:fiscal|fy|financial\s+year)\s*(?:year\s*)?(?P<fy>20\d{2}|\d{2}(?!\d)))"
    r"|(?P<date>(?P<month>" + _MONTH_RX + r")\s+(?P<day>\d{1,2}),?\s*(?P<year>20\d{2}))"
    r"|(?P<date2>(?P<day2>\d{1,2})\s*(?:st|nd|rd|th)?\s+(?P<month2>" + _MONTH_RX + r"),?\s*(?P<year2>20\d{2}))"
    r"|(?P<date3>(?<![\d.])(?P<day3>\d{1,2})[./-](?P<month3>\d{1,2})[./-](?P<year3>20\d{2}))", re.I)
_CAPTION_YEAR_END = re.compile(r"years?\s+ended\s+(?:as\s+)?(?:on\s+)?(?P<month>" + _MONTH_RX + r")\s+(?P<day>\d{1,2})", re.I)
# "Fiscal 2025 2024 2023" / "Fiscal 2025, 2024 and 2023": one prefix, several years.
_FISCAL_RUN = re.compile(r"\b(fiscal|fy|financial\s+years?)\s+((?:20\d{2}\s*(?:,|and|&)?\s*){2,})", re.I)


def _month_index(name: str) -> int:
    return _MONTHS.index(next(m for m in _MONTHS if m.startswith(name.lower().rstrip(".")[:3]))) + 1


def _expand_fiscal_runs(block: str) -> str:
    return _FISCAL_RUN.sub(lambda m: " ".join(f"Fiscal {y}" for y in re.findall(r"20\d{2}", m.group(2))) + " ", block)
# Text just before a date that marks it as the end of a part-year period.
_STUB_CONTEXT = re.compile(r"(?:\bto|\bperiod(?:\s+(?:ended|from))?|months?\s+ended|stub)[^|]{0,12}$", re.I)
_STUB = re.compile(r"\b(?:three|six|nine|3|6|9)\s+months?\b|\bperiod\s+ended\b|\bstub\b", re.I)


@dataclass(frozen=True)
class ColumnLabel:
    year: int
    month_day: tuple[int, int] | None  # None for "Fiscal YYYY" labels (India fiscal year ends March 31)
    stub: bool = False  # printed as the end of a part-year period ("... to March 31, 2024")


def _numbers(line: str) -> list[float]:
    out = []
    for tok in _NUM.findall(line):
        t = tok.replace(",", "")
        neg = t.startswith("(") or t.startswith("-")
        t = t.strip("()-")
        if not t or t.count(".") > 1:
            continue
        try:
            v = float(t)
        except ValueError:
            continue
        out.append(-v if neg else v)
    return out


def _row_values(line: str) -> list[float]:
    """Numbers printed after the row label. Footnote markers such as "(1)"
    and label references like "(C = A + B)" are removed first."""
    body = re.sub(r"\((?:[a-z]\s*[=+-].*?|[\divx]+\s*[+-]\s*[\divx]+.*?|\d{1,2}|note\s*\d+)\)", " ", line, flags=re.I)
    m = _REVENUE.match(body) or _PROFIT.match(body)
    tail = body[m.end():] if m else body
    return [v for v in _numbers(tail) if not (1990 <= abs(v) <= 2099 and float(v).is_integer())]


def _drop_note_refs(values: list[float], n_columns: int) -> list[float]:
    """Statements print a note number ("19", "II.1") between the row label
    and the amounts. Leading extras are dropped only when every one of them
    is a small whole number, so a genuine amount is never discarded."""
    extra = len(values) - n_columns
    if n_columns and extra > 0 and all(abs(v) < NOTE_REF_MAX and float(v).is_integer() for v in values[:extra]):
        return values[extra:]
    return values


NOTE_REF_MAX = 100
HEADER_MAX_LINES = 30
# Header labels sit close together; a longer run of text between two dates
# means the earlier one belongs to prose above the table.
LABEL_GAP_CHARS = 200


def _header_years(block: str) -> list[int]:
    """Bare years in the header, dropping any that sit in prose above it."""
    years: list[int] = []
    last_end = None
    for m in re.finditer(r"(?<![\d.,/])(20\d{2})(?![\d.,/])", block):
        if last_end is not None and m.start() - last_end > LABEL_GAP_CHARS:
            years = []
        last_end = m.end()
        years.append(int(m.group(1)))
    return years


def _labels(page: str, first_row: str, n_values: int | None = None) -> list[ColumnLabel]:
    """Column labels in reading order from the table header: the lines
    between the last "Particulars" line (else the last HEADER_MAX_LINES
    lines) and the first data row, joined so that labels wrapped over
    several lines ("March" / "31, 2023") still read as one. When the dated
    labels do not account for every value column, bare years under a
    "year ended <Month> <day>" caption are used instead."""
    lines = page.splitlines()
    try:
        end = lines.index(first_row)
    except ValueError:
        return []
    start = max(0, end - HEADER_MAX_LINES)
    for k in range(end - 1, start - 1, -1):
        if re.search(r"\bparticulars\b", lines[k], re.I):
            start = k
            break
    block = _expand_fiscal_runs(re.sub(r"\s+", " ", " ".join(lines[start:end])))
    # "For the year / period ended" is a generic caption, not a stub marker.
    block = re.sub(r"\byear\s*/\s*period\b", "year", block, flags=re.I)
    found = []
    last_end = None
    for m in _COL_LABEL.finditer(block):
        if last_end is not None and m.start() - last_end > LABEL_GAP_CHARS:
            found = []  # the earlier dates sat in prose above the table, not in its header
        last_end = m.end()
        stub = bool(_STUB_CONTEXT.search(block[max(0, m.start() - 45):m.start()]))
        if m.group("fiscal"):
            fy = int(m.group("fy"))
            found.append(ColumnLabel(fy + 2000 if fy < 100 else fy, None))
        elif m.group("date"):
            found.append(ColumnLabel(int(m.group("year")), (_month_index(m.group("month")), int(m.group("day"))), stub))
        elif m.group("date3"):
            found.append(ColumnLabel(int(m.group("year3")), (int(m.group("month3")), int(m.group("day3"))), stub))
        else:
            found.append(ColumnLabel(int(m.group("year2")), (_month_index(m.group("month2")), int(m.group("day2"))), stub))
    if n_values is not None and len(found) != n_values:
        cap = _CAPTION_YEAR_END.search(block)
        years = _header_years(block)
        if cap and len(years) == n_values:
            md = (_month_index(cap.group("month")), int(cap.group("day")))
            return [ColumnLabel(y, md) for y in years]
    return found


def _unit(page: str) -> float | None:
    for rx, mult in _UNIT:
        if rx.search(page):
            return mult
    return None


def _annual_mask(labels: list[ColumnLabel], page: str) -> list[bool]:
    """Full fiscal years only. "Fiscal YYYY" labels are annual. Dated labels
    are annual when they share the most common month/day and the page does
    not mark that column as a stub; a lone differing date is a stub period."""
    dated = [c.month_day for c in labels if c.month_day]
    common = max(set(dated), key=dated.count) if dated else None
    mask = []
    for c in labels:
        if c.month_day is None:
            mask.append(True)
        else:
            mask.append(not c.stub and c.month_day == common and dated.count(common) >= 2)
    if _STUB.search(page) and all(mask) and dated and len(set(dated)) == 1 and len(labels) >= 4:
        # "Three months ended June 30, 2026" alongside fiscal years that also
        # end June 30: the first column is the stub.
        mask[0] = False
    return mask


def _period_end(c: ColumnLabel) -> str:
    if c.month_day:
        return f"{c.year:04d}-{c.month_day[0]:02d}-{c.month_day[1]:02d}"
    return f"{c.year:04d}-03-31"  # Indian fiscal year


MAX_CANDIDATE_PAGES = 10
_PEER_PAGE = re.compile(r"comparison\s+(?:of\s+\S+\s+)?(?:\S+\s+){0,4}with\s+(?:our\s+)?(?:listed\s+)?(?:industry\s+)?peers|peer\s+(?:group\s+)?(?:financial\s+)?(?:kpis?|comparison)", re.I)
_OTHER_COMPANY_CAPTION = re.compile(r"\s*for\s+(?!the\b)[A-Z][\w&.\- ]{2,80}\b(?:ltd|limited)\b", re.I)


def _summary_pages(pages: list[str]):
    """(page index, revenue line, profit line) for each page with both rows,
    in page order, up to MAX_CANDIDATE_PAGES."""
    found = 0
    for i, tx in enumerate(pages):
        rev = prof = None
        for ln in tx.splitlines():
            if rev is None and _REVENUE.match(ln) and not _EXCLUDE.search(ln[:80]) and len(_row_values(ln)) >= 2:
                rev = ln
            elif prof is None and _PROFIT.match(ln) and not _EXCLUDE.search(ln[:60]) and len(_row_values(ln)) >= 2:
                prof = ln
        if rev and prof:
            yield i, rev, prof
            found += 1
            if found >= MAX_CANDIDATE_PAGES:
                return


def find_summary_page(pages: list[str]) -> tuple[int, str, str] | None:
    """The first candidate page, or None."""
    return next(_summary_pages(pages), None)


def availability_date(ipo) -> str:
    """The RHP is public before the issue opens. Use the open date, else the
    close date, else the day before listing (still after publication and
    never later than the prediction time). "" when none is known."""
    for d in (ipo.open_date, ipo.close_date):
        v = normalize_date(d)
        if v:
            return v
    ld = normalize_date(ipo.listing_date)
    if ld:
        from datetime import date, timedelta
        return (date.fromisoformat(ld) - timedelta(days=1)).isoformat()
    return ""


def extract(pages: list[str], available: str) -> tuple[dict, str]:
    """(observations keyed by feature name, reason). Pure: no I/O.
    `available` is the date the RHP was public (see availability_date)."""
    available = normalize_date(available)
    if not available:
        return {}, "no date to establish when the prospectus was public"
    first_reason = None
    for hit in _summary_pages(pages):
        obs, reason = _extract_page(pages, hit, available)
        if obs:
            return obs, reason
        first_reason = first_reason or reason
    return {}, first_reason or "no page with both revenue and restated profit rows"


def _is_peer_table(page: str, rev_line: str) -> bool:
    """A page comparing listed peers, or a table captioned with another
    company's name ("For Balaji Telefilms Ltd"), is not the issuer's data."""
    if _PEER_PAGE.search(page):
        return True
    lines = page.splitlines()
    k = lines.index(rev_line)
    return any(_OTHER_COMPANY_CAPTION.match(ln) for ln in lines[max(0, k - HEADER_MAX_LINES):k])


def _extract_page(pages: list[str], hit: tuple[int, str, str], available: str) -> tuple[dict, str]:
    i, rev_line, prof_line = hit
    page = pages[i]
    if _is_peer_table(page, rev_line):
        return {}, f"page {i + 1}: peer comparison"
    first_row = next((ln for ln in page.splitlines() if _REVENUE.match(ln) or _PROFIT.match(ln) or re.match(r"\s*equity\s+share\s+capital", ln, re.I)), rev_line)
    first_row = min((ln for ln in (first_row, rev_line) if ln in page.splitlines()), key=page.splitlines().index)
    rev, prof = _row_values(rev_line), _row_values(prof_line)
    n = min(len(rev), len(prof))
    # The header sits above the table's first data row; a revenue line further
    # up the page may belong to a different table, so try the nearest anchor first.
    labels = _labels(page, rev_line, n)
    if len(labels) != n and first_row != rev_line:
        labels = _labels(page, first_row, n) or labels
    rev, prof = _drop_note_refs(rev, len(labels)), _drop_note_refs(prof, len(labels))
    mult = _unit(page) or _unit(pages[i - 1] if i else "")
    if not labels:
        return {}, f"page {i + 1}: no fiscal-year column header"
    if mult is None:
        return {}, f"page {i + 1}: no unit (million / lakh / crore)"
    if len(rev) != len(labels) or len(prof) != len(labels):
        return {}, f"page {i + 1}: {len(labels)} columns but {len(rev)} revenue / {len(prof)} profit values"
    mask = _annual_mask(labels, page)
    annual = sorted([(labels[k], rev[k], prof[k]) for k in range(len(labels)) if mask[k]], key=lambda t: -t[0].year)
    if not annual:
        return {}, f"page {i + 1}: no full fiscal-year column"
    if len({_period_end(c) for c, _, _ in annual}) != len(annual):
        # The same year twice means several companies side by side (a peer table).
        return {}, f"page {i + 1}: repeated fiscal years (peer comparison table)"

    def ob(c: ColumnLabel, v: float, concept: str) -> dict:
        return {"value": round(v * mult, 4), "period_end": _period_end(c), "available_at": available,
                "availability_rule": RULE_PROSPECTUS, "confidence": CONFIDENCE, "concept": concept,
                "unit": "INR_m", "page": i + 1, "fiscal_year": c.year}

    out: dict[str, dict] = {}
    for name, idx in (("revenue_m", 0), ("revenue_prev_m", 1), ("revenue_2y_ago_m", 2)):
        if len(annual) > idx and annual[idx][1] >= 0:
            out[name] = ob(annual[idx][0], annual[idx][1], "revenue from operations")
    out["net_income_m"] = ob(annual[0][0], annual[0][2], "restated profit after tax")
    return out, f"page {i + 1}"


def pick_pdf(names: list[str], size) -> str | None:
    pdfs = [n for n in names if n.lower().endswith(".pdf")]
    if not pdfs:
        return None
    return max(pdfs, key=lambda n: ("rhp" in n.lower() and "gid" not in n.lower() and "abridged" not in n.lower(), size(n)))


def pages_from_zip(content: bytes, max_pages: int = SCAN_PAGES) -> list[str]:
    from pypdf import PdfReader  # heavy import, only when parsing
    z = zipfile.ZipFile(io.BytesIO(content))
    name = pick_pdf(z.namelist(), lambda n: z.getinfo(n).file_size)
    if name is None:
        raise ValueError("no PDF in RHP archive")
    reader = PdfReader(io.BytesIO(z.read(name)))
    out = []
    for pg in reader.pages[:max_pages]:
        try:
            out.append(pg.extract_text() or "")
        except Exception:  # a single unreadable page must not lose the document
            out.append("")
    return out


def fetch_rhp(symbol: str, timeout: float = 120.0) -> bytes:
    url = RHP_URL.format(symbol=symbol.upper())
    validate_outbound_url(url, allowed_hosts=_ALLOWED_HOSTS)
    with httpx.Client(headers=_UA, timeout=timeout, follow_redirects=True) as c:
        with c.stream("GET", url) as r:
            r.raise_for_status()
            buf = bytearray()
            for chunk in r.iter_bytes():
                buf.extend(chunk)
                if len(buf) > MAX_ZIP_BYTES:
                    raise ValueError("RHP archive larger than cap")
    return bytes(buf)


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
        existing.source_form = "RHP"
        existing.available_at = o["available_at"]
        existing.availability_rule = o["availability_rule"]
        existing.confidence = o["confidence"]
        existing.observed_at = datetime.now(timezone.utc)
        existing.raw = {k: v for k, v in o.items() if k != "value"}
        if name in DISPLAY_COLUMNS and getattr(ipo, name) is None:
            setattr(ipo, name, o["value"])
            add_provenance(db, ipo, name, o["value"], PROVENANCE_SOURCE, source_url, 1)
            filled.append(name)
    if filled:
        ipo.updated_at = datetime.now(timezone.utc)
    return {"observations": len(obs), "columns_filled": filled}
