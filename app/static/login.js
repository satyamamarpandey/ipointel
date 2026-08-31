(() => {
  const form = document.getElementById('loginForm'), msg = document.getElementById('loginMessage');
  const err = new URLSearchParams(location.search).get('error');
  if (err) {
    const label = {
      invalid_token: 'That sign-in link is invalid.', already_used: 'That sign-in link was already used.',
      expired: 'That sign-in link expired - request a new one below.', disabled: 'This account has been disabled.',
      revoked: 'That sign-in link was revoked.',
    }[err] || 'Could not sign you in with that link.';
    msg.textContent = label;
  }
  form.addEventListener('submit', async e => {
    e.preventDefault();
    msg.textContent = 'Sending…';
    try {
      const email = document.getElementById('email').value.trim();
      const r = await fetch('/api/auth/request-login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ email }) });
      const body = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(body.detail || 'Could not send sign-in link.');
      msg.textContent = body.message || 'If that email has beta access, a sign-in link is on its way.';
      form.querySelector('button').disabled = true;
    } catch (er) { msg.textContent = er.message || 'Could not send sign-in link.'; }
  });

  // ---------- Clerk social sign-in (Google / Apple) - fully inert until a
  // CLERK_PUBLISHABLE_KEY is configured server-side, see /api/clerk/config
  // and app/services/clerk_auth.py. IMPLEMENTED, not necessarily LIVE
  // CONFIGURED - see the V0.4 certification report. ----------
  async function initClerk() {
    let cfg;
    try { cfg = await (await fetch('/api/clerk/config')).json(); } catch (e) { return; }
    if (!cfg.configured) return;

    document.getElementById('socialProviders').style.display = 'flex';
    document.getElementById('authDivider').style.display = 'flex';

    let host;
    try {
      const encoded = cfg.publishable_key.split('_').slice(2).join('_');
      const padded = encoded + '='.repeat((4 - encoded.length % 4) % 4);
      host = atob(padded).replace(/\$$/, '');
    } catch (e) { return; }

    await new Promise((resolve, reject) => {
      const s = document.createElement('script');
      s.async = true; s.crossOrigin = 'anonymous';
      s.dataset.clerkPublishableKey = cfg.publishable_key;
      s.src = `https://${host}/npm/@clerk/clerk-js@5/dist/clerk.browser.js`;
      s.addEventListener('load', resolve); s.addEventListener('error', reject);
      document.head.appendChild(s);
    }).catch(() => {});
    if (!window.Clerk) return;
    await window.Clerk.load();

    // Returning from an OAuth redirect: Clerk's own client state is already
    // populated by the time load() resolves - bridge it into our session.
    if (window.Clerk.session) await completeClerkSignIn();

    document.getElementById('googleBtn').addEventListener('click', () => startOAuth('oauth_google'));
    document.getElementById('appleBtn').addEventListener('click', () => startOAuth('oauth_apple'));
  }

  async function startOAuth(strategy) {
    try {
      await window.Clerk.client.signIn.authenticateWithRedirect({
        strategy, redirectUrl: location.origin + '/login', redirectUrlComplete: location.origin + '/login',
      });
    } catch (e) { msg.textContent = 'Could not start sign-in - try again.'; }
  }

  async function completeClerkSignIn() {
    try {
      const token = await window.Clerk.session.getToken();
      const r = await fetch('/api/auth/clerk-session', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ session_token: token }) });
      const body = await r.json().catch(() => ({}));
      if (r.ok && body.ok) { location.href = '/app'; return; }
      msg.textContent = body.message || 'Your account is not yet active on the beta.';
    } catch (e) { /* Clerk session present but bridge failed - fall back to email link */ }
  }

  initClerk();
})();
