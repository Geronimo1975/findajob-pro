// The frontend's single HTTP owner (spec §8) plus the demo fixtures.
// There is deliberately no second fetch helper anywhere else in web/ — one
// implementation of CSRF, one error shape, one place to change the base URL.
export const BASE = '/api/v1';
export const ENDPOINTS = {
  // --- auth / session (M1) ---
  authCsrf: '/auth/csrf/',                  // GET  -> sets the csrftoken cookie
  authRequest: '/auth/request-code/',       // POST {email, lang} -> 202 {masked_email, expires_in, resend_in}
  authVerify: '/auth/verify-code/',         // POST {email, code, accepts_terms} -> 200 + session
  authLogout: '/auth/logout/',              // POST -> 204
  me: '/me/',                               // GET  -> {user, org, membership_role, memberships, lang}

  platforms: '/platforms/',                 // GET list, PATCH /platforms/:id/ {is_active}
  jobs: '/jobs/',                           // GET ?platform=&status=
  generate: '/jobs/:id/generate/',          // POST -> Application {generated_content, generated_cover_letter, suggested_documents}
  send: '/applications/:id/send/',          // POST {sent_manually}
  documents: '/documents/',
  profile: '/profile/',
  integrations: '/integrations/',           // GET list, POST /integrations/:id/connect/ {api_key}, DELETE key
  contacts: '/contacts/',                   // networking CRM
  org: '/org/',                             // GET org, POST /org/invite/ {email, role}
  billing: '/billing/',                     // GET {plan, usage}, POST /billing/subscribe/ {plan, cycle}
  skillmatch: '/jobs/:id/skill-match/',     // GET {score, matched, missing}
};

// ─────────────────────────────────────────────────────────────────────────────
// HTTP layer
// ─────────────────────────────────────────────────────────────────────────────

// Mocks are opt-in and fail CLOSED (spec §9). Live is the default: a bundle that
// silently served invented data in production would be worse than an outage,
// because the user cannot tell the difference.
export const USE_MOCK = typeof window !== 'undefined' && window.__FINDJOB_MOCK__ === true;
if (USE_MOCK && !/^(localhost|127\.|.*\.local)$/.test(location.hostname)) {
  throw new Error('mock API blocked off-localhost');
}

/** Every failure the API can produce, in one shape: {"error":{code, detail}}. */
export class ApiError extends Error {
  constructor(status, code, detail, retryAfter) {
    super(detail || code || `HTTP ${status}`);
    this.name = 'ApiError';
    this.status = status;
    this.code = code || 'http_error';
    this.retryAfter = retryAfter;      // seconds, only meaningful on 429
  }
  /** True when the request never reached the server — offline, DNS, 502. */
  get isUnreachable() { return this.status === 0; }
}

export function readCookie(name) {
  const prefix = name + '=';
  for (const part of document.cookie.split('; ')) {
    if (part.startsWith(prefix)) return decodeURIComponent(part.slice(prefix.length));
  }
  return '';
}

export async function request(path, { method = 'GET', body } = {}) {
  let res;
  try {
    res = await fetch(BASE + path, {
      method,
      // 'same-origin', NOT 'include': nginx proxies /api/ on this very origin,
      // so the session cookie rides along without opening a CORS surface.
      credentials: 'same-origin',
      headers: {
        ...(body ? { 'Content-Type': 'application/json' } : {}),
        // DRF's APIView is csrf_exempt, so the token is the only thing standing
        // between an anonymous POST and cross-site login CSRF.
        ...(method !== 'GET' ? { 'X-CSRFToken': readCookie('csrftoken') } : {}),
      },
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch {
    // Kept distinct from an HTTP error on purpose: callers must be able to tell
    // "the API said no" from "there is no API", and degrade rather than bounce
    // the visitor to a login page that cannot work either.
    throw new ApiError(0, 'network_unreachable', 'The server could not be reached.');
  }

  if (res.status === 204) return null;

  const payload = await res.json().catch(() => null);
  if (!res.ok) {
    const err = (payload && payload.error) || {};
    const retryAfter = Number(
      err.retry_after ?? (payload && payload.retry_after) ?? res.headers.get('Retry-After') ?? 0
    ) || undefined;
    throw new ApiError(res.status, err.code, err.detail, retryAfter);
  }
  return payload;
}

/**
 * Prime the csrftoken cookie. Must be awaited on load of every page that can
 * POST, before any form is submittable — the first POST would otherwise be
 * rejected for a token the browser has never been given.
 */
export async function bootstrapCsrf() {
  try {
    await request(ENDPOINTS.authCsrf);
    return true;
  } catch {
    return false;      // no backend yet, or offline: the page stays readable
  }
}

export const requestCode = (email, lang) =>
  request(ENDPOINTS.authRequest, { method: 'POST', body: { email, lang } });

export const verifyCode = (email, code, accepts_terms) =>
  request(ENDPOINTS.authVerify, { method: 'POST', body: { email, code, accepts_terms } });

export const logout = () => request(ENDPOINTS.authLogout, { method: 'POST' });

export const getMe = () => request(ENDPOINTS.me);

// --- account data (M2) — the dashboard reads. Same shapes as the fixtures
// below; a brand-new account legitimately gets [] from /jobs/.
export const getPlatforms = () => request(ENDPOINTS.platforms);

export const getJobs = (params = {}) => {
  const qs = new URLSearchParams(
    Object.entries(params).filter(([, v]) => v != null && v !== '')
  ).toString();
  return request(ENDPOINTS.jobs + (qs ? '?' + qs : ''));
};

// SaaS plans — Claude.ai-style tier structure (per-seat for Team/Enterprise)
export const PLANS = [
  { id: 'free',       name: 'Free',       monthly: 0,   yearly: 0,   seat: false, tagline: 'Try the tracker',
    limits: { apps: 10,  gens: 20,   platforms: 2,  seats: 1 },
    features: ['10 applications / month', '20 AI generations', '2 platform modules', 'Manual send only', 'Community support'] },
  { id: 'pro',        name: 'Pro',        monthly: 19,  yearly: 16,  seat: false, tagline: 'For an active job search', popular: true,
    limits: { apps: 100, gens: 500,  platforms: 99, seats: 1 },
    features: ['100 applications / month', '500 AI generations', 'All platform modules', 'Auto-send (low-risk sources)', 'Canva templates', 'Email support'] },
  { id: 'max',        name: 'Max',        monthly: 99,  yearly: 82,  seat: false, tagline: '5×–20× more usage',
    limits: { apps: 1000, gens: 5000, platforms: 99, seats: 1 },
    features: ['1,000 applications / month', '5,000 AI generations', 'Priority AI queue', 'n8n workflow automation', 'API access', 'Priority support'] },
  { id: 'team',       name: 'Team',       monthly: 29,  yearly: 24,  seat: true,  tagline: 'Agencies & career coaches · min 5 seats',
    limits: { apps: 500, gens: 2500, platforms: 99, seats: 100 },
    features: ['Everything in Pro, per seat', 'Central admin & roles', 'Shared document library', 'Client workspaces', 'Usage analytics', 'DPA (AVV) included'] },
];

export const ORG = {
  name: 'Popescu Career Services', plan: 'pro', cycle: 'monthly', seats_included: 1,
  members: [
    { id: 1, name: 'Andrei Popescu', email: 'andrei@popescu.dev',   role: 'owner',  status: 'active',  last_active: 'now' },
    { id: 2, name: 'Ioana Radu',     email: 'ioana@popescu.dev',    role: 'admin',  status: 'active',  last_active: '2h ago' },
    { id: 3, name: 'Mihai Georgescu', email: 'mihai.g@gmail.com',   role: 'member', status: 'active',  last_active: '1d ago' },
    { id: 4, name: 'elena.v@yahoo.com', email: 'elena.v@yahoo.com', role: 'member', status: 'invited', last_active: '—' },
  ],
};

export const USAGE = { apps: 34, gens: 212, docs_mb: 48, period_ends: 'Aug 28, 2026' };


// Connected apps — API keys are stored encrypted server-side, never in the frontend.
export const INTEGRATIONS = [
  { id: 'anthropic', name: 'Anthropic',  category: 'AI generation',   purpose: 'Claude API — generates emails, cover letters and founder messages.', connected: true,  key_hint: 'sk-ant-••••••••7f2a', used_by: 'AI Engine (core)' },
  { id: 'openai',    name: 'OpenAI',     category: 'AI generation',   purpose: 'Fallback model for content generation and embeddings for skill matching.', connected: false, key_hint: null, used_by: 'AI Engine (core)' },
  { id: 'canva',     name: 'Canva',      category: 'Documents',       purpose: 'Export CVs and cover letters into branded Canva templates.', connected: false, key_hint: null, used_by: 'Document Library' },
  { id: 'telegram',  name: 'Telegram',   category: 'Notifications',   purpose: 'Bot notifications: new skill matches, daily digest, send confirmations.', connected: true,  key_hint: '7293•••••••:AAH••••', used_by: 'n8n workflows' },
  { id: 'whatsapp',  name: 'WhatsApp',   category: 'Notifications',   purpose: 'WhatsApp Business API alerts for high-priority job matches.', connected: false, key_hint: null, used_by: 'n8n workflows' },
  { id: 'jotform',   name: 'Jotform',    category: 'Forms',           purpose: 'Pre-fill external application forms from your master profile.', connected: false, key_hint: null, used_by: 'Apply pipeline' },
  { id: 'slack',     name: 'Slack',      category: 'Notifications',   purpose: 'Channel alerts on skill match + daily aggregated digest.', connected: false, key_hint: null, used_by: 'n8n workflows' },
];

export const PLATFORMS = [
  { id: 'linkedin',  name: 'LinkedIn',          connector_type: 'browser_automation', auth_method: 'cookie_session', apply_mechanism: 'in_platform_form',   risk: 'high',   auto_send: false, is_active: true,  rate_limit: '10/day, 90s delay', note: 'Semi-manual only. Submit is always a human click — automation violates the User Agreement.' },
  { id: 'wwr',       name: 'We Work Remotely',  connector_type: 'rss',                auth_method: 'none',           apply_mechanism: 'email',              risk: 'low',    auto_send: true,  is_active: true,  rate_limit: '60/day, 5s delay',  note: 'Official RSS feed. Safe for full automation.' },
  { id: 'remoteok',  name: 'RemoteOK',          connector_type: 'api',                auth_method: 'none',           apply_mechanism: 'email',              risk: 'low',    auto_send: true,  is_active: true,  rate_limit: '60/day, 5s delay',  note: 'Official JSON API. Safe for full automation.' },
  { id: 'wellfound', name: 'Wellfound',         connector_type: 'scraper_html',       auth_method: 'login_form',     apply_mechanism: 'message_to_founder', risk: 'medium', auto_send: false, is_active: true,  rate_limit: '15/day, 60s delay', note: 'Founder messages need mandatory human review before send.' },
  { id: 'indeed',    name: 'Indeed',            connector_type: 'api',                auth_method: 'api_key',        apply_mechanism: 'external_redirect',  risk: 'high',   auto_send: false, is_active: false, rate_limit: 'feed only',         note: 'Scraping prohibited. Publisher XML feed or manual paste only.' },
];

const H = 3600e3, D = 24 * H;
const now = Date.now();
export const JOBS = [
  { id: 101, description: "Own the billing and provisioning backend (Django/DRF, Postgres). You will design APIs consumed by 40+ enterprise labels, harden Celery pipelines and mentor two mid-level engineers. Remote-first, EU hours.", platform: 'remoteok',  title: 'Senior Django Engineer',        company: 'Loftware',      salary: '$140–170k', tags: ['Django','DRF','Postgres'],   scraped_at: now - 2*H,  status: 'pending', psd: { apply_email: 'jobs@loftware.io' } },
  { id: 102, description: "Build clinical data pipelines in Python. Celery + Redis at scale (2M tasks/day), HIPAA-adjacent compliance, strong testing culture. Fully remote.", platform: 'wwr',       title: 'Backend Engineer (Python)',     company: 'Fathom Health',  salary: '$130–160k', tags: ['Python','Celery','Redis'],   scraped_at: now - 4*H,  status: 'pending', psd: { apply_email: 'hiring@fathom.health' } },
  { id: 103, description: "Staff role across platform teams: architecture reviews, incident leadership, AWS cost ownership. Hybrid Berlin or remote EU.", platform: 'linkedin',  title: 'Staff Software Engineer',       company: 'Datchworth',     salary: '$180–220k', tags: ['Python','AWS','Leadership'], scraped_at: now - 6*H,  status: 'pending', psd: { easy_apply: true, connection: true } },
  { id: 104, description: "First backend hire. Django + HTMX monolith, you set the conventions. Equity-heavy package, weekly founder pairing.", platform: 'wellfound', title: 'Founding Backend Engineer',     company: 'Relay (YC S25)', salary: '$120k + 1.2%', tags: ['Django','HTMX','0→1'],    scraped_at: now - 8*H,  status: 'pending', psd: { founder: 'Maya Chen' } },
  { id: 105, description: "Platform team: k8s operators, internal dev tooling in Go/Python, golden paths for 80 engineers.", platform: 'remoteok',  title: 'Platform Engineer',             company: 'Sourcegraph',    salary: '$150–185k', tags: ['Go','Python','K8s'],         scraped_at: now - 11*H, status: 'pending', psd: { apply_email: 'talent@sourcegraph.com' } },
  { id: 106, platform: 'linkedin',  title: 'Engineering Manager, Platform', company: 'Vercel',         salary: '—',         tags: ['Management','Infra'],        scraped_at: now - D,    status: 'interview', sent_manually: true,  sent_at: now - 20*H, psd: { easy_apply: true, connection: false } },
  { id: 107, platform: 'wwr',       title: 'Python Developer (Contract)',   company: 'Doist',          salary: '$95/hr',    tags: ['Python','APIs'],             scraped_at: now - D,    status: 'sent', sent_manually: false, sent_at: now - 22*H, psd: { apply_email: 'work@doist.com' } },
  { id: 108, description: "Full-stack across Django + React marketplace. Payments experience a plus.", platform: 'wellfound', title: 'Senior Full-stack Engineer',    company: 'Braintrust',     salary: '$140–175k', tags: ['Django','React'],            scraped_at: now - 30*H, status: 'pending', psd: { founder: 'Adam Jackson' } },
  { id: 109, description: "Python/SQL services for claims processing. On-site 2 days/week.", platform: 'indeed',    title: 'Software Engineer III',         company: 'Cigna Group',    salary: '$125–155k', tags: ['Python','SQL'],              scraped_at: now - 32*H, status: 'pending', psd: {} },
  { id: 110, platform: 'remoteok',  title: 'Backend Engineer, Billing',     company: 'Paddle',         salary: '£85–105k',  tags: ['Python','Stripe','DRF'],     scraped_at: now - 2*D,  status: 'sent', sent_manually: false, sent_at: now - 40*H, psd: { apply_email: 'careers@paddle.com' } },
  { id: 111, description: "Principal IC on payments rails. Python, ISO 20022, four-eyes reviews.", platform: 'linkedin',  title: 'Principal Engineer, Payments',  company: 'Wise',           salary: '—',         tags: ['Python','Payments'],         scraped_at: now - 2*D,  status: 'pending', psd: { easy_apply: false, connection: true } },
  { id: 112, description: "Kraken energy platform: Django, Celery, big Postgres. Mission-driven, EU remote.", platform: 'wwr',       title: 'Django Developer',              company: 'Kraken Tech',    salary: '€90–115k',  tags: ['Django','Energy','Celery'],  scraped_at: now - 3*D,  status: 'pending', psd: { apply_email: 'jobs@kraken.tech' } },
];

export const DOCUMENTS = [
  { id: 1, name: 'CV — Backend (EN).pdf',        kind: 'cv',          tags: ['Django','Python','APIs'], size: '184 KB', updated: 'Jul 12, 2026', ats_score: 92 },
  { id: 2, name: 'CV — Full-stack (EN).pdf',     kind: 'cv',          tags: ['Django','React','HTMX'],  size: '201 KB', updated: 'Jul 12, 2026', ats_score: 87 },
  { id: 3, name: 'CV — Engineering Lead.pdf',    kind: 'cv',          tags: ['Leadership','Infra'],     size: '176 KB', updated: 'Jun 03, 2026', ats_score: 74 },
  { id: 4, name: 'AWS Solutions Architect.pdf',  kind: 'certificate', tags: ['AWS'],                    size: '96 KB',  updated: 'Feb 20, 2026' },
  { id: 5, name: 'BSc Computer Science.pdf',     kind: 'diploma',     tags: [],                         size: '312 KB', updated: 'Jan 08, 2025' },
  { id: 6, name: 'Celery at Scale — talk.pdf',   kind: 'other',       tags: ['Celery','Redis'],         size: '2.1 MB', updated: 'May 30, 2026' },
];

export const PROFILE = {
  name: 'Andrei Popescu', headline: 'Senior Backend Engineer — Django / Python',
  email: 'andrei@popescu.dev', location: 'Bucharest, RO (UTC+2) — remote',
  years: 9, tone: 'direct',
  skills: ['Django','DRF','Python','Celery','Redis','PostgreSQL','HTMX','Playwright','AWS','n8n'],
  summary: '9 years building backend systems in Python/Django — billing platforms, task pipelines and internal tooling. Comfortable owning services end-to-end, from data model to deploy.',
};

// Networking CRM (mirrors GET /contacts/)
export const CONTACTS = [
  { id: 1, name: 'Maya Chen',        role: 'Founder & CEO',       company: 'Relay (YC S25)', source: 'Wellfound', status: 'messaged',   last_touch: 'Jul 28', next_action: 'Follow up if no reply by Aug 4' },
  { id: 2, name: 'Tom Okafor',       role: 'Eng Manager',         company: 'Vercel',         source: 'LinkedIn',  status: 'replied',    last_touch: 'Jul 30', next_action: 'Send CV — he offered referral' },
  { id: 3, name: 'Sara Lindqvist',   role: 'Head of Talent',      company: 'Kraken Tech',    source: 'LinkedIn',  status: 'to_contact', last_touch: '—',      next_action: 'Intro message re: Django role' },
  { id: 4, name: 'Diego Fernández',  role: 'Staff Engineer',      company: 'Paddle',         source: 'Referral',  status: 'referred',   last_touch: 'Jul 22', next_action: 'Thank-you note after interview' },
  { id: 5, name: 'Priya Nair',       role: 'Technical Recruiter', company: 'Sourcegraph',    source: 'RemoteOK',  status: 'messaged',   last_touch: 'Jul 26', next_action: 'Awaiting screening slot' },
];

// Skill match — % of job tags covered by profile skills (mirrors GET /jobs/:id/skill-match/)
export function skillMatch(job, profile) {
  const tags = job.tags || [];
  const matched = tags.filter(t => profile.skills.includes(t));
  const missing = tags.filter(t => !profile.skills.includes(t));
  // Zero-guard: a real scraped job can arrive with no tags — 0%, never NaN%.
  return { score: tags.length ? Math.round((matched.length / tags.length) * 100) : 0, matched, missing };
}

// Typeahead datasets — LinkedIn-style exact-term suggestions (GET /api/v1/suggest/?type=&q=)
export const SUGGEST = {
  titles: [
    'AI Software Developer', 'AI Engineer', 'Artificial Intelligence Consultant', 'Automation Architect', 'Automation Engineer',
    'Backend Developer', 'Backend Engineer', 'Cloud Architect', 'Cloud Engineer', 'Data Engineer', 'Data Scientist',
    'DevOps Developer', 'DevOps Engineer', 'Django Developer', 'Engineering Manager', 'Full-stack Developer',
    'Full-stack Engineer', 'Machine Learning Engineer', 'Platform Engineer', 'Principal Engineer', 'Python Developer',
    'Python Engineer', 'Site Reliability Engineer', 'Software Architect', 'Software Engineer', 'Solutions Architect',
    'Staff Software Engineer', 'Systems Engineer', 'Technical Lead', 'VP of Engineering',
  ],
  locations: [
    'Amsterdam, Netherlands', 'Austin, Texas, United States', 'Barcelona, Spain', 'Berlin, Germany', 'Bucharest, Romania',
    'Cluj-Napoca, Romania', 'Cologne, Germany', 'Copenhagen, Denmark', 'DACH', 'Dubai, United Arab Emirates',
    'Dublin, Ireland', 'EMEA', 'European Union', 'Frankfurt, Germany', 'Hamburg, Germany', 'Lisbon, Portugal',
    'London, United Kingdom', 'Madrid, Spain', 'Munich, Germany', 'Paris, France', 'Riyadh, Saudi Arabia',
    'Romania', 'Stuttgart, Germany', 'Timișoara, Romania', 'Vienna, Austria', 'Warsaw, Poland', 'Zurich, Switzerland',
  ],
  remote: [
    'Austria', 'DACH', 'Europe', 'European Union', 'Germany', 'Netherlands', 'Poland', 'Portugal', 'Romania',
    'Saudi Arabia', 'Spain', 'Switzerland', 'United Arab Emirates', 'United Kingdom', 'Worldwide',
  ],
};

// Canva Brand Templates (mirrors GET /integrations/canva/templates/?type=cv|cover)
export const CANVA_TEMPLATES = {
  cv: [
    { id: 'cv1',  name: 'Geneva',     layout: 'sidebar',  accent: '#0d6e64' },
    { id: 'cv2',  name: 'Helsinki',   layout: 'band',     accent: '#17191d' },
    { id: 'cv3',  name: 'Porto',      layout: 'two-col',  accent: '#b3382c' },
    { id: 'cv4',  name: 'Kyoto',      layout: 'minimal',  accent: '#9a6a00' },
    { id: 'cv5',  name: 'Oslo',       layout: 'centered', accent: '#5b5bd6' },
    { id: 'cv6',  name: 'Marseille',  layout: 'sidebar',  accent: '#1a7f37' },
    { id: 'cv7',  name: 'Austin',     layout: 'band',     accent: '#0d6e64' },
    { id: 'cv8',  name: 'Tallinn',    layout: 'two-col',  accent: '#17191d' },
    { id: 'cv9',  name: 'Bergen',     layout: 'minimal',  accent: '#5b5bd6' },
    { id: 'cv10', name: 'Valencia',   layout: 'centered', accent: '#b3382c' },
  ],
  cover: [
    { id: 'cl1',  name: 'Letterhead Classic', layout: 'band',     accent: '#17191d' },
    { id: 'cl2',  name: 'Monogram',           layout: 'centered', accent: '#0d6e64' },
    { id: 'cl3',  name: 'Margin Note',        layout: 'sidebar',  accent: '#9a6a00' },
    { id: 'cl4',  name: 'Plainspoken',        layout: 'minimal',  accent: '#6b6e74' },
    { id: 'cl5',  name: 'Split Header',       layout: 'two-col',  accent: '#5b5bd6' },
    { id: 'cl6',  name: 'Signature',          layout: 'minimal',  accent: '#0d6e64' },
    { id: 'cl7',  name: 'Bold Address',       layout: 'band',     accent: '#b3382c' },
    { id: 'cl8',  name: 'Studio',             layout: 'sidebar',  accent: '#17191d' },
    { id: 'cl9',  name: 'Centerline',         layout: 'centered', accent: '#1a7f37' },
    { id: 'cl10', name: 'Typewriter',         layout: 'minimal',  accent: '#9a6a00' },
  ],
};

// AI content generation — tone adapted per platform (mirrors POST /jobs/:id/generate/).
export function generateContent(job, profile) {
  const p = job.platform, first = profile.name.split(' ')[0];
  if (p === 'linkedin') return {
    message: `Hi — I saw the ${job.title} opening at ${job.company}. I've spent ${profile.years} years on ${job.tags.slice(0,2).join(' and ')} systems and I think there's a strong overlap with what your team is building. Happy to share specifics — CV attached. — ${first}`,
    cover_letter: null,
  };
  if (p === 'wellfound') return {
    message: `Hey ${job.psd.founder || 'there'} — ${first} here. ${job.company} caught my eye because you're solving this at the data-model level, not with another dashboard. I've built ${job.tags[0]} systems for ${profile.years} years, mostly 0→1, and I'd genuinely enjoy doing it again at your stage. Short intro call this week?`,
    cover_letter: null,
  };
  return {
    message: `Subject: Application — ${job.title}\n\nDear ${job.company} team,\n\nI'm applying for the ${job.title} role. Over ${profile.years} years I've shipped production systems in ${job.tags.join(', ')}, most recently ${profile.headline.toLowerCase().replace('senior backend engineer — ','on ')} platforms at scale.\n\nMy CV and cover letter are attached. I'm available for an interview at your convenience.\n\nBest regards,\n${profile.name}`,
    cover_letter: `Dear Hiring Team,\n\n${job.company}'s ${job.title} role matches my background closely: ${profile.summary}\n\nSpecifically relevant to this position: ${job.tags.join(', ')}. I keep review cycles short and documentation honest.\n\nSincerely,\n${profile.name}`,
  };
}

export function suggestDocuments(job) {
  const scored = DOCUMENTS.map(d => ({ d, s: d.tags.filter(t => job.tags.includes(t)).length + (d.kind === 'cv' ? 1 : 0) }));
  scored.sort((a, b) => b.s - a.s);
  return scored.slice(0, 3).map(x => x.d.id);
}

// ── Career Advisor algorithm (spec for the future Python implementation) ──
// Composite score = 0.40·skill_coverage + 0.25·conversion + 0.20·activity + 0.15·targeting
// All components normalized 0–100. Rule engine below emits prioritized recommendations
// with the evidence that triggered each rule — the Python port must keep this explainability.
export function careerAnalysis(jobs, profile) {
  const done = j => ['sent', 'interview', 'offer', 'rejected'].includes(j.status);
  const sent = jobs.filter(done);
  const interviews = jobs.filter(j => ['interview', 'offer'].includes(j.status));
  // 1. market demand: tag frequency across all scraped listings
  const demand = {};
  jobs.forEach(j => j.tags.forEach(t => { demand[t] = (demand[t] || 0) + 1; }));
  const topDemand = Object.entries(demand).sort((a, b) => b[1] - a[1]).slice(0, 8);
  const covered = topDemand.filter(([t]) => profile.skills.includes(t));
  const gaps = topDemand.filter(([t]) => !profile.skills.includes(t));
  const skillCoverage = Math.round((covered.length / Math.max(1, topDemand.length)) * 100);
  // 2. conversion: interviews / sent
  const conversion = sent.length ? Math.round((interviews.length / sent.length) * 100) : 0;
  // 3. activity: sent volume vs a 10/week healthy baseline
  const activity = Math.min(100, Math.round((sent.length / 10) * 100));
  // 4. targeting: average skill match of applications actually sent
  const sentMatches = sent.map(j => skillMatch(j, profile).score);
  const targeting = sentMatches.length ? Math.round(sentMatches.reduce((a, b) => a + b, 0) / sentMatches.length) : 0;
  const score = Math.round(0.40 * skillCoverage + 0.25 * Math.min(100, conversion * 2.5) + 0.20 * activity + 0.15 * targeting);
  // rule engine → recommendations with evidence
  const recs = [];
  gaps.slice(0, 2).forEach(([t, n]) => recs.push({
    prio: 'high', title: `Close the ${t} gap`,
    why: `${t} appears in ${n} of ${jobs.length} scraped listings but is missing from your profile.`,
    action: `Add a ${t} project or certification, then attach it in Documents.`,
  }));
  if (targeting < 70 && sent.length) recs.push({
    prio: 'high', title: 'Raise your application bar',
    why: `Average match of sent applications is ${targeting}% — applying below 70% wastes quota and hurts response rate.`,
    action: 'Filter the dashboard to ≥ 80% matches before pressing Start.',
  });
  if (conversion < 20 && sent.length >= 3) recs.push({
    prio: 'medium', title: 'Interview conversion is low',
    why: `${interviews.length} interviews from ${sent.length} sent (${conversion}%). Healthy range: 20–35%.`,
    action: 'Regenerate cover letters against the AI Summary keywords and send follow-ups after 7 days.',
  });
  const liSent = sent.filter(j => j.platform === 'linkedin').length;
  if (liSent === 0) recs.push({
    prio: 'medium', title: 'Activate your network channel',
    why: 'No LinkedIn applications yet — referred candidates convert ~4× better than cold feeds.',
    action: 'Use the Network CRM: message the 2 contacts with existing connections first.',
  });
  recs.push({
    prio: 'low', title: 'Keep documents fresh',
    why: 'CVs older than 60 days score lower in ATS re-scans.',
    action: 'Regenerate your top CV in Canva template "Geneva" with current skills.',
  });
  // career paths: fit = overlap between path requirements and profile skills
  const paths = [
    { role: 'Staff / Principal Backend Engineer', horizon: '12–18 months', needs: ['Django', 'AWS', 'Leadership', 'PostgreSQL'] },
    { role: 'AI Platform Engineer', horizon: '6–12 months', needs: ['Python', 'Django', 'AWS', 'LLM APIs'] },
    { role: 'Engineering Manager', horizon: '18–24 months', needs: ['Leadership', 'Hiring', 'Django', 'Agile'] },
  ].map(p => {
    const have = p.needs.filter(n => profile.skills.includes(n));
    return { ...p, fit: Math.round((have.length / p.needs.length) * 100), missing: p.needs.filter(n => !profile.skills.includes(n)) };
  }).sort((a, b) => b.fit - a.fit);
  return {
    score, components: [
      ['Skill coverage', skillCoverage, 0.40, `${covered.length}/${topDemand.length} top-demand skills covered`],
      ['Conversion', Math.min(100, conversion * 2.5), 0.25, `${interviews.length} interviews / ${sent.length} sent`],
      ['Activity', activity, 0.20, `${sent.length} applications vs 10/week baseline`],
      ['Targeting', targeting, 0.15, `avg. match of sent applications`],
    ],
    demand: topDemand.map(([tag, count]) => ({ tag, count, covered: profile.skills.includes(tag) })),
    recs, paths,
  };
}

// ── Funnel analytics (CRM view over the pipeline) ──
export function funnelAnalysis(jobs, platforms) {
  const stages = ['pending', 'review', 'sent', 'interview', 'offer'];
  const reached = st => jobs.filter(j => {
    const order = ['pending', 'running', 'review', 'sent', 'interview', 'offer'];
    const ji = order.indexOf(j.status === 'rejected' ? 'sent' : j.status);
    return ji >= order.indexOf(st);
  }).length;
  const funnel = stages.map(st => ({ stage: st, count: st === 'pending' ? jobs.length : reached(st) }));
  const byPlatform = platforms.map(p => {
    const pj = jobs.filter(j => j.platform === p.id);
    const psent = pj.filter(j => ['sent', 'interview', 'offer', 'rejected'].includes(j.status));
    const pint = pj.filter(j => ['interview', 'offer'].includes(j.status));
    return { name: p.name, listings: pj.length, sent: psent.length, interviews: pint.length, rate: psent.length ? Math.round((pint.length / psent.length) * 100) : 0 };
  }).filter(r => r.listings > 0);
  return { funnel, byPlatform };
}
