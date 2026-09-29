// IPOIntel terminal. Runs unmodified in server mode (FastAPI /api/*) and in
// GitHub Pages mode (pages-adapter.js answers the same fetch() calls from
// static JSON). Nothing here recomputes a score; it only formats what the
// backend produced.
(() => {
  'use strict';
  const $ = s => document.querySelector(s), $$ = s => [...document.querySelectorAll(s)];
  const EM_DASH = String.fromCharCode(8212);
  // Legacy immutable score snapshots may still carry the old em-dash wording;
  // the product ships zero user-visible em dashes, so normalise at render time.
  const deDash = s => String(s ?? '').split(' ' + EM_DASH + ' ').join(': ').split(EM_DASH).join('-');
  const esc = s => deDash(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const fmt = (v, d = 1) => v == null || Number.isNaN(Number(v)) ? '–' : Number(v).toFixed(d);
  const sym = x => x.currency === 'INR' ? '₹' : '$';
  const money = x => {
    if (x.price_high == null && x.price_low == null && x.final_price == null) return '–';
    if (x.price_high == null && x.price_low == null) return `${sym(x)}${fmt(x.final_price, 2)}`;
    const lo = x.price_low ?? x.price_high, hi = x.price_high ?? x.price_low;
    return `${sym(x)}${fmt(lo, 0)}${lo !== hi ? '–' + fmt(hi, 0) : ''}`;
  };
  const cls = v => v == null ? 'neutral' : v >= 70 ? 'good' : v >= 55 ? 'warn' : 'bad';
  const parseDate = s => { const t = Date.parse(String(s || '').replace(/^(\d{4})(\d{2})(\d{2})$/, '$1-$2-$3')); return Number.isNaN(t) ? null : t; };
  const marketLabel = c => c === 'India' ? 'India' : 'U.S.';
  const STATUS_ORDER = { Open: 0, Closed: 1, Upcoming: 2, Priced: 3, Filed: 4, Listed: 5, Withdrawn: 6 };

  let ipos = [], selected = null, minConfidence = 70;

  async function json(url, opt) {
    const r = await fetch(url, opt);
    if (r.status === 401) { location.href = '/login?next=' + encodeURIComponent(location.pathname); throw new Error('Sign-in required'); }
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `HTTP ${r.status}`);
    return r.json();
  }
  function showNotice(t) { const n = $('#globalNotice'); n.textContent = deDash(t); n.style.display = 'block'; }
  function hideNotice() { $('#globalNotice').style.display = 'none'; }

  // ---------------------------------------------------------------- summary
  const PIPELINE_LABEL = { LIVE: 'Live data monitor', DELAYED: 'Data delayed', PARTIAL: 'Sources partial', STALE: 'Data stale', FAILED: 'Source failure' };
  const PIPELINE_COLOR = { LIVE: 'var(--good)', DELAYED: 'var(--warn)', PARTIAL: 'var(--warn)', STALE: 'var(--bad)', FAILED: 'var(--bad)' };
  async function loadSummary() {
    const s = await json('/api/summary');
    minConfidence = Number(s.min_confidence) || minConfidence;
    $('#mTotal').textContent = s.total; $('#mActive').textContent = s.active; $('#mListed').textContent = s.listed;
    $('#mConf').textContent = s.high_confidence_scores; $('#mGate').textContent = `${s.min_confidence}%`;
    if (s.pipeline_status) {
      $('#liveText').textContent = PIPELINE_LABEL[s.pipeline_status] || s.pipeline_status;
      $('#liveDot').style.background = PIPELINE_COLOR[s.pipeline_status] || 'var(--muted2)';
      $('#liveDot').parentElement.title = `Pipeline status derived from required sources: ${s.pipeline_status}`;
    }
    if (s.last_ingestion && s.last_ingestion.status === 'error') {
      showNotice(`Latest ${s.last_ingestion.source} refresh failed. Existing data remains visible; the Reliability tab shows details.`);
    }
  }

  // ------------------------------------------------------------------ radar
  const eventDate = x => parseDate(x.open_date) || parseDate(x.close_date) || parseDate(x.listing_date) || parseDate(x.filing_date) || 0;
  const changeDate = x => parseDate(x.score && x.score.created_at) || parseDate(x.updated_at) || 0;
  const SORTS = {
    updated: (a, b) => (parseDate(b.updated_at) || 0) - (parseDate(a.updated_at) || 0),
    date: (a, b) => (STATUS_ORDER[a.status] ?? 9) - (STATUS_ORDER[b.status] ?? 9) || eventDate(b) - eventDate(a),
    overall: (a, b) => (b.score?.overall ?? -1) - (a.score?.overall ?? -1),
    listing: (a, b) => (b.score?.listing ?? -1) - (a.score?.listing ?? -1),
    long: (a, b) => (b.score?.long_term ?? -1) - (a.score?.long_term ?? -1),
    confidence: (a, b) => (b.score?.confidence ?? -1) - (a.score?.confidence ?? -1),
    change: (a, b) => changeDate(b) - changeDate(a),
  };

  async function loadIPOs() {
    const c = $('#country').value, st = $('#status').value, q = encodeURIComponent($('#search').value.trim());
    $('#ipoRows').innerHTML = '<tr><td colspan="9"><div class="empty" role="status">Loading IPOs</div></td></tr>';
    try {
      ipos = await json(`/api/ipos?country=${encodeURIComponent(c)}&status=${encodeURIComponent(st)}&q=${q}&limit=500`);
    } catch (e) {
      ipos = [];
      $('#ipoRows').innerHTML = `<tr><td colspan="9"><div class="empty">Could not load IPOs: ${esc(e.message)}</div></td></tr>`;
      throw e;
    }
    populateSectors();
    renderRows();
    populateCompare();
    renderCalendar();
  }

  function visibleRows() {
    const board = $('#board').value, sector = $('#sector').value, conf = Number($('#confidence').value || 0);
    let a = ipos.slice();
    if (board !== 'all') a = a.filter(x => (x.board || 'Mainboard') === board);
    if (sector !== 'all') a = a.filter(x => (x.sector || 'Unknown') === sector);
    if (conf) a = a.filter(x => (x.score?.confidence ?? -1) >= conf);
    const sorter = SORTS[$('#sort').value] || SORTS.date;
    a.sort(sorter);
    return a;
  }

  function populateSectors() {
    const el = $('#sector'), old = el.value || 'all';
    const counts = {};
    ipos.forEach(x => { const s = x.sector || 'Unknown'; counts[s] = (counts[s] || 0) + 1; });
    const sectors = Object.keys(counts).filter(s => s !== 'Unknown').sort((a, b) => counts[b] - counts[a] || a.localeCompare(b)).slice(0, 40);
    el.innerHTML = ['<option value="all">All sectors</option>', ...sectors.map(s => `<option value="${esc(s)}">${esc(s)}</option>`)].join('');
    el.value = sectors.includes(old) ? old : 'all';
  }

  function stageBadge(x) {
    const s = x.status || '';
    const k = s === 'Open' ? 'good' : s === 'Closed' ? 'warn' : s === 'Withdrawn' ? 'bad' : 'neutral';
    return `<span class="tier ${k}">${esc(s)}</span>`;
  }

  function renderRows() {
    const a = visibleRows();
    $('#countPill').textContent = `${a.length} issue${a.length === 1 ? '' : 's'}`;
    if (!a.length) {
      $('#ipoRows').innerHTML = `<tr><td colspan="9"><div class="empty">${ipos.length ? 'No IPO matches the current filters.' : 'No IPOs are stored for this selection. The terminal never invents sample IPOs.'}</div></td></tr>`;
      return;
    }
    $('#ipoRows').innerHTML = a.map(x => `<tr data-id="${x.id}" data-status="${esc(x.status)}" tabindex="0" aria-selected="${selected && selected.id === x.id ? 'true' : 'false'}">
      <td><b>${esc(x.company)}</b><div class="kicker">${esc(x.symbol || (x.sector && x.sector !== 'Unknown' ? x.sector : x.exchange || ''))}${x.board && x.board !== 'Mainboard' ? ' · ' + esc(x.board) : ''}</div></td>
      <td>${marketLabel(x.country)}</td>
      <td>${stageBadge(x)}</td>
      <td>${money(x)}</td>
      <td class="score ${cls(x.score?.overall)}">${fmt(x.score?.overall, 0)}</td>
      <td class="${cls(x.score?.listing)}">${fmt(x.score?.listing, 0)}</td>
      <td class="${cls(x.score?.long_term)}">${fmt(x.score?.long_term, 0)}</td>
      <td class="${cls(x.score?.confidence)}">${fmt(x.score?.confidence, 0)}%</td>
      <td><span class="kicker">${esc(x.score?.recommendation || 'Pending')}</span></td>
    </tr>`).join('');
    $$('#ipoRows tr[data-id]').forEach(tr => {
      const open = () => openDetail(Number(tr.dataset.id));
      tr.addEventListener('click', open);
      tr.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); } });
    });
  }

  // ----------------------------------------------------------------- detail
  function bars(p) {
    return Object.entries(p || {}).map(([k, v]) => `<div class="barrow"><span>${esc(k)}</span><div class="bar"><i style="width:${Math.max(0, Math.min(100, v))}%"></i></div><b>${fmt(v, 0)}</b></div>`).join('');
  }
  const sevClass = sv => sv === 'CRITICAL' || sv === 'HIGH' ? 'bad' : sv === 'WATCH' ? 'warn' : 'neutral';
  function flagsBlock(rf) {
    const flags = (rf && rf.flags) || [];
    if (!flags.length) return '<p class="muted">No deterministic red flags triggered on currently disclosed fields.</p>';
    return flags.map(f => `<div class="evidence"><div><span class="tier ${sevClass(f.severity)}">${esc(f.severity)}</span> <b>${esc(f.title)}</b></div><div class="muted" style="font-size:11px">${esc(f.detail)}</div><div class="kicker">rule: ${esc(f.rule)}</div></div>`).join('');
  }
  function contradictionsBlock(items) {
    if (!items || !items.length) return '<p class="muted">No cross-source or cross-field inconsistencies detected in currently structured data.</p>';
    return items.map(c => `<div class="evidence"><div><b>${esc(c.summary)}</b></div><div class="muted" style="font-size:11px">A: ${esc(c.evidence_a.source)} = ${esc(c.evidence_a.value)} &nbsp;|&nbsp; B: ${esc(c.evidence_b.source)} = ${esc(c.evidence_b.value)}</div></div>`).join('');
  }
  function sensitivityBlock(sn) {
    if (!sn) return '';
    const up = (sn.upgrade_conditions || []).map(u => `<li><b>${esc(u.lever)}:</b> ${esc(u.detail)}</li>`).join('');
    const down = (sn.downgrade_conditions || []).map(d => `<li><b>${esc(d.lever)}:</b> ${esc(d.detail)}</li>`).join('');
    return `<p class="kicker">Could upgrade to a better verdict if</p>${up ? `<ul>${up}</ul>` : '<p class="muted">No single realistic lever upgrades this within bounds tested.</p>'}<p class="kicker">Could downgrade if</p>${down ? `<ul>${down}</ul>` : '<p class="muted">No single realistic lever downgrades this within bounds tested.</p>'}<p class="kicker">${esc(sn.note || '')}</p>`;
  }
  function confidenceBlock(x, s) {
    const reasons = (x.confidence_reasons || []).filter(r => !String(r).startsWith('ipo_classification'));
    const gated = /NO RECOMMENDATION/.test(s.recommendation || '');
    let h = `<div class="notice ${gated ? '' : 'ok'}" style="margin:0 0 12px"><b>${gated ? 'Recommendation withheld.' : 'Recommendation issued.'}</b> Confidence ${fmt(s.confidence, 0)}% against a ${minConfidence}% gate.</div>`;
    if (reasons.length) h += `<p class="kicker">Why confidence is ${gated ? 'below the gate' : 'not higher'}</p><ul>${reasons.map(r => `<li>${esc(r)}</li>`).join('')}</ul>`;
    else h += '<p class="muted">No missing or conflicting inputs recorded.</p>';
    return h;
  }
  function freshnessBlock(x, s) {
    const f = x.source_freshness || {};
    const at = f.latest_observation_at ? new Date(f.latest_observation_at) : null;
    const age = at ? Math.max(0, Math.round((Date.now() - at.getTime()) / 86400000)) : null;
    return `<div class="kv"><div><span>Latest source observation</span><b>${at ? at.toISOString().slice(0, 10) + (age != null ? ` (${age}d ago)` : '') : '–'}</b></div><div><span>Primary-source rows</span><b>${f.primary_source_rows ?? '–'} of ${f.provenance_rows ?? '–'}</b></div><div><span>Model version</span><b>${esc(s.model_version || '–')}</b></div><div><span>Scored at</span><b>${s.created_at ? String(s.created_at).slice(0, 16).replace('T', ' ') : '–'}</b></div></div>`;
  }

  async function openDetail(id) {
    $('#detail').innerHTML = '<div class="empty" role="status">Loading analysis</div>';
    try { selected = await json(`/api/ipos/${id}`); } catch (e) { $('#detail').innerHTML = `<div class="empty">Could not load this IPO: ${esc(e.message)}</div>`; return; }
    $$('#ipoRows tr[data-id]').forEach(tr => tr.setAttribute('aria-selected', String(Number(tr.dataset.id) === id)));
    const x = selected, s = x.score || {}, prov = x.provenance || [];
    const dates = [['Filed', x.filing_date], ['Opens', x.open_date], ['Closes', x.close_date], ['Lists', x.listing_date]].filter(([, v]) => v).map(([k, v]) => `<div><span>${k}</span><b>${esc(v)}</b></div>`).join('');
    $('#detail').innerHTML = `<div class="kicker">${esc(x.country)} · ${esc(x.status)} · ${esc(x.symbol || 'No ticker yet')}${x.board && x.board !== 'Mainboard' ? ' · ' + esc(x.board) : ''}${x.page_url ? ` · <a class="permalink" href="${esc(x.page_url)}">Open full page</a>` : ''}</div><h2>${esc(x.company)}</h2>
<div class="scorehero"><div class="ring" style="--p:${s.overall || 0}"><b>${fmt(s.overall, 0)}</b></div><div><div class="kicker">Overall / 100</div><b>${esc(s.recommendation || 'Pending score')}</b><div class="muted" style="font-size:12px;margin-top:5px">${esc(s.horizon || '')}</div><div style="margin-top:9px"><span class="pill">Listing score ${fmt(s.listing, 0)}</span> <span class="pill">Long-term score ${fmt(s.long_term, 0)}</span></div><div class="kicker" style="margin-top:7px">Scores are heuristic and not calibrated probabilities. See Model performance.</div></div></div>
<div class="kv"><div><span>Valuation</span><b>${esc(s.valuation || '–')}</b></div><div><span>Confidence</span><b class="${cls(s.confidence)}">${fmt(s.confidence, 0)}%</b></div><div><span>Price band</span><b>${money(x)}</b></div><div><span>Fair range</span><b>${s.fair_low ? `${sym(x)}${fmt(s.fair_low, 0)}–${fmt(s.fair_high, 0)}` : '–'}</b></div>${dates}</div>
<h3>Confidence</h3>${confidenceBlock(x, s)}
<h3>Score decomposition</h3>${bars(s.pillars)}
<h3>Why it scores this way</h3>${(s.rationale || []).length ? `<ul>${s.rationale.map(r => `<li>${esc(r)}</li>`).join('')}</ul>` : '<p class="muted">No strong positive evidence has cleared the configured thresholds yet.</p>'}
<h3>Risks</h3>${(s.risks || []).length ? `<ul>${s.risks.map(r => `<li>${esc(r)}</li>`).join('')}</ul>` : '<p class="muted">No model-level red flags recorded.</p>'}
<h3>Red flags <span class="pill">${x.red_flags ? x.red_flags.summary.total : 0}</span></h3>${flagsBlock(x.red_flags)}
<h3>Potential disclosure inconsistencies</h3>${contradictionsBlock(x.contradictions)}
<h3>What would change my mind</h3>${sensitivityBlock(x.sensitivity)}
<h3>Dig deeper</h3><div class="toolbar"><button class="btn ghost" type="button" data-lazy="dcf">Reverse DCF &amp; valuation</button><button class="btn ghost" type="button" data-lazy="similar">Similar historical IPOs</button><button class="btn ghost" type="button" data-lazy="changes">Score history &amp; attribution</button></div><div id="lazyPane" aria-live="polite"></div>
<h3>Source freshness</h3>${freshnessBlock(x, s)}
<h3>Evidence stack</h3>${prov.length ? prov.slice(0, 24).map(p => `<div class="evidence"><div><span class="tier">Tier ${p.tier}</span> <b>${esc(p.field)}</b>${p.conflict ? ' <span class="tier bad">CONFLICT</span>' : ''}</div><div class="muted" style="font-size:11px">${esc(p.source)} · ${esc(p.value)} · ${esc(String(p.observed_at || '').slice(0, 10))}</div>${p.url ? `<a class="kicker" href="${esc(p.url)}" target="_blank" rel="noopener noreferrer">Open source ↗</a>` : ''}</div>`).join('') : '<p class="muted">No field-level provenance stored yet.</p>'}`;
    $$('#detail [data-lazy]').forEach(b => b.addEventListener('click', () => loadLazyPane(id, b.dataset.lazy)));
    if (window.matchMedia('(max-width: 1080px)').matches) $('#detail').scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  async function loadLazyPane(id, kind) {
    const el = $('#lazyPane');
    el.innerHTML = '<div class="empty">Loading</div>';
    try {
      if (kind === 'dcf') el.innerHTML = dcfBlock(await json(`/api/ipos/${id}/valuation`));
      else if (kind === 'similar') el.innerHTML = similarBlock(await json(`/api/ipos/${id}/similar`));
      else if (kind === 'changes') el.innerHTML = changesBlock(await json(`/api/ipos/${id}/changes`));
    } catch (e) { el.innerHTML = `<p class="muted">Could not load: ${esc(e.message)}</p>`; }
  }
  function dcfBlock(v) {
    const sc = v.scenario_dcf, rv = v.reverse_dcf; let h = '';
    if (rv && rv.available) {
      const gapCls = rv.expectations_gap === 'EXTREME' || rv.expectations_gap === 'HIGH' ? 'bad' : rv.expectations_gap === 'MODERATE' ? 'warn' : 'neutral';
      h += `<div class="evidence"><div><b>Reverse DCF</b> <span class="tier ${gapCls}">${esc(rv.expectations_gap)} expectations gap</span></div><p style="font-size:12px">${esc(rv.narrative)}</p>
      <div class="kv"><div><span>IPO-implied revenue CAGR</span><b>${fmt(rv.implied_revenue_cagr_pct, 1)}%</b></div><div><span>Company historical growth</span><b>${rv.company_historical_growth_pct == null ? 'not disclosed' : fmt(rv.company_historical_growth_pct, 1) + '%'}</b></div><div><span>Peer growth anchor</span><b>${rv.peer_growth_anchor_pct == null ? 'no peer set' : fmt(rv.peer_growth_anchor_pct, 1) + '%'}</b></div><div><span>Margin path</span><b>${fmt(rv.assumed_margin_start_pct, 0)}% to ${fmt(rv.assumed_margin_end_pct, 0)}%</b></div><div><span>WACC</span><b>${fmt(rv.wacc_pct, 1)}%</b></div><div><span>Terminal growth</span><b>${fmt(rv.terminal_growth_pct, 1)}%</b></div></div>
      ${(rv.expectations_gap_reasons || []).length ? `<ul>${rv.expectations_gap_reasons.map(r => `<li>${esc(r)}</li>`).join('')}</ul>` : ''}</div>`;
    } else h += `<p class="muted">Reverse DCF unavailable: ${esc((rv && rv.reason) || 'insufficient data')}</p>`;
    if (sc && sc.available) {
      h += '<div class="kv">' + ['bear', 'base', 'bull'].map(k => { const sd = sc.scenarios[k]; return sd && sd.available ? `<div><span>${k[0].toUpperCase() + k.slice(1)} case</span><b>${sd.fair_value_per_share != null ? fmt(sd.fair_value_per_share, 2) : '–'} (${sd.upside_vs_ipo_price_pct != null ? fmt(sd.upside_vs_ipo_price_pct, 0) + '%' : '–'})</b></div>` : ''; }).join('') + '</div>';
    } else h += `<p class="muted">Scenario DCF unavailable: ${esc((sc && sc.reason) || 'insufficient data')}</p>`;
    return h;
  }
  function similarBlock(r) {
    if (!r.available) return `<p class="muted">${esc(r.reason)}</p>`;
    let h = `<p class="kicker">Match quality: ${esc(r.match_quality)} · features used: ${esc((r.features_used || []).join(', '))}</p>`;
    if (r.aggregate) h += `<div class="kv"><div><span>Median listing return (n=${r.aggregate.n_with_return})</span><b>${fmt(r.aggregate.median_listing_return_pct, 1)}%</b></div><div><span>Success rate</span><b>${fmt(r.aggregate.success_rate_pct, 0)}%</b></div></div>`;
    h += (r.matches || []).map(m => `<div class="evidence"><div><b>${esc(m.company)}</b> ${m.symbol ? '(' + esc(m.symbol) + ')' : ''}</div><div class="muted" style="font-size:11px">${esc((m.matched_on || []).join(', '))} · listing return: ${m.listing_return_pct != null ? fmt(m.listing_return_pct, 1) + '%' : '–'}</div></div>`).join('');
    if (!(r.matches || []).length) h += '<p class="muted">Similarity too weak to show comparable listings.</p>';
    return h;
  }
  function changesBlock(r) {
    if (!r.timeline || r.timeline.length < 2) return '<p class="muted">Only one score snapshot recorded so far, so there is no change to attribute yet.</p>';
    return r.timeline.slice().reverse().map(t => `<div class="evidence"><div><b>${esc(String(t.at).slice(0, 16).replace('T', ' '))}</b>, overall ${fmt(t.overall, 0)} ${t.delta_overall != null ? `(${t.delta_overall >= 0 ? '+' : ''}${t.delta_overall})` : ''}</div>${t.recommendation_change ? `<div class="muted" style="font-size:11px">${esc(t.recommendation_change)}</div>` : ''}${Array.isArray(t.drivers) && t.drivers.length && t.drivers[0].pillar ? `<ul>${t.drivers.map(d => `<li>${esc(d.pillar)}: ${d.delta >= 0 ? '+' : ''}${d.delta}</li>`).join('')}</ul>` : (typeof (t.drivers || [])[0] === 'string' ? `<p class="muted" style="font-size:11px">${esc(t.drivers[0])}</p>` : '')}</div>`).join('');
  }

  // ---------------------------------------------------------------- history
  async function loadPerformance() {
    const country = $('#country').value;
    const rows = await json(`/api/performance?country=${encodeURIComponent(country)}&limit=3000`);
    $('#perfCount').textContent = `${rows.length} listed`;
    $('#perfRows').innerHTML = rows.length ? rows.map(x => `<tr><td><b>${esc(x.company)}</b><div class="kicker">${esc(x.listing_date || '')}${x.board && x.board !== 'Mainboard' ? ' · ' + esc(x.board) : ''}</div></td><td>${marketLabel(x.country)}</td><td>${x.final_price != null ? fmt(x.final_price, 2) : x.price_high != null ? fmt(x.price_high, 2) : '–'}</td><td>${x.performance?.close != null ? fmt(x.performance.close, 2) : '–'}</td><td class="${x.performance?.listing_return_pct == null ? 'neutral' : x.performance.listing_return_pct >= 0 ? 'good' : 'bad'}">${x.performance?.listing_return_pct == null ? '–' : fmt(x.performance.listing_return_pct, 1) + '%'}</td><td>${fmt(x.score?.overall, 0)}</td><td>${fmt(x.score?.confidence, 0)}%</td></tr>`).join('') : '<tr><td colspan="7"><div class="empty">No listed IPO in the current window for this selection.</div></td></tr>';
    drawReturns(rows.map(x => x.performance?.listing_return_pct).filter(v => v != null));
  }
  function drawReturns(vals) {
    const el = $('#returnChart');
    if (!vals.length) { el.innerHTML = '<div class="empty">No realized listing returns yet for this selection.</div>'; return; }
    const min = Math.min(-50, ...vals), max = Math.max(50, ...vals), w = 600, h = 200, p = 25;
    const x = v => p + (v - min) / (max - min) * (w - 2 * p), y0 = 100;
    el.innerHTML = `<svg viewBox="0 0 ${w} ${h}" role="img" aria-label="Listing return distribution for ${vals.length} IPOs"><line x1="${p}" y1="${y0}" x2="${w - p}" y2="${y0}" stroke="#22323f"/><line x1="${x(0)}" y1="20" x2="${x(0)}" y2="180" stroke="#6b7f8f" stroke-dasharray="3 4"/>${vals.slice(0, 240).map((v, i) => `<circle cx="${x(v)}" cy="${60 + (i % 6) * 16}" r="4" fill="${v >= 0 ? '#72e6c2' : '#e2828a'}" opacity=".7"/>`).join('')}<text x="${p}" y="190" fill="#93a4b2" font-size="11">${fmt(min, 0)}%</text><text x="${w - p - 25}" y="190" fill="#93a4b2" font-size="11">${fmt(max, 0)}%</text></svg><p class="kicker" style="margin:8px 0 0">${vals.length} realized listing returns, measured against the issue price.</p>`;
  }

  // ------------------------------------------------------------ reliability
  async function loadSources() {
    const a = await json('/api/source-health');
    const badge = s => {
      const st = s.public_status || s.status;
      if (st === 'LIVE' || st === 'ok') return { c: 'var(--good)', l: 'LIVE' };
      if (st === 'PARTIAL' || st === 'partial') return { c: 'var(--warn)', l: 'PARTIAL' };
      if (st === 'DELAYED') return { c: 'var(--warn)', l: 'DELAYED' };
      if (st === 'STALE') return { c: 'var(--bad)', l: 'STALE' };
      if (st === 'OPTIONAL_UNCONFIGURED' || (st === 'never run' && s.tier === 3)) return { c: 'var(--muted2)', l: 'OPTIONAL · NOT CONFIGURED' };
      if (st === 'DISABLED') return { c: 'var(--muted2)', l: 'DISABLED' };
      return { c: 'var(--bad)', l: String(st || 'FAILED').toUpperCase() };
    };
    $('#sourceHealth').innerHTML = a.map(s => { const b = badge(s); return `<div class="sourceitem"><div><b>${esc(s.source)}</b><div class="kicker">Tier ${s.tier} · ${s.rows} rows last run${s.last_run ? ' · ' + esc(String(s.last_run).slice(0, 16).replace('T', ' ')) + ' UTC' : ''}${s.required === false ? ' · optional' : ''}</div>${s.error && s.status !== 'never run' ? `<div class="bad" style="font-size:11px;max-width:480px">${esc(s.error)}</div>` : ''}</div><span class="pill"><span class="dot" style="background:${b.c}"></span>${esc(b.l)}</span></div>`; }).join('');
  }

  // ----------------------------------------------------------- track record
  async function loadBacktest() {
    const b = await json('/api/backtest');
    $('#backtest').innerHTML = `<div class="metricgrid" style="grid-template-columns:1fr 1fr"><div class="metric"><span>Realized samples</span><b>${b.sample_size}</b></div><div class="metric"><span>Brier score</span><b>${b.brier_score ?? '–'}</b></div></div><p><b>Status:</b> ${esc(b.status)}</p><p class="muted">Top-decile average listing return: ${b.top_decile_avg_listing_return_pct == null ? '–' : fmt(b.top_decile_avg_listing_return_pct, 1) + '%'}</p><p class="kicker">Historical backtest: retrospective scoring of already-listed IPOs. Kept separate from the live forward record below.</p>`;
  }
  // Outcome cell for one forward prediction. Grading categories come from the
  // backend ledger (services.forward_grading); an older JSON without
  // `grading` falls back to the listed / not-listed wording.
  function outcomeCell(p) {
    const g = p.grading || null;
    const ret = p.outcome && p.outcome.listing_close_return_pct != null ? p.outcome.listing_close_return_pct : null;
    if (!g) return p.outcome_known ? (ret != null ? fmt(ret, 1) + '%' : 'listed, price data pending') : 'not yet listed';
    const reason = esc(g.reason || '');
    switch (g.category) {
      case 'GRADED': return ret != null ? `<b class="${ret >= 0 ? 'good' : 'bad'}">${ret >= 0 ? '+' : ''}${fmt(ret, 1)}%</b>` : 'graded';
      case 'PENDING': return 'listed, grading pending';
      case 'NOT_YET_ELIGIBLE': return 'not yet listed';
      case 'INVALID_FORWARD_RECORD': return `invalid: ${reason}`;
      default: return String(g.category || '').startsWith('BLOCKED') ? `blocked: ${reason}` : esc(g.category || '');
    }
  }
  async function loadTrackRecord() {
    const t = await json('/api/track-record');
    const rows = t.predictions.slice(0, 40).map(p => `<tr><td><b>${esc(p.company)}</b><div class="kicker">${esc(p.model_version)} · ${esc(p.feature_schema_version || '')}</div></td><td>${marketLabel(p.country)}</td><td>${esc(p.event_stage)}</td><td>${new Date(p.predicted_at).toISOString().slice(0, 10)}</td><td>${p.overall_score}</td><td>${fmt(p.confidence, 0)}%</td><td>${esc(p.recommendation)}</td><td>${outcomeCell(p)}</td></tr>`).join('');
    const L = t.ledger && t.ledger.by_category ? t.ledger : null;
    let metrics;
    if (L) {
      const c = L.by_category, n = k => c[k] || 0;
      const blocked = n('BLOCKED_IDENTITY') + n('BLOCKED_OFFER_PRICE') + n('BLOCKED_MARKET_DATA');
      const gradable = L.gradable != null ? L.gradable : n('GRADED') + n('PENDING') + blocked;
      const graded = L.graded != null ? L.graded : n('GRADED');
      metrics = [['Forward predictions recorded', t.total_forward_predictions], ['Gradable (listed)', gradable], ['Graded with outcome', graded], ['Pending', n('PENDING')], ['Blocked', blocked], ['Not yet eligible', n('NOT_YET_ELIGIBLE')], ['Invalid records', n('INVALID_FORWARD_RECORD')]];
    } else {
      metrics = [['Forward predictions recorded', t.total_forward_predictions], ['Graded with a realized outcome', t.graded_with_outcome]];
    }
    const grid = `<div class="metricgrid" style="grid-template-columns:repeat(auto-fit,minmax(150px,1fr))">${metrics.map(([k, v]) => `<div class="metric"><span>${esc(k)}</span><b>${v}</b></div>`).join('')}</div>`;
    $('#forwardrecord').innerHTML = `<p class="muted">${esc(t.note)}</p>${grid}${t.predictions.length ? `<div class="tablewrap"><table class="table"><thead><tr><th scope="col">Company</th><th scope="col">Market</th><th scope="col">Trigger</th><th scope="col">Predicted</th><th scope="col">Overall</th><th scope="col">Confidence</th><th scope="col">Recommendation</th><th scope="col">Outcome</th></tr></thead><tbody>${rows}</tbody></table></div>` : '<p class="empty">No forward predictions recorded yet.</p>'}`;
  }

  // ------------------------------------------------------ compare / calendar
  function populateCompare() {
    const opts = ['<option value="">Select IPO</option>', ...ipos.slice().sort((a, b) => a.company.localeCompare(b.company)).map(x => `<option value="${x.id}">${esc(x.company)}${x.symbol ? ' (' + esc(x.symbol) + ')' : ''}</option>`)].join('');
    ['cmp1', 'cmp2', 'cmp3'].forEach((id, i) => { const el = $('#' + id), old = el.value; el.innerHTML = opts; if (old && ipos.some(x => String(x.id) === old)) el.value = old; else if (ipos[i]) el.value = ipos[i].id; });
  }
  function renderCalendar() {
    const ev = [];
    ipos.forEach(x => { [['Filing', x.filing_date], ['Opens', x.open_date], ['Closes', x.close_date], ['Lists', x.listing_date]].forEach(([kind, d]) => { if (d) ev.push({ kind, date: d, ts: parseDate(d) || 9e15, company: x.company, country: x.country, status: x.status }); }); });
    ev.sort((a, b) => b.ts - a.ts);
    $('#calendarEvents').innerHTML = ev.length ? ev.slice(0, 160).map(e => `<div class="sourceitem"><div><b>${esc(e.company)}</b><div class="kicker">${esc(e.country)} · ${esc(e.status)}</div></div><div style="text-align:right"><b>${esc(e.kind)}</b><div class="kicker">${esc(e.date)}</div></div></div>`).join('') : '<div class="empty">No dated IPO events stored for this selection.</div>';
  }
  async function renderCompare() {
    const ids = ['cmp1', 'cmp2', 'cmp3'].map(id => Number($('#' + id).value)).filter(Boolean);
    const unique = [...new Set(ids)];
    if (!unique.length) { $('#compareGrid').innerHTML = '<div class="empty">Choose at least one IPO.</div>'; return; }
    $('#compareGrid').innerHTML = '<div class="empty" role="status">Loading comparison</div>';
    let rows;
    try { rows = await Promise.all(unique.map(id => json(`/api/ipos/${id}`))); } catch (e) { $('#compareGrid').innerHTML = `<div class="empty">Could not load comparison: ${esc(e.message)}</div>`; return; }
    const m = v => v == null ? '–' : fmt(v, 0) + 'm';
    $('#compareGrid').innerHTML = rows.map(x => { const s = x.score || {}, f = x.fundamentals || {}; return `<div class="card"><div class="kicker">${esc(x.country)} · ${esc(x.status)}${x.board && x.board !== 'Mainboard' ? ' · ' + esc(x.board) : ''}</div><h3>${esc(x.company)}</h3><div class="kv"><div><span>Overall</span><b class="${cls(s.overall)}">${fmt(s.overall, 0)}</b></div><div><span>Confidence</span><b>${fmt(s.confidence, 0)}%</b></div><div><span>Listing</span><b>${fmt(s.listing, 0)}</b></div><div><span>Long term</span><b>${fmt(s.long_term, 0)}</b></div><div><span>Valuation</span><b>${esc(s.valuation || '–')}</b></div><div><span>Price band</span><b>${money(x)}</b></div><div><span>Revenue</span><b>${m(f.revenue_m)}</b></div><div><span>Net income</span><b>${m(f.net_income_m)}</b></div><div><span>Cash flow</span><b>${m(f.cfo_m)}</b></div><div><span>Verdict</span><b style="font-size:12px">${esc(s.recommendation || 'Pending')}</b></div></div><div style="margin-top:12px">${bars(s.pillars)}</div></div>`; }).join('');
  }

  // ------------------------------------------------------ model performance
  function bandTable(bands) {
    if (!bands || !bands.length) return '<p class="muted">No band data.</p>';
    return `<div class="tablewrap"><table class="table"><thead><tr><th scope="col">Band</th><th scope="col">n</th><th scope="col">Positive %</th><th scope="col">Mean return</th><th scope="col">Median</th><th scope="col">&gt;10%</th><th scope="col">&gt;20%</th><th scope="col">Loss %</th></tr></thead><tbody>${bands.map(b => b.n ? `<tr><td>${esc(b.band)}</td><td>${b.n}</td><td>${fmt(b.positive_pct, 0)}%</td><td>${fmt(b.mean_return_pct, 1)}%</td><td>${fmt(b.median_return_pct, 1)}%</td><td>${fmt(b.gt10pct_pct, 0)}%</td><td>${fmt(b.gt20pct_pct, 0)}%</td><td>${fmt(b.loss_pct, 0)}%</td></tr>` : `<tr><td>${esc(b.band)}</td><td colspan="7" class="muted">no realized sample</td></tr>`).join('')}</tbody></table></div>`;
  }
  function modelBlock(title, m) {
    let h = `<h3>${esc(title)} <span class="pill">${esc(m.status === 'evaluated' ? 'evaluated' : 'insufficient sample')}</span></h3>`;
    if (m.status === 'evaluated') {
      h += `<div class="kv"><div><span>Sample</span><b>${m.sample_size}</b></div><div><span>AUC</span><b>${m.auc ?? '–'}</b></div><div><span>Brier</span><b>${m.brier_score ?? '–'}</b></div><div><span>Log loss</span><b>${m.log_loss ?? '–'}</b></div><div><span>Spearman (score vs return)</span><b>${m.spearman_score_vs_return ?? '–'}</b></div><div><span>Actual positive rate</span><b>${fmt(m.positive_rate_actual_pct, 0)}%</b></div></div>`;
      if (m.auc == null || m.auc <= 0.55) h += '<p class="muted">No discriminating power measured yet; scores are shown for transparency only.</p>';
      if (m.calibration && m.calibration.length) h += `<p class="kicker">Calibration</p><div class="tablewrap"><table class="table"><thead><tr><th scope="col">Predicted</th><th scope="col">n</th><th scope="col">Avg predicted</th><th scope="col">Actual positive</th></tr></thead><tbody>${m.calibration.map(c => `<tr><td>${esc(c.predicted_range)}</td><td>${c.n}</td><td>${fmt(c.avg_predicted_pct, 1)}%</td><td>${fmt(c.actual_positive_pct, 1)}%</td></tr>`).join('')}</tbody></table></div>`;
    } else h += `<p class="muted">${esc(m.status)}</p>`;
    h += bandTable(m.band_breakdown);
    return h;
  }
  function semanticsLine(ps) {
    if (!ps || !ps.listing) return '';
    const one = (label, s) => `${label}: ${s.calibrated ? 'calibrated probability' : 'score, not a probability'}${s.walk_forward_auc != null ? ` (walk-forward AUC ${fmt(s.walk_forward_auc, 2)}, n ${s.walk_forward_n})` : ''}`;
    return `<p class="kicker">${esc(one('Listing output', ps.listing))}. ${esc(one('Long-term output', ps.long_term))}.</p>`;
  }
  function researchBlock(res) {
    if (!res || !res.targets) return '';
    let h = '<h3>Walk-forward research (out of sample by listing year)</h3>';
    h += `<p class="kicker">Ridge logistic on features available at or before listing, trained only on earlier years. Baselines are scored on the same test rows. Coverage: ${esc(Object.entries(res.feature_coverage_pct || {}).filter(([, v]) => v != null && v > 0).map(([k, v]) => `${k} ${v}%`).join(', ') || 'no features populated')}.</p>`;
    for (const [target, t] of Object.entries(res.targets)) {
      const oos = t.out_of_sample || {};
      if (!oos.n) { h += `<p class="muted">${esc(target)}: no out-of-sample predictions yet (insufficient training history).</p>`; continue; }
      const rows = [['Walk-forward model', oos]].concat(Object.entries(t.baselines || {}).map(([k, v]) => [k.replace(/_/g, ' '), v]));
      h += `<p class="kicker">${esc(target === '12m' ? 'Long-term (12m) target' : 'Listing target')}: ${oos.n} out-of-sample rows, positive rate ${fmt(oos.positive_rate_pct, 1)}%. Release gate: <b>${t.release_gate && t.release_gate.passed ? 'passed' : 'not met'}</b>.</p>`;
      h += `<div class="tablewrap"><table class="table"><thead><tr><th scope="col">Predictor</th><th scope="col">AUC</th><th scope="col">PR-AUC</th><th scope="col">Brier</th><th scope="col">Log loss</th></tr></thead><tbody>${rows.filter(([, v]) => v && v.n).map(([k, v]) => `<tr><td>${esc(k)}</td><td>${fmt(v.auc, 3)}</td><td>${v.pr_auc == null ? 'n/a' : fmt(v.pr_auc, 3)}</td><td>${fmt(v.brier, 4)}</td><td>${fmt(v.log_loss, 4)}</td></tr>`).join('')}</tbody></table></div>`;
    }
    return h;
  }
  async function loadModelPerf() {
    if ($('#modelPerf').dataset.loaded) return;
    const r = await json('/api/model-performance');
    let h = '<p class="kicker">Historical backtest by market. Fixed-weight model, evaluated on the earliest recorded score per IPO. Statistics are withheld below the minimum sample.</p>';
    for (const country of Object.keys(r)) {
      const c = r[country];
      if (!c || typeof c !== 'object' || !('listing_model' in c)) continue;
      h += `<h2>${esc(country)} <span class="kicker">${c.total_listed_with_score} listed IPOs with a recorded score</span></h2>`;
      h += semanticsLine(c.probability_semantics);
      h += modelBlock('Listing model', c.listing_model);
      h += modelBlock('Long-term (12m) model', c.long_term_model);
      h += researchBlock(c.research);
    }
    $('#modelPerf').innerHTML = h;
    $('#modelPerf').dataset.loaded = '1';
  }

  // ------------------------------------------------------------------- tabs
  const tabs = $$('.tab');
  function activateTab(b, focus) {
    tabs.forEach(x => { x.classList.remove('active'); x.setAttribute('aria-selected', 'false'); x.setAttribute('tabindex', '-1'); });
    b.classList.add('active'); b.setAttribute('aria-selected', 'true'); b.setAttribute('tabindex', '0');
    if (focus) b.focus();
    $$('.view').forEach(x => { x.classList.remove('active'); x.hidden = true; });
    const view = $('#' + b.dataset.view); view.classList.add('active'); view.hidden = false;
    if (b.dataset.view === 'modelperf') loadModelPerf().catch(e => showNotice(`Model performance unavailable: ${e.message}`));
    if (b.dataset.view === 'history' && !$('#perfRows').children.length) loadPerformance().catch(e => showNotice(`History unavailable: ${e.message}`));
    try { history.replaceState(null, '', '#' + b.dataset.view); } catch (e) { /* ignore */ }
  }
  tabs.forEach((b, i) => {
    b.id = b.id || `tab-${b.dataset.view}`;
    const view = $('#' + b.dataset.view);
    view.setAttribute('role', 'tabpanel'); view.setAttribute('aria-labelledby', b.id); view.hidden = !b.classList.contains('active');
    b.setAttribute('aria-controls', b.dataset.view);
    b.setAttribute('tabindex', b.classList.contains('active') ? '0' : '-1');
    b.addEventListener('click', () => activateTab(b));
    b.addEventListener('keydown', e => {
      const n = tabs.length;
      if (e.key === 'ArrowRight') { e.preventDefault(); activateTab(tabs[(i + 1) % n], true); }
      else if (e.key === 'ArrowLeft') { e.preventDefault(); activateTab(tabs[(i - 1 + n) % n], true); }
      else if (e.key === 'Home') { e.preventDefault(); activateTab(tabs[0], true); }
      else if (e.key === 'End') { e.preventDefault(); activateTab(tabs[n - 1], true); }
    });
  });
  const initial = tabs.find(b => '#' + b.dataset.view === location.hash);
  if (initial) activateTab(initial);

  // ---------------------------------------------------------------- wiring
  async function loadAll() {
    hideNotice();
    const results = await Promise.allSettled([loadSummary(), loadIPOs(), loadSources(), loadBacktest(), loadTrackRecord()]);
    if ($('#history').classList.contains('active')) await loadPerformance().catch(() => {});
    const failed = results.filter(r => r.status === 'rejected');
    if (failed.length) { showNotice(`Some panels could not refresh: ${failed.map(f => f.reason && f.reason.message).join('; ')}`); if (!$('#liveDot').style.background) $('#liveText').textContent = 'Using last loaded data'; }
  }
  ['country', 'status'].forEach(id => $('#' + id).addEventListener('change', () => { loadIPOs().catch(e => showNotice(e.message)); if ($('#history').classList.contains('active')) loadPerformance().catch(() => {}); }));
  ['board', 'sector', 'confidence', 'sort'].forEach(id => $('#' + id).addEventListener('change', renderRows));
  let t; $('#search').addEventListener('input', () => { clearTimeout(t); t = setTimeout(() => loadIPOs().catch(e => showNotice(e.message)), 250); });
  $('#refreshBtn').addEventListener('click', loadAll);
  $('#compareBtn').addEventListener('click', renderCompare);
  try {
    const es = new EventSource('/api/events');
    es.addEventListener('data', () => loadAll());
    es.onerror = () => { if (!window.PAGES_MODE) $('#liveText').textContent = 'Polling fallback active'; };
  } catch (e) { /* no live stream available */ }
  if (!window.PAGES_MODE) setInterval(loadAll, 60000);
  loadAll();
})();
