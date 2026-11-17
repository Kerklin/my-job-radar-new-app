# 📡 Damir Job Radar

Personal job-matching dashboard for construction / infrastructure / international-development roles.
Collects vacancies daily, normalizes and deduplicates them, scores them against your profile with a
**transparent, deterministic** model, and shows them ranked in a Streamlit dashboard.

- **No paid APIs.** No AI inference required.
- **No servers to manage.** GitHub Actions collects once a day; Streamlit Community Cloud hosts the dashboard.
- **Explainable scoring.** Every point is traceable to a component; missing facts are marked *uncertain*, never silently matched.

---

## 1. What it does

| Stage | What happens |
|---|---|
| Collect | ReliefWeb Jobs API + optional Greenhouse / Lever / Ashby company feeds (configured in `config.yaml`) + manual CSV / paste imports |
| Normalize | One `JobRecord` schema; HTML stripped; UTC dates |
| Clean | Expired vacancies removed; duplicates removed (canonical URL, then fuzzy fingerprint) |
| Score | 100-point deterministic score with mandatory-requirement checks |
| Show | Streamlit dashboard with filters, cards, exports |
| Automate | GitHub Actions runs daily at 05:15 UTC and commits `data/latest_jobs.csv` |

## 2. Repository layout

```
damir-job-radar/
├── streamlit_app.py        # dashboard (entrypoint)
├── collector.py            # CLI: collect -> dedupe -> score -> CSV
├── sources.py              # ReliefWeb / Greenhouse / Lever / Ashby / manual adapters
├── scoring.py              # deterministic 100-point scoring engine
├── database.py             # JobRecord, CSV feed, statuses (SQLite)
├── config.yaml             # candidate profile, weights, keywords, sources
├── requirements.txt
├── tests/test_core.py
└── .github/workflows/collect-jobs.yml
```

Generated at runtime (not hand-edited): `data/latest_jobs.csv`, `data/collection_status.json`.

## 3. Quick start (local, Windows PowerShell)

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
pytest -q                      # should pass
python collector.py --sample   # writes demo feed
streamlit run streamlit_app.py
```

Open http://localhost:8501

## 4. Daily automation via GitHub Actions

1. Push the repository to GitHub (see §7).
2. Add your ReliefWeb appname as a secret:
   - Repo → **Settings → Secrets and variables → Actions → New repository secret**
   - Name: `RELIEFWEB_APPNAME`  Value: your approved appname
   - Get an appname from https://apidoc.reliefweb.int/ (requests need a pre-approved appname).
3. The workflow runs daily at **05:15 UTC**. To run it manually:
   - Repo → **Actions → Collect jobs daily → Run workflow**

The workflow runs the test suite first, then the collector, then commits
`data/latest_jobs.csv` **only if it changed**. If at least one source was attempted and *all* of them
fail, the previous feed is kept and the run fails loudly instead of publishing an empty feed.
If no source is configured at all (no `RELIEFWEB_APPNAME` secret and no ATS boards in
`config.yaml`), the run stays green with a prominent `no_sources_configured` warning in the job
summary instead of a false outage alarm — the feed is still preserved untouched.

> ReliefWeb requires an approved `appname`. Without the secret, ReliefWeb is skipped and you
> still get any configured ATS feeds + the sample dataset.

## 5. Configuring your own job sources

Edit `config.yaml` → `sources:`. Board identifiers come **only** from this file.

```yaml
sources:
  reliefweb: { enabled: true, page_size: 20, max_pages: 3, max_records: 60 }
  greenhouse:
    enabled: true
    boards:
      - { name: "Acme Construction", board: "acme", content: true }   # boards-api.greenhouse.io/v1/boards/acme/jobs
  lever:
    enabled: true
    boards:
      - { name: "Example NGO", company: "example", eu: false }        # api.lever.co (or eu.api.lever.co)
  ashby:
    enabled: true
    boards:
      - { name: "Example Consultancy", org: "example", includeCompensation: true }
```

Find board IDs from the public job-board URL of each employer (e.g. `boards.greenhouse.io/<board>`).
These feeds are **company-specific**, not global aggregators — add exactly the employers you care about.

## 6. Scoring model (100 points)

| Component | Max |
|---|---|
| Job family / title match | 20 |
| Technical competencies | 25 |
| Sector & organization match | 15 |
| Experience / seniority | 15 |
| Education & certification | 10 |
| Location & work authorization | 5 |
| Language | 5 |
| Contract / travel preference | 5 |

Categories: **85–100** Excellent · **75–84** Strong · **65–74** Good · **50–64** Possible · **<50** Low · **Disqualified** if a mandatory requirement is demonstrably unmet.

Mandatory checks: nationality restrictions, native-language requirements, explicit C1/C2 levels (French/Italian marked *uncertain* — levels not confirmed), named licences (CEng/PE/RICS → *uncertain*), unrelated-discipline degrees (penalty). Missing descriptions cap the score and force *low confidence*. Every explanation lists strengths, gaps, uncertainties, penalties and mandatory findings — and every such finding is visible in the UI.

## 7. Publish to GitHub (drag & drop or command line)

```powershell
git add .
git commit -m "Build initial Damir Job Radar"
git branch -M main
git remote add origin https://github.com/<you>/damir-job-radar.git
git push -u origin main
```

Or upload the folder via GitHub's **Add file → Upload files**.

## 8. Deploy to Streamlit Community Cloud

1. Push to GitHub (§7).
2. Go to https://share.streamlit.io → **Create app** → select your repo, branch `main`.
3. Main file path: `streamlit_app.py`
4. **Advanced settings → Python version: 3.12**
5. Optional secrets (for direct ReliefWeb reads from the live app):
   ```toml
   RELIEFWEB_APPNAME = "your-approved-appname"
   ```
   (The cleaner setup: GitHub Actions collects; Streamlit only reads `data/latest_jobs.csv`.)
6. **Deploy.** Subsequent pushes to `main` redeploy automatically.

## 9. Troubleshooting

| Symptom | Fix |
|---|---|
| Dashboard shows "Feed last updated: never" | Run `python collector.py --sample`, or check **Actions → Collect jobs daily** logs; ensure the workflow has `contents: write` permission. |
| "All sources failed" banner | ReliefWeb `RELIEFWEB_APPNAME` missing/not approved; check the Action log. The previous feed is preserved automatically. |
| Green run but a `no_sources_configured` warning | Nothing was collected by design: no `RELIEFWEB_APPNAME` secret and no ATS boards in `config.yaml`. Set the secret (§4) or add boards (§5); the old feed stays live meanwhile. |
| ReliefWeb skipped locally | `setx RELIEFWEB_APPNAME "your-appname"` then restart the terminal (or use a `.env` loader of your choice — secrets are never committed). |
| Empty Low-scoring list | Most jobs fail the penalties/caps. Remove `--sample`, add ATS boards, or lower *Minimum match score*. |
| `pip install` fails | Use the venv from §3; upgrade pip; ensure Python 3.12. |
| Statuses lost after redeploy | Expected on Streamlit Cloud (ephemeral FS). Use **Shortlist & Guidance → Export status CSV**, then re-upload after redeploys. |
| Workflow not committing | Check **Actions** permissions: Settings → Actions → General → Workflow permissions → **Read and write permissions**. |
| New feed not visible | Sidebar → **Reload feed from disk**. |

## 10. Known limitations

- Closing dates are often missing from ATS feeds (Greenhouse/Lever/Ashby expose none) — they are kept, flagged as *uncertain*.
- ReliefWeb returns at most `max_pages × page_size` records; raise the caps in `config.yaml` if needed.
- Scores are deterministic keyword/rule-based, not semantic. They steer triage, not decisions.
- Sample data is clearly labelled (`source = "sample"`) and is never mixed into a live feed run.

## 11. Roadmap (v2 ideas, not v1)

- Supabase/Render Postgres for cross-device saved/applied statuses.
- Optional Claude API layer for deeper per-job CV analysis (behind a flag; off by default).
- Workday connector (tenant-specific; fragile).
- Devex / UN Careers feeds if official APIs appear.
- Email digest of new strong matches.
