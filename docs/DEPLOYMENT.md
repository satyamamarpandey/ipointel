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

Backups run inside the stack as the `backup` service - there is no crontab to
add, and a stack moved to a new machine keeps backing itself up from the first
`up`. It connects to Postgres as an ordinary client (no docker socket, no
privileges) and writes to `./backups` on the host. Daily, keeping 14 days;
both are tunable with `BACKUP_INTERVAL_SECONDS` and `BACKUP_KEEP_DAYS`.

```bash
docker compose -f docker-compose.production.yml logs backup   # confirm it is writing
ls -lh backups/
```

`deploy/backup_postgres.sh` still exists for an on-demand dump before a risky
migration.

Both write to `.partial` and rename only on success, and prune only after a
confirmed good dump - so an interrupted dump never lands under the real name,
and a run of failures can never age out the last known-good backup.

Restore:

```bash
gunzip -c backups/ipo_<stamp>.sql.gz \
  | docker compose -f docker-compose.production.yml exec -T db psql -U ipo ipo
```

**Verify a restore into a scratch database at least once.** An unverified
backup is not a backup - this is the one item on this page that cannot be
checked by a test.

## 6. Health endpoints

| Endpoint | Touches the DB | Use for |
|---|---|---|
| `/health` | yes | the compose healthcheck (unchanged) |
| `/health/live` | no | liveness - "is this process serving at all" |
| `/health/ready` | yes | readiness - "should this instance get traffic" |

Liveness deliberately ignores the database: a database blip must not convince
an orchestrator to restart web containers that are working and would recover
on their own.

## 7. Things that will bite you

- **Do not publish a port on `web`.** The Dockerfile runs uvicorn with
  `--forwarded-allow-ips "*"`, which is only safe because Caddy is the sole
  route to the app. Without that flag uvicorn ignores `X-Forwarded-For`
  (its default trusts only `127.0.0.1`, and Caddy's container address is
  not that), every request reports Caddy's IP, and all three rate limiters -
  sign-in 5/10min, signup 6/min, events 60/min - collapse into one bucket
  shared by every visitor. `tests/test_deploy_config.py` guards both halves.
- **Rate limits are per-process and in-memory.** Bounded now (expiry plus an
  LRU ceiling), so they cannot grow without limit, but they still reset on
  restart and do not coordinate across processes. Running uvicorn with
  `--workers > 1` or scaling `web` divides every limit by the number of
  processes; that needs a shared store (Redis) first.
- **The schema comes only from Alembic here.** `init_db()` is a no-op under
  `APP_ENV=production`, so any model change needs a committed migration or
  the column simply will not exist in production.
- **`/api/docs` is off in production** unless `ENABLE_API_DOCS=true`. It
  enumerates every route, admin included.
- **Secrets never enter the image.** `.dockerignore` excludes `.env*`, local
  databases and `backups/`; `COPY . .` would otherwise bake `ADMIN_TOKEN` and
  the Postgres password into a readable layer.
- **Container-executed scripts must stay LF.** `.gitattributes` pins this;
  a CRLF `deploy/*.sh` fails in Alpine with a bare `\r: not found`.

## 8. Deploying and rolling back

One command, from the repository root on the VPS:

```bash
./scripts/deploy.sh              # deploy origin/master
./scripts/deploy.sh <sha|tag>    # deploy a specific revision
```

It refuses to run on a dirty tree (the stack must match a revision that
exists on GitHub), takes a **pre-migration backup and aborts if that backup
fails**, runs migrations, restarts, and polls `/health`. If any step fails it
returns the checkout to the previous revision and rebuilds - a failed deploy
leaves the box on the last revision that worked, not on a half-built one.

Concurrent deploys are refused rather than interleaved (`scripts/_lock.sh`).
It prefers `flock` and falls back to an atomic `mkdir` lock where `flock` is
absent, reclaiming a lock older than an hour as abandoned. The two cases are
distinguished deliberately: a missing `flock` reported as "another deploy is
running" would be a misleading message on every single deploy.

Rolling back **code**:

```bash
./scripts/rollback.sh --list     # recent revisions
./scripts/rollback.sh <sha|tag>
```

### Migration rollback policy

`rollback.sh` never runs `alembic downgrade` and never touches the Postgres
volume. **Forward-fix is the default**: write a new migration that corrects
the problem and deploy forward.

The reason is that a downgrade is only as reversible as the migration was.
An additive migration (a new nullable column, a new table) downgrades
cleanly; one that drops or rewrites a column cannot restore the data it
destroyed, and running the downgrade is how you turn a bad deploy into
permanent data loss.

So:

- Rolling code back past an **additive** migration is safe - old code simply
  ignores the new column. `rollback.sh` detects that the target revision has
  fewer migrations, says so, and asks for confirmation.
- For a **destructive** migration, do not roll back. Fix forward, and if the
  data is already gone, restore from the pre-deploy backup that `deploy.sh`
  took before migrating.
- Every schema change is preceded by a backup because `deploy.sh` takes one
  and refuses to migrate without it.

## 9. Offsite backups

Backups written only to the VPS do not survive the VPS. `BACKUP_OFFSITE_CMD`
runs after each successful local backup, with the file path appended as the
final argument:

```dotenv
BACKUP_OFFSITE_CMD=rclone copy --config /config/rclone.conf
```

It is a command hook rather than a built-in S3 client on purpose: no vendor
lock-in, no extra dependency in the backup image, and it works with rclone,
`aws s3 cp`, `b2`, `rsync` or `scp` alike. A failing upload logs a loud
WARNING and never deletes or fails the local backup - but check for that
warning, because an offsite copy that has silently never run looks exactly
like one that works until the day you need it.

Recommended destination: **Cloudflare R2** (zero egress fees, S3-compatible,
10GB free) via rclone. Backblaze B2 is equally fine. Either needs an account,
so neither is configured here.

## 10. Uptime monitoring

Point a monitor at `https://api.<your-domain>/health` - it returns 200 with a
JSON body and touches the database, so it fails when the stack is genuinely
broken rather than only when the host is off.

Free options that need an account (so not configured here): UptimeRobot
(50 monitors, 5-minute interval), Better Stack, or a self-hosted Uptime Kuma
if you would rather not depend on a third party.
