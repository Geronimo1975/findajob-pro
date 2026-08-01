---
name: voltagent-design
description: The findajob.pro design system (dark Voltagent theme) — colors, typography, spacing, component chrome, and do/don't rules. Use before writing or editing ANY findajob UI (templates, CSS, components, new screens), so everything matches the prototype pixel-perfectly.
---

# findajob.pro design system — dark Voltagent theme

Source of truth: `prototype/Job Tracking Platform Voltagent.dc.html` + `prototype/Landing.dc.html`
+ `prototype/uploads/DESIGN-voltagent.md` (high-fidelity — colors, typography, spacing and
interactions are FINAL; recreate pixel-perfectly). What actually ships is `web/`.
**Ignore `prototype/design_handoff_findjob_pro/` — it is an older snapshot missing two screens.**

## The 13 app screens (sidebar order)

Dashboard · Job Search · **Advisor** · **Analytics** · History · Network · Documents · Profile ·
APIs · Modules · Organization · Billing · Legal. (Advisor and Analytics exist only in the
top-level prototype and in `web/app.html`; the stale handoff copy has neither.)

## Tokens

**Surfaces:** canvas `#101010` (the ONLY page background — no light mode exists) ·
surface-soft `#1a1a1a` (inputs, code blocks) · surface-softer `#2a2a2a`.
**Edge:** hairline `#3d3a39`, 1px solid everywhere. **No drop shadows** except modals
(`0 20px 60px rgba(0,0,0,0.7)`). Hairline borders ARE the elevation system.
**Accent (reserved):** primary `#00d992` — CTAs, active nav, live status ONLY; never body
text. soft `#2fd6a1` (success, ghost buttons) · deep `#10b981` (links) · text-on-primary `#101010`.
**Text:** ink `#f2f2f2` · strong `#ffffff` · body `#bdbdbd` · mute `#8b949e`.
**Semantic:** success `#2fd6a1` · warning `#e6b34d` · danger `#e5534b`. Tinted fills =
rgba pairs, e.g. bg `rgba(0,217,146,0.10)` + border `rgba(0,217,146,0.35)`; danger
`rgba(229,83,75,0.12)` / `rgba(229,83,75,0.35)`.

**Fonts:** Inter (400/500/600/700) for UI; **JetBrains Mono** for data, labels, metrics, code.
Uppercase eyebrow label = JetBrains Mono 10.5px, letter-spacing 0.07em, color mute.
Hero display = Inter 56–60px **weight 400** (calm, never 700+), tracking ≈ -0.65px.

**Radii:** buttons 6px · cards 8–10px · pills 9999px. **Spacing scale:** 4px base —
2/4/8/12/16/20/24/32/40/48/64.

## Component chrome

- **button-primary:** bg primary, text `#101010`, Inter 600, radius 6px, padding 12px 16px; hover opacity ~0.9.
- **button-outline:** bg canvas, text ink, 1px hairline border, same shape.
- **pill/chip:** radius 9999px, 1px border; selected = green tinted fill pair + text `#2fd6a1`.
- **card:** bg canvas or `#1a1a1a`, 1px hairline, radius 8–10px, padding 18–24px. Featured/active = border `rgba(0,217,146,0.5)` or 2px solid primary.
- **input/select/textarea:** bg `#1a1a1a`, 1px hairline, radius 6px; focus = `outline: 2px solid #00d992; outline-offset: -1px`.
- **data table:** header mono-caps eyebrow on `#1a1a1a`; rows body-sm with hairline row borders. Dashboard grid columns: `minmax(200px,1.8fr) 132px minmax(140px,1fr) 64px 104px 78px 90px 122px` inside `min-width:1024px` + `overflow-x:auto`.
- **sidebar:** collapsible 58px icon-only ↔ 218px; active nav row uses primary indicator; inline SVG stroke icons (stroke-width 1.8, 17px).
- **toast:** bottom-center, card chrome. **Review drawer:** right side, 560px.
- **status pills:** PENDING mute / RUNNING spinner / REVIEW warning tint / SENT green tint.

## Rules

- Dark canvas only — never introduce a light mode.
- Green is CTA/status only — never a body-text color.
- Hairlines, not shadows; no gradients, no illustrations — typographic moments only.
- Inter sentence-case + JetBrains Mono for anything "typed at a terminal"; don't substitute fonts.
- Breakpoints: <768 mobile (1-up, hero 32px), 768–1023 (2-up), ≥1024 (3-up, full grids).
  The prototype is authored desktop-first **with inline styles only**, so responsive rules live
  in each page's `<style>` block in `<helmet>` and must use `!important` + attribute selectors
  (`[style*="repeat(3, 1fr)"]`) to beat the inline styles. Already implemented in `web/`:
  landing collapses at 1023/767; app collapses at 899/560 and its sidebar starts icon-only via
  `collapsed: window.innerWidth < 900`. **Never give a wide grid `min-width` unless its
  ancestor card is the scroll container** (`overflow-x: auto` + `max-width: 100%`), or it
  widens the whole page instead of scrolling.
- Buttons ≥44px tall touch targets.
- i18n: every user-visible string exists in EN/DE/RO (header language switcher translates nav, titles, cookie banner, legal).
- TTDSG cookie banner (Accept all / Essential only) on first load, before any non-essential storage.
