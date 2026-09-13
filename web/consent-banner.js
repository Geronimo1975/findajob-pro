/**
 * consent-banner.js — the TTDSG consent banner for the two pages that are not the
 * React dashboard: index.html (landing) and auth.html (login).
 *
 * app.html renders its own banner inside the .dc.html template and does NOT use
 * this module — it already had the markup and the i18n table. This file exists so
 * the landing and login pages get the same gate without dragging the visual-editor
 * runtime into them. Both implementations write the SAME localStorage key through
 * analytics.js, so a visitor answers once for the whole site.
 *
 * Chrome copied from app.html's banner so the three pages are visually identical:
 * surface-soft #1a1a1a, hairline #3d3a39, radius 10px, primary #00d992 on #101010.
 * Built with DOM calls rather than innerHTML because the CSP forbids inline event
 * handlers and this way no string ever gets parsed as markup.
 */

import { needsConsent, setConsent } from './analytics.js';

/** Written by auth.html's language switcher; the landing page keeps lang in React state. */
const LANG_KEY = 'findajob.lang';

const T = {
  en: {
    title: 'We value your privacy',
    text: 'We use essential cookies for login and security. Optional cookies enable Google Analytics so we can see which pages are useful (§ 25 TTDSG). This sends data to Google in the USA.',
    link: 'Cookie Policy →',
    all: 'Accept all',
    essential: 'Essential only',
  },
  de: {
    title: 'Wir respektieren Ihre Privatsphäre',
    text: 'Wir verwenden essenzielle Cookies für Login und Sicherheit. Optionale Cookies aktivieren Google Analytics, damit wir sehen, welche Seiten nützlich sind (§ 25 TTDSG). Dabei werden Daten an Google in den USA übermittelt.',
    link: 'Cookie-Richtlinie →',
    all: 'Alle akzeptieren',
    essential: 'Nur essenzielle',
  },
  ro: {
    title: 'Vă respectăm confidențialitatea',
    text: 'Folosim cookie-uri esențiale pentru autentificare și securitate. Cookie-urile opționale activează Google Analytics, ca să vedem ce pagini sunt utile (§ 25 TTDSG). Astfel se transmit date către Google în SUA.',
    link: 'Politica privind cookie-urile →',
    all: 'Acceptă toate',
    essential: 'Doar esențiale',
  },
};

function pickLang() {
  try {
    const stored = window.localStorage.getItem(LANG_KEY);
    if (T[stored]) return stored;
  } catch { /* storage blocked — fall through to the browser's own preference */ }
  const nav = (navigator.language || 'en').slice(0, 2).toLowerCase();
  return T[nav] ? nav : 'en';
}

function button(label, primary, onClick) {
  const b = document.createElement('button');
  b.type = 'button';
  b.textContent = label;
  b.style.cssText = primary
    ? 'font-size:12.5px;font-weight:600;background:#00d992;color:#101010;border:none;border-radius:6px;padding:10px 16px;min-height:44px;cursor:pointer;font-family:inherit;'
    : 'font-size:12.5px;font-weight:600;background:#101010;color:#f2f2f2;border:1px solid #3d3a39;border-radius:6px;padding:10px 16px;min-height:44px;cursor:pointer;font-family:inherit;';
  b.addEventListener('click', onClick);
  return b;
}

/**
 * Renders the banner if — and only if — no choice has been recorded yet.
 * Safe to call on every page load; it is a no-op for a returning visitor.
 */
export function mountConsentBanner() {
  if (!needsConsent()) return;

  const t = T[pickLang()];

  const box = document.createElement('div');
  // role=dialog + aria-label so screen readers announce it as a decision to make,
  // not as stray text at the end of the document.
  box.setAttribute('role', 'dialog');
  box.setAttribute('aria-label', t.title);
  box.style.cssText = 'position:fixed;bottom:16px;left:50%;transform:translateX(-50%);width:640px;max-width:94vw;background:#1a1a1a;border:1px solid #3d3a39;border-radius:10px;padding:16px 18px;z-index:9999;box-shadow:0 12px 40px rgba(0,0,0,0.6);font-family:Inter,system-ui,sans-serif;';

  const title = document.createElement('div');
  title.textContent = t.title;
  title.style.cssText = 'font-size:13px;font-weight:600;color:#f2f2f2;';

  const text = document.createElement('div');
  text.textContent = t.text + ' ';
  text.style.cssText = 'font-size:12px;color:#bdbdbd;line-height:1.5;margin-top:5px;';

  // Deep-links into the dashboard's legal viewer, the same route index.html's
  // footer already uses — the policy lives in one place (legal.js), not three.
  const link = document.createElement('a');
  link.href = 'app.html#legal-cookies';
  link.textContent = t.link;
  link.style.cssText = 'color:#2fd6a1;text-decoration:none;font-size:12px;';
  text.appendChild(link);

  const row = document.createElement('div');
  row.style.cssText = 'display:flex;gap:8px;margin-top:12px;flex-wrap:wrap;';

  const close = (choice) => { setConsent(choice); box.remove(); };
  row.appendChild(button(t.all, true, () => close('all')));
  row.appendChild(button(t.essential, false, () => close('essential')));

  box.append(title, text, row);
  document.body.appendChild(box);
}
