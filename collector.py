"""Damir Job Radar collector.

Usage:
    python collector.py            # collect from configured live sources
    python collector.py --sample   # write a small built-in demo dataset

Safety rules:
- Expired vacancies are excluded.
- Duplicates are removed (canonical URL first, fuzzy fingerprint second).
- If every configured source fails, the previous feed is preserved and the
  process exits with code 3. An empty file never overwrites a valid feed.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import yaml

from database import (FEED_PATH, JobRecord, days_until, records_to_csv,
                      parse_iso_date, write_collection_status)
from scoring import score_job, canonical_url, normalize_fingerprint
from sources import collect_all
from rapidfuzz import fuzz

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
logger = logging.getLogger("damir-job-radar.collector")

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
                # All sources failed and we have an existing feed: do NOT overwrite it.
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
