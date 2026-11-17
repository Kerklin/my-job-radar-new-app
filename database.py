"""Storage layer for Damir Job Radar.

Two kinds of storage are used:

1. ``data/latest_jobs.csv`` - the consolidated feed committed by GitHub
   Actions and read by Streamlit. This is the deployment read path.
2. A small SQLite database (``data/jobs.db``) for personal statuses
   (saved / applied / rejected / notes) when running locally. On hosts
   where the filesystem is read-only or ephemeral, statuses fall back to
   the browser session plus a downloadable CSV.
"""

from __future__ import annotations

import csv
import html
import io
import json
import sqlite3
from dataclasses import asdict, dataclass, field, fields
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

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
