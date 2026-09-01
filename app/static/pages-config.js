// Default (server-mode) config. The GitHub Pages build overwrites this exact
// file with PAGES_MODE=true plus the production base URL and (if configured)
// the public waitlist endpoint - see scripts/build_pages.py. Everything else
// (index.html/app.html/login.html, app.js, landing.js) is byte-identical
// between server mode and Pages mode; this is the only file that differs.
window.PAGES_MODE = false;
window.PUBLIC_BASE_URL = "";
window.PUBLIC_WAITLIST_ENDPOINT = "";
