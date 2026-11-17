# CLAUDE.md - Damir Job Radar

## What this is
Personal job-matching app for Damir Hadžić (Construction Engineer, UNICEF BiH, Sarajevo).
Collects vacancies, scores them deterministically against his profile, shows them ranked in Streamlit.

## Architecture (deliberately minimal - 2 Python files)
- `radar.py` (engine + CLI) runs daily via GitHub Actions → writes `data/latest_jobs.csv` → commits it.
  Sections: Storage (JobRecord, CSV feed IO, Formula-injection-safe export, SQLite statuses) →
  Scoring (100-point deterministic scorer) → Sources (ReliefWeb API **v2**, Greenhouse, Lever, Ashby,
  manual CSV/paste) → CLI (`python radar.py [--sample]`).
- `streamlit_app.py` reads that CSV. No API server, no DB server, no Docker.
- ReliefWeb note: the **v1 API was decommissioned**; the v2 endpoint is `POST https://api.reliefweb.int/v2/jobs`
  and requires an **approved appname** (free, https://apidoc.reliefweb.int/). Jobs cannot be collected
  without one - nothing else in the BiH/UN ecosystem exposes a public feed.
- `config.yaml` = candidate profile, weights, keywords, sources (incl. `reliefweb.query`).

## Commands
```bash
python -m venv .venv && .venv\Scripts\Activate.ps1
pip install -r requirements.txt
pytest -q
python radar.py --sample     # demo feed (sample rows are labelled and hidden by default)
python radar.py              # live collection (needs RELIEFWEB_APPNAME)
streamlit run streamlit_app.py
```

## Hard rules for future edits
1. **Explanations are mandatory.** Every score component must produce a human-readable reason. Never output a bare number.
2. **Missing = uncertain.** A field not present in the posting is never scored as matched. Use the `uncertainties` list and `confidence`.
3. **Mandatory unmet ⇒ disqualified.** Language native-requirements, nationality restrictions, demonstrably unmet degrees. Licences are *uncertain*, not auto-disqualified.
4. **No secrets in the repo.** `RELIEFWEB_APPNAME` comes from env / Streamlit secrets / GitHub Actions secrets only.
5. **A source failure never kills the run.** Each adapter catches its own exceptions; `collect_all` isolates crashes. ReliefWeb query rejection (4xx) auto-retries without the query - a bad query string can never fail the run.
6. **All-sources-failed must not overwrite the last valid feed** (radar exits 3 when at least one source was attempted and everything failed; when NOTHING was configured/attempted, exits 0 with a `no_sources_configured` status + loud warning instead — feed preserved either way; workflow keeps going to the summary step).
7. **Safe CSV export.** Every user-controlled string passes `csv_safe` (formula injection). No `unsafe_allow_html` anywhere.
8. **Expired jobs are removed** before scoring/sorting. Missing closing date = kept + flagged uncertain.
9. **Dedup**: canonical URL first (scheme/host/www/query/fragment/slash stripped), then RapidFuzz `token_set_ratio ≥ 88` on `org + title + location`.
10. **Keyword stuffing resistance**: distinct keywords each count once; title hits (3.0) outrank description hits (1.5); job-family (20) outranks generic terms; repeated keywords carry no extra weight.
11. **Scoring weights must sum to 100** — guarded by `test_scoring_weights_sum_to_100`.
12. Keep the file count minimal: `radar.py` + `streamlit_app.py` + `tests/test_core.py`. Do not add FastAPI/React/Docker/Redis/Celery/Postgres/LangChain/microservices.

## Data verification
- Tests: `pytest -q` (fixtures cover: construction/PM/heritage/WASH roles score high; software role scores low; nationality & native-language disqualify; CEng → uncertain; missing description caps score; weights sum 100).
- Sample mode (`--sample`) contains: 1 expired record (filtered), 1 unrelated software role, 1 graduate scheme, 1 UNICEF WASH, 1 UNOPS construction, EU/IPA PM, heritage, World Bank TA, 2 roles with French uncertainty.
- The committed feed ships EMPTY of sample rows; the dashboard hides sample rows by default behind the sidebar toggle.

## Deployment
- Streamlit Community Cloud, entrypoint `streamlit_app.py`, Python 3.12, branch `main`.
- GitHub Actions: `.github/workflows/collect-jobs.yml` — cron `15 5 * * *`, `workflow_dispatch`, `contents: write`, concurrency-guarded, commits only on diff, Node 24 actions (`checkout@v7`, `setup-python@v7`), `ubuntu-24.04`.
