# GEO Audit Report

**Data:** 2026-08-22 · **Auditor:** GeorgeAI (self-audit)
**Site-uri:** georgeai.dev · findajob.pro
**Metodă:** robots.txt / llms.txt / sitemap, headers, JSON-LD, analiză HTML static vs. randat client-side, prezență brand terță

---

# 1. georgeai.dev

**Tip:** Agency / Services · **Pagini în sitemap:** 21 · **Pagini analizate:** 3 reprezentative

## Scor GEO general: **47/100 — Poor**

Fundația tehnică e printre cele mai bune pe care le vezi la un site de agenție. Problema nu e pe site — e **în afara lui**. Autoritatea de brand și prezența pe platforme sunt aproape de zero, iar împreună cântăresc 30% din scor. La asta se adaugă un defect de randare pe pagina cea mai importantă.

| Categorie | Scor | Pondere | Contribuție |
|---|---|---|---|
| AI Citability | 62/100 | 25% | 15,5 |
| Brand Authority | 15/100 | 20% | 3,0 |
| Content E-E-A-T | 40/100 | 20% | 8,0 |
| Technical GEO | 72/100 | 15% | 10,8 |
| Schema & Structured Data | 85/100 | 10% | 8,5 |
| Platform Optimization | 12/100 | 10% | 1,2 |
| **Total** | | | **47,0** |

## 🔴 Critic

**C1. Homepage-ul e randat client-side — 226 de placeholdere `{{ }}` în HTML-ul static.**
HTML-ul livrat conține `{{ t.pg.servicesTitle }}` în locul H1-ului, plus `{{ t.nav.about }}`, `{{ email }}`, `{{ curLangLabel }}` și încă 222 de variabile nerandate. Din 1.036 de cuvinte statice, o mare parte sunt nume de variabile.

De ce e critic: **GPTBot, PerplexityBot și ClaudeBot nu execută JavaScript.** Homepage-ul e ancora de entitate a site-ului — pagina din care un sistem AI înțelege „ce este GeorgeAI". Exact acea pagină le apare goală.

Ironia arhitecturală: **paginile de serviciu și blogul sunt randate server-side corect** (0 placeholdere). Deci site-ul e bine construit peste tot, mai puțin acolo unde contează cel mai mult.

*Fix:* pre-randare / SSR pentru homepage, sau minimum un bloc `<noscript>` cu H1, propunerea de valoare și lista de servicii în HTML static.

## 🟠 High

**H1. Zero prezență de brand terță.** Nicio mențiune pe Wikipedia, Reddit, LinkedIn (pagină de companie), YouTube, directoare de agenții sau presă. Sistemele AI recunosc entități prin coroborare între surse; o singură sursă (site-ul propriu) nu constituie o entitate.

**H2. Cartea publicată nu e legată nicăieri.** *„AI Revolution Job Crisis in Germany: A Roadmap for Recovery (2024–2030)"*, Kindle, autor George Sebastian Cucuiet — apare în căutare, dar nu e menționată pe georgeai.dev, nu e în schema `Person`, nu e în llms.txt. **E cel mai puternic semnal E-E-A-T pe care îl deții și e complet nefolosit.**

**H3. Paginile de serviciu sunt subțiri.** `/services/seo-aio-geo/` are 156 de cuvinte statice și 0 `<h3>`. Prea puțin ca să fie citată; un sistem AI n-are ce extrage. Toate cele 16 pagini de serviciu par să aibă acest profil.

**H4. Fără autor și fără bio pe blog.** Schema `BlogPosting` nu are `author` cu credențiale. Pentru E-E-A-T, articolul e orfan.

## 🟡 Medium

**M1. Doar 2 articole pe blog.** Autoritate topică insuficientă pentru a fi sursa preferată pe subiectul GEO.
**M2. Fără studii de caz.** Rotocon (500 oferte/lună) nu apare nicăieri pe site — nici ca pagină, nici în llms.txt.
**M3. Fără `FAQPage` pe paginile de serviciu.** Există pe blog, dovadă că știi să o implementezi. Serviciile n-o au.
**M4. CSP e `report-only`.** Nu se aplică efectiv.

## 🟢 Low

**L1.** `Bytespider` permis explicit — crawler ByteDance, agresiv, valoare de citare aproape nulă. Decizie conștientă sau scăpare?
**L2.** Fără `dateModified` vizibil în schema articolelor.

## Ce e făcut foarte bine

- **robots.txt** cu allow-list explicit pentru 12 crawlere AI, plus comentariu care explică politica. Mai bun decât la majoritatea agențiilor GEO.
- **llms.txt** — 4.218 octeți, real și util: descriere, toate cele 16 servicii cu stack, blog, pachete de colaborare, contact. Nu e bifă, e un document.
- **Schema bogată și corectă:** `ProfessionalService`, `Person`, `OfferCatalog`, `Service`, `ServiceChannel`, `BreadcrumbList`, `WebSite`, `WebPage`, iar pe blog `BlogPosting` + `FAQPage` + `Question`/`Answer`.
- **Blogul e citabil.** 1.220 de cuvinte, 9 × H2, statistici cu sursă („Ahrefs, 2025"), format întrebare-răspuns. Exact ce citează un motor generativ.
- Securitate: HSTS, `X-Content-Type-Options`, `X-Frame-Options`. Toate imaginile au `alt`.

---

# 2. findajob.pro

**Tip:** SaaS · **Pagini în sitemap:** 1 · **Pagini analizate:** 1

## Scor GEO general: **15/100 — Critical**

| Categorie | Scor | Pondere | Contribuție |
|---|---|---|---|
| AI Citability | 18/100 | 25% | 4,5 |
| Brand Authority | 8/100 | 20% | 1,6 |
| Content E-E-A-T | 15/100 | 20% | 3,0 |
| Technical GEO | 38/100 | 15% | 5,7 |
| Schema & Structured Data | 0/100 | 10% | 0,0 |
| Platform Optimization | 5/100 | 10% | 0,5 |
| **Total** | | | **15,3** |

## 🔴 Critic

**C1. 147 de cuvinte statice în total.** Din care 30 sunt placeholdere `{{ }}`: `{{ tHeroTitle }}`, `{{ tHeroSub }}`, `{{ tFeatures }}`, `{{ tPricing }}`. Pentru un crawler fără JavaScript, site-ul e practic gol.

**C2. Zero structured data.** Niciun bloc JSON-LD pe întreg site-ul. Fără `SoftwareApplication`, `Organization`, `Offer`, `FAQPage`. Un sistem AI nu are cum să știe că e un produs SaaS, cine îl face, cât costă.

**C3. Fără llms.txt** (404).

## 🟠 High

**H1. Sitemap cu o singură pagină.** Nu există pagini de funcționalități, prețuri, despre, docs sau blog indexabile. Pentru un SaaS, comparațiile de funcționalități și paginile de preț sunt exact conținutul cel mai citat de AI.
**H2. Fără Open Graph.** Nicio previzualizare la partajare.
**H3. Fără pagină „About", fără echipă, fără autor.** Zero semnale E-E-A-T.
**H4. Zero prezență terță.** Căutarea returnează concurenți (Loopcv, JobCopilot, AIApply, Jobright), nu findajob.pro.

## 🟡 Medium

**M1.** `robots.txt` blochează `/app.html` și `/auth.html` — corect. Dar nu există conținut public dincolo de root.
**M2.** Meta description e bună și specifică (numește LinkedIn, RemoteOK, We Work Remotely, Wellfound, Indeed) — și e practic singurul conținut lizibil pentru un AI. Risipă.

## Ce e bine
- Headers de securitate excelente: HSTS cu `preload`, CSP aplicat (nu report-only), `X-Frame-Options: DENY`.
- Meta description precisă și utilă.

---

# 3. Quick wins — de făcut săptămâna asta

| # | Acțiune | Site | Efort | Impact |
|---|---|---|---|---|
| 1 | Pre-randare / `<noscript>` cu H1 + propunere de valoare + servicii | ambele | mediu | 🔴 critic |
| 2 | Adaugă JSON-LD `SoftwareApplication` + `Organization` + `Offer` | findajob.pro | mic | 🔴 critic |
| 3 | Creează `llms.txt` (copiază structura de la georgeai.dev) | findajob.pro | mic | 🟠 mare |
| 4 | Leagă cartea Kindle: `Person.sameAs` + secțiune în llms.txt + mențiune pe homepage | georgeai.dev | mic | 🟠 mare |
| 5 | Extinde 5 pagini de serviciu la 600+ cuvinte cu `FAQPage` | georgeai.dev | mediu | 🟠 mare |
| 6 | Adaugă `author` cu bio și credențiale în schema blogului | georgeai.dev | mic | 🟠 mare |
| 7 | Pagini publice pentru funcționalități + prețuri, în sitemap | findajob.pro | mediu | 🟠 mare |
| 8 | Studiu de caz Rotocon (500 oferte/lună) ca pagină + în llms.txt | georgeai.dev | mic | 🟡 mediu |
| 9 | Open Graph complet | findajob.pro | mic | 🟡 mediu |
| 10 | Pagină de companie LinkedIn pentru ambele branduri | ambele | mic | 🟠 mare |

# 4. Plan 30 de zile

**Săptămâna 1 — reparații critice de randare**
- [ ] SSR sau `<noscript>` pe homepage-urile ambelor site-uri
- [ ] JSON-LD complet pe findajob.pro
- [ ] llms.txt pentru findajob.pro

**Săptămâna 2 — E-E-A-T**
- [ ] Autor + bio + credențiale în schema blogului georgeai.dev
- [ ] Cartea Kindle legată prin `Person.sameAs` pe ambele
- [ ] Pagină „About / Team" pe findajob.pro
- [ ] Studiu de caz Rotocon publicat

**Săptămâna 3 — profunzime de conținut**
- [ ] 5 pagini de serviciu extinse la 600+ cuvinte + FAQPage
- [ ] Pagini publice funcționalități și prețuri pe findajob.pro
- [ ] 2 articole noi pe blog (autoritate topică pe GEO)

**Săptămâna 4 — autoritate off-site**
- [ ] Pagini de companie LinkedIn, ambele branduri
- [ ] Prezență pe Reddit / comunități relevante, cu răspunsuri utile, nu promo
- [ ] Listări în directoare de agenții
- [ ] Re-rulare audit, măsurare delta

**Țintă realistă la 30 de zile:** georgeai.dev **47 → 78** · findajob.pro **15 → 62**

---

# 5. Nota strategică

Diagnosticul e neobișnuit și merită spus clar: **georgeai.dev face bine exact partea pe care majoritatea o ratează** — robots.txt cu allow-list AI, un llms.txt real, schema completă și corectă. Și ratează două lucruri de bază: homepage-ul nu se randează pentru crawlerele fără JS, și nu există nicio dovadă că brandul există în afara propriului domeniu.

Al doilea e cel greu. Autoritatea de brand și prezența pe platforme sunt 30% din scor și nu se rezolvă cu cod — se rezolvă în luni de prezență publică. Primul se rezolvă într-o zi.

**Cartea Kindle e activul irosit al acestui audit.** Un autor publicat pe tema AI și piața muncii din Germania, invizibil pe ambele site-uri. Legarea ei e o oră de muncă și mută E-E-A-T mai mult decât orice altă acțiune din listă.
