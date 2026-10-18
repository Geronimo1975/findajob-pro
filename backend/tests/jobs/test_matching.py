"""Deterministic matcher — ported 1:1 from Job_app tests/test_relevance.py.

Pure functions, no DB, except the skill_match tests at the bottom.
"""

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from apps.jobs.matching import (
    HARD_REJECT,
    passes_prefilter,
    prescore,
    requires_email,
    skill_match,
    skill_tokens,
)
from apps.jobs.models import JobListing, Platform


CFG = {
    "skills": {
        "primary": ["Python", "Django", "AI/ML"],
        "secondary": ["Docker", "PostgreSQL"],
    },
    "scraping": {
        "filters": {
            "exclude_keywords": ["Director", "VP", "Head of"],
            "min_prescore": 40,
        }
    },
}


def _job(title, description="", **extra):
    job = {"title": title, "description": description}
    job.update(extra)
    return job


class TestSkillTokens:
    def test_splits_on_slash(self):
        assert skill_tokens("AI/ML") == ["ai", "ml"]

    def test_strips_parenthetical(self):
        assert skill_tokens("LLM APIs (OpenAI, Claude)") == ["llm apis"]

    def test_simple_skill(self):
        assert skill_tokens("Python") == ["python"]


class TestHardReject:
    def test_excluded_keyword_in_title_rejects(self):
        score, reasons = prescore(_job("Director of Engineering"), CFG)
        assert score == HARD_REJECT
        assert any("Director" in r for r in reasons)

    def test_exclusion_is_case_insensitive(self):
        score, _ = prescore(_job("vp of python engineering"), CFG)
        assert score == HARD_REJECT

    def test_exclusion_only_on_title_not_description(self):
        """Un job normal care menționează „Director" în text nu se respinge."""
        score, _ = prescore(
            _job("Python Developer", "Du berichtest an den Director."), CFG
        )
        assert score > 0

    def test_rejected_job_does_not_pass(self):
        assert passes_prefilter(_job("VP Engineering"), CFG) is False

    def test_exclusion_matches_whole_words_only(self):
        """„VP" nu trebuie să respingă „MVP"."""
        score, _ = prescore(_job("MVP Launch Engineer (Python)"), CFG)
        assert score > 0

    def test_multiword_exclusion_still_rejects(self):
        """Reparația nu trebuie să strice keyword-urile din mai multe cuvinte."""
        score, reasons = prescore(_job("Head of Data Platform"), CFG)
        assert score == HARD_REJECT
        assert any("Head of" in r for r in reasons)


class TestScoring:
    def test_primary_skill_in_title_scores_high(self):
        score, reasons = prescore(_job("Python Developer"), CFG)
        assert score >= 20
        assert "Python" in reasons

    def test_primary_skill_in_description_scores_less_than_title(self):
        in_title, _ = prescore(_job("Python Developer"), CFG)
        in_desc, _ = prescore(_job("Backend Developer", "Wir nutzen Python."), CFG)
        assert in_desc < in_title
        assert in_desc > 0

    def test_secondary_skill_scores_less_than_primary(self):
        primary, _ = prescore(_job("Python Developer"), CFG)
        secondary, _ = prescore(_job("Docker Developer"), CFG)
        assert secondary < primary

    def test_score_is_capped_at_100(self):
        """Șase skill-uri primare în titlu ar da 120 brut — se plafonează la 100."""
        cfg = {
            "skills": {
                "primary": ["Python", "Django", "Java", "Rust", "Kotlin", "Scala"],
                "secondary": [],
            },
            "scraping": {"filters": {"exclude_keywords": [], "min_prescore": 0}},
        }
        score, _ = prescore(
            _job("Python Django Java Rust Kotlin Scala Developer"), cfg
        )
        assert score == 100

    def test_word_boundary_prevents_false_match(self):
        """„ml" nu trebuie să se potrivească în interiorul lui „html"."""
        score, reasons = prescore(_job("HTML Developer"), CFG)
        assert "AI/ML" not in reasons

    def test_irrelevant_job_scores_zero(self):
        score, reasons = prescore(_job("Truck Driver", "Führerschein C+E"), CFG)
        assert score == 0
        assert reasons == []


class TestThreshold:
    def test_score_above_threshold_passes(self):
        assert passes_prefilter(_job("Python Django Developer"), CFG) is True

    def test_score_below_threshold_fails(self):
        assert passes_prefilter(_job("Truck Driver"), CFG) is False

    def test_missing_filters_config_defaults_to_permissive(self):
        """Fără blocul `filters`, nimic nu se respinge pe prag."""
        score, _ = prescore(_job("Python Developer"), {"skills": {"primary": ["Python"]}})
        assert score > 0
        assert passes_prefilter(_job("Python Developer"), {"skills": {"primary": ["Python"]}}) is True


CFG_MARKETS = {
    "skills": {"primary": ["Python", "Django", "AI/ML"], "secondary": ["Docker"]},
    "scraping": {
        "filters": {"exclude_keywords": ["Director"], "min_prescore": 40},
        "markets": {
            "de": {"keyword_groups": [["Python Developer", "Backend Engineer"],
                                       ["AI Engineer", "Machine Learning Engineer"]]},
            "ro_remote": {"keyword_groups": [["Django Developer"]]},
        },
    },
}


class TestMarketKeywordBonus:
    def test_search_keyword_in_title_passes_threshold(self):
        """„AI Engineer (m/w/d)" e chiar interogarea căutată — trebuie să treacă
        pragul chiar dacă titlul nu conține un skill primar ca „Python"."""
        job = _job("AI Engineer (m/w/d)", "Wir nutzen Python und Docker.", market="de")
        score, reasons = prescore(job, CFG_MARKETS)
        assert score >= 40
        assert any("AI Engineer" in r for r in reasons)

    def test_keyword_bonus_is_capped(self):
        """Trei keyword-uri potrivite nu dau mai mult bonus decât plafonul."""
        job = _job("Python Developer / Backend Engineer / AI Engineer", "", market="de")
        with_cap, _ = prescore(job, CFG_MARKETS)
        # scorul fără plafon ar fi cu 15 mai mare (3×15=45 > plafon 30)
        assert with_cap <= 100
        # verificare directă a plafonului: bonusul efectiv e exact 30, nu 45.
        # titlul conține „Python" (primar, +20) și „ai" ca token separat din
        # skill-ul primar „AI/ML" (via „AI Engineer", +20) → 20+20+30 = 70
        assert with_cap == 70

    def test_job_without_market_uses_all_markets(self):
        """Career pages (fără câmp market): reuniunea keyword-urilor."""
        job = _job("Django Developer", "")  # doar în ro_remote
        score, reasons = prescore(job, CFG_MARKETS)
        assert any("Django Developer" in r for r in reasons)

    def test_no_keyword_no_bonus(self):
        job = _job("Truck Driver", "", market="de")
        score, reasons = prescore(job, CFG_MARKETS)
        assert score == 0
        assert not any(r.startswith("cautare:") for r in reasons)

    def test_keyword_matches_as_phrase_with_boundaries(self):
        """„AI Engineer" se potrivește în „Senior AI Engineer", nu fragmentat."""
        score_senior, _ = prescore(_job("Senior AI Engineer", "", market="de"), CFG_MARKETS)
        score_split, reasons_split = prescore(_job("AI Systems / Test Engineer", "", market="de"), CFG_MARKETS)
        assert score_senior >= 15
        assert not any("AI Engineer" in r for r in reasons_split if r.startswith("cautare:"))


class TestAdHocSearchKeywords:
    """Căutările din dashboard n-au piață în `scraping.markets`."""

    def test_adhoc_keyword_absent_from_settings_earns_its_reason(self):
        """Fără `search_keywords`, „Game Designer" nu există în nicio piață,
        deci n-ar primi niciodată bonus, iar scorul (și ordinea implicită a
        tabelului, `-prescore`) s-ar baza pe keyword-urile altor căutări."""
        job = _job("Senior Game Designer", "Unity, level design.")
        score, reasons = prescore(job, CFG_MARKETS,
                                  search_keywords=["Game Designer"])
        assert "cautare: Game Designer" in reasons
        assert score >= 15

    def test_adhoc_keywords_replace_the_market_union(self):
        """Un titlu care ar fi luat bonus din reuniunea piețelor NU-l mai ia
        când căutarea a fost pe altceva."""
        job = _job("Django Developer", "")
        _, market_reasons = prescore(job, CFG_MARKETS)
        _, adhoc_reasons = prescore(job, CFG_MARKETS,
                                    search_keywords=["Game Designer"])
        assert any("Django Developer" in r for r in market_reasons)
        assert not any(r.startswith("cautare:") for r in adhoc_reasons)

    def test_cli_market_path_is_unchanged(self):
        """Fără argumentul nou, comportamentul e cel dinainte (calea CLI)."""
        job = _job("AI Engineer (m/w/d)", "Wir nutzen Python und Docker.", market="de")
        assert prescore(job, CFG_MARKETS) == prescore(job, CFG_MARKETS,
                                                      search_keywords=None)
        score, reasons = prescore(job, CFG_MARKETS)
        assert score >= 40
        assert "cautare: AI Engineer" in reasons


class TestNegativeKeywords:
    CFG = {
        "scraping": {"filters": {
            "min_prescore": 40,
            "negative_keywords": ["sales", "SAP consultant", "recruiter"],
        }},
        "skills": {"primary": ["n8n"], "secondary": []},
    }

    def test_negative_keyword_in_title_sinks_score(self):
        job = {"title": "Sales Manager n8n", "description": ""}
        score, reasons = prescore(job, self.CFG)
        assert score == 0                      # 20 (primary în titlu) - 60, plafonat la 0
        assert "rol nepotrivit: sales" in reasons

    def test_negative_matches_whole_words_only(self):
        # „sales" nu trebuie să se prindă în „Salesforce" — potrivire pe cuvânt întreg
        job = {"title": "Salesforce n8n Developer", "description": ""}
        score, reasons = prescore(job, self.CFG)
        assert not any(r.startswith("rol nepotrivit") for r in reasons)

    def test_negative_only_on_title_not_description(self):
        # Descrierile menționează des alte departamente („work with sales team")
        job = {"title": "n8n Automation Engineer", "description": "you support the sales team"}
        score, reasons = prescore(job, self.CFG)
        assert not any(r.startswith("rol nepotrivit") for r in reasons)

    def test_multiword_negative_rejects(self):
        job = {"title": "SAP Consultant (m/w/d)", "description": ""}
        score, reasons = prescore(job, self.CFG)
        assert "rol nepotrivit: SAP consultant" in reasons

    def test_negative_after_a_separator_is_a_qualifier_not_the_role(self):
        """„Junior AI Engineer – Conversational AI & Sales Solutions" (freenet,
        găsit live 2026-08-06) cădea la 0: „Sales" din calificativ declanșa
        −60, deși rolul e AI Engineer. Capul titlului spune ce e jobul;
        ce urmează după separator spune pentru ce domeniu.
        """
        cfg = {
            "scraping": {"filters": {"min_prescore": 40, "negative_keywords": ["sales"]}},
            "skills": {"primary": ["Conversational AI"], "secondary": []},
        }
        job = {"title": "Junior AI Engineer – Conversational AI & Sales Solutions",
               "description": ""}
        score, reasons = prescore(job, cfg)
        assert not any(r.startswith("rol nepotrivit") for r in reasons)
        assert score == 20                     # skill primar în titlu, fără −60

    def test_negative_in_the_head_still_sinks_the_score(self):
        """Regula nu trebuie să dezarmeze penalizarea: dacă rolul ÎNSUȘI e de
        vânzări, keyword-ul e în cap și −60 se aplică."""
        job = {"title": "Senior Solution Sales Executive - Finance", "description": ""}
        score, reasons = prescore(job, self.CFG)
        assert "rol nepotrivit: sales" in reasons

    def test_separator_rule_does_not_rescue_a_plain_sales_title(self):
        """`Sales Manager n8n` n-are separator: capul e tot titlul."""
        job = {"title": "Sales Manager n8n", "description": ""}
        score, reasons = prescore(job, self.CFG)
        assert "rol nepotrivit: sales" in reasons
        assert score == 0

    def test_hyphen_inside_a_word_is_not_a_separator(self):
        """`Pre-Sales Manager` e tot un rol de vânzări — cratima din interiorul
        cuvântului nu deschide un calificativ (spre deosebire de „ - ")."""
        job = {"title": "Pre-Sales Manager", "description": ""}
        _, reasons = prescore(job, self.CFG)
        assert "rol nepotrivit: sales" in reasons

    def test_comma_also_opens_a_qualifier(self):
        job = {"title": "n8n Engineer, Sales Enablement", "description": ""}
        _, reasons = prescore(job, self.CFG)
        assert not any(r.startswith("rol nepotrivit") for r in reasons)

    def test_strong_signals_can_survive_penalty(self):
        # Penalizarea e mare, dar nu HARD_REJECT: semnale multe pot compensa
        cfg = {
            "scraping": {"filters": {"min_prescore": 40,
                                     "negative_keywords": ["recruiter"]}},
            "skills": {"primary": ["n8n", "chatbot", "Claude Code",
                                   "workflow automation", "conversational AI"],
                       "secondary": []},
        }
        job = {"title": "Recruiter for n8n chatbot Claude Code "
                        "workflow automation conversational AI team",
               "description": ""}
        score, _ = prescore(job, cfg)
        assert score == 40                     # 5×20 - 60 = 40 → exact pragul


class TestFlexSignals:
    CFG = {
        "scraping": {"filters": {
            "min_prescore": 40,
            "flex_signals": ["part-time", "freelance", "flexible hours", "remote"],
        }},
        "skills": {"primary": ["chatbot"], "secondary": []},
    }

    def test_flex_signal_in_title_adds_bonus(self):
        base = {"title": "Chatbot Developer", "description": ""}
        flex = {"title": "Chatbot Developer (part-time)", "description": ""}
        s_base, _ = prescore(base, self.CFG)
        s_flex, reasons = prescore(flex, self.CFG)
        assert s_flex == s_base + 10
        assert "flexibil: part-time" in reasons

    def test_flex_signal_in_description_counts(self):
        job = {"title": "Chatbot Developer",
               "description": "freelance collaboration, flexible hours"}
        _, reasons = prescore(job, self.CFG)
        assert "flexibil: freelance" in reasons
        assert "flexibil: flexible hours" in reasons

    def test_flex_bonus_is_capped(self):
        job = {"title": "Chatbot Developer",
               "description": "remote, part-time, freelance, flexible hours"}
        s, _ = prescore(job, self.CFG)
        base = {"title": "Chatbot Developer", "description": ""}
        s_base, _ = prescore(base, self.CFG)
        assert s == s_base + 20                # 4 semnale × 10, plafonat la 20

    def test_no_signals_no_bonus_no_reasons(self):
        job = {"title": "Chatbot Developer", "description": "on-site role"}
        _, reasons = prescore(job, self.CFG)
        assert not any(r.startswith("flexibil") for r in reasons)


class TestInteraction:
    """Interacțiunea penalizare rol nepotrivit × bonus flex × bonus keyword piață."""

    CFG = {
        "scraping": {
            "filters": {
                "min_prescore": 30,
                "negative_keywords": ["sales"],
                "flex_signals": ["freelance"],
            },
            "markets": {
                "ro_remote": {"keyword_groups": [["chatbot developer"]]},
            },
        },
        "skills": {"primary": [], "secondary": []},
    }

    def test_market_bonus_flex_and_negative_sum_and_clamp(self):
        job = {
            "title": "Sales Chatbot Developer",
            "description": "freelance role available",
            "market": "ro_remote",
        }
        score, reasons = prescore(job, self.CFG)

        # bonus piață 15 + flex 10 - 60 = -35, plafonat la 0
        assert score == 0
        assert any(r.startswith("cautare:") for r in reasons)
        assert any(r.startswith("flexibil:") for r in reasons)
        assert any(r.startswith("rol nepotrivit:") for r in reasons)


class TestRequiresEmailPerMarket:
    """Emailul e obligatoriu doar acolo unde există: DE/NL scriu „Bewerbung
    an …", pe când anunțurile din România se aplică prin platformă (măsurat
    2026-08-06: 1 email la 106 joburi eJobs). O regulă globală ar goli exact
    piața pe care userul a cerut-o."""

    CFG = {
        "scraping": {
            "filters": {"require_email": True},
            "markets": {
                "de": {"country": "germany", "language": "de", "locations": ["Germany"]},
                "ro_remote": {"country": "romania", "language": "en",
                              "locations": ["Romania"], "require_email": False},
            },
        }
    }

    def test_market_without_override_follows_the_global_rule(self):
        assert requires_email({"market": "de"}, self.CFG) is True

    def test_market_override_wins(self):
        assert requires_email({"market": "ro_remote"}, self.CFG) is False

    def test_unknown_market_follows_the_global_rule(self):
        """Joburile fără piață (career pages) nu au voie să scape regula."""
        assert requires_email({"market": "necunoscuta"}, self.CFG) is True
        assert requires_email({}, self.CFG) is True

    def test_market_can_be_stricter_than_the_global_rule(self):
        cfg = {"scraping": {"filters": {"require_email": False},
                            "markets": {"de": {"require_email": True}}}}
        assert requires_email({"market": "de"}, cfg) is True

    def test_default_is_permissive_when_nothing_is_configured(self):
        assert requires_email({"market": "de"}, {}) is False


# ── skill_match (new in findajob) ───────────────────────────────────

class StubProfile:
    """Duck-typed stand-in: SearchProfile does not exist yet."""

    def __init__(self, primary=(), secondary=()):
        self._cfg = {"skills": {"primary": list(primary),
                                "secondary": list(secondary)}}

    def to_matching_config(self):
        return self._cfg


class SkillMatchTests(TestCase):
    def _listing(self, **extra):
        call_command("seed_platforms", verbosity=0)
        defaults = {
            "title": "Senior Django Engineer",
            "company": "Loftware",
            "url": "https://remoteok.com/jobs/201",
            "description": "We use Docker daily.",
            "requirements": "Strong Python background.",
            "tags": ["DRF"],
            "scraped_at": timezone.now(),
        }
        defaults.update(extra)
        return JobListing.objects.create(
            platform=Platform.objects.get(slug="remoteok"), **defaults)

    def test_scores_over_title_description_tags_requirements(self):
        profile = StubProfile(primary=["Django", "Python", "DRF"],
                              secondary=["Docker", "Kubernetes"])
        result = skill_match(self._listing(), profile)
        assert result["matched"] == ["Django", "Python", "DRF", "Docker"]
        assert result["missing"] == ["Kubernetes"]
        assert result["score"] == 80

    def test_word_boundary_applies(self):
        result = skill_match(self._listing(title="HTML Developer",
                                           description="", requirements="",
                                           tags=[]),
                             StubProfile(primary=["AI/ML"]))
        assert result == {"score": 0, "matched": [], "missing": ["AI/ML"]}

    def test_empty_profile_is_zero_not_a_crash(self):
        result = skill_match(self._listing(), StubProfile())
        assert result == {"score": 0, "matched": [], "missing": []}

    def test_profile_without_skills_key_is_zero(self):
        class Bare:
            def to_matching_config(self):
                return {}
        assert skill_match(self._listing(), Bare())["score"] == 0
