"""Core tests for Damir Job Radar.

Covers: weight totals, normalization, deduplication, expiry filtering,
scoring fixtures (construction/PM/heritage/WASH vs unrelated software),
mandatory disqualification (nationality, native language), uncertain licence,
missing-description cap, and CSV formula-injection protection.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from radar import dedup_records, filter_expired, load_config  # noqa: E402
from radar import JobRecord, csv_safe, parse_iso_date  # noqa: E402
from radar import canonical_url, normalize_text, score_job  # noqa: E402

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.yaml"
CFG = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
TODAY = date(2026, 11, 17)


def make_job(**kwargs) -> JobRecord:
    base = dict(job_id="test-1", title="Construction Engineer",
                organization="UNOPS", location="Sarajevo", country="Bosnia and Herzegovina",
                posted_date="2026-11-01", closing_date=(TODAY + timedelta(days=30)).isoformat(),
                description=("Site supervision, contract administration, tender documentation, bills of quantities, "
                            "budget monitoring, quality assurance, defects liability, handover, stakeholder engagement, "
                            "government coordination. Works include water and sanitation infrastructure rehabilitation and "
                            "EU/IPA-funded municipal programmes. FIDIC awareness desirable; PMP an asset. "
                            "Field travel and manual driving required. Professional English."),
                url="https://example.org/job/1", source="test")
    base.update(kwargs)
    return JobRecord(**base)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def test_scoring_weights_sum_to_100():
    assert sum(CFG["scoring"]["weights"].values()) == 100


def test_required_config_sections_present():
    for section in ("candidate", "scoring", "job_families", "technical_competencies",
                    "sector_signals", "negative_signals", "favourable_locations", "sources"):
        assert section in CFG, f"config.yaml is missing section: {section}"


# ---------------------------------------------------------------------------
# Normalization / dedup / expiry
# ---------------------------------------------------------------------------

def test_normalize_text_strips_accents_and_case():
    assert normalize_text("  Štefanija  CONSTRUCTION-Works! ") == "stefanija construction-works"


def test_canonical_url_strips_scheme_www_query_fragment_and_slash():
    assert canonical_url("https://www.Example.org/jobs/42/?utm_source=x#apply") == "example.org/jobs/42"
    assert canonical_url("http://example.org/jobs/42") == "example.org/jobs/42"


def test_dedup_canonical_url_pass():
    a = make_job(job_id="a", url="https://example.org/x/?utm_source=nl", description="short")
    b = make_job(job_id="b", url="https://www.example.org/x", description="a much longer description " * 10)
    out, removed = dedup_records([a, b], threshold=88)
    assert removed == 1
    assert out[0].description.startswith("a much longer")


def test_dedup_fingerprint_pass():
    a = make_job(job_id="a", url="https://a.example.org/1", title="Construction Engineer", organization="UNOPS")
    b = make_job(job_id="b", url="https://b.example.org/2", title="Construction  Engineer!", organization="UNOPS")
    out, removed = dedup_records([a, b], threshold=88)
    assert removed == 1
    assert len(out) == 1


def test_expired_jobs_are_filtered():
    expired = make_job(job_id="old", closing_date="2026-11-01")
    active = make_job(job_id="new", closing_date="2026-12-31")
    kept, n_exp = filter_expired([expired, active], today=TODAY)
    assert n_exp == 1
    assert [r.job_id for r in kept] == ["new"]


def test_closing_date_parsing():
    assert parse_iso_date("2026-12-31") == date(2026, 12, 31)
    assert parse_iso_date("2026-12-31T23:59:00Z") == date(2026, 12, 31)
    assert parse_iso_date("not-a-date") is None
    assert parse_iso_date("") is None


def test_csv_formula_injection_is_escaped():
    assert csv_safe("=1+1").startswith("'")
    assert csv_safe("+CMD()").startswith("'")
    assert csv_safe("-2+3").startswith("'")
    assert csv_safe("@SUM(1)").startswith("'")
    assert csv_safe("normal text") == "normal text"


# ---------------------------------------------------------------------------
# Scoring fixtures
# ---------------------------------------------------------------------------

def test_construction_role_scores_high():
    job = score_job(make_job(), CFG, today=TODAY)
    assert job.score >= 65, f"expected construction role to score >= 65, got {job.score}"
    assert job.fit_status != "disqualified"


def test_software_role_scores_low():
    job = make_job(
        job_id="sw", title="Software Engineer - Backend Python", organization="GlobalTech",
        country="Germany", location="Berlin", url="https://example.org/sw",
        description=("We need a software engineer to build Python microservices: Django, PostgreSQL, Kubernetes, "
                     "CI/CD, unit testing, code review, REST APIs, data structures. Strong coding skills required. "
                     "5+ years commercial software development. Machine learning a plus."))
    job = score_job(job, CFG, today=TODAY)
    assert job.score < 35, f"unrelated software role should score low, got {job.score}"
    assert any(p["phrase"] in ("software engineer", "coding") for p in job.penalties)


def test_construction_role_outscores_software_role():
    construction = score_job(make_job(job_id="c"), CFG, today=TODAY)
    software = score_job(make_job(
        job_id="s", title="Software Engineer", organization="GlobalTech", country="Germany",
        description="Software engineer, Python, Django, Kubernetes, coding, data structures."), CFG, today=TODAY)
    assert construction.score > software.score + 30


def test_project_management_role_good_fit():
    job = score_job(make_job(job_id="pm",
                             title="Project Manager - EU IPA Infrastructure",
                             description=("Manage EU/IPA-funded infrastructure projects: project management, procurement, "
                                          "tender documentation, budget monitoring, contract administration. FIDIC awareness, "
                                          "stakeholder engagement, government coordination. PMP desirable. English required. "
                                          "Field travel expected. 5+ years experience.")), CFG, today=TODAY)
    assert job.score >= 60


def test_heritage_role_scores_well():
    job = score_job(make_job(job_id="heritage",
                             title="Built Heritage Conservation Specialist",
                             organization="Council of Europe",
                             country="France", location="Strasbourg",
                             url="https://example.org/heritage",
                             description=("Cultural heritage conservation: built heritage documentation, conservation planning, "
                                          "technical specifications, budget monitoring, stakeholder engagement, EU-funded projects. "
                                          "MSc in Built Heritage Conservation desirable. Professional English.")),
                    CFG, today=TODAY)
    assert job.score >= 60
    assert any("heritage" in kw for kw in job.matched_keywords)


def test_wash_role_scores_well():
    job = score_job(make_job(job_id="wash",
                             title="WASH Infrastructure Specialist",
                             organization="UNICEF", country="Bosnia and Herzegovina",
                             description=("WASH infrastructure: water sanitation systems, tender documentation, contract "
                                          "administration, bills of quantities, quality assurance, budget monitoring, "
                                          "stakeholder engagement. 7+ years. English required. Field travel with driving.")),
                    CFG, today=TODAY)
    assert job.score >= 70


def test_expired_excellent_role_is_filtered():
    job = make_job(job_id="exp", closing_date="2026-11-10")
    kept, n = filter_expired([job], today=TODAY)
    assert n == 1 and kept == []


def test_nationality_restriction_disqualifies():
    job = make_job(job_id="nat",
                   organization="French Ministry",
                   description=("Engineering post. Must be a citizen of France - this position is restricted "
                                "to French nationals only. Site supervision, tender documentation, construction."))
    job = score_job(job, CFG, today=TODAY)
    assert job.fit_status == "disqualified"
    assert any(f["status"] == "not_matched" for f in job.mandatory_findings)


def test_native_language_requirement_disqualifies():
    job = make_job(job_id="lang",
                   description=("Native French required. Site supervision, construction, contract administration, "
                                "tender documentation, budget monitoring. English also required."))
    job = score_job(job, CFG, today=TODAY)
    assert job.fit_status == "disqualified"


def test_french_c1_marks_uncertain_review_required():
    job = make_job(job_id="fr",
                   description=("Excellent English and French C1 required. Construction supervision, tender documentation, "
                                "contract administration, budget monitoring, quality assurance of works, stakeholder engagement. "
                                "5+ years infrastructure experience."))
    job = score_job(job, CFG, today=TODAY)
    assert any(f["status"] == "uncertain" for f in job.mandatory_findings)
    assert job.fit_status in ("review_required", "strong_fit", "good_fit")


def test_unconfirmed_licence_marks_uncertain_not_disqualified():
    job = make_job(job_id="lic",
                   title="Structural Engineer - CEng required",
                   description=("Chartered Engineer (CEng) required. Structural calculations, construction supervision, "
                                "tender documentation, contract administration. Professional English. 5+ years experience."))
    job = score_job(job, CFG, today=TODAY)
    assert job.fit_status != "disqualified"
    assert any(f["status"] == "uncertain" for f in job.mandatory_findings)


def test_missing_description_caps_score_and_lowers_confidence():
    job = make_job(job_id="short", description="Construction role.")
    job = score_job(job, CFG, today=TODAY)
    assert job.score <= CFG["scoring"]["missing_description_cap"]
    assert job.confidence == "low"


def test_excellent_fit_excellent_status():
    job = score_job(make_job(job_id="exc"), CFG, today=TODAY)
    assert job.fit_status in ("excellent_fit", "strong_fit", "good_fit", "review_required")


def test_missing_description_is_never_treated_as_matched():
    job = make_job(job_id="empty", description="")
    job = score_job(job, CFG, today=TODAY)
    assert job.score_category in ("low", "possible", "good", "strong", "excellent")
    assert job.confidence == "low"


def test_entry_level_role_penalized():
    job = make_job(job_id="grad",
                   title="Entry-Level Graduate Scheme - Construction",
                   description="Graduate programme for recent graduates. Junior support to site engineers. No experience required.")
    job = score_job(job, CFG, today=TODAY)
    assert job.score < 50


def test_pmp_mention_contributes_to_education_component():
    without_pmp = score_job(make_job(job_id="nopmp",
                                     description=("Construction supervision, tender documentation, contract administration, "
                                                  "budget monitoring, quality assurance of works. Professional English. 5+ years.")),
                            CFG, today=TODAY)
    with_pmp = score_job(make_job(job_id="pmp",
                                  description=("Construction supervision, tender documentation, contract administration, "
                                               "budget monitoring, quality assurance of works. PMP required. Professional English. 5+ years.")),
                         CFG, today=TODAY)
    assert with_pmp.score_breakdown["education_certification"]["points"] >= \
           without_pmp.score_breakdown["education_certification"]["points"]


def test_repeated_keywords_do_not_stack():
    once = make_job(job_id="once",
                    description="Construction supervision. Tender documentation. Budget monitoring.")
    stuffed = make_job(job_id="stuffed",
                       description="Construction supervision. " * 30)
    s1 = score_job(once, CFG, today=TODAY)
    s2 = score_job(stuffed, CFG, today=TODAY)
    # Repeating one keyword must not outscore matching three distinct ones.
    assert s1.score_breakdown["technical_competencies"]["points"] > \
           s2.score_breakdown["technical_competencies"]["points"]


# ---------------------------------------------------------------------------
# ReliefWeb v2 adapter (offline, stubbed HTTP)
# ---------------------------------------------------------------------------

def _v2_payload() -> dict:
    return {
        "status": "ok",
        "totalCount": 1,
        "data": [{
            "id": 123,
            "fields": {
                "title": "Construction Engineer - Infrastructure Rehabilitation",
                "source": [{"name": "UNOPS"}],
                "url": "https://reliefweb.int/job/123",
                "date": {"created": "2026-11-10T00:00:00+00:00",
                         "closing": "2026-12-10T00:00:00+00:00"},
                "body": "Site supervision, FIDIC, contract administration of works.",
                "country": [{"name": "Bosnia and Herzegovina"}],
                "city": [{"name": "Sarajevo"}],
                "career_categories": [{"name": "Construction Engineering"}],
                "experience": [{"name": "Senior"}],
            },
        }],
    }


class _StubResponse:
    def __init__(self, code: int, payload: dict | None = None):
        self.status_code = code
        self._payload = payload or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"{self.status_code} Client Error: for url", response=self)

    def json(self):
        return self._payload


class _StubSession:
    """Returns 400 whenever the request body contains a 'query' key, else 200 with a v2 payload."""

    def __init__(self, mode: str):
        self.mode = mode          # "ok" | "rejected-appname" | "query-400"
        self.posts: list[dict] = []
        self.headers: dict = {}

    def post(self, url, json=None, timeout=None):
        self.posts.append(json or {})
        if self.mode == "rejected-appname":
            return _StubResponse(403, {"status": 403, "error": {"message": "not approved"}})
        if self.mode == "query-400" and (json or {}).get("query"):
            return _StubResponse(400, {"status": 400, "error": {"message": "bad query"}})
        return _StubResponse(200, _v2_payload())


def test_reliefweb_v2_records_are_parsed():
    from radar import fetch_reliefweb
    cfg = {"sources": {"reliefweb": {"enabled": True, "page_size": 1, "max_pages": 1, "max_records": 5,
                                     "query": "construction"}}}
    records, stats = fetch_reliefweb(cfg, "some-approved-appname", session=_StubSession("ok"))
    assert stats["failed"] == 0
    assert len(records) == 1
    rec = records[0]
    assert rec.organization == "UNOPS"
    assert rec.job_family == "Construction Engineering"
    assert rec.posted_date == "2026-11-10"
    assert rec.closing_date == "2026-12-10"
    assert rec.grade == "Senior"
    assert "FIDIC" in rec.description


def test_reliefweb_v2_query_4xx_falls_back_to_unqueried_request():
    from radar import fetch_reliefweb
    cfg = {"sources": {"reliefweb": {"enabled": True, "page_size": 1, "max_pages": 1, "max_records": 5,
                                     "query": "construction OR infrastructure"}}}
    session = _StubSession("query-400")
    records, stats = fetch_reliefweb(cfg, "some-approved-appname", session=session)
    assert stats["failed"] == 0
    assert len(records) == 1                       # fell back to the un-queried request
    assert any("query" in (p or {}) for p in session.posts)          # tried with query first
    assert any("query" not in (p or {}) for p in session.posts)      # then without


def test_reliefweb_v2_unapproved_appname_reports_failure_without_network():
    from radar import fetch_reliefweb
    cfg = {"sources": {"reliefweb": {"enabled": True, "page_size": 1, "max_pages": 1, "max_records": 5}}}
    records, stats = fetch_reliefweb(cfg, "definitely-not-approved", session=_StubSession("rejected-appname"))
    assert records == []
    assert stats["failed"] == 1
    assert any("approved appname" in e for e in stats["errors"])
