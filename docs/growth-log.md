# Growth ledger — findajob.pro (target ≥ €5k MRR, hibrid B2B)

| Data | Găsit | Pas propus | Status |
|---|---|---|---|
| 2026-09-15 | Fără Stripe/billing/subscription în backend (0 MRR real); prețuri în USD; landing 100% client-rendered (crawlerii văd `{{ tHeroTitle }}`); Google indexează snippet-ul platformei moarte; zero rută B2B; hero nou (Higgsfield) necomis, nedeployat. | Pagina B2B statică `/unternehmen.html` (SSR-safe, DE, „Demo buchen" → mailto/Calendly-free form) + prețuri în EUR. | GO — implementat (commit `feat(growth)`): `web/unternehmen.html`, `POST /api/v1/leads/demo/`, EUR pe landing+billing. Necesită `DJANGO_ADMINS` în env prod. |
| 2026-09-15 | Landing fără vector de curiozitate: hero centrat + poză stock, 3×3 carduri, client-rendered (crawlerii vedeau `{{ tHeroTitle }}`). Comparativ Teal/Huntr/Jobright/pland: produsul în primele 600px. | Rescris `index.html` static, DE implicit, hero split cu **try-it fără cont** (match real + draft tastat din fixtures `api.js`), toggle Bewerber/Unternehmen, ledger de risc per platformă (stance), screenshot real din app, bandă B2B, prețuri pe rânduri, FAQ DE. | implementat |
