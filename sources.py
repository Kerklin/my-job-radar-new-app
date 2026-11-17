"""Source adapters for Damir Job Radar.

Every adapter:
- uses a shared HTTP session with retries, timeouts and a descriptive User-Agent;
- verifies HTTP status before parsing;
- validates the expected response structure;
- skips malformed records individually instead of failing the whole source;
- logs collected / skipped / failed counts;
- never raises out of ``collect_all`` - a failed source is logged and skipped.

Configuration comes from config.yaml only. No board identifiers are hard-coded
as logic, and no source is scraped if it does not expose a public feed.
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
import re
from datetime import date, datetime
from typing import Any, Iterable

import requests
from requests.adapters import HTTPAdapter

try:
    from urllib3.util.retry import Retry
except Exception:  # pragma: no cover - extremely defensive fallback
    Retry = None  # type: ignore[assignment]

from database import JobRecord, days_until, ensure_data_dir, parse_iso_date, strip_html

logger = logging.getLogger("damir-job-radar.sources")

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

def fetch_reliefweb(cfg: dict, appname: str | None, session: requests.Session | None = None) -> tuple[list[JobRecord], dict]:
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

    fields_include = [
        "title", "body", "career", "country", "city", "source",
        "date", "experience", "url", "language",
    ]

    for page in range(max_pages):
        offset = page * limit
        body = {
            "limit": limit,
            "offset": offset,
            "fields": {"include": fields_include},
            "filter": {"field": "status", "value": "active"},
            "sort": ["date.posted:desc"],
        }
        try:
            payload = http_post_json(
                session,
                f"https://api.reliefweb.int/v1/jobs?appname={appname}",
                json_body=body,
            )
        except requests.RequestException as exc:
            stats["failed"] += 1
            stats["errors"].append(f"ReliefWeb request failed (page {page + 1}): {exc}")
            logger.error("ReliefWeb request failed on page %s: %s", page + 1, exc)
            break
        except ValueError:
            stats["failed"] += 1
            stats["errors"].append(f"ReliefWeb returned non-JSON on page {page + 1}.")
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
                sources = fields_.get("source") or []
                organization = ""
                if isinstance(sources, list) and sources:
                    organization = str(sources[0].get("name", "")).strip()
                url = str(fields_.get("url", "")).strip() or f"https://reliefweb.int/job/{item.get('id', '')}"
                body_text = strip_html(str(fields_.get("body", "")))
                countries = fields_.get("country") or []
                country = ""
                if isinstance(countries, list) and countries:
                    country = str(countries[0].get("name", "")).strip()
                cities = fields_.get("city") or []
                city = str(cities[0].get("name", "")).strip() if isinstance(cities, list) and cities else ""
                location = ", ".join(part for part in (city, country) if part)
                date_block = fields_.get("date") or {}
                # ReliefWeb date block uses 'created' / 'closing' / 'changed'
                posted = str(date_block.get("created", ""))[:10]
                closing = str(date_block.get("closing", ""))[:10]
                careers = fields_.get("career") or []
                family = ""
                if isinstance(careers, list) and careers:
                    family = str(careers[0].get("name", "")).strip()
                record = JobRecord(
                    job_id=make_job_id("reliefweb", str(item.get("id", url))),
                    title=title,
                    organization=organization,
                    location=location,
                    country=country,
                    remote_type="",
                    contract_type="",
                    grade=str(fields_.get("experience", "") or "").strip(),
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
