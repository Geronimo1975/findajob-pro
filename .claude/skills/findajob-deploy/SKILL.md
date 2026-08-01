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
