"""Damir Job Radar - Streamlit dashboard.

Reads the repository-managed feed at data/latest_jobs.csv (produced daily by
GitHub Actions). Personal statuses are kept in the browser session and, when
the local filesystem is writable, in data/jobs.db. Statuses can always be
downloaded/uploaded as CSV.

No secrets are rendered. Missing configuration shows actionable guidance,
never a stack trace. Description text is never rendered as raw HTML.
"""

from __future__ import annotations

import io
import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st
import yaml

from database import (FEED_PATH, load_feed, read_collection_status,
                      save_status, load_statuses, statuses_to_csv_bytes,
                      dataframe_to_csv_bytes, records_to_csv)
from scoring import score_job, explanation_markdown
from sources import import_manual_csv, job_from_pasted_text

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


# ---------------------------------------------------------------------------
# Loading helpers
# ---------------------------------------------------------------------------

def load_config() -> dict:
    try:
        with open(REPO_CONFIG, encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except Exception:
        return {}


def records_to_dataframe(records) -> pd.DataFrame:
    if not records:
        return pd.DataFrame(columns=[
            "job_id", "title", "organization", "location", "country", "remote_type",
            "contract_type", "grade", "posted_date", "closing_date", "days_remaining",
            "description", "url", "source", "sector", "job_family", "score",
            "score_category", "fit_status", "confidence", "matched_keywords",
            "strengths", "gaps", "uncertainties", "penalties", "mandatory_findings",
            "score_breakdown"])
    rows = []
    for r in records:
        row = dict(r.__dict__)
        for key in ("matched_keywords", "strengths", "gaps", "uncertainties", "penalties", "mandatory_findings"):
            row[key] = "; ".join(str(x) if not isinstance(x, dict) else ", ".join(f"{k}: {v}" for k, v in x.items()) for x in (row.get(key) or []))
        row["score_breakdown"] = json.dumps(row.get("score_breakdown") or {}, ensure_ascii=False)
        rows.append(row)
    return pd.DataFrame(rows)


@st.cache_data(ttl=300, show_spinner=False)
def get_feed_df() -> tuple[pd.DataFrame, str]:
    records = load_feed(FEED_PATH)
    df = records_to_dataframe(records)
    updated = ""
    try:
        updated = datetime.fromtimestamp(FEED_PATH.stat().st_mtime).strftime("%Y-%m-%d %H:%M UTC")
    except OSError:
        pass
    if not updated and not df.empty:
        updated = "unknown"
    return df, updated


def apply_filters(df: pd.DataFrame, *, search, min_score, families, orgs, countries,
                  remote_types, sector_choice, closing_from, closing_to,
                  only_saved, only_applied, only_new, new_days, statuses, known_ids) -> pd.DataFrame:
    out = df.copy()
    if search:
        q = search.lower()
        mask = (out["title"].fillna("").str.lower().str.contains(q, na=False) |
                out["organization"].fillna("").str.lower().str.contains(q, na=False) |
                out["description"].fillna("").str.lower().str.contains(q, na=False) |
                out["location"].fillna("").str.lower().str.contains(q, na=False))
        out = out[mask]
    out = out[out["score"].fillna(0) >= min_score]
    if families:
        out = out[out["job_family"].isin(families)]
    if orgs:
        out = out[out["organization"].isin(orgs)]
    if countries:
        out = out[out["country"].fillna("").isin(countries)]
    if remote_types:
        out = out[out["remote_type"].fillna("").isin(remote_types) | (out["remote_type"].fillna("") == "")]
    if sector_choice == "UN / international development":
        out = out[out["sector"].fillna("").str.contains("un|united nations|ebrd|world bank|european|development|humanitarian|international", case=False, na=False)]
    elif sector_choice == "Private sector":
        out = out[out["sector"].fillna("").str.contains("private|company|consultan|construction group", case=False, na=False)]
    if closing_from:
        out = out[out["closing_date"].fillna("9999-12-31") >= closing_from.isoformat()]
    if closing_to:
        out = out[out["closing_date"].fillna("0000-01-01") <= closing_to.isoformat()]
    if only_saved:
        saved_ids = {jid for jid, st_ in statuses.items() if st_.get("saved")}
        out = out[out["job_id"].isin(saved_ids)]
    if only_applied:
        applied_ids = {jid for jid, st_ in statuses.items() if st_.get("applied")}
        out = out[out["job_id"].isin(applied_ids)]
    if only_new:
        cutoff = (date.today() - timedelta(days=new_days)).isoformat()
        out = out[out["posted_date"].fillna("") >= cutoff]
    return out


def score_badge(category: str) -> str:
    dot, label = CATEGORY_LABEL.get(str(category), ("⚪", str(category or "low")))
    return f"{dot} {label}"


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


def render_job_card(row: pd.Series, statuses: dict, idx_prefix: str) -> None:
    job_id = str(row.get("job_id", ""))
    st_kwargs = statuses.get(job_id, {})
    dot, label = CATEGORY_LABEL.get(str(row.get("score_category", "low")), ("⚪", "Low priority"))
    with st.expander(
        f"{dot} **{row.get('score', 0)}** · {row.get('title', '')} — {row.get('organization', '')}"
        f"  ·  {days_remaining_label(row.get('days_remaining'))} (closes {row.get('closing_date') or 'n/a'})",
        expanded=False,
    ):
        meta_cols = st.columns(4)
        meta_cols[0].markdown(f"**Organization**\n\n{row.get('organization', '')}")
        meta_cols[1].markdown(f"**Location**\n\n{row.get('location', '') or 'n/a'}")
        meta_cols[2].markdown(f"**Contract / grade**\n\n{row.get('contract_type', '') or 'n/a'} · {row.get('grade', '') or 'n/a'}")
        meta_cols[3].markdown(f"**Source**\n\n{row.get('source', '')}")

        st.markdown(f"**Fit:** {score_badge(row.get('score_category'))} · **Status:** "
                    f"{FIT_STATUS_LABEL.get(str(row.get('fit_status')), row.get('fit_status', ''))} · "
                    f"**Confidence:** {row.get('confidence', 'medium')}")
        if row.get("job_family"):
            st.markdown(f"**Job family:** {row.get('job_family')}")

        if row.get("url"):
            st.link_button("Apply / view vacancy", str(row["url"]), width="content")

        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**Matching strengths**")
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
            kw = str(row.get("matched_keywords", "")).strip()
            st.caption(kw if kw else "—")

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

        # Personal status controls
        st.markdown("**Your status**")
        b1, b2, b3, b4 = st.columns(4)
        saved = b1.checkbox("Saved", value=bool(st_kwargs.get("saved", False)), key=f"save_{idx_prefix}_{job_id}")
        applied = b2.checkbox("Applied", value=bool(st_kwargs.get("applied", False)), key=f"apply_{idx_prefix}_{job_id}")
        rejected = b3.checkbox("Rejected", value=bool(st_kwargs.get("rejected", False)), key=f"reject_{idx_prefix}_{job_id}")
        notes = b4.text_input("Notes", value=str(st_kwargs.get("notes", "")), key=f"notes_{idx_prefix}_{job_id}",
                              label_visibility="collapsed", placeholder="Notes…")
        new_status = {"saved": saved, "applied": applied, "rejected": rejected, "notes": notes}
        if new_status != {"saved": bool(st_kwargs.get("saved", False)),
                          "applied": bool(st_kwargs.get("applied", False)),
                          "rejected": bool(st_kwargs.get("rejected", False)),
                          "notes": str(st_kwargs.get("notes", ""))}:
            statuses[job_id] = new_status
            st.session_state["statuses"] = statuses
            save_status(job_id, saved, applied, rejected, notes)


def shortlist_markdown(df: pd.DataFrame, cfg: dict, top_n: int = 10) -> str:
    cand = cfg.get("candidate", {})
    strong = df[df["score"].fillna(0) >= 65].sort_values("score", ascending=False).head(top_n)
    lines = [
        f"# Job Shortlist - {cand.get('name', 'Candidate')}",
        f"Generated: {date.today().isoformat()} · {len(strong)} roles scoring ≥ 65",
        "",
    ]
    for _, row in strong.iterrows():
        lines += [
            f"## {row.get('title')} — {row.get('organization')}",
            f"- Score: **{row.get('score')}/100** ({row.get('score_category')}) · Status: {row.get('fit_status')} · Confidence: {row.get('confidence')}",
            f"- Location: {row.get('location') or 'n/a'} · Contract: {row.get('contract_type') or 'n/a'} · Closes: {row.get('closing_date') or 'n/a'} ({days_remaining_label(row.get('days_remaining'))})",
            f"- Source: {row.get('source')} · Link: {row.get('url')}",
            f"- Strengths: {row.get('strengths') or '—'}",
            f"- Gaps: {row.get('gaps') or 'none'}",
            f"- Uncertainties: {row.get('uncertainties') or '—'}",
            "",
        ]
    if len(strong) == 0:
        lines.append("No roles scored ≥ 65 under the current filters.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

cfg = load_config()

if "statuses" not in st.session_state:
    st.session_state["statuses"] = load_statuses()
statuses = st.session_state["statuses"]

df, feed_updated = get_feed_df()
collection_status = read_collection_status()
is_sample = bool(collection_status.get("sample_mode")) or (not df.empty and set(df["source"].unique()) == {"sample"})

st.title("📡 Damir Job Radar")
subtitle = f"Ranked construction / infrastructure / international-development vacancies · Feed last updated: {feed_updated or 'never'}"
if is_sample:
    subtitle += " · **SAMPLE DATA**"
st.caption(subtitle)

# Source / config warnings (never a stack trace)
if not collection_status:
    st.info("No collection metadata found. The GitHub Action may not have run yet, or you are running locally with `python collector.py --sample`.", icon="ℹ️")
else:
    failed = {k: v for k, v in (collection_status.get("sources") or {}).items()
              if v.get("enabled") and v.get("failed") and not v.get("collected")}
    if collection_status.get("reason") == "all_sources_failed":
        st.error("All configured sources failed during the last collection. The previous feed was preserved. Check the GitHub Actions log.", icon="🛑")
    elif failed:
        st.warning("Some sources failed in the last run: " + ", ".join(sorted(failed)) + ". Other sources were used; see the Settings tab.", icon="⚠️")

if FEED_PATH.exists() is False and df.empty:
    st.markdown(
        "### No jobs yet\n"
        "1. Add your `RELIEFWEB_APPNAME` (see README), or run `python collector.py --sample`.\n"
        "2. Upload a manual CSV in the **Settings** tab, or paste a vacancy in **Manual Analysis**.\n"
        "3. Configure ATS boards in `config.yaml` and push to GitHub — Actions collects daily at 05:15 UTC."
    )

# Summary metrics
total = len(df)
strong_80 = int((df["score"] >= 80).sum()) if total else 0
good_65 = int(((df["score"] >= 65) & (df["score"] < 80)).sum()) if total else 0
possible_50 = int(((df["score"] >= 50) & (df["score"] < 65)).sum()) if total else 0
cutoff_new = (date.today() - timedelta(days=7)).isoformat()
new_7d = int((df["posted_date"].fillna("") >= cutoff_new).sum()) if total else 0
closing_7d = int(((pd.to_numeric(df["days_remaining"], errors="coerce") >= 0) &
                  (pd.to_numeric(df["days_remaining"], errors="coerce") <= 7)).sum()) if total else 0

m = st.columns(6)
m[0].metric("Active jobs", total)
m[1].metric("Strong ≥80", strong_80)
m[2].metric("Good 65–79", good_65)
m[3].metric("Possible 50–64", possible_50)
m[4].metric("New ≤7d", new_7d)
m[5].metric("Closing ≤7d", closing_7d)

# Sidebar filters
with st.sidebar:
    st.header("Filters")
    search = st.text_input("Search text", "")
    min_score = st.slider("Minimum match score", 0, 100, 50, 5)
    families = st.multiselect("Job family", sorted({x for x in df["job_family"].fillna("") if x}) if total else [])
    orgs = st.multiselect("Organization", sorted(df["organization"].dropna().unique()) if total else [])
    countries = st.multiselect("Country", sorted({x for x in df["country"].fillna("") if x}) if total else [])
    remote_types = st.multiselect("Remote / hybrid / on-site", ["remote", "hybrid", "on-site"])
    sector_choice = st.selectbox("Sector", ["All sectors", "UN / international development", "Private sector"])
    st.markdown("**Closing-date range**")
    cd1, cd2 = st.columns(2)
    closing_from = cd1.date_input("From", value=None, format="YYYY-MM-DD")
    closing_to = cd2.date_input("To", value=None, format="YYYY-MM-DD")
    only_saved = st.checkbox("Saved only")
    only_applied = st.checkbox("Applied only")
    only_new = st.checkbox("New since last scan (7 days)")
    new_days = st.number_input("New = posted within (days)", min_value=1, max_value=90, value=7, disabled=not only_new)
    st.divider()
    if st.button("Reload feed from disk", width="stretch"):
        get_feed_df.clear()
        st.rerun()

filtered = apply_filters(
    df, search=search, min_score=min_score, families=families, orgs=orgs,
    countries=countries, remote_types=remote_types, sector_choice=sector_choice,
    closing_from=closing_from or None, closing_to=closing_to or None,
    only_saved=only_saved, only_applied=only_applied, only_new=only_new,
    new_days=int(new_days), statuses=statuses, known_ids=set(),
)

# Tabs
tab_all, tab_strong, tab_closing, tab_saved, tab_manual, tab_settings = st.tabs(
    ["All Jobs", "Strong Matches", "Closing Soon", "Shortlist & Guidance", "Manual Analysis", "Settings"])

export_cols = st.columns(2)
with tab_all:
    st.subheader(f"All jobs ({len(filtered)} of {total})")
    if filtered.empty:
        st.markdown(
            "**Nothing matches the current filters.**\n\n"
            "- Lower the *Minimum match score* in the sidebar.\n"
            "- Clear the search box or broaden the family/organization filters.\n"
            "- If the feed is empty, run `python collector.py --sample` locally or check GitHub Actions."
        )
    else:
        table = filtered[["title", "organization", "location", "score", "score_category",
                          "fit_status", "closing_date", "days_remaining", "source"]].copy()
        table["score_category"] = table["score_category"].map(lambda c: CATEGORY_LABEL.get(str(c), ("", str(c)))[1])
        table = table.sort_values("score", ascending=False)
        st.dataframe(table, hide_index=True, width="stretch")
        st.markdown("#### Details")
        for i, (_, row) in enumerate(filtered.sort_values("score", ascending=False).iterrows()):
            render_job_card(row, statuses, f"all{i}")

with tab_strong:
    strong_df = filtered[filtered["score"] >= 75].sort_values("score", ascending=False)
    st.subheader(f"Strong matches (≥75): {len(strong_df)}")
    if strong_df.empty:
        st.info("No strong matches under the current filters. Try lowering the minimum score or clearing filters.", icon="💡")
    else:
        for i, (_, row) in enumerate(strong_df.iterrows()):
            render_job_card(row, statuses, f"strong{i}")

with tab_closing:
    closing_df = filtered[pd.to_numeric(filtered["days_remaining"], errors="coerce").between(0, 7)].sort_values("days_remaining")
    st.subheader(f"Closing within 7 days: {len(closing_df)}")
    if closing_df.empty:
        st.info("Nothing closes in the next 7 days under the current filters.", icon="📅")
    else:
        for i, (_, row) in enumerate(closing_df.iterrows()):
            render_job_card(row, statuses, f"closing{i}")

with tab_saved:
    saved_ids = {jid for jid, s in statuses.items() if s.get("saved")}
    applied_ids = {jid for jid, s in statuses.items() if s.get("applied")}
    saved_df = df[df["job_id"].isin(saved_ids)].sort_values("score", ascending=False) if saved_ids else df.iloc[0:0]
    applied_df = df[df["job_id"].isin(applied_ids)].sort_values("score", ascending=False) if applied_ids else df.iloc[0:0]
    st.subheader(f"Saved ({len(saved_df)}) · Applied ({len(applied_df)})")
    if saved_df.empty and applied_df.empty:
        st.markdown(
            "**Nothing shortlisted yet.**\n\n"
            "Open a vacancy card and tick **Saved** or **Applied**. "
            "Export the shortlist as CSV or Markdown below for your application tracker."
        )
    else:
        for i, (_, row) in enumerate(pd.concat([saved_df, applied_df]).drop_duplicates(subset=["job_id"]).iterrows()):
            render_job_card(row, statuses, f"saved{i}")
    st.markdown("#### Application guidance")
    st.markdown(
        "- Tailor the CV to the job-family keywords listed on each card; mirror the employer's wording.\n"
        "- For EU/IPA and UN roles, lead with **results** (budget size, number of contracts managed, % delivered on time).\n"
        "- FIDIC awareness, bills of quantities and handover/defects documentation are strong differentiators — state them explicitly.\n"
        "- Note field-travel and 4WD readiness for field roles.\n"
        "- Never hand in English forms for a post requiring C1 French unless your French level is genuinely B2+ — flag uncertainty instead."
    )
    st.markdown("#### Export status CSV")
    st.download_button("Download statuses (CSV)", statuses_to_csv_bytes(statuses, load_feed()),
                       file_name="job_statuses.csv", mime="text/csv", width="content")
    uploaded_status = st.file_uploader("Restore statuses from CSV", type=["csv"], key="status_restore")
    if uploaded_status is not None:
        try:
            restored = pd.read_csv(uploaded_status)
            n = 0
            for _, row in restored.iterrows():
                jid = str(row.get("job_id", "")).strip()
                if not jid:
                    continue
                st.session_state["statuses"][jid] = {
                    "saved": str(row.get("saved", "")).strip() in ("1", "True", "true", "yes"),
                    "applied": str(row.get("applied", "")).strip() in ("1", "True", "true", "yes"),
                    "rejected": str(row.get("rejected", "")).strip() in ("1", "True", "true", "yes"),
                    "notes": str(row.get("notes", "") or ""),
                }
                save_status(jid, st.session_state["statuses"][jid]["saved"],
                            st.session_state["statuses"][jid]["applied"],
                            st.session_state["statuses"][jid]["rejected"],
                            st.session_state["statuses"][jid]["notes"])
                n += 1
            st.success(f"Restored {n} statuses.")
        except Exception:
            st.error("That file could not be parsed as a statuses CSV. Expected columns: job_id, saved, applied, rejected, notes.")

with tab_manual:
    st.subheader("Analyse a vacancy manually")
    with st.form("manual_form"):
        m_title = st.text_input("Job title *")
        m_org = st.text_input("Organization *")
        m_loc = st.text_input("Location")
        m_url = st.text_input("Link (URL) *")
        m_close = st.text_input("Closing date (YYYY-MM-DD, optional)")
        m_desc = st.text_area("Job description (paste here)", height=220)
        submitted = st.form_submit_button("Score this vacancy")
    if submitted:
        if not (m_title.strip() and m_org.strip() and m_url.strip()):
            st.error("Title, organization and link are required.")
        else:
            cfg_loaded = load_config()
            record = job_from_pasted_text(title=m_title, organization=m_org, location=m_loc,
                                          url=m_url, closing_date=m_close, description=m_desc)
            record = score_job(record, cfg_loaded)
            st.markdown(explanation_markdown(record))
            st.link_button("Open vacancy", record.url)
    st.markdown("#### Copy-ready analysis prompt for CV tailoring")
    st.code(
        "You are an expert CV writer for UN and EU infrastructure roles. "
        "Job title: <paste>. Organization: <paste>. Job description: <paste>. "
        "Candidate: Damir Hadžić — Construction Engineer, PhD in Architectural Engineering, MSc in Built Heritage Conservation, "
        "PMP certified, BiH professional exam, Bosnian/Croatian/Serbian native, English professional, experience with UNICEF, "
        "EU/IPA-funded projects, construction supervision, contract administration, FIDIC, tendering, WASH, infrastructure "
        "rehabilitation, built heritage conservation, procurement. "
        "Produce: (1) a fit assessment with honest gaps, (2) tailored CV bullet points with quantified results, "
        "(3) a tailored cover letter, (4) key questions to expect in interview. Never invent credentials.",
        language="text",
    )

with tab_settings:
    st.subheader("Settings & diagnostics")
    st.markdown(
        f"- **Feed file:** `{FEED_PATH.name}` — {'present' if FEED_PATH.exists() else 'missing'}\n"
        f"- **Records in feed:** {total}\n"
        f"- **RELIEFWEB_APPNAME:** set it as a GitHub Actions / Streamlit secret — its value is never shown here"
    )
    st.markdown("#### Last collection report")
    st.json(collection_status or {"info": "No collection metadata yet. Run `python collector.py --sample` or wait for the daily GitHub Action."})
    st.markdown("#### Manual CSV import")
    uploaded = st.file_uploader("Upload vacancies CSV (columns: title, organization, url [+ optional location, country, contract_type, grade, posted_date, closing_date, description, remote_type, sector, job_family])",
                                type=["csv"], key="manual_csv")
    if uploaded is not None:
        cfg_loaded = load_config()
        records, errors = import_manual_csv(uploaded)
        for e in errors:
            st.warning(e)
        if records:
            for rec in records:
                score_job(rec, cfg_loaded)
            try:
                existing = load_feed()
                records_to_csv(sorted(existing + records, key=lambda r: -r.score))
                st.success(f"Imported {len(records)} vacancies and rewrote the feed. Reload the feed from the sidebar.")
                get_feed_df.clear()
            except OSError:
                st.warning("Could not write the feed (read-only filesystem). Imported vacancies were scored but not persisted.")
    st.markdown("#### Export")
    st.download_button("Download filtered results (CSV)", dataframe_to_csv_bytes(filtered),
                       file_name="job_radar_filtered.csv", mime="text/csv", width="stretch")
    st.download_button("Download Markdown shortlist (≥65)", shortlist_markdown(filtered, cfg).encode("utf-8"),
                       file_name="job_radar_shortlist.md", mime="text/markdown", width="stretch")
    st.download_button("Download filtered results (JSON)", filtered.to_json(orient="records").encode("utf-8"),
                       file_name="job_radar_filtered.json", mime="application/json", width="stretch")
    st.markdown("#### Scoring weights (from config.yaml)")
    st.json(cfg.get("scoring", {}).get("weights", {}))
    total_w = sum(cfg.get("scoring", {}).get("weights", {}).values())
    if total_w != 100:
        st.error(f"Scoring weights sum to {total_w}, not 100. Fix config.yaml.")
    else:
        st.caption("Scoring weights sum to 100 ✓")
    st.markdown("#### Candidate profile (from config.yaml)")
    cand = cfg.get("candidate", {})
    st.markdown(
        f"- **Name:** {cand.get('name')}\n"
        f"- **Location:** {cand.get('location')}\n"
        f"- **Education:** {', '.join(cand.get('education', []))}\n"
        f"- **Certifications:** {', '.join(cand.get('certifications', []))}\n"
        f"- **Native languages:** Bosnian/Croatian/Serbian")

# Footer export buttons visible on main tabs too
st.divider()
foot = st.columns(2)
foot[0].download_button("⬇ Download filtered results (CSV)", dataframe_to_csv_bytes(filtered),
                        file_name="job_radar_filtered.csv", mime="text/csv", width="stretch")
foot[1].download_button("⬇ Download Markdown shortlist", shortlist_markdown(filtered, cfg).encode("utf-8"),
                        file_name="job_radar_shortlist.md", mime="text/markdown", width="stretch")
