"""
target_finder.py -- LinkedIn profile discovery via Google Search + DuckDuckGo fallback.

Uses ``googlesearch-python`` as the primary search engine (far more accurate
results than DuckDuckGo) with ``duckduckgo_search`` (DDGS) as a fallback
when Google rate-limits.

Key improvements over DuckDuckGo-only approach:
  - Google returns more relevant LinkedIn profiles
  - Multiple query patterns for broader coverage
  - Relevance filtering: only keeps profiles whose snippet mentions the company
  - De-duplication across query attempts

Public API
----------
    find_targets(company, job_title, max_results=5) -> list[dict]
"""

from __future__ import annotations

import logging
import re
import time
from urllib.parse import urlparse

from googlesearch import search as google_search
from ddgs import DDGS

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clean_name(raw_title: str) -> str:
    """Extract the person's name from a LinkedIn title string.

    Typical formats returned by search engines:
        "Sundar Pichai - CEO - Google | LinkedIn"
        "Jane Doe - Engineering Manager - Acme Corp | LinkedIn"
        "John Smith | LinkedIn"

    Strategy:
        1. Split on ``|`` and take the left-hand side (drops "LinkedIn").
        2. Split on `` - `` and take the first segment (the name).
        3. Strip whitespace and any residual non-alpha junk.
    """
    # Step 1 -- drop everything after the last pipe
    name_part = raw_title.split("|")[0].strip()

    # Step 2 -- the name is always the first dash-delimited segment
    name_part = name_part.split(" - ")[0].strip()

    # Step 3 -- remove stray "LinkedIn" if it somehow survived, and trim
    name_part = re.sub(r"\bLinkedIn\b", "", name_part, flags=re.IGNORECASE).strip()

    # Step 4 -- collapse multiple spaces
    name_part = re.sub(r"\s{2,}", " ", name_part)

    return name_part


def _split_name(full_name: str) -> tuple[str, str]:
    """Split *full_name* into (first_name, last_name).

    If only one token is present, last_name is returned as an empty string.
    For names with three or more tokens the first token is the first name and
    the rest are joined as the last name (e.g. "Mary Jane Watson" ->
    ("Mary", "Jane Watson")).
    """
    parts = full_name.split()
    if len(parts) == 0:
        return ("", "")
    if len(parts) == 1:
        return (parts[0], "")
    return (parts[0], " ".join(parts[1:]))


def _is_linkedin_profile_url(url: str) -> bool:
    """Return True if *url* is a personal LinkedIn profile URL."""
    return "linkedin.com/in/" in url


def _normalize_linkedin_url(url: str) -> str:
    """Normalize a LinkedIn URL for dedup (strip query params, trailing slash)."""
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    # Keep only the /in/username part
    return f"https://www.linkedin.com{path}" if "/in/" in path else url


def _company_mentioned_in_snippet(snippet: str, company: str) -> bool:
    """Check if the company name appears in the search snippet (case-insensitive).

    Also handles partial matches -- e.g. "Coder Army" matches "CoderArmy" or
    "coder army" in the snippet.
    """
    if not snippet or not company:
        return True  # Can't filter if no snippet, so include it

    snippet_lower = snippet.lower()
    company_lower = company.lower()

    # Exact match
    if company_lower in snippet_lower:
        return True

    # Match without spaces (e.g. "Coder Army" -> "coderarmy")
    company_no_space = company_lower.replace(" ", "")
    snippet_no_space = snippet_lower.replace(" ", "")
    if company_no_space in snippet_no_space:
        return True

    # Match individual significant words (each word with 4+ chars must appear)
    words = [w for w in company_lower.split() if len(w) >= 4]
    if words and all(w in snippet_lower for w in words):
        return True

    return False


# ---------------------------------------------------------------------------
# Search backends
# ---------------------------------------------------------------------------

def _search_google(query: str, max_results: int) -> list[dict]:
    """Search Google and return results as list of dicts with url + title."""
    results = []
    try:
        for url in google_search(query, num_results=max_results * 3, sleep_interval=2, lang="en"):
            if _is_linkedin_profile_url(url):
                results.append({"url": url, "title": "", "snippet": ""})
    except Exception as exc:
        logger.warning("Google search error: %s. Got %d results.", exc, len(results))
    return results


def _search_ddgs(query: str, max_results: int) -> list[dict]:
    """Search DuckDuckGo and return results with url, title, snippet."""
    results = []
    try:
        ddgs = DDGS()
        raw = ddgs.text(query, max_results=max_results * 3)
        for r in raw:
            url = r.get("href", "")
            if _is_linkedin_profile_url(url):
                results.append({
                    "url": url,
                    "title": r.get("title", ""),
                    "snippet": r.get("body", ""),
                })
    except Exception as exc:
        logger.warning("DuckDuckGo search error: %s. Got %d results.", exc, len(results))
    return results


# ---------------------------------------------------------------------------
# LLM-based verification (NVIDIA Devstral)
# ---------------------------------------------------------------------------

import json
from openai import OpenAI
import config


def _get_llm_client() -> OpenAI:
    """Return a configured OpenAI client for NVIDIA NIM."""
    return OpenAI(
        base_url=config.NVIDIA_BASE_URL,
        api_key=config.NVIDIA_API_KEY,
    )


def _llm_verify_candidates(
    candidates: list[dict],
    job_title: str,
    company: str,
    max_results: int,
) -> list[dict]:
    """Use LLM to verify which candidates actually hold *job_title* at *company*.

    Sends a single batch prompt with all candidates' LinkedIn titles and
    snippets, asks the LLM to return only the indices of verified matches.
    """
    if not config.NVIDIA_API_KEY:
        logger.warning("NVIDIA_API_KEY not set — skipping LLM verification.")
        return candidates[:max_results]

    # Build the candidate list for the prompt
    candidate_lines = []
    for i, c in enumerate(candidates):
        title = c.get("_raw_title", "")
        snippet = c.get("_snippet", "")
        candidate_lines.append(
            f"  [{i}] Name: {c['full_name']}\n"
            f"      LinkedIn Headline: {title}\n"
            f"      Page Snippet: {snippet[:300]}"
        )

    candidates_text = "\n".join(candidate_lines)

    prompt = f"""I searched LinkedIn for people with the role "{job_title}" at the company "{company}".
Below are candidates. MOST of them are FALSE POSITIVES — they just mention "{company}" somewhere on their profile (e.g. in a post, comment, or course they took) but do NOT actually hold the role "{job_title}" at "{company}".

CANDIDATES:
{candidates_text}

TASK: Return ONLY the index numbers of candidates who ACTUALLY hold the role "{job_title}" (or a very similar role like Co-Founder, Founder & CEO, etc.) specifically AT "{company}".

CRITICAL RULES — read carefully:
1. The "LinkedIn Headline" field shows the person's ACTUAL headline. If it says a DIFFERENT company (e.g. "Name - OtherCompany | LinkedIn"), they are NOT at "{company}". EXCLUDE them.
2. Someone who QUOTES, SHARES, or COMMENTS about "{company}" is NOT a {job_title} of "{company}". EXCLUDE them.
3. Someone who is "{job_title}" of a DIFFERENT company must be EXCLUDED even if they mention "{company}".
4. Someone who is a student, intern, employee, or follower of "{company}" is NOT a {job_title}. EXCLUDE them.
5. The role "{job_title}" must be EXPLICITLY stated as their position AT "{company}" in their headline or title — not just appearing near the company name in a snippet.
6. When in DOUBT, EXCLUDE. Only include if you are CERTAIN.

Return ONLY a JSON array of index numbers. If none match, return [].
NO explanation, ONLY the JSON array."""

    try:
        client = _get_llm_client()
        response = client.chat.completions.create(
            model=config.NVIDIA_MODEL,
            messages=[
                {"role": "system", "content": "You are a precise data verification assistant. Return only valid JSON arrays of integers. No explanations."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
            max_tokens=200,
        )

        text = (response.choices[0].message.content or "").strip()
        logger.debug("LLM verification response: %s", text)

        # Parse the JSON array from the response
        # Handle cases where LLM wraps in markdown code block
        text = text.strip("`").strip()
        if text.startswith("json"):
            text = text[4:].strip()

        verified_indices = json.loads(text)

        if not isinstance(verified_indices, list):
            logger.warning("LLM returned non-list: %r. Keeping all candidates.", text)
            return candidates[:max_results]

        # Filter to only verified candidates
        verified = []
        for idx in verified_indices:
            if isinstance(idx, int) and 0 <= idx < len(candidates):
                verified.append(candidates[idx])
                logger.info(
                    "  ✔ VERIFIED [%d] %s", idx, candidates[idx]["full_name"],
                )
            if len(verified) >= max_results:
                break

        logger.info(
            "LLM verified %d out of %d candidates.",
            len(verified), len(candidates),
        )
        return verified

    except Exception as exc:
        logger.error("LLM verification failed: %s. Returning top candidates.", exc)
        return candidates[:max_results]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def find_targets(
    company: str,
    job_title: str,
    max_results: int = 5,
) -> list[dict]:
    """Search for LinkedIn profiles matching *job_title* at *company*.

    Uses Google Search as primary source (more accurate), with DuckDuckGo
    as fallback. Applies relevance filtering to ensure results actually
    match the target company.

    Parameters
    ----------
    company : str
        Target company name (e.g. ``"Coder Army"``).
    job_title : str
        Role to search for (e.g. ``"Founder"``).
    max_results : int
        Maximum number of profiles to return (default 5).

    Returns
    -------
    list[dict]
        Each dict contains:
        ``full_name``, ``first_name``, ``last_name``,
        ``job_title``, ``company``, ``profile_url``.
    """
    # -- Build multiple query patterns for better coverage --
    queries = [
        f'"{job_title}" "{company}" site:linkedin.com/in',
        f'"{company}" "{job_title}" LinkedIn profile',
    ]

    seen_urls: set[str] = set()
    all_results: list[dict] = []

    for query in queries:
        logger.info("Google query: %s", query)

        # -- Try Google first (more accurate) --
        results = _search_google(query, max_results)
        source = "Google"

        # -- Fallback to DuckDuckGo if Google returned nothing --
        if not results:
            logger.info("Google returned 0 results, falling back to DuckDuckGo.")
            results = _search_ddgs(query, max_results)
            source = "DuckDuckGo"

        logger.info("  %s returned %d LinkedIn profile(s).", source, len(results))

        for r in results:
            norm_url = _normalize_linkedin_url(r["url"])
            if norm_url in seen_urls:
                continue
            seen_urls.add(norm_url)
            all_results.append(r)

        # Brief pause between queries to be polite
        time.sleep(1)

    # -- Now try to enrich results missing title/snippet via DuckDuckGo --
    # Google's `googlesearch-python` only returns URLs, no titles/snippets.
    # Do a single DDGS fetch to get titles for name extraction.

    if all_results and not all_results[0].get("title"):
        logger.info("Enriching results via DuckDuckGo for name extraction...")
        enrich_query = f'"{job_title}" "{company}" site:linkedin.com/in'
        ddgs_results = _search_ddgs(enrich_query, max_results * 2)

        # Build URL -> (title, snippet) map
        ddgs_map: dict[str, dict] = {}
        for r in ddgs_results:
            norm = _normalize_linkedin_url(r["url"])
            ddgs_map[norm] = r

        for r in all_results:
            norm = _normalize_linkedin_url(r["url"])
            if norm in ddgs_map:
                if not r.get("title"):
                    r["title"] = ddgs_map[norm].get("title", "")
                if not r.get("snippet"):
                    r["snippet"] = ddgs_map[norm].get("snippet", "")

    # -- Extract names and build candidate list --
    candidates: list[dict] = []

    for r in all_results:
        url = r["url"]
        raw_title = r.get("title", "")
        snippet = r.get("snippet", "")

        # Try to extract name from title
        full_name = _clean_name(raw_title) if raw_title else ""

        # If no title, try extracting from URL slug
        if not full_name:
            # e.g. https://linkedin.com/in/rohit-negi -> "Rohit Negi"
            match = re.search(r"linkedin\.com/in/([^/?#]+)", url)
            if match:
                slug = match.group(1)
                # Remove trailing numbers/hashes from slug
                slug = re.sub(r"-[a-f0-9]{6,}$", "", slug)
                slug = re.sub(r"-\d+$", "", slug)
                parts = slug.split("-")
                full_name = " ".join(p.capitalize() for p in parts if p)

        if not full_name:
            logger.debug("Could not extract name from: %r", raw_title or url)
            continue

        # -- Relevance check: company must appear in title or snippet --
        combined_text = f"{raw_title} {snippet}"
        if not _company_mentioned_in_snippet(combined_text, company):
            logger.debug(
                "  SKIP %s — '%s' not mentioned in snippet.",
                full_name, company,
            )
            continue

        first_name, last_name = _split_name(full_name)

        candidates.append({
            "full_name": full_name,
            "first_name": first_name,
            "last_name": last_name,
            "job_title": job_title,
            "company": company,
            "profile_url": url,
            "_raw_title": raw_title,
            "_snippet": snippet,
        })

    # -- LLM verification: filter out false positives --
    if candidates:
        logger.info(
            "Verifying %d candidate(s) via LLM (is this person actually '%s' at '%s'?)...",
            len(candidates), job_title, company,
        )
        targets = _llm_verify_candidates(candidates, job_title, company, max_results)
    else:
        targets = []

    # Clean up internal fields
    for t in targets:
        t.pop("_raw_title", None)
        t.pop("_snippet", None)

    logger.info(
        "find_targets complete: %d verified profile(s) for '%s' at '%s'.",
        len(targets),
        job_title,
        company,
    )
    return targets


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("=" * 60)
    print("  target_finder.py -- smoke test (Google + DuckDuckGo)")
    print("=" * 60)

    test_company = "Coder Army"
    test_title = "Founder"

    print(f"\n  Company  : {test_company}")
    print(f"  Title    : {test_title}\n")

    hits = find_targets(test_company, test_title, max_results=5)

    if not hits:
        print("\n  (no results)")
    else:
        print(f"\n  {'#':<4} {'Name':<28} {'URL'}")
        print(f"  {'-'*3:<4} {'-'*26:<28} {'-'*50}")
        for i, t in enumerate(hits, 1):
            print(f"  {i:<4} {t['full_name']:<28} {t['profile_url']}")

    print()
