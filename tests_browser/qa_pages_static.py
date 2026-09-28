"""Real-browser QA of the GitHub Pages build, served by a plain static file
server (never FastAPI) - exactly how the production site is hosted.

  python scripts/build_pages.py --out dist
  python tests_browser/qa_pages_static.py [dist_dir] [--keep]

Serves dist/ on a local port with extensionless-route and 404.html handling
that mirrors GitHub Pages, then drives Chromium through landing, dashboard
(every tab, India/US/SME filters, search, sort, compare), an IPO detail page,
privacy, terms, login, a fake URL (must be a real HTTP 404 with the branded
page), and the early-access modal, at six viewport widths. Fails on any
console error, page error, failed data request, horizontal overflow, raw
HTML rendering, or user-visible em dash. Exit code 0 only if everything
passed; the JSON report is written to tests_browser/qa_pages_static_report.json."""
from __future__ import annotations
import http.server, json, os, socketserver, sys, threading, time
from functools import partial
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
DIST = Path(sys.argv[1]) if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else ROOT / "dist"
VIEWPORTS = [("desktop-1920", 1920, 1080), ("desktop-1644", 1644, 900), ("desktop-1440", 1440, 900),
             ("tablet-1024", 1024, 900), ("tablet-768", 768, 1024), ("mobile-390", 390, 844)]
TABS = ["radar", "calendar", "compare", "history", "reliability", "trackrecord", "modelperf"]
EM_DASH = "—"


class PagesHandler(http.server.SimpleHTTPRequestHandler):
    """Mimics GitHub Pages: /privacy -> privacy.html, /x/ -> /x/index.html,
    unknown -> 404.html with a real 404 status."""
    def log_message(self, *a): pass
    def send_head(self):
        path = self.translate_path(self.path)
        if os.path.isdir(path):
            if not self.path.endswith("/"):
                self.send_response(301); self.send_header("Location", self.path.split("?")[0] + "/"); self.end_headers(); return None
            path = os.path.join(path, "index.html")
        if not os.path.exists(path) and os.path.exists(path + ".html"):
            path = path + ".html"
        if not os.path.exists(path):
            body = (DIST / "404.html").read_bytes()
            self.send_response(404); self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body))); self.end_headers()
            return __import__("io").BytesIO(body)
        self.path = "/" + os.path.relpath(path, DIST).replace("\\", "/")
        return super().send_head()


def serve():
    handler = partial(PagesHandler, directory=str(DIST))
    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


results = {"errors": [], "warnings": [], "checks": 0, "timings_ms": {}}


def attach(page, tag):
    # The deliberate fake-URL navigation is a real HTTP 404 and Chromium logs
    # it as a console error; that one is the expected outcome, not a defect.
    page.on("console", lambda m: results["errors"].append(f"[{tag}] console {m.type}: {m.text}") if m.type == "error" and "favicon" not in m.text and "/fake-" not in page.url else None)
    page.on("pageerror", lambda e: results["errors"].append(f"[{tag}] page error: {e}"))
    page.on("response", lambda r: results["errors"].append(f"[{tag}] HTTP {r.status}: {r.url}") if r.status >= 400 and ("/data/" in r.url or "/static/" in r.url) and "/fake-" not in r.url else None)


def check_page(page, tag, expect_text=None):
    results["checks"] += 1
    overflow = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
    if overflow and overflow > 4:
        results["errors"].append(f"[{tag}] horizontal overflow: {overflow}px")
    body = page.inner_text("body")
    if EM_DASH in body:
        results["errors"].append(f"[{tag}] user-visible em dash present")
    if "&lt;" in body or "<div" in body or "</span>" in body:
        results["errors"].append(f"[{tag}] raw HTML rendered as text")
    # innerText applies CSS text-transform, so compare case-insensitively.
    if expect_text and expect_text.lower() not in body.lower():
        results["errors"].append(f"[{tag}] expected text missing: {expect_text!r}")


def wait_rows(page, selector, tag, min_rows=1, timeout=15000):
    try:
        page.wait_for_function(f"document.querySelectorAll('{selector}').length >= {min_rows}", timeout=timeout)
    except Exception:
        results["errors"].append(f"[{tag}] fewer than {min_rows} rows in {selector}")


def run():
    httpd, base = serve()
    manifest = json.loads((DIST / "data" / "manifest.json").read_text(encoding="utf-8"))
    upcoming_in = json.loads((DIST / "data" / "upcoming" / "india.json").read_text(encoding="utf-8"))
    upcoming_us = json.loads((DIST / "data" / "upcoming" / "us.json").read_text(encoding="utf-8"))
    history_in = json.loads((DIST / "data" / "history" / "india-5y.json").read_text(encoding="utf-8"))
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for name, w, h in VIEWPORTS:
            ctx = browser.new_context(viewport={"width": w, "height": h}, reduced_motion="reduce" if name == "mobile-390" else "no-preference")
            page = ctx.new_page()
            attach(page, name)
            # ---- landing
            t0 = time.time(); page.goto(base + "/", wait_until="networkidle"); results["timings_ms"][f"landing@{name}"] = round((time.time() - t0) * 1000)
            wait_rows(page, "#ipoRowsPublic tr td .issuer", name + " landing")
            check_page(page, name + " landing", "Know the IPO")
            # modal: auto-offers once after ~3s, moves focus into the form,
            # traps Tab, closes on ESC; every "Join early access" trigger
            # re-opens it; the close button closes it.
            page.wait_for_selector("#modalOverlay.open", timeout=6000)
            focused = page.evaluate("document.activeElement && document.activeElement.id")
            if focused != "modalEmail":
                results["errors"].append(f"[{name}] modal did not move focus to the email field (got {focused})")
            page.keyboard.press("Shift+Tab")
            if not page.evaluate("document.getElementById('modalOverlay').contains(document.activeElement)"):
                results["errors"].append(f"[{name}] modal focus trap let focus escape")
            page.keyboard.press("Escape")
            page.wait_for_function("document.getElementById('modalOverlay').hidden === true", timeout=3000)
            check_page(page, name + " landing+modal")
            if w > 900:
                page.click("#navCta"); page.wait_for_selector("#modalOverlay.open", timeout=3000)
                page.click("#modalClose"); page.wait_for_function("document.getElementById('modalOverlay').hidden === true", timeout=3000)
            if w <= 900:
                page.click("#navBurger"); page.wait_for_selector("#mobileNav.open", timeout=3000); page.keyboard.press("Escape")
            # ---- dashboard
            t0 = time.time(); page.goto(base + "/dashboard/", wait_until="networkidle"); results["timings_ms"][f"dashboard@{name}"] = round((time.time() - t0) * 1000)
            wait_rows(page, "#ipoRows tr[data-id]", name + " radar", min_rows=min(10, len(upcoming_in) + len(upcoming_us)))
            check_page(page, name + " radar")
            live_text = page.inner_text("#liveText")
            if "Loading" in live_text:
                results["errors"].append(f"[{name}] pipeline status pill never resolved")
            for tab in TABS:
                page.click(f".tab[data-view='{tab}']"); page.wait_for_timeout(250)
                if tab == "history":
                    wait_rows(page, "#perfRows tr", name + " history", min_rows=10)
                if tab == "modelperf":
                    page.wait_for_function("document.getElementById('modelPerf').dataset.loaded === '1'", timeout=10000)
                if tab == "reliability":
                    wait_rows(page, "#sourceHealth .sourceitem", name + " reliability", min_rows=3)
                if tab == "trackrecord":
                    page.wait_for_function("document.getElementById('forwardrecord').children.length > 0", timeout=10000)
                if tab == "compare":
                    page.click("#compareBtn"); page.wait_for_function("document.querySelectorAll('#compareGrid .card').length >= 1", timeout=10000)
                check_page(page, f"{name} tab:{tab}")
                if page.evaluate(f"document.getElementById('{tab}').hidden"):
                    results["errors"].append(f"[{name}] tab {tab} panel hidden after activation")
            # keyboard tab navigation
            page.focus(".tab[aria-selected='true']"); page.keyboard.press("Home"); page.keyboard.press("ArrowRight")
            if page.evaluate("document.querySelector('.tab[aria-selected=\"true\"]').dataset.view") != "calendar":
                results["errors"].append(f"[{name}] arrow-key tab navigation failed")
            page.click(".tab[data-view='radar']")
            # ---- filters: India, US, SME, Listed, Withdrawn, search, sort
            page.select_option("#country", "India"); wait_rows(page, "#ipoRows tr[data-id]", name + " india", min_rows=min(5, len(upcoming_in)))
            page.select_option("#board", "SME"); page.wait_for_timeout(200)
            sme_rows = page.evaluate("document.querySelectorAll('#ipoRows tr[data-id]').length")
            page.select_option("#board", "all")
            page.select_option("#country", "United States"); wait_rows(page, "#ipoRows tr[data-id]", name + " us", min_rows=min(5, len(upcoming_us)))
            page.select_option("#country", "all")
            page.select_option("#status", "Listed"); wait_rows(page, "#ipoRows tr[data-id]", name + " listed", min_rows=50)
            if not page.is_visible("#historyWindowNote"):
                results["errors"].append(f"[{name}] five-year window note not shown for Listed filter")
            page.select_option("#status", "Withdrawn"); page.wait_for_timeout(400)
            check_page(page, name + " withdrawn")
            page.select_option("#status", "all"); wait_rows(page, "#ipoRows tr[data-id]", name + " all again")
            page.select_option("#sort", "confidence"); page.wait_for_timeout(200)
            confs = page.evaluate("[...document.querySelectorAll('#ipoRows tr[data-id] td:nth-child(8)')].map(t=>parseFloat(t.textContent)||-1)")
            if confs != sorted(confs, reverse=True):
                results["errors"].append(f"[{name}] confidence sort not descending")
            target = upcoming_in[0]["company"][:12] if upcoming_in else upcoming_us[0]["company"][:12]
            page.fill("#search", target); page.wait_for_timeout(600)
            wait_rows(page, "#ipoRows tr[data-id]", name + " search")
            page.fill("#search", ""); page.wait_for_timeout(500)
            # ---- detail pane in the radar
            page.click("#ipoRows tr[data-id]"); page.wait_for_function("document.querySelector('#detail h2') !== null", timeout=10000)
            page.click("#detail [data-lazy='dcf']"); page.wait_for_function("document.getElementById('lazyPane').innerText.length > 20", timeout=10000)
            page.click("#detail [data-lazy='similar']"); page.wait_for_timeout(400)
            page.click("#detail [data-lazy='changes']"); page.wait_for_timeout(400)
            check_page(page, name + " detail pane", "Confidence")
            # ---- standalone IPO detail pages (one India, one US, one history)
            for row in [x for x in (upcoming_in[:1] + upcoming_us[:1] + history_in[:1])]:
                slug_dir = next((d for d in (DIST / "ipo").iterdir() if (d / "index.html").exists() and f'data-ipo-id="{row["id"]}"' in (d / "index.html").read_text(encoding="utf-8")), None)
                if not slug_dir:
                    results["errors"].append(f"[{name}] no detail page for IPO {row['id']}"); continue
                t0 = time.time(); page.goto(f"{base}/ipo/{slug_dir.name}/", wait_until="networkidle"); results["timings_ms"][f"detail@{name}"] = round((time.time() - t0) * 1000)
                page.wait_for_function("document.querySelector('#pagesDetail h1') !== null", timeout=10000)
                check_page(page, f"{name} ipo:{slug_dir.name}", "Evidence stack")
                if "insufficient reliable data" in page.inner_text("#pagesDetail").lower() and "withheld by design" not in page.inner_text("#pagesDetail").lower():
                    results["errors"].append(f"[{name}] gated recommendation shown without explanation on {slug_dir.name}")
            # ---- static pages
            for path, text in (("/privacy", "Privacy"), ("/terms", "Terms"), ("/login/", "Sign in")):
                page.goto(base + path, wait_until="networkidle"); check_page(page, f"{name} {path}", text)
            # ---- 404
            resp = page.goto(base + "/fake-url-that-does-not-exist/", wait_until="networkidle")
            if resp.status != 404:
                results["errors"].append(f"[{name}] fake URL returned HTTP {resp.status}, expected 404")
            check_page(page, name + " 404", "Error 404")
            if not page.is_visible(".notfound-actions a[href='/dashboard/']") or not page.is_visible(".notfound-actions a[href='/']"):
                results["errors"].append(f"[{name}] 404 page missing Home/Dashboard links")
            ctx.close()
        browser.close()
    httpd.shutdown()
    results["manifest"] = {"generated_at": manifest["generated_at"], "pipeline_status": manifest["pipeline_status"], "published_ipo_pages": manifest["published_ipo_pages"]}
    results["sme_rows_india_sample"] = sme_rows
    out = ROOT / "tests_browser" / "qa_pages_static_report.json"
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in results.items() if k != "timings_ms"}, indent=2))
    print("timings_ms:", json.dumps(results["timings_ms"]))
    return 0 if not results["errors"] else 1


if __name__ == "__main__":
    sys.exit(run())
