/**
 * analytics.js — the single owner of Google Analytics 4 on findajob.pro.
 *
 * The hard rule in CLAUDE.md is that the frontend contacts no third-party origin.
 * GA4 is the one deliberate exception, and it is exempt ONLY while it stays behind
 * affirmative consent: nothing here touches the network until the visitor has
 * clicked "Accept all". Before that, googletagmanager.com is never requested, so a
 * German visitor's IP never reaches a US server — which is what § 25 (1) TTDSG
 * requires (consent BEFORE storage/access on the device) and what Art. 44 GDPR
 * makes risky for the transfer itself.
 *
 * Consequences, so nobody "simplifies" this later:
 *   - Do NOT paste the raw gtag.js snippet into any .html file. A <script async src>
 *     in the markup fires on page load, before any banner can be answered, which is
 *     precisely the violation this module exists to prevent.
 *   - The CSP in deploy/nginx.conf must allow the Google origins or the injected
 *     script is blocked and no hit is ever recorded.
 *   - web/legal.js documents this processing. If the measurement ID or the vendor
 *     changes, the Cookie Policy and Datenschutzerklärung change with it, and
 *     `manage.py seed_legal` has to be re-run.
 */

/** GA4 measurement ID for findajob.pro. */
const MEASUREMENT_ID = 'G-5SH3ZXQD6C';

/**
 * Consent lives in localStorage, not a cookie, and deliberately so: writing a
 * cookie to record "no thanks" is itself device storage, and while the consent
 * record is defensible as strictly necessary (§ 25 (2) TTDSG), localStorage keeps
 * it out of every HTTP request to the API. Shared verbatim by index/app/auth so a
 * choice made on the landing page is honoured inside the dashboard.
 */
const STORAGE_KEY = 'fj.consent.v1';

/** The only two answers the banner can produce. */
const ALL = 'all';
const ESSENTIAL = 'essential';

/**
 * Private-mode Safari and hardened browsers throw on localStorage access rather
 * than returning null, and an exception here would take the whole page down with
 * it. Analytics is never worth breaking the app for, so every access degrades to
 * "no consent recorded" — the banner simply asks again.
 */
function safeRead() {
  try {
    return window.localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

function safeWrite(value) {
  try {
    window.localStorage.setItem(STORAGE_KEY, value);
  } catch {
    /* Consent still applies for this page view; it just will not survive a reload. */
  }
}

/**
 * Returns 'all', 'essential', or null when the visitor has not answered yet.
 * Anything unrecognised in storage is treated as unanswered rather than trusted —
 * a corrupted or hand-edited value must never read as consent.
 */
export function getConsent() {
  const stored = safeRead();
  return stored === ALL || stored === ESSENTIAL ? stored : null;
}

/** True while the banner should be shown. */
export function needsConsent() {
  return getConsent() === null;
}

let loaded = false;

/**
 * Injects gtag.js exactly once. Guarded because both the banner handler and the
 * boot-time replay of stored consent call into here, and a second <script> tag
 * would double-count every pageview.
 */
function loadGtag() {
  if (loaded) return;
  loaded = true;

  window.dataLayer = window.dataLayer || [];
  // Google's snippet requires the `arguments` object itself, not an array copy —
  // gtag.js reads it positionally. An arrow function has no `arguments` binding,
  // so this stays a function declaration.
  function gtag() { window.dataLayer.push(arguments); }
  window.gtag = gtag;

  // Consent Mode v2. Declared BEFORE config so the very first hit already carries
  // the right signals: we asked only about analytics, so every advertising
  // purpose stays denied permanently rather than defaulting to granted.
  gtag('consent', 'default', {
    ad_storage: 'denied',
    ad_user_data: 'denied',
    ad_personalization: 'denied',
    analytics_storage: 'granted',
  });

  gtag('js', new Date());
  gtag('config', MEASUREMENT_ID, {
    // Truncates the IP before storage. Not a legal cure for the US transfer, but
    // it is the minimisation Art. 5 (1)(c) expects and costs us nothing.
    anonymize_ip: true,
    // findajob is a single-page shell that swaps `nav` in React state without a
    // navigation, so GA's automatic history tracking would invent pageviews for
    // route changes we have not modelled. Pageviews are sent explicitly instead.
    send_page_view: true,
  });

  const s = document.createElement('script');
  s.async = true;
  s.src = `https://www.googletagmanager.com/gtag/js?id=${encodeURIComponent(MEASUREMENT_ID)}`;
  document.head.appendChild(s);
}

/**
 * Records the visitor's answer and acts on it.
 *
 * Note the asymmetry: granting consent loads GA immediately, but revoking it
 * cannot unload a script that is already running. Downgrading to 'essential'
 * therefore flips analytics_storage to denied AND sets the window-level opt-out
 * flag gtag.js checks on every hit, then the next page load simply never injects
 * it. Callers that want a guaranteed-clean state should reload.
 */
export function setConsent(choice) {
  const value = choice === ALL ? ALL : ESSENTIAL;
  safeWrite(value);

  if (value === ALL) {
    loadGtag();
    return value;
  }

  window[`ga-disable-${MEASUREMENT_ID}`] = true;
  if (typeof window.gtag === 'function') {
    window.gtag('consent', 'update', { analytics_storage: 'denied' });
  }
  return value;
}

/**
 * Call once per page, as early as possible. Replays a consent decision the
 * visitor already made on a previous visit or on another page of the site, so
 * the banner is answered once rather than on every navigation.
 */
export function initAnalytics() {
  if (getConsent() === ALL) loadGtag();
}

/**
 * Explicit pageview for the SPA shell. No-op unless GA is actually live, so
 * callers never have to guard on consent themselves.
 */
export function trackPageView(path, title) {
  if (typeof window.gtag !== 'function') return;
  window.gtag('event', 'page_view', {
    page_path: path,
    page_title: title,
    page_location: window.location.origin + path,
  });
}
