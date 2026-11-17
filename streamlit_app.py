"""Damir Job Radar - Streamlit dashboard.

Reads the repository-managed feed at data/latest_jobs.csv (produced by the
daily GitHub Action). Personal statuses live in the browser session and, when
the filesystem is writable, in data/jobs.db; they can always be downloaded
and restored as CSV.

Design goals: one job list ranked by fit, an honest "is this live data or
demo data" banner, and everything operational in three tabs (Jobs, Shortlist,
Settings). No secrets are rendered, missing configuration gets actionable
guidance, and description text is never rendered as raw HTML.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import streamlit as st
import yaml

from radar import (FEED_PATH, JobRecord, explanation_markdown,
                   import_manual_csv, dataframe_to_csv_bytes, job_from_pasted_text,
                   load_feed, load_statuses, read_collection_status, records_to_csv,
                   save_status, score_job, statuses_to_csv_bytes)

REPO_ROOT = Path(__file__).resolve().parent
REPO_CONFIG = REPO_ROOT / "config.yaml"

st.set_page_config(page_title="Damir Job Radar", page_icon="📡", layout="wide")

CATEGORY_LABEL = {
    "excellent": ("🟢", "Excellent fit (85-100)"),
    "strong": ("🟢", "Strong fit (75-84)"),
    "good": ("🟡", "Good fit (65-74)"),
    "possible": ("🟠", "Possible fit (50-64)"),
    "low": ("⚪", "Low priority (<50)"),
    "disqualified": ("🔴", "Disqualified (mandatory gap)"),
}
FIT_STATUS_LABEL = {
    "excellent_fit": "Excellent fit", "strong_fit": "Strong fit", "good_fit": "Good fit",
    "possible_fit": "Possible / stretch fit", "review_required": "Review required",
    "low_priority": "Low priority", "disqualified": "Disqualified",
}
FEED_COLUMNS_FOR_TABLE = ["title", "organization", "location", "score", "score_category",
                          "closing_date", "days_remaining", "source"]


# ---------------------------------------------------------------------------
# Loading helpers
# ---------------------------------------------------------------------------

@st.cache_data(ttl=300, show_spinner=False)
def load_feed_cached() -> tuple[list[JobRecord], str]:
    records = load_feed(FEED_PATH)
    updated = ""
    try:
        updated = datetime.fromtimestamp(FEED_PATH.stat().st_mtime).strftime("%Y-%m-%d %H:%M UTC")
    except OSError:
        pass
    return records, updated


def records_to_frame(records: list[JobRecord]) -> pd.DataFrame:
    rows = []
    for r in records:
        row = dict(r.__dict__)
        for key in ("matched_keywords", "strengths", "gaps", "uncertainties", "penalties", "mandatory_findings"):
            row[key] = "; ".join(str(x) if not isinstance(x, dict) else ", ".join(f"{k}: {v}" for k, v in x.items())
                                 for x in (row.get(key) or []))
        row["score_breakdown"] = json.dumps(row.get("score_breakdown") or {}, ensure_ascii=False)
        rows.append(row)
    return pd.DataFrame(rows)


def days_remaining_label(val) -> str:
    try:
        days = int(val)
    except (TypeError, ValueError):
        return "n/a"
    if days < 0:
        return "closed"
    if days == 0:
        return "closes today"
    if days == 1:
        return "1 day left"
    return f"{days} days left"


# ---------------------------------------------------------------------------
# UI fragments
# ---------------------------------------------------------------------------

def render_job_card(row, statuses: dict, idx_prefix: str) -> None:
    job_id = str(row.get("job_id", ""))
    current = statuses.get(job_id, {})
    dot, _ = CATEGORY_LABEL.get(str(row.get("score_category")), ("⚪", "Low priority"))
    with st.expander(
        f"{dot} **{row.get('score', 0)}** · {row.get('title', '')} — {row.get('organization', '')}"
        f"  ·  {days_remaining_label(row.get('days_remaining'))} (closes {row.get('closing_date') or 'n/a'})",
        expanded=False,
    ):
        meta = st.columns(4)
        meta[0].markdown(f"**Organization**\n\n{row.get('organization', '')}")
        meta[1].markdown(f"**Location**\n\n{row.get('location', '') or 'n/a'}")
        meta[2].markdown(f"**Contract / grade**\n\n{row.get('contract_type', '') or 'n/a'} · {row.get('grade', '') or 'n/a'}")
        meta[3].markdown(f"**Source**\n\n{row.get('source', '')}")

        st.markdown(
            f"**Fit:** {CATEGORY_LABEL.get(str(row.get('score_category')), ('⚪', str(row.get('score_category'))))[0]} "
            f"{CATEGORY_LABEL.get(str(row.get('score_category')), ('⚪', str(row.get('score_category'))))[1]} · "
            f"**Status:** {FIT_STATUS_LABEL.get(str(row.get('fit_status')), row.get('fit_status', ''))} · "
            f"**Confidence:** {row.get('confidence')}")
        if row.get("job_family"):
            st.markdown(f"**Job family:** {row['job_family']}")
        if row.get("url"):
            st.link_button("Apply / view vacancy", str(row["url"]), width="content")

        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**Strengths**")
            strengths = str(row.get("strengths", "")).strip()
            if strengths:
                for s in [x for x in strengths.split("; ") if x]:
                    st.markdown(f"- {s}")
            else:
                st.caption("None detected.")
            st.markdown("**Gaps**")
            gaps = str(row.get("gaps", "")).strip()
            if gaps:
                for g in [x for x in gaps.split("; ") if x]:
                    st.markdown(f"- {g}")
            else:
                st.caption("None detected.")
        with c2:
            st.markdown("**Uncertainties (never counted as matched)**")
            unc = str(row.get("uncertainties", "")).strip()
            if unc:
                for u in [x for x in unc.split("; ") if x]:
                    st.markdown(f"- {u}")
            else:
                st.caption("None flagged.")
            st.markdown("**Matched keywords**")
            st.caption(str(row.get("matched_keywords", "")).strip() or "—")

        breakdown = str(row.get("score_breakdown", "")).strip()
        if breakdown and breakdown != "{}":
            with st.expander("Score breakdown (component weights)"):
                try:
                    parsed = json.loads(breakdown)
                    st.dataframe(pd.DataFrame([
                        {"Component": k.replace("_", " ").title(),
                         "Points": v.get("points"), "Max": v.get("max"), "Why": v.get("note")}
                        for k, v in parsed.items()
                    ]), hide_index=True, width="stretch")
                except json.JSONDecodeError:
                    st.caption("Breakdown unavailable.")

        desc = str(row.get("description", "")).strip()
        with st.expander("Full description (plain text)"):
            st.write(desc[:4000] + (" …[truncated]" if len(desc) > 4000 else "") if desc else "No description stored.")

        st.markdown("**Your status**")
        b1, b2, b3, b4 = st.columns(4)
        saved = b1.checkbox("Saved", value=bool(current.get("saved", False)), key=f"save_{idx_prefix}_{job_id}")
        applied = b2.checkbox("Applied", value=bool(current.get("applied", False)), key=f"apply_{idx_prefix}_{job_id}")
        rejected = b3.checkbox("Rejected", value=bool(current.get("rejected", False)), key=f"reject_{idx_prefix}_{job_id}")
        notes = b4.text_input("Notes", value=str(current.get("notes", "")), key=f"notes_{idx_prefix}_{job_id}",
                              label_visibility="collapsed", placeholder="Notes…")
        fresh = {"saved": saved, "applied": applied, "rejected": rejected, "notes": notes}
        old = {"saved": bool(current.get("saved", False)), "applied": bool(current.get("applied", False)),
               "rejected": bool(current.get("rejected", False)), "notes": str(current.get("notes", ""))}
        if fresh != old:
            statuses[job_id] = fresh
            st.session_state["statuses"] = statuses
            save_status(job_id, saved, applied, rejected, notes)


def shortlist_markdown(df: pd.DataFrame, cfg: dict, top_n: int = 10) -> str:
    cand = cfg.get("candidate", {})
    strong = df[df["score"] >= 65].sort_values("score", ascending=False).head(top_n)
    lines = [f"# Job Shortlist - {cand.get('name', 'Candidate')}",
             f"Generated: {date.today().isoformat()} · {len(strong)} roles scoring ≥ 65", ""]
    for _, row in strong.iterrows():
        lines += [
            f"## {row.get('title')} — {row.get('organization')}",
            f"- Score: **{row.get('score')}/100** ({row.get('score_category')}) · Status: {row.get('fit_status')} · Confidence: {row.get('confidence')}",
            f"- Location: {row.get('location') or 'n/a'} · Closes: {row.get('closing_date') or 'n/a'} ({days_remaining_label(row.get('days_remaining'))})",
            f"- Source: {row.get('source')} · Link: {row.get('url')}",
            f"- Strengths: {row.get('strengths') or '—'}",
            f"- Gaps: {row.get('gaps') or 'none'}",
            f"- Uncertainties: {row.get('uncertainties') or '—'}",
            "",
        ]
    if strong.empty:
        lines.append("No roles scored ≥ 65 under the current filters.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

try:
    with open(REPO_CONFIG, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
except Exception:
    cfg = {}

if "statuses" not in st.session_state:
    st.session_state["statuses"] = load_statuses()
statuses = st.session_state["statuses"]

records, feed_updated = load_feed_cached()
df = records_to_frame(records)

has_sample = bool(df["source"].eq("sample").any()) if not df.empty else False
collection_status = read_collection_status()
is_sample_feed = bool(collection_status.get("sample_mode")) or (not df.empty and set(df["source"].unique()) == {"sample"})

st.title("📡 Damir Job Radar")
caption = f"Ranked construction / infrastructure / international-development vacancies · Feed updated: {feed_updated or 'never'}"
if is_sample_feed:
    caption += " · **SAMPLE DATA**"
st.caption(caption)

# ---------------------------------------------------------------- banners
if not collection_status:
    st.info("No collection metadata yet. The daily Action writes it on its next run.", icon="ℹ️")
elif collection_status.get("reason") == "no_sources_configured":
    st.info("Live collection is off because no source is configured. Set the `RELIEFWEB_APPNAME` "
            "repository secret (free at apidoc.reliefweb.int) or add ATS boards to `config.yaml` - "
            "the daily Action then refreshes the feed automatically.", icon="ℹ️")
elif collection_status.get("reason") == "all_sources_failed":
    st.error("All configured sources failed in the last run. The previous feed was preserved. "
             "Check the Actions log.", icon="🛑")
elif is_sample_feed:
    st.warning("The feed currently contains sample (demo) jobs, not live vacancies.", icon="⚠️")

# ---------------------------------------------------------------- sidebar
with st.sidebar:
    st.header("Filters")
    search = st.text_input("Search text", "")
    min_score = st.slider("Minimum match score", 0, 100, 50, 5)
    show_sample = st.checkbox("Show sample (demo) jobs", value=False) if has_sample else True
    only_saved = st.checkbox("Saved / applied only", value=False)
    with st.expander("Advanced filters", expanded=False):
        families = st.multiselect("Job family", sorted({x for x in df.get("job_family", pd.Series(dtype=str)).fillna("") if x})) if not df.empty else []
        orgs = st.multiselect("Organization", sorted(df["organization"].dropna().unique()) if not df.empty else [])
        closing_from = st.date_input("Closing from", value=None, format="YYYY-MM-DD")
        closing_to = st.date_input("Closing to", value=None, format="YYYY-MM-DD")
    st.divider()
    if st.button("Reload feed from disk", width="stretch"):
        load_feed_cached.clear()
        st.rerun()

view = df.copy()
if not show_sample and has_sample:
    view = view[view["source"] != "sample"]
if search:
    q = search.lower()
    mask = ((view["title"].fillna("").str.lower().str.contains(q, na=False)) |
            (view["organization"].fillna("").str.lower().str.contains(q, na=False)) |
            (view["description"].fillna("").str.lower().str.contains(q, na=False)) |
            (view["location"].fillna("").str.lower().str.contains(q, na=False)))
    view = view[mask]
view = view[view["score"] >= min_score]
if families:
    view = view[view["job_family"].isin(families)]
if orgs:
    view = view[view["organization"].isin(orgs)]
if closing_from:
    view = view[view["closing_date"].fillna("9999-12-31") >= closing_from.isoformat()]
if closing_to:
    view = view[view["closing_date"].fillna("0000-01-01") <= closing_to.isoformat()]
if only_saved:
    wanted = {jid for jid, s in statuses.items() if s.get("saved") or s.get("applied")}
    view = view[view["job_id"].isin(wanted)]
view = view.sort_values("score", ascending=False)

total = len(view)
strong = int((view["score"] >= 75).sum()) if total else 0
closing_7d = int(pd.to_numeric(view["days_remaining"], errors="coerce").between(0, 7).sum()) if total else 0
m = st.columns(3)
m[0].metric("Active jobs", total)
m[1].metric("Strong ≥75", strong)
m[2].metric("Closing ≤7d", closing_7d)

tab_jobs, tab_shortlist, tab_settings = st.tabs(["All Jobs", "Shortlist & Guidance", "Settings"])

with tab_jobs:
    st.subheader(f"Jobs ({total})")
    if view.empty:
        st.markdown(
            "**Nothing to show yet.**\n\n"
            "1. The daily GitHub Action refreshes this feed at 05:15 UTC — it needs "
            "`RELIEFWEB_APPNAME` set (free at apidoc.reliefweb.int) to collect live jobs.\n"
            "2. Or paste a vacancy under **Settings → Analyse a vacancy** right now.\n"
            "3. See **Settings → Collection report** for what the last run did.")
    else:
        table = view[FEED_COLUMNS_FOR_TABLE].copy()
        table["score_category"] = table["score_category"].map(lambda c: CATEGORY_LABEL.get(str(c), ("", str(c)))[1])
        st.dataframe(table, hide_index=True, width="stretch")
        st.markdown("#### Details")
        for i, (_, row) in enumerate(view.iterrows()):
            render_job_card(row, statuses, f"job{i}")

with tab_shortlist:
    saved_ids = {jid for jid, s in statuses.items() if s.get("saved")}
    applied_ids = {jid for jid, s in statuses.items() if s.get("applied")}
    chosen = df[df["job_id"].isin(saved_ids | applied_ids)].sort_values("score", ascending=False) if not df.empty else pd.DataFrame()
    st.subheader(f"Shortlist ({len(chosen)} saved/applied)")
    if chosen.empty:
        st.markdown("Tick **Saved** or **Applied** on any job card. "
                    "Export the shortlist below for your application tracker.")
    else:
        for i, (_, row) in enumerate(chosen.iterrows()):
            render_job_card(row, statuses, f"short{i}")
    st.markdown("#### Application guidance")
    st.markdown(
        "- Tailor the CV to each job-family's keywords; mirror the employer's wording.\n"
        "- For EU/IPA and UN roles, lead with **results**: budget size, contracts managed, on-time delivery.\n"
        "- FIDIC awareness, bills of quantities and handover/defects documentation are strong differentiators.\n"
        "- For C1-French postings your French level is not confirmed — flag uncertainty instead of claiming it.")
    st.markdown("#### Export")
    dl1, dl2 = st.columns(2)
    dl1.download_button("⬇ Filtered results (CSV)", dataframe_to_csv_bytes(view),
                        file_name="job_radar_filtered.csv", mime="text/csv", width="stretch")
    dl2.download_button("⬇ Shortlist (Markdown)", shortlist_markdown(view, cfg).encode("utf-8"),
                        file_name="job_radar_shortlist.md", mime="text/markdown", width="stretch")
    st.markdown("#### Statuses (saved / applied)")
    st.download_button("⬇ Download statuses (CSV)", statuses_to_csv_bytes(statuses, load_feed()),
                       file_name="job_statuses.csv", mime="text/csv", width="content")
    restored_file = st.file_uploader("Restore statuses from CSV", type=["csv"], key="status_restore")
    if restored_file is not None:
        try:
            restored = pd.read_csv(restored_file)
            n = 0
            for _, row in restored.iterrows():
                jid = str(row.get("job_id", "")).strip()
                if not jid:
                    continue
                st.session_state["statuses"][jid] = {
                    "saved": str(row.get("saved", "")).strip().lower() in ("1", "true", "yes"),
                    "applied": str(row.get("applied", "")).strip().lower() in ("1", "true", "yes"),
                    "rejected": str(row.get("rejected", "")).strip().lower() in ("1", "true", "yes"),
                    "notes": str(row.get("notes", "") or ""),
                }
                save_status(jid, st.session_state["statuses"][jid]["saved"],
                            st.session_state["statuses"][jid]["applied"],
                            st.session_state["statuses"][jid]["rejected"],
                            st.session_state["statuses"][jid]["notes"])
                n += 1
            st.success(f"Restored {n} statuses.")
        except Exception:
            st.error("That file could not be parsed. Expected columns: job_id, saved, applied, rejected, notes.")

with tab_settings:
    st.subheader("Settings & diagnostics")
    feed_desc = "present" if FEED_PATH.exists() else "missing"
    st.markdown(
        f"- **Feed:** `{FEED_PATH.name}` — {feed_desc}, {len(df)} records\n"
        f"- **RELIEFWEB_APPNAME:** set it as a GitHub Actions secret (never shown here)\n"
        "- **How to collect:** add your approved appname to the repo secrets, or add ATS boards to `config.yaml`")
    st.markdown("#### Collection report")
    st.json(collection_status or {"info": "No collection metadata yet — the daily Action writes it."})
    st.markdown("#### Scoring weights")
    weights = cfg.get("scoring", {}).get("weights", {})
    total_w = sum(weights.values())
    st.json(weights)
    st.caption(f"Weights sum to {total_w} " + ("✓" if total_w == 100 else "— fix config.yaml!"))
    st.markdown("#### Analyse a vacancy manually")
    with st.form("manual_form"):
        m_title = st.text_input("Job title *")
        m_org = st.text_input("Organization *")
        m_loc = st.text_input("Location")
        m_url = st.text_input("Link (URL) *")
        m_close = st.text_input("Closing date (YYYY-MM-DD, optional)")
        m_desc = st.text_area("Job description (paste here)", height=180)
        submitted = st.form_submit_button("Score this vacancy")
    if submitted:
        if not (m_title.strip() and m_org.strip() and m_url.strip()):
            st.error("Title, organization and link are required.")
        else:
            record = score_job(
                job_from_pasted_text(title=m_title, organization=m_org, location=m_loc,
                                     url=m_url, closing_date=m_close, description=m_desc),
                cfg)
            st.markdown(explanation_markdown(record))
            st.link_button("Open vacancy", record.url)
    st.markdown("#### Manual CSV import")
    uploaded = st.file_uploader("Upload vacancies CSV (title, organization, url [+ optional location, country, remote_type, contract_type, grade, posted_date, closing_date, description, sector, job_family])",
                                type=["csv"], key="manual_csv")
    if uploaded is not None:
        imported, errors = import_manual_csv(uploaded)
        for e in errors:
            st.warning(e)
        if imported:
            for rec in imported:
                score_job(rec, cfg)
            try:
                existing = load_feed()
                records_to_csv(sorted(existing + imported, key=lambda r: -r.score))
                st.success(f"Imported {len(imported)} vacancies and rewrote the feed.")
                load_feed_cached.clear()
            except OSError:
                st.warning("Read-only filesystem — imported vacancies were scored but not persisted.")
