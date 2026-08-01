# findajob.pro — deploying the Django API next to the live static site

**VPS:** SSH host `macbook-air` (85.215.69.83, IONOS Frankfurt).
**Project root on the VPS:** `/root/findajob/`.
**Reverse proxy:** Traefik, separate stack, dynamic config in `/opt/gethub/traefik/dynamic/`,
external docker network `gethub_gethub-network`.

The governing rule for everything below is **UI-first**: `https://findajob.pro` must keep
serving the existing interface at every point in this procedure. The API is added *underneath*
it; nothing here takes the site down.

---

## 0. What is where

| Repo path | Path on the VPS | Notes |
|---|---|---|
| `deploy/docker-compose.yml` | `/root/findajob/docker-compose.yml` | **repo ROOT**, not `deploy/` — every relative path inside it is relative to the root |
| `deploy/nginx.conf` | `/root/findajob/deploy/nginx.conf` | bind-mounted into `findajob-web` |
| `deploy/traefik/findajob.yml` | `/opt/gethub/traefik/dynamic/findajob.yml` | Traefik hot-reloads this |
| `web/` | `/root/findajob/web/` | bind-mounted read-only into `findajob-web` |
| `backend/` | `/root/findajob/backend/` | the `api` image's build context |
| `backend/.env.example` | `/root/findajob/.env` | **one** env file, mode 0600, gitignored |

Containers: `findajob-web` (nginx), `findajob-api` (Django+gunicorn), `findajob-db`
(postgres:16-alpine), `findajob-redis` (redis:7-alpine).
Networks: `gethub_gethub-network` (external, shared with unrelated stacks) and
`findajob-internal` (`internal: true` — no route on or off the box).
Volumes: `findajob_pg_data`, `findajob_media_data`. Redis has **no** volume: cache only,
persistence disabled.

**No service publishes a port.** Never add a `ports:` mapping — Traefik reaches nginx
in-network and nginx reaches the API in-network.

---

## 1. The one environment file

Compose reads `/root/findajob/.env` **twice**: for `${VAR}` interpolation in the compose file
(the database and Redis passwords) and as the `api` service's `env_file`. One file, one place to
edit.

```bash
scp backend/.env.example macbook-air:/root/findajob/.env.new
ssh macbook-air
cd /root/findajob
# fill it in, then:
mv .env.new .env && chmod 600 .env
```

Values that must be set before the first `up`:

```bash
python3 -c "import secrets,string;a=string.ascii_letters+string.digits+'!@#%^&*(-_=+)';print(''.join(secrets.choice(a) for _ in range(64)))"  # SECRET_KEY
openssl rand -hex 32   # POSTGRES_PASSWORD
openssl rand -hex 32   # REDIS_PASSWORD   <- hex on purpose: it goes inside a URL
```

`ALLOWED_HOSTS=findajob.pro,www.findajob.pro` and
`CSRF_TRUSTED_ORIGINS=https://findajob.pro,https://www.findajob.pro` are **required** —
`config/settings/prod.py` raises `ImproperlyConfigured` without them, and so does a
`SECRET_KEY` left on the development default.

`REDIS_PASSWORD` and `POSTGRES_PASSWORD` use Compose's `${VAR:?…}` form: if either is missing,
`docker compose up` **fails immediately** rather than starting an unauthenticated cache.

---

## 2. First deployment

```bash
# 1. ship the code (web/ is already there; this adds backend/ and the deploy files)
rsync -az --delete \
  --exclude '.venv/' --exclude '__pycache__/' --exclude '*.pyc' \
  --exclude '.env' --exclude 'staticfiles/' --exclude 'media/' --exclude 'db.sqlite3' \
  backend/ macbook-air:/root/findajob/backend/
rsync -az deploy/nginx.conf   macbook-air:/root/findajob/deploy/nginx.conf
rsync -az deploy/docker-compose.yml macbook-air:/root/findajob/docker-compose.yml

ssh macbook-air
cd /root/findajob

# 2. build the API image (a build never touches the running site)
docker compose build api

# 3. start the data layer first and wait for it to be healthy
docker compose up -d db redis
docker compose ps            # both must read (healthy) before continuing

# 4. MIGRATE — an explicit step, see §3
docker compose run --rm -e DJANGO_COLLECTSTATIC=0 api python manage.py migrate

# 5. start the API
docker compose up -d api
docker compose logs -f api   # expect the gunicorn boot line, no tracebacks

# 6. LAST: recreate nginx so it picks up the new config with the /api/ proxy
docker compose up -d --force-recreate web
```

Step 6 is last on purpose. Until it runs, nginx serves exactly what it serves today, so an API
that fails to start cannot affect the live site.

### The bind-mount trap (this has cost real time before)

`deploy/nginx.conf` is a **single-file** bind mount. Docker binds it by inode; rsync writes a
temp file and renames it, so after an rsync the container is still serving the **old** config.
`nginx -t` and `nginx -s reload` both report success while changing nothing — that is the
deceptive part. **Always `docker compose up -d --force-recreate web` after touching
`nginx.conf`.** Directory mounts like `web/` are unaffected.

---

## 3. Migrations are a separate step. Always.

The image's entrypoint (`backend/docker/entrypoint.sh`) **never** runs `migrate`, and it must
stay that way. An entrypoint migration runs once per *container*, i.e. once per replica: a
routine `docker compose up -d --scale api=3` becomes three concurrent schema writers racing the
same lock. It also silently couples "restart the API" to "mutate the production schema", which
is the one operation that should require a human.

```bash
# always dry-run first
docker compose run --rm -e DJANGO_COLLECTSTATIC=0 api python manage.py migrate --plan
docker compose run --rm -e DJANGO_COLLECTSTATIC=0 api python manage.py migrate
```

`DJANGO_COLLECTSTATIC=0` skips the entrypoint's collectstatic, which is pure overhead for a
one-off command.

Any other management command runs the same way — e.g. the break-glass login code from spec §6:

```bash
docker compose run --rm -e DJANGO_COLLECTSTATIC=0 api python manage.py issue_login_code --email you@example.com
docker compose run --rm -e DJANGO_COLLECTSTATIC=0 api python manage.py createsuperuser
```

---

## 4. Routine changes — what to run for what

| What changed | Command |
|---|---|
| `web/**` (html/js/css/assets) | `rsync -az --delete web/ macbook-air:/root/findajob/web/` — nginx serves the bind mount, **no restart** |
| `backend/**/*.py` (no new dependency) | rsync `backend/`, then `docker compose restart api` |
| **`backend/requirements.txt`** | `docker compose build api && docker compose up -d api` — **a bare `restart` runs the OLD image** and 500s on the missing import |
| `backend/Dockerfile`, `backend/gunicorn.conf.py`, `backend/docker/*` | `docker compose build api && docker compose up -d api` |
| a new migration | §3, then `docker compose restart api` |
| `deploy/nginx.conf` | rsync, then `docker compose up -d --force-recreate web` (see the bind-mount trap) |
| `deploy/docker-compose.yml` | rsync to `/root/findajob/docker-compose.yml`, then `docker compose up -d` |
| `deploy/traefik/findajob.yml` | `scp` to `/opt/gethub/traefik/dynamic/findajob.yml` — Traefik hot-reloads, nothing to restart |
| `.env` | `docker compose up -d api` (env is read at container create, not at restart) |

**A new Python dependency means `docker compose build`, never a bare restart.**
`requirements.txt` is `COPY`ed before `pip install` in the Dockerfile, so only a rebuild
installs it. `docker compose restart api` re-runs the existing image and the container dies on
`ModuleNotFoundError`.

Do **not** touch the other stacks on this box (`djaingo-*`, `n8n-*`, `wp-*`, `rotocon-*`,
`georgeai`, `aiopify`).

---

## 5. Verification after any deploy

```bash
# --- the live site, first and always ---
curl -sI https://findajob.pro | head -1                      # 200
curl -s -o /dev/null -w '%{http_code}\n' https://findajob.pro/nope   # 404
curl -sI https://www.findajob.pro | head -1                  # 308
curl -sI https://findajob.pro/ | grep -i 'content-security\|x-frame'

# --- cache policy: HTML and the app-shell scripts must NOT be cached ---
for p in / /app.html /auth.html /api.js /support.js /legal.js; do
  printf '%-14s ' "$p"; curl -sI "https://findajob.pro$p" | grep -i '^cache-control'
done                                                          # all: no-cache
curl -sI https://findajob.pro/vendor/react.production.min.js | grep -i '^cache-control'
                                                              # max-age=3600 — fine, immutable

# --- the API ---
curl -s -o /dev/null -w '%{http_code}\n' https://findajob.pro/api/v1/me/     # 401 (anonymous)
curl -si https://findajob.pro/api/v1/auth/csrf/ | grep -i 'set-cookie'       # csrftoken

# --- containers ---
ssh macbook-air 'cd /root/findajob && docker compose ps'      # db + redis (healthy), api up

# --- Redis really is closed ---
ssh macbook-air 'docker compose -f /root/findajob/docker-compose.yml exec -T redis redis-cli ping'
#   -> NOAUTH Authentication required.        <- this is the PASS condition
ssh macbook-air 'docker port findajob-redis'  # empty: nothing published

# --- the API really does have internet egress (it is on an internal network too) ---
ssh macbook-air 'cd /root/findajob && docker compose exec -T api python -c "
import socket; socket.create_connection((\"smtp-relay.brevo.com\", 587), 5); print(\"egress ok\")"'
```

If that last check fails, the `internal: true` flag on the `findajob-internal` network has
captured the container's default route. The one-line fallback is to delete `internal: true`
from `deploy/docker-compose.yml`; Redis stays protected by the other three controls (no
published ports, `--requirepass`, no persistence).

`docker compose config` is worth running locally before shipping — it catches interpolation and
schema errors without touching the VPS. nginx config can be checked the same way:

```bash
docker run --rm -v "$PWD/deploy/nginx.conf:/etc/nginx/conf.d/default.conf:ro" nginx:alpine nginx -t
```

---

## 6. Known gap to close on the Django side — the client IP

`X-Forwarded-For` arrives as `"<client>, <traefik-ip>"` (Traefik sets the client, nginx appends
its own peer). Django does **not** read that header by default, so `request.META["REMOTE_ADDR"]`
is the **nginx container's** IP for every request.

That matters: spec §5's per-IP OTP budget (`otp:ip:{ip}`, 20/hour) and `EmailOTP.request_ip`
would otherwise collapse every visitor on the planet into one bucket — the limit would either
lock out the whole internet or be meaningless.

The fix belongs in `apps/common` (a small `client_ip(request)` helper), **not** here and **not**
in a blanket `SetRemoteAddrFromForwardedFor` middleware. There are exactly two trusted hops
(Traefik, nginx); take the entry at index `-2` of the parsed list, or the leftmost if the header
is shorter, and never trust a longer chain than that. `nginx.conf` already supplies both
`X-Forwarded-For` and `X-Real-IP` so either strategy works.

---

## 7. Logging, `/admin/`, and other deliberate omissions

- **`/admin/` is not proxied.** nginx forwards `/api/` only, so the Django admin is not reachable
  from the internet at all. That is the intent — it removes a whole class of exposure for a
  surface only the operator needs. Administration is done with
  `docker compose run --rm -e DJANGO_COLLECTSTATIC=0 api python manage.py <command>`. If a web
  admin is ever genuinely required, add a **separate** Traefik router on its own hostname with an
  IP-allowlist middleware — do not widen the nginx proxy.
- **`/media/` is not served by nginx either.** Candidate documents are personal data; they are
  delivered only through an owner-checked Django view. `MEDIA_ROOT` is the `findajob_media_data`
  volume, mode 0700.
- **`/static/`** (Django admin + DRF assets) is likewise not proxied. It only exists inside the
  api container for the admin, which is not exposed.
- **Logs** go to Docker's `json-file` driver, capped at 3 × 10 MB per container. gunicorn's
  access log includes the client IP (needed for abuse forensics) but never the query string —
  the format in `backend/gunicorn.conf.py` logs method and path separately on purpose, so a
  token or address that ever lands in a URL cannot be written to disk. That 30 MB cap is the
  de-facto retention window; if a longer one is ever configured, the privacy policy needs
  updating with it.

---

## 8. Rollback

```bash
ssh macbook-air
cd /root/findajob

# API only — the static site is untouched by this
docker compose stop api

# put nginx back to serving static-only (removes the /api/ proxy)
git -C /root/findajob checkout deploy/nginx.conf   # or restore your copy
docker compose up -d --force-recreate web
```

Rolling the **database** back is a restore, not a `down`: `docker compose down -v` would delete
`findajob_pg_data`. Never use `-v` on this stack. Existing backups live in `/root/backups/` on
the VPS and `~/Desktop/findajob-atomic-backup-2026-08-01/` locally.
