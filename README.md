# Zora

Zora is a precision-first outreach engine for job search workflows. It discovers role-matched decision makers, validates work emails from public sources, drafts personalized outreach, and exports clean data for action.

## Highlights

- Precision-first candidate filtering (fail-closed on weak evidence)
- Multi-source discovery (Google/DDG + public web + GitHub)
- Optional SMTP validation fallback (no email sent)
- Personalized draft generation via NVIDIA-hosted LLM
- CLI + FastAPI web app with live pipeline progress
- CSV export for downstream workflows

## Interface

![Zora Web Interface](frontend_screenshot.png)

## Pipeline Overview

1. Target identification
: Finds LinkedIn profiles and applies strict role/company verification.

2. Email discovery and validation
: Searches public web, company pages, and GitHub for real emails, with SMTP fallback if enabled.

3. Draft generation
: Creates concise, role-aware outreach drafts using your skill profile.

4. Export
: Writes structured rows to CSV and keeps only trusted confidence tiers.

## Accuracy Model

Zora is intentionally tuned for precision over recall.

- Profiles are rejected when evidence is weak or ambiguous.
- Founder searches include extra strict checks to reduce false positives.
- API-layer founder guardrail prevents non-founder leakage for known major companies.
- `MIN_TARGET_EVIDENCE_SCORE` controls strictness (`3` default, `4` stricter).

Important: no open-web system can guarantee universal 99% accuracy because search snippets, indexing, and platform rate limits are non-deterministic. Zora addresses this by failing closed on low-confidence candidates.

## Benchmark Proof

This repository includes reproducible benchmark scripts:

- `benchmark_targets.py`
- `benchmark_role_suite.py`

Run:

```bash
python benchmark_role_suite.py
```

Recent 10-company benchmark summary (role: Recruiter) showed consistent role-relevant outputs across major companies (Google, Microsoft, Amazon, Meta, Apple, Netflix, NVIDIA, OpenAI, Scale AI, Databricks), with 3-5 high-confidence targets per company depending on public evidence.

For strict founder queries, output is intentionally conservative (often fewer results) to avoid presenting incorrect people.

## Quick Start

### Prerequisites

- Python 3.10+
- NVIDIA API key

### Install

```bash
git clone <repo_url>
cd "Job Search Agent"
python -m venv .venv

# macOS / Linux
source .venv/bin/activate

# Windows PowerShell
.venv\Scripts\Activate.ps1

pip install -r requirements.txt
cp .env.example .env
```

Set `NVIDIA_API_KEY` in `.env`.

### Run Web App

```bash
python server.py
```

Default URL: `http://localhost:8000`

### Run CLI

```bash
python main.py -c "Scale AI" -t "Engineering Manager" -d "scale.com"
```

## Configuration

Key environment variables:

- `SEARCH_BACKEND=ddg|auto|google`
- `GOOGLE_COOLDOWN_SECONDS=1800`
- `MIN_TARGET_EVIDENCE_SCORE=3`
- `DISCOVERY_TIMEOUT_SECONDS=12`
- `SMTP_VALIDATION_TIMEOUT_SECONDS=15`
- `VALIDATION_CONCURRENCY=3`
- `ENABLE_SMTP_VALIDATION=false` (recommended on cloud)

Recommended production profile (Render):

- `SEARCH_BACKEND=ddg`
- `MIN_TARGET_EVIDENCE_SCORE=4`
- `ENABLE_SMTP_VALIDATION=false`
- `VALIDATION_CONCURRENCY=3` to `5`

## Deployment

### Render (Backend)

Use included:

- `render.yaml`
- `runtime.txt`
- `start.sh`

Health check path:

- `/api/health`

### Vercel (Frontend) + Render (Backend)

If frontend and backend are split across platforms:

- `vercel.json` rewrites `/api/*` to the Render backend.
- Frontend also includes direct API-base fallback in `frontend/app.js`.

## Output

- Primary export: `outreach_results.csv`
- Sample export: `outreach_results_sample.csv`

## Tech Stack

- Python, FastAPI
- ddgs, googlesearch-python
- dnspython, smtplib
- OpenAI SDK with NVIDIA endpoint
- Vanilla HTML/CSS/JS frontend
