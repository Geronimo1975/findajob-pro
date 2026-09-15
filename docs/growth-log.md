# Growth ledger — findajob.pro (target ≥ €5k MRR, hibrid B2B)

| Data | Găsit | Pas propus | Status |
|---|---|---|---|
| 2026-09-15 | Fără Stripe/billing/subscription în backend (0 MRR real); prețuri în USD; landing 100% client-rendered (crawlerii văd `{{ tHeroTitle }}`); Google indexează snippet-ul platformei moarte; zero rută B2B; hero nou (Higgsfield) necomis, nedeployat. | Pagina B2B statică `/unternehmen.html` (SSR-safe, DE, „Demo buchen" → mailto/Calendly-free form) + prețuri în EUR. | GO — implementat (commit `feat(growth)`): `web/unternehmen.html`, `POST /api/v1/leads/demo/`, EUR pe landing+billing. Necesită `DJANGO_ADMINS` în env prod. |
