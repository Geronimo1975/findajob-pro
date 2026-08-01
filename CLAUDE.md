# findajob.pro — project instructions

**Fresh start (2026-08-01, UI-first).** The old Django/Jobick platform is dead — wiped from
prod and local the same day. Anything referencing Jobick, `dz.py`, `dashboard_views.py`, or
the old `.claude/rules/*` describes the DEAD platform; ignore it.

## Sources of truth

1. `main_prompt.md` — platform-adapter architecture, per-platform risk rules, MVP list.
2. `prototype/` (**top level only**) — the high-fidelity interactive prototype, 13 screens,
   dark Voltagent theme. `prototype/api.js` = the API/serializer contract (byte-identical to
   the deployed `web/api.js`); `legal.js` = legal texts (6 docs × EN/DE/RO, DE legally binding);
   `uploads/DESIGN-voltagent.md` = the design tokens.
   **`prototype/design_handoff_findjob_pro/` is a STALE snapshot — do not read it.** Its
   `.dc.html` is missing the Advisor and Analytics screens entirely and its `api.js` lacks
   `SUGGEST`, `careerAnalysis` and `funnelAnalysis`. Verified by diff against the deployed files.
3. `web/` — what is actually live on findajob.pro. `web/` is authoritative for "what ships";
   `prototype/` is authoritative for design intent. `web/` already carries post-prototype work:
   responsive breakpoints, self-hosted React + fonts, hash deep-links, real Impressum data.

## Mandatory skills (invoke before working)

- Any architecture/backend/model/module work → `findajob-architecture`
- Any UI work → `voltagent-design` (plus global `ui-ux-pro-max` rule)
- Any deploy/infra/VPS work → `findajob-deploy`

## Hard rules

- **UI-first:** findajob.pro must always serve the visible interface; backend wires in
  underneath without regressing it.
- **Never automate submission on LinkedIn or Indeed** (ban/legal risk) — semi-manual flows
  as designed.
- VPS SSH host is `macbook-air` (85.215.69.83). n8n = PERSONAL account (app.n8n-studio.com).
- Languages: EN default, DE, RO. Germany/GDPR market — TTDSG cookie gate, encrypted
  documents and API keys.
- No heavy new dependencies without approval.
- **The frontend contacts no third-party origin.** React and both fonts are self-hosted under
  `web/vendor/` and `web/fonts/`, and nginx sends a CSP with `default-src 'self'`. A
  Germany-targeted site must not ship a visitor's IP to a US CDN before consent. Do not
  re-introduce a CDN `<link>`/`<script>`.
- **The published Impressum carries a real person's real address** (operator: George Sebastian
  Cucuiet, Greuner Brook 1, 21271 Hanstedt). It is legally required (§ 5 DDG) and was supplied
  deliberately — never replace it with placeholder or invented company data, and never invent
  a HRB / VAT number.
