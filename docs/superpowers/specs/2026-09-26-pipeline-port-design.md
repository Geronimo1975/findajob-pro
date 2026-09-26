# Portarea pipeline-ului Job_app în findajob — design

Data: 2026-09-26. Stare: propus, așteaptă review.

## 1. Scop și intenție

Job_app (`~/Projects/Job_app`) este un pipeline CLI pentru o singură persoană: scrape → prescore
→ analiză AI → Anschreiben + PDF → trimitere SMTP, cu Google Sheet ca sursă de adevăr. findajob
(acest repo, findajob.pro) este un SaaS multi-tenant care azi are doar login, listă de joburi
din feed-uri și conectarea unui mailbox. Interfața promite generare, documente, profil, trimitere
și skill-match, dar niciun endpoint din acestea nu există (`web/api.js` ENDPOINTS vs
`config/urls.py`).

**Rezultat urmărit:** orice utilizator înscris pe findajob.pro obține același flux pe care îl are
operatorul în Job_app, dar per cont: profilul lui, documentele lui, scrisoarea generată pentru
el, trimisă din mailbox-ul lui. Succes = un utilizator nou poate, fără intervenția operatorului:
(1) completa profilul, (2) încărca CV-ul, (3) vedea joburile ordonate după potrivire, (4) genera
email + Anschreiben + PDF pentru un job, (5) trimite din propriul mailbox pe platformele
low-risk sau copia textul pe cele semi-manuale, (6) vedea istoricul.

**Ce a spus utilizatorul:** „fă migrarea către Find a Job". **Presupuneri (de confirmat):**
- Numele produsului rămâne cel al repo-ului (findajob / findajob.pro); nu se redenumește nimic.
- Cheia Anthropic e a platformei (o singură cheie în env), nu per utilizator. Modelul
  `Integration` cu cheie per user din backlog-ul `findajob-architecture` rămâne neimplementat.
- Scraping-ul LinkedIn/Indeed din Job_app **nu se portează** — tabela de risc din
  `findajob-architecture` îl interzice, iar CLAUDE.md o numește regulă dură.

## 2. Ce se portează și ce nu

| Modul Job_app | Decizie | Unde ajunge |
|---|---|---|
| `filters/relevance.py` (prescore determinist) | **port, ca funcție pură** | `apps/jobs/matching.py`; configul vine din `SearchProfile`, nu din YAML |
| `agents/analyzer_agent.py` (prompt + schema JSON, fallback keyword) | **port** | `apps/jobs/ai.py` |
| `generators/cover_letter_gen.py` (prompturi DE/EN, template fallback) | **port** | `apps/jobs/ai.py`; identitatea hardcodată („George…") dispare, vine din `UserProfile` |
| `generators/email_body_gen.py` (template-uri + paragraf personalizat) | **port** | `apps/jobs/generation.py`; template-urile devin constante Python, nu fișiere pe disc |
| `generators/pdf_gen.py` (fpdf2 + pypdf, `_latin1_safe`) | **port, pe BytesIO** | `apps/jobs/pdf.py` |
| `sender/smtp_sender.build_email` (MIME cu atașamente) | **port** | `apps/tenancy/outbox/services.py` |
| `sender/smtp_sender` rate-limit pe fișier + `getpass` | **nu**; înlocuit cu `EmailSendLog` în DB | — |
| `agents/base_agent.cv_path_for`, `package_is_stale` | **nu**; CV-ul e `Document(kind=cv)` ales pe limbă la generare, pachetul combinat nu se cache-uiește | — |
| `scrapers/jobspy_scraper.py`, `scrapers/base.Market` | **nu** (LinkedIn/Indeed interzise; contractul `BaseAdapter` e altul) | — |
| n8n, Google Sheet, `store/seen.py`, Excel tracker | **nu**; DB-ul e sursa de adevăr | — |
| `scripts/`, VPS `job-webhook`, Apps Script | **nu** | — |

Ce pierde utilizatorul față de Job_app: piețele/keyword-urile pe 9 țări prin JobSpy. Sursa de
joburi rămâne registrul de adaptoare (RemoteOK, WWR azi). Extinderea surselor (ex. API-ul public
Arbeitsagentur, low-risk) e un proiect separat, nu parte din acest spec.

## 3. Arhitectură

Trei unități noi, în ordinea dependențelor:

1. **`apps.profiles`** (app nou) — `UserProfile` și `SearchProfile`, org-scoped, una per
   membership. Înlocuiesc blocurile `applicant`, `skills` și `scraping.filters` din
   `settings.yaml`.
2. **`apps.jobs`** (extins) — matching determinist, analiză AI, generare text, PDF, API pentru
   documente și generare. `Application` primește câmpurile de analiză.
3. **`apps.tenancy.outbox`** (extins) — `EmailSendLog`, `send_application()`, endpoint-ul
   `POST /applications/<id>/send/` exact cum îl descrie `docs/2026-08-02-outbox-spec.md`.

Fluxul de date pentru un job:

```
JobListing (global, din adaptor)
   │  GET /jobs/           → match_score determinist per SearchProfile (ieftin, la citire)
   ▼
POST /jobs/<id>/generate/  → Application(status=RUNNING→REVIEW)
   │   1. analiză AI (match_score, matching_skills, missing_skills, language, seniority)
   │   2. email body (template + paragraf personalizat) + Anschreiben (AI, fallback template)
   │   3. PDF Anschreiben → Document(kind=cover_letter, generated=True)
   │   4. suggested documents: CV pe limbă + certificate
   ▼
utilizatorul editează în drawer (UI existent)
   │
POST /applications/<id>/send/  → EmailSendLog, SMTP din MailboxConnection, status=SENT
       sau {sent_manually:true}  → status=SENT fără trimitere (platforme semi-manuale)
```

**Sincron, nu coadă.** Compose declară Redis „never a queue" și nu există worker. Generarea
face două apeluri AI (analiză ~800 tokens, scrisoare ~2000) și rulează sincron în request,
cu timeout per apel de 25 s și buget total 60 s; gunicorn are timeout-ul ridicat la 90 s
pentru ruta asta (setare globală, documentată în deploy). Trimiterea SMTP e sincronă cu
buget 20 s (atașamentele cântăresc). Dacă volumul o cere, migrarea la Django Tasks cu un
worker e un spec ulterior; contractul API nu se schimbă (răspunsul rămâne obiectul
Application, nu un job id).

## 4. Modele

### `apps.profiles`

`UserProfile` (org, user; unique împreună): `full_name`, `headline`, `phone`, `location`,
`linkedin_url`, `github_url`, `years_experience` (int, null), `tone` (formal/friendly/direct),
`summary` (text), `preferred_language` (de/en). Numele și emailul din `User` nu se dublează:
emailul de contact e `user.email`.

`SearchProfile` (org, user; unique împreună): `skills_primary` JSON list, `skills_secondary`
JSON list, `keyword_groups` JSON list[list[str]] (bonus titlu, ca la piețele Job_app),
`negative_keywords` JSON list, `exclude_keywords` JSON list, `flex_signals` JSON list,
`min_prescore` int (default 30). `to_matching_config()` întoarce dict-ul în forma pe care o
așteaptă `prescore()`, ca portarea funcției să fie 1:1 și testată cu aceleași cazuri.

Ambele folosesc `OrgScopedManager(user_field="user")` + `all_objects`, ca `Document`.
Se creează lazy la primul `GET /profile/` (goale), nu în `bootstrap_workspace` — funcția aia e
testată să nu crească.

### `apps.jobs`

`Application` primește: `analysis` JSON (schema din §5), `match_score` SmallInt null,
`language` (de/en, din analiză), `subject` există deja. `generated_content` = body-ul
emailului, `generated_cover_letter` = textul Anschreiben. Nu se adaugă `suggested_documents`:
M2M-ul `documents` existent se populează la generare cu sugestiile, iar utilizatorul îl
editează înainte de send.

`Document` primește `generated` Bool (default False) și `language` (de/en/"" ); PDF-ul
Anschreiben e un `Document(kind=cover_letter, generated=True)` legat de aplicație prin
`ApplicationDocument` la poziția 0. `ats_score` rămâne null (nu e în scop).

### `apps.tenancy.outbox`

`EmailSendLog` conform spec-ului outbox §Phase 3: org, user, application FK, mailbox FK,
to_email, subject, status (queued/sent/failed), error_code, error_detail, attempts,
created_at, sent_at. Rata zilnică = `count(status=sent, user, created_at::date=today)`,
plafon din `Organization.plan` (free 5, pro 30, max/team 100) — înlocuiește fișierul
`email_rate_state.json`.

## 5. Module și contracte

`apps/jobs/matching.py` — `prescore(job: dict, config: dict, search_keywords) -> (int,
list[str])` și `skill_tokens()` copiate din `filters/relevance.py` cu testele lor; plus
`skill_match(job_listing, search_profile) -> {score, matched, missing}` (tokenizare pe
title+description+tags+requirements, zero-guard).

`apps/jobs/ai.py` — `analyze_job(listing, profile, search) -> Analysis` și
`write_cover_letter(listing, profile, analysis, *, language) -> str`. Client Anthropic creat
din `settings.ANTHROPIC_API_KEY`; model din `settings.ANTHROPIC_MODEL` (default
`claude-sonnet-4-6`). Fără cheie: `analyze_job` cade pe keyword match (`method="keyword"`),
`write_cover_letter` pe template; ambele loghează un avertisment, nu ridică. Schema
`Analysis`: `match_score, matching_skills, missing_skills, key_requirements, language,
seniority, summary, method`.

`apps/jobs/generation.py` — `generate_application(listing, membership) -> Application`:
orchestrarea din §3, într-o tranzacție; ton per platformă din tabela de risc (feed/email →
email formal + Anschreiben; linkedin → notă scurtă fără scrisoare; wellfound → mesaj către
fondator fără scrisoare). Salutul german pe gen și template-urile DE/EN formal/generic devin
constante în modul.

`apps/jobs/pdf.py` — `text_to_pdf(text) -> bytes`, `merge_pdfs(list[bytes]) -> bytes`,
`_latin1_safe`. Fără căi pe disc.

`apps/tenancy/outbox/services.py` — `build_message(from_, to, subject, body, attachments:
list[(filename, bytes, mimetype)], reply_to)` (port din `build_email`) și
`send_application(application, *, subject, body, cover_letter, document_ids, notes,
sent_manually) -> EmailSendLog`, cu ordinea de erori din spec-ul outbox: 409 `wrong_status`,
409 `auto_send_forbidden`, 409 `mailbox_not_connected`, 422 `missing_apply_email`, 429
`daily_send_limit`, 502 `send_failed`.

## 6. API (toate sub `/api/v1/`, sesiune + `IsOrgMember`)

| Metodă | Rută | Răspuns |
|---|---|---|
| GET/PUT | `profile/` | `{profile: UserProfile, search: SearchProfile}`; PUT parțial |
| GET | `jobs/` | ca azi, plus `match_score` și `status`-ul aplicației userului |
| GET | `jobs/<id>/skill-match/` | `{score, matched, missing}` |
| POST | `jobs/<id>/generate/` | 201 Application; a doua oară regenerează (200) dacă status ∈ {pending, review, failed}; 409 `wrong_status` altfel |
| GET | `applications/` | istoricul userului |
| GET/PATCH | `applications/<id>/` | editările din drawer (subject, generated_content, generated_cover_letter, documents) |
| POST | `applications/<id>/send/` | 202 `{send_log_id, status}`; corp `{subject?, body?, cover_letter?, document_ids?, notes?, sent_manually}` |
| GET/POST | `documents/` | listă / upload multipart (`kind`, `name`, `language`, `tags`); 5 MB, doar PDF |
| DELETE | `documents/<id>/` | 204; 409 dacă e atașat unei aplicații trimise |
| GET | `documents/<id>/download/` | fișierul, verificat pe org (nginx nu servește `/media`) |

Serializarea Application pentru UI expune **și** aliasurile `message` / `cover_letter`
(read-only) ca să nu se rescrie drawer-ul acum; sursa de adevăr rămân numele din model.
Toate erorile păstrează forma `{"error": {code, detail}}`.

## 7. Frontend (`web/app.html`, `web/api.js`)

Se înlocuiesc cele patru simulări client-side cu apeluri reale, păstrând ecranele:
`startJob` → `POST generate` (cele 3 faze devin un singur spinner), `skillMatch` →
`GET skill-match` (cu fallback local doar în demo), `send` → `POST send`, `onUpload`/
`runGenerate` din Documents → upload real (Canva „generate" dispare), Profile → `GET/PUT
profile/`. Utilizatorii anonimi și `?demo=1` rămân pe fixture-uri. Nicio origine terță nouă.

## 8. Dependențe, config, deploy

`requirements.txt` (allowlist explicit, decizie aici): `anthropic`, `fpdf2`, `pypdf`, pinate la
versiunile din `Job_app/.venv` (0.111.0 / 2.8.7 / 6.14.2). Env nou: `ANTHROPIC_API_KEY`
(opțional; lipsa lui = fallback-uri), `ANTHROPIC_MODEL`. `docker-compose.yml`: timeout gunicorn
90 s. Nginx: `client_max_body_size 6m` pe `/api/v1/documents/`. `verify_platform` primește un
modul `pipeline` care verifică doar că rutele răspund 401 (nu consumă AI).

## 9. Erori și limite

- AI indisponibil/expirat: generarea reușește cu fallback și `analysis.method="keyword"`;
  UI-ul arată eticheta. Nu se blochează utilizatorul.
- `_latin1_safe` transliterează; caractere ne-Latin-1 rămân aproximate în PDF (limitare
  moștenită, documentată).
- CV lipsă la generare: aplicația se creează, `documents` rămâne gol, răspunsul poartă
  `warnings: ["no_cv"]`; `send` refuză cu 422 `missing_cv` dacă platforma e email.
- Documentele generate ale unei aplicații se șterg odată cu aplicația; cele încărcate nu.

## 10. Testare

`django.test.TestCase` ca restul repo-ului, în `backend/tests/{profiles,jobs,outbox}/`:
- `matching`: cazurile din `Job_app/tests/test_relevance.py` portate 1:1 pe config din
  `SearchProfile.to_matching_config()`.
- `ai`: client Anthropic mock-uit; fallback la lipsă cheie, la JSON invalid, la timeout.
- `generation`: ton per platformă, alegerea CV-ului pe limbă, tranzacție la eșec PDF.
- API: izolare cross-org pentru fiecare rută nouă (ca `TenantIsolation` din outbox), ordinea
  codurilor de eroare la send, upload > 5 MB, non-PDF, download străin → 404.
- `pdf`: text cu diacritice → PDF valid cu strat de text (`pypdf` extrage textul înapoi).

## 11. În afara scopului

Adaptoare noi de surse, OAuth Gmail/Outlook, coadă/worker, cheie AI per utilizator, ATS
score, billing/Stripe, redenumirea produsului.
