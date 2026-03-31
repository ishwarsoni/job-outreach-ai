# Zora

A data-driven outreach pipeline that prioritizes accuracy over volume by discovering and validating real emails from public sources.

## Why This Exists

Cold outreach is a numbers game, but the numbers only work if the data is accurate and the message is highly relevant. Manual outreach is slow, while automated tools often rely on generic email permutations and send low-effort messages that get ignored. 

This project was built to solve the data reliability problem in job searching. It automates finding the right engineering leaders, filters based on role and company match, discovers their actual work emails using public data, and drafts grounded, specific outreach.

## Design Decisions & Tradeoffs

To prioritize data accuracy over high-volume spam, this system implements a multi-source data pipeline and strict filtering mechanisms.

* **Multi-Source Discovery:** Relying on a single API is brittle. The pipeline queries Google/DuckDuckGo for LinkedIn profiles, then searches GitHub commits, company websites, and public web pages to find published email addresses.
* **Fallback Validation Strategy:** If no public email is discovered, the system falls back to generating common permutations (e.g., `first.last@`) and tests them via raw SMTP `RCPT TO` probes without actually sending an email.
* **Accuracy vs. Coverage Tradeoff:** Because the system refuses to use paid email-finding APIs, it cannot find every email. It trades total coverage for high confidence—if an email can't be found or verified, the profile is excluded from the final export.
* **Stateless Operation:** The pipeline runs locally and exports to a CSV file. No database is required, ensuring portability and data privacy.

## How It Works

1. **Target Identification:** You input a company name and job title (e.g., "Scale AI", "Engineering Manager").
2. **LLM Verification:** The system searches for matching LinkedIn profiles and uses an LLM to verify the person currently holds the target role at the target company, filtering out recruiters or individuals with stale job histories.
3. **Data Pipeline:** It searches the open web, GitHub, and company pages for the target's actual email address. If none is found, it falls back to SMTP probing.
4. **Draft Generation:** Profiles with discovered or verified emails receive a tailored cold email draft based on your technical background.
5. **Quality Gate:** Only high-confidence emails (Found or Verified) are exported. Pattern-based guesses are excluded.

## Demo / Output Preview

**Input:** Company: `Scale AI`, Role: `Engineering Manager`
*(Example output, anonymized)*

```text
Name: Alex Chen
Role: Engineering Manager @ Scale AI
Email: alex.c@scale.com
Confidence: ✓ Found

Email Draft:
Subject: Question about ML infrastructure at Scale AI

Hi Alex,

I came across your work at Scale AI while looking into teams working on ML infrastructure.

I’ve been working on motion processing pipelines (SMPL/SMPL-H, BVH data) and recently built a system that automates outreach by discovering and validating real emails from public sources.

I’d be interested to understand what your team is currently focused on — would you be open to a quick chat?

Thanks,
Ishwar
```

## Sample Output File

A sample CSV export demonstrating the structured, high-confidence data pipeline output is included in the repository as `outreach_results_sample.csv`.

## Failure Cases & Limitations

Real-world systems fail. Here is where Zora reaches its limits:

* **No Public Digital Footprint:** If a hiring manager has absolutely no public email presence (no GitHub commits, conference talks, or company team pages), the primary discovery module will fail.
* **Network Restrictions (Port 25):** The fallback SMTP validation requires outbound port 25. Most residential ISPs block this, meaning local runs will often fail the SMTP step and rely entirely on the web scraping module.
* **Catch-All Domains:** Many large tech companies configure their mail servers to accept all incoming mail (`250 OK`). For these domains, SMTP validation is inconclusive, and the email is flagged as `Likely` rather than `Verified`.
* **Search Engine Rate Limits:** The profile scraping relies on Google and DuckDuckGo search results. Aggressive usage will result in HTTP 429s or CAPTCHAs, reducing the yield.

### Google 429 on Render / Cloud Hosts

If logs show Google responses like `429 Too Many Requests` or `/sorry/index`, your host IP is being rate-limited (common on shared cloud infrastructure).

Zora now supports a configurable search backend:

* `SEARCH_BACKEND=ddg` → uses DuckDuckGo only (recommended on Render)
* `SEARCH_BACKEND=auto` → Google first, DuckDuckGo fallback
* `SEARCH_BACKEND=google` → force Google only

It also applies automatic cooldown after a Google 429 via `GOOGLE_COOLDOWN_SECONDS` so repeated requests do not keep hitting the same block window.

For stricter target quality, set:

* `MIN_TARGET_EVIDENCE_SCORE=3` (default)
* Increase to `4` for maximum precision (fewer but cleaner profiles)

**Metrics:** 
* Tested across ~20 tech companies.
* Yields valid/found emails for ~60% of identified profiles.
* Average pipeline execution time: 2–4 minutes depending on rate limits.

## Setup

**Prerequisites:** Python 3.10+ and an NVIDIA API Key.

```bash
git clone <repo_url>
cd "Job Search Agent"
python -m venv .venv

# Activate virtual environment
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env and supply your NVIDIA_API_KEY
```

**Run Web UI:**
```bash
python server.py
# Default: http://localhost:8000
```

**Run CLI:**
```bash
python main.py -c "Scale AI" -t "Engineering Manager" -d "scale.com"
```

## Deployment

Zora can be deployed effortlessly using Render.

Steps:

1. Connect your GitHub repository to Render as a Web Service.
2. Set the build command: `pip install -r requirements.txt`
3. Set the start command: `bash start.sh`
4. Add environment variable: `NVIDIA_API_KEY`

Recommended settings for reliable cloud deploys:

* Set health check path to `/api/health`
* Set `SEARCH_BACKEND=ddg` (avoids shared-IP Google 429/CAPTCHA issues)
* Keep `GOOGLE_COOLDOWN_SECONDS=1800`
* Use Python `3.11.x` runtime

This repo also includes:

* `render.yaml` for declarative Render service setup
* `runtime.txt` to pin Python for stable builds

*(Note: SMTP validation probes require outbound port 25, which may be blocked or restricted depending on your host. If blocked, Zora will automatically rely on the public web metadata discovery).*

### Vercel Frontend + Render Backend

If the frontend is hosted on Vercel and the API is hosted on Render, the frontend must forward `/api/*` requests to the backend.

This repo includes `vercel.json` with a rewrite:

* `/api/(.*)` → `https://zora-backend-0jg5.onrender.com/api/$1`

After pulling latest code, redeploy the Vercel project so `/api/search`, `/api/status/:job_id`, and `/api/download` resolve correctly.

If your Vercel project still does not apply rewrites (for example, different Root Directory settings), the frontend now also uses a direct backend base URL fallback in `frontend/app.js`:

* default: `https://zora-backend-0jg5.onrender.com`
* optional runtime override: `window.ZORA_API_BASE_URL`

### Why Validation Can Feel Slow

If you request many profiles (for example 10), step 2 can be slow because each profile may trigger web discovery plus SMTP probing.

To avoid long stalls in hosted deployments, Zora now supports worker timeouts and SMTP toggles:

* `DISCOVERY_TIMEOUT_SECONDS` (default `12`)
* `SMTP_VALIDATION_TIMEOUT_SECONDS` (default `15`)
* `VALIDATION_CONCURRENCY` (default `3`)
* `ENABLE_SMTP_VALIDATION` (default `false` on Render, `true` locally)

Recommended for Render:

* `ENABLE_SMTP_VALIDATION=false`
* `DISCOVERY_TIMEOUT_SECONDS=8` to `12`
* `SMTP_VALIDATION_TIMEOUT_SECONDS=10` to `15`
* `VALIDATION_CONCURRENCY=3` to `5`

This keeps progress moving and prevents the UI from appearing stuck on a single profile.

## Tech Stack

* **Core:** Python, FastAPI, Server-Sent Events (SSE)
* **Ingestion:** `googlesearch-python`, `ddgs` (DuckDuckGo Search)
* **Networking:** `dnspython`, native `smtplib` / raw sockets
* **AI/LLM:** NVIDIA Devstral (via OpenAI Python SDK)
* **Frontend:** Vanilla HTML/JS/CSS
