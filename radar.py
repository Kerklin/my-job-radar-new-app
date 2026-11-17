"""Damir Job Radar - single engine module.

Storage + scoring + sources + collection CLI in one file, deliberately.

Sections:
1. Storage   - JobRecord, CSV feed (formula-injection safe), statuses (SQLite).
2. Scoring   - deterministic 100-point engine, explainable, mandatory checks.
3. Sources   - ReliefWeb (v2), Greenhouse, Lever, Ashby, manual import. All config-driven.
4. CLI       - `python radar.py [--sample]` (used by the daily GitHub Action).
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import io
import json
import logging
import os
import re
import sqlite3
import sys
import unicodedata
from dataclasses import asdict, dataclass, field, fields
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
import yaml
from rapidfuzz import fuzz
from requests.adapters import HTTPAdapter

try:
    from urllib3.util.retry import Retry
except Exception:  # pragma: no cover - extremely defensive fallback
    Retry = None  # type: ignore[assignment]

REPO_ROOT = Path(__file__).resolve().parent
DATA_DIR = REPO_ROOT / "data"
FEED_PATH = DATA_DIR / "latest_jobs.csv"
META_PATH = DATA_DIR / "collection_status.json"
SQLITE_PATH = DATA_DIR / "jobs.db"

FEED_COLUMNS = [
    "job_id", "title", "organization", "location", "country", "remote_type",
    "contract_type", "grade", "posted_date", "closing_date", "days_remaining",
    "description", "url", "source", "source_url", "sector", "job_family",
    "score", "score_category", "fit_status", "confidence",
    "matched_keywords", "strengths", "gaps", "uncertainties",
    "penalties", "mandatory_findings", "score_breakdown",
]


@dataclass
class JobRecord:
    """Normalized vacancy record used across collection, scoring and UI."""

    job_id: str = ""
    title: str = ""
    organization: str = ""
    location: str = ""
    country: str = ""
    remote_type: str = ""           # remote / hybrid / on-site / ""
    contract_type: str = ""         # permanent / fixed-term / consultancy / internship / ""
    grade: str = ""                 # grade or seniority label, if available
    posted_date: str = ""           # ISO YYYY-MM-DD or ""
    closing_date: str = ""          # ISO YYYY-MM-DD or ""
    days_remaining: int | None = None
    description: str = ""
    url: str = ""
    source: str = ""                # reliefweb / greenhouse / lever / ashby / manual / sample
    source_url: str = ""
    sector: str = ""
    job_family: str = ""
    score: float = 0.0
    score_category: str = "low"
    fit_status: str = "low_priority"  # excellent_fit / strong_fit / good_fit / possible_fit / review_required / disqualified / low_priority
    confidence: str = "medium"        # high / medium / low
    matched_keywords: list = field(default_factory=list)
    strengths: list = field(default_factory=list)
    gaps: list = field(default_factory=list)
    uncertainties: list = field(default_factory=list)
    penalties: list = field(default_factory=list)
    mandatory_findings: list = field(default_factory=list)
    score_breakdown: dict = field(default_factory=dict)

    def to_row(self) -> dict[str, Any]:
        row = asdict(self)
        for key in ("matched_keywords", "strengths", "gaps", "uncertainties", "penalties", "mandatory_findings"):
            row[key] = json.dumps(row[key], ensure_ascii=False)
        row["score_breakdown"] = json.dumps(row["score_breakdown"], ensure_ascii=False)
        return row

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "JobRecord":
        kwargs: dict[str, Any] = {}
        valid = {f.name for f in fields(cls)}
        for key, value in row.items():
            if key not in valid:
                continue
            if key in ("matched_keywords", "strengths", "gaps", "uncertainties", "penalties", "mandatory_findings", "score_breakdown"):
                try:
                    value = json.loads(value) if isinstance(value, str) and value.strip() else ([] if key != "score_breakdown" else {})
                except json.JSONDecodeError:
                    value = [] if key != "score_breakdown" else {}
            elif key == "days_remaining":
                try:
                    value = int(value) if str(value).strip() not in ("", "None") else None
                except ValueError:
                    value = None
            elif key == "score":
                try:
                    value = float(value)
                except (TypeError, ValueError):
                    value = 0.0
            kwargs[key] = value
        return cls(**kwargs)


def ensure_data_dir() -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR


def strip_html(raw_html: str) -> str:
    """Convert an HTML description to safe plain text."""
    if not raw_html:
        return ""
    try:
        from bs4 import BeautifulSoup
        text = BeautifulSoup(raw_html, "html.parser").get_text(separator=" ")
    except Exception:
        text = html.unescape(raw_html)
    return " ".join(text.split())


def csv_safe(value: Any) -> str:
    """Prevent CSV formula injection on export (=, +, -, @, tab, CR prefixes)."""
    text = "" if value is None else str(value)
    if text and text[0] in "=+-@\t\r":
        return "'" + text
    return text


def dataframe_to_csv_bytes(df) -> bytes:
    """Serialize a DataFrame to UTF-8 BOM CSV with formula-injection protection."""
    safe = df.copy()
    for col in safe.columns:
        if safe[col].dtype == object:
            safe[col] = safe[col].map(csv_safe)
    buf = io.StringIO()
    safe.to_csv(buf, index=False)
    return buf.getvalue().encode("utf-8-sig")


def records_to_csv(records: list[JobRecord], path: Path | None = None) -> Path:
    path = path or FEED_PATH
    ensure_data_dir()
    rows = [r.to_row() for r in records]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FEED_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            # Formula-injection guard for spreadsheet consumers.
            for col in list(row):
                if isinstance(row[col], str):
                    row[col] = csv_safe(row[col])
            writer.writerow(row)
    return path


def load_feed(path: Path | None = None) -> list[JobRecord]:
    path = path or FEED_PATH
    if not path.exists():
        return []
    records: list[JobRecord] = []
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            records.append(JobRecord.from_row(row))
    return records


def write_collection_status(status: dict[str, Any]) -> None:
    try:
        ensure_data_dir()
        status = dict(status)
        status.setdefault("collected_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
        META_PATH.write_text(json.dumps(status, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError:
        # Non-fatal: feed remains the source of truth.
        pass


def read_collection_status() -> dict[str, Any]:
    try:
        if META_PATH.exists():
            return json.loads(META_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    return {}


# ---------------------------------------------------------------------------
# Personal statuses (saved / applied / rejected / notes)
# ---------------------------------------------------------------------------

def init_status_db(path: Path | None = None) -> bool:
    """Create the status table if possible. Returns True on success."""
    path = path or SQLITE_PATH
    try:
        ensure_data_dir()
        with sqlite3.connect(path) as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS job_status(
                       job_id TEXT PRIMARY KEY,
                       saved INTEGER DEFAULT 0,
                       applied INTEGER DEFAULT 0,
                       rejected INTEGER DEFAULT 0,
                       notes TEXT DEFAULT '',
                       updated_at TEXT DEFAULT '')"""
            )
        return True
    except OSError:
        return False


def save_status(job_id: str, saved: bool, applied: bool, rejected: bool, notes: str,
                path: Path | None = None) -> bool:
    path = path or SQLITE_PATH
    try:
        if not init_status_db(path):
            return False
        with sqlite3.connect(path) as conn:
            conn.execute(
                """INSERT INTO job_status(job_id, saved, applied, rejected, notes, updated_at)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(job_id) DO UPDATE SET
                     saved=excluded.saved, applied=excluded.applied,
                     rejected=excluded.rejected, notes=excluded.notes,
                     updated_at=excluded.updated_at""",
                (job_id, int(saved), int(applied), int(rejected), notes or "",
                 datetime.now(timezone.utc).isoformat(timespec="seconds")),
            )
        return True
    except (OSError, sqlite3.Error):
        return False


def load_statuses(path: Path | None = None) -> dict[str, dict[str, Any]]:
    path = path or SQLITE_PATH
    if not path.exists():
        return {}
    try:
        with sqlite3.connect(path) as conn:
            rows = conn.execute(
                "SELECT job_id, saved, applied, rejected, notes FROM job_status"
            ).fetchall()
        return {
            r[0]: {"saved": bool(r[1]), "applied": bool(r[2]), "rejected": bool(r[3]), "notes": r[4] or ""}
            for r in rows
        }
    except (OSError, sqlite3.Error):
        return {}


def statuses_to_csv_bytes(statuses: dict[str, dict[str, Any]], feed: list[JobRecord]) -> bytes:
    title_by_id = {j.job_id: j.title for j in feed}
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["job_id", "title", "saved", "applied", "rejected", "notes"])
    for job_id, st in statuses.items():
        writer.writerow([csv_safe(job_id), csv_safe(title_by_id.get(job_id, "")),
                         int(st.get("saved", False)), int(st.get("applied", False)),
                         int(st.get("rejected", False)), csv_safe(st.get("notes", ""))])
    return buf.getvalue().encode("utf-8-sig")


def parse_iso_date(value: str) -> date | None:
    if not value:
        return None
    value = value.strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%d/%m/%Y", "%d.%m.%Y"):
        try:
            normalized = value.replace("Z", "+00:00")
            if fmt == "%Y-%m-%dT%H:%M:%S" and "+" in normalized:
                return datetime.fromisoformat(normalized).date()
            return datetime.strptime(value[:len(fmt) + (2 if fmt.endswith("Z") else 0)], fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def days_until(closing: date | None, today: date | None = None) -> int | None:
    if closing is None:
        return None
    today = today or date.today()
    return (closing - today).days

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


# =========================================================================
# Sources
# =========================================================================

logger = logging.getLogger("damir-job-radar")

USER_AGENT = "DamirJobRadar/1.0 (personal job search; contact via repository owner)"
TIMEOUT = (10, 30)  # (connect, read) seconds


# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------

def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    })
    if Retry is not None:
        retry = Retry(
            total=3,
            connect=3,
            read=3,
            backoff_factor=1.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET", "POST"]),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
    return session


def http_get_json(session: requests.Session, url: str, *, params: dict | None = None) -> Any:
    resp = session.get(url, params=params, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def http_post_json(session: requests.Session, url: str, *, json_body: dict) -> Any:
    resp = session.post(url, json=json_body, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def make_job_id(source: str, stable_key: str) -> str:
    digest = hashlib.sha256(f"{source}|{stable_key}".encode("utf-8")).hexdigest()[:16]
    return f"{source}-{digest}"


def empty_stats() -> dict[str, Any]:
    return {"collected": 0, "skipped": 0, "failed": 0, "errors": []}


def _finalize(record: JobRecord) -> JobRecord | None:
    """Reject records with no title, organization or application URL."""
    if not record.title.strip() or not record.organization.strip() or not record.url.strip():
        return None
    record.days_remaining = days_until(parse_iso_date(record.closing_date))
    return record


# ---------------------------------------------------------------------------
# ReliefWeb
# ---------------------------------------------------------------------------

def _first_names(value) -> list[str]:
    """Tolerate ReliefWeb shortcut fields that may be a string, a dict, or a list of either."""
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    names: list[str] = []
    for entry in items:
        if isinstance(entry, dict):
            name = str(entry.get("name", "")).strip()
        else:
            name = str(entry).strip()
        if name:
            names.append(name)
    return names


def fetch_reliefweb(cfg: dict, appname: str | None, session: requests.Session | None = None) -> tuple[list[JobRecord], dict]:
    """Collect active jobs from the ReliefWeb API v2 (v1 was decommissioned).

    Requests `profile=list` (title, url, date.created, date.closing, status,
    country.name, source.name) plus `fields.include` for body and a few extra
    names. The default `minimal` preset already hides expired jobs.
    """
    stats = empty_stats()
    records: list[JobRecord] = []
    if not appname:
        stats["errors"].append("RELIEFWEB_APPNAME is not set; ReliefWeb was skipped.")
        logger.warning("ReliefWeb skipped: RELIEFWEB_APPNAME is not set.")
        return records, stats

    session = session or build_session()
    rw_cfg = cfg.get("sources", {}).get("reliefweb", {})
    limit = int(rw_cfg.get("page_size", 20))
    max_pages = int(rw_cfg.get("max_pages", 3))
    max_records = int(rw_cfg.get("max_records", 60))
    query_text = str(rw_cfg.get("query", "") or "").strip()

    def build_body(page: int, with_query: bool) -> dict:
        body = {
            "limit": limit,
            "offset": page * limit,
            "profile": "list",
            "fields": {"include": ["body", "city.name", "career_categories.name",
                                   "experience.name", "language.name"]},
            "sort": ["date.created:desc"],
        }
        if with_query and query_text:
            body["query"] = {"value": query_text}
        return body

    for page in range(max_pages):
        payload = None
        for with_query in ((True, False) if query_text else (False,)):
            try:
                payload = http_post_json(
                    session,
                    f"https://api.reliefweb.int/v2/jobs?appname={appname}",
                    json_body=build_body(page, with_query),
                )
                break
            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else None
                if with_query and status in (400, 422):
                    logger.warning("ReliefWeb query rejected (HTTP %s) - retrying page %s without query.", status, page + 1)
                    continue
                stats["failed"] += 1
                if status in (401, 403):
                    stats["errors"].append(
                        f"ReliefWeb rejected the appname (HTTP {status}). "
                        "Request an approved appname for free at https://apidoc.reliefweb.int/")
                else:
                    stats["errors"].append(f"ReliefWeb request failed (page {page + 1}): {exc}")
                logger.error("ReliefWeb request failed on page %s: %s", page + 1, exc)
                break
            except requests.RequestException as exc:
                stats["failed"] += 1
                stats["errors"].append(f"ReliefWeb request failed (page {page + 1}): {exc}")
                logger.error("ReliefWeb request failed on page %s: %s", page + 1, exc)
                break
            except ValueError:
                stats["failed"] += 1
                stats["errors"].append(f"ReliefWeb returned non-JSON on page {page + 1}.")
                break
        if payload is None:  # hard failure already logged and counted
            break

        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            stats["failed"] += 1
            stats["errors"].append("ReliefWeb response did not contain a 'data' list.")
            break
        if not data:
            break

        for item in data:
            try:
                fields_ = item.get("fields", {}) if isinstance(item, dict) else {}
                title = str(fields_.get("title", "")).strip()
                organization = (_first_names(fields_.get("source")) or [""])[0]
                url = str(fields_.get("url", "")).strip() or f"https://reliefweb.int/job/{item.get('id', '')}"
                body_text = strip_html(str(fields_.get("body", "")))
                country = (_first_names(fields_.get("country")) or [""])[0]
                city = (_first_names(fields_.get("city")) or [""])[0]
                location = ", ".join(part for part in (city, country) if part)
                date_block = fields_.get("date") or {}
                if not isinstance(date_block, dict):
                    date_block = {}
                posted = str(date_block.get("created", ""))[:10]
                closing = str(date_block.get("closing", ""))[:10]
                family = (_first_names(fields_.get("career_categories")) or [""])[0]
                experience = (_first_names(fields_.get("experience")) or [""])[0]
                record = JobRecord(
                    job_id=make_job_id("reliefweb", str(item.get("id", url))),
                    title=title,
                    organization=organization,
                    location=location,
                    country=country,
                    remote_type="",
                    contract_type="",
                    grade=experience,
                    posted_date=posted,
                    closing_date=closing,
                    description=body_text,
                    url=url,
                    source="reliefweb",
                    source_url=url,
                    sector="international development",
                    job_family=family,
                )
                final = _finalize(record)
                if final is None:
                    stats["skipped"] += 1
                    continue
                records.append(final)
                stats["collected"] += 1
                if len(records) >= max_records:
                    break
            except Exception as exc:  # noqa: BLE001 - per-record isolation
                stats["skipped"] += 1
                stats["errors"].append(f"ReliefWeb record skipped: {exc}")
                logger.warning("ReliefWeb record skipped: %s", exc)
        if len(records) >= max_records:
            break

    logger.info("ReliefWeb: collected=%s skipped=%s failed=%s",
                stats["collected"], stats["skipped"], stats["failed"])
    return records, stats


# ---------------------------------------------------------------------------
# Greenhouse
# ---------------------------------------------------------------------------

def fetch_greenhouse(cfg: dict, session: requests.Session | None = None) -> tuple[list[JobRecord], dict]:
    stats = empty_stats()
    records: list[JobRecord] = []
    boards = cfg.get("sources", {}).get("greenhouse", {}).get("boards", []) or []
    session = session or build_session()

    for board in boards:
        name = str(board.get("name", board.get("board", "unknown")))
        board_id = str(board.get("board", "")).strip()
        if not board_id:
            stats["failed"] += 1
            stats["errors"].append(f"Greenhouse board missing 'board' id: {board}")
            continue
        content_flag = "true" if board.get("content", True) else "false"
        url = f"https://boards-api.greenhouse.io/v1/boards/{board_id}/jobs"
        try:
            payload = http_get_json(session, url, params={"content": content_flag})
        except requests.RequestException as exc:
            stats["failed"] += 1
            stats["errors"].append(f"Greenhouse '{name}' failed: {exc}")
            logger.error("Greenhouse board %s failed: %s", name, exc)
            continue
        except ValueError:
            stats["failed"] += 1
            stats["errors"].append(f"Greenhouse '{name}' returned non-JSON.")
            continue

        jobs = payload.get("jobs") if isinstance(payload, dict) else None
        if not isinstance(jobs, list):
            stats["failed"] += 1
            stats["errors"].append(f"Greenhouse '{name}' response missing 'jobs' list.")
            continue

        for item in jobs:
            try:
                title = str(item.get("title", "")).strip()
                url_job = str(item.get("absolute_url", "")).strip()
                location_block = item.get("location") or {}
                location = str(location_block.get("name", "")).strip() if isinstance(location_block, dict) else str(location_block or "").strip()
                posted = str(item.get("updated_at", ""))[:10]
                content_html = str(item.get("content", "") or "")
                description = strip_html(content_html)
                record = JobRecord(
                    job_id=make_job_id("greenhouse", f"{board_id}-{item.get('id', url_job)}"),
                    title=title,
                    organization=name,
                    location=location,
                    country="",
                    remote_type="",
                    contract_type="",
                    grade="",
                    posted_date=posted,
                    closing_date="",
                    description=description,
                    url=url_job,
                    source="greenhouse",
                    source_url=f"https://boards.greenhouse.io/{board_id}",
                )
                final = _finalize(record)
                if final is None:
                    stats["skipped"] += 1
                    continue
                records.append(final)
                stats["collected"] += 1
            except Exception as exc:  # noqa: BLE001
                stats["skipped"] += 1
                stats["errors"].append(f"Greenhouse '{name}' record skipped: {exc}")
    logger.info("Greenhouse: collected=%s skipped=%s failed=%s",
                stats["collected"], stats["skipped"], stats["failed"])
    return records, stats


# ---------------------------------------------------------------------------
# Lever
# ---------------------------------------------------------------------------

def fetch_lever(cfg: dict, session: requests.Session | None = None) -> tuple[list[JobRecord], dict]:
    stats = empty_stats()
    records: list[JobRecord] = []
    boards = cfg.get("sources", {}).get("lever", {}).get("boards", []) or []
    session = session or build_session()

    for board in boards:
        name = str(board.get("name", board.get("company", "unknown")))
        company = str(board.get("company", "")).strip()
        if not company:
            stats["failed"] += 1
            stats["errors"].append(f"Lever board missing 'company' id: {board}")
            continue
        eu = bool(board.get("eu", False))
        base = "https://eu.api.lever.co" if eu else "https://api.lever.co"
        url = f"{base}/v0/postings/{company}"
        try:
            payload = http_get_json(session, url, params={"mode": "json"})
        except requests.RequestException as exc:
            stats["failed"] += 1
            stats["errors"].append(f"Lever '{name}' failed: {exc}")
            logger.error("Lever board %s failed: %s", name, exc)
            continue
        except ValueError:
            stats["failed"] += 1
            stats["errors"].append(f"Lever '{name}' returned non-JSON.")
            continue

        if not isinstance(payload, list):
            stats["failed"] += 1
            stats["errors"].append(f"Lever '{name}' response was not a list.")
            continue

        for item in payload:
            try:
                title = str(item.get("text", "")).strip()
                url_job = str(item.get("hostedUrl", "") or item.get("applyUrl", "")).strip()
                categories = item.get("categories") or {}
                location = str(categories.get("location", "") or "").strip()
                commitment = str(categories.get("commitment", "") or "").strip()
                team = str(categories.get("team", "") or "").strip()
                created = item.get("createdAt")
                posted = ""
                if isinstance(created, (int, float)):
                    posted = datetime.fromtimestamp(created / 1000).date().isoformat()
                description = strip_html(str(item.get("descriptionPlain") or item.get("description", "") or ""))
                workplace = str(item.get("workplaceType", "") or "").strip()
                remote_type = "remote" if workplace == "remote" else ("hybrid" if workplace == "hybrid" else ("on-site" if workplace == "on-site" else ""))
                record = JobRecord(
                    job_id=make_job_id("lever", f"{company}-{item.get('id', url_job)}"),
                    title=title,
                    organization=name,
                    location=location,
                    country="",
                    remote_type=remote_type,
                    contract_type=commitment,
                    grade=team,
                    posted_date=posted,
                    closing_date="",
                    description=description,
                    url=url_job,
                    source="lever",
                    source_url=f"https://jobs.lever.co/{company}",
                )
                final = _finalize(record)
                if final is None:
                    stats["skipped"] += 1
                    continue
                records.append(final)
                stats["collected"] += 1
            except Exception as exc:  # noqa: BLE001
                stats["skipped"] += 1
                stats["errors"].append(f"Lever '{name}' record skipped: {exc}")
    logger.info("Lever: collected=%s skipped=%s failed=%s",
                stats["collected"], stats["skipped"], stats["failed"])
    return records, stats


# ---------------------------------------------------------------------------
# Ashby
# ---------------------------------------------------------------------------

def fetch_ashby(cfg: dict, session: requests.Session | None = None) -> tuple[list[JobRecord], dict]:
    stats = empty_stats()
    records: list[JobRecord] = []
    boards = cfg.get("sources", {}).get("ashby", {}).get("boards", []) or []
    session = session or build_session()

    for board in boards:
        name = str(board.get("name", board.get("org", "unknown")))
        org = str(board.get("org", "")).strip()
        if not org:
            stats["failed"] += 1
            stats["errors"].append(f"Ashby board missing 'org' id: {board}")
            continue
        params = {"includeCompensation": "true"} if board.get("includeCompensation", True) else {}
        url = f"https://api.ashbyhq.com/posting-api/job-board/{org}"
        try:
            payload = http_get_json(session, url, params=params)
        except requests.RequestException as exc:
            stats["failed"] += 1
            stats["errors"].append(f"Ashby '{name}' failed: {exc}")
            logger.error("Ashby board %s failed: %s", name, exc)
            continue
        except ValueError:
            stats["failed"] += 1
            stats["errors"].append(f"Ashby '{name}' returned non-JSON.")
            continue

        jobs = payload.get("jobs") if isinstance(payload, dict) else None
        if not isinstance(jobs, list):
            stats["failed"] += 1
            stats["errors"].append(f"Ashby '{name}' response missing 'jobs' list.")
            continue

        for item in jobs:
            try:
                title = str(item.get("title", "")).strip()
                url_job = str(item.get("applyUrl", "") or item.get("jobUrl", "")).strip()
                location = str(item.get("location", "") or "").strip()
                is_remote = bool(item.get("isRemote", False))
                remote_type = "remote" if is_remote else ("on-site" if location else "")
                description = strip_html(str(item.get("descriptionHtml", "") or item.get("descriptionPlain", "") or ""))
                posted = str(item.get("publishedDate", ""))[:10]
                record = JobRecord(
                    job_id=make_job_id("ashby", f"{org}-{item.get('id', url_job)}"),
                    title=title,
                    organization=name,
                    location=location,
                    country="",
                    remote_type=remote_type,
                    contract_type=str(item.get("employmentType", "") or ""),
                    grade=str(item.get("department", "") or item.get("team", "") or ""),
                    posted_date=posted,
                    closing_date="",
                    description=description,
                    url=url_job,
                    source="ashby",
                    source_url=f"https://jobs.ashbyhq.com/{org}",
                )
                final = _finalize(record)
                if final is None:
                    stats["skipped"] += 1
                    continue
                records.append(final)
                stats["collected"] += 1
            except Exception as exc:  # noqa: BLE001
                stats["skipped"] += 1
                stats["errors"].append(f"Ashby '{name}' record skipped: {exc}")
    logger.info("Ashby: collected=%s skipped=%s failed=%s",
                stats["collected"], stats["skipped"], stats["failed"])
    return records, stats


# ---------------------------------------------------------------------------
# Manual CSV import
# ---------------------------------------------------------------------------

REQUIRED_MANUAL_COLUMNS = ("title", "organization", "url")
MANUAL_COLUMNS = ["title", "organization", "location", "country", "contract_type",
                  "grade", "posted_date", "closing_date", "description", "url",
                  "remote_type", "sector", "job_family"]


def import_manual_csv(file_obj: Any) -> tuple[list[JobRecord], list[str]]:
    """Import a user-supplied CSV. Returns (records, row-level error messages)."""
    errors: list[str] = []
    records: list[JobRecord] = []
    try:
        raw = file_obj.read()
        if isinstance(raw, bytes):
            text = raw.decode("utf-8-sig", errors="replace")
        else:
            text = str(raw)
    except Exception as exc:  # noqa: BLE001
        return [], [f"Could not read the uploaded file: {exc}"]

    try:
        reader = list(csv.DictReader(io.StringIO(text)))
    except csv.Error as exc:
        return [], [f"CSV parsing failed: {exc}"]

    if not reader:
        return [], ["CSV contains no data rows."]

    missing = [c for c in REQUIRED_MANUAL_COLUMNS if c not in (reader[0].keys() if reader else [])]
    if missing:
        return [], [f"Missing required column(s): {', '.join(missing)}. Required: {', '.join(REQUIRED_MANUAL_COLUMNS)}."]

    for idx, row in enumerate(reader, start=2):  # header is row 1
        try:
            get = lambda key: str(row.get(key) or "").strip()  # noqa: E731
            title, organization, url = get("title"), get("organization"), get("url")
            if not title or not organization or not url:
                errors.append(f"Row {idx}: title, organization and url are required - skipped.")
                continue
            closing = get("closing_date")
            if closing and parse_iso_date(closing) is None:
                errors.append(f"Row {idx}: closing_date '{closing}' is not a recognizable date - kept, but date handling may be off.")
            record = JobRecord(
                job_id=make_job_id("manual", f"{organization}|{title}|{url}"),
                title=title,
                organization=organization,
                location=get("location"),
                country=get("country"),
                remote_type=get("remote_type"),
                contract_type=get("contract_type"),
                grade=get("grade"),
                posted_date=get("posted_date"),
                closing_date=closing,
                description=strip_html(get("description")),
                url=url,
                source="manual",
                source_url=url,
                sector=get("sector"),
                job_family=get("job_family"),
            )
            final = _finalize(record)
            if final is None:
                errors.append(f"Row {idx}: missing title/organization/url - skipped.")
                continue
            records.append(final)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Row {idx}: {exc}")

    return records, errors


def job_from_pasted_text(*, title: str, organization: str, location: str,
                         url: str, closing_date: str, description: str) -> JobRecord:
    """Create a record from a manually pasted vacancy."""
    closing = (closing_date or "").strip()
    record = JobRecord(
        job_id=make_job_id("manual", f"{organization}|{title}|{url or description[:80]}"),
        title=title.strip(),
        organization=organization.strip(),
        location=location.strip(),
        country="",
        remote_type="",
        contract_type="",
        grade="",
        posted_date=date.today().isoformat(),
        closing_date=closing,
        description=strip_html(description),
        url=url.strip() or "about:blank",
        source="manual",
        source_url=url.strip(),
        sector="",
        job_family="",
    )
    final = _finalize(record)
    if final is None:
        raise ValueError("title, organization and url are required.")
    return final


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def collect_all(cfg: dict, appname: str | None, *, session: requests.Session | None = None) -> tuple[list[JobRecord], dict[str, dict]]:
    """Run every enabled source. One failing source never stops the others."""
    session = session or build_session()
    all_records: list[JobRecord] = []
    status: dict[str, dict] = {}

    def run(name: str, enabled: bool, fn):
        if not enabled:
            status[name] = {"enabled": False, **empty_stats()}
            return []
        try:
            got, stats = fn()
            status[name] = {"enabled": True, **stats}
            return got
        except Exception as exc:  # noqa: BLE001 - a source must never crash the collector
            logger.exception("Source %s crashed", name)
            status[name] = {"enabled": True, **empty_stats(), "failed": 1,
                            "errors": [f"{name} crashed: {exc}"]}
            return []

    all_records += run("reliefweb", cfg.get("sources", {}).get("reliefweb", {}).get("enabled", True),
                       lambda: fetch_reliefweb(cfg, appname, session))
    all_records += run("greenhouse", cfg.get("sources", {}).get("greenhouse", {}).get("enabled", True),
                       lambda: fetch_greenhouse(cfg, session))
    all_records += run("lever", cfg.get("sources", {}).get("lever", {}).get("enabled", True),
                       lambda: fetch_lever(cfg, session))
    all_records += run("ashby", cfg.get("sources", {}).get("ashby", {}).get("enabled", True),
                       lambda: fetch_ashby(cfg, session))

    return all_records, status


# =========================================================================
# Collection CLI
# =========================================================================

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
logger = logging.getLogger("damir-job-radar")

REPO_ROOT = Path(__file__).resolve().parent


def load_config(path: Path | None = None) -> dict:
    path = path or (REPO_ROOT / "config.yaml")
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def dedup_records(records: list[JobRecord], threshold: int = 88) -> tuple[list[JobRecord], int]:
    """Canonical-URL pass first, then a fuzzy org+title+location fingerprint."""
    before = len(records)
    # Pass 1: canonical URL
    url_pass: list[JobRecord] = []
    url_index: dict[str, int] = {}
    for rec in records:
        key = canonical_url(rec.url)
        if key and key in url_index:
            i = url_index[key]
            # Keep the more complete record (longer description wins).
            if len(rec.description or "") > len(url_pass[i].description or ""):
                url_pass[i] = rec
            continue
        if key:
            url_index[key] = len(url_pass)
        url_pass.append(rec)

    # Pass 2: fuzzy fingerprint on org + title + location
    result: list[JobRecord] = []
    fps: list[str] = []
    for rec in url_pass:
        fp = f"{normalize_fingerprint(rec.organization)} {normalize_fingerprint(rec.title)} {normalize_fingerprint(rec.location)}"
        dup_index = -1
        for i, prev_fp in enumerate(fps):
            if fuzz.token_set_ratio(fp, prev_fp) >= threshold:
                dup_index = i
                break
        if dup_index == -1:
            result.append(rec)
            fps.append(fp)
        else:
            if len(rec.description or "") > len(result[dup_index].description or ""):
                result[dup_index] = rec
                fps[dup_index] = fp
    return result, before - len(result)


def filter_expired(records: list[JobRecord], today: date | None = None) -> tuple[list[JobRecord], int]:
    today = today or date.today()
    kept: list[JobRecord] = []
    expired = 0
    for rec in records:
        closing = parse_iso_date(rec.closing_date)
        if closing is not None and closing < today:
            expired += 1
            continue
        kept.append(rec)
    return kept, expired


def sort_records(records: list[JobRecord]) -> list[JobRecord]:
    status_rank = {"excellent_fit": 0, "strong_fit": 1, "good_fit": 2, "possible_fit": 3,
                   "review_required": 4, "low_priority": 5, "disqualified": 6}
    return sorted(records, key=lambda r: (status_rank.get(r.fit_status, 5), -r.score))


# ---------------------------------------------------------------------------
# Sample dataset (clearly labelled, relative dates so it never expires)
# ---------------------------------------------------------------------------

def build_sample_records() -> list[JobRecord]:
    today = date.today()
    iso = lambda d: d.isoformat()
    sample = [
        dict(title="Construction Engineer - Infrastructure Rehabilitation",
             organization="UNOPS", location="Sarajevo, Bosnia and Herzegovina", country="Bosnia and Herzegovina",
             closing_date=iso(today + timedelta(days=21)), posted_date=iso(today - timedelta(days=3)),
             url="https://example.org/sample/unops-construction-engineer",
             description=("UNOPS seeks a Construction Engineer for infrastructure rehabilitation projects across Bosnia and "
                          "Herzegovina. Responsibilities include site supervision, contract administration, tender documentation, "
                          "bills of quantities, budget monitoring, quality assurance, defects liability tracking and handover. "
                          "Works include water and sanitation infrastructure, community facilities and EU/IPA-funded rehabilitation. "
                          "Field travel and site visits required; manual driving licence B required. FIDIC awareness desirable. "
                          "PMP certification is an asset. Fluent English; Bosnian/Croatian/Serbian required."),
             sector="UN / international development", job_family="Construction Engineering"),
        dict(title="WASH Infrastructure Specialist", organization="UNICEF",
             location="Remote with field missions (Western Balkans)", country="Bosnia and Herzegovina",
             remote_type="hybrid", closing_date=iso(today + timedelta(days=14)), posted_date=iso(today - timedelta(days=1)),
             url="https://example.org/sample/unicef-wash-specialist",
             description=("UNICEF WASH Infrastructure Specialist for municipal water and sanitation systems. Scope: technical "
                          "specifications, tendering, contract administration of works, quality assurance of construction, "
                          "budget monitoring, stakeholder engagement with municipal and government partners, post-conflict recovery "
                          "programming, EU/IPA funded projects. Requires 7+ years in construction or infrastructure delivery, "
                          "professional English. Driving licence and field travel required."),
             sector="UN / international development", job_family="WASH Infrastructure"),
        dict(title="Rehabilitation Project Manager", organization="EU Delegation (IPA)",
             location="Sarajevo, Bosnia and Herzegovina", country="Bosnia and Herzegovina",
             closing_date=iso(today + timedelta(days=25)), posted_date=iso(today - timedelta(days=6)),
             url="https://example.org/sample/eu-ipa-rehab-pm",
             description=("Project Manager for EU/IPA-funded post-conflict reconstruction of public infrastructure. Responsibilities: "
                          "project management, procurement planning, tender documentation, contract administration, cost estimation, "
                          "budget monitoring, government coordination, stakeholder engagement. Rehabilitation of schools, health "
                          "facilities and built heritage. Requires 5+ years project management experience; PMP desirable."),
             sector="European Union / IPA", job_family="Rehabilitation Project Management"),
        dict(title="Built Heritage Conservation Specialist", organization="Council of Europe / EU",
             location="Skopje, North Macedonia", country="North Macedonia",
             closing_date=iso(today + timedelta(days=30)), posted_date=iso(today - timedelta(days=4)),
             url="https://example.org/sample/heritage-conservation",
             description=("Cultural heritage conservation specialist for EU-funded built heritage projects. MSc in Built Heritage "
                          "Conservation required; architectural engineering background desirable. Responsibilities: conservation "
                          "planning, technical specifications, budget monitoring, stakeholder engagement with cultural institutions, "
                          "tender documentation. Professional English and EU project experience an asset."),
             sector="Cultural heritage / EU", job_family="Built Heritage"),
        dict(title="Infrastructure Project Manager - Roads", organization="European Bank for Reconstruction and Development",
             location="Sarajevo, Bosnia and Herzegovina", country="Bosnia and Herzegovina",
             closing_date=iso(today + timedelta(days=5)), posted_date=iso(today - timedelta(days=10)),
             url="https://example.org/sample/ebrd-infrastructure-pm",
             description=("Infrastructure project manager to oversee EU/IPA-funded civil works contracts. Responsibilities: contract "
                          "administration, FIDIC-based tendering, cost estimation, budget monitoring, quality assurance, defects "
                          "liability, stakeholder engagement with government institutions. Requires 8+ years experience in construction "
                          "or infrastructure delivery and excellent English. Driving licence required."),
             sector="Development bank", job_family="Infrastructure Project Management"),
        dict(title="Project Manager - Solar Energy Programme", organization="UNDP",
             location="Sarajevo, Bosnia and Herzegovina", country="Bosnia and Herzegovina",
             closing_date=iso(today + timedelta(days=18)), posted_date=iso(today - timedelta(days=2)),
             url="https://example.org/sample/undp-solar-pm",
             description=("Programme project manager for municipal solar infrastructure (photovoltaics on public buildings). Manage "
                          "procurement, tender documentation, contract administration, cost estimates, budget monitoring, stakeholder "
                          "engagement with municipal and government partners, reporting on EU/IPA-funded grants. Field travel required."),
             sector="UN / international development", job_family="Solar Infrastructure"),
        dict(title="Senior Procurement Officer - Works", organization="UNICEF",
             location="Geneva, Switzerland (hybrid)", country="Switzerland", remote_type="hybrid",
             closing_date=iso(today + timedelta(days=12)), posted_date=iso(today - timedelta(days=7)),
             url="https://example.org/sample/unicef-procurement",
             description=("Senior procurement officer for works contracts (construction, rehabilitation, facilities). Lead tendering, "
                          "tender documentation review, bills of quantities, contract administration, budget monitoring, supplier "
                          "evaluation. FIDIC awareness and works-procurement experience required; 7+ years; fluent English. "
                          "Nationals of all countries may apply."),
             sector="UN / international development", job_family="Procurement and Contracts"),
        dict(title="Facilities Manager", organization="International Committee of the Red Cross",
             location="Banja Luka, Bosnia and Herzegovina", country="Bosnia and Herzegovina",
             closing_date=iso(today - timedelta(days=2)), posted_date=iso(today - timedelta(days=40)),
             url="https://example.org/sample/icrc-facilities",
             description="Expired sample record. Facilities management of office buildings, 4WD fleet, contracts.",
             sector="Humanitarian", job_family="Facilities Management"),
        dict(title="Software Engineer - Backend (Python/Django)", organization="GlobalTech Solutions",
             location="Berlin, Germany (remote)", country="Germany", remote_type="remote",
             closing_date=iso(today + timedelta(days=15)), posted_date=iso(today - timedelta(days=1)),
             url="https://example.org/sample/globaltech-swe",
             description=("Backend software engineer to build microservices. Python, Django, PostgreSQL, Kubernetes, CI/CD, unit "
                          "testing, code review. Strong coding skills, data structures and algorithms required. 5+ years commercial "
                          "software development."),
             sector="Private sector", job_family="Software Engineering"),
        dict(title="Entry-Level Graduate Programme - Construction", organization="Regional Construction Group",
             location="Tuzla, Bosnia and Herzegovina", country="Bosnia and Herzegovina",
             closing_date=iso(today + timedelta(days=20)), posted_date=iso(today - timedelta(days=5)),
             url="https://example.org/sample/grad-programme",
             description=("Graduate programme for recent civil engineering graduates. Two-year rotational scheme. Entry level; junior "
                          "support to site engineers; no prior professional experience required."),
             sector="Private sector", job_family="Graduate Programme"),
        dict(title="Technical Adviser - Infrastructure Reconstruction", organization="World Bank",
             location="Sarajevo, Bosnia and Herzegovina", country="Bosnia and Herzegovina",
             grade="Senior specialist",
             closing_date=iso(today + timedelta(days=28)), posted_date=iso(today - timedelta(days=3)),
             url="https://example.org/sample/worldbank-ta",
             description=("Technical Adviser supporting government counterparts on EU/IPA infrastructure reconstruction. Responsibilities: "
                          "technical review, procurement oversight, tender documentation, contract administration of works, budget "
                          "monitoring, stakeholder engagement, post-conflict recovery programming. 10+ years infrastructure experience; "
                          "PhD desirable; excellent English; field travel and 4WD driving required."),
             sector="Development bank / international development", job_family="Technical Adviser"),
        dict(title="Programme Officer - EU IPA Infrastructure", organization="UNDP",
             location="Kyiv, Ukraine", country="Ukraine",
             closing_date=iso(today + timedelta(days=9)), posted_date=iso(today - timedelta(days=5)),
             url="https://example.org/sample/undp-programme-officer",
             description=("Programme officer supporting EU/IPA-funded infrastructure projects. Monitor progress, budget monitoring, "
                          "procurement follow-up, reporting, government coordination. 3+ years in international development. Fluent "
                          "English and French (C1) required."),
             sector="UN / international development", job_family="Programme Officer"),
    ]
    records: list[JobRecord] = []
    import hashlib
    for item in sample:
        digest = hashlib.sha256(item["url"].encode()).hexdigest()[:16]
        records.append(JobRecord(
            job_id=f"sample-{digest}",
            title=item["title"], organization=item["organization"],
            location=item.get("location", ""), country=item.get("country", ""),
            remote_type=item.get("remote_type", ""), contract_type=item.get("contract_type", "fixed-term"),
            grade=item.get("grade", ""), posted_date=item["posted_date"],
            closing_date=item.get("closing_date", ""), description=item["description"],
            url=item["url"], source="sample", source_url="built-in sample data",
            sector=item.get("sector", ""), job_family=item.get("job_family", ""),
        ))
    return records


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _any_source_attempted(status: dict, appname: str | None, cfg: dict) -> bool:
    """True if at least one source actually attempted a fetch this run.

    'Nothing configured' (no appname, no ATS boards) is not an outage;
    the feed is preserved but the run is not a failure.
    """
    src = cfg.get("sources", {})
    rw = status.get("reliefweb", {})
    if rw.get("enabled", True) and appname:
        return True
    for name in ("greenhouse", "lever", "ashby"):
        scfg = src.get(name, {})
        if scfg.get("enabled", True) and scfg.get("boards"):
            return True
    # Any recorded hard failure counts as an attempt too.
    return any(int(s.get("failed", 0) or 0) > 0 for s in status.values())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Damir Job Radar collector")
    parser.add_argument("--sample", action="store_true",
                        help="Write the built-in demo dataset instead of calling live APIs.")
    parser.add_argument("--output", type=Path, default=FEED_PATH,
                        help="Override the output CSV path.")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format=LOG_FORMAT, stream=sys.stdout)

    cfg = load_config()

    if args.sample:
        logger.info("Sample mode: writing built-in demo dataset.")
        records = build_sample_records()
        status = {"sample": {"enabled": True, "collected": len(records), "skipped": 0, "failed": 0, "errors": []}}
    else:
        appname = os.environ.get("RELIEFWEB_APPNAME", "").strip() or None
        if not appname:
            logger.warning("RELIEFWEB_APPNAME is not set - ReliefWeb will be skipped. "
                           "Set it to your approved appname to enable ReliefWeb.")
        records, status = collect_all(cfg, appname)
        if not records:
            total_collected = sum(s.get("collected", 0) for s in status.values())
            if total_collected == 0 and args.output.exists() and args.output.stat().st_size > 0:
                if not _any_source_attempted(status, appname, cfg):
                    # Nothing was attempted, so this is a configuration gap, not
                    # an outage: keep the feed, stay honest, don't cry wolf.
                    logger.warning("No job sources are configured (ReliefWeb skipped: no "
                                   "RELIEFWEB_APPNAME; no ATS boards in config.yaml). "
                                   "Feed preserved at %s - set RELIEFWEB_APPNAME or add boards to start collecting.",
                                   args.output)
                    write_collection_status({"ok": False, "reason": "no_sources_configured",
                                             "sources": status, "feed_preserved": True})
                    return 0
                # At least one source was attempted and all failed: do NOT overwrite the feed.
                logger.error("All sources failed (%s). Keeping existing feed at %s.",
                             ", ".join(f"{k}: {'; '.join(v.get('errors', [])[:1]) or 'no records'}" for k, v in status.items()),
                             args.output)
                write_collection_status({"ok": False, "reason": "all_sources_failed",
                                         "sources": status, "feed_preserved": True})
                return 3
            logger.warning("No records collected and no existing feed to preserve. Writing empty feed.")

    # Expiry filter
    records, expired_count = filter_expired(records)
    logger.info("Expired records removed: %s", expired_count)

    # Deduplication
    threshold = int(cfg.get("dedup", {}).get("fingerprint_threshold", 88))
    records, dup_count = dedup_records(records, threshold)
    logger.info("Duplicates removed: %s", dup_count)

    # days_remaining + scoring
    for rec in records:
        rec.days_remaining = days_until(parse_iso_date(rec.closing_date))
        score_job(rec, cfg)
    records = sort_records(records)

    records_to_csv(records, args.output)
    write_collection_status({
        "ok": True,
        "sample_mode": bool(args.sample),
        "total_written": len(records),
        "expired_removed": expired_count,
        "duplicates_removed": dup_count,
        "sources": status,
    })
    logger.info("Wrote %s records to %s", len(records), args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
