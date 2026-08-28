# Production checklist

- [ ] Replace `ADMIN_TOKEN` and database password.
- [ ] Set a real SEC User-Agent with a monitored email address.
- [ ] Use HTTPS at the reverse proxy.
- [ ] Set `APP_ENV=production`, `STRICT_RELIABILITY=true`, and `ALLOW_SECONDARY_MARKET_DATA` according to your data policy.
- [ ] Configure database backups and retention.
- [ ] Configure monitoring for `/health` and worker/source-health failures.
- [ ] Configure Resend only after DNS/domain verification; otherwise leave email disabled.
- [ ] Review SEBI/NSE/BSE/secondary-source terms before enabling any additional scraper.
- [ ] Add legal/privacy/terms pages before public commercial launch.
- [ ] Backtest and calibrate on point-in-time historical data before marketing any probability as validated.
