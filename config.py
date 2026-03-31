"""
Configuration module — loads environment variables and defines project-wide constants.
"""

import json
import os
from pathlib import Path

from dotenv import load_dotenv

# ── Load .env from project root ──────────────────────────────────────────────
load_dotenv(Path(__file__).resolve().parent / ".env")

# ── API Keys ─────────────────────────────────────────────────────────────────
NVIDIA_API_KEY: str = os.getenv("NVIDIA_API_KEY", "")
HUNTER_API_KEY: str = os.getenv("HUNTER_API_KEY", "")  # Optional — free tier: 25 lookups/month

# ── SMTP probe settings ─────────────────────────────────────────────────────
SMTP_TIMEOUT: int = int(os.getenv("SMTP_TIMEOUT", "10"))
SMTP_FROM_ADDRESS: str = os.getenv("SMTP_FROM_ADDRESS", "probe@yourdomain.com")


def _as_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# Timeouts for background worker stages (web API mode)
DISCOVERY_TIMEOUT_SECONDS: int = int(os.getenv("DISCOVERY_TIMEOUT_SECONDS", "12"))
SMTP_VALIDATION_TIMEOUT_SECONDS: int = int(os.getenv("SMTP_VALIDATION_TIMEOUT_SECONDS", "15"))

# SMTP is often blocked on cloud hosts (port 25). Disable by default on Render
# to keep runs responsive. Local runs keep SMTP enabled by default.
ENABLE_SMTP_VALIDATION: bool = _as_bool(
    "ENABLE_SMTP_VALIDATION",
    default=not bool(os.getenv("RENDER")),
)

# ── Scraping settings ───────────────────────────────────────────────────────
SEARCH_ENGINE_URL: str = "https://html.duckduckgo.com/html/"
REQUEST_TIMEOUT: int = int(os.getenv("REQUEST_TIMEOUT", "15"))
MAX_SEARCH_RESULTS: int = int(os.getenv("MAX_SEARCH_RESULTS", "10"))

# Search backend control:
#   auto   -> use Google first, then DuckDuckGo fallback
#   google -> force Google only
#   ddg    -> force DuckDuckGo only
_default_search_backend = "ddg" if os.getenv("RENDER") else "auto"
SEARCH_BACKEND: str = os.getenv("SEARCH_BACKEND", _default_search_backend).lower()
GOOGLE_COOLDOWN_SECONDS: int = int(os.getenv("GOOGLE_COOLDOWN_SECONDS", "1800"))

# ── User-Agent rotation pool ────────────────────────────────────────────────
USER_AGENTS: list[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:126.0) Gecko/20100101 Firefox/126.0",
]

# ── NVIDIA NIM model settings ───────────────────────────────────────────────
NVIDIA_BASE_URL: str = os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1")
NVIDIA_MODEL: str = os.getenv("NVIDIA_MODEL", "mistralai/devstral-2-123b-instruct-2512")
NVIDIA_TEMPERATURE: float = float(os.getenv("NVIDIA_TEMPERATURE", "0.5"))
NVIDIA_MAX_TOKENS: int = int(os.getenv("NVIDIA_MAX_TOKENS", "300"))

# ── Output ───────────────────────────────────────────────────────────────────
OUTPUT_CSV: str = os.getenv("OUTPUT_CSV", "outreach_results.csv")

# ── Your tech profile (override via .env JSON or edit here) ──────────────────

_default_skills = {
    "languages": ["Python", "JavaScript", "SQL"],
    "frameworks": ["FastAPI", "Flask", "Selenium", "BeautifulSoup"],
    "domains": [
        "web scraping",
        "data processing pipelines",
        "AI/ML integration",
        "REST API development",
        "cloud deployment (AWS/GCP)",
    ],
    "highlights": [
        "Built production scraping systems handling 1M+ pages/day",
        "Designed real-time data pipelines with Apache Kafka",
        "Integrated LLM APIs into customer-facing products",
    ],
}
TECH_SKILLS: dict = json.loads(os.getenv("TECH_SKILLS", json.dumps(_default_skills)))
