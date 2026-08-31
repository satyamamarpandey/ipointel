"""Real browser QA smoke test using Playwright. Not part of the pytest suite (needs a
running server + downloaded Chromium) - run explicitly:
  .venv/Scripts/python.exe tests_browser/qa_smoke.py [base_url]
Checks every major view at 4 viewport widths for console errors, failed API
requests, and horizontal overflow, exercises waitlist signup (success, invalid,
duplicate), and opens an IPO detail + its lazy-loaded panes.

/app is beta-gated (see app/services/auth.py) - this script creates its own
disposable waitlist lead, invites it via the admin API, redeems the invite
link from Mailpit, and reuses that session cookie for every authenticated
context it opens. Requires ADMIN_TOKEN (env var, falls back to .env.production's
value) and a reachable Mailpit at MAILPIT_URL.

The script only has HTTP access (no DB), so cleanup_lead() can only disable
the test lead via the admin API, not delete it - each run leaves one
DISABLED test lead (qa.browser.auth.<timestamp>@example.com) and one
WAITLISTED one (qa.browser.<timestamp>@example.com, from the plain landing-
page signup check) in the database. Prune periodically with:
    DELETE FROM email_messages WHERE lead_id IN (SELECT id FROM waitlist_leads WHERE email LIKE 'qa.browser%@example.com');
    DELETE FROM waitlist_leads WHERE email LIKE 'qa.browser%@example.com';
"""
import os, re, sys, time, json, urllib.request, urllib.error
from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8010"
MAILPIT_URL = os.environ.get("MAILPIT_URL", "http://127.0.0.1:8025")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
VIEWPORTS = [("desktop-1920", 1920, 1080), ("desktop-1644", 1644, 900), ("desktop-1440", 1440, 900), ("tablet-1024", 1024, 900), ("tablet-768", 768, 1024), ("mobile-390", 390, 844)]
TABS = ["radar", "calendar", "compare", "history", "reliability", "trackrecord", "modelperf"]


def _http(method, url, headers=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def create_authenticated_lead():
    """Signs up + admin-invites + redeems a disposable test lead. Returns
    (lead_id, session_cookie_value) or (None, None) with a warning recorded
    if ADMIN_TOKEN/Mailpit aren't available - callers must handle that."""
    if not ADMIN_TOKEN:
        results["warnings"].append("[auth] ADMIN_TOKEN not set - skipping authenticated-page checks")
        return None, None
    email = f"qa.browser.auth.{int(time.time())}@example.com"
    _http("POST", f"{BASE}/api/waitlist", {"Content-Type": "application/json"},
          {"email": email, "name": "QA Auth", "consent": True})
    users = _http("GET", f"{BASE}/api/admin/users", {"X-Admin-Token": ADMIN_TOKEN})
    lead = next((u for u in users if u["email"] == email), None)
    if not lead:
        results["warnings"].append("[auth] could not find just-created test lead")
        return None, None
    _http("POST", f"{BASE}/api/admin/users/{lead['id']}/invite", {"X-Admin-Token": ADMIN_TOKEN})
    token = None
    for _ in range(10):
        msgs = _http("GET", f"{MAILPIT_URL}/api/v1/messages?limit=10")["messages"]
        for m in msgs:
            if m["To"][0]["Address"] == email and "sign-in" in m["Subject"].lower():
                full = _http("GET", f"{MAILPIT_URL}/api/v1/message/{m['ID']}")
                match = re.search(r"auth/callback\?token=([A-Za-z0-9_\-]+)", full.get("Text", "") + full.get("HTML", ""))
                if match:
                    token = match.group(1)
                break
        if token:
            break
        time.sleep(0.5)
    if not token:
        results["warnings"].append("[auth] invite email/token not found in Mailpit")
        return lead["id"], None
    # Default urlopen auto-follows the 307 and discards its Set-Cookie
    # header along with the redirect - use a no-redirect opener so we see
    # the actual /auth/callback response.
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **kw):
            return None
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(f"{BASE}/auth/callback?token={token}")
    try:
        resp = opener.open(req, timeout=15)
    except urllib.error.HTTPError as e:
        resp = e  # HTTPError still carries .headers for 3xx "errors"
    cookie_header = resp.headers.get_all("Set-Cookie") or []
    session_val = None
    for c in cookie_header:
        m = re.match(r"ipo_session=([^;]+)", c)
        if m:
            session_val = m.group(1)
    if not session_val:
        results["warnings"].append("[auth] redeem did not set a session cookie")
    return lead["id"], session_val


def cleanup_lead(lead_id):
    if not lead_id or not ADMIN_TOKEN:
        return
    try:
        _http("POST", f"{BASE}/api/admin/users/{lead_id}/disable", {"X-Admin-Token": ADMIN_TOKEN})
    except Exception:
        pass

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

def layout_assertions(page, tag, viewport_width):
    """Computed-style/bounding-box checks for the specific failure mode a
    2026-08-31 manual review caught that DOM/console checks missed entirely:
    a stale/unstyled page render (oversized raw-SVG logo, visible hamburger
    on desktop, visibly-laid-out skip-link, nav links running together).
    These assert against computed values, not screenshots, so they fail
    loudly and specifically instead of silently passing on a broken render."""
    is_desktop = viewport_width >= 900
    data = page.evaluate("""() => {
        const cs = el => el ? getComputedStyle(el) : null;
        const rect = el => el ? el.getBoundingClientRect() : null;
        const $ = s => document.querySelector(s);
        const logo = $('.navbrand svg'), nav = $('.sitenav'), burger = $('.navburger'),
              navlinks = $('.navlinks'), skip = $('.skip-link'), hero = $('.hero-wrap'), h1 = $('h1');
        return {
            logoH: logo ? Math.round(rect(logo).height) : null,
            burgerDisplay: burger ? cs(burger).display : null,
            navlinksDisplay: navlinks ? cs(navlinks).display : null,
            navH: nav ? Math.round(rect(nav).height) : null,
            skipPosition: skip ? cs(skip).position : null,
            skipLeft: skip ? cs(skip).left : null,
            heroTop: hero ? Math.round(rect(hero).top) : null,
            bodyFontSize: parseFloat(cs(document.body).fontSize),
            h1FontSize: h1 ? parseFloat(cs(h1).fontSize) : null,
        };
    }""")
    results["checks"] += 1
    if data["logoH"] is not None and data["logoH"] > 40:
        results["errors"].append(f"[{tag}] logo mark height {data['logoH']}px exceeds 40px - looks like unstyled/raw SVG")
    skip_offscreen = data["skipPosition"] == "absolute" and str(data["skipLeft"]).startswith("-")
    if not skip_offscreen:
        results["errors"].append(f"[{tag}] skip-link is not off-screen by default (position={data['skipPosition']}, left={data['skipLeft']})")
    if data["heroTop"] is not None and data["navH"] is not None and data["heroTop"] > data["navH"] + 40:
        results["warnings"].append(f"[{tag}] hero starts {data['heroTop']}px down, well past the nav ({data['navH']}px) - unexpected gap above the fold")
    if data["bodyFontSize"] is not None and data["bodyFontSize"] < 15:
        results["errors"].append(f"[{tag}] body font-size {data['bodyFontSize']}px looks unstyled")
    if is_desktop:
        if data["burgerDisplay"] not in ("none", None):
            results["errors"].append(f"[{tag}] hamburger is visible on desktop (display={data['burgerDisplay']})")
        if data["navlinksDisplay"] in ("none", None):
            results["errors"].append(f"[{tag}] desktop nav links are hidden at a desktop width")
        if data["h1FontSize"] is not None and data["h1FontSize"] < 40:
            results["errors"].append(f"[{tag}] H1 font-size {data['h1FontSize']}px too small for a desktop hero")
    else:
        if data["burgerDisplay"] in ("none", None):
            results["errors"].append(f"[{tag}] hamburger is hidden at a mobile width")
        if data["navlinksDisplay"] not in ("none", None):
            results["errors"].append(f"[{tag}] desktop nav links are visible at a mobile width (display={data['navlinksDisplay']})")

def main():
    from urllib.parse import urlparse
    host = urlparse(BASE).hostname or "127.0.0.1"
    lead_id = session_cookie = None

    def authed_context(**kw):
        ctx = browser.new_context(**kw)
        if session_cookie:
            ctx.add_cookies([{"name": "ipo_session", "value": session_cookie, "domain": host, "path": "/"}])
        return ctx

    with sync_playwright() as p:
        browser = p.chromium.launch()
        lead_id, session_cookie = create_authenticated_lead()
        for name, w, h in VIEWPORTS:
            ctx = browser.new_context(viewport={"width": w, "height": h})
            page = ctx.new_page()
            record_console(page, f"landing@{name}")
            page.goto(f"{BASE}/", wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(300)
            results["checks"] += 1
            check_overflow(page, f"landing@{name}")
            layout_assertions(page, f"landing@{name}", w)
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
        if page.locator("#waitlistForm #email").count():
            page.fill("#waitlistForm #email", test_email)
            page.fill("#waitlistForm #name", "QA Browser Test")
            page.click("#waitlistForm button[type=submit]")
            page.wait_for_timeout(1500)
            msg = page.locator("#waitlistForm #message").inner_text()
            if "reserved" not in msg.lower() and "already" not in msg.lower():
                results["warnings"].append(f"[waitlist] unexpected success message: {msg!r}")
        else:
            results["warnings"].append("[waitlist] #waitlistForm not found on landing page")

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

        # Early-access modal: 3s delay, ESC dismiss + persistence (does not
        # reopen on reload), then a real submit via the modal's own form.
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        record_console(page, "modal")
        page.goto(f"{BASE}/", wait_until="domcontentloaded", timeout=20000)
        overlay = page.locator("#modalOverlay")
        if overlay.count() == 0:
            results["errors"].append("[modal] #modalOverlay not present in DOM")
        else:
            page.wait_for_timeout(3400)
            results["checks"] += 1
            if "open" not in (overlay.get_attribute("class") or ""):
                results["errors"].append("[modal] did not open ~3s after landing")
            else:
                page.keyboard.press("Escape")
                page.wait_for_timeout(400)
                if "open" in (overlay.get_attribute("class") or ""):
                    results["errors"].append("[modal] ESC did not close it")
                page.reload(wait_until="domcontentloaded")
                page.wait_for_timeout(3400)
                if "open" in (overlay.get_attribute("class") or ""):
                    results["warnings"].append("[modal] reopened on reload despite recent dismissal")
        ctx.close()

        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        record_console(page, "modal-submit")
        page.goto(f"{BASE}/", wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(3400)
        modal_email = f"qa.browser.modal.{int(time.time())}@example.com"
        if page.locator("#modalForm #modalEmail").count() and "open" in (page.locator("#modalOverlay").get_attribute("class") or ""):
            page.fill("#modalForm #modalEmail", modal_email)
            page.click("#modalForm button[type=submit]")
            page.wait_for_timeout(1200)
            results["checks"] += 1
            if "open" in (page.locator("#modalOverlay").get_attribute("class") or ""):
                results["warnings"].append("[modal] stayed open after a successful submit")
        else:
            results["warnings"].append("[modal] not open at submit-test time - could not exercise modal signup")

        # Brand assets actually resolve (not just referenced in <head>).
        for path in ("/static/favicon.ico", "/static/brand/favicon.svg", "/static/brand/apple-touch-icon.png", "/static/site.webmanifest"):
            r = page.request.get(f"{BASE}{path}")
            results["checks"] += 1
            if r.status != 200:
                results["errors"].append(f"[brand] {path} returned {r.status}")
        ctx.close()

        # Dashboard: all tabs at desktop width (requires the authenticated
        # session - authed_context() degrades gracefully with no cookie: the
        # app redirects to /login and the tab-click loop below just reports
        # its existing "tab button missing" warnings, no crash).
        ctx = authed_context(viewport={"width": 1440, "height": 900})
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
        radar_tab = page.locator('.tab[data-view="radar"]')
        if radar_tab.count():
            radar_tab.click()
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
            ctx = authed_context(viewport={"width": w, "height": h})
            page = ctx.new_page()
            record_console(page, f"dashboard@{name}")
            page.goto(f"{BASE}/app", wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(1200)
            check_overflow(page, f"dashboard@{name}")
            ctx.close()

        # /login and /admin as rendered pages (branding, not full auth flow -
        # that's already covered by create_authenticated_lead()).
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        record_console(page, "login")
        page.goto(f"{BASE}/login", wait_until="domcontentloaded", timeout=20000)
        results["checks"] += 1
        check_overflow(page, "login")
        if page.locator(".navbrand").count() == 0:
            results["warnings"].append("[login] branded navbrand mark not found")
        if page.locator("#socialProviders").is_visible():
            results["warnings"].append("[login] social providers visible despite Clerk not being configured")
        ctx.close()

        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        record_console(page, "admin")
        page.goto(f"{BASE}/admin", wait_until="domcontentloaded", timeout=20000)
        results["checks"] += 1
        check_overflow(page, "admin")
        ctx.close()

        browser.close()
        cleanup_lead(lead_id)

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
