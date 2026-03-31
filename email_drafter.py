"""
email_drafter.py — Generates short, personalised cold outreach emails via the
NVIDIA NIM API (OpenAI-compatible endpoint) using the Devstral model.

Uses the ``openai`` Python SDK with the base URL pointed to NVIDIA's NIM
endpoint and the API key loaded from a ``.env`` file through ``config.py``.

The core function :func:`draft_email` accepts a target's name, role, company,
and the sender's tech-skills dict, then returns a short 2-paragraph email
(under 80 words) with a natural, human tone.
"""

from __future__ import annotations

import json
import logging
import random
import time
from typing import Any

from openai import OpenAI

import config

logger = logging.getLogger(__name__)


# ─── NVIDIA NIM client (lazy singleton) ─────────────────────────────────────

_client: OpenAI | None = None


def _get_client() -> OpenAI:
    """Return a configured OpenAI client pointed at NVIDIA NIM, creating it on first call."""
    global _client
    if _client is None:
        if not config.NVIDIA_API_KEY:
            raise RuntimeError(
                "NVIDIA_API_KEY is not set.  "
                "Copy .env.example → .env and add your key from https://build.nvidia.com/"
            )
        _client = OpenAI(
            base_url=config.NVIDIA_BASE_URL,
            api_key=config.NVIDIA_API_KEY,
        )
        logger.debug(
            "NVIDIA NIM client initialised (model=%s, base_url=%s).",
            config.NVIDIA_MODEL,
            config.NVIDIA_BASE_URL,
        )
    return _client


# ─── System prompt ──────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You write short cold emails from a software engineer looking for work.
Keep it human—write like a real person, not a copywriter.

RULES:
1.  Subject line on line 1 prefixed with "Subject: " — keep it short and specific.
2.  EXACTLY 2 short paragraphs after the subject line.
3.  Paragraph 1 (1-2 sentences): Greet by first name, say you're interested
    in their team, mention ONE skill from the sender's profile that fits
    their role. Do NOT make up facts about their company.
4.  Paragraph 2 (1 sentence): Simple call to action like
    "Would a quick chat this week work?"
5.  TOTAL body under 80 words. Shorter is better.
6.  Tone: casual, confident, peer-to-peer. No exclamation marks.
7.  No placeholders, no brackets, no sign-off, no signature.
8.  Only mention skills the sender actually has. Never invent anything.
"""


# ─── Prompt builder ─────────────────────────────────────────────────────────

def _build_user_prompt(
    target_name: str,
    target_role: str,
    target_company: str,
    tech_skills: dict[str, Any],
) -> str:
    """Assemble the user-facing prompt that feeds into Devstral."""
    # Flatten skills into a simple comma-separated list
    flat_skills: list[str] = []
    for key, val in tech_skills.items():
        if isinstance(val, list):
            flat_skills.extend(str(s) for s in val)
    skills_line = ", ".join(flat_skills) if flat_skills else json.dumps(tech_skills)
    return (
        f"Write a short cold email.\n\n"
        f"TO: {target_name}, {target_role} at {target_company}\n"
        f"SENDER'S SKILLS: {skills_line}\n\n"
        f"Pick the ONE most relevant skill for their role and mention it "
        f"naturally. Do NOT guess or invent details about {target_company}. "
        f"Keep it under 80 words. Subject line + 2 paragraphs only."
    )


# ─── Public API ──────────────────────────────────────────────────────────────

def draft_email(
    target_name: str,
    target_role: str,
    target_company: str,
    tech_skills: dict[str, Any] | None = None,
    *,
    max_retries: int = 4,
    backoff_base: float = 2.0,
) -> str:
    """
    Generate a personalised 3-paragraph cold email via NVIDIA Devstral.

    Parameters
    ----------
    target_name : str
        Full name of the hiring manager (e.g. ``"Jane Lee"``).
    target_role : str
        Their job title (e.g. ``"Engineering Manager"``).
    target_company : str
        Company name (e.g. ``"Google"``).
    tech_skills : dict, optional
        JSON-serialisable dict describing the sender's skills.  Falls back
        to ``config.TECH_SKILLS`` when *None*.
    max_retries : int
        How many attempts before giving up (default 4).
    backoff_base : float
        Base seconds for exponential back-off on rate-limit errors.
        Actual wait = ``backoff_base * 2^(attempt-1) + jitter``.

    Returns
    -------
    str
        The complete drafted email (subject line + body).

    Raises
    ------
    RuntimeError
        If all retry attempts are exhausted or the API key is missing.
    """
    client = _get_client()

    if tech_skills is None:
        tech_skills = config.TECH_SKILLS

    user_prompt = _build_user_prompt(
        target_name, target_role, target_company, tech_skills,
    )

    last_exc: Exception | None = None

    for attempt in range(1, max_retries + 1):
        try:
            response = client.chat.completions.create(
                model=config.NVIDIA_MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=config.NVIDIA_TEMPERATURE,
                max_tokens=config.NVIDIA_MAX_TOKENS,
            )

            text = (response.choices[0].message.content or "").strip()
            if text:
                logger.info(
                    "Draft generated for %s (%d chars, attempt %d).",
                    target_name, len(text), attempt,
                )
                return text

            logger.warning(
                "NVIDIA API returned empty text (attempt %d/%d).", attempt, max_retries,
            )

        # ── Rate-limit (HTTP 429 / quota exhausted) ─────────────────────
        except Exception as exc:
            last_exc = exc
            error_msg = str(exc).lower()

            is_rate_limit = (
                "429" in str(exc)
                or "rate_limit" in error_msg
                or "rate limit" in error_msg
                or "resource_exhausted" in error_msg
            )

            wait = backoff_base * (2 ** (attempt - 1)) + random.uniform(0, 1)

            if is_rate_limit:
                logger.warning(
                    "Rate-limit hit (attempt %d/%d).  Backing off %.1f s …",
                    attempt, max_retries, wait,
                )
            else:
                logger.error(
                    "API error (attempt %d/%d): %s  — retrying in %.1f s",
                    attempt, max_retries, exc, wait,
                )
            time.sleep(wait)

    raise RuntimeError(
        f"Failed to draft email after {max_retries} attempts.  "
        f"Last error: {last_exc}"
    )


# ─── CLI smoke test ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )

    # Tech-skills dict specified in the requirements
    test_skills: dict[str, Any] = {
        "languages": ["Python"],
        "frameworks": ["FastAPI", "Scikit-learn"],
        "specialties": [
            "Computer Vision pipelines",
            "Motion processing",
            "Web scraping",
        ],
    }

    target = {
        "name": "Sarah Chen",
        "role": "Engineering Manager",
        "company": "Google",
    }

    print("\n" + "=" * 60)
    print("  Email Drafter — smoke test (NVIDIA Devstral)")
    print(f"  Model  : {config.NVIDIA_MODEL}")
    print(f"  Target : {target['name']}, {target['role']} @ {target['company']}")
    print(f"  Skills : {json.dumps(test_skills)}")
    print("=" * 60 + "\n")

    try:
        email = draft_email(
            target_name=target["name"],
            target_role=target["role"],
            target_company=target["company"],
            tech_skills=test_skills,
        )
        print(email)
    except RuntimeError as err:
        print(f"\n[ERROR] {err}")
