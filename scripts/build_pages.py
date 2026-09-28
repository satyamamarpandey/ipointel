#!/usr/bin/env python
"""Static-site generator for the GitHub Pages production build of IPOIntel
(ipointel.brandsap.com).

This freezes REAL backend output into static JSON by importing app.main and
calling its existing route functions directly, in-process, against whichever
DATABASE_URL is configured (no HTTP server, no localhost dependency at
runtime, no reimplemented scoring/DCF/red-flag/similarity logic - see
docs/GITHUB_PAGES.md for the full architecture and rationale).

Usage:
    python scripts/build_pages.py [--out dist] [--base-url https://ipointel.brandsap.com]
                                   [--waitlist-endpoint <google-apps-script-url>]

Output layout (see docs/GITHUB_PAGES.md):
    dist/index.html                    landing (public)
    dist/dashboard/index.html          full public dashboard (same UI as server mode)
    dist/login/index.html              present for structural parity; non-functional
                                        without a real server (magic-link auth needs one)
    dist/ipo/<slug>/index.html         one static detail page per published IPO
    dist/data/manifest.json            build metadata, counts, exclusions (with reasons),
                                        cutoffs, pipeline/source status
    dist/data/highlights.json          landing hero/ticker (mirrors /api/public/highlights)
    dist/data/upcoming/{india,us}.json every currently active IPO that passed the
                                        publish rules (each drop is listed in the manifest)
    dist/data/withdrawn.json           withdrawn / cancelled offerings, never mixed into
                                        the active counts
    dist/data/history/{india,us}-5y.json  listed IPOs within the rolling 5-year window
    dist/data/ipo/<id>.json            per-IPO {detail, valuation, similar, changes}
    dist/data/{track-record,source-health,backtest,model-performance}.json
    dist/sitemap.xml, dist/robots.txt, dist/CNAME, dist/404.html
"""
from __future__ import annotations
import argparse, json, re, shutil, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app.main as M  # noqa: E402  (importing the FastAPI module gives us its plain route functions)
from app.config import get_settings  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.models import IPO  # noqa: E402
from app.services.market import parse_date  # noqa: E402
from app.services import similarity as similarity_svc  # noqa: E402
from app.services.identity import canonical_name, sanitize_tree, sanitize_label, ACTIVE_STATUSES  # noqa: E402
from app.services.pipeline import INDIA_LISTING_GRACE_DAYS  # noqa: E402
from sqlalchemy import select  # noqa: E402

STATIC = ROOT / "app" / "static"
UPCOMING_STATUSES = tuple(ACTIVE_STATUSES)  # Filed, Upcoming, Open, Closed, Priced
# A US registration with no amendment, no pricing and no withdrawal for this
# long is not an upcoming IPO in any practical sense. It stays in the database
# (it may still price one day) but is not published as upcoming.
US_STALE_FILING_DAYS = 365
# Sources whose health decides the public pipeline status. The licensed
# enrichment feed is optional and never counts against it.
REQUIRED_SOURCES = ("SEC EDGAR", "SEC Priced IPOs", "NSE", "NSE Primary Market Reports")
PUBLIC_SOURCES = set(REQUIRED_SOURCES) | {"Licensed enrichment feed"}
STATUS_SEVERITY = {"LIVE": 0, "OPTIONAL_UNCONFIGURED": 0, "DELAYED": 1, "PARTIAL": 2, "STALE": 3, "FAILED": 4}
# Fields the per-IPO detail artifact already carries; dropping them from the
# multi-thousand-row history LIST keeps that file small (the History tab only
# renders numbers), while every detail page still gets the full record.
HISTORY_LIST_SCORE_FIELDS = ("overall", "listing", "long_term", "confidence", "recommendation", "valuation", "model_version", "created_at")


def slugify(external_key: str) -> str:
    s = external_key.lower().replace(":", "-")
    s = re.sub(r"[^a-z0-9\-]+", "-", s).strip("-")
    return s or "ipo"


def git_sha() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    except Exception:
        return "unknown"


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Last line of defence for the zero-em-dash rule: immutable legacy score
    # snapshots still carry the old wording, and source data can carry anything.
    path.write_text(json.dumps(sanitize_tree(obj), indent=None, separators=(",", ":"), default=str), encoding="utf-8")


def history_cutoff(now: datetime, window_years: int = 5) -> datetime:
    return now.replace(year=now.year - window_years)


def in_history_window(date_str: str, now: datetime, cutoff: datetime) -> bool | None:
    """Pure boundary logic, unit-tested directly in tests/test_pages_build.py
    (exact cutoff included, one day before excluded). Returns None when the
    date string can't be parsed at all - the caller must treat that as
    "excluded, and audited as unparseable", never as a silent pass/fail."""
    d = parse_date(date_str)
    if d is None:
        return None
    return cutoff <= d <= now


def _days_since(date_str: str, now: datetime) -> int | None:
    d = parse_date(date_str)
    return None if d is None else (now - d).days


def upcoming_exclusion_reason(ipo: IPO, now: datetime) -> str | None:
    """Why an active-status row is NOT published as an upcoming IPO. None means
    publish. Every rule here is deterministic and spelled out in the manifest."""
    flags = [str(f) for f in (ipo.data_flags or [])]
    if any(f.startswith("non_ipo_registration") for f in flags):
        return "not an IPO registration: " + "; ".join(f.split(":", 1)[1].strip() for f in flags if f.startswith("non_ipo_registration"))
    if ipo.country == "United States" and ipo.status == "Filed":
        age = _days_since(ipo.filing_date, now)
        if age is not None and age > US_STALE_FILING_DAYS:
            return f"stale registration: no amendment, pricing or withdrawal in {age} days (limit {US_STALE_FILING_DAYS})"
    if ipo.country == "India" and ipo.status == "Closed":
        age = _days_since(ipo.close_date, now)
        if age is not None and age > INDIA_LISTING_GRACE_DAYS:
            return f"issue closed {age} days ago with no confirmed listing (limit {INDIA_LISTING_GRACE_DAYS})"
    return None


def _completeness(ipo: IPO) -> tuple:
    """Sort key for choosing which duplicate record to keep: more data wins,
    then the row that carries a symbol, then the lowest (oldest) id."""
    filled = sum(1 for f in ("listing_date", "final_price", "price_high", "isin", "issue_size_m", "fresh_issue_pct", "sector") if getattr(ipo, f) not in (None, "", "Unknown"))
    return (-filled, 0 if ipo.symbol else 1, ipo.id)


def dedupe_by_issuer(rows: list[IPO]) -> tuple[list[IPO], list[dict]]:
    """Publish-time issuer de-duplication (see GitHub issue #1). Rows are
    grouped by country + canonical issuer name; within a group one record is
    kept and the rest are excluded with an explicit reason naming the kept
    id. Nothing is merged or deleted in the database - a later, evidence-based
    merge can still happen there; this only keeps the public site honest now."""
    groups: dict[tuple[str, str], list[IPO]] = {}
    for ipo in rows:
        groups.setdefault((ipo.country, canonical_name(ipo.company)), []).append(ipo)
    kept, dropped = [], []
    for (_country, cname), members in groups.items():
        if len(members) == 1 or not cname:
            kept.extend(members)
            continue
        members = sorted(members, key=_completeness)
        keep = members[0]
        kept.append(keep)
        for d in members[1:]:
            dropped.append({"id": d.id, "company": d.company, "country": d.country, "status": d.status,
                            "reason": f"duplicate issuer record (kept id {keep.id}, external_key {keep.external_key})"})
    return kept, dropped


def classify(db, now: datetime, window_years: int = 5):
    """Splits every IPO row into upcoming / withdrawn / published-history /
    excluded buckets, using the SAME date parser the rest of the app trusts
    (app.services.market.parse_date), never a raw string comparison - the
    listing_date column mixes "30-Nov-2022" (India) and "20251022" (US)
    styles, which do not sort or compare correctly as strings.

    The 5-year cutoff applies ONLY to Listed rows. An upcoming IPO is never
    excluded for having an old filing_date - see in_history_window's docstring
    and tests/test_pages_build.py::test_upcoming_never_excluded_by_filing_date.

    Every row that is discovered but not published lands in `excluded` with a
    reason; nothing is silently dropped."""
    cutoff = history_cutoff(now, window_years)
    rows = db.scalars(select(IPO)).all()
    upcoming, withdrawn, history_in, history_out, unparseable, excluded = [], [], [], [], [], []
    for ipo in rows:
        if ipo.status in UPCOMING_STATUSES:
            reason = upcoming_exclusion_reason(ipo, now)
            if reason:
                excluded.append({"id": ipo.id, "company": ipo.company, "country": ipo.country, "status": ipo.status, "reason": reason})
            else:
                upcoming.append(ipo)
        elif ipo.status == "Withdrawn":
            withdrawn.append(ipo)
        elif ipo.status == "Listed":
            verdict = in_history_window(ipo.listing_date, now, cutoff)
            if verdict is None:
                unparseable.append(ipo)
            elif verdict:
                history_in.append(ipo)
            else:
                history_out.append(ipo)
        elif ipo.status == "Not IPO":
            excluded.append({"id": ipo.id, "company": ipo.company, "country": ipo.country, "status": ipo.status,
                             "reason": "not an IPO: " + (str((ipo.raw or {}).get("issue_type") or "; ".join(str(f) for f in (ipo.data_flags or []) if "non_ipo" in str(f)) or "source classified this issue as a non-IPO offering"))})
        else:
            excluded.append({"id": ipo.id, "company": ipo.company, "country": ipo.country, "status": ipo.status, "reason": f"unrecognised status {ipo.status!r}"})

    # A Listed record for the same issuer supersedes any still-active record
    # (the live feed row that never transitioned) - an already-listed IPO is
    # never shown as upcoming.
    listed_names = {(x.country, canonical_name(x.company)): x for x in history_in + history_out}
    still_upcoming = []
    for ipo in upcoming:
        match = listed_names.get((ipo.country, canonical_name(ipo.company)))
        if match is not None and match.id != ipo.id:
            excluded.append({"id": ipo.id, "company": ipo.company, "country": ipo.country, "status": ipo.status,
                             "reason": f"already listed: superseded by listed record id {match.id} ({match.listing_date or 'listing date pending'})"})
        else:
            still_upcoming.append(ipo)
    upcoming, dup_up = dedupe_by_issuer(still_upcoming)
    history_in, dup_hist = dedupe_by_issuer(history_in)
    duplicates = dup_up + dup_hist
    excluded.extend(duplicates)
    return {"cutoff": cutoff, "upcoming": upcoming, "withdrawn": withdrawn, "history_in": history_in,
            "history_out": history_out, "unparseable": unparseable, "excluded": excluded, "duplicates": duplicates}


def source_status(row: dict, now: datetime, *, optional_unconfigured: bool = False) -> str:
    # A non-empty `error` field does NOT by itself mean total failure -
    # ingest_sec_priced (and others) log per-day/per-item warnings there even
    # on an otherwise-successful "partial" run (e.g. a weekend or not-yet-
    # published day in a lookback window is an expected miss, not a broken
    # collector). The pipeline's own IngestionRun.status is authoritative;
    # only trust `error` as a hard failure when status says so.
    status = row.get("status")
    if status in ("never run", None) or not row.get("last_run"):
        # An optional provider with no credential configured was never even
        # attempted - that's a deliberate, honest non-issue, not the same as
        # a required source that failed to run. Never let it read as FAILED.
        return "OPTIONAL_UNCONFIGURED" if optional_unconfigured else "FAILED"
    if status == "error":
        return "FAILED"
    if status == "partial":
        return "PARTIAL"
    last_run = datetime.fromisoformat(row["last_run"])
    if last_run.tzinfo is None:  # SQLite doesn't truly persist tz-awareness even with DateTime(timezone=True)
        last_run = last_run.replace(tzinfo=timezone.utc)
    age_hours = (now - last_run).total_seconds() / 3600
    # The daily NSE report pass legitimately runs once a day; the rest every few hours.
    fresh_hours = 30 if row.get("source") == "NSE Primary Market Reports" else 4
    if age_hours <= fresh_hours:
        return "LIVE"
    if age_hours <= 48:
        return "DELAYED"
    return "STALE"


def pipeline_status(source_health: list[dict]) -> str:
    """Overall public status, derived only from the REQUIRED sources: the worst
    individual status wins, so one failed required feed shows as FAILED even
    while three others are LIVE. Never hardcoded."""
    worst = "LIVE"
    for r in source_health:
        if r["source"] not in REQUIRED_SOURCES:
            continue
        st = r.get("public_status", "FAILED")
        if STATUS_SEVERITY.get(st, 4) > STATUS_SEVERITY.get(worst, 0):
            worst = st
    return worst


def _slim_history_row(row: dict) -> dict:
    out = dict(row)
    sc = row.get("score")
    if sc:
        out["score"] = {k: sc.get(k) for k in HISTORY_LIST_SCORE_FIELDS}
    return out


def build(out_dir: Path, base_url: str, waitlist_endpoint: str) -> dict:
    init_db()  # idempotent - creates any missing tables, never touches existing data.
    # Only ever relied on a separate caller-run step for this before, which
    # meant a fresh checkout with no pre-existing local dev DB (no CI runner
    # has one) crashed with "no such table: ipos" - this makes the build
    # self-sufficient regardless of what ran before it.
    db = SessionLocal()
    now = datetime.now(timezone.utc)
    audit = {}

    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    # ---------- classify + fetch real per-IPO detail (real scoring/DCF/etc.) ----------
    c = classify(db, now)
    cutoff, upcoming, withdrawn, history_in = c["cutoff"], c["upcoming"], c["withdrawn"], c["history_in"]

    # Last-known-good protection: a database with no publishable IPO at all
    # means the restore step failed or every source has been wiped. Deploying
    # that would replace a working public site with an empty one. Fail loudly.
    if not upcoming and not history_in:
        raise SystemExit("REFUSING TO BUILD: no publishable upcoming or historical IPO in the database - "
                         "check the data-state restore step; not deploying an empty site.")

    # Listed candidates per country, fetched once (not once per target IPO) -
    # see similarity.find_similar's `candidates` param docstring. Identical
    # matching result, avoids an O(n^2) rescan across hundreds of IPOs.
    listed_by_country = {
        "India": db.scalars(select(IPO).where(IPO.country == "India", IPO.status == "Listed")).all(),
        "United States": db.scalars(select(IPO).where(IPO.country == "United States", IPO.status == "Listed")).all(),
    }

    def full_detail(ipo: IPO) -> dict:
        return {
            "detail": M.ipo_detail(ipo_id=ipo.id, db=db, _lead=None),
            "valuation": M.ipo_valuation_detail(ipo_id=ipo.id, db=db, _lead=None),
            "similar": similarity_svc.find_similar(db, ipo, candidates=listed_by_country.get(ipo.country, [])),
            "changes": M.ipo_changes(ipo_id=ipo.id, db=db, _lead=None),
        }

    published = upcoming + withdrawn + history_in

    # external_key isn't punctuation-normalized at ingestion (e.g. India rows
    # for the same company differ by a trailing period/comma/asterisk), so
    # slugify() can collapse two different IPOs to the same base slug. Fixing
    # that at the source means merging DB rows - out of scope here and risky
    # without dedicated tooling. Instead, make the URL layer collision-safe:
    # every IPO still gets its own page, deterministically (lowest id keeps
    # the bare slug; a colliding IPO gets "<slug>-<id>"), so no rebuild ever
    # silently drops or overwrites a company's detail page.
    base_slug_ids: dict[str, list[int]] = {}
    for ipo in published:
        base_slug_ids.setdefault(slugify(ipo.external_key), []).append(ipo.id)

    id_to_slug: dict[int, str] = {}
    slug_collisions = 0
    for base, ids in base_slug_ids.items():
        ids.sort()
        for pos, ipo_id in enumerate(ids):
            id_to_slug[ipo_id] = base if pos == 0 else f"{base}-{ipo_id}"
            if pos:
                slug_collisions += 1

    for i, ipo in enumerate(published):
        write_json(out_dir / "data" / "ipo" / f"{ipo.id}.json", full_detail(ipo))
        if (i + 1) % 200 == 0:
            print(f"  ...{i + 1}/{len(published)} IPO detail artifacts built", file=sys.stderr)

    def bucket(rows, country):
        return [M.ipo_json(db, x) for x in rows if x.country == country]

    def event_date(row):
        for k in ("open_date", "close_date", "listing_date", "filing_date"):
            d = parse_date(row.get(k))
            if d:
                return d
        return now

    upcoming_india, upcoming_us = bucket(upcoming, "India"), bucket(upcoming, "United States")
    # Currently open issues first, then by next event date, so an imminent
    # issue is never buried under old filings.
    status_order = {"Open": 0, "Closed": 1, "Upcoming": 2, "Priced": 3, "Filed": 4}
    for lst in (upcoming_india, upcoming_us):
        lst.sort(key=lambda x: (status_order.get(x["status"], 9), -event_date(x).timestamp()))
    history_india, history_us = bucket(history_in, "India"), bucket(history_in, "United States")
    history_india.sort(key=lambda x: parse_date(x["listing_date"]) or now, reverse=True)
    history_us.sort(key=lambda x: parse_date(x["listing_date"]) or now, reverse=True)
    withdrawn_rows = [M.ipo_json(db, x) for x in withdrawn]

    write_json(out_dir / "data" / "upcoming" / "india.json", upcoming_india)
    write_json(out_dir / "data" / "upcoming" / "us.json", upcoming_us)
    write_json(out_dir / "data" / "withdrawn.json", withdrawn_rows)
    write_json(out_dir / "data" / "history" / "india-5y.json", [_slim_history_row(r) for r in history_india])
    write_json(out_dir / "data" / "history" / "us-5y.json", [_slim_history_row(r) for r in history_us])

    write_json(out_dir / "data" / "highlights.json", M.public_highlights(db=db))
    write_json(out_dir / "data" / "track-record.json", M.track_record(limit=500, db=db, _lead=None))
    write_json(out_dir / "data" / "backtest.json", M.backtest(db=db, _lead=None))
    write_json(out_dir / "data" / "model-performance.json", M.model_performance(db=db, _lead=None))

    raw_source_rows = M._source_health_rows(db)
    enrichment_configured = bool(get_settings().secondary_enrichment_url)
    source_health = []
    for r in raw_source_rows:
        if r["source"] not in PUBLIC_SOURCES:
            continue  # never publish backend-operational rows (email/worker) on the public site
        optional = r["source"] == "Licensed enrichment feed" and not enrichment_configured
        # Only exception class names are published, never raw exception text
        # (which can carry hostnames, paths or upstream response fragments).
        error = re.sub(r"[A-Za-z]:\\[^\s|]+|/home/[^\s|]+", "<path>", str(r.get("error") or ""))[:400]
        source_health.append({**r, "error": error, "required": r["source"] in REQUIRED_SOURCES,
                              "public_status": source_status(r, now, optional_unconfigured=optional)})
    write_json(out_dir / "data" / "source-health.json", source_health)
    overall_status = pipeline_status(source_health)

    summary = M.summary(db=db, _lead=None)
    # M.summary() reports true lifetime DB totals (e.g. every IPO ever
    # marked Listed) - on Pages, only the rolling-5-year window and the
    # current upcoming set are actually published/clickable, so the
    # dashboard's headline stat cards must match what a visitor can really
    # reach here, not the server-mode "all of history" figure (which would
    # read as a broken/misleading number once the Listed filter only
    # returns a fraction of it).
    summary["total"] = len(published)
    summary["active"] = len(upcoming)
    summary["listed"] = len(history_in)
    summary["withdrawn"] = len(withdrawn)
    summary["pipeline_status"] = overall_status
    # Same principle for the high-confidence card: count only published rows
    # whose CURRENT score clears the gate, not every historical snapshot row.
    summary["high_confidence_scores"] = sum(1 for r in upcoming_india + upcoming_us + history_india + history_us
                                            if r.get("score") and r["score"]["confidence"] >= summary["min_confidence"])

    def by_country(rows, country):
        return len([x for x in rows if (x["country"] if isinstance(x, dict) else x.country) == country])

    excluded = c["excluded"]
    discovered_active = upcoming + [x for x in db.scalars(select(IPO).where(IPO.status.in_(UPCOMING_STATUSES))).all() if x.id in {e["id"] for e in excluded}]
    audit["upcoming"] = {
        "india_discovered": by_country(discovered_active, "India"), "us_discovered": by_country(discovered_active, "United States"),
        "india_published": len(upcoming_india), "us_published": len(upcoming_us),
        "india_excluded": len([e for e in excluded if e["country"] == "India" and e["status"] in UPCOMING_STATUSES]),
        "us_excluded": len([e for e in excluded if e["country"] == "United States" and e["status"] in UPCOMING_STATUSES]),
        "excluded": [e for e in excluded if e["status"] in UPCOMING_STATUSES],
    }
    audit["withdrawn"] = {"india": by_country(withdrawn, "India"), "us": by_country(withdrawn, "United States")}
    audit["not_ipo"] = {"india": len([e for e in excluded if e["country"] == "India" and e["status"] == "Not IPO"]),
                        "us": len([e for e in excluded if e["country"] == "United States" and e["status"] == "Not IPO"])}
    audit["duplicates"] = {"issuer_records_excluded": len(c["duplicates"]), "slug_collisions_resolved": slug_collisions,
                           "records": c["duplicates"]}
    audit["history"] = {
        "india_published": len(history_india), "us_published": len(history_us),
        "india_excluded_out_of_window": by_country(c["history_out"], "India"),
        "us_excluded_out_of_window": by_country(c["history_out"], "United States"),
        "india_excluded_unparseable_date": by_country(c["unparseable"], "India"),
        "us_excluded_unparseable_date": by_country(c["unparseable"], "United States"),
        "india_excluded_duplicate": len([d for d in c["duplicates"] if d["country"] == "India" and d["status"] == "Listed"]),
        "us_excluded_duplicate": len([d for d in c["duplicates"] if d["country"] == "United States" and d["status"] == "Listed"]),
    }
    audit["high_confidence_published"] = sum(1 for r in upcoming_india + upcoming_us + history_india + history_us
                                             if r.get("score") and r["score"]["confidence"] >= summary["min_confidence"])

    # ---------- manifest ----------
    model_version = next((r["score"]["model_version"] for r in upcoming_india + upcoming_us + history_india + history_us if r.get("score")), "unknown")
    manifest = {
        "generated_at": now.isoformat(),
        "history_window_start": cutoff.date().isoformat(),
        "history_window_end": now.date().isoformat(),
        "history_window_years": 5,
        "model_version": model_version,
        "schema_version": "2",
        "build_commit": git_sha(),
        "base_url": base_url,
        "pipeline_status": overall_status,
        "required_sources": list(REQUIRED_SOURCES),
        "summary": summary,
        "counts": audit,
        "published_ipo_pages": len(published),
    }
    write_json(out_dir / "data" / "manifest.json", manifest)

    # ---------- static assets ----------
    static_out = out_dir / "static"
    shutil.copytree(STATIC / "brand", static_out / "brand")
    for fn in ["styles.css", "nav.js", "landing.js", "app.js", "login.js", "pages-adapter.js", "pages-ipo-detail.js", "site.webmanifest", "favicon.ico"]:
        src = STATIC / fn
        if src.exists():
            shutil.copy2(src, static_out / fn)
    config_js = f"window.PAGES_MODE = true;\nwindow.PUBLIC_BASE_URL = {json.dumps(base_url)};\nwindow.PUBLIC_WAITLIST_ENDPOINT = {json.dumps(waitlist_endpoint)};\n"
    (static_out / "pages-config.js").write_text(config_js, encoding="utf-8")
    for fn in ["favicon.ico"]:
        src = STATIC / fn
        if src.exists():
            shutil.copy2(src, out_dir / fn)

    # ---------- pages ----------
    shutil.copy2(STATIC / "index.html", out_dir / "index.html")
    (out_dir / "dashboard").mkdir(exist_ok=True)
    shutil.copy2(STATIC / "app.html", out_dir / "dashboard" / "index.html")
    (out_dir / "login").mkdir(exist_ok=True)
    shutil.copy2(STATIC / "login.html", out_dir / "login" / "index.html")
    for fn in ["privacy.html", "terms.html"]:
        src = STATIC / fn
        if src.exists():
            shutil.copy2(src, out_dir / fn)
    # admin.html is deliberately never copied - admin stays server-only.

    template = (STATIC / "pages-detail-template.html").read_text(encoding="utf-8")
    snapshot_label = now.strftime("%b %d, %Y %H:%M UTC")
    # Only routes this build actually emits. /privacy and /terms are the
    # extensionless forms: GitHub Pages resolves them to the privacy.html /
    # terms.html written below, and they are also the real FastAPI routes in
    # server mode - so they match the <link rel="canonical"> in those files.
    urls = [
        base_url + "/",
        base_url + "/dashboard/",
        base_url + "/privacy",
        base_url + "/terms",
    ]

    def html_attr(s: str) -> str:
        return sanitize_label(s).replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")

    for ipo in published:
        slug = id_to_slug[ipo.id]
        canonical = f"{base_url}/ipo/{slug}/"
        page = (template
                .replace("__TITLE__", html_attr(f"{ipo.company} · IPOIntel"))
                .replace("__DESCRIPTION__", html_attr(f"Evidence-first score, valuation and risk analysis for {ipo.company} ({ipo.country}, {ipo.status})."))
                .replace("__CANONICAL__", canonical)
                .replace("__ID__", str(ipo.id))
                .replace("__SNAPSHOT__", snapshot_label))
        d = out_dir / "ipo" / slug
        d.mkdir(parents=True, exist_ok=True)
        (d / "index.html").write_text(page, encoding="utf-8")
        urls.append(canonical)

    (out_dir / "CNAME").write_text(base_url.replace("https://", "").replace("http://", "").rstrip("/") + "\n", encoding="utf-8")
    (out_dir / "robots.txt").write_text(f"User-agent: *\nAllow: /\nDisallow: /login/\nSitemap: {base_url}/sitemap.xml\n", encoding="utf-8")
    sitemap = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for u in urls:
        sitemap.append(f"<url><loc>{u}</loc></url>")
    sitemap.append("</urlset>")
    (out_dir / "sitemap.xml").write_text("\n".join(sitemap), encoding="utf-8")

    not_found_html = (STATIC / "404.html").read_text(encoding="utf-8")
    (out_dir / "404.html").write_text(not_found_html, encoding="utf-8")

    db.close()
    audit["manifest"] = manifest
    audit["published_ipo_pages"] = len(published)
    return audit


SECRET_PATTERNS = [
    r"CLERK_SECRET", r"sk_live_[A-Za-z0-9]{8,}", r"sk_test_[A-Za-z0-9]{8,}", r"GOOGLE_APPLICATION_CREDENTIALS", r"AWS_SECRET", r"RESEND_API_KEY", r"re_[A-Za-z0-9]{20,}",
    r"DATABASE_URL\s*=\s*postgresql", r"postgres(?:ql)?(?:\+psycopg)?://[^\s\"']+", r"sqlite:///", r"ADMIN_TOKEN\s*=\s*(?!change-me)", r"POSTGRES_PASSWORD",
    r"SMTP_PASSWORD", r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----", r"\"private_key\"",
    r"Traceback \(most recent call last\)", r"[A-Za-z]:\\\\?Users\\\\?", r"E:\\\\?IPO Analysis", r"/home/[a-z0-9_-]+/",
]
# Real people's email addresses must never appear in the public data feed
# (waitlist leads live in a separate table that is never exported, but a
# regex over the output proves it rather than assuming it).
EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def secret_scan(out_dir: Path) -> list[str]:
    hits = []
    pats = [re.compile(p) for p in SECRET_PATTERNS]
    for f in out_dir.rglob("*"):
        if not f.is_file() or f.suffix in (".png", ".ico", ".jpg", ".woff", ".woff2"):
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for p in pats:
            if p.search(text):
                hits.append(f"{p.pattern} in {f.relative_to(out_dir)}")
        if f.suffix == ".json":
            for m in EMAIL_PATTERN.findall(text):
                hits.append(f"email address {m!r} in {f.relative_to(out_dir)}")
    return hits


def em_dash_scan(out_dir: Path) -> list[str]:
    """Every user-visible text artifact must be free of U+2014."""
    hits = []
    for f in out_dir.rglob("*"):
        if not f.is_file() or f.suffix not in (".html", ".json", ".js", ".css", ".txt", ".xml", ".webmanifest"):
            continue
        text = f.read_text(encoding="utf-8", errors="ignore")
        if "\u2014" in text:
            hits.append(str(f.relative_to(out_dir)))
    return hits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="dist")
    ap.add_argument("--base-url", default="https://ipointel.brandsap.com")
    ap.add_argument("--waitlist-endpoint", default="")
    args = ap.parse_args()

    out_dir = ROOT / args.out
    audit = build(out_dir, args.base_url.rstrip("/"), args.waitlist_endpoint)
    hits = secret_scan(out_dir)
    if hits:
        print("SECRET SCAN FAILED:")
        for h in hits:
            print("  -", h)
        sys.exit(1)
    dashes = em_dash_scan(out_dir)
    if dashes:
        print("EM DASH SCAN FAILED (user-visible U+2014 present):")
        for h in dashes:
            print("  -", h)
        sys.exit(1)

    m = audit["manifest"]
    print(json.dumps({
        "published_ipo_pages": audit["published_ipo_pages"],
        "pipeline_status": m["pipeline_status"],
        "upcoming": {k: v for k, v in audit["upcoming"].items() if k != "excluded"},
        "upcoming_excluded_count": len(audit["upcoming"]["excluded"]),
        "withdrawn": audit["withdrawn"],
        "not_ipo": audit["not_ipo"],
        "duplicates": {k: v for k, v in audit["duplicates"].items() if k != "records"},
        "history": audit["history"],
        "high_confidence_published": audit["high_confidence_published"],
        "history_window": [m["history_window_start"], m["history_window_end"]],
        "build_commit": m["build_commit"],
        "secret_scan": "clean", "em_dash_scan": "clean",
    }, indent=2))


if __name__ == "__main__":
    main()
