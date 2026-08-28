# Security notes

- Do not commit `.env` or API keys.
- Rotate `ADMIN_TOKEN` and database credentials before deployment.
- Public waitlist writes are rate-limited and honeypot-protected; database uniqueness prevents duplicate emails.
- Admin refresh/export endpoints require `X-Admin-Token`.
- The application sets CSP, frame, MIME-sniffing, referrer and permissions headers; production Caddy adds HSTS.
- Keep dependencies patched and retain the GitHub Actions test gate.
- Email is optional and only sent to consented leads; unsubscribe is supported.
