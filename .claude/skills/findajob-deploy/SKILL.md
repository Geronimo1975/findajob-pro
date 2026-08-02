---
name: findajob-deploy
description: How findajob.pro is deployed on the VPS (SSH host macbook-air, 85.215.69.83) behind Traefik — the static-frontend stack, nginx hardening, Traefik routes, the bind-mount gotcha, rollback backups, and the rules for evolving to the Django backend. Use for any deploy, infra, DNS, TLS, or "site is down" work on findajob.
---

# findajob.pro deployment

**VPS:** SSH host `macbook-air` (85.215.69.83). There is no host named "macbook-pro" — if the
user says that, they mean `macbook-air`. Hosting provider is **IONOS SE** (Frankfurt, DE) —
verified by whois; the privacy policy must name IONOS, not Hetzner.
Reverse proxy = **Traefik** (separate stack), dynamic config dir
`/opt/gethub/traefik/dynamic/`, external docker network `gethub_gethub-network`, TLS via the
`letsencrypt` certResolver. Traefik already redirects http→https globally.

## Email delivery — configured and verified 2026-08-02

Login is a 6-digit email OTP sent synchronously, so mail delivery IS the login path. It works
end to end; a delivered message was confirmed in Brevo's transactional log, not merely a 202.

| Thing | Value |
|---|---|
| Transport | Brevo **SMTP relay**, `smtp-relay.brevo.com:587`, TLS. Django's built-in SMTP backend |
| `EMAIL_HOST_USER` | `a4167f001@smtp-brevo.com` (Brevo's SMTP login, from Settings → SMTP & API) |
| `EMAIL_HOST_PASSWORD` | SMTP key named "findajob.pro OTP login" — **expires 2027-08-01** |
| Sender | `'"Findjob.pro" <noreply@mail.findajob.pro>'` in `.env` |
| Sending domain | `mail.findajob.pro`, authenticated in Brevo (DKIM + SPF + DMARC live) |

**The SMTP key expires 2027-08-01 and nothing warns you.** When it does, every login stops:
`request-code` returns 503 and no code is ever delivered. Rotate it in Brevo (Settings →
SMTP & API → Generate SMTP key), put it in `/root/findajob/.env`, then
`docker compose up -d api` — env is read at container CREATE, so a bare `restart` keeps the
old value.

**Do NOT use the Brevo API backend** (`anymail.backends.brevo.EmailBackend`). Brevo enforces an
IP allowlist on API calls and 401s this VPS; SMTP authenticates by credential instead. Note
`backend/.env.example` still suggests the API backend — the VPS `.env` deliberately overrides it.
Equally, do NOT click "Activate for SMTP keys" on Brevo's IP-blocking banner: SMTP keys are not
IP-restricted today, which is exactly why sending works from a box whose IP is not allowlisted.

**Two failures that cost time, both silent-looking:**
- Django 6 parses addresses with the modern email API and rejects an unquoted display name
  containing a period: `findajob.pro <noreply@…>` raises `ValueError: period in 'phrase'`, which
  surfaces only as a 503. The value needs inner double quotes — and because Compose's `env_file`
  parser reads quotes, the whole thing must additionally be wrapped in single quotes.
- An unauthenticated sending domain is accepted by SMTP and then rejected at delivery. Brevo
  logs it as an `Error` event next to the `Sent` one, so the API sees a clean 202 and the user
  sees nothing arrive. **After any sender change, check the transactional log for `Delivered`,
  not for a 2xx from our own API.**

### DNS at Hostinger (registrar + DNS, NS = `dns-parking.com`)

Added 2026-08-02, all live and verified on the authoritative NS and on 1.1.1.1. Names are
relative to `findajob.pro`:

| Type | Name | Value |
|---|---|---|
| TXT | `mail` | `brevo-code:0d15e912ee86a10908120a5066bda8bf` |
| TXT | `mail` | `v=spf1 include:spf.brevo.com ~all` |
| CNAME | `brevo1._domainkey.mail` | `b1.mail-findajob-pro.dkim.brevo.com` |
| CNAME | `brevo2._domainkey.mail` | `b2.mail-findajob-pro.dkim.brevo.com` |
| TXT | `_dmarc.mail` | `v=DMARC1; p=none; rua=mailto:rua@dmarc.brevo.com` |

The zone also holds `A @ → 85.215.69.83` and `CNAME www → findajob.pro`, which serve the site.
Never touch those. DMARC is deliberately `p=none` — read the reports for 2–3 weeks, then move to
`p=quarantine; pct=25` and only later `p=reject`. There are still **no MX records**: nothing can
receive mail at `@findajob.pro`, which is fine for OTP (send-only) but means the Impressum's
contact address must stay a mailbox that actually exists elsewhere.

## Current stack (UI-first phase, since 2026-08-01)

| Thing | Value |
|---|---|
| Project root on VPS | `/root/findajob/` |
| Compose file (VPS) | `/root/findajob/docker-compose.yml` — repo copy lives at `deploy/docker-compose.yml` |
| Container | `findajob-web` (`nginx:alpine`), no published ports, on `gethub_gethub-network` |
| Mounts | `./web` → `/usr/share/nginx/html:ro`, `./deploy/nginx.conf` → `/etc/nginx/conf.d/default.conf:ro` |
| Traefik route | `/opt/gethub/traefik/dynamic/findajob.yml` → `http://findajob-web:80`; apex + a www→apex 308 redirect |

`web/` = `index.html` (landing), `app.html` (13-screen dashboard), `support.js` (the .dc
runtime), `api.js` (mock API), `legal.js`, `404.html`, `robots.txt`, `sitemap.xml`,
`favicon.svg`, plus **self-hosted** `vendor/` (React UMD) and `fonts/` (Inter + JetBrains Mono).

### Deploy commands

```bash
# content change (html/js/css/assets) — nginx serves the bind mount, no restart needed
rsync -az --delete web/ macbook-air:/root/findajob/web/

# nginx.conf or docker-compose.yml change — see the gotcha below, a reload is NOT enough
rsync -az deploy/nginx.conf macbook-air:/root/findajob/deploy/nginx.conf
ssh macbook-air "cd /root/findajob && docker compose up -d --force-recreate web"

# Traefik route change — Traefik hot-reloads the dynamic file, nothing to restart
scp deploy/traefik/findajob.yml macbook-air:/opt/gethub/traefik/dynamic/findajob.yml
```

## Scheduled jobs (cron)

Scheduled work lives in `/etc/cron.d/` and `/etc/logrotate.d/` on the VPS, so it is **invisible
to git and to any rsync of the project**. Record every entry in the table below or it is lost on
the next rebuild. The *source* of each entry is kept in `deploy/cron/` in the repo; the installed
copy is what actually runs.

| Entry | Schedule | Source (repo) | Log |
|---|---|---|---|
| `findajob-login-check` | daily 06:17 | `deploy/cron/findajob-login-check` (+ `run-login-check.sh`, `login-check-history`, `findajob-login-check.logrotate`) | `/var/log/findajob/login-check.log` |

*(This is the first cron entry of the post-2026-08-01 stack. The old Django/Jobick platform's
`findajob-scrape` entry died with that platform — if you find it still installed on the box, it
is pointing at containers that no longer exist and should be removed.)*

### The pattern — every entry must follow it

```
SHELL=/bin/sh
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
MAILTO=""

17 6 * * * root mkdir -p /var/log/findajob && flock -n /var/lock/findajob-login-check.lock /root/findajob/deploy/cron/run-login-check.sh >> /var/log/findajob/login-check.log 2>&1
```

Every part is load-bearing:

- **`SHELL` + `PATH` spelled out.** cron starts with a near-empty environment; its default `PATH`
  differs per distro. `docker` is `/usr/bin/docker`, `flock` is `/usr/bin/flock`, but anything
  hand-installed is under `/usr/local/bin`. This is the #1 reason a line that works by hand never
  runs on schedule.
- **`MAILTO=""`.** The box has no MTA and `findajob.pro` has no MX records, so cron's own mail
  attempt only produces syslog noise. The log file is the record.
- **`flock -n <lock>`.** Runs must never overlap — overlap happens exactly when something is
  already hanging, i.e. when the job is about to report a problem, and two writers corrupt the
  chronological log. `-n` **skips** rather than queues: a delayed run reports on a moment nobody
  is watching, and the next tick supersedes it anyway. The skip message lands in the log and is
  itself the signal that the previous run hung.
- **`timeout` somewhere, always.** `flock` bounds concurrency but not duration; a hung run with
  no timeout holds the lock **forever** and silently kills every later tick — worse than the
  original hang. Put it **inside** the lock, wrapping the blocking call (the login check does this
  inside `run-login-check.sh`), so the lock holder always returns. Outside the lock it kills the
  holder and orphans the work. Residual: `timeout` kills the docker CLI on the host, not
  necessarily the process inside the container.
- **`docker compose exec -T`.** cron has no TTY; without `-T` Compose dies with "the input device
  is not a TTY". Use `exec` (the running container, the environment real requests use), not `run`
  (a second container). The command needs `cd /root/findajob` first — Compose resolves the project
  from the cwd and cron's cwd is `$HOME`.
- **`mkdir -p` the log directory.** A `>>` redirection creates a missing *file*, never a missing
  *directory*. Without it a fresh box discards every run silently.
- **Append with `2>&1`, one writer.** Let the script write to stdout only and let cron redirect;
  that way a hand-run prints to the terminal instead of polluting the record, and flock's own
  message lands in the same stream.
- **Ship a logrotate file with every log.** `/etc/logrotate.d/<name>`, and run
  `logrotate -d <file>` after editing — a syntax error in one file aborts the daily pass for
  **every** log on the box.

### Two silent-skip traps

- **cron ignores a `/etc/cron.d/` file whose name contains a `.`**, or that is group/other
  writable, or that lacks a trailing newline — with no error anywhere. Same taboo rule applies to
  `/etc/logrotate.d/`. Hence the repo keeps `findajob-login-check.logrotate` but installs it as
  `findajob-login-check`.
- **`scp` does not preserve the executable bit** (`rsync -a` does). A non-executable runner fails
  with a bare "Permission denied" in the log.

Verify a change under cron's own environment rather than waiting for the tick:

```bash
env -i SHELL=/bin/sh PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  /bin/sh -c '<the exact command from the cron line>'
ssh macbook-air "run-parts --test /etc/cron.d"     # is the file even eligible?
```

### The login healthcheck specifically

Answers "has login been working?" in one command:

```bash
ssh macbook-air /root/findajob/deploy/cron/login-check-history --limit 7
ssh macbook-air /root/findajob/deploy/cron/login-check-history --last      # full block
```

Greppable summary line: `LOGIN-CHECK <iso8601+offset> <PASS|FAIL> duration= exit= steps= failed= run= invoked=`.

**A `PASS` means Brevo ACCEPTED the message. It does NOT mean a human received it** — real
delivery depends on SPF/DKIM/DMARC on `mail.findajob.pro` and a verified sender in Brevo, none of
which is observable from inside our network. After any sender or DNS change, check Brevo's
transactional log for `Delivered`, exactly as the email section above says. Full rationale,
per-failure-mode runbook and log format: `docs/2026-08-02-login-healthcheck.md`.

Its main reason to exist is dated: **the Brevo SMTP key expires 2027-08-01**, and the api
container's own Docker `HEALTHCHECK` is liveness-only by design, so it reports "healthy" during a
total login outage.

## Gotchas that cost real time

- **A single-file bind mount breaks when rsync replaces the file.** Docker binds
  `deploy/nginx.conf` by **inode**; rsync writes a temp file and renames it, so the container
  keeps serving the OLD config. `nginx -t` and `nginx -s reload` both report success while
  changing nothing — the deceptive part. Always `docker compose up -d --force-recreate web`
  after touching `nginx.conf`. (Directory mounts like `web/` are unaffected.)
- **The container has NO published ports.** Traefik reaches it in-network. Never add `ports:`.
- Do NOT touch the other stacks on the box (djaingo-*, n8n-*, wp-*, rotocon-*, georgeai, aiopify).

## nginx configuration — what is load-bearing

- **No SPA fallback.** This is a two-page static site: `try_files $uri $uri/ =404;`. The earlier
  `/index.html` fallback made every unknown path a 200 soft-404 and made `/robots.txt` serve the
  landing page.
- **`absolute_redirect off;`** — without it `location = /app` emitted a plaintext
  `http://findajob.pro/app.html` Location behind the TLS-terminating proxy.
- **Caching uses `expires`, never `add_header`, inside locations.** An `add_header` in a
  location block drops every header inherited from the server block — it would silently kill
  the CSP.
- Traefik's `secure-headers` middleware already supplies HSTS, `X-Content-Type-Options` and
  `Referrer-Policy`; nginx adds CSP, `X-Frame-Options: DENY` and `Permissions-Policy`.
- The CSP needs `'unsafe-eval'` (the .dc runtime evaluates its page script through
  `new Function`) and `'unsafe-inline'` for styles (the prototype is 100% inline styles). What
  it does buy is `default-src 'self'` — no third-party origin can be contacted — plus no framing.

## Third-party origins: zero, on purpose

React/ReactDOM are served from `web/vendor/` via the runtime's `window.__resources` override
(set in each page's `<head>` **before** `support.js`); the files are byte-identical to the SRI
hashes hardcoded in `support.js`. Inter and JetBrains Mono are self-hosted woff2 in
`web/fonts/` (latin + latin-ext; Google ships them as variable fonts, so 4 files cover all 7
weights). Reason: a Germany-targeted site must not hand a visitor's IP to unpkg.com or Google
before consent, and a CDN outage would otherwise blank both pages. Do not re-add a CDN tag.

## Checks

```bash
curl -sI https://findajob.pro            # 200 text/html
curl -s -o /dev/null -w '%{http_code}' https://findajob.pro/nope   # 404, branded page
curl -sI https://www.findajob.pro        # 308 -> https://findajob.pro/
curl -sI https://findajob.pro/ | grep -i 'content-security\|x-frame'
ssh macbook-air "docker ps --filter name=findajob-web"
```

## Backups / rollback (do not delete)

- VPS `/root/backups/`: `findajob-django-tree-FINAL-20260801.tar.gz` (old platform code),
  `findajob-prod-FINAL-20260801_1415.sql` (final Postgres dump),
  `findajob-traefik-route-FINAL-20260801.yml` (old Traefik route).
- Desktop `~/Desktop/findajob-atomic-backup-2026-08-01/` — immutable full backup incl. real
  prod secrets, git bundle M1→M6, prototype, docs.
- GitHub `Geronimo1975/findjob-pro` = discarded R0/R1 Django-6 attempt (parts bin only).

## When the Django backend lands

Keep the same Traefik router; route `/api/` (and later `/`) to the Django service. The static
frontend goes live-data by swapping `api.js` mock bodies for `fetch(BASE+path)` — but read the
"Contract gaps" section of `findajob-architecture` first: `ENDPOINTS` is missing auth entirely
and 6+ other routes the UI already calls.

## Traefik traps measured on this box (2026-08-02)

- **The file provider's watcher is unreliable here.** After an edit, Traefik kept serving the
  previous configuration for over an hour — `touch`, a real content change and an atomic
  `mv` all produced zero reload events in the log. If a route change does not take effect,
  check `docker logs gethub-traefik | grep "Configuration loaded"` before debugging the rule.
  The only reliable apply is `docker restart gethub-traefik` (~0.5 s; all sites recover).
- **A router and the service it names can live in different files.** `n8n-studio.yml` routed
  the live `app.n8n-studio.com` to `service: n8n`, which was defined in the *dead*
  `n8n.get-hub.io` file. Deleting the dead file 404'd the live site. Before removing any
  dynamic file, grep the others for the service names it defines.
- **`get-hub.io` is dead (NXDOMAIN, zone gone) but "gethub" names are NOT.** `gethub-traefik`
  is the shared reverse proxy for every site on the box, `gethub-postgres` / `gethub-redis`
  back nine containers across resume/resume-v2/saas-ai, `gethub_gethub-network` is the network
  findajob-api and findajob-web sit on, and `/opt/gethub/traefik/dynamic/` holds findajob's own
  route and htpasswd. Never delete by the string "gethub".
- **Dead routes come from two providers.** File routes were removed and 27 dead certificates
  pruned from `acme.json` (12 live remain). Nine containers still declare `get-hub.io` hosts via
  **docker labels** — `saas-ai-web`, `rotocon-web`, `rotocon-configurator`, `gmx_management-web-1`,
  `mycloud-ocis`, `resume-nginx`, `resume-v2-nginx`, `social_media_web`, `gethub-traefik` — and
  each keeps asking Let's Encrypt for a certificate it can never get. None of them serves a
  reachable host, so this is noise and ACME rate-limit pressure, not an outage.
