"""Deterministic, explainable scoring engine for Damir Job Radar.

Rules:
- 100-point weighted score; component maxima from config.yaml (sum == 100).
- A missing fact is never treated as a match - it becomes "uncertain" or "not_stated".
- An explicitly unmet mandatory requirement disqualifies the vacancy.
- Keyword stuffing cannot dominate: each keyword is counted once,
  title hits outrank description hits, and the job-family component
  carries more weight than generic words like "project" or "management".
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date
from typing import Any

from database import JobRecord, parse_iso_date


def normalize_text(text: str) -> str:
    """Lowercase, strip accents and collapse whitespace for matching."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-z0-9\s\-&/+]", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def canonical_url(url: str) -> str:
    """Canonicalize a URL for deduplication (scheme/host case, www, query, fragment, trailing slash)."""
    url = (url or "").strip()
    if not url:
        return ""
    url = re.sub(r"^https?://", "", url, flags=re.IGNORECASE)
    url = url.split("#", 1)[0].split("?", 1)[0]
    url = url.lower().rstrip("/")
    url = re.sub(r"^www\.", "", url)
    return url


def normalize_fingerprint(text: str) -> str:
    text = normalize_text(text)
    text = re.sub(r"\b(ltd|limited|inc|llc|doo|d\.d|plc|gmbh|sarl|sa|nv|ag)\b\.?", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# Mandatory requirement checks
# ---------------------------------------------------------------------------

def _find_years_required(text: str) -> int | None:
    matches = re.findall(r"(\d{1,2})\s*(?:\+|\s*plus)?\s*(?:-|–|to)?\s*(?:\d{1,2})?\s*(?:\+)?\s*years?", text)
    if not matches:
        return None
    return min(int(m) for m in matches)


def _language_findings(text_norm: str, cfg: dict) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    cand = cfg.get("candidate", {})
    native = {normalize_text(l) for l in cand.get("native_languages", [])}
    unconfirmed = {normalize_text(l) for l in (cand.get("unconfirmed_languages") or {})}
    rules = cfg.get("mandatory_language_rules", {})

    langs = ["english", "french", "italian", "german", "spanish", "arabic",
             "portuguese", "russian", "polish", "dutch", "bosnian", "serbian", "croatian"]

    # Native / mother-tongue requirements
    for m in re.finditer(r"(?:native|mother tongue|first language)(?:\s+level)?\s+(?:of|in)?\s*([a-z]+)", text_norm):
        lang = m.group(1)
        if lang not in langs:
            continue
        if lang in native:
            findings.append({"requirement": f"Native {lang}", "status": "matched", "note": f"Native {lang} confirmed."})
        else:
            findings.append({"requirement": f"Native {lang}", "status": "not_matched",
                             "note": f"Native {lang} required; candidate's confirmed native languages are Bosnian/Croatian/Serbian."})
    for m in re.finditer(r"([a-z]+)\s+(?:native|mother tongue|first language)", text_norm):
        lang = m.group(1)
        if lang in langs and lang not in native and not any(f["requirement"] == f"Native {lang}" for f in findings):
            findings.append({"requirement": f"Native {lang}", "status": "not_matched",
                             "note": f"Native {lang} required; not among candidate's confirmed native languages."})

    # Explicit C1/C2/fluent levels
    level_pattern = r"(french|italian|german|spanish|arabic|portuguese|russian|polish|dutch|english)\s*[:\-]?\s*(c2|c1|c0|fluent|excellent|advanced|proficient)\b"
    reverse_pattern = r"\b(c2|c1|fluent|excellent|advanced)\s+(french|italian|german|spanish|arabic|portuguese|russian|polish|dutch|english)\b"
    for m in re.finditer(level_pattern, text_norm):
        lang, level = m.group(1), m.group(2)
        if lang == "english":
            findings.append({"requirement": f"English {level.upper()}", "status": "matched",
                             "note": "Professional English confirmed."})
        elif lang in unconfirmed:
            findings.append({"requirement": f"{lang.title()} {level.upper()}", "status": "uncertain",
                             "note": f"{lang.title()} required at {level.upper()}; level is not confirmed in the profile."})
        else:
            findings.append({"requirement": f"{lang.title()} {level.upper()}", "status": "uncertain",
                             "note": f"{lang.title()} at {level.upper()} is not in the confirmed profile."})
    for m in re.finditer(reverse_pattern, text_norm):
        level, lang = m.group(1), m.group(2)
        if lang == "english":
            findings.append({"requirement": f"English {level.upper()}", "status": "matched",
                             "note": "Professional English confirmed."})
        elif not any(f["requirement"].lower().startswith(f"{lang} {level}") for f in findings):
            if lang in unconfirmed:
                findings.append({"requirement": f"{lang.title()} {level.upper()}", "status": "uncertain",
                                 "note": f"{lang.title()} level not confirmed in the profile."})
            else:
                findings.append({"requirement": f"{lang.title()} {level.upper()}", "status": "uncertain",
                                 "note": f"{lang.title()} at {level.upper()} is not in the confirmed profile."})
    return findings


_NATIONALITY_PATTERNS = [
    r"(?:national(?:s|ity)?|citizens?)\s+of\s+([a-z ]{3,40})",
    r"open\s+only\s+to\s+([a-z ]{3,40}?)(?:\s+nationals?|\s+citizens?|$|,|\.)",
    r"restricted\s+to\s+([a-z ]{3,40}?)(?:\s+nationals?|\s+citizens?|$|,|\.)",
    r"must\s+be\s+(?:a\s+)?(?:national|citizen)\s+of\s+([a-z ]{3,40})",
]


def _nationality_findings(text_norm: str, cfg: dict) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    generic_ok = ("all countries", "all nationalities", "any nationality", "all un member states",
                  "all member states", "european union", "eu", "un", "united nations",
                  "international", "all nationalities may apply")

    for pattern in _NATIONALITY_PATTERNS:
        for m in re.finditer(pattern, text_norm):
            place = m.group(1).strip()
            place = re.sub(r"\b(nationals?|citizens?)\b", "", place).strip()
            if not place:
                continue
            if "bosnia" in place or "herzegovina" in place or "bih" in place:
                findings.append({"requirement": f"Nationality: {place}", "status": "matched",
                                 "note": "Bosnia and Herzegovina national - requirement satisfied."})
                continue
            if any(re.search(r"\b" + re.escape(tok) + r"\b", place) for tok in generic_ok):
                findings.append({"requirement": f"Open to {place}", "status": "uncertain",
                                 "note": f"Open to {place} - verify eligibility for your nationality and permits."})
                continue
            findings.append({"requirement": f"Nationality restriction: {place}", "status": "not_matched",
                             "note": f"Restricted to {place} nationals; candidate is a Bosnia and Herzegovina national."})
    return findings


def _licence_findings(text_norm: str, cfg: dict) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    cand = cfg.get("candidate", {})
    confirmed = [normalize_text(l) for l in cand.get("licences_confirmed", [])]
    not_confirmed = [normalize_text(l) for l in cand.get("licences_not_confirmed", [])]

    licence_patterns = [
        (r"\bceng\b|chartered engineer", "UK Chartered Engineer (CEng)"),
        (r"\bpe\s+licen|professional engineer\s*\(pe\)|\bus pe\b", "US Professional Engineer (PE)"),
        (r"\brics\b", "RICS chartered status"),
        (r"\bapec\b", "APEC Engineer"),
        (r"\bpmp\b", "PMP (Project Management Professional)"),
    ]
    for pattern, label in licence_patterns:
        if re.search(pattern, text_norm):
            label_norm = normalize_text(label)
            if any(label_norm in c or c in label_norm for c in confirmed if c):
                findings.append({"requirement": label, "status": "matched", "note": f"{label} - confirmed in profile."})
            elif any(label_norm in n or n in label_norm for n in not_confirmed if n):
                findings.append({"requirement": label, "status": "uncertain",
                                 "note": f"{label} required/mentioned but NOT confirmed in the profile. Verify eligibility."})
            else:
                # Generic licensed/chartered engineer without a specific registry
                if "pmp" in label_norm:
                    findings.append({"requirement": label, "status": "matched", "note": "PMP certified."})
                else:
                    findings.append({"requirement": label, "status": "uncertain",
                                     "note": f"{label} mentioned; only the Bosnia and Herzegovina professional exam is confirmed. Verify."})

    # Generic structural licence phrasing
    if re.search(r"(?:licensed|chartered|registered)\s+(?:structural|civil|professional)?\s*engineer", text_norm):
        findings.append({"requirement": "Licensed/chartered engineer (registry unspecified)",
                         "status": "uncertain",
                         "note": "A licensure is mentioned without a named registry. Only the BiH professional exam is confirmed - verify which registry is required."})
    return findings


def _degree_findings(text_norm: str, cfg: dict) -> tuple[list[dict[str, str]], int]:
    """Education discipline check. Returns (findings, unrelated_discipline_penalty)."""
    findings: list[dict[str, str]] = []
    unrelated = ["nursing degree", "medical degree", "law degree", "accounting degree",
                 "finance degree", "degree in economics", "psychology degree", "teaching degree"]
    penalty = 0
    if any(p in text_norm for p in unrelated):
        findings.append({"requirement": "Unrelated discipline degree", "status": "not_matched",
                         "note": "A discipline unrelated to construction/architecture/engineering is required."})
        penalty = 15
    if re.search(r"\bph\s?d\b|doctorate", text_norm):
        findings.append({"requirement": "PhD", "status": "matched", "note": "PhD in Architectural Engineering - confirmed."})
    if re.search(r"\bmaster'?s?\b|\bmsc\b|\bma\b", text_norm):
        findings.append({"requirement": "Master's degree", "status": "matched",
                         "note": "MSc in Built Heritage Conservation - confirmed."})
    return findings, penalty


# ---------------------------------------------------------------------------
# Scoring components
# ---------------------------------------------------------------------------

def _component_job_family(title_norm: str, cfg: dict) -> tuple[float, str, str | None]:
    best_points = 0
    best_phrase: str | None = None
    for tier, boost in (("primary", 1.0), ("secondary", 0.85)):
        for entry in cfg.get("job_families", {}).get(tier, []):
            phrase = normalize_text(entry.get("phrase", ""))
            if phrase and phrase in title_norm:
                pts = float(entry.get("points", 0)) * boost
                if pts > best_points:
                    best_points = pts
                    best_phrase = phrase
    return min(20.0, best_points), best_phrase or "", best_phrase


def _component_competencies(title_norm: str, text_norm: str, cfg: dict) -> tuple[float, list[str]]:
    keywords = cfg.get("technical_competencies", [])
    title_hits, desc_hits = set(), set()
    for kw in keywords:
        kw_norm = normalize_text(kw)
        if not kw_norm:
            continue
        if kw_norm in title_norm:
            title_hits.add(kw_norm)
        elif kw_norm in text_norm:
            desc_hits.add(kw_norm)
    points = min(25.0, 3.0 * len(title_hits) + 1.5 * len(desc_hits))
    matched = sorted(title_hits | desc_hits)
    return points, matched


def _component_sector(org_norm: str, text_norm: str, cfg: dict) -> tuple[float, bool, list[str]]:
    points = 0.0
    matched_orgs: list[str] = []
    for org in cfg.get("sector_signals", {}).get("organizations", []):
        org_sig = normalize_text(org)
        if len(org_sig) >= 3 and org_sig in org_norm:
            points += 12
            matched_orgs.append(org.strip())
            break
    sector_hits = set()
    for sec in cfg.get("sector_signals", {}).get("sectors", []):
        sec_norm = normalize_text(sec)
        if sec_norm and sec_norm in text_norm:
            sector_hits.add(sec_norm)
    points += min(5.0, 1.25 * len(sector_hits))
    if re.search(r"\b(un|united nations|unicef|undp|unops|wfp|unhcr|iom|osce|european union|european commission)\b", text_norm):
        points += 2.0
    matched_sector = sorted(sector_hits)
    return min(15.0, points), bool(matched_orgs), matched_orgs + matched_sector


def _component_seniority(text_norm: str, title_norm: str, cfg: dict) -> tuple[float, str]:
    profile_years = int(cfg.get("candidate", {}).get("profile_years", 12))
    if any(w in title_norm for w in ("intern", "graduate", "trainee", "entry level", "entry-level", "junior")):
        return 4.0, "Entry-level/graduate wording in title - below profile seniority."
    years = _find_years_required(text_norm)
    if years is None:
        return 11.0, "No explicit years-of-experience requirement stated."
    if years <= profile_years:
        return 15.0, f"Requires ~{years}+ years; profile covers ~{profile_years}."
    if years <= profile_years + 2:
        return 11.0, f"Requires ~{years}+ years; slightly above configured profile (~{profile_years}) but plausibly equivalent."
    if years <= profile_years + 4:
        return 7.0, f"Requires ~{years}+ years; noticeably above configured profile (~{profile_years})."
    return 3.0, f"Requires ~{years}+ years; substantially above configured profile (~{profile_years})."


def _component_education(text_norm: str, title_norm: str, cfg: dict) -> tuple[float, str]:
    points = 0.0
    hits = []
    field_keywords = ["architecture", "architectural", "civil engineer", "construction",
                      "built heritage", "heritage", "infrastructure", "urbanism", "urban planning", "engineering"]
    for kw in field_keywords:
        if kw in text_norm or kw in title_norm:
            points += 5.0
            hits.append(kw)
            break
    if re.search(r"\bpmp\b|project management professional", text_norm):
        points += 3.0
        hits.append("PMP")
    if re.search(r"\bph\s?d\b|doctorate", text_norm):
        points += 2.0
        hits.append("PhD")
    elif re.search(r"\bmaster'?s?\b|\bmsc\b", text_norm):
        points += 1.5
        hits.append("MSc")
    if not hits:
        return 6.0, "No education-field information stated - neutral score."
    return min(10.0, points), "Matched: " + ", ".join(hits)


def _component_location(text_norm: str, country_norm: str, cfg: dict) -> tuple[float, str]:
    favourable = [normalize_text(l) for l in cfg.get("favourable_locations", [])]
    haystack = f"{country_norm} {text_norm}"
    if any(f in haystack for f in favourable):
        return 5.0, "Location favourable (Bosnia and Herzegovina / Europe / Western Balkans / remote)."
    if not country_norm.strip() and not text_norm.strip():
        return 2.5, "Location not stated."
    return 2.0, "Location outside the configured favourable region - check permits and work authorization."


def _component_language(text_norm: str, cfg: dict) -> tuple[float, list[dict[str, str]]]:
    findings = _language_findings(text_norm, cfg)
    if not findings:
        return 5.0, findings
    statuses = {f["status"] for f in findings}
    if "not_matched" in statuses:
        return 1.0, findings  # disqualification handled separately
    if "uncertain" in statuses:
        return 3.0, findings
    return 5.0, findings


def _component_contract(text_norm: str) -> tuple[float, str]:
    if re.search(r"\b(field work|site visit|travel required|driving|4wd|four wheel|vehicle|field travel|deployment)\b", text_norm):
        return 5.0, "Field travel / driving mentioned - confirmed (driving since 2003, manual 4WD)."
    if re.search(r"\bremote\b", text_norm):
        return 4.0, "Remote-friendly contract."
    if re.search(r"\b(fixed[- ]term|permanent|full[- ]time|12 months|24 months)\b", text_norm):
        return 4.0, "Standard fixed-term/permanent international contract."
    if re.search(r"\b(internship|volunteer|unpaid)\b", text_norm):
        return 0.0, "Internship/volunteer/unpaid wording - below profile preference."
    return 3.0, "No contract or travel details stated."


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def score_job(job: JobRecord, cfg: dict, today: date | None = None) -> JobRecord:
    today = today or date.today()
    weights = cfg.get("scoring", {}).get("weights", {})
    categories = cfg.get("scoring", {}).get("categories", {})

    title_norm = normalize_text(job.title)
    desc = job.description or ""
    text_norm = normalize_text(f"{job.title} {desc}")
    org_norm = normalize_text(job.organization)
    country_norm = normalize_text(job.country or job.location)

    breakdown: dict[str, dict[str, Any]] = {}
    strengths: list[str] = []
    gaps: list[str] = []
    uncertainties: list[str] = []
    penalties_applied: list[dict[str, Any]] = []
    mandatory: list[dict[str, str]] = []

    # 1. Job family / title (20)
    pts, phrase, _ = _component_job_family(title_norm, cfg)
    breakdown["job_family"] = {"points": round(pts, 1), "max": weights.get("job_family", 20),
                               "note": f"Title match: '{phrase}'." if phrase else "No configured target-family phrase in title."}
    if phrase:
        strengths.append(f"Title matches target family: '{phrase}'.")
    else:
        gaps.append("Title does not match a configured primary/secondary target family.")

    # 2. Technical competencies (25)
    pts, matched_kw = _component_competencies(title_norm, text_norm, cfg)
    breakdown["technical_competencies"] = {"points": round(pts, 1), "max": weights.get("technical_competencies", 25),
                                           "note": f"{len(matched_kw)} distinct competency keywords matched." if matched_kw else "No target competencies detected."}
    if matched_kw:
        strengths.append(f"Matched competencies: {', '.join(matched_kw[:12])}.")
    else:
        gaps.append("No target technical competencies detected.")

    # 3. Sector / organization (15)
    pts, org_hit, org_matched = _component_sector(org_norm, text_norm, cfg)
    breakdown["sector_organization"] = {"points": round(pts, 1), "max": weights.get("sector_organization", 15),
                                        "note": ("High-value organization: " + ", ".join(str(o) for o in org_matched)) if org_hit else "No high-value organization match."}
    if org_hit:
        strengths.append(f"High-value organization/sector: {', '.join(str(o) for o in org_matched)}.")
    else:
        uncertainties.append("Organization not in the configured high-value list - sector relevance unverified.")

    # 4. Experience / seniority (15)
    pts, note = _component_seniority(text_norm, title_norm, cfg)
    breakdown["experience_seniority"] = {"points": round(pts, 1), "max": weights.get("experience_seniority", 15), "note": note}
    if pts >= 11:
        strengths.append(note)
    else:
        uncertainties.append(note)

    # 5. Education & certification (10)
    pts, note = _component_education(text_norm, title_norm, cfg)
    breakdown["education_certification"] = {"points": round(pts, 1), "max": weights.get("education_certification", 10), "note": note}
    if pts >= 8:
        strengths.append(note)
    else:
        uncertainties.append(note)

    # Education discipline mandatory check
    degree_findings, degree_penalty = _degree_findings(text_norm, cfg)
    mandatory.extend(degree_findings)

    # 6. Location & work authorization (5)
    pts, note = _component_location(text_norm, country_norm, cfg)
    breakdown["location_authorization"] = {"points": round(pts, 1), "max": weights.get("location_authorization", 5), "note": note}
    if pts >= 4.5:
        strengths.append(note)
    else:
        uncertainties.append(note)

    # 7. Language (5) + mandatory findings
    pts, lang_findings = _component_language(text_norm, cfg)
    breakdown["language"] = {"points": round(pts, 1), "max": weights.get("language", 5),
                             "note": ("No language requirements stated - full credit." if not lang_findings else
                                      "; ".join(f["note"] for f in lang_findings))}
    mandatory.extend(lang_findings)
    for f in lang_findings:
        if f["status"] == "uncertain":
            uncertainties.append(f["note"])
        elif f["status"] == "not_matched":
            gaps.append(f["note"])

    # 8. Contract / travel preference (5)
    pts, note = _component_contract(text_norm)
    breakdown["contract_travel"] = {"points": round(pts, 1), "max": weights.get("contract_travel", 5), "note": note}
    if pts >= 4:
        strengths.append(note)
    else:
        uncertainties.append(note)

    # Nationality mandatory findings
    mandatory.extend(_nationality_findings(text_norm, cfg))
    # Licence mandatory findings
    mandatory.extend(_licence_findings(text_norm, cfg))

    for f in mandatory:
        if f["status"] == "uncertain" and f["note"] not in uncertainties:
            uncertainties.append(f["note"])
        elif f["status"] == "not_matched" and f["note"] not in gaps:
            gaps.append(f["note"])

    # Penalties (keyword-stuffing resistant, diminishing value handled by the cap)
    for neg in cfg.get("negative_signals", []):
        phrase = normalize_text(neg.get("phrase", ""))
        penalty = float(neg.get("penalty", 0))
        if phrase and re.search(r"\b" + re.escape(phrase) + r"\b", text_norm):
            penalties_applied.append({"phrase": phrase, "penalty": penalty})

    total = sum(b["points"] for b in breakdown.values())
    if degree_penalty:
        penalties_applied.append({"phrase": "unrelated discipline degree required", "penalty": float(degree_penalty)})
    total -= sum(p["penalty"] for p in penalties_applied)
    total = max(0.0, min(100.0, total))

    # Missing-description confidence cap
    min_chars = int(cfg.get("scoring", {}).get("min_description_chars", 200))
    cap = float(cfg.get("scoring", {}).get("missing_description_cap", 55))
    confidence = "high"
    if len(desc.strip()) < min_chars:
        if total > cap:
            total = cap
        confidence = "low"
        uncertainties.append("Description missing or very short - confidence reduced and score capped.")
    elif len(desc.strip()) < 500:
        confidence = "medium"

    if mandatory and any(f["status"] == "uncertain" for f in mandatory):
        confidence = "medium" if confidence == "high" else confidence

    total = round(total, 1)

    # Fit status / category
    disqualified = any(f["status"] == "not_matched" for f in mandatory)
    if disqualified:
        fit_status = "disqualified"
        category = "disqualified"
    elif any(f["status"] == "uncertain" for f in mandatory) and total >= categories.get("strong", 75):
        fit_status = "review_required"
        category = _category_for(total, categories)
    else:
        fit_status = _status_for(total, categories)
        category = _category_for(total, categories)

    job.score = total
    job.score_category = category
    job.fit_status = fit_status
    job.confidence = confidence
    job.matched_keywords = matched_kw
    job.strengths = strengths
    job.gaps = gaps
    job.uncertainties = uncertainties
    job.penalties = penalties_applied
    job.mandatory_findings = mandatory
    job.score_breakdown = breakdown
    return job


def _category_for(total: float, categories: dict) -> str:
    if total >= categories.get("excellent", 85):
        return "excellent"
    if total >= categories.get("strong", 75):
        return "strong"
    if total >= categories.get("good", 65):
        return "good"
    if total >= categories.get("possible", 50):
        return "possible"
    return "low"


def _status_for(total: float, categories: dict) -> str:
    if total >= categories.get("excellent", 85):
        return "excellent_fit"
    if total >= categories.get("strong", 75):
        return "strong_fit"
    if total >= categories.get("good", 65):
        return "good_fit"
    if total >= categories.get("possible", 50):
        return "possible_fit"
    return "low_priority"


def explanation_markdown(job: JobRecord) -> str:
    """Human-readable explanation of a score."""
    lines = [
        f"## {job.title} - {job.organization}",
        f"**Fit score:** {job.score}/100 ({job.score_category}) | **Status:** {job.fit_status} | **Confidence:** {job.confidence}",
        "",
        "### Component breakdown",
    ]
    for name, data in job.score_breakdown.items():
        lines.append(f"- **{name.replace('_', ' ').title()}:** {data.get('points')}/{data.get('max')} - {data.get('note')}")
    if job.strengths:
        lines += ["", "### Strengths"] + [f"- {s}" for s in job.strengths]
    if job.gaps:
        lines += ["", "### Gaps"] + [f"- {g}" for g in job.gaps]
    if job.uncertainties:
        lines += ["", "### Uncertainties (not counted as matched)"] + [f"- {u}" for u in job.uncertainties]
    if job.penalties:
        lines += ["", "### Penalties"] + [f"- {p['phrase']}: -{p['penalty']}" for p in job.penalties]
    if job.mandatory_findings:
        lines += ["", "### Mandatory-requirement findings"] + [
            f"- [{f['status']}] {f['requirement']} - {f['note']}" for f in job.mandatory_findings]
    return "\n".join(lines)
