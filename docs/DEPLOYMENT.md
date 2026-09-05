# Production deployment (VPS + docker compose)

The stack is `docker-compose.production.yml`: Caddy (TLS) -> web (uvicorn) ->
Postgres, plus a worker and a one-shot migration job.

This is separate from the public marketing site. `ipointel.brandsap.com` is a
static GitHub Pages build (`.github/workflows/pages.yml`) and stays that way;
this stack is the API/dashboard backend and wants its **own hostname**, e.g.
`api.ipointel.brandsap.com`.

---

## 1. Prerequisites

- A VPS with Docker Engine + the compose plugin.
- Ports **80** and **443** open. Nothing else needs to be reachable - `web`
  and `db` publish no ports and are only reachable inside the compose network.
- A DNS **A record** for your API hostname pointing at the VPS, resolving
  *before* the first `up`. Caddy provisions its certificate on boot and will
  fail the ACME challenge if DNS is not live yet.

## 2. Environment file

Compose reads **one** file: `.env` in the project directory. It serves two
purposes at once, which is easy to get wrong:

- `${DOMAIN}` / `${POSTGRES_PASSWORD}` are interpolated by compose itself.
- `env_file: .env` passes the same file into the containers.

So on the VPS the file must be named `.env` - not `.env.production`. Copy your
local `.env.production` to `.env` on the server, then fix the values below.

`APP_ENV`, `DATABASE_URL` and `STRICT_RELIABILITY` are set in the compose
`environment:` block and **override** whatever the file says - leave them.

Values that must change from the current local file:

| Key | Current (dev) | Production |
|---|---|---|
| `PUBLIC_BASE_URL` | `http://localhost` | `https://api.your-domain.com` - used to build sign-in, unsubscribe and preference links in outbound email. Wrong value = dead links in real mail. |
| `EMAIL_PROVIDER` | `mailpit` | `smtp` |
| `SMTP_HOST` | `127.0.0.1` | your relay host (e.g. `smtp.mailgun.org`, `email-smtp.<region>.amazonaws.com`) |
| `SMTP_PORT` | `1025` | `587` for STARTTLS |
| `SMTP_TLS` | `false` | `true` |
| `SMTP_USER` / `SMTP_PASSWORD` | empty | relay credentials |
| `EMAIL_FROM` | - | a sender on a domain your relay is authorised for (SPF/DKIM), e.g. `IPO Intelligence <updates@your-domain.com>` |

New keys compose needs, which the local file does not have yet:

```dotenv
DOMAIN=api.your-domain.com
POSTGRES_PASSWORD=<generate a long random secret>
```

`ADMIN_TOKEN` is already a real 32-char secret in the local file - keep it,
and do not reuse it anywhere else.

**The app refuses to start if this is wrong.** `validate_production_settings`
fails fast under `APP_ENV=production` on: a SQLite `DATABASE_URL`, an unset
`PUBLIC_BASE_URL`, a placeholder/empty `ADMIN_TOKEN`, and - with
`ENABLE_EMAIL=true` - the mailpit dev catcher, a loopback `SMTP_HOST`,
resend/freeresend without credentials, an unknown provider, or no sender
address. That is deliberate: the alternative is queueing real signup mail into
a transport that silently drops it. To defer email entirely, set
`ENABLE_EMAIL=false` and none of the email keys are validated.

## 3. Deploy

```bash
git clone https://github.com/satyamamarpandey/ipointel.git && cd ipointel
# put your prepared .env in place here
docker compose -f docker-compose.production.yml up -d --build
```

Boot order is enforced by compose: `db` becomes healthy -> `migrate` runs
`alembic upgrade head` to completion -> `web` and `worker` start. Exactly one
process migrates, and neither serves traffic against a schema that is behind
the migration history. `app.db.init_db()` is a no-op under
`APP_ENV=production` for the same reason - **the schema comes only from
Alembic here**, so a new migration must be committed for any model change.

Check it came up:

```bash
docker compose -f docker-compose.production.yml ps          # migrate should be "exited (0)"
docker compose -f docker-compose.production.yml logs migrate
curl -fsS https://api.your-domain.com/health
```

## 4. Upgrades

```bash
git pull
docker compose -f docker-compose.production.yml up -d --build
```

`migrate` re-runs on every `up` and is a no-op when already at head.

## 5. Backups

`deploy/backup_postgres.sh` dumps the database and prunes dumps older than 14
days. Nothing schedules it - add a cron entry on the VPS:

```cron
17 3 * * * cd /path/to/ipointel && ./deploy/backup_postgres.sh >> /var/log/ipo-backup.log 2>&1
```

Restore:

```bash
gunzip -c backups/ipo_<stamp>.sql.gz | \
  docker compose -f docker-compose.production.yml exec -T db psql -U ipo ipo
```

Verify a restore into a scratch database at least once - an unverified backup
is not a backup.

## 6. Things that will bite you

- **Do not publish a port on `web`.** The Dockerfile runs uvicorn with
  `--forwarded-allow-ips "*"`, which is only safe because Caddy is the sole
  route to the app. Without that flag uvicorn ignores `X-Forwarded-For`
  (its default trusts only `127.0.0.1`, and Caddy's container address is
  not that), every request reports Caddy's IP, and all three rate limiters -
  sign-in 5/10min, signup 6/min, events 60/min - collapse into one bucket
  shared by every visitor. `tests/test_deploy_config.py` guards both halves.
- **Rate limits are per-process and in-memory.** They reset on restart and do
  not coordinate across replicas. Running uvicorn with `--workers > 1` or
  scaling `web` divides every limit by the number of processes; that needs a
  shared store (Redis) first.
- **`/api/docs` is public.** FastAPI's Swagger UI is served at `/api/docs`
  with no auth. Fine for a documented API, but it does enumerate every route -
  set `docs_url=None` in `app/main.py` if you would rather it not.
- **Secrets never enter the image.** `.dockerignore` excludes `.env*`, local
  databases and `backups/`; `COPY . .` would otherwise bake `ADMIN_TOKEN` and
  the Postgres password into a readable layer.
