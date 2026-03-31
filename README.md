<p align="center">
  <h1 align="center">🎯 AI-Powered Job Search Outreach Agent</h1>
  <p align="center">
    <em>Automate your job search — find hiring managers, discover real emails, and draft personalized outreach at scale.</em>
  </p>
  <p align="center">
    <img src="https://img.shields.io/badge/python-3.10%2B-blue?logo=python&logoColor=white" alt="Python 3.10+">
    <img src="https://img.shields.io/badge/LLM-NVIDIA%20Devstral-76b900?logo=nvidia&logoColor=white" alt="NVIDIA Devstral">
    <img src="https://img.shields.io/badge/search-Google%20%2B%20DuckDuckGo-f5a623" alt="Google + DuckDuckGo">
    <img src="https://img.shields.io/badge/emails-Real%20Discovery-22c55e" alt="Real Email Discovery">
    <img src="https://img.shields.io/badge/license-MIT-green" alt="MIT License">
  </p>
</p>

---

## 📌 What Is This?

A fully automated **cold-outreach pipeline** designed for job seekers. Give it a company name and a job title, and it will:

1. **🔍 Find** real hiring managers on LinkedIn via Google Search (with DuckDuckGo fallback)
2. **🤖 Verify** candidates using LLM-based relevance filtering (NVIDIA Devstral)
3. **📧 Discover** their real corporate email addresses using web dorking, company website scraping, and GitHub commit analysis — **no API keys or signups required**
4. **✍️ Draft** short, personalized cold emails (≤80 words) powered by AI
5. **📊 Export** verified results to a clean CSV — **only accurate contacts, no guesses**

> **No emails are ever sent.** This tool only finds, discovers, and prepares — you stay in full control of what gets sent.

---

## 🏗️ Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                          server.py / main.py                         │
│                     (Orchestrator / Web + CLI)                        │
│                                                                      │
│  Step 1            Step 2              Step 3            Step 4      │
│  ┌──────────┐     ┌────────────────┐   ┌────────────┐   ┌────────┐  │
│  │ Target   │────▶│ Email          │──▶│ Email      │──▶│ Data   │  │
│  │ Finder   │     │ Discovery      │   │ Drafter    │   │ Export │  │
│  └──────────┘     └────────────────┘   └────────────┘   └────────┘  │
└──────────────────────────────────────────────────────────────────────┘
       │                   │                    │              │
       ▼                   ▼                    ▼              ▼
  Google Search       Web Dorking          NVIDIA NIM API    CSV File
  + DuckDuckGo        Website Scraping     (Devstral LLM)
  + LLM Verify        GitHub Commits
                      SMTP Fallback
```

| File | Purpose |
|---|---|
| `server.py` | FastAPI web server with SSE streaming |
| `main.py` | CLI entry point & pipeline orchestrator |
| `config.py` | Environment variables, constants, and your tech profile |
| `target_finder.py` | LinkedIn profile discovery via web search + LLM verification |
| `email_finder.py` | **Real email discovery** — web dorking, website scraping, GitHub commits |
| `email_validator.py` | Email permutation generator + DNS/SMTP validation (fallback) |
| `email_drafter.py` | Personalized cold email drafting via NVIDIA Devstral |
| `data_export.py` | CSV writer with Excel-compatible encoding |

---

## ⚡ Quick Start

### Prerequisites

- **Python 3.10+**
- **NVIDIA API Key** — [Get one free from NVIDIA Build](https://build.nvidia.com/)

### 1. Clone & Install

```bash
git clone <your-repo-url>
cd "Job Search Agent"

# Create virtual environment
python -m venv .venv

# Activate it
# Windows:
.venv\Scripts\activate
# macOS / Linux:
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 2. Configure Environment

```bash
cp .env.example .env
```

Open `.env` and add your API key:

```env
NVIDIA_API_KEY=nvapi-your_key_here
```

<details>
<summary><strong>📋 Full list of environment variables</strong></summary>

| Variable | Required | Default | Description |
|---|:---:|---|---|
| `NVIDIA_API_KEY` | ✅ | — | Your NVIDIA NIM API key |
| `NVIDIA_MODEL` | | `mistralai/devstral-2-123b-instruct-2512` | LLM model for email drafting & verification |
| `NVIDIA_BASE_URL` | | `https://integrate.api.nvidia.com/v1` | NVIDIA NIM API endpoint |
| `NVIDIA_TEMPERATURE` | | `0.5` | LLM response creativity (0.0–1.0) |
| `NVIDIA_MAX_TOKENS` | | `300` | Max tokens per LLM response |
| `SMTP_FROM_ADDRESS` | | `probe@yourdomain.com` | Address used in `MAIL FROM` probe |
| `SMTP_TIMEOUT` | | `10` | Seconds before SMTP probe times out |
| `REQUEST_TIMEOUT` | | `15` | HTTP request timeout for web searches |
| `MAX_SEARCH_RESULTS` | | `10` | Maximum LinkedIn profiles to return |
| `OUTPUT_CSV` | | `outreach_results.csv` | Output file path |

</details>

### 3. Run the Pipeline

#### Option A: Web UI (Recommended)

```bash
python server.py
# Open http://localhost:8000 in your browser
```

The web interface provides a modern, dark-themed dashboard with a sleek violet/purple aesthetic:
- **Real-time pipeline progress** — watch each step complete with live status updates
- **Email confidence badges** — see at a glance which emails are ✓ Found, ✓ Verified, ~ Likely
- **Automatic filtering** — only profiles with real, discovered emails get drafts and export
- **One-click Gmail compose** — opens Gmail with To, Subject, and Body pre-filled
- **CSV download** — export verified results directly from the browser

#### Option B: CLI

```bash
# Interactive mode — you'll be prompted for company, title, and domain
python main.py

# CLI mode — pass arguments directly
python main.py --company "Google" --title "Engineering Manager" --domain "google.com"

# Short flags
python main.py -c Stripe -t "Head of Engineering" -d stripe.com

# Dry-run — test search only, skip validation & drafting
python main.py --company "Google" --title "Engineering Manager" --dry-run
```

### 4. Example Searches

These companies work well for testing (engineers tend to have public emails):

| Company | Job Title | Domain |
|---|---|---|
| Vercel | Engineering Manager | `vercel.com` |
| GitLab | Engineering Manager | `gitlab.com` |
| Stripe | Software Engineer | `stripe.com` |
| Basecamp | Product Manager | `basecamp.com` |

### 5. View Results

Results are saved to `outreach_results.csv` with the following columns:

| Column | Description |
|---|---|
| `full_name` | Target's full name |
| `first_name` | First name |
| `last_name` | Last name |
| `job_title` | Role/position at the company |
| `company` | Company name |
| `domain` | Corporate email domain |
| `profile_url` | LinkedIn profile URL |
| `validated_email` | Discovered/validated email address |
| `email_confidence` | `found`, `verified`, `likely` — guessed emails are excluded |
| `email_body` | AI-drafted personalized cold outreach email |

---

## 🔧 How Each Module Works

### 🔍 Target Finder (`target_finder.py`)

Discovers LinkedIn profiles matching your search criteria using a dual-search strategy:

- **Google Search** (primary) — more accurate results via `googlesearch-python`
- **DuckDuckGo** (fallback) — used when Google is unavailable
- **LLM Verification** — sends candidate profiles to NVIDIA Devstral to verify they actually hold the specified role at the target company
- **Smart Filtering** — deduplication, company-name matching, credential stripping, and name validation

### 📧 Email Discovery (`email_finder.py`)

Finds **real email addresses** published on the internet — no API keys or signups required:

1. **Web Search Dorking** — searches DuckDuckGo for `"firstname lastname" "@domain"` and extracts matching emails from result snippets and scraped pages
2. **Company Website Scraping** — crawls common pages on the company domain (`/about`, `/team`, `/contact`, `/people`, etc.) and extracts `@domain` emails. Filters out generic addresses like `info@`, `support@`, `hr@`
3. **GitHub Commit Emails** — searches GitHub for the person, reads their public push events to find commit-author emails. The GitHub API is free (60 requests/hour, no key needed)

> **Only real, discovered emails are kept.** Profiles where no email could be found or verified are shown in the UI but without email actions or drafts.

### 📧 Email Validator (`email_validator.py`) — Fallback

If the email discovery module doesn't find a published email, the validator tries SMTP probing:

1. **Permutation Generation** — creates 10 common corporate email formats
2. **DNS MX Lookup** — resolves the domain's mail exchange servers
3. **SMTP `RCPT TO` Probe** — tests if the mailbox exists. **No email is ever sent.**
4. **Catch-All Detection** — detects domains that accept everything to avoid false positives

> **Note:** SMTP probing requires port 25 to be open. Most home ISPs block it — this is why the email discovery module was added as the primary method.

### ✍️ Email Drafter (`email_drafter.py`)

Generates personalized cold outreach emails via NVIDIA's Devstral LLM:

- **Short & Natural** — exactly 2 paragraphs, ≤80 words, written like a real person
- **No Hallucinations** — the prompt explicitly forbids making up facts about the target company
- **Zero Placeholders** — every email is ready to send as-is, no `[Your Name]` tokens
- **Skills Mapping** — maps your technical skills to the target role
- **Retry Logic** — exponential backoff with jitter on rate limits

### 📨 Gmail Integration (`app.js`)

When the pipeline finishes, each verified profile card shows an **Email** button:

- Opens **Gmail Compose** in a new tab with **To**, **Subject**, and **Body** pre-filled
- You review, edit if needed, and hit Send — no copy-paste required

### 📊 Data Export (`data_export.py`)

- Writes results to a UTF-8 BOM-encoded CSV (opens correctly in Excel)
- Includes `email_confidence` column so you know how each email was found
- Only exports profiles with verified/found emails — no guesses in the CSV

---

## 🎨 Email Confidence Levels

| Badge | Meaning | How it was found |
|---|---|---|
| ✓ **Found** | Real email found online | Web search, company website, or GitHub commits |
| ✓ **Verified** | SMTP confirmed the mailbox exists | SMTP `RCPT TO` returned 250 (Valid) |
| ~ **Likely** | Probably correct, can't fully verify | SMTP 250 but domain is catch-all |
| *(excluded)* | Guessed pattern — **not shown** | Only `first.last@domain`, not trustworthy |

---

## 🎨 Customizing Your Tech Profile

The LLM uses your tech profile to craft personalized emails. Edit `TECH_SKILLS` in `config.py`, or override via `.env`:

```bash
TECH_SKILLS='{
  "languages": ["Python", "Go", "Rust"],
  "frameworks": ["Django", "gRPC", "FastAPI"],
  "domains": ["distributed systems", "ML pipelines", "cloud infrastructure"],
  "highlights": [
    "Scaled API from 1k to 50k RPS",
    "Led migration to Kubernetes across 200+ services",
    "Built real-time fraud detection pipeline processing 10M events/day"
  ]
}'
```

---

## 🛡️ Error Handling

The pipeline is designed to be resilient — individual failures never crash the entire run:

| Scenario | What Happens |
|---|---|
| Google Search blocked / CAPTCHA | Falls back to DuckDuckGo automatically |
| HTTP 429 / 503 rate limit | Exponential backoff with jitter |
| No matching LinkedIn profiles | Logs a warning; pipeline exits cleanly |
| Name looks like a job title | Profile is skipped to prevent false positives |
| Email discovery finds nothing | Falls back to SMTP validation, then pattern guess (excluded from results) |
| No MX records for domain | Email candidates marked `UNKNOWN` |
| SMTP timeout / connection refused | Individual candidate marked `UNKNOWN`; pipeline continues |
| NVIDIA API rate limit | Exponential backoff; raises `RuntimeError` after exhausting retries |
| GitHub API rate limit (60/hr) | Method skipped; other discovery methods tried |

---

## 📁 Project Structure

```
Job Search Agent/
├── .env.example          # Template for environment variables
├── .env                  # Your local config (git-ignored)
├── .gitignore
├── requirements.txt      # Python dependencies
├── README.md
│
├── main.py               # CLI entry point & pipeline orchestrator
├── server.py             # FastAPI web server with SSE streaming
├── config.py             # Configuration & environment loader
├── target_finder.py      # LinkedIn profile discovery (Google + DuckDuckGo + LLM)
├── email_finder.py       # Real email discovery (web dork + website scrape + GitHub)
├── email_validator.py    # Email permutation & SMTP validation (fallback)
├── email_drafter.py      # AI-powered email drafting (≤80 words, 2 paragraphs)
├── data_export.py        # CSV export with confidence column
│
├── frontend/
│   ├── index.html        # Web UI — main page
│   ├── app.js            # Client-side logic (SSE, rendering, confidence badges)
│   └── styles.css        # Dark violet theme with glassmorphism
│
└── outreach_results.csv  # Output (generated after first run)
```

---

## 📦 Dependencies

| Package | Purpose |
|---|---|
| `googlesearch-python` | Google Search scraping for LinkedIn profile discovery |
| `ddgs` | DuckDuckGo search (fallback engine + email dorking) |
| `dnspython` | DNS MX record resolution |
| `openai` | NVIDIA NIM API client (OpenAI-compatible) |
| `python-dotenv` | Environment variable loading from `.env` |
| `fastapi` | Web server framework for the browser-based UI |
| `uvicorn` | ASGI server to run the FastAPI application |

---

## ⚖️ Legal & Ethical Notes

> [!IMPORTANT]
> This tool is intended for **personal job-search assistance** only.

- **No emails are sent** — the SMTP probe only issues `RCPT TO` and disconnects immediately
- **Email discovery** uses only publicly available information (web pages, GitHub public events)
- **Scraping LinkedIn** search results via a search engine is subject to their Terms of Service — use responsibly and at low volume
- **Comply with CAN-SPAM / GDPR** when actually sending outreach emails through your own email client
- **Rate limiting is built in** — the tool includes delays and backoff to be respectful to external services

---

## 📄 License

This project is open source and available under the [MIT License](LICENSE).
