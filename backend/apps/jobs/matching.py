"""Deterministic job matcher, ported from Job_app ``filters/relevance.py``.

Pure functions: no network, no AI, no DB. ``prescore`` is a verbatim-in-behavior
port (comments are the original Romanian); ``skill_match`` is the findajob
convenience built on the same tokenizer.
"""

import re

# Scor special: jobul e respins din start (ex. rol de conducere).
HARD_REJECT = -1

# Greutăți de scor. Un skill în titlu contează mult mai mult decât în
# descriere: descrierile enumeră tehnologii pe care firma le folosește
# undeva, titlul spune ce cauți de fapt.
_W_PRIMARY_TITLE = 20
_W_PRIMARY_DESC = 8
_W_SECONDARY_TITLE = 8
_W_SECONDARY_DESC = 3

# Bonus pentru un keyword de căutare al pieței găsit în titlu: jobul e exact
# ce am căutat, nu doar unul care menționează un skill în treacăt.
_W_MARKET_KEYWORD = 15
# Bonusul total de keyword e plafonat: două keyword-uri potrivite ajung ca
# semnal; mai multe nu fac jobul mai relevant.
_MAX_KEYWORD_BONUS = 30

# Bonus pentru semnale de flexibilitate (part-time/freelance/remote…): profilul
# caută explicit angajamente sub 40h, deci flexibilitatea E relevanță, nu
# doar un beneficiu. Plafonat: două semnale ajung.
_W_FLEX = 10
_MAX_FLEX_BONUS = 20

# Penalizare pentru rol greșit în titlu (sales, consultant SAP…): mare cât să
# scufunde jobul sub min_prescore, dar NU hard-reject — un titlu cu multe
# semnale bune poate compensa (spre deosebire de exclude_keywords).
_W_NEGATIVE = -60

# Separatorii după care titlul trece de la ROL la calificativ („– Conversational
# AI & Sales Solutions", ", Sales Enablement", "(m/w/d)"). Penalizarea de rol se
# uită doar în capul titlului: „Junior AI Engineer – … & Sales Solutions" e un
# post de AI Engineer, nu de vânzări, dar cădea la 0 (găsit live 2026-08-06).
# Cratima intră în listă DOAR încadrată de spații: „Pre-Sales"/„Full-Stack" sunt
# cuvinte compuse, nu titlu urmat de calificativ.
_TITLE_QUALIFIER_SPLIT = re.compile(r"[–—|(,:]|\s-\s")


def _role_head(title: str) -> str:
    """Partea din titlu care spune ce E jobul, fără calificative."""
    return _TITLE_QUALIFIER_SPLIT.split(title, maxsplit=1)[0]


def skill_tokens(skill: str) -> list[str]:
    """Sparge un skill din config în termeni căutabili.

    „AI/ML" → ["ai", "ml"]; „LLM APIs (OpenAI, Claude)" → ["llm apis"].
    Parantezele se aruncă: sunt exemple, nu termeni de căutat.
    """
    without_parens = re.sub(r"\([^)]*\)", "", skill)
    parts = [p.strip().lower() for p in without_parens.split("/")]
    return [p for p in parts if p]


def _contains(text: str, token: str) -> bool:
    """Potrivire pe cuvânt întreg, ca „ml" să nu se prindă în „html"."""
    return re.search(rf"\b{re.escape(token)}\b", text) is not None


def _matches(text: str, skill: str) -> bool:
    return any(_contains(text, tok) for tok in skill_tokens(skill))


def _market_keywords(job: dict, config: dict) -> list[str]:
    """Keyword-urile de căutare relevante pentru piața jobului.

    Joburile fără câmp `market` (ex. career pages, scraper pre-piețe) primesc
    reuniunea keyword-urilor din toate piețele.
    """
    markets_cfg = (config.get("scraping", {}) or {}).get("markets", {}) or {}
    market_key = job.get("market")
    if market_key in markets_cfg:
        groups = markets_cfg[market_key].get("keyword_groups", []) or []
    else:
        groups = [g for m in markets_cfg.values()
                  for g in (m.get("keyword_groups", []) or [])]
    return [kw for group in groups for kw in group]


def prescore(job: dict, config: dict,
             search_keywords: list[str] | None = None) -> tuple[int, list[str]]:
    """Calculează un scor 0-100 (sau HARD_REJECT) plus motivele.

    Motivele ajung în coloana „Keywords Match" din Sheet, ca să fie
    auditabil de ce a trecut un job.

    `search_keywords` e pentru căutările AD-HOC (dashboard), care nu au o
    piață în `scraping.markets`: fără el, `_market_keywords` cade pe reuniunea
    keyword-urilor TUTUROR piețelor, deci o căutare de „Game Designer" era
    punctată — și ordonată, tabelul sortează implicit pe `-prescore` — cu
    bonusurile „Python Developer"/„AI Engineer" și nu primea niciodată un
    motiv `cautare: …` pentru ce s-a căutat efectiv. Calea CLI nu-l dă, deci
    scoringul pe piețe rămâne bit-identic.
    """
    title = str(job.get("title") or "")
    description = str(job.get("description") or "")
    title_l = title.lower()
    text_l = f"{title_l}\n{description.lower()}"

    filters = (config.get("scraping", {}) or {}).get("filters", {}) or {}
    skills = config.get("skills", {}) or {}

    # ── Excludere dură: doar pe titlu ───────────────────────────
    # Pe descriere ar respinge joburi bune care doar menționează
    # „berichtest an den Director".
    # Potrivire pe cuvânt întreg, la fel ca la skill-uri: cu `in` simplu,
    # keyword-ul „VP" respingea „MVP Launch Engineer".
    for excluded in filters.get("exclude_keywords", []) or []:
        if _contains(title_l, excluded.lower()):
            return HARD_REJECT, [f"exclus: {excluded}"]

    score = 0
    reasons: list[str] = []

    # ── Penalizare rol nepotrivit: doar pe CAPUL titlului, cuvânt întreg ──
    # (pe descriere ar penaliza „work with the sales team"; pe calificativ ar
    # penaliza „AI Engineer – … & Sales Solutions", care e tocmai un post bun)
    role_head = _role_head(title_l)
    for negative in filters.get("negative_keywords", []) or []:
        if _contains(role_head, negative.lower()):
            score += _W_NEGATIVE
            reasons.append(f"rol nepotrivit: {negative}")

    for skill in skills.get("primary", []) or []:
        if _matches(title_l, skill):
            score += _W_PRIMARY_TITLE
            reasons.append(skill)
        elif _matches(text_l, skill):
            score += _W_PRIMARY_DESC
            reasons.append(skill)

    for skill in skills.get("secondary", []) or []:
        if _matches(title_l, skill):
            score += _W_SECONDARY_TITLE
            reasons.append(skill)
        elif _matches(text_l, skill):
            score += _W_SECONDARY_DESC
            reasons.append(skill)

    # ── Bonus keyword de piață (spec §5.3) ─────────────────────
    # Un titlu care conține chiar interogarea căutată („AI Engineer") e
    # semnalul cel mai puternic — fără bonus, astfel de joburi cădeau sub
    # prag dacă titlul nu repeta și un skill primar (ex. „Python").
    keyword_bonus = 0
    keywords = (search_keywords if search_keywords is not None
                else _market_keywords(job, config))
    for keyword in keywords:
        if _contains(title_l, keyword.lower()):
            keyword_bonus += _W_MARKET_KEYWORD
            reasons.append(f"cautare: {keyword}")
    score += min(keyword_bonus, _MAX_KEYWORD_BONUS)

    # ── Bonus flexibilitate: titlu SAU descriere ────────────────
    # (aici descrierea contează — „remote, part-time possible" apare rar în titlu)
    flex_bonus = 0
    for signal in filters.get("flex_signals", []) or []:
        if _contains(text_l, signal.lower()):
            flex_bonus += _W_FLEX
            reasons.append(f"flexibil: {signal}")
    score += min(flex_bonus, _MAX_FLEX_BONUS)

    return max(0, min(score, 100)), reasons


def requires_email(job: dict, config: dict) -> bool:
    """True dacă jobul are nevoie de adresă de contact ca să fie reținut.

    Regula e per PIAȚĂ, nu globală, fiindcă emailul nu există peste tot:
    anunțurile DE/NL scriu „Bewerbung an …", pe când cele din România (și din
    piețele remote/EN) trimit la un portal — măsurat 2026-08-06, eJobs dă un
    email la 106 anunțuri. O regulă globală ar goli exact piețele fără email.
    `scraping.markets.<piață>.require_email` bate `scraping.filters.
    require_email`; joburile fără piață cunoscută urmează regula globală.
    """
    scraping = config.get("scraping", {}) or {}
    market_cfg = (scraping.get("markets", {}) or {}).get(job.get("market")) or {}
    if "require_email" in market_cfg:
        return bool(market_cfg["require_email"])
    return bool((scraping.get("filters", {}) or {}).get("require_email", False))


def passes_prefilter(job: dict, config: dict) -> bool:
    """True dacă jobul merită cheltuiala de analiză AI."""
    score, _ = prescore(job, config)
    if score == HARD_REJECT:
        return False
    filters = (config.get("scraping", {}) or {}).get("filters", {}) or {}
    return score >= int(filters.get("min_prescore", 0))


def skill_match(job_listing, search_profile) -> dict:
    """Skill coverage of one JobListing against a search profile.

    `search_profile` is duck-typed: anything with ``to_matching_config()``
    returning the Job_app config shape (``skills.primary`` / ``skills.secondary``).
    Text searched: title + description + tags + requirements, whole-word.
    Score = share of profile skills found, 0-100. A profile with no skills
    scores 0 (no division by zero).
    """
    config = search_profile.to_matching_config() or {}
    skills_cfg = config.get("skills", {}) or {}
    skills = [*(skills_cfg.get("primary", []) or []),
              *(skills_cfg.get("secondary", []) or [])]

    tags = job_listing.tags or []
    text_l = "\n".join([
        str(job_listing.title or ""),
        str(job_listing.description or ""),
        " ".join(str(t) for t in tags),
        str(job_listing.requirements or ""),
    ]).lower()

    matched = [s for s in skills if _matches(text_l, s)]
    missing = [s for s in skills if s not in matched]
    score = round(100 * len(matched) / len(skills)) if skills else 0
    return {"score": score, "matched": matched, "missing": missing}
