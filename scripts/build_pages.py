#!/usr/bin/env python
"""Static-site generator for the GitHub Pages production build of IPO
Intelligence (ipointel.brandsap.com).

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
    dist/data/manifest.json            build metadata, counts, cutoffs, source status
    dist/data/highlights.json          landing hero/ticker (mirrors /api/public/highlights)
    dist/data/upcoming/{india,us}.json all currently discovered upcoming/active IPOs
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
from sqlalchemy import select  # noqa: E402

STATIC = ROOT / "app" / "static"
UPCOMING_STATUSES = ("Filed", "Open", "Upcoming")


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
    path.write_text(json.dumps(obj, indent=None, separators=(",", ":"), default=str), encoding="utf-8")


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


def classify(db, now: datetime, window_years: int = 5):
    """Splits every IPO row into upcoming / published-history / excluded-history,
    using the SAME date parser the rest of the app trusts for these exact
    formats (app.services.market.parse_date), never a raw string comparison -
    the underlying listing_date column mixes "30-Nov-2022" (India) and
    "20251022" (US) styles, which do not sort or compare correctly as strings.

    The cutoff applies ONLY to Listed rows. An upcoming IPO is never excluded
    for having an old filing_date - see in_history_window's docstring and
    tests/test_pages_build.py::test_upcoming_never_excluded_by_filing_date."""
    cutoff = history_cutoff(now, window_years)
    rows = db.scalars(select(IPO)).all()
    upcoming, history_in, history_out, unparseable = [], [], [], []
    for ipo in rows:
        if ipo.status in UPCOMING_STATUSES:
            upcoming.append(ipo)
        elif ipo.status == "Listed":
            verdict = in_history_window(ipo.listing_date, now, cutoff)
            if verdict is None:
                unparseable.append(ipo)
            elif verdict:
                history_in.append(ipo)
            else:
                history_out.append(ipo)
        # any other status (none currently exist in this pipeline) is neither
        # published nor silently dropped without accounting - see manifest.excluded.
    return cutoff, upcoming, history_in, history_out, unparseable


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
    if age_hours <= 2:
        return "LIVE"
    if age_hours <= 24:
        return "DELAYED"
    return "STALE"


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
    cutoff, upcoming, history_in, history_out, unparseable = classify(db, now)

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

    published = upcoming + history_in

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
    for base, ids in base_slug_ids.items():
        ids.sort()
        for pos, ipo_id in enumerate(ids):
            id_to_slug[ipo_id] = base if pos == 0 else f"{base}-{ipo_id}"

    for i, ipo in enumerate(published):
        write_json(out_dir / "data" / "ipo" / f"{ipo.id}.json", full_detail(ipo))
        if (i + 1) % 200 == 0:
            print(f"  ...{i + 1}/{len(published)} IPO detail artifacts built", file=sys.stderr)

    def bucket(rows, country):
        return [M.ipo_json(db, x) for x in rows if x.country == country]

    upcoming_india, upcoming_us = bucket(upcoming, "India"), bucket(upcoming, "United States")
    history_india, history_us = bucket(history_in, "India"), bucket(history_in, "United States")
    history_india.sort(key=lambda x: parse_date(x["listing_date"]) or now, reverse=True)
    history_us.sort(key=lambda x: parse_date(x["listing_date"]) or now, reverse=True)

    write_json(out_dir / "data" / "upcoming" / "india.json", upcoming_india)
    write_json(out_dir / "data" / "upcoming" / "us.json", upcoming_us)
    write_json(out_dir / "data" / "history" / "india-5y.json", history_india)
    write_json(out_dir / "data" / "history" / "us-5y.json", history_us)

    write_json(out_dir / "data" / "highlights.json", M.public_highlights(db=db))
    write_json(out_dir / "data" / "track-record.json", M.track_record(limit=500, db=db, _lead=None))
    write_json(out_dir / "data" / "backtest.json", M.backtest(db=db, _lead=None))
    write_json(out_dir / "data" / "model-performance.json", M.model_performance(db=db, _lead=None))

    raw_source_rows = M._source_health_rows(db)
    public_sources = {"SEC EDGAR", "SEC Priced IPOs", "NSE", "NSE Primary Market Reports", "Licensed enrichment feed"}
    enrichment_configured = bool(get_settings().secondary_enrichment_url)
    source_health = []
    for r in raw_source_rows:
        if r["source"] not in public_sources:
            continue  # never publish backend-operational rows (email/worker) on the public site
        optional = r["source"] == "Licensed enrichment feed" and not enrichment_configured
        source_health.append({**r, "public_status": source_status(r, now, optional_unconfigured=optional)})
    write_json(out_dir / "data" / "source-health.json", source_health)

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

    audit["upcoming"] = {
        "india_discovered": len(upcoming_india), "us_discovered": len(upcoming_us),
        "india_published": len(upcoming_india), "us_published": len(upcoming_us),
        "excluded": [],  # every discovered upcoming row is published as-is - no filtering applied here
    }
    audit["history"] = {
        "india_published": len(history_india), "us_published": len(history_us),
        "india_excluded_out_of_window": len([x for x in history_out if x.country == "India"]),
        "us_excluded_out_of_window": len([x for x in history_out if x.country == "United States"]),
        "india_excluded_unparseable_date": len([x for x in unparseable if x.country == "India"]),
        "us_excluded_unparseable_date": len([x for x in unparseable if x.country == "United States"]),
    }

    # ---------- manifest ----------
    manifest = {
        "generated_at": now.isoformat(),
        "history_window_start": cutoff.date().isoformat(),
        "history_window_end": now.date().isoformat(),
        "model_version": (history_india[0]["score"]["model_version"] if history_india and history_india[0].get("score") else
                           (upcoming_india[0]["score"]["model_version"] if upcoming_india and upcoming_india[0].get("score") else "unknown")),
        "schema_version": "1",
        "build_commit": git_sha(),
        "base_url": base_url,
        "summary": summary,
        "counts": audit,
        "published_ipo_pages": len(published),
    }
    write_json(out_dir / "data" / "manifest.json", manifest)

    # ---------- static assets ----------
    static_out = out_dir / "static"
    shutil.copytree(STATIC / "brand", static_out / "brand")
    for fn in ["styles.css", "landing.js", "app.js", "login.js", "pages-adapter.js", "pages-ipo-detail.js", "site.webmanifest"]:
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
    urls = [base_url + "/", base_url + "/dashboard/"]
    for ipo in published:
        slug = id_to_slug[ipo.id]
        canonical = f"{base_url}/ipo/{slug}/"
        page = (template
                .replace("__TITLE__", f"{ipo.company} — IPO Intelligence")
                .replace("__DESCRIPTION__", f"Evidence-first score, valuation and risk analysis for {ipo.company} ({ipo.country}).")
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

    not_found_html = (STATIC / "index.html").read_text(encoding="utf-8")
    (out_dir / "404.html").write_text(not_found_html, encoding="utf-8")

    db.close()
    audit["manifest"] = manifest
    audit["published_ipo_pages"] = len(published)
    return audit


SECRET_PATTERNS = [
    r"CLERK_SECRET", r"GOOGLE_APPLICATION_CREDENTIALS", r"AWS_SECRET", r"RESEND_API_KEY",
    r"DATABASE_URL\s*=\s*postgresql", r"ADMIN_TOKEN\s*=\s*(?!change-me)", r"POSTGRES_PASSWORD",
    r"-----BEGIN (RSA |EC )?PRIVATE KEY-----",
]


def secret_scan(out_dir: Path) -> list[str]:
    hits = []
    pats = [re.compile(p) for p in SECRET_PATTERNS]
    for f in out_dir.rglob("*"):
        if not f.is_file() or f.suffix in (".png", ".ico", ".jpg"):
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for p in pats:
            if p.search(text):
                hits.append(f"{p.pattern} in {f.relative_to(out_dir)}")
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

    print(json.dumps({
        "published_ipo_pages": audit["published_ipo_pages"],
        "upcoming": audit["upcoming"],
        "history": audit["history"],
        "history_window": [audit["manifest"]["history_window_start"], audit["manifest"]["history_window_end"]],
        "build_commit": audit["manifest"]["build_commit"],
        "secret_scan": "clean",
    }, indent=2))


if __name__ == "__main__":
    main()
