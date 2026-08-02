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
| — (never in the repo) | `/opt/gethub/traefik/dynamic/findajob-admin.htpasswd` | basicAuth credential for `/admin`, created by hand, mode 0600 — see §2a |

Containers: `findajob-web` (nginx), `findajob-api` (Django+gunicorn), `findajob-db`
(postgres:16-alpine), `findajob-redis` (redis:7-alpine).
Networks: `gethub_gethub-network` (external, shared with unrelated stacks) and
`findajob-internal` (`internal: true` — no route on or off the box).
Volumes: `findajob_pg_data`, `findajob_media_data`, `findajob_static_data`. Redis has **no**
volume: cache only, persistence disabled.

`findajob_static_data` is the only one that is disposable — it is `collectstatic` output and
the api entrypoint rewrites it on every start. `pg_data` and `media_data` are not.

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

# 4b. SEED THE LEGAL DOCUMENTS — required, and idempotent.
# Registration consent resolves the current German AGB/Datenschutz server-side
# and fails CLOSED (503) if they are missing, so an unseeded database means
# nobody can complete a first login. Re-run it after every legal.js change.
docker compose run --rm -e DJANGO_COLLECTSTATIC=0 api python manage.py seed_legal

# 5. start the API. Its entrypoint runs collectstatic, which is also what fills
#    the shared `static_data` volume that nginx serves as /static/ (§2a).
docker compose up -d api
docker compose logs -f api   # expect the collectstatic + gunicorn boot lines, no tracebacks

# 6. LAST: recreate nginx so it picks up the new config with the /api/ and
#    /admin/ proxies and the /static/ mount
docker compose up -d --force-recreate web
```

Step 6 is last on purpose. Until it runs, nginx serves exactly what it serves today, so an API
that fails to start cannot affect the live site. The same ordering matters for `/static/`: run
step 5 first and the volume is already populated by the time nginx starts pointing at it.

Exposing the Django admin on top of this is §2a — it is a separate, opt-in step and the site
works fine without it.

### The bind-mount trap (this has cost real time before)

`deploy/nginx.conf` is a **single-file** bind mount. Docker binds it by inode; rsync writes a
temp file and renames it, so after an rsync the container is still serving the **old** config.
`nginx -t` and `nginx -s reload` both report success while changing nothing — that is the
deceptive part. **Always `docker compose up -d --force-recreate web` after touching
`nginx.conf`.** Directory mounts like `web/` are unaffected.

---

## 2a. Exposing `/admin/` — two independent gates

`https://findajob.pro/admin/` is reachable, behind **two gates that do not share a failure
mode**:

1. **Traefik basicAuth** — a second router, `findajob-admin`, matching
   `Host(findajob.pro) && (PathPrefix(/admin/) || Path(/admin))` at `priority: 100` against the
   main router's `priority: 10`. An anonymous request is answered `401` by Traefik and never
   reaches nginx, Django or PostgreSQL.
2. **Django's own superuser login** — `is_staff` + `is_active`, session cookie, CSRF-protected.

Neither is a substitute for the other. basicAuth keeps the internet (and every scanner) away
from the login form; the Django login is the one that always applies, including to anything
already inside the shared `gethub_gethub-network`, which can reach `findajob-web:80` directly
and skip Traefik entirely — exactly as it already can for `/api/`.

Two supporting pieces make the page usable:

- nginx proxies `/admin/` to `findajob-api` with the same lazy-resolver pattern as `/api/`, so
  a missing or rebuilding API container is a `502` on `/admin/` and never a config that stops
  nginx from booting.
- nginx serves `/static/` (Django admin + DRF assets) from the **shared named volume**
  `findajob_static_data`, which the api entrypoint's `collectstatic` writes on every start.
  Without it the admin renders as unstyled HTML.

`/media/` is **not** exposed and must not be. Candidate documents stay behind an owner-checked
Django view.

### Step 1 — create the credential on the VPS (never in the repo)

Do not invent a password, and do not commit one. `htpasswd` is already installed at
`/usr/bin/htpasswd`.

```bash
ssh macbook-air

# a password worth using
openssl rand -base64 24

# -B = bcrypt.  -c CREATES the file (omit it to append a second user).
htpasswd -B -c /opt/gethub/traefik/dynamic/findajob-admin.htpasswd findajob-admin
chmod 600 /opt/gethub/traefik/dynamic/findajob-admin.htpasswd

# non-interactive equivalent, if a script needs it:
#   htpasswd -nbB findajob-admin '<password>' >> /opt/gethub/traefik/dynamic/findajob-admin.htpasswd
```

**Why `usersFile` and not inline `users`.** `deploy/traefik/findajob.yml` is tracked in git and
deployed by copying that one file to the VPS. An inline `users:` list would put a bcrypt hash
of the admin password into the repository and onto GitHub — offline-crackable, and impossible
to rotate without a commit. A `usersFile` keeps the credential on the VPS only: the deploy
copies the `.yml` and nothing else, so **the `.htpasswd` survives every deploy untouched**, and
rotating it is one command with no code change. It can also be `chmod 600`, which a config
value cannot. And it sidesteps the `$` / `$$` escaping trap that bites people who copy an
inline-users snippet written for docker-compose labels.

It lives in `/opt/gethub/traefik/dynamic/` because that directory is the only host path
bind-mounted into the Traefik container (`→ /etc/traefik/dynamic`, read-only). Traefik's file
provider parses **only** `.toml` / `.yml` / `.yaml`, so a `.htpasswd` sitting next to the route
files is ignored as configuration — verified: that directory already holds `.bak.<timestamp>`
files for the same reason.

### Step 2 — ship the four changed files

```bash
# from the repo root, locally
rsync -az deploy/nginx.conf         macbook-air:/root/findajob/deploy/nginx.conf
rsync -az deploy/docker-compose.yml macbook-air:/root/findajob/docker-compose.yml
rsync -az --delete --exclude '.venv/' --exclude '__pycache__/' --exclude '*.pyc' \
  --exclude '.env' --exclude 'staticfiles/' --exclude 'media/' --exclude 'db.sqlite3' \
  backend/ macbook-air:/root/findajob/backend/
scp deploy/traefik/findajob.yml     macbook-air:/opt/gethub/traefik/dynamic/findajob.yml
```

### Step 3 — apply, in this order

```bash
ssh macbook-air
cd /root/findajob

# a. api first: `up -d` (not `restart`) — the static_data volume is a new mount,
#    and a mount change requires the container to be RECREATED. `restart` keeps
#    the old container with the old mounts and the volume stays empty.
docker compose up -d api
docker compose logs --tail=20 api          # expect the collectstatic + gunicorn lines

# b. prove the permissions fix landed before wiring nginx to it
docker compose exec -T api ls -ld /app/staticfiles/admin
#    -> drwxr-xr-x ... findajob findajob     (NOT drwx------)
docker compose exec -T api ls -l /app/staticfiles/admin/css/base.css
#    -> -rw-r--r-- ... findajob findajob     (NOT -rw-------)

# c. nginx last — single-file bind mount, so force-recreate (see §2's trap)
docker compose up -d --force-recreate web

# d. a superuser to log in as, if there is not one yet
docker compose run --rm -e DJANGO_COLLECTSTATIC=0 api python manage.py createsuperuser
```

Traefik needs nothing restarted: `watch: true` on the file provider picks the route up within a
second or two.

### The permission trap this had to fix first

`config/settings/base.py` sets `FILE_UPLOAD_PERMISSIONS = 0o600` and
`FILE_UPLOAD_DIRECTORY_PERMISSIONS = 0o700` — correct for uploaded candidate documents, and
those are also the values a `FileSystemStorage` falls back to when it is constructed without
explicit modes. `StaticFilesStorage` *is* a `FileSystemStorage`, so `collectstatic` was writing
`STATIC_ROOT` as `drwx------` / `-rw-------`, owned by uid 10001. nginx's workers run as uid
101 in a different container: every `/static/` request would have been a **403**, and the admin
would have rendered as unstyled HTML with no obvious cause.

The fix is `STORAGES["staticfiles"]["OPTIONS"]` in `config/settings/base.py` **and** in
`config/settings/prod.py` (prod replaces the whole `staticfiles` entry, so it does not inherit
them), pinning `file_permissions_mode = 0o644` / `directory_permissions_mode = 0o755`. Django
passes `OPTIONS` straight to the storage class as keyword arguments. If `/static/` ever starts
403ing, this is the first thing to check.

### The Django side — what was already right, what changed, what was declined

**Already correct, verified against `/root/findajob/.env` on 2026-08-01 — nothing to change:**

| Setting | Value | Why it already covers `/admin/` |
|---|---|---|
| `ALLOWED_HOSTS` | `findajob.pro,www.findajob.pro` | host-based, not path-based, so it covers every route on the host at once |
| `CSRF_TRUSTED_ORIGINS` | `https://findajob.pro,https://www.findajob.pro` | the admin login POST is CSRF-checked against the browser's `Origin`; Traefik `passHostHeader: true` + nginx `proxy_set_header Host $host` mean Django compares the real `https://findajob.pro` |
| `SESSION_COOKIE_SECURE` | `True` (`prod.py`) | the admin session cookie is the same `sessionid` cookie the app uses |
| `CSRF_COOKIE_SECURE` | `True` (`prod.py`) | — |
| `SESSION_COOKIE_HTTPONLY` / `SAMESITE` | `True` / `Lax` | blocks JS read and cross-site POST into the admin |
| `SECURE_REFERRER_POLICY` | `same-origin` | admin URLs carry object ids; they never leave in a `Referer` |
| `X_FRAME_OPTIONS` + nginx `X-Frame-Options` + CSP `frame-ancestors 'none'` | `DENY` | the admin cannot be framed, three ways |
| `manage.py check --deploy` | clean (`0 issues`) | re-run it after any settings change |

`CSRF_COOKIE_HTTPONLY` stays **False**, and that is not an admin regression: `web/api.js` has
to read the `csrftoken` cookie to build the `X-CSRFToken` header (spec §8). The CSRF token is
not a session credential, and Django's own default is `False` for exactly this reason.

**Does the Django admin honour `X-Forwarded-Proto` here? Yes — and it has to.**
`config/settings/prod.py` sets `SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')`,
and the new `location ^~ /admin/` forwards `$findajob_forwarded_proto` (the same `map` the
`/api/` block uses). Three things depend on it:

- `SECURE_SSL_REDIRECT = True` — without a truthful `https` here, Django answers every admin
  page with a `301` back to itself and the browser loops. This is the same trap already
  documented at the top of `deploy/nginx.conf` for `/api/`.
- `request.is_secure()` gates whether Django will actually set the `Secure` cookies it just
  marked secure.
- Absolute URLs the admin builds (password-reset mail, `View site`) come out `https://`.

Note that gunicorn's `forwarded_allow_ips = "127.0.0.1"` is **irrelevant** to this and is
correctly left alone: it only controls whether *gunicorn* rewrites `wsgi.url_scheme`. Django
reads the raw header itself. Django also trusts that header unconditionally, so the real
control is that only Traefik → nginx can reach the api container's port — the same assumption
`/api/` has always run on.

**Changed for this, and only this:** `STORAGES["staticfiles"]["OPTIONS"]` in
`config/settings/base.py` and `config/settings/prod.py` (the permission trap above). Plus
`removeHeader: true` on the basicAuth middleware, so the `Authorization` header is stripped at
Traefik instead of sitting in gunicorn's request environment for a header Django never reads.

**Declined, with reasons — do not add these later without re-reading this:**

- **A shorter session age for staff is NOT cheap.** `SESSION_COOKIE_AGE` is a single global
  value. Making it role-dependent means a middleware calling `request.session.set_expiry()` on
  every request from a staff user, which drags in `SESSION_SAVE_EVERY_REQUEST` (a database
  write per request, and sessions live in PostgreSQL here) — and because staff accounts are the
  *same* `accounts.User` rows as ordinary accounts, it would also shorten the operator's
  **product** session and log them out mid-task. Real cost, speculative benefit. Not done.
- **Admin login rate limiting.** Would need a new dependency or a hand-rolled cache middleware.
  basicAuth already answers unauthenticated traffic with a `401` before Django is reached, so
  the volume this would throttle does not arrive.
- **2FA on the admin** (`django-otp`) is a heavy new dependency and out of scope per
  `CLAUDE.md`. basicAuth is the second factor-ish layer for now, honestly labelled as a shared
  secret rather than a second factor.

**One real gap left open, deliberately visible:** `DJANGO_ADMINS` is **unset** in
`/root/findajob/.env`, so `ADMINS = []` and an unhandled admin `500` mails nobody. Setting it is
one line in `.env` plus `docker compose up -d api`; it is left as an operator decision because
it starts sending tracebacks (which can contain request data) to a mailbox.

### Rotating the credential

```bash
ssh macbook-air
htpasswd -B /opt/gethub/traefik/dynamic/findajob-admin.htpasswd findajob-admin
touch /opt/gethub/traefik/dynamic/findajob.yml   # force Traefik to re-read usersFile
```

The `touch` is not superstition: Traefik reads `usersFile` when it *builds* the middleware, and
the file provider watches the directory for config changes. Editing only the `.htpasswd` may
not trigger a rebuild.

If the file is missing entirely, Traefik logs an error and the middleware rejects everything —
`/admin` 401s and the rest of findajob.pro is served by the other router, unaffected. That is
the correct failure direction.

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
# legal documents: idempotent, safe to re-run, `--dry-run` reports without writing
docker compose run --rm -e DJANGO_COLLECTSTATIC=0 api python manage.py seed_legal --dry-run
docker compose run --rm -e DJANGO_COLLECTSTATIC=0 api python manage.py seed_legal
```

---

## 4. Routine changes — what to run for what

| What changed | Command |
|---|---|
| `web/**` (html/js/css/assets) | `deploy/deploy.sh web` — ships from `git archive HEAD`, snapshots first, seeds legal, verifies. A bare `rsync -az --delete web/` also publishes `web/graphify-out/`, which `.gitignore` excludes and rsync does not read |
| **`web/legal.js`** | `deploy/deploy.sh web` — it re-seeds. Doing it by hand means `docker compose exec -T api python manage.py seed_legal` **as well as** the rsync: that file is both what the browser renders and what is hashed into `LegalDocument`, and consent fails **closed** with 503 when the two disagree |
| `backend/**/*.py` (no new dependency) | rsync `backend/`, then `docker compose restart api` |
| **`backend/requirements.txt`** | `docker compose build api && docker compose up -d api` — **a bare `restart` runs the OLD image** and 500s on the missing import |
| `backend/Dockerfile`, `backend/gunicorn.conf.py`, `backend/docker/*` | `docker compose build api && docker compose up -d api` |
| a new migration | §3, then `docker compose restart api` |
| `deploy/nginx.conf` | rsync, then `docker compose up -d --force-recreate web` (see the bind-mount trap) |
| `deploy/docker-compose.yml` | rsync to `/root/findajob/docker-compose.yml`, then `docker compose up -d` |
| **a volume added/removed in `docker-compose.yml`** | `docker compose up -d <service>` — **`restart` is not enough.** Mounts are fixed at container create, so a `restart` reuses the old container and the new volume never appears |
| `deploy/traefik/findajob.yml` | `scp` to `/opt/gethub/traefik/dynamic/findajob.yml` — Traefik hot-reloads, nothing to restart |
| the `/admin` basicAuth password | `htpasswd -B …` on the VPS, then `touch` the Traefik yml — §2a |
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

# --- /admin gate 1: Traefik basicAuth (no credentials) ---
for p in /admin /admin/ /admin/login/ /admin/accounts/user/; do
  printf '%-26s ' "$p"; curl -s -o /dev/null -w '%{http_code}\n' "https://findajob.pro$p"
done                                                          # all four: 401
curl -sI https://findajob.pro/admin/ | grep -i '^www-authenticate'
#   -> Basic realm="findajob.pro admin"   <- proves the middleware is attached
curl -s -o /dev/null -w '%{http_code}\n' https://www.findajob.pro/admin/     # 308 to the apex,
#   which then 401s — the www router redirects before serving anything

# --- /admin gate 1 satisfied: Django's own login must now answer ---
curl -s -u 'findajob-admin:<password>' -o /dev/null -w '%{http_code}\n' \
  https://findajob.pro/admin/                                 # 302 -> /admin/login/
curl -s -u 'findajob-admin:<password>' https://findajob.pro/admin/login/ \
  | grep -c 'csrfmiddlewaretoken'                             # 1 — the form rendered
# and gate 2 really is a gate: a valid basicAuth pair alone reaches no data
curl -s -u 'findajob-admin:<password>' -o /dev/null -w '%{http_code}\n' \
  https://findajob.pro/admin/accounts/user/                   # 302 -> login, NOT 200

# --- /static/ (admin CSS). NOT gated: it has to load, and it holds nothing private ---
curl -s -o /dev/null -w '%{http_code}\n' https://findajob.pro/static/admin/css/base.css
#   -> 200.  A 403 here means the STORAGES OPTIONS permission fix did not land (§2a).
curl -sI https://findajob.pro/static/admin/css/base.css | grep -iE '^(cache-control|content-type)'
#   -> max-age=2592000, text/css
curl -s -o /dev/null -w '%{http_code}\n' https://findajob.pro/static/staticfiles.json  # 404
# the CSP is still inherited on both proxied surfaces (no add_header anywhere in a location)
curl -sI https://findajob.pro/static/admin/css/base.css | grep -ci 'content-security-policy'  # 1

# --- /media/ must stay unreachable through nginx, forever ---
curl -s -o /dev/null -w '%{http_code}\n' https://findajob.pro/media/            # 404
curl -s -o /dev/null -w '%{http_code}\n' https://findajob.pro/media/anything    # 404

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

## 7. `/admin/`, `/media/`, logging — what is exposed and what is not

- **`/admin/` IS exposed**, at `https://findajob.pro/admin/`, behind Traefik basicAuth plus
  Django's superuser login. Full procedure in §2a. This reverses the earlier position that it
  should stay unreachable; the reasoning is recorded there rather than repeated here.
  Management commands remain the right tool for anything scriptable:
  `docker compose run --rm -e DJANGO_COLLECTSTATIC=0 api python manage.py <command>`.
- **What the Django admin can reach, it can reach fully.** `apps.tenancy`'s default managers
  raise `OrgScopeError` on an unscoped read, and every `ModelAdmin` there deliberately uses
  `Model.all_objects` (`apps/tenancy/admin.py`) — i.e. admin sees **every tenant**. That is
  intended for an operator-only surface and is the reason the gate is two-layered. Do not
  "fix" those `all_objects` calls: without them the admin pages raise.
- **`/media/` is NOT served by nginx and never will be.** Candidate documents are personal
  data; they are delivered only through an owner-checked Django view. `MEDIA_ROOT` is the
  `findajob_media_data` volume, mode 0700. There is no `location` for it in `deploy/nginx.conf`
  and adding one would bypass the ownership check entirely.
- **`/static/` IS served by nginx**, from the shared `findajob_static_data` volume, and is
  deliberately **outside** the basicAuth router: the admin's own stylesheets have to load, they
  contain nothing private, and gating them would make the browser re-authenticate per asset.
  The only file excluded is `staticfiles.json`, Django's internal unhashed→hashed map — not
  secret, just not ours to serve.
- **Renaming `/admin/` to an obscure path buys nothing here** and is deliberately not done.
  basicAuth already answers every unauthenticated scanner with a `401` before Django is
  reached, so obscurity would add a second name to keep in sync for no measurable gain, and
  would invalidate every URL in this file.
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

# put nginx back to serving static-only (removes the /api/ and /admin/ proxies)
git -C /root/findajob checkout deploy/nginx.conf   # or restore your copy
docker compose up -d --force-recreate web
```

### Closing `/admin/` again, without touching anything else

Fastest, and it needs no deploy — rename the credential file, which makes Traefik's basicAuth
reject every request:

```bash
ssh macbook-air
mv /opt/gethub/traefik/dynamic/findajob-admin.htpasswd{,.disabled}
touch /opt/gethub/traefik/dynamic/findajob.yml     # force the middleware rebuild
curl -s -o /dev/null -w '%{http_code}\n' https://findajob.pro/admin/   # 401, forever
```

To remove the surface rather than lock it: delete the `findajob-admin` router from
`/opt/gethub/traefik/dynamic/findajob.yml` (leaving it 200-reachable via the main router — so
do this only *together with* dropping the `^~ /admin/` and `= /admin` locations from
`deploy/nginx.conf` and force-recreating `web`). Order matters: **remove the nginx locations
first**, or there is a window where `/admin/` is proxied with no gate in front of it.

`/static/` can stay either way — it is only Django's admin CSS.

Rolling the **database** back is a restore, not a `down`: `docker compose down -v` would delete
`findajob_pg_data`. Never use `-v` on this stack. Existing backups live in `/root/backups/` on
the VPS and `~/Desktop/findajob-atomic-backup-2026-08-01/` locally.
