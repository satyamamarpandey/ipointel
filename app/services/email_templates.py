from __future__ import annotations
"""Plain functions returning (subject, html, text). No templating engine -
these are short and don't need one, and it keeps the render path auditable:
nothing here can execute user-supplied code, only string interpolation of
values already validated/escaped by the caller."""
import html as _html

BRAND = "#0c1722"
ACCENT = "#1a5c46"

def esc(s) -> str:
    return _html.escape(str(s if s is not None else ""), quote=True)

def _shell(preheader: str, body_html: str, unsubscribe_url: str, prefs_url: str) -> str:
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f4f6f8;font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;">
<div style="display:none;max-height:0;overflow:hidden;">{esc(preheader)}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f6f8;padding:24px 0;">
<tr><td align="center">
<table role="presentation" width="100%" style="max-width:560px;background:#ffffff;border-radius:12px;overflow:hidden;border:1px solid #e3e8ec;">
<tr><td style="background:{BRAND};padding:20px 28px;">
<span style="color:#eef4f8;font-size:16px;font-weight:700;letter-spacing:-0.02em;">IPO Intelligence Terminal</span>
</td></tr>
<tr><td style="padding:28px;color:#1a2733;font-size:14px;line-height:1.55;">
{body_html}
</td></tr>
<tr><td style="padding:18px 28px;background:#f8f9fb;border-top:1px solid #e3e8ec;color:#7b8a97;font-size:11px;line-height:1.6;">
Research and decision-support software only. No IPO outcome, listing gain or return is guaranteed. This is not personalized investment advice.<br>
<a href="{esc(prefs_url)}" style="color:#7b8a97;">Manage email preferences</a> &nbsp;·&nbsp;
<a href="{esc(unsubscribe_url)}" style="color:#7b8a97;">Unsubscribe</a>
</td></tr>
</table>
</td></tr>
</table>
</body></html>"""

def _urls(base_url: str, token: str) -> tuple[str, str]:
    return f"{base_url}/unsubscribe?token={token}", f"{base_url}/preferences?token={token}"

def welcome_email(base_url: str, name: str, referral_code: str, markets: str, token: str) -> tuple[str, str, str]:
    unsub, prefs = _urls(base_url, token)
    market_label = {"india": "India", "us": "United States", "both": "India + United States"}.get(markets, "India + United States")
    greeting = f"Hi {esc(name)}," if name else "Hi,"
    body = f"""<p style="margin:0 0 14px;">{greeting}</p>
<p style="margin:0 0 14px;">You're on the early-access list for the <b>IPO Intelligence Terminal</b> — an evidence-first India + U.S. IPO research platform. Your focus: <b>{esc(market_label)}</b>.</p>
<p style="margin:0 0 14px;">Every IPO score comes with field-level source provenance, a confidence gate that refuses to recommend on thin evidence, and separate listing-gain vs long-term signals. We'll email you when something material changes on an IPO you'd care about — not on every tick.</p>
<p style="margin:0 0 14px;">Your referral code: <b style="font-family:monospace;background:#f1f4f6;padding:2px 6px;border-radius:4px;">{esc(referral_code)}</b></p>
<p style="margin:0 0 22px;"><a href="{esc(base_url)}/app" style="display:inline-block;background:{ACCENT};color:#ffffff;text-decoration:none;padding:11px 20px;border-radius:8px;font-weight:600;">Open the live dashboard</a></p>
<p style="margin:0;color:#5b6b78;font-size:12px;">No system can promise IPO returns. We measure and publish our own model's track record rather than claim certainty — see the Model Performance tab.</p>"""
    text = (f"{greeting}\n\nYou're on the early-access list for the IPO Intelligence Terminal ({market_label}).\n"
            f"Referral code: {referral_code}\nDashboard: {base_url}/app\n\n"
            f"Manage preferences: {prefs}\nUnsubscribe: {unsub}\n\n"
            "Research software only. No IPO outcome is guaranteed. Not personalized investment advice.")
    return "You're on the IPO Intelligence early-access list", _shell("Early access confirmed - here's what happens next.", body, unsub, prefs), text

def login_link_email(base_url: str, login_url: str, token: str) -> tuple[str, str, str]:
    unsub, prefs = _urls(base_url, token)
    body = f"""<p style="margin:0 0 14px;">Click below to sign in to the IPO Intelligence Terminal. This link works once and expires in 15 minutes.</p>
<p style="margin:0 0 22px;"><a href="{esc(login_url)}" style="display:inline-block;background:{ACCENT};color:#ffffff;text-decoration:none;padding:11px 20px;border-radius:8px;font-weight:600;">Sign in</a></p>
<p style="margin:0;color:#5b6b78;font-size:12px;">If you didn't request this, you can ignore this email — no account action will be taken.</p>"""
    text = f"Sign in to IPO Intelligence Terminal: {login_url}\n\nThis link works once and expires in 15 minutes. If you didn't request this, ignore this email."
    return "Your IPO Intelligence sign-in link", _shell("Your one-time sign-in link (expires in 15 minutes).", body, unsub, prefs), text

def _pill(label: str, value: str) -> str:
    return f'<div style="display:inline-block;background:#f1f4f6;border-radius:6px;padding:6px 10px;margin:0 6px 6px 0;font-size:12px;"><span style="color:#7b8a97;">{esc(label)}</span> <b>{esc(value)}</b></div>'

def alert_email(base_url: str, kind: str, company: str, country: str, prev, cur, reasons: list[str], risks: list[str], token: str) -> tuple[str, str, str]:
    """kind: 'recommendation' | 'score' | 'valuation' | 'red_flag'"""
    unsub, prefs = _urls(base_url, token)
    ipo_id = cur.get("ipo_id")
    if kind == "recommendation":
        headline = f'{esc(prev.get("recommendation") or "—")} → {esc(cur["recommendation"])}'
        subject = f"{company}: recommendation changed to {cur['recommendation']}"
    elif kind == "valuation":
        headline = f'{esc(prev.get("valuation") or "—")} → {esc(cur["valuation"])}'
        subject = f"{company}: valuation now {cur['valuation']}"
    elif kind == "red_flag":
        headline = "New critical red flag detected"
        subject = f"{company}: critical red flag"
    else:
        delta = cur["overall"] - (prev.get("overall") or cur["overall"])
        headline = f'{prev.get("overall", cur["overall"]):.0f} → {cur["overall"]:.0f} ({delta:+.0f})'
        subject = f"IPO score update: {company}"
    pills = "".join([
        _pill("Overall", f"{cur['overall']:.0f}/100"), _pill("Listing", f"{cur['listing']:.0f}/100"),
        _pill("Long term", f"{cur['long_term']:.0f}/100"), _pill("Confidence", f"{cur['confidence']:.0f}%"),
    ])
    reasons_html = "".join(f'<li style="margin:4px 0;">{esc(r)}</li>' for r in reasons) or '<li style="color:#7b8a97;">No individual driver crossed the reporting threshold.</li>'
    risks_html = "".join(f'<li style="margin:4px 0;">{esc(r)}</li>' for r in risks) or '<li style="color:#7b8a97;">No elevated risk flags recorded.</li>'
    body = f"""<p style="margin:0 0 6px;color:#7b8a97;font-size:12px;text-transform:uppercase;letter-spacing:.04em;">{esc(country)}</p>
<h2 style="margin:0 0 6px;font-size:19px;">{esc(company)}</h2>
<p style="margin:0 0 16px;font-size:16px;font-weight:700;color:{ACCENT};">{headline}</p>
<div style="margin:0 0 16px;">{pills}</div>
<p style="margin:0 0 4px;font-weight:700;font-size:13px;">Why it changed</p>
<ul style="margin:0 0 16px;padding-left:18px;">{reasons_html}</ul>
<p style="margin:0 0 4px;font-weight:700;font-size:13px;">Most important risks</p>
<ul style="margin:0 0 20px;padding-left:18px;">{risks_html}</ul>
<p style="margin:0;"><a href="{esc(base_url)}/app#ipo-{esc(ipo_id)}" style="display:inline-block;background:{ACCENT};color:#ffffff;text-decoration:none;padding:11px 20px;border-radius:8px;font-weight:600;">View full analysis</a></p>"""
    text = (f"{company} ({country})\n{headline}\n\n"
            f"Overall {cur['overall']:.0f}/100 | Listing {cur['listing']:.0f}/100 | Long term {cur['long_term']:.0f}/100 | Confidence {cur['confidence']:.0f}%\n\n"
            "Why it changed:\n" + "\n".join(f"- {r}" for r in reasons) + "\n\nRisks:\n" + "\n".join(f"- {r}" for r in risks) +
            f"\n\nFull analysis: {base_url}/app\nPreferences: {prefs}\nUnsubscribe: {unsub}")
    return subject, _shell(headline, body, unsub, prefs), text

def digest_email(base_url: str, india_rows: list[dict], us_rows: list[dict], changes: list[dict], new_filings: list[dict], red_flags: list[dict], token: str) -> tuple[str, str, str]:
    unsub, prefs = _urls(base_url, token)
    def row_block(rows):
        if not rows:
            return '<p style="color:#7b8a97;margin:0 0 12px;">Nothing cleared the bar this week.</p>'
        items = "".join(
            f'<div style="padding:8px 0;border-bottom:1px solid #eef1f3;"><b>{esc(r["company"])}</b> — overall {r["overall"]:.0f}, '
            f'listing {r["listing"]:.0f}, long term {r["long_term"]:.0f}, {esc(r["valuation"])}, confidence {r["confidence"]:.0f}%</div>'
            for r in rows[:5])
        return f'<div style="margin:0 0 16px;">{items}</div>'
    def line_list(rows, key="company"):
        if not rows:
            return '<p style="color:#7b8a97;margin:0 0 12px;">None this week.</p>'
        return '<ul style="margin:0 0 16px;padding-left:18px;">' + "".join(f'<li style="margin:3px 0;">{esc(r.get(key, r))}</li>' for r in rows[:6]) + "</ul>"
    body = f"""<h2 style="margin:0 0 14px;">Weekly IPO digest</h2>
<p style="margin:0 0 4px;font-weight:700;font-size:13px;">Best opportunities — India</p>{row_block(india_rows)}
<p style="margin:0 0 4px;font-weight:700;font-size:13px;">Best opportunities — United States</p>{row_block(us_rows)}
<p style="margin:0 0 4px;font-weight:700;font-size:13px;">Biggest score changes this week</p>{line_list(changes, "label")}
<p style="margin:0 0 4px;font-weight:700;font-size:13px;">New filings</p>{line_list(new_filings)}
<p style="margin:0 0 4px;font-weight:700;font-size:13px;">Critical red flags detected</p>{line_list(red_flags, "label")}
<p style="margin:16px 0 0;"><a href="{esc(base_url)}/app" style="display:inline-block;background:{ACCENT};color:#ffffff;text-decoration:none;padding:11px 20px;border-radius:8px;font-weight:600;">Open dashboard</a></p>"""
    text = "Weekly IPO digest\n\nOpen the dashboard for full detail: " + base_url + "/app\nPreferences: " + prefs + "\nUnsubscribe: " + unsub
    return "Your weekly IPO digest", _shell("This week's IPO scores, changes and filings.", body, unsub, prefs), text
