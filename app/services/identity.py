"""Entity identity, lifecycle vocabulary and user-visible label hygiene.

Three small, deterministic concerns that several ingesters and the static
Pages build all need to agree on:

1. canonical_name(): a punctuation/whitespace/Unicode/corporate-suffix
   normalised form of an issuer name, used ONLY to recognise that two source
   records describe the same issuer (NSE's live API keys issues by symbol,
   its monthly Primary Market Report has no symbol and spells the name with
   different punctuation). It is never used as the stored external_key, so
   no existing /ipo/<slug>/ URL ever changes - see pipeline.resolve_existing.

2. classify_issue_type() / STATUS_RANK: which NSE report rows are actually
   IPOs (the report also lists preferential allotments, QIPs, rights issues
   and warrant conversions, none of which are IPOs) and which lifecycle
   transitions are allowed (a Listed or Withdrawn row never regresses to
   Open/Upcoming because a stale feed still mentions it).

3. sanitize_label(): the product ships zero user-visible em dashes. Score
   snapshots are immutable, so legacy rows written with the old
   "INVEST — STRONG" wording are normalised at read/publish time instead of
   being rewritten in place.
"""
from __future__ import annotations
import re
import unicodedata

# ---------------------------------------------------------------- names ----
_CORPORATE_SUFFIXES = {
    "limited", "ltd", "private", "pvt", "inc", "incorporated", "corporation", "corp",
    "co", "company", "llc", "plc", "llp", "lp", "sa", "nv", "ag", "se", "the",
}
_PAREN = re.compile(r"\([^)]*\)")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def canonical_name(name: str | None) -> str:
    """Lower-cased, NFKC-normalised, punctuation-free issuer name with trailing
    corporate suffix tokens removed. "Adani Green Energy Ltd." and
    "ADANI GREEN ENERGY LIMITED" both canonicalise to "adani green energy".
    Deterministic and total: never raises, returns "" for empty input."""
    if not name:
        return ""
    s = unicodedata.normalize("NFKC", str(name)).lower()
    s = _PAREN.sub(" ", s)
    s = s.replace("&", " and ")
    tokens = [t for t in _NON_ALNUM.split(s) if t]
    while tokens and tokens[-1] in _CORPORATE_SUFFIXES:
        tokens.pop()
    if not tokens:  # the name was nothing but suffix tokens - fall back to the raw tokens
        tokens = [t for t in _NON_ALNUM.split(s) if t]
    return " ".join(tokens)


# ----------------------------------------------------------- issue types ----
IPO_TYPES = ("IPO", "SME IPO", "InvIT IPO", "REIT IPO")


def classify_issue_type(raw_type: str | None) -> str | None:
    """Maps the free-text 'issue_type' column of NSE's Primary Market Monthly
    Report onto a small closed vocabulary. Returns None for anything that is
    not an initial public offering (preferential, QIP, rights, warrants...),
    which callers must treat as "not an IPO", never as a Mainboard IPO."""
    if raw_type is None:
        return None
    t = " ".join(str(raw_type).lower().replace("-", " ").split())
    if not t or t == "none":
        return None
    if "sme" in t and "ipo" in t or t in ("nse sme", "sme"):
        return "SME IPO"
    if "invit" in t and "ipo" in t:
        return "InvIT IPO"
    if "reit" in t and "ipo" in t:
        return "REIT IPO"
    if t == "ipo" or t.endswith(" ipo") or t.startswith("ipo "):
        return "IPO"
    return None


def board_for_issue_type(issue_type: str | None, exchange: str = "") -> str:
    if issue_type == "SME IPO" or "sme" in (exchange or "").lower() or "emerge" in (exchange or "").lower():
        return "SME"
    return "Mainboard"


# --------------------------------------------------------------- statuses ----
# Terminal / late statuses outrank early ones: an ingester that still sees an
# issue in a stale "upcoming" feed can never pull a Listed row back to Upcoming.
STATUS_RANK = {
    "Filed": 10, "Upcoming": 20, "Open": 30, "Closed": 40,
    "Priced": 50, "Listed": 60, "Withdrawn": 70, "Not IPO": 80,
}
ACTIVE_STATUSES = ("Filed", "Upcoming", "Open", "Closed", "Priced")
TERMINAL_STATUSES = ("Listed", "Withdrawn", "Not IPO")


def status_can_transition(current: str | None, new: str | None) -> bool:
    """False when `new` would regress the lifecycle (e.g. Listed -> Open)."""
    if not new:
        return False
    if not current or current == new:
        return True
    return STATUS_RANK.get(new, 0) >= STATUS_RANK.get(current, 0)


# ------------------------------------------------------------------ labels ----
# Exact legacy wordings (still present on immutable historical snapshots) and
# their em-dash-free replacements. Anything not listed here goes through the
# generic rule below.
LEGACY_LABELS = {
    "INSUFFICIENT RELIABLE DATA — NO RECOMMENDATION": "INSUFFICIENT RELIABLE DATA: NO RECOMMENDATION",
    "INVEST — STRONG": "INVEST: STRONG",
    "BOTH — LISTING + LONG TERM": "BOTH: LISTING + LONG TERM",
    "LONG TERM — WAIT FOR PRICE DISCOVERY": "LONG TERM: WAIT FOR PRICE DISCOVERY",
}
EM_DASH = "—"


def sanitize_label(text):
    """Returns `text` with every em dash replaced by a natural construction.
    Non-string input is returned unchanged so it is safe to map over any
    JSON-shaped value."""
    if not isinstance(text, str) or EM_DASH not in text:
        return text
    if text in LEGACY_LABELS:
        return LEGACY_LABELS[text]
    out = text
    for old, new in LEGACY_LABELS.items():
        out = out.replace(old, new)
    out = out.replace(" " + EM_DASH + " ", ", ").replace(EM_DASH, "-")
    return out


def sanitize_tree(obj):
    """Deep copy of a JSON-shaped structure with sanitize_label applied to
    every string (keys included). Used by the static build as the last line
    of defence before anything is written to the public dist."""
    if isinstance(obj, str):
        return sanitize_label(obj)
    if isinstance(obj, list):
        return [sanitize_tree(x) for x in obj]
    if isinstance(obj, tuple):
        return tuple(sanitize_tree(x) for x in obj)
    if isinstance(obj, dict):
        return {sanitize_label(k): sanitize_tree(v) for k, v in obj.items()}
    return obj
