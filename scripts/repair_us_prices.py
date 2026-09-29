#!/usr/bin/env python
"""Phase 5: resolve missing final offer prices on US Listed rows from the
424B4 cover page, and classify the documents that cannot carry one.

For every US row with status Listed and no final price:
  direct listing      -> stays Listed, flagged: a reference price is not an
                         offer price, so final_price stays null by design
  resale-only / debt / de-SPAC proxy / follow-on
                      -> status "Not IPO" with the reason in data_flags
  ipo                 -> price/symbol re-read (sec.parse_priced_ipo, now with
                         the v4 cover readers); provenance "SEC 424B4 (price repair)"
  still no price      -> flagged "final_price_unparsed" (idempotent)

Idempotent and bounded; only SEC-sourced US rows are touched.

    python scripts/repair_us_prices.py --limit 60
"""
from __future__ import annotations
import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.models import IPO  # noqa: E402
from app.services import sec  # noqa: E402
from app.services.pipeline import add_provenance  # noqa: E402

DIRECT_LISTING_FLAG = "offering_type: direct listing (no offer price by definition)"
UNPARSED_FLAG = "final_price_unparsed"
NOT_IPO_PREFIX = sec.NON_IPO_FLAG_PREFIX
NOT_IPO_REASON = {
    sec.RESALE_ONLY: f"{NOT_IPO_PREFIX}: resale-only registration by selling shareholders (price repair pass)",
    sec.DEBT_OFFERING: f"{NOT_IPO_PREFIX}: debt securities offering (price repair pass)",
    sec.MERGER_PROXY: f"{NOT_IPO_PREFIX}: business-combination proxy statement/prospectus (price repair pass)",
    "follow_on": f"{NOT_IPO_PREFIX}: follow-on prospectus (price repair pass)",
    sec.SPIN_OFF: f"{NOT_IPO_PREFIX}: spin-off distribution to existing shareholders, no offering (price repair pass)",
}
PROVENANCE = "SEC 424B4 (price repair)"
HEAD_BYTES = 1_500_000


def _add_flag(ipo: IPO, flag: str) -> None:
    flags = [str(f) for f in (ipo.data_flags or [])]
    if flag not in flags:
        ipo.data_flags = [*flags, flag]


def candidates(db: Session, limit: int) -> list[IPO]:
    rows = db.scalars(select(IPO).where(IPO.country == "United States", IPO.status == "Listed", IPO.filing_url.like("https://www.sec.gov/%")).order_by(IPO.listing_date)).all()
    return [r for r in rows if not r.final_price or r.final_price <= 0][:limit]


def repair_row(db: Session, ipo: IPO, text: str, truncated: bool) -> str:
    """Apply the repair for one row given its filing head. Returns the outcome label."""
    flat = sec.flatten_filing_text(text)
    otype = sec.classify_offering_type(flat)
    if otype == sec.DIRECT_LISTING:
        _add_flag(ipo, DIRECT_LISTING_FLAG)
        sym = sec.parse_symbol(flat)
        if sym and not ipo.symbol:
            ipo.symbol = sym
            add_provenance(db, ipo, "symbol", ipo.symbol, PROVENANCE, ipo.filing_url, 1)
        return "direct_listing"
    if otype in NOT_IPO_REASON:
        ipo.status = "Not IPO"
        _add_flag(ipo, NOT_IPO_REASON[otype])
        ipo.updated_at = datetime.now(timezone.utc)
        return f"not_ipo:{otype}"
    if otype == "ipo":
        parsed = sec.parse_priced_ipo(flat) or {}
        price = parsed.get("final_price")
        if price:
            ipo.final_price = price
            add_provenance(db, ipo, "final_price", price, PROVENANCE, ipo.filing_url, 1)
            for f in ("price_low", "price_high"):
                if parsed.get(f) is not None and getattr(ipo, f) is None:
                    setattr(ipo, f, parsed[f])
                    add_provenance(db, ipo, f, parsed[f], PROVENANCE, ipo.filing_url, 1)
            if parsed.get("symbol") and not ipo.symbol:
                ipo.symbol = parsed["symbol"]
                add_provenance(db, ipo, "symbol", ipo.symbol, PROVENANCE, ipo.filing_url, 1)
            ipo.data_flags = [f for f in (ipo.data_flags or []) if str(f) != UNPARSED_FLAG]
            ipo.updated_at = datetime.now(timezone.utc)
            return "priced"
    if truncated and otype == "unknown":
        _add_flag(ipo, UNPARSED_FLAG + " (cover page beyond the fetched head)")
        return "inconclusive_truncated"
    _add_flag(ipo, UNPARSED_FLAG)
    return f"unparsed:{otype}"


def run(db: Session, *, limit: int, log=print) -> dict:
    s = get_settings()
    outcomes: dict[str, int] = {}
    for ipo in candidates(db, limit):
        try:
            text, truncated = sec.filing_head(ipo.filing_url, s.sec_user_agent, max_bytes=HEAD_BYTES)
            label = repair_row(db, ipo, text, truncated)
            db.commit()
        except Exception as e:
            db.rollback()
            label = f"error:{type(e).__name__}"
        outcomes[label] = outcomes.get(label, 0) + 1
        log(f"  {ipo.id} {ipo.company}: {label}" + (f" price={ipo.final_price}" if label == "priced" else ""))
    log(f"price repair outcomes: {outcomes}")
    return outcomes


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=100)
    args = ap.parse_args()
    init_db()
    db = SessionLocal()
    try:
        run(db, limit=args.limit)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
