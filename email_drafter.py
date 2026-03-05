"""
email_drafter.py — Generates personalised cold outreach emails via the NVIDIA
NIM API (OpenAI-compatible endpoint) using the Devstral-2-123B-Instruct model.

Uses the ``openai`` Python SDK with the base URL pointed to NVIDIA's NIM
endpoint and the API key loaded from a ``.env`` file through ``config.py``.

The core function :func:`draft_email` accepts a target's name, role, company,
and the sender's tech-skills dict, then returns a concise 3-paragraph email
whose second paragraph maps those skills directly to challenges the target
role is likely facing.
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
You are an expert cold-email copywriter for software engineers pursuing new
roles.  Your emails are direct, technically credible, and free of filler.

STRICT RULES — violate none:
1.  Write EXACTLY 3 paragraphs.  No more, no fewer.
2.  Paragraph 1 (2–3 sentences): Open with the recipient's name and a
    *specific* observation about their company or team that proves genuine
    research.  State why you are reaching out — one line, no grovelling.
3.  Paragraph 2 (3–4 sentences): Map the sender's technical skills
    DIRECTLY to concrete pain points or opportunities that someone in the
    target's role at that company is likely dealing with.  Use ONLY the
    skills listed in the provided profile — never invent capabilities the
    sender does not have.  Be specific: name technologies, scale, or
    outcomes where possible.
4.  Paragraph 3 (1–2 sentences): A clear, low-friction call to action
    (e.g. "Would a 15-minute call next week work?").  No desperation, no
    clichés like "I'd love to pick your brain."
5.  Tone: confident peer-to-peer.  Zero exclamation marks.
6.  Subject line: a short, curiosity-driven line at the very top, prefixed
    with "Subject: ".
7.  Total word count: under 180 words (excluding the subject line).
8.  Do NOT include placeholders like [Your Name] — the sender will sign
    the email themselves.
"""


# ─── Prompt builder ─────────────────────────────────────────────────────────

def _build_user_prompt(
    target_name: str,
    target_role: str,
    target_company: str,
    tech_skills: dict[str, Any],
) -> str:
    """Assemble the user-facing prompt that feeds into Devstral."""
    skills_block = json.dumps(tech_skills, indent=2)
    return (
        f"Write a cold outreach email for the following scenario.\n\n"
        f"RECIPIENT:\n"
        f"  Name   : {target_name}\n"
        f"  Role   : {target_role}\n"
        f"  Company: {target_company}\n\n"
        f"SENDER'S TECHNICAL PROFILE (use ONLY these skills):\n"
        f"{skills_block}\n\n"
        f"TASK:\n"
        f"Identify 2-3 realistic technical challenges that a {target_role} at "
        f"{target_company} probably faces right now.  Then show — in concrete "
        f"terms — how the sender's profile addresses those challenges.\n\n"
        f"OUTPUT: Subject line on line 1, then exactly 3 paragraphs, "
        f"under 180 words total.  No placeholders."
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
