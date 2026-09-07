# Launch checklist

Everything that could be automated, is. What remains here needs a human
because it needs an account, a credential, a DNS record or a judgement call.

`docs/DEPLOYMENT.md` is the how; this is the what-is-still-outstanding.

---

## A. Blocking - launch cannot happen without these

Only one file needs editing: **`.env` on the server**. Nothing else in the
repository requires changes to go live.

- [ ] **Pick the API hostname.** The marketing site keeps
      `ipointel.brandsap.com` (GitHub Pages). The backend needs its own, e.g.
      `api.ipointel.brandsap.com`.
- [ ] **Point an A record at the VPS**, and let it resolve *before* the first
      `docker compose up`. Caddy requests its certificate on boot and the ACME
      challenge fails if DNS is not live yet.
- [ ] **Provision the VPS**: Docker Engine + compose plugin, ports 80/443 open.
- [ ] **Fill in `.env`** (copy `.env.example`, or the existing
      `.env.production` renamed to `.env` - compose reads `.env` specifically):
      - `DOMAIN` - the API hostname above
      - `POSTGRES_PASSWORD` - a long random secret, new
      - `PUBLIC_BASE_URL` - `https://<that hostname>`; currently
        `http://localhost`, which would put dead links in every outbound email
      - `ADMIN_TOKEN` - already a real 32-char secret locally; keep it, do not
        reuse it elsewhere
- [ ] **Email**: either set `ENABLE_EMAIL=false` and launch without it, or
      supply SMTP relay credentials (`SMTP_HOST/PORT/TLS/USER/PASSWORD`) and an
      `EMAIL_FROM` on a domain the relay is authorised for. The app refuses to
      start with `ENABLE_EMAIL=true` pointed at the mailpit dev catcher, which
      is what the file currently says - that is deliberate, because the
      alternative is silently accepting signups and never sending the mail.
      **SPF and DKIM must be set for the sending domain** or the welcome mail
      lands in spam.

## B. Should do before real traffic

- [ ] **Verify a restore.** Take a backup from `./backups`, restore it into a
      scratch database, confirm the row counts. This is the only item on either
      page that no test can check for you, and an unverified backup is not a
      backup.
- [ ] **Decide on error reporting.** `SENTRY_DSN` blank means nothing is sent
      anywhere. Sentry's free tier is enough for this; paste the DSN to enable.
- [ ] **Review Terms and Privacy.** Both pages say, in their own text, that
      they need review by counsel before a commercial launch. This is a
      financial-research product in two jurisdictions.
- [ ] **Check the `data-state` snapshot.** The public site rebuilds from the
      SQLite snapshot on the `data-state` branch. Confirm the first production
      refresh succeeded rather than silently falling back to it.

## C. Known and deliberate - not bugs

- **The dashboard is gated.** Beta access is per-lead (`access_status`). A new
  signup lands as `WAITLISTED` and cannot open `/app`; promoting them to
  `INVITED` lets them in, and redeeming the sign-in link flips them to
  `ACTIVE`. `DISABLED` is refused outright. That is the designed funnel, not a
  broken login - expect "I signed up but cannot log in" reports otherwise.
- **Rate limits are per-process.** Bounded and safe, but they do not coordinate
  across processes. Do not run `--workers > 1` or scale `web` without moving
  them to a shared store first - it divides every limit by the process count.
- **Sitemap includes `/dashboard/`.** It resolves, so it is not a broken entry,
  but it is an app shell rather than content. Say the word and it comes out.
- **`/api/docs` is off in production** unless `ENABLE_API_DOCS=true`.
- **Pages deploys do not trigger on backend changes.** The workflow's path
  filter watches `app/static/**`, `scripts/build_pages.py` and its own file. A
  services-only change reaches the live site on the next 45-minute cron, or
  immediately via `gh workflow run pages.yml`.

## D. Deliberately not done

- **No Redis.** Nothing currently needs a shared cache or queue; the email
  queue and sheets outbox are database tables, which is one less service to
  run and back up. Add it when rate limits need to span processes.
- **No Prometheus/Grafana.** Structured JSON access logs with request IDs
  already go to stdout, which `docker compose logs` and any log shipper can
  read. A metrics stack is worth adding when there is someone to watch it.
- **No CDN in front of the API.** Caddy already does TLS and compression, and
  the static marketing site is on Pages, which is already a CDN.
