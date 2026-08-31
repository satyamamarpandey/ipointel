(() => {
  'use strict';

  // Marks JS as available so CSS can gate the scroll-reveal animation behind
  // `html.js .reveal` (see styles.css) - done here, not an inline <script>,
  // because our CSP is script-src 'self' with no 'unsafe-inline'/nonce.
  document.documentElement.classList.add('js');

  // ---------- analytics (first-party, internal-only, best-effort) ----------
  function track(name) {
    try {
      const body = JSON.stringify({ name, path: location.pathname });
      if (navigator.sendBeacon) navigator.sendBeacon('/api/events', new Blob([body], { type: 'application/json' }));
      else fetch('/api/events', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body, keepalive: true }).catch(() => {});
    } catch (e) { /* analytics must never break the page */ }
  }
  track('landing_view');

  // ---------- session-aware nav (Sign in / Open dashboard) ----------
  fetch('/api/auth/me').then(r => r.json()).then(me => {
    if (me.authenticated) {
      const cta = document.getElementById('navCta');
      cta.textContent = 'Open dashboard'; cta.href = '/app';
      document.getElementById('navAuthLink').style.display = 'none';
    }
  }).catch(() => {});

  // ---------- mobile nav ----------
  const burger = document.getElementById('navBurger'), mobileNav = document.getElementById('mobileNav');
  if (burger) burger.addEventListener('click', () => {
    const open = mobileNav.classList.toggle('open');
    burger.setAttribute('aria-expanded', String(open));
  });

  // ---------- signup submit helper (shared by hero form + modal) ----------
  function utm(name) { return new URLSearchParams(location.search).get(name) || ''; }
  async function submitWaitlist(fields, msgEl, btn) {
    msgEl.textContent = 'Reserving your spot…';
    const body = {
      email: fields.email, name: fields.name || '', investor_type: fields.investor_type || 'retail',
      markets: fields.markets || 'both', consent: true, website: fields.website || '',
      source: utm('utm_source') || 'direct', referred_by: utm('ref') || '',
      campaign: utm('utm_campaign') || '', page_path: location.pathname,
    };
    try {
      const r = await fetch('/api/waitlist', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      const j = await r.json();
      if (!r.ok) throw new Error(j.detail || 'Signup failed');
      msgEl.textContent = j.message + (j.referral_code ? ` Referral code: ${j.referral_code}` : '');
      if (btn) btn.disabled = true;
      track('early_access_completed');
      try { localStorage.setItem('ipo_ea_joined', '1'); } catch (e) {}
      closeModal();
      return true;
    } catch (err) {
      msgEl.textContent = err.message || 'Could not reserve your spot.';
      return false;
    }
  }

  const heroForm = document.getElementById('waitlistForm');
  if (heroForm) {
    heroForm.addEventListener('submit', e => {
      e.preventDefault();
      track('early_access_started');
      submitWaitlist({
        email: heroForm.email.value, name: heroForm.name.value, investor_type: heroForm.investor_type.value,
        markets: heroForm.markets.value, website: heroForm.website.value,
      }, document.getElementById('message'), heroForm.querySelector('button'));
    });
  }

  // ---------- early-access modal ----------
  const overlay = document.getElementById('modalOverlay');
  const modalForm = document.getElementById('modalForm');
  const modalMsg = document.getElementById('modalMessage');
  let modalMarket = 'both', lastFocused = null;
  const DISMISS_DAYS = 4;

  function shouldOfferModal() {
    try {
      if (localStorage.getItem('ipo_ea_joined') === '1') return false;
      const dismissedAt = parseInt(localStorage.getItem('ipo_ea_dismissed_at') || '0', 10);
      if (dismissedAt && (Date.now() - dismissedAt) < DISMISS_DAYS * 86400000) return false;
    } catch (e) { /* localStorage unavailable - offer the modal, fail open */ }
    return true;
  }

  function openModal() {
    if (!overlay) return;
    lastFocused = document.activeElement;
    overlay.hidden = false;
    requestAnimationFrame(() => overlay.classList.add('open'));
    document.getElementById('modalEmail').focus();
    document.addEventListener('keydown', onModalKeydown);
    track('early_access_modal_shown');
  }
  function closeModal(remember) {
    if (!overlay || overlay.hidden) return;
    overlay.classList.remove('open');
    setTimeout(() => { overlay.hidden = true; }, 250);
    document.removeEventListener('keydown', onModalKeydown);
    if (remember) {
      try { localStorage.setItem('ipo_ea_dismissed_at', String(Date.now())); } catch (e) {}
      track('early_access_modal_dismissed');
    }
    if (lastFocused && lastFocused.focus) lastFocused.focus();
  }
  function onModalKeydown(e) {
    if (e.key === 'Escape') { closeModal(true); return; }
    if (e.key !== 'Tab') return;
    const focusables = overlay.querySelectorAll('button, input, [tabindex]:not([tabindex="-1"])');
    if (!focusables.length) return;
    const first = focusables[0], last = focusables[focusables.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  }
  if (overlay) {
    overlay.addEventListener('mousedown', e => { if (e.target === overlay) closeModal(true); });
    document.getElementById('modalClose').addEventListener('click', () => closeModal(true));
    document.querySelectorAll('.modalprefs button').forEach(b => b.addEventListener('click', () => {
      document.querySelectorAll('.modalprefs button').forEach(x => x.classList.remove('active'));
      b.classList.add('active'); modalMarket = b.dataset.val;
    }));
    modalForm.addEventListener('submit', e => {
      e.preventDefault();
      submitWaitlist({ email: modalForm.email.value, markets: modalMarket }, modalMsg, modalForm.querySelector('button'));
    });
    if (shouldOfferModal()) setTimeout(openModal, 3000);
  }

  // ---------- hero product preview + upcoming strip (real data, honestly labeled) ----------
  const REDUCE_MOTION = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const valuationTag = v => v || 'PENDING';
  const DIM_LABEL = { overall: 'Overall', listing: 'Listing', long_term: 'Long term' };
  let currentIpo = null, currentLive = false, currentDim = 'overall';

  function evidenceRows(ipo) {
    const rows = [];
    rows.push(ipo.valuation === 'FAIR' ? ['ok', '✓', 'Valuation in a reasonable range']
      : ipo.valuation ? ['watch', '△', `Valuation flagged: ${ipo.valuation.toLowerCase()}`]
      : ['watch', '△', 'Valuation: insufficient data']);
    rows.push(ipo.confidence >= 80 ? ['ok', '✓', 'High-confidence evidence base'] : ['watch', '△', 'Confidence still building']);
    rows.push((ipo.listing - ipo.long_term) > 10 ? ['watch', '△', 'Listing-driven; weaker long-term case']
      : ['ok', '✓', 'Listing and long-term signals aligned']);
    return rows;
  }

  // Small position-only settle when a number changes - never gates
  // visibility, so it stays safe under the same rule that governs .reveal.
  function swapNumber(el, text) {
    if (!el) return;
    if (REDUCE_MOTION) { el.textContent = text; return; }
    el.style.transition = 'opacity ' + getComputedStyle(document.documentElement).getPropertyValue('--motion-medium').trim();
    el.style.opacity = '0';
    setTimeout(() => { el.textContent = text; el.style.opacity = '1'; }, 140);
  }

  function renderDimension() {
    if (!currentIpo) return;
    const ipo = currentIpo;
    document.getElementById('pcDimLabel').textContent = DIM_LABEL[currentDim];
    swapNumber(document.getElementById('pcBigScore'), String(ipo[currentDim]));
    document.getElementById('pcEvidence').innerHTML = evidenceRows(ipo).map(([cls, icon, label]) =>
      `<div class="${cls}"><span class="mk">${icon}</span> ${label}</div>`).join('');
  }

  function renderPreview(ipo, live) {
    currentIpo = ipo; currentLive = live;
    document.getElementById('pcLabel').textContent = live ? 'Live coverage' : 'Illustrative analysis';
    document.getElementById('pcCompany').textContent = ipo.company;
    document.getElementById('pcTag').textContent = live ? ipo.country : 'SAMPLE';
    document.getElementById('pcOverall').textContent = ipo.overall;
    document.getElementById('pcListing').textContent = ipo.listing;
    document.getElementById('pcLongTerm').textContent = ipo.long_term;
    document.getElementById('pcConfidence').textContent = ipo.confidence + '%';
    document.getElementById('pcValuation').textContent = valuationTag(ipo.valuation);
    renderDimension();
  }

  document.querySelectorAll('.dimtab').forEach(tab => tab.addEventListener('click', () => {
    document.querySelectorAll('.dimtab').forEach(t => { t.classList.remove('active'); t.setAttribute('aria-selected', 'false'); });
    tab.classList.add('active'); tab.setAttribute('aria-selected', 'true');
    currentDim = tab.dataset.dim;
    renderDimension();
  }));

  function renderStrip(list) {
    const el = document.getElementById('ipoStrip');
    if (!list.length) { el.innerHTML = '<div class="stripempty">No open coverage right now — check back soon.</div>'; return; }
    el.innerHTML = list.map(ipo => `
      <div class="stripcard">
        <div class="country">${ipo.country} · ${ipo.status}</div>
        <h4>${ipo.company}</h4>
        <div class="striprow"><span>Overall</span><b>${ipo.overall}</b></div>
        <div class="striprow"><span>Listing / Long term</span><b>${ipo.listing} / ${ipo.long_term}</b></div>
        <div class="striprow"><span>Valuation</span><b>${valuationTag(ipo.valuation)}</b></div>
      </div>`).join('');
  }

  function renderTicker(list) {
    const track = document.getElementById('tickerTrack');
    if (!track) return;
    if (!list.length) { track.parentElement.hidden = true; return; }
    const items = list.map(ipo => `
      <div class="tick"><span class="cc">${ipo.country}</span><span class="co">${ipo.company}</span>
        <span class="lb">Overall</span><span class="sc">${ipo.overall}</span>
        <span class="lb">Confidence</span><span class="sc">${ipo.confidence}%</span></div>`).join('');
    // Duplicated once so the CSS keyframe (-50%) loops seamlessly.
    track.innerHTML = items + items;
  }

  fetch('/api/public/highlights').then(r => r.json()).then(data => {
    const list = data.ipos || [];
    renderStrip(list);
    renderTicker(list);
    if (list.length) {
      const best = list.slice().sort((a, b) => (b.confidence || 0) - (a.confidence || 0))[0];
      renderPreview(best, true);
    } else {
      renderPreview({ company: 'Example Manufacturing Ltd.', country: 'India', overall: 82, listing: 76, long_term: 88, confidence: 91, valuation: 'FAIR' }, false);
    }
  }).catch(() => {
    document.getElementById('ipoStrip').innerHTML = '<div class="stripempty">Coverage temporarily unavailable.</div>';
    const overlay = document.getElementById('tickerTrack');
    if (overlay) overlay.parentElement.hidden = true;
    renderPreview({ company: 'Example Manufacturing Ltd.', country: 'India', overall: 82, listing: 76, long_term: 88, confidence: 91, valuation: 'FAIR' }, false);
  });

  // ---------- scroll reveal ----------
  if ('IntersectionObserver' in window) {
    const io = new IntersectionObserver(entries => {
      entries.forEach(e => { if (e.isIntersecting) { e.target.classList.add('in'); io.unobserve(e.target); } });
    }, { threshold: 0.12 });
    document.querySelectorAll('.reveal').forEach(el => io.observe(el));
  } else {
    document.querySelectorAll('.reveal').forEach(el => el.classList.add('in'));
  }
})();
