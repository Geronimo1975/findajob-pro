# findajob.pro — how the PostgreSQL database was actually created

**Written 2026-08-01, against the live database.** Every table, column, index, constraint and
foreign key below was read out of the running `findajob-db` container and cross-checked against
the migration files that produced it. Nothing here is inferred from the models alone — where
the model and the database disagree (and they do, in two important ways: defaults and
`on_delete`), the database wins and the difference is called out.

§7 lists the exact commands to reproduce every claim.

---

## 1. Provenance — the four steps that produced this schema

There is no schema file, no `.sql` dump to apply, and no `initdb` hook. The database is
produced entirely by these four steps, in this order:

**1. The container is created by Compose** (`deploy/docker-compose.yml`, service `db`):

```yaml
image: postgres:16-alpine
environment:
  POSTGRES_DB:        ${POSTGRES_DB:-findajob}
  POSTGRES_USER:      ${POSTGRES_USER:-findajob}
  POSTGRES_PASSWORD:  ${POSTGRES_PASSWORD:?…}   # no default: `up` fails loudly
  POSTGRES_HOST_AUTH_METHOD: scram-sha-256
  POSTGRES_INITDB_ARGS: "--data-checksums"
  PGDATA: /var/lib/postgresql/data/pgdata
volumes:
  - pg_data:/var/lib/postgresql/data            # named volume findajob_pg_data
```

That is the whole of the "create the database" part: the official image's entrypoint runs
`initdb` on first start, creates role `findajob` and database `findajob`, and never runs again
because `pg_data` persists. `--data-checksums` applies **only** on that first `initdb` — it
cannot be added later without a dump/restore. `scram-sha-256` is already the PG16 default and
is stated so a future edit cannot quietly downgrade it.

Resulting cluster, read from the live server:

| | |
|---|---|
| Server | `PostgreSQL 16.11 on x86_64-pc-linux-musl` (Alpine) |
| Database / role | `findajob` / `findajob` |
| Encoding | `UTF8` |
| Collation / ctype | `en_US.utf8` / `en_US.utf8` |
| Network | `findajob-internal` only (`internal: true`) — no route on or off the box |

**2. `migrate` is run by hand.** The api image's entrypoint deliberately never migrates
(`backend/docker/entrypoint.sh`, `deploy/README.md` §3), so the schema only ever changes when
an operator runs:

```bash
docker compose run --rm -e DJANGO_COLLECTSTATIC=0 api python manage.py migrate
```

**3. Twenty-one migrations applied, once, on 2026-08-01 17:47:53 UTC** — the whole schema was
created in a single run and has not been touched since. In `django_migrations` order:

| # | app | migration | what it creates |
|---|---|---|---|
| 1–2 | `contenttypes` | `0001`, `0002` | `django_content_type` |
| 3–14 | `auth` | `0001`–`0012` | `auth_permission`, `auth_group`, `auth_group_permissions` |
| **15** | **`accounts`** | **`0001_initial`** | **`accounts_user`, `accounts_user_groups`, `accounts_user_user_permissions`, `accounts_emailotp`, `accounts_suppressedaddress`** |
| 16–18 | `admin` | `0001`–`0003` | `django_admin_log` |
| **19** | **`legal`** | **`0001_initial`** | **`legal_legaldocument`, `legal_consentrecord`** |
| 20 | `sessions` | `0001` | `django_session` |
| **21** | **`tenancy`** | **`0001_initial`** | **`tenancy_organization`, `tenancy_membership`, `tenancy_auditlog`** |

The ordering is itself evidence of a decision that could only be taken once:
`accounts.0001_initial` runs at position **15, before `admin.0001_initial` at 16**, because
`AUTH_USER_MODEL = "accounts.User"` was set before the first `migrate` (spec §2 — *"there is
exactly one chance at this"*). The visible consequence is that **`auth_user` does not exist**
and `django_admin_log.user_id` points at `accounts_user`.

Each of our three apps has exactly one migration, all `0001_initial`. There is no `0002`
anywhere, and `makemigrations --check --dry-run` reports **"No changes detected"** — the models
and the database are in sync with no pending drift.

**4. `seed_legal` populates `legal_legaldocument`.** Data, not schema, but the database is not
usable without it: registration consent resolves the current German AGB/Datenschutz
server-side and fails **closed** (503) when they are missing, so an unseeded database means
nobody can complete a first login.

```bash
docker compose run --rm -e DJANGO_COLLECTSTATIC=0 api python manage.py seed_legal
```

It reads the same `web/legal.js` that nginx serves (bind-mounted read-only at `/web`), so the
hashed consent text can never drift from the rendered text.

Current row counts, for orientation:

```
accounts_user 1 · accounts_emailotp 14 · accounts_suppressedaddress 0
legal_legaldocument 18 (6 documents x 3 languages) · legal_consentrecord 2
tenancy_organization 1 · tenancy_membership 1 · tenancy_auditlog 0
django_session 3 · django_content_type 13 · auth_permission 52 (13 x 4)
```

---

## 2. The real table list — 17 tables

```
accounts_emailotp                 legal_consentrecord
accounts_suppressedaddress        legal_legaldocument
accounts_user                     tenancy_auditlog
accounts_user_groups              tenancy_membership
accounts_user_user_permissions    tenancy_organization
                                  ------------------------- 10 ours
auth_group                        django_admin_log
auth_group_permissions            django_content_type
auth_permission                   django_migrations
                                  django_session
                                  ------------------------- 7 Django's
```

"Ours" means: created by a migration in `backend/apps/`. Two of the ten
(`accounts_user_groups`, `accounts_user_user_permissions`) are join tables Django generates
automatically from `PermissionsMixin` on our swapped `User` — our tables, Django's semantics.

---

## 3. Our ten tables, column by column

### 3.1 `accounts_user` — `apps/accounts/models.py::User`

`AbstractBaseUser` + `PermissionsMixin`, email as the username field, **no `username` column**.

| Column | Type | Null | Default in DDL | Django default | Notes |
|---|---|---|---|---|---|
| `id` | `bigint` | no | `GENERATED BY DEFAULT AS IDENTITY` | — | `BigAutoField`; `DEFAULT_AUTO_FIELD` is deliberately not set, and 6.0's default is already `BigAutoField` |
| `password` | `varchar(128)` | no | — | — | from `AbstractBaseUser`. OTP accounts hold an **unusable** password (`set_unusable_password()`), not an empty one |
| `last_login` | `timestamptz` | **yes** | — | — | from `AbstractBaseUser` |
| `is_superuser` | `boolean` | no | — | `False` | from `PermissionsMixin` |
| `email` | `varchar(254)` | no | — | — | `unique`; RFC 5321 practical max. Normalised (NFKC, lowercased, non-printables stripped) in both `clean()` and `save()` |
| `is_verified` | `boolean` | no | — | `False` | true once an OTP verify succeeded; surfaced by `GET /api/v1/me/` |
| `lang` | `varchar(8)` | no | — | `'en'` | choices `en` / `de` / `ro` — enforced by forms/`full_clean`, **not** by a DB constraint |
| `is_active` | `boolean` | no | — | `True` | |
| `is_staff` | `boolean` | no | — | `False` | the Django-admin gate |
| `date_joined` | `timestamptz` | no | — | `auto_now_add` | |
| `updated_at` | `timestamptz` | no | — | `auto_now` | not written by `.update()` / `bulk_update()` |

**Indexes**
- `accounts_user_pkey` — PK btree on `id`
- `accounts_user_email_key` — UNIQUE constraint on `email`
- `accounts_user_email_b2644a56_like` — btree on `email varchar_pattern_ops` (see §4.3)

**Constraints**
- `user_email_not_blank` — `CHECK (NOT (email = ''))`. Written as
  `CheckConstraint(condition=~Q(email=""))` — Django 6.0 removed `check=`.

**Referenced by** (all `DEFERRABLE INITIALLY DEFERRED`, none with an `ON DELETE` clause — §4.2):
`accounts_user_groups.user_id`, `accounts_user_user_permissions.user_id`,
`django_admin_log.user_id`, `legal_consentrecord.user_id`, `tenancy_auditlog.actor_id`,
`tenancy_membership.user_id`.

### 3.2 `accounts_user_groups` and `accounts_user_user_permissions`

Auto-generated M2M join tables for `PermissionsMixin.groups` / `.user_permissions`.

| Table | Columns | Unique | FKs (Django `on_delete`) |
|---|---|---|---|
| `accounts_user_groups` | `id bigint`, `user_id bigint`, `group_id integer` | `(user_id, group_id)` | → `accounts_user`, → `auth_group`; both CASCADE |
| `accounts_user_user_permissions` | `id bigint`, `user_id bigint`, `permission_id integer` | `(user_id, permission_id)` | → `accounts_user`, → `auth_permission`; both CASCADE |

Both also carry a plain btree index on each FK column. `group_id` / `permission_id` are
`integer`, not `bigint`, because `auth.Group` and `auth.Permission` were created under
Django's own historical `AutoField` — a size mismatch that is harmless and permanent.

Both are empty (0 rows): permissions are not used yet; admin access is superuser-only.

### 3.3 `accounts_emailotp` — `apps/accounts/models.py::EmailOTP`

One issued login code. **The code itself is not in this table** — see §5.1.

| Column | Type | Null | Django default | Notes |
|---|---|---|---|---|
| `id` | `bigint` | no | identity | |
| `email` | `varchar(254)` | no | — | `db_index=True`. Normalised the same way as `User.email`, so a rate-limit bucket cannot be dodged by changing case |
| `code_hash` | `varchar(64)` | no | — | 64 hex chars = sha256. `salted_hmac(key_salt, code, algorithm="sha256")`, keyed by `SECRET_KEY`, key salt `"findajob.otp:<normalised email>"` |
| `purpose` | `varchar(32)` | no | `'login'` | |
| `created_at` | `timestamptz` | no | `auto_now_add` | |
| `expires_at` | `timestamptz` | no | — | set explicitly by the caller, `created_at + 10 min` |
| `attempts` | `smallint` | no | `0` | |
| `consumed_at` | `timestamptz` | **yes** | `None` | non-null = burned |
| `request_ip` | `inet` | **yes** | `None` | `GenericIPAddressField` → native `inet`. Resolved through `TRUSTED_PROXY_COUNT=2` |

**Indexes**
- `accounts_emailotp_pkey`
- `accounts_emailotp_email_d0b1e7f0` — btree on `email` (from `db_index=True`)
- `accounts_emailotp_email_d0b1e7f0_like` — btree on `email varchar_pattern_ops`
- `otp_email_created_idx` — btree on `(email, created_at DESC)`. Covers the only hot query:
  *live codes for this address, newest first*.

**Constraints — there are two, and only one of them is ours**
- `otp_attempts_bounded` — `CHECK (attempts <= 5)`, declared in `Meta.constraints`.
- `accounts_emailotp_attempts_check` — `CHECK (attempts >= 0)`, emitted **automatically** by
  Django for every `PositiveSmallIntegerField`. It is not in the migration file's
  `constraints` list; it is part of the column definition
  (`"attempts" smallint NOT NULL CHECK ("attempts" >= 0)`). Together they pin `0 <= attempts <= 5`.

No foreign key to `User`, deliberately: `request-code` is branch-free (spec §6) and must behave
identically for an address that has an account and one that does not, so it cannot look a user
up — and a nullable FK that is usually null would tempt someone to.

### 3.4 `accounts_suppressedaddress` — `apps/accounts/models.py::SuppressedAddress`

| Column | Type | Null | Django default | Notes |
|---|---|---|---|---|
| `id` | `bigint` | no | identity | |
| `email` | `varchar(254)` | no | — | `unique` |
| `reason` | `varchar(64)` | no | `'hard_bounce'` | choices: `hard_bounce`, `complaint`, `unsubscribe`, `blocked`, `manual` — not a DB constraint |
| `created_at` | `timestamptz` | no | `auto_now_add` | |
| `updated_at` | `timestamptz` | no | `auto_now` | |

**Indexes:** PK; `accounts_suppressedaddress_email_key` UNIQUE; `…_email_0b7b21b0_like`.
**Constraints:** none beyond the unique.

Exists because `request-code` is branch-free and therefore mails addresses that do not exist.
Brevo posts hard bounces and complaints to the signature-verified
`POST /webhooks/email/brevo/`; `issue_login_code` skips a suppressed address **while still
returning 202**, so suppression never becomes an enumeration oracle. Deleting a row here is the
supported un-suppress.

### 3.5 `legal_legaldocument` — `apps/legal/models.py::LegalDocument`

One version of one document in one language. 18 rows = 6 documents × 3 languages.

| Column | Type | Null | Django default | Notes |
|---|---|---|---|---|
| `id` | `bigint` | no | identity | |
| `doc_id` | `varchar(32)` | no | — | `terms`, `privacy`, `cookies`, `impressum`, `withdrawal`, `dpa` |
| `lang` | `varchar(8)` | no | — | `en` / `de` / `ro`; **`de` is the legally binding text** |
| `version` | `varchar(32)` | no | — | derived from the document date, e.g. `2026-08-01` |
| `title` | `varchar(200)` | no | — | |
| `sections` | `jsonb` | no | `list` | ordered `[{"heading":…, "body":…}]` |
| `note` | `text` | no | `''` | per-language notice (e.g. "DE is binding") |
| `content_hash` | `varchar(64)` | no | — | `db_index=True`; sha256 over `(doc_id, lang, version, title, sections)` |
| `effective_from` | `timestamptz` | no | — | |
| `is_current` | `boolean` | no | `False` | at most one true per `(doc_id, lang)` — §5.2 |
| `created_at` | `timestamptz` | no | `auto_now_add` | |
| `updated_at` | `timestamptz` | no | `auto_now` | |

**Indexes**
- `legal_legaldocument_pkey`
- `legal_legaldocument_content_hash_27d4ed92` + `…_like` — from `db_index=True`
- `legaldoc_doc_lang_eff_idx` — btree on `(doc_id, lang, effective_from DESC)`
- `uniq_current_legaldocument` — **partial** UNIQUE index:
  `CREATE UNIQUE INDEX … ON (doc_id, lang) WHERE "is_current"` — §5.2

**Constraints**
- `uniq_legaldocument_version` — UNIQUE `(doc_id, lang, version)`
- `legaldocument_content_hash_not_blank` — `CHECK (NOT (content_hash = ''))`
- `legaldocument_version_not_blank` — `CHECK (NOT (version = ''))`

No foreign keys in either direction. That is the point: see §5.3.

### 3.6 `legal_consentrecord` — `apps/legal/models.py::ConsentRecord`

The append-only consent ledger.

| Column | Type | Null | Django default | Notes |
|---|---|---|---|---|
| `id` | `bigint` | no | identity | |
| `user_id` | `bigint` | no | — | **FK → `accounts_user.id`, `on_delete=CASCADE`** |
| `type` | `varchar(32)` | no | — | `terms`, `privacy`, `send_on_behalf`, `marketing`, `analytics`, `functional` |
| `granted` | `boolean` | no | — | **no default** — a consent record with an unstated answer is meaningless |
| `doc_version` | `varchar(32)` | no | `''` | snapshot, not a pointer |
| `content_hash` | `varchar(64)` | no | `''` | snapshot of the exact text agreed to |
| `doc_lang` | `varchar(8)` | no | `''` | always `de` for registration consents, whatever the UI showed |
| `ip` | `inet` | **yes** | `None` | |
| `user_agent` | `varchar(400)` | no | `''` | truncated, not rejected |
| `ui_language` | `varchar(8)` | no | `''` | what the visitor was *reading* — evidence, not authority |
| `created_at` | `timestamptz` | no | `auto_now_add` | |

**Indexes**
- `legal_consentrecord_pkey`
- `legal_consentrecord_user_id_6fb583eb` — the implicit FK index
- `consent_user_type_idx` — `(user_id, type, created_at DESC)` → "this user's latest answer for this type"
- `consent_type_idx` — `(type, created_at DESC)` → "everyone who currently grants `send_on_behalf`"

**Constraints:** none beyond the FK. The append-only rule is enforced in `save()`, not by the
database — §5.3.

### 3.7 `tenancy_organization` — `apps/tenancy/models.py::Organization`

The scope root. Every piece of tenant data hangs off exactly one of these.

| Column | Type | Null | Django default | Notes |
|---|---|---|---|---|
| `id` | `bigint` | no | identity | |
| `name` | `varchar(120)` | no | — | |
| `slug` | `varchar(140)` | no | — | `unique`; `SlugField` → plain `varchar` plus validation in Python |
| `plan` | `varchar(16)` | no | `'free'` | `free` / `pro` / `max` / `team`, mirroring `web/api.js`'s `PLANS` |
| `cycle` | `varchar(16)` | no | `'monthly'` | `monthly` / `yearly` |
| `created_at` | `timestamptz` | no | `auto_now_add` | |
| `updated_at` | `timestamptz` | no | `auto_now` | |

**Indexes:** PK; `tenancy_organization_slug_key` UNIQUE; `…_slug_380107f5_like`.
**Constraints:** `organization_name_not_blank` — `CHECK (NOT (name = ''))`. Deliberately **not**
a regex: SQLite cannot evaluate Django's user-registered `REGEXP` inside a `CHECK`, so a regex
here would break `migrate` on the test database.

`plan` and `cycle` are plain fields, not a FK to a `Subscription` that does not exist yet. That
is what keeps `apps.tenancy` free of any dependency on billing; when `Subscription` ships it
becomes the source of truth and these stay as the cheap denormalised copy.

### 3.8 `tenancy_membership` — `apps/tenancy/models.py::Membership`

| Column | Type | Null | Django default | Notes |
|---|---|---|---|---|
| `id` | `bigint` | no | identity | |
| `org_id` | `bigint` | no | — | **FK → `tenancy_organization.id`, CASCADE** |
| `user_id` | `bigint` | no | — | **FK → `accounts_user.id`, CASCADE** |
| `role` | `varchar(16)` | no | `'member'` | `owner` / `admin` / `member` |
| `created_at` | `timestamptz` | no | `auto_now_add` | |
| `updated_at` | `timestamptz` | no | `auto_now` | |

**Indexes**
- `tenancy_membership_pkey`
- `tenancy_membership_org_id_4fc3a038`, `tenancy_membership_user_id_69c4e8b6` — implicit FK indexes
- `membership_user_org_idx` — `(user_id, org_id)`, for "which orgs is this user in"

**Constraints**
- `uniq_membership_per_org_user` — UNIQUE `(org_id, user_id)`: one row per person per workspace
- `membership_role_valid` — `CHECK (role IN ('owner','admin','member'))`. Note this is the
  **only** enum-style choice field in the whole schema that is enforced by the database. Every
  other `choices=` list (`User.lang`, `SuppressedAddress.reason`, `Organization.plan`,
  `LegalDocument.doc_id`, `ConsentRecord.type`, …) is validated in Python only. It is the one
  that got a constraint because role *is* the authorisation decision.

**Managers:** `objects = OrgScopedManager(user_field="user")` (guarded),
`all_objects = models.Manager()` (unguarded); `default_manager_name="objects"`,
`base_manager_name="all_objects"` — §5.4.

### 3.9 `tenancy_auditlog` — `apps/tenancy/models.py::AuditLog`

| Column | Type | Null | Django default | Notes |
|---|---|---|---|---|
| `id` | `bigint` | no | identity | |
| `org_id` | `bigint` | **yes** | `None` | **FK → `tenancy_organization.id`, CASCADE.** Nullable because some events legitimately precede a workspace (a login attempt, an email suppression) |
| `actor_id` | `bigint` | **yes** | `None` | **FK → `accounts_user.id`, `on_delete=SET_NULL`** — deleting a user must not delete the evidence |
| `action` | `varchar(64)` | no | — | truncated to 64 by `AuditLog.objects.record()` |
| `target_type` | `varchar(64)` | no | `''` | `Model._meta.label_lower` |
| `target_id` | `varchar(64)` | no | `''` | `str(pk)` — a string, so it can point at any model |
| `payload` | `jsonb` | no | `dict` | passed through `scrub_payload()` in `save()`: any key matching `code`/`otp`/`password`/`secret`/`token`/`api_key`… becomes `"[redacted]"`, recursively |
| `ip` | `inet` | **yes** | `None` | |
| `user_agent` | `varchar(400)` | no | `''` | |
| `created_at` | `timestamptz` | no | `auto_now_add` | |

**Indexes**
- `tenancy_auditlog_pkey`
- `tenancy_auditlog_org_id_430df6ae`, `tenancy_auditlog_actor_id_a75f60eb` — implicit FK indexes
- `auditlog_org_created_idx` — `(org_id, created_at DESC)`
- `auditlog_action_idx` — `(action, created_at DESC)`

**Constraints:** none beyond the FKs. Append-only is enforced in `save()`, same as
`ConsentRecord`.

The two FKs disagree on purpose, and the disagreement is the design: `org` CASCADEs (delete a
workspace, its audit trail goes with it — it is that workspace's data) while `actor` nulls
(delete a person, the record of what they did survives, attributed to nobody).

---

## 4. Four cross-cutting facts that are easy to get wrong

### 4.1 There is not a single `DEFAULT` clause in this schema

`\d+` shows an empty Default column for every table except the identity `id`, and `sqlmigrate`
confirms it: `CREATE TABLE "accounts_user" (… "is_active" boolean NOT NULL, …)` — no
`DEFAULT true`. Django applies `default=` **in Python, at instantiation**, and never emits it
as DDL.

The practical consequence: an `INSERT` issued outside the ORM — `psql`, a raw
`cursor.execute()`, a restore of a partial dump — **fails on every NOT NULL column** rather
than quietly defaulting. Nothing is broken; it just means the model file, not the database, is
the place to look up what a default is.

### 4.2 `on_delete` is enforced by Django, not by PostgreSQL

Every foreign key in this schema is emitted as:

```sql
FOREIGN KEY ("user_id") REFERENCES "accounts_user" ("id") DEFERRABLE INITIALLY DEFERRED
```

— with **no `ON DELETE` clause at all**. `CASCADE` and `SET_NULL` live in Django's deletion
collector, which issues the child `DELETE`/`UPDATE` statements itself before the parent
`DELETE`. So:

- `User.objects.get(...).delete()` through the ORM removes their `legal_consentrecord` rows and
  their `tenancy_membership` rows, and nulls `tenancy_auditlog.actor_id`. As designed.
- `DELETE FROM accounts_user WHERE id = 1;` in `psql` raises a foreign-key violation. It does
  **not** cascade, and it does not silently orphan anything.

`DEFERRABLE INITIALLY DEFERRED` is Django's default on PostgreSQL and is what lets it insert
rows in whatever order a fixture or a `bulk_create` produces, with the check happening at
`COMMIT`.

### 4.3 The `…_like` indexes are not duplicates

Every indexed or unique `CharField`/`EmailField`/`SlugField` gets a **second** btree index with
`varchar_pattern_ops` (`accounts_user_email_b2644a56_like`,
`tenancy_organization_slug_380107f5_like`, and four more). Django adds them on PostgreSQL
because the default `en_US.utf8` collation makes a plain btree unusable for
`LIKE 'prefix%'`; the `pattern_ops` operator class restores C-collation comparison semantics so
`__startswith` can use an index. They cost write throughput and disk, they are not redundant,
and they are not safe to drop.

### 4.4 `timestamptz`, `inet`, `jsonb`

- Every datetime is `timestamp with time zone`, because `USE_TZ = True` and `TIME_ZONE = "UTC"`.
- `GenericIPAddressField` maps to PostgreSQL's native `inet`, not `varchar` — so `request_ip`
  and `ip` are queryable as addresses (subnet containment, etc.) without casting.
- `JSONField` maps to `jsonb` (binary, indexable), not `json`.

None of these hold on the SQLite fallback used by the test suite, which is one reason
`deploy/README.md` insists that new query code be exercised against Postgres.

---

## 5. Why the non-obvious parts are shaped the way they are

### 5.1 `EmailOTP` stores a hash, and specifically an HMAC rather than a slow hash

The plaintext code is never written anywhere — not to the table, not to a log, not into an
audit payload. `accounts_emailotp.code_hash` is
`salted_hmac("findajob.otp:<email>", code, algorithm="sha256").hexdigest()`, and verification is
`constant_time_compare` against it.

Three separate decisions are folded into that one column:

**Why a hash at all.** A stolen database dump, a leaked backup, or read access through some
future SQL-injection bug would otherwise hand the attacker every live login code for every
address — a complete authentication bypass with no password to crack. Hashing reduces that to
"an attacker who has the dump *and* `SECRET_KEY`".

**Why the email is mixed into the key salt.** Without it, two people issued the same six-digit
code in the same ten-minute window get identical `code_hash` values, and a hash captured for one
address could be replayed against another. With it, the hash is bound to the address.

**Why HMAC and not PBKDF2/argon2 — which looks wrong and is not.** The keyspace is 10⁶. Anyone
holding both the database and `SECRET_KEY` can enumerate all one million candidates regardless
of how slow the hash is; a slow hash buys nothing against that attacker while costing ~300 ms
on *every* login verify. The control that actually protects a six-digit code is the
`attempts <= 5` per-row cap: five wrong guesses and the row is dead, which forces an attacker
back to the mailbox for a new code. That is why `otp_attempts_bounded` is a database CHECK and
not just application logic — it is the real security boundary, so it lives where nothing can
route around it.

Related: `MAX_LIVE_OTPS = 3`, and a resend does **not** supersede earlier codes. German mail
providers greylist and then deliver two codes seconds apart, and the user types the older one.
Verify iterates live rows newest-first, so all three are acceptable. That is why the schema has
no "one live code per address" unique constraint — there deliberately is not one.

### 5.2 The partial unique index on `LegalDocument.is_current`

```sql
CREATE UNIQUE INDEX "uniq_current_legaldocument"
    ON "legal_legaldocument" ("doc_id", "lang") WHERE "is_current";
```

Declared as `UniqueConstraint(fields=("doc_id","lang"), condition=Q(is_current=True))`.

The rule it enforces is **at most one live version per document per language**, and it has to be
a database constraint rather than application logic because of what depends on it. At
registration, the server resolves *"the current German AGB"* and writes its `content_hash` onto
a `ConsentRecord`. If two rows for `(terms, de)` ever carried `is_current=True`, the server
could not say which text a user agreed to — and that answer is the entire evidentiary value of
the consent ledger. A half-finished `seed_legal` run, an interrupted deploy, or someone ticking
the checkbox by hand in Django admin (`LegalDocumentAdmin` is editable, on purpose, so a typo
can be fixed in an emergency) are all realistic ways to produce that state. The index makes it
unrepresentable.

It is *partial* — `WHERE is_current` — because superseded versions must stay: every historic
`(doc_id, lang, version)` triple has to remain readable so a consent record from two rewrites
ago still resolves. A plain `UNIQUE (doc_id, lang)` would forbid keeping history at all.
The companion `uniq_legaldocument_version` UNIQUE `(doc_id, lang, version)` is the total one,
and stops the same version being seeded twice.

The constraint dictates the write order in `seed_legal`: inside one `transaction.atomic()`,
demote the existing current row (`.filter(is_current=True).update(is_current=False)`) **before**
promoting the new one. Promote-then-demote violates the index mid-transaction.

### 5.3 `ConsentRecord` is append-only — and why that is in `save()`, not in a constraint

```python
def save(self, **kwargs) -> None:
    if self.pk is not None and not self._state.adding:
        raise ValueError("ConsentRecord is append-only: …")
    if kwargs.get("force_update"):
        raise ValueError("ConsentRecord is append-only: force_update is refused.")
    kwargs["force_insert"] = True
    super().save(**kwargs)
```

A consent record is **evidence**, not state. Under GDPR the controller must be able to
demonstrate *that* consent was given, *when*, *to what exact text*, and *from where*. An
`UPDATE` destroys the "when" and the "to what". So revoking `send_on_behalf` does not flip a
boolean — it appends a second row with `granted=False`, and the current answer is
"the newest row for this `(user, type)`", which is exactly what `consent_user_type_idx`
`(user_id, type, created_at DESC)` is built to serve. `ConsentRecordQuerySet.latest_per_type()`
orders by `(created_at, id)` explicitly rather than relying on `Meta.ordering`, because several
rows are written inside one transaction with `created_at` values identical to the microsecond
and the id is the only remaining tiebreak.

`doc_version`, `content_hash` and `doc_lang` are **copied in at write time rather than being a
foreign key** for the same reason: the record has to survive the document row being superseded,
re-seeded or deleted. A snapshot is what makes it evidence; a pointer would make it a claim
about whatever the row says today.

Two limits stated plainly, because a document that overclaims is worse than one that does not:

- **This is enforced in Python, not in PostgreSQL.** A direct `UPDATE legal_consentrecord …` in
  `psql` succeeds. There is no `BEFORE UPDATE` trigger and no revocation of the table's UPDATE
  privilege. `apps/legal/admin.py` closes the obvious hole (`has_add_permission`,
  `has_change_permission` and `has_delete_permission` all return `False`, and every field is in
  `readonly_fields`) — but the guarantee is "the application cannot do it", not "the database
  will not allow it".
- **Deletion is deliberately *not* blocked.** `user_id` is `on_delete=CASCADE`, so erasing a
  user erases their consent history with them. That is Art. 17 working as intended, and it is
  the one operation that is allowed to remove a row.

The same `save()`-level append-only guard is on `AuditLog`, plus `scrub_payload()`, which
recursively redacts any key matching `code|otp|password|secret|token|api_key` before the JSON
reaches `jsonb`. Spec §4 forbids the OTP code from appearing in an audit payload; that function
is the backstop that makes it structurally hard rather than merely forbidden.

### 5.4 `OrgScopedQuerySet`, and how `base_manager_name = "all_objects"` keeps admin and cascades working

`Membership` and `AuditLog` are multi-tenant tables. The bug class that leaks one tenant's rows
into another tenant's response is almost always a **forgotten** filter, not a wrong one — so
`apps/tenancy/managers.py` makes the forgotten filter impossible instead of merely discouraged:

```python
Membership.objects.all()          # OrgScopeError: … queries must be org-scoped
Membership.objects.for_org(org)   # fine
Membership.objects.for_user(user) # fine
Membership.objects.unscoped()     # the deliberate, greppable escape hatch
```

The guard fires when the queryset is **evaluated**, not when it is built, which is what lets
`.create()` still work. It is installed on `_fetch_all` (covering iteration, `len()`, `bool()`,
`list()`, `.get()`, `.first()`, `.last()`, `.latest()`, `.in_bulk()` and every `values*()`
variant) plus `iterator()`, `count()`, `exists()`, `aggregate()`, `update()` and `delete()`
individually, because those last six never route through `_fetch_all`.

Reads are guarded; **writes are not** — `.create()` requires the caller to pass `org` anyway, so
there is nothing to forget.

Now the part that makes it survivable. Both models declare:

```python
class Meta:
    default_manager_name = "objects"      # the guarded OrgScopedManager
    base_manager_name    = "all_objects"  # a plain models.Manager
```

Django uses `_default_manager` and `_base_manager` for different things, and this split assigns
the guard to exactly the right one:

| Django uses… | for | with `base_manager_name="all_objects"` |
|---|---|---|
| `_default_manager` | `Model.objects`, reverse related managers (`org.memberships`) | **guarded** — `org.memberships.all()` raises, which is the intent |
| `_base_manager` | forward FK traversal (`membership.org`, `audit.actor`), the deletion collector's cascade queries, `refresh_from_db()` | **unguarded** — so none of these can raise |

Without `base_manager_name`, `Organization.delete()` would raise `OrgScopeError` from inside
Django's own deletion collector while it looked for the memberships to cascade, and
`audit.actor` would raise on plain attribute access. The FK relationships in §3.8 and §3.9 only
work because of that one line.

Two consequences worth knowing:

- **Django admin must use `all_objects` explicitly**, and does: all three `ModelAdmin` classes
  in `apps/tenancy/admin.py` override `get_queryset()` to return `Membership.all_objects…` /
  `AuditLog.all_objects…`, and `MembershipInline` does the same. Staff-only admin is one of the
  four legitimate escape hatches (admin, migrations, management commands, tests). **Do not
  "clean up" those calls** — the pages raise without them, and `/admin/` is now internet-exposed
  (`deploy/README.md` §2a), so the org guard is deliberately not the thing standing between a
  tenant and another tenant's data there. The gates are.
- **Migrations see a plain manager.** `OrgScopedManager.use_in_migrations = False`, and Django's
  autodetector substitutes a plain `Manager()` shim for whichever managers are the default and
  base ones. That is why `tenancy/migrations/0001_initial.py` records
  `managers=[('objects', Manager()), ('all_objects', Manager())]` — a data migration has no
  request and therefore no org to scope to, so it gets an unguarded `objects`.

`Organization` itself is **not** guarded (`OrganizationManager`): it is the scope root, so there
is nothing above it to scope to. Its `for_user()` helper exists as a convenience, not a gate.

---

## 6. The seven tables that are Django's own

| Table | Created by | What it holds here |
|---|---|---|
| `django_migrations` | the migration executor itself (`MigrationRecorder.ensure_schema()`), **not** by any migration | the 21 applied migrations. Editing it by hand is how you tell Django a migration "already ran" |
| `django_content_type` | `contenttypes.0001` | 13 rows — one per model (8 ours + `auth.Permission`, `auth.Group`, `contenttypes.ContentType`, `sessions.Session`, `admin.LogEntry`) |
| `auth_permission` | `auth.0001` | 52 rows = 13 content types × 4 (`add`/`change`/`delete`/`view`), generated by a post-migrate signal |
| `auth_group` | `auth.0001` | 0 rows — groups are unused; admin access is superuser-only |
| `auth_group_permissions` | `auth.0001` | 0 rows |
| `django_session` | `sessions.0001` | 3 rows. `SESSION_ENGINE = "django.contrib.sessions.backends.db"` — **sessions live in PostgreSQL, not Redis**, which is exactly why a Redis outage throttles logins loosely instead of logging everybody out |
| `django_admin_log` | `admin.0001` | 0 rows. `user_id` FKs to **`accounts_user`**, which is the visible fingerprint of the swapped user model |

**`auth_user` does not exist**, and neither do `auth_user_groups` / `auth_user_user_permissions`.
`AUTH_USER_MODEL = "accounts.User"` was set before the first `migrate`, so `auth.0001` created
only the permission and group tables. Restoring a dump taken from a project with the stock user
model into this database would not work.

---

## 7. Inspecting the live schema — reproduce every claim above

Everything runs from `/root/findajob/` on the VPS (`ssh macbook-air`). `-T` disables the
pseudo-TTY, which is what makes the output pipeable; drop it for an interactive `psql`.

### The table list (§2)

```bash
docker compose exec -T db psql -U findajob -d findajob -c '\dt'
```

### One table in full — columns, types, nullability, defaults, indexes, constraints, FKs (§3)

```bash
docker compose exec -T db psql -U findajob -d findajob -c '\d+ accounts_emailotp'
docker compose exec -T db psql -U findajob -d findajob -c '\d+ legal_legaldocument'
docker compose exec -T db psql -U findajob -d findajob -c '\d+ tenancy_membership'
# …and the other seven
```

Or interactively, where tab-completion works:

```bash
docker compose exec db psql -U findajob -d findajob
findajob=# \dt
findajob=# \d+ tenancy_auditlog
findajob=# \di                 -- every index
findajob=# \q
```

### Every constraint and index at once, without going table by table (§3, §4.2, §4.3)

```bash
docker compose exec -T db psql -U findajob -d findajob -c "
SELECT conrelid::regclass AS tbl, conname, pg_get_constraintdef(oid) AS def
FROM   pg_constraint
WHERE  connamespace = 'public'::regnamespace
ORDER  BY 1, 2;"

docker compose exec -T db psql -U findajob -d findajob -c "
SELECT tablename, indexname, indexdef
FROM   pg_indexes WHERE schemaname = 'public' ORDER BY 1, 2;"
```

The FK rows in the first query are what prove §4.2: every one reads
`FOREIGN KEY (…) REFERENCES … DEFERRABLE INITIALLY DEFERRED` with **no `ON DELETE`**.
The second is where `uniq_current_legaldocument … WHERE is_current` (§5.2) and the `…_like`
`varchar_pattern_ops` indexes (§4.3) appear.

### The migration ledger and its ordering (§1)

```bash
docker compose exec -T db psql -U findajob -d findajob -c \
  'SELECT id, app, name, applied FROM django_migrations ORDER BY id;'

docker compose run --rm -e DJANGO_COLLECTSTATIC=0 api python manage.py showmigrations
```

### The exact DDL a migration emits, without running it (§1, §4.1, §4.2)

`sqlmigrate` renders the SQL against the configured backend and prints it — it does not execute
anything:

```bash
docker compose run --rm -e DJANGO_COLLECTSTATIC=0 api python manage.py sqlmigrate accounts 0001
docker compose run --rm -e DJANGO_COLLECTSTATIC=0 api python manage.py sqlmigrate legal    0001
docker compose run --rm -e DJANGO_COLLECTSTATIC=0 api python manage.py sqlmigrate tenancy  0001
```

This is the single best check for §4.1 (grep the output for `DEFAULT` — the only match is
`GENERATED BY DEFAULT AS IDENTITY`) and §4.2 (grep for `ON DELETE` — no matches).

### Is the database still in sync with the models?

```bash
docker compose run --rm -e DJANGO_COLLECTSTATIC=0 api python manage.py makemigrations --check --dry-run
# -> "No changes detected", exit 0.  Anything else means the models moved without a migration.

docker compose run --rm -e DJANGO_COLLECTSTATIC=0 api python manage.py migrate --plan
# -> "Planned operations:" / "  No planned migration operations."
```

### Row counts, server version, encoding (§1)

```bash
docker compose exec -T db psql -U findajob -d findajob -c \
  'SELECT relname, n_live_tup FROM pg_stat_user_tables ORDER BY relname;'
docker compose exec -T db psql -U findajob -d findajob -Atc 'SELECT version();'
docker compose exec -T db psql -U findajob -d findajob -Atc \
  'SELECT datcollate, datctype FROM pg_database WHERE datname = current_database();'
```

`n_live_tup` is a planner estimate, not an exact count — `SELECT count(*)` if the number has to
be exact.

### Backing it up before you change anything

```bash
docker compose exec -T db pg_dump -U findajob -d findajob --no-owner \
  > /root/backups/findajob-$(date +%Y%m%d_%H%M).sql
```

**Never run `docker compose down -v` on this stack.** `-v` deletes `findajob_pg_data` and
`findajob_media_data`. (`findajob_static_data` is the one volume that is safe to delete — it is
`collectstatic` output and the api entrypoint rebuilds it on the next start.)

---

## 8. What this schema does **not** contain

Stated so nobody goes looking. Build-order positions 5+ are not built, and nothing in
`INSTALLED_APPS` may import from them:

- No `Invitation` — invitation acceptance is a separate token-authenticated endpoint that ships
  with `org-members`. `bootstrap_workspace()` never consults one, because auto-accepting on
  login is a cross-tenant account-capture hole.
- No `Subscription`, no `UsageQuota` — `Organization.plan` / `.cycle` are the placeholder.
- No job, application, document or platform-adapter tables yet. The `/api/v1/me/` response is
  additive-only by design (`usage` and `flags` arrive later), so the shell never breaks.
- `apps.automation` is in `INSTALLED_APPS` and is **model-less** — read-only visibility into the
  n8n layer, zero migrations, zero tables.
