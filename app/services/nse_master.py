from __future__ import annotations
"""NSE's official listed-security masters, used to resolve an India IPO row
to its exchange symbol with Tier-1 evidence instead of a guess.

Two CSVs, both public and both maintained by the exchange itself:
  EQUITY_L.csv       every mainboard equity (SYMBOL, NAME OF COMPANY, ISIN, DATE OF LISTING ...)
  SME_EQUITY_L.csv   every NSE Emerge (SME) equity, same columns with underscores

Resolution is exact-match only (ISIN, then canonical issuer name with an ISIN
prefix check). Anything else is left unresolved with a stated reason - a
wrong symbol would attach another company's price history to an IPO, which
is worse than no history at all.
"""
import csv
import io
from dataclasses import dataclass
from datetime import date

import httpx

from .identity import canonical_name, normalize_date
from .net_safety import validate_outbound_url

EQUITY_MASTER_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
SME_MASTER_URL = "https://nsearchives.nseindia.com/emerge/corporates/content/SME_EQUITY_L.csv"
_ALLOWED_HOSTS = {"nsearchives.nseindia.com"}
_UA = {"User-Agent": "Mozilla/5.0 IPOIntelligence/2.0"}

SOURCE_MAINBOARD = "NSE equity master"
SOURCE_SME = "NSE SME equity master"

# ISIN suffix (last three characters) changes on a face-value change / split;
# the first nine characters identify the issuer and stay put.
ISIN_ISSUER_PREFIX_LEN = 9
# A live issue lists within about a week of closing (T+3 in India); a master
# row dated far outside that window is a different listing of the symbol.
LISTING_WINDOW_DAYS = 30

RESOLVED_ISIN = "RESOLVED_ISIN"
RESOLVED_NAME_ISIN_PREFIX = "RESOLVED_NAME_ISIN_PREFIX"
CONFLICTING = "CONFLICTING"
AMBIGUOUS = "AMBIGUOUS"
UNRESOLVED = "UNRESOLVED"


@dataclass(frozen=True)
class MasterRow:
    symbol: str
    name: str
    isin: str
    series: str
    board: str  # Mainboard | SME
    date_of_listing: str
    source_name: str
    source_url: str


def _norm_header(h: str) -> str:
    return " ".join(str(h or "").replace("_", " ").split()).upper()


def parse_master_csv(text: str, board: str, source_name: str, source_url: str) -> list[MasterRow]:
    """Pure parser for either master file. Header spellings differ between
    the two files ("NAME OF COMPANY" vs "NAME_OF_COMPANY", " ISIN NUMBER"
    with a leading space); both normalise to the same keys."""
    reader = csv.reader(io.StringIO(text.lstrip("﻿")))
    try:
        header = [_norm_header(h) for h in next(reader)]
    except StopIteration:
        return []
    idx = {h: i for i, h in enumerate(header) if h}
    def col(row, key):
        i = idx.get(key)
        return row[i].strip() if i is not None and i < len(row) else ""
    out = []
    for row in reader:
        if not row or not row[0].strip():
            continue
        symbol = col(row, "SYMBOL")
        if not symbol:
            continue
        out.append(MasterRow(symbol=symbol.upper(), name=col(row, "NAME OF COMPANY"), isin=col(row, "ISIN NUMBER").upper(),
                             series=col(row, "SERIES"), board=board, date_of_listing=col(row, "DATE OF LISTING"),
                             source_name=source_name, source_url=source_url))
    return out


def fetch_masters(timeout: float = 30.0) -> list[MasterRow]:
    rows: list[MasterRow] = []
    with httpx.Client(headers=_UA, timeout=timeout, follow_redirects=True) as c:
        for url, board, source in ((EQUITY_MASTER_URL, "Mainboard", SOURCE_MAINBOARD), (SME_MASTER_URL, "SME", SOURCE_SME)):
            validate_outbound_url(url, allowed_hosts=_ALLOWED_HOSTS)
            r = c.get(url)
            r.raise_for_status()
            rows.extend(parse_master_csv(r.text, board, source, url))
    return rows


class MasterIndex:
    """Exact-match lookups over the master rows."""
    def __init__(self, rows: list[MasterRow]):
        self.by_isin: dict[str, list[MasterRow]] = {}
        self.by_name: dict[str, list[MasterRow]] = {}
        self.by_symbol: dict[str, list[MasterRow]] = {}
        for r in rows:
            if r.isin:
                self.by_isin.setdefault(r.isin, []).append(r)
            c = canonical_name(r.name)
            if c:
                self.by_name.setdefault(c, []).append(r)
            self.by_symbol.setdefault(r.symbol, []).append(r)

    @staticmethod
    def _distinct(rows: list[MasterRow]) -> list[MasterRow]:
        seen, out = set(), []
        for r in rows:
            if r.symbol not in seen:
                seen.add(r.symbol)
                out.append(r)
        return out

    def resolve(self, isin: str, company: str) -> tuple[str, MasterRow | None, str]:
        """(classification, matched master row or None, reason)."""
        isin = (isin or "").upper().strip()
        if isin and isin in self.by_isin:
            rows = self._distinct(self.by_isin[isin])
            if len(rows) == 1:
                return RESOLVED_ISIN, rows[0], f"ISIN {isin} matches {rows[0].symbol} in {rows[0].source_name}"
            return AMBIGUOUS, None, f"ISIN {isin} maps to several symbols: {', '.join(r.symbol for r in rows)}"
        cname = canonical_name(company)
        rows = self._distinct(self.by_name.get(cname, [])) if cname else []
        if not rows:
            return UNRESOLVED, None, "no ISIN or exact issuer-name match in NSE masters"
        if len(rows) > 1:
            return AMBIGUOUS, None, f"issuer name matches several symbols: {', '.join(r.symbol for r in rows)}"
        m = rows[0]
        if not isin:
            return UNRESOLVED, None, f"name matches {m.symbol} but the row has no ISIN to confirm it"
        if m.isin and m.isin[:ISIN_ISSUER_PREFIX_LEN] == isin[:ISIN_ISSUER_PREFIX_LEN]:
            return RESOLVED_NAME_ISIN_PREFIX, m, f"issuer name matches {m.symbol}; ISIN changed {isin} -> {m.isin} (same issuer prefix)"
        return CONFLICTING, None, f"issuer name matches {m.symbol} but ISIN {isin} does not share issuer prefix with master ISIN {m.isin}"

    def equity_for_non_equity_isin(self, isin: str, company: str) -> tuple[MasterRow | None, str]:
        """A row that carries a non-equity ISIN of the right issuer (Indian
        ISINs: 7-character issuer code, then a 2-digit instrument type where
        01 is equity and 07 is debt). When the issuer name matches exactly one
        master row whose equity ISIN shares the issuer code, that equity row
        is the listing this IPO record describes. Returns (row, reason) or
        (None, reason)."""
        isin = (isin or "").upper().strip()
        if len(isin) != 12 or isin[7:9] == "01":
            return None, "ISIN is already an equity ISIN or malformed"
        cname = canonical_name(company)
        rows = self._distinct(self.by_name.get(cname, [])) if cname else []
        if len(rows) != 1:
            return None, "issuer name does not match exactly one master row"
        m = rows[0]
        if not m.isin or m.isin[:7] != isin[:7]:
            return None, f"master ISIN {m.isin} is a different issuer than {isin}"
        return m, f"non-equity ISIN {isin} (instrument type {isin[7:9]}) replaced by equity ISIN {m.isin} of the same issuer, symbol {m.symbol}"

    def listing_for_live_symbol(self, symbol: str, company: str, close_date: str,
                                max_days_after_close: int = LISTING_WINDOW_DAYS) -> tuple[MasterRow | None, str, str]:
        """A live issue (symbol from the NSE live feed, no ISIN yet) that now
        appears in the masters has listed. Accepted only when exactly one
        master row carries the symbol, the issuer name matches exactly
        (canonical form), and the master's DATE OF LISTING falls within
        max_days_after_close days after the subscription close. Returns
        (row, listing date YYYY-MM-DD, reason) or (None, "", reason)."""
        rows = self._distinct(self.by_symbol.get((symbol or "").upper().strip(), []))
        if len(rows) != 1:
            return None, "", "symbol not in masters" if not rows else "symbol maps to several master rows"
        m = rows[0]
        if canonical_name(m.name) != canonical_name(company):
            return None, "", f"master name '{m.name}' does not match issuer"
        listed, close = normalize_date(m.date_of_listing), normalize_date(close_date)
        if not listed or not close:
            return None, "", "listing or close date missing"
        gap = (date.fromisoformat(listed) - date.fromisoformat(close)).days
        if not 0 <= gap <= max_days_after_close:
            return None, "", f"master listing date {listed} is {gap} days from close {close}"
        return m, listed, f"{m.symbol} listed {listed} per {m.source_name} ({gap} days after close)"

    def check_symbol(self, isin: str, symbol: str) -> tuple[bool | None, str]:
        """For rows that already carry a symbol: True agree / False disagree /
        None when the ISIN is not in the masters."""
        isin = (isin or "").upper().strip()
        rows = self.by_isin.get(isin) if isin else None
        if not rows:
            return None, ""
        syms = {r.symbol for r in rows}
        if symbol.upper() in syms:
            return True, ""
        return False, f"stored {symbol}, master {'/'.join(sorted(syms))}"
