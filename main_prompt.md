
# PROMPT: Platformă de Job Tracking & Auto-Aplicare (AI-Assisted) — Arhitectură Modulară pe Platforme

> Prompt de arhitectură pentru generarea aplicației (Claude Code / alt tool AI-assisted dev).

## 1. Context & Obiectiv

Aplicație web internă, acces restricționat (login), care agregă joburi din **module separate per platformă sursă** (LinkedIn, RemoteOK, We Work Remotely, Wellfound, Indeed etc.), fiecare cu propria logică de extragere și aplicare — pentru că fiecare platformă are particularități diferite de autentificare, format de date și mecanism de aplicare. Un **core comun** gestionează AI-ul, documentele și dashboard-ul agregat.

---

## 2. Stack tehnic recomandat

- **Backend:** Django 5.x + DRF, arhitectură de tip **plugin/adapter pattern** (fiecare platformă = un modul Django app separat, ex. `platforms/linkedin/`, `platforms/remoteok/`, `platforms/wwr/`)
- **Frontend:** HTMX + Alpine.js — tab-uri per platformă, actualizări parțiale pe status
- **Task queue:** Celery + Redis — un set de task-uri separate per modul (ușor de rulat/debugat individual)
- **Browser automation** (doar acolo unde e strict necesar, ex. LinkedIn): Playwright, sesiune izolată, cu avertismentele de la secțiunea 6
- **DB:** PostgreSQL
- **Orchestrare:** n8n — **workflow separat per platformă**, nu unul monolitic (mai ușor de întreținut, de reluat la eșec, de dezactivat individual dacă o sursă își schimbă structura)
- **AI:** Anthropic API pentru generare conținut
- **Storage documente:** local/R2

---

## 3. Arhitectură modulară — Platform Adapter Pattern

**Core comun (shared, independent de sursă):**

- `UserProfile` — profilul tău master (experiență, skill-uri, ton preferat)
- `DocumentLibrary` — CV-uri, certificate, diplome, tag-uri
- `AI Engine` — serviciu unic de generare email + cover letter, apelat de orice modul
- `JobListing` (model normalizat) — schema comună în care fiecare modul își "traduce" datele
- `Dashboard` — agregă toate modulele, dar permite view individual per platformă

**Fiecare modul de platformă expune un contract comun:**

| Componentă       | Descriere                                                         |
| ----------------- | ----------------------------------------------------------------- |
| connector_type    | api / rss / scraper_html / browser_automation                     |
| auth_method       | none / api_key / cookie_session / login_form                      |
| apply_mechanism   | email / external_redirect / in_platform_form / message_to_founder |
| rate_limit        | config proprie (delay, cote zilnice)                              |
| field_mapping     | cum se traduc câmpurile brute în JobListing normalizat          |
| particularități | note specifice (vezi secțiunea 4)                                |

Acest contract permite adăugarea unei platforme noi fără să afectezi restul sistemului — fiecare modul e izolat, testabil și dezactivabil individual.

---

## 4. Module per platformă (particularități + recomandări)

### 4.1 LinkedIn

- **Connector:** browser automation (nu are API public de job search/apply pentru terți)
- **Apply mechanism:** Easy Apply (in-platform) sau redirect extern
- **Risc major:** automatizarea acțiunilor pe LinkedIn (scraping + auto-apply) încalcă explicit User Agreement-ul lor, iar sistemele lor anti-bot detectează activ pattern-uri automate, cu risc real de suspendare/ban de cont, inclusiv cont personal.
- **Recomandare:** modul **semi-automat** — extragi manual/asistat datele jobului (paste link sau copy-paste descriere), AI-ul generează mesajul/cover letter, dar trimiterea/Easy Apply rămâne acțiune manuală, făcută de tine, logat normal în browser. Nu automatiza click-ul de submit pe LinkedIn.

### 4.2 We Work Remotely / RemoteOK / Himalayas / Remotive

- **Connector:** RSS/JSON feed oficial (toate patru au)
- **Apply mechanism:** de obicei email sau link extern către site-ul companiei
- **Particularitate:** date structurate curat, fără risc de ban — cel mai sigur grup pentru automatizare completă (scraping + generare + trimitere email automată, dacă alegi asta).

### 4.3 Wellfound (AngelList)

- **Connector:** are API limitat / necesită scraping asistat de sesiune (login)
- **Apply mechanism:** de multe ori presupune mesaj direct către founder, nu doar CV, deci AI-ul trebuie să genereze un mesaj mai personal/conversațional, nu un email formal clasic
- **Particularitate:** matching bazat pe profil — recomandat flux semi-manual, cu review obligatoriu.

### 4.4 Indeed

- **Connector:** Indeed interzice explicit scraping-ul și acționează activ legal împotriva lui; folosește în schimb Indeed Publisher/XML feed oficial dacă ai acces, sau tratează sursa ca input manual (copy-paste link).
- **Apply mechanism:** Indeed Apply (quick apply) sau extern.

### 4.5 Arc, JustRemote, Contra, NoDesk, JavaScript Jobs — grup generic

- Tratează-le ca modul generic „RSS/simple listing", folosind același template ca 4.2, adaptat per structura fiecărui feed.

**Notă generală:** fiecare modul nou (inclusiv unul viitor, ex. Toptal) se construiește pornind de la template-ul din secțiunea 3, completând cele 5 câmpuri de contract — nu se scrie logică nouă de la zero.

---

## 5. Dashboard — vizualizare per modul + agregat

- **Tab-uri în UI, câte unul per platformă** (LinkedIn / We Work Remotely / RemoteOK / Wellfound / Indeed / Altele), fiecare cu propriul tabel de status.
- **Tab „All"** — vedere agregată din toate modulele, cu coloană „Sursă" vizibilă.
- Fiecare modul poate avea coloane suplimentare specifice:
  - LinkedIn: „Easy Apply Y/N", „Conexiune existentă Y/N"
  - Wellfound: „Mesaj către founder trimis Y/N"
  - We Work Remotely/RemoteOK: „Email destinatar"
- Status workflow rămâne comun tuturor modulelor: Pending → Start → Running → Sent/Done (verde), dar pentru LinkedIn/Wellfound, „Sent/Done" e marcat manual de tine după ce ai dat efectiv click, nu automat de sistem.

---

## 6. Generare AI conținut — adaptată per platformă

Motorul AI e comun, dar tonul și formatul output-ului diferă per modul:

- We Work Remotely / RemoteOK / Indeed → email formal clasic + cover letter standard
- LinkedIn → mesaj scurt, conversațional, potrivit pentru Easy Apply note sau InMail
- Wellfound → mesaj personal către founder, menționează motivația specifică pentru startup

Prompt-ul trimis către Claude API include: descrierea jobului, cerințele, profilul tău, și platforma sursă, ca modelul să adapteze automat registrul de limbaj.

Pas de review manual rămâne obligatoriu pentru toate modulele înainte de confirmare finală.

---

## 7. Documente (CV, certificate, diplome)

Neschimbat față de versiunea anterioară — mapare automată pe baza skill-urilor cerute, cu override manual. Se aplică identic indiferent de modul/platformă.

---

## 8. Automatizare n8n — per platformă

- Un workflow n8n separat pentru fiecare modul (n8n: LinkedIn scraping check, n8n: RemoteOK RSS pull, etc.) — dacă o sursă își schimbă structura sau pică, dezactivezi doar acel workflow, restul funcționează neafectat.
- Workflow-uri comune (nu per platformă): digest zilnic agregat, notificare Slack la match de skill-uri.

---

## 9. Schema bază de date (conceptual)

- `Platform` — id, name, connector_type, auth_method, apply_mechanism, rate_limit_config, is_active
- `JobListing` — platform_fk, title, company, url, description, requirements_tags, salary, scraped_at, status, platform_specific_data (JSON, pentru câmpurile particulare per modul, ex. easy_apply: true)
- `Application` — job_listing_fk, generated_content (email/mesaj), generated_cover_letter, status, sent_manually (bool), created_at, sent_at
- `Document` / `ApplicationDocument` — neschimbate
- `UserProfile` — neschimbat

---

## 10. Securitate & etică — pe platformă

| Platformă                                         | Risc automatizare                        | Recomandare                              |
| -------------------------------------------------- | ---------------------------------------- | ---------------------------------------- |
| LinkedIn                                           | Ridicat (ban cont)                       | Semi-manual, trimitere doar manuală     |
| Indeed                                             | Ridicat (acțiune legală anti-scraping) | Folosește feed oficial sau input manual |
| Wellfound                                          | Mediu                                    | Semi-manual, mesaj personalizat          |
| We Work Remotely / RemoteOK / Himalayas / Remotive | Scăzut                                  | Automatizare completă OK (feed oficial) |

Documente cu date personale (CV, diplome) — criptare la rest, acces restricționat, fără indexare publică.

---

## 11. Flux utilizator (rezumat)

1. Fiecare modul își aduce joburile independent (Celery/n8n separat per sursă) → Pending.
2. Navighezi pe tab-ul platformei relevante sau pe „All".
3. Apeși Start → AI generează conținut adaptat platformei + documente sugerate.
4. Review manual.
5. Confirmi trimiterea — automată (surse cu risc scăzut) sau manuală (LinkedIn, Wellfound, Indeed) → Sent/Done.
6. Istoric agregat, filtrabil pe platformă/status/dată.

---

## 12. Referințe open-source (repo-uri GitHub de inspecționat ca șabloane)

### 12.1 Generare CV / Resume Builder

- **Reactive Resume** (rxresu.me, self-hostable Docker, MIT) — cel mai popular resume builder open-source, multiple template-uri, editor vizual, export PDF.
- **JSON Resume** (jsonresume.org) — standard deschis pentru date de CV în format JSON + zeci de teme community-built; util ca schemă pentru `UserProfile` normalizat.
- **RenderCV** — "CV as code": conținut separat de design, generează PDF profesional automat din YAML/JSON, 17k+ stars.
- **Resumake / latexresu.me** — generează CV-uri LaTeX din interfață web, output foarte curat, potrivit dacă vrei variantă "tehnică" de CV.

### 12.2 ATS Scanner / Scoring

- **sunnypatell/ats-screener** — <cite index="24-1">simulează cum 6 platforme ATS enterprise reale (Workday, Taleo, iCIMS, Greenhouse, Lever, SuccessFactors) parsează și scorează un CV</cite>; parsare complet în browser, fără upload pe server — model bun de urmat pentru privacy.
- **Hashsharma/ATS-Resume-Checker** — <cite index="26-1">parsează CV-ul și job description-ul cu Python, extrage keyword-uri relevante folosind ML, oferă sugestii de aliniere.</cite>
- Topicuri GitHub cu zeci de variante similare: `ats-checker`, `ats-score-checker`, `ats-resume-checker` (majoritatea Python + Streamlit/FastAPI + OpenAI/Gemini/Groq).

### 12.3 Generare Cover Letter (LLM-based)

- **JensBender/chatgpt-cover-letter-generator** — <cite index="43-1">extrage automat descrierea jobului direct din URL-ul anunțului (Requests + BeautifulSoup), identifică cerințele/task-urile via OpenAI structured outputs validate cu Pydantic, generează 3 variante de cover letter, apoi le rafinează și le consolidează într-una finală</cite>; <cite index="43-1">suportă generare în engleză și germană.</cite> — foarte aproape de fluxul tău RO/EN/DE.
- **talqadi7/CoverLetterGenerator** — <cite index="42-1">Flask + LangChain + OpenAI, primește nume companie/job title/job description, generează scrisoare folosind un istoric de CV-uri și scrisori anterioare ca bază de referință.</cite>
- **Dheeru66k/Cover_Letter_Generator_LLM** — <cite index="47-1">folosește modelul Llama pentru generare, cu extragere automată a rolului/cerințelor din descrierea jobului via NLP, gândit pentru integrare ușoară în alte workflow-uri de automatizare.</cite>

### 12.4 Pipeline complet (scraping + CV + cover letter + auto-apply)

- **AIHawk / Auto_Jobs_Applier_AIHawk** (feder-cr, 30k+ stars) — <cite index="41-1">se conectează la LinkedIn, citește CV-ul și preferințele, aplică automat la joburi Easy Apply folosind AI pentru cover letter-uri și răspunsuri la întrebările din formular.</cite> Cel mai complet ca scop față de viziunea ta, dar confirmă riscurile discutate deja: <cite index="37-1">arhitectura de bază rămâne open-source, însă din motive de copyright plugin-urile pentru provideri terți au fost eliminate din repo,</cite> iar proiectul <cite index="38-1">este dezvoltat exclusiv în scop educațional — utilizatorii trebuie să respecte Termenii de Serviciu ai LinkedIn.</cite> Util ca **referință de arhitectură** (module de parsing, generare CV dinamic, personalizare AI), nu neapărat de rulat 1:1 pe LinkedIn.

### 12.5 Cum le folosești ca șabloane în platforma ta

- **CV/cover letter:** preia schema `JSON Resume` ca model pentru `UserProfile`, iar logica de generare din `chatgpt-cover-letter-generator` (extract → draft → refine → consolidate) ca pattern pentru task-ul Celery de generare conținut.
- **ATS scoring:** poți adăuga un modul opțional de scoring (inspirat din `ats-screener`) care rulează înainte de `Start`, ca semnal suplimentar pe cardul din Kanban ("Match Score" — vezi și Seekario/Jobright din research-ul anterior).
- **Auto-apply:** păstrează arhitectura modulară per platformă (secțiunea 3-4), dar inspectează AIHawk doar pentru partea de parsing/generare, nu pentru submit automat pe LinkedIn.

---

## 13. Top 20 funcționalități recomandate (sinteză din research)

Sintetizat din toate tool-urile analizate: Oaki, LazyApply, Simplify, LoopCV, JobCopilot, Sonara, AIApply, Jobright, Tracker.cv, Seekario, Careerflow.ai, Teal, AIHawk, plus repo-urile open-source (ats-screener, ATS-Resume-Checker, chatgpt-cover-letter-generator).

### A. Descoperire & Matching

1. **Agregare multi-sursă modulară** — un conector dedicat per platformă, izolat (arhitectura ta din secțiunea 3-4).
2. **Match Score AI per job** — scor de compatibilitate semantic (nu doar keyword matching), afișat direct pe cardul din Kanban (Seekario, Jobright).
3. **Analiză skill-gap** — ce cerințe din anunț nu sunt acoperite de profilul tău, afișat înainte de Start (Jobright).
4. **Extensie browser universală "Save Job"** — salvează orice job de pe orice site cu un click, parsare automată (Seekario, Careerflow.ai).

### B. CV & ATS

5. **Tailoring AI al CV-ului per job** — nu document generic, ci variantă adaptată la fiecare anunț (diferențiatorul central Oaki).
6. **Simulare scor ATS multi-motor** — testează CV-ul înainte de trimitere pe simulări ale Workday, Taleo, iCIMS, Greenhouse, Lever, SuccessFactors (pattern din `ats-screener`).
7. **Detectare keyword-gap** — compară CV vs. cerințele jobului, sugerează aliniere (`ATS-Resume-Checker`).
8. **Multi-versiuni CV** — RO/EN/DE, tehnic vs. management, selecție automată per platformă/companie (deja în arhitectura ta, Modul 4).
9. **A/B testing CV** — trimite variante diferite, tracking performanță, optimizare pe date reale (LoopCV).
10. **Teme/format-uri CV multiple** — inclusiv variantă cu logo-ul companiei integrat (Oaki, 3 teme dinamice).

### C. Cover Letter & Conținut personalizat

11. **Pattern de generare extract → draft → refine → consolidate** — 3 variante generate, apoi unificate într-una finală (`chatgpt-cover-letter-generator`).
12. **Ton adaptat per platformă** — email formal vs. mesaj scurt LinkedIn vs. mesaj personal către founder pe Wellfound (deja definit, secțiunea 6).
13. **Bibliotecă de răspunsuri salvate** — pentru întrebările repetitive de screening din formulare (Simplify).
14. **Referință istorică de stil** — folosește CV-uri/scrisori anterioare trimise ca bază de ton pentru cele noi (`CoverLetterGenerator`).

### D. Workflow & Siguranță

15. **Auto-fill fără auto-submit** pe platformele cu risc de ban (LinkedIn, Indeed) — pattern Simplify, deja parte din arhitectura ta modulară.
16. **Rate-limiting + comportament human-like** pentru orice modul de browser automation.
17. **Listă de excludere companii** — nu aplica niciodată la anumite firme (LoopCV).
18. **Limite zilnice/săptămânale de aplicări per platformă**, cu throttling configurabil (JobCopilot).

### E. Analytics & Networking

19. **Detectare conexiuni/referral** — scanează rețeaua ta LinkedIn pentru contacte la compania țintă, înainte de Start (Jobright).
20. **Dashboard analytics pe funnel** — rata de conversie trimis → răspuns → interviu, per platformă și per versiune de CV (combină analytics LoopCV + tracking generic).

**Prioritizare recomandată pentru MVP:** 1, 5, 8, 12, 15, 18, 20 — restul sunt extensii de rundă 2, odată ce arhitectura modulară de bază funcționează stabil.
