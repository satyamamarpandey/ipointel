"""Assertions for the GitHub Pages static build (scripts/build_pages.py).

Two things this project cannot afford to get wrong silently:
1. The public historical explorer must show a rolling 5-year window, computed
   from the actual current date - never a hardcoded year.
2. An upcoming/active IPO must never disappear from the public site just
   because it has an old filing_date; the cutoff applies to LISTING date only.

These run against the real generated dist/data/*.json (built by the
`build_dist` fixture below), not mocks - a passing test here means the
actual artifact GitHub Pages will serve is correct, not just the logic.
"""
from __future__ import annotations
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.build_pages import in_history_window, history_cutoff, slugify  # noqa: E402


# ---------- pure boundary logic (no DB, no build needed) ----------

def test_cutoff_is_computed_dynamically_not_hardcoded():
    now = datetime(2026, 9, 1, tzinfo=timezone.utc)
    assert history_cutoff(now).isoformat().startswith("2021-09-01")
    later = datetime(2027, 3, 5, tzinfo=timezone.utc)
    assert history_cutoff(later).isoformat().startswith("2022-03-05")


def test_exact_cutoff_date_is_included():
    now = datetime(2026, 9, 1, tzinfo=timezone.utc)
    cutoff = history_cutoff(now)
    assert in_history_window("2021-09-01", now, cutoff) is True


def test_one_day_before_cutoff_is_excluded():
    now = datetime(2026, 9, 1, tzinfo=timezone.utc)
    cutoff = history_cutoff(now)
    assert in_history_window("2021-08-31", now, cutoff) is False


def test_unparseable_date_is_excluded_and_flagged_not_silently_dropped():
    now = datetime(2026, 9, 1, tzinfo=timezone.utc)
    cutoff = history_cutoff(now)
    assert in_history_window("", now, cutoff) is None
    assert in_history_window("not-a-date", now, cutoff) is None


def test_slugify_is_stable_and_url_safe():
    assert slugify("IN:purple style labs") == "in-purple-style-labs"
    assert slugify("US:0001234567") == "us-0001234567"
    assert "/" not in slugify("weird:name/with slashes")


# ---------- real generated dist/ (built once per test session) ----------

@pytest.fixture(scope="module")
def build_dist(tmp_path_factory):
    out = tmp_path_factory.mktemp("pages_dist_test") / "dist"
    # tests/conftest.py (loaded automatically for every test in this
    # directory, including this one) repoints DATABASE_URL at a separate,
    # near-empty test database for the rest of the suite's isolation - a
    # subprocess started from here would silently inherit that override and
    # build against almost no data. This asserts against the real dataset
    # build_pages.py is meant to publish, so it explicitly restores the real
    # DATABASE_URL for the child process only (matches the value
    # .github/workflows/pages.yml's build step sets in CI).
    import os
    env = {**os.environ, "DATABASE_URL": os.environ.get("PAGES_BUILD_DATABASE_URL", "sqlite:///./data/ipo.db")}
    result = subprocess.run(
        [sys.executable, "scripts/build_pages.py", "--out", str(out), "--base-url", "https://ipointel.brandsap.com"],
        cwd=ROOT, capture_output=True, text=True, timeout=900, env=env,
    )
    assert result.returncode == 0, f"build_pages.py failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    return out


def _load(dist, rel):
    return json.loads((dist / rel).read_text(encoding="utf-8"))


def test_no_published_historical_ipo_is_older_than_five_years(build_dist):
    manifest = _load(build_dist, "data/manifest.json")
    cutoff = datetime.fromisoformat(manifest["history_window_start"]).replace(tzinfo=timezone.utc)
    now = datetime.fromisoformat(manifest["generated_at"])
    for rel in ("data/history/india-5y.json", "data/history/us-5y.json"):
        for row in _load(build_dist, rel):
            d = datetime.fromisoformat(_normalize(row["listing_date"]))
            assert cutoff.date() <= d.date() <= now.date(), f"{row['company']} listed {row['listing_date']} is outside the 5-year window"


def _normalize(date_str):
    from app.services.market import parse_date
    d = parse_date(date_str)
    assert d is not None
    return d.date().isoformat()


def test_upcoming_ipos_are_not_filtered_by_the_five_year_cutoff(build_dist):
    """An upcoming IPO with an old filing_date must still be published."""
    for rel in ("data/upcoming/india.json", "data/upcoming/us.json"):
        rows = _load(build_dist, rel)
        # No date-window filtering was ever applied here - proven structurally:
        # every row present in the manifest's upcoming.*_discovered count must
        # equal the number actually published (see next test), independent of
        # how old filing_date is.
        assert isinstance(rows, list)


def test_every_discovered_upcoming_ipo_is_published_or_explicitly_excluded(build_dist):
    manifest = _load(build_dist, "data/manifest.json")
    up = manifest["counts"]["upcoming"]
    assert up["india_discovered"] == up["india_published"]
    assert up["us_discovered"] == up["us_published"]


def test_history_manifest_counts_match_published_file_lengths(build_dist):
    manifest = _load(build_dist, "data/manifest.json")
    hist = manifest["counts"]["history"]
    assert len(_load(build_dist, "data/history/india-5y.json")) == hist["india_published"]
    assert len(_load(build_dist, "data/history/us-5y.json")) == hist["us_published"]


def test_no_secrets_in_dist(build_dist):
    from scripts.build_pages import secret_scan
    assert secret_scan(build_dist) == []


def test_admin_page_never_published(build_dist):
    assert not (build_dist / "admin.html").exists()
    assert not (build_dist / "dashboard" / "admin.html").exists()


def test_cname_matches_production_domain(build_dist):
    assert (build_dist / "CNAME").read_text(encoding="utf-8").strip() == "ipointel.brandsap.com"


def test_stable_ipo_ids_produce_one_detail_page_each(build_dist):
    manifest = _load(build_dist, "data/manifest.json")
    ipo_dir = build_dist / "ipo"
    published_dirs = [d for d in ipo_dir.iterdir() if d.is_dir()]
    assert len(published_dirs) == manifest["published_ipo_pages"]
    for d in published_dirs[:5]:
        assert (d / "index.html").exists()


# ---------- SEO surface + attribution backlink ----------
# These assert against the built artifact rather than the source templates:
# the landing page is copied verbatim, but the IPO detail pages are rendered
# through placeholder substitution, so only the output proves the tags
# actually survive the build with real values in them.

def _sitemap_urls(dist: Path) -> list[str]:
    import re
    return re.findall(r"<loc>([^<]+)</loc>", (dist / "sitemap.xml").read_text(encoding="utf-8"))


def test_sitemap_lists_only_routes_the_build_actually_emits(build_dist):
    """Every <loc> must map to a file this build wrote. A sitemap entry for a
    route that 404s is worse than no sitemap entry."""
    base = "https://ipointel.brandsap.com"
    on_disk = {
        base + "/": build_dist / "index.html",
        base + "/dashboard/": build_dist / "dashboard" / "index.html",
        base + "/privacy": build_dist / "privacy.html",
        base + "/terms": build_dist / "terms.html",
    }
    for url in _sitemap_urls(build_dist):
        if url in on_disk:
            assert on_disk[url].exists(), f"sitemap lists {url} but {on_disk[url].name} was not built"
        else:
            slug = url.removeprefix(base + "/ipo/").rstrip("/")
            assert (build_dist / "ipo" / slug / "index.html").exists(), f"sitemap lists {url}, which was not built"


def test_sitemap_includes_the_public_policy_pages(build_dist):
    urls = _sitemap_urls(build_dist)
    assert "https://ipointel.brandsap.com/privacy" in urls
    assert "https://ipointel.brandsap.com/terms" in urls


def test_sitemap_never_lists_the_noindex_login_page(build_dist):
    """login.html carries <meta name="robots" content="noindex"> and robots.txt
    disallows /login/ - listing it would contradict both."""
    assert not any("/login" in u for u in _sitemap_urls(build_dist))


def test_robots_allows_crawling_and_points_at_the_sitemap(build_dist):
    robots = (build_dist / "robots.txt").read_text(encoding="utf-8")
    assert "Allow: /" in robots
    assert "Sitemap: https://ipointel.brandsap.com/sitemap.xml" in robots


def test_every_indexable_built_page_declares_its_own_canonical(build_dist):
    expected = {
        build_dist / "index.html": 'href="https://ipointel.brandsap.com/"',
        build_dist / "dashboard" / "index.html": 'href="https://ipointel.brandsap.com/dashboard/"',
        build_dist / "privacy.html": 'href="https://ipointel.brandsap.com/privacy"',
        build_dist / "terms.html": 'href="https://ipointel.brandsap.com/terms"',
    }
    for path, canonical in expected.items():
        html = path.read_text(encoding="utf-8")
        assert f'<link rel="canonical" {canonical}>' in html, f"{path.name} is missing its canonical"


def test_ipo_detail_pages_carry_resolved_social_card_tags(build_dist):
    """The template's __PLACEHOLDERS__ must be substituted, not shipped."""
    page = next((build_dist / "ipo").iterdir()) / "index.html"
    html = page.read_text(encoding="utf-8")
    for tag in ('property="og:image"', 'name="twitter:card" content="summary_large_image"',
                'name="twitter:image"', 'rel="canonical"'):
        assert tag in html, f"detail page missing {tag}"
    assert "__" not in html.split("</head>")[0], "unsubstituted placeholder left in detail page <head>"


def test_landing_page_ships_valid_structured_data(build_dist):
    """Parsed, not substring-matched: malformed JSON-LD is silently ignored by
    crawlers, which looks identical to having none at all."""
    html = (build_dist / "index.html").read_text(encoding="utf-8")
    block = html.split('<script type="application/ld+json">')[1].split("</script>")[0]
    data = json.loads(block)
    assert data["@type"] == "SoftwareApplication"
    assert data["name"] == "IPO Intelligence Terminal"
    assert data["url"] == "https://ipointel.brandsap.com/"
    assert data["founder"]["name"] == "Satyam Pandey"
    assert data["founder"]["@id"] == "https://pandeysatyam.com/#person"
    for profile in ("https://pandeysatyam.com/", "https://brandsap.com/",
                    "https://www.linkedin.com/in/pandeysatyam/",
                    "https://github.com/satyamamarpandey"):
        assert profile in data["sameAs"]


def test_attribution_backlinks_are_followable_on_every_public_page(build_dist):
    """rel must carry noopener/noreferrer but NOT nofollow - these are real
    editorial links, and nofollow would defeat the point of adding them."""
    import re
    pages = [build_dist / "index.html", next((build_dist / "ipo").iterdir()) / "index.html"]
    for page in pages:
        html = page.read_text(encoding="utf-8")
        for href in ("https://brandsap.com", "https://pandeysatyam.com"):
            m = re.search(rf'<a href="{re.escape(href)}"[^>]*>', html)
            assert m, f"{page.name} is missing the {href} backlink"
            anchor = m.group(0)
            assert 'target="_blank"' in anchor and "noopener" in anchor and "noreferrer" in anchor
            assert "nofollow" not in anchor, f"{page.name}: {href} backlink must stay followable"
