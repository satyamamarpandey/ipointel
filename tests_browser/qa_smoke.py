"""Real browser QA smoke test using Playwright. Not part of the pytest suite (needs a
running server + downloaded Chromium) - run explicitly:
  .venv/Scripts/python.exe tests_browser/qa_smoke.py [base_url]
Checks every major view at 4 viewport widths for console errors, failed API
requests, and horizontal overflow, exercises waitlist signup (success, invalid,
duplicate), and opens an IPO detail + its lazy-loaded panes."""
import sys, time
from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8010"
VIEWPORTS = [("desktop-1440", 1440, 900), ("tablet-1024", 1024, 900), ("tablet-768", 768, 1024), ("mobile-390", 390, 844)]
TABS = ["radar", "calendar", "compare", "history", "reliability", "trackrecord", "modelperf"]

results = {"errors": [], "warnings": [], "checks": 0}

def record_console(page, tag):
    def on_console(msg):
        if msg.type == "error" and "422 (Unprocessable" not in msg.text:
            results["errors"].append(f"[{tag}] console error: {msg.text}")
    def on_pageerror(exc):
        results["errors"].append(f"[{tag}] page error: {exc}")
    def on_response(resp):
        if resp.status >= 400 and "/api/" in resp.url and "/api/waitlist" not in resp.url:
            results["errors"].append(f"[{tag}] API {resp.status}: {resp.url}")
    page.on("console", on_console)
    page.on("pageerror", on_pageerror)
    page.on("response", on_response)

def check_overflow(page, tag):
    overflow = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
    if overflow and overflow > 4:
        results["warnings"].append(f"[{tag}] horizontal overflow: {overflow}px")

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for name, w, h in VIEWPORTS:
            ctx = browser.new_context(viewport={"width": w, "height": h})
            page = ctx.new_page()
            record_console(page, f"landing@{name}")
            page.goto(f"{BASE}/", wait_until="domcontentloaded", timeout=20000)
            results["checks"] += 1
            check_overflow(page, f"landing@{name}")
            if page.locator("#waitlistForm, form").count() == 0:
                results["warnings"].append(f"[landing@{name}] no signup form found")
            ctx.close()

        # Landing waitlist interaction: real form submit (success), then API-level
        # invalid/duplicate checks (client-side type=email blocks invalid at the DOM
        # level, so those two are exercised directly against /api/waitlist).
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        record_console(page, "waitlist")
        page.goto(f"{BASE}/", wait_until="domcontentloaded", timeout=20000)
        test_email = f"qa.browser.{int(time.time())}@example.com"
        if page.locator("#waitlist #email").count():
            page.fill("#waitlist #email", test_email)
            page.fill("#waitlist #name", "QA Browser Test")
            page.click("#waitlist button[type=submit]")
            page.wait_for_timeout(1500)
            msg = page.locator("#message").inner_text()
            if "reserved" not in msg.lower() and "already" not in msg.lower():
                results["warnings"].append(f"[waitlist] unexpected success message: {msg!r}")
        else:
            results["warnings"].append("[waitlist] #waitlist form not found on landing page")

        invalid = page.evaluate(f"""async () => {{
            const r = await fetch('/api/waitlist', {{method:'POST', headers:{{'Content-Type':'application/json'}},
                body: JSON.stringify({{email:'not-an-email', name:'x', investor_type:'retail', markets:'both', consent:true, website:''}})}});
            return r.status;
        }}""")
        if invalid < 400:
            results["errors"].append(f"[waitlist] invalid email accepted, status={invalid}")

        dup1 = page.evaluate(f"""async () => {{
            const r = await fetch('/api/waitlist', {{method:'POST', headers:{{'Content-Type':'application/json'}},
                body: JSON.stringify({{email:'{test_email}', name:'QA', investor_type:'retail', markets:'both', consent:true, website:''}})}});
            return (await r.json()).message;
        }}""")
        if "already" not in dup1.lower():
            results["warnings"].append(f"[waitlist] duplicate signup did not report already-registered: {dup1!r}")
        ctx.close()

        # Dashboard: all tabs at desktop width
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        record_console(page, "dashboard")
        page.goto(f"{BASE}/app", wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(1500)
        for tab in TABS:
            btn = page.locator(f'.tab[data-view="{tab}"]')
            if btn.count():
                btn.click()
                page.wait_for_timeout(900)
                results["checks"] += 1
                check_overflow(page, f"dashboard/{tab}")
            else:
                results["warnings"].append(f"[dashboard] tab button missing: {tab}")

        # IPO detail + lazy panes
        page.locator('.tab[data-view="radar"]').click()
        page.wait_for_timeout(800)
        rows = page.locator("#ipoRows tr[data-id]")
        if rows.count():
            rows.first.click()
            page.wait_for_timeout(1200)
            for kind in ("dcf", "similar", "changes"):
                b = page.locator(f'[data-lazy="{kind}"]')
                if b.count():
                    b.click()
                    page.wait_for_timeout(1200)
                    results["checks"] += 1
        else:
            results["warnings"].append("[dashboard] no IPO rows to click for detail view")
        ctx.close()

        # Responsive check of dashboard at all viewports
        for name, w, h in VIEWPORTS:
            ctx = browser.new_context(viewport={"width": w, "height": h})
            page = ctx.new_page()
            record_console(page, f"dashboard@{name}")
            page.goto(f"{BASE}/app", wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(1200)
            check_overflow(page, f"dashboard@{name}")
            ctx.close()

        browser.close()

    print(f"checks_run={results['checks']}")
    print(f"errors={len(results['errors'])}")
    for e in results["errors"]:
        print("ERROR:", e)
    print(f"warnings={len(results['warnings'])}")
    for w in results["warnings"]:
        print("WARN:", w)
    return 1 if results["errors"] else 0

if __name__ == "__main__":
    sys.exit(main())
