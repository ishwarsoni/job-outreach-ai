"""
target_finder.py -- LinkedIn profile discovery via Google Search + DuckDuckGo fallback.

Uses ``googlesearch-python`` as the primary search engine (far more accurate
results than DuckDuckGo) with ``duckduckgo_search`` (DDGS) as a fallback
when Google rate-limits.

Key accuracy features:
  - Google Search with tight site:linkedin.com/in queries
  - DuckDuckGo enrichment for title/snippet data
  - Mandatory LLM verification to filter false positives
  - Name validation (rejects job-title-like names)
  - Company-mention relevance filtering

Public API
----------
    find_targets(company, job_title, max_results=5) -> list[dict]
"""

from __future__ import annotations

import json
import logging
import re
import time
from urllib.parse import urlparse

from googlesearch import search as google_search
from ddgs import DDGS
from openai import OpenAI

import config

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# If Google starts returning 429/CAPTCHA, pause Google lookups temporarily
# and route all searches through DuckDuckGo for the cooldown window.
_GOOGLE_BLOCKED_UNTIL: float = 0.0


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Words that indicate a scraped "name" is actually a job title, not a person
_TITLE_WORDS = frozenset({
    "lead", "recruiter", "manager", "engineer", "director", "specialist",
    "coordinator", "consultant", "analyst", "developer", "designer",
    "founder", "co-founder", "cofounder", "ceo", "cto", "cfo", "coo", "vp",
    "president", "head", "chief", "officer", "partner", "architect",
    "scientist", "professor", "teacher", "instructor", "associate",
    "intern", "trainee", "senior", "junior", "staff", "principal",
})

# High-precision founder guardrail for common companies.
_KNOWN_FOUNDERS: dict[str, set[str]] = {
    "google": {"larry page", "sergey brin"},
    "microsoft": {"bill gates", "paul allen"},
    "amazon": {"jeff bezos"},
    "meta": {"mark zuckerberg", "eduardo saverin", "dustin moskovitz", "chris hughes", "andrew mccollum"},
    "apple": {"steve jobs", "steve wozniak", "ronald wayne"},
    "netflix": {"reed hastings", "marc randolph"},
    "nvidia": {"jensen huang", "chris malachowsky", "curtis priem"},
    "openai": {"sam altman", "greg brockman", "ilya sutskever", "wojciech zaremba", "john schulman", "elon musk"},
    "scale ai": {"alexandr wang", "lucy guo"},
    "databricks": {"ali ghodsi", "matei zaharia", "ion stoica", "reynold xin", "patrick wendell", "arsalan tavakoli", "andy konwinski"},
}


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
    # Step 1 -- drop everything after the first pipe
    name_part = re.split(r"\s*\|\s*", raw_title, maxsplit=1)[0].strip()

    # Step 2 -- name is usually the first dash-delimited segment
    # Handles both "Name - Role - Company" and "Name-Role" variants.
    name_part = re.split(r"\s*-\s*", name_part, maxsplit=1)[0].strip()

    # Step 3 -- remove stray "LinkedIn" if it somehow survived, and trim
    name_part = re.sub(r"\bLinkedIn\b", "", name_part, flags=re.IGNORECASE).strip()

    # Step 4 -- collapse multiple spaces
    name_part = re.sub(r"\s{2,}", " ", name_part)

    # Step 5 -- remove credential suffixes like MBA, PhD, PMP, CPA, etc.
    name_part = re.sub(r",?\s+(?:MBA|PhD|PMP|CPA|PE|MD|JD|CFA|CISSP|PgMP|CSM)\.?$",
                       "", name_part, flags=re.IGNORECASE).strip()

    return name_part


def _split_name(full_name: str) -> tuple[str, str]:
    """Split *full_name* into (first_name, last_name)."""
    parts = full_name.split()
    if len(parts) == 0:
        return ("", "")
    if len(parts) == 1:
        return (parts[0], "")
    return (parts[0], " ".join(parts[1:]))


def _is_known_founder(full_name: str, company: str) -> bool:
    """Return True if full_name matches known founders for company (when configured)."""
    c = _normalize_token_text(company)
    known = _KNOWN_FOUNDERS.get(c)
    if not known:
        return True
    n = _normalize_token_text(full_name)
    if not n:
        return False
    return any(k in n for k in known)


def _is_linkedin_profile_url(url: str) -> bool:
    """Return True if *url* is a personal LinkedIn profile URL."""
    return "linkedin.com/in/" in url


def _normalize_linkedin_url(url: str) -> str:
    """Normalize a LinkedIn URL for dedup (strip query params, trailing slash)."""
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    return f"https://www.linkedin.com{path}" if "/in/" in path else url


def _name_from_url_slug(url: str) -> str:
    """Extract a name from the LinkedIn URL slug (e.g. /in/rohit-negi -> Rohit Negi)."""
    match = re.search(r"linkedin\.com/in/([^/?#]+)", url)
    if not match:
        return ""
    slug = match.group(1)
    # Remove trailing hash IDs and numbers
    slug = re.sub(r"-[a-f0-9]{6,}$", "", slug)
    slug = re.sub(r"-\d+$", "", slug)

    raw_parts = [p for p in slug.split("-") if p]

    # Stop when role/company/meta tokens start appearing in slug.
    # Example bad slug: "john-doe-founder-microsoft" -> keep "john doe".
    stop_tokens = {
        "founder", "cofounder", "co", "recruiter", "manager", "engineer",
        "director", "lead", "ceo", "cto", "vp", "president", "head",
        "microsoft", "google", "amazon", "meta", "apple", "netflix",
        "nvidia", "openai", "scale", "databricks", "at", "of",
    }

    parts: list[str] = []
    for p in raw_parts:
        p_low = p.lower()
        if p_low in stop_tokens:
            break
        # Drop pure numeric/hash fragments
        if re.fullmatch(r"[0-9a-f]{6,}", p_low):
            continue
        if re.fullmatch(r"\d+", p_low):
            continue
        parts.append(p)

    return " ".join(p.capitalize() for p in parts if p)


def _is_valid_person_name(full_name: str) -> bool:
    """Return True only for likely real person names."""
    if not full_name:
        return False

    n = full_name.strip()
    if len(n) < 3 or len(n) > 60:
        return False

    # Must contain letters and only reasonable punctuation.
    if not re.fullmatch(r"[A-Za-z .'-]+", n):
        return False

    # At least first+last in most cases for precision mode.
    parts = [p for p in n.split() if p]
    if len(parts) < 2:
        return False

    # Reject if any role words leak into name text.
    lower_words = {w.lower().strip(".'") for w in parts}
    if lower_words & _TITLE_WORDS:
        return False

    # Reject obvious broken extraction artifacts.
    bad_fragments = {"undefined", "linkedin", "profile", "founder", "recruiter", "microsoft", "google"}
    if lower_words & bad_fragments:
        return False

    return True


def _looks_like_title(name: str) -> bool:
    """Return True if *name* looks like a job title rather than a person's name."""
    words = {w.lower() for w in name.split()}
    # If more than half the words are title-words, it's probably a title
    overlap = words & _TITLE_WORDS
    if len(overlap) >= max(1, len(words) * 0.5):
        return True
    return False


def _company_mentioned_in_snippet(snippet: str, company: str) -> bool:
    """Check if the company name appears in the search snippet."""
    if not snippet or not company:
        return True  # Can't filter if no snippet
    snippet_lower = snippet.lower()
    company_lower = company.lower()

    if company_lower in snippet_lower:
        return True

    # Without spaces
    if company_lower.replace(" ", "") in snippet_lower.replace(" ", ""):
        return True

    # All significant words present
    words = [w for w in company_lower.split() if len(w) >= 4]
    if words and all(w in snippet_lower for w in words):
        return True

    return False


def _normalize_token_text(text: str) -> str:
    """Lowercase and collapse non-alnum for resilient text comparisons."""
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _company_strict_match(text: str, company: str) -> bool:
    """Stricter company match than generic mention check."""
    if not text or not company:
        return False
    hay = _normalize_token_text(text)
    needle = _normalize_token_text(company)
    if not hay or not needle:
        return False

    # Exact phrase match with word boundaries.
    if re.search(rf"\b{re.escape(needle)}\b", hay):
        return True

    # Compact fallback (handles names like "ScaleAI").
    if needle.replace(" ", "") in hay.replace(" ", ""):
        return True

    return False


def _matches_role(text: str, job_title: str) -> bool:
    """Heuristic role match for high-precision filtering."""
    hay = (text or "").lower()
    jt = (job_title or "").lower().strip()
    if not hay or not jt:
        return False

    # Common strict aliases for high-signal roles.
    if "founder" in jt:
        return any(x in hay for x in (" founder", "co-founder", "cofounder", "founding "))

    if "recruiter" in jt:
        return any(
            x in hay for x in (
                "recruiter", "talent acquisition", "talent partner",
                "sourcer", "technical recruiter", "hiring",
            )
        )

    if "engineering manager" in jt:
        return (
            "engineering manager" in hay
            or ("manager" in hay and "engineering" in hay)
        )

    # Generic fallback: require most significant title words.
    stop = {"and", "of", "the", "at", "for", "in", "to", "&"}
    tokens = [t for t in re.findall(r"[a-z0-9]+", jt) if len(t) >= 3 and t not in stop]
    if not tokens:
        return False
    matched = sum(1 for t in tokens if t in hay)
    return matched >= max(1, int(len(tokens) * 0.6))


def _founder_relation_match(raw_title: str, snippet: str, company: str) -> bool:
    """Return True only when founder/co-founder is explicitly tied to company.

    This avoids cases where text mentions "Founder" and the company separately
    (e.g. "Founder at Startup | ex-Google").
    """
    if not company:
        return False

    company_norm = _normalize_token_text(company)
    if not company_norm:
        return False

    checks = [
        _normalize_token_text(raw_title),
        _normalize_token_text(snippet),
    ]

    rel_patterns = [
        rf"\b(?:co\s*founder|cofounder|founder|founding)\b[^|\n]{{0,50}}\b(?:at|of)\b[^|\n]{{0,40}}\b{re.escape(company_norm)}\b",
        rf"\b{re.escape(company_norm)}\b[^|\n]{{0,40}}\b(?:co\s*founder|cofounder|founder|founding)\b",
    ]

    for text in checks:
        if not text:
            continue
        if any(re.search(pat, text) for pat in rel_patterns):
            # Exclude obvious "former/ex" contexts.
            if re.search(r"\b(former|ex|previously|past)\b", text):
                continue
            return True
    return False


def _evidence_score(raw_title: str, snippet: str, company: str, job_title: str) -> int:
    """Compute strict evidence score for candidate acceptance.

    Score components:
      +2 role in headline/title
      +2 company in headline/title
      +1 role in snippet
      +1 company in snippet
    """
    score = 0
    if _matches_role(raw_title, job_title):
        score += 2
    if _company_strict_match(raw_title, company):
        score += 2
    if _matches_role(snippet, job_title):
        score += 1
    if _company_strict_match(snippet, company):
        score += 1
    return score


def _deterministic_verify_candidates(
    candidates: list[dict],
    job_title: str,
    company: str,
    max_results: int,
) -> list[dict]:
    """Strict non-LLM verifier used as fallback for reliability."""
    verified: list[dict] = []
    for c in candidates:
        title = c.get("_raw_title", "")
        snippet = c.get("_snippet", "")
        combined = f"{title} {snippet}".strip()

        if not _company_mentioned_in_snippet(combined, company):
            continue
        if not _matches_role(combined, job_title):
            continue

        if "founder" in (job_title or "").lower():
            # Founder role is highly prone to false positives in snippet text.
            # Require explicit founder-company relation in headline itself.
            if not _matches_role(title, job_title):
                continue
            if not _company_strict_match(title, company):
                continue
            if not _founder_relation_match(title, "", company):
                continue

        # Require strong evidence from title/snippet to avoid false positives.
        if _evidence_score(title, snippet, company, job_title) < config.MIN_TARGET_EVIDENCE_SCORE:
            continue

        verified.append(c)
        if len(verified) >= max_results:
            break
    return verified


# ---------------------------------------------------------------------------
# Search backends
# ---------------------------------------------------------------------------

def _search_google(query: str, max_results: int) -> list[dict]:
    """Search Google and return LinkedIn profile URLs."""
    global _GOOGLE_BLOCKED_UNTIL

    results = []
    try:
        for url in google_search(query, num_results=max_results * 3, sleep_interval=2, lang="en"):
            if _is_linkedin_profile_url(url):
                results.append({"url": url, "title": "", "snippet": ""})
    except Exception as exc:
        error_text = str(exc).lower()
        if "429" in error_text or "too many requests" in error_text or "sorry/index" in error_text:
            _GOOGLE_BLOCKED_UNTIL = time.time() + max(60, config.GOOGLE_COOLDOWN_SECONDS)
            cooldown = int(_GOOGLE_BLOCKED_UNTIL - time.time())
            logger.info(
                "Google rate-limited (429/CAPTCHA). Skipping Google for %ds and using DuckDuckGo fallback.",
                cooldown,
            )
        else:
            logger.warning("Google search error: %s. Got %d results.", exc, len(results))
    return results


def _should_use_google() -> bool:
    """Return whether Google should be queried for this request."""
    backend = config.SEARCH_BACKEND
    if backend == "ddg":
        return False
    if backend == "google":
        return True
    if backend != "auto":
        logger.warning("Unknown SEARCH_BACKEND=%r, falling back to auto mode.", backend)

    if time.time() < _GOOGLE_BLOCKED_UNTIL:
        return False
    return True


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
# LLM verification
# ---------------------------------------------------------------------------

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
    """Use LLM to verify which candidates ACTUALLY hold *job_title* at *company*.

    This is the critical accuracy gate. The LLM examines each candidate's
    LinkedIn headline, snippet, and URL slug to determine if they genuinely
    hold the specified role at the target company.
    """
    if not config.NVIDIA_API_KEY:
        logger.warning("NVIDIA_API_KEY not set — using strict deterministic verification.")
        return _deterministic_verify_candidates(candidates, job_title, company, max_results)

    # Build candidate descriptions for the prompt
    candidate_lines = []
    for i, c in enumerate(candidates):
        title = c.get("_raw_title", "")
        snippet = c.get("_snippet", "")
        url = c.get("profile_url", "")
        slug_name = _name_from_url_slug(url)

        candidate_lines.append(
            f"  [{i}] Name: {c['full_name']}\n"
            f"      URL slug: {slug_name}\n"
            f"      LinkedIn Headline: {title or '(not available)'}\n"
            f"      Page Snippet: {snippet[:300] if snippet else '(not available)'}"
        )

    candidates_text = "\n".join(candidate_lines)

    prompt = f"""I am searching for people who hold the role "{job_title}" at the company "{company}".
I found the following LinkedIn profiles via web search. MANY of them are FALSE POSITIVES.

CANDIDATES:
{candidates_text}

YOUR TASK: Return ONLY the indices of people who GENUINELY hold the role "{job_title}" (or a closely related title) at "{company}" as their CURRENT primary position.

STRICT RULES — apply ALL of these:
1. The person must CURRENTLY work at "{company}" in a role matching or closely related to "{job_title}".
2. If the LinkedIn Headline says "(not available)" AND the snippet says "(not available)", you have NO evidence this person works at "{company}" as "{job_title}". You MUST EXCLUDE them.
3. If the headline mentions a DIFFERENT company as their employer, EXCLUDE them — even if they mention "{company}" elsewhere.
4. People who merely FOLLOW, LIKE, SHARE, or COMMENT about "{company}" are NOT employees. EXCLUDE them.
5. Students, interns, or people who took a course at "{company}" are NOT "{job_title}". EXCLUDE them.
6. The role "{job_title}" must be their ACTUAL JOB TITLE at "{company}", not just a word that appears near the company name.
7. If in ANY doubt, EXCLUDE. Only include candidates you are HIGHLY CONFIDENT about.
8. If NOBODY qualifies, return an empty array []. Do NOT force-include anyone.

Return ONLY a JSON array of integer indices. Example: [0, 3] or [].
No explanation, no text — ONLY the JSON array."""

    try:
        client = _get_llm_client()
        response = client.chat.completions.create(
            model=config.NVIDIA_MODEL,
            messages=[
                {"role": "system", "content": "You are a strict data verification assistant. You return only valid JSON arrays of integers. When evidence is missing or ambiguous, you exclude the candidate. You never guess."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,  # Deterministic — no creativity needed
            max_tokens=200,
        )

        text = (response.choices[0].message.content or "").strip()
        logger.debug("LLM verification response: %s", text)

        # Parse JSON, handle markdown code blocks
        text = text.strip("`").strip()
        if text.startswith("json"):
            text = text[4:].strip()

        verified_indices = json.loads(text)

        if not isinstance(verified_indices, list):
            logger.warning("LLM returned non-list: %r. Returning empty.", text)
            return []

        verified = []
        for idx in verified_indices:
            if isinstance(idx, int) and 0 <= idx < len(candidates):
                verified.append(candidates[idx])
                logger.info("  ✔ VERIFIED [%d] %s", idx, candidates[idx]["full_name"])
            if len(verified) >= max_results:
                break

        logger.info(
            "LLM verified %d out of %d candidates.",
            len(verified), len(candidates),
        )
        return verified

    except Exception as exc:
        logger.error("LLM verification failed: %s. Falling back to deterministic verification.", exc)
        return _deterministic_verify_candidates(candidates, job_title, company, max_results)


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
    as fallback. Applies company-mention filtering AND LLM verification
    to ensure high-quality results.

    Parameters
    ----------
    company : str
        Target company name (e.g. ``"Google"``).
    job_title : str
        Role to search for (e.g. ``"Engineering Manager"``).
    max_results : int
        Maximum number of profiles to return (default 5).

    Returns
    -------
    list[dict]
        Each dict contains:
        ``full_name``, ``first_name``, ``last_name``,
        ``job_title``, ``company``, ``profile_url``.
    """
    # -- Build targeted search queries --
    queries = [
        f'"{job_title}" "{company}" site:linkedin.com/in',
        f'"{company}" "{job_title}" LinkedIn profile',
        f'site:linkedin.com/in "{job_title}" at "{company}"',
    ]

    seen_urls: set[str] = set()
    all_results: list[dict] = []

    for query in queries:
        results = []
        source = "DuckDuckGo"

        # Try Google first unless disabled by config/cooldown.
        if _should_use_google():
            logger.info("Google query: %s", query)
            results = _search_google(query, max_results)
            source = "Google"
        else:
            logger.info("DuckDuckGo query (Google disabled/cooling down): %s", query)

        # Fallback to DuckDuckGo
        if not results:
            if source == "Google":
                logger.info("Google returned 0, falling back to DuckDuckGo.")
            results = _search_ddgs(query, max_results)
            source = "DuckDuckGo"

        logger.info("  %s returned %d LinkedIn profile(s).", source, len(results))

        for r in results:
            norm_url = _normalize_linkedin_url(r["url"])
            if norm_url in seen_urls:
                continue
            seen_urls.add(norm_url)
            all_results.append(r)

        time.sleep(1)

        # Stop early if we already have enough raw candidates
        if len(all_results) >= max_results * 4:
            break

    # -- Enrich results missing title/snippet via DuckDuckGo --
    if all_results and not all_results[0].get("title"):
        logger.info("Enriching results via DuckDuckGo for name extraction...")
        enrich_query = f'"{job_title}" "{company}" site:linkedin.com/in'
        ddgs_results = _search_ddgs(enrich_query, max_results * 2)

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

        # Extract name from title or URL slug
        full_name = _clean_name(raw_title) if raw_title else ""
        if not full_name:
            full_name = _name_from_url_slug(url)

        if not full_name:
            logger.debug("Could not extract name from: %r", raw_title or url)
            continue

        if not _is_valid_person_name(full_name):
            logger.debug("  SKIP '%s' — invalid or malformed person name.", full_name)
            continue

        # Skip names that look like job titles (e.g. "Senior Manager")
        if _looks_like_title(full_name):
            logger.debug("  SKIP '%s' — looks like a job title, not a name.", full_name)
            continue

        # Relevance check — company must appear in combined text
        combined_text = f"{raw_title} {snippet}"
        has_company_mention = _company_mentioned_in_snippet(combined_text, company)

        # If we have text to check AND company isn't mentioned, skip
        if combined_text.strip() and not has_company_mention:
            logger.debug("  SKIP %s — '%s' not mentioned in text.", full_name, company)
            continue

        # High-precision pre-filter so obvious title mismatches are removed
        # before the LLM gate.
        if combined_text.strip() and not _matches_role(combined_text, job_title):
            logger.debug("  SKIP %s — role/title does not match '%s'.", full_name, job_title)
            continue

        if "founder" in job_title.lower():
            if not _matches_role(raw_title, job_title):
                logger.debug("  SKIP %s — founder role missing in title.", full_name)
                continue
            if not _company_strict_match(raw_title, company):
                logger.debug("  SKIP %s — company '%s' missing in title.", full_name, company)
                continue
            if not _founder_relation_match(raw_title, "", company):
                logger.debug(
                    "  SKIP %s — founder relation to '%s' not explicit in title.",
                    full_name,
                    company,
                )
                continue
            if not _is_known_founder(full_name, company):
                logger.debug(
                    "  SKIP %s — not in known-founder set for '%s'.",
                    full_name,
                    company,
                )
                continue

        # Reject weak-evidence candidates early (precision > recall).
        score = _evidence_score(raw_title, snippet, company, job_title)
        if score < config.MIN_TARGET_EVIDENCE_SCORE:
            logger.debug(
                "  SKIP %s — weak evidence score=%d for '%s' at '%s'.",
                full_name,
                score,
                job_title,
                company,
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

    # -- LLM verification (MANDATORY accuracy gate) --
    if candidates:
        logger.info(
            "Verifying %d candidate(s) via LLM — is this person actually '%s' at '%s'?",
            len(candidates), job_title, company,
        )
        targets = _llm_verify_candidates(candidates, job_title, company, max_results)
    else:
        targets = []

    # Clean up internal fields
    if "founder" in (job_title or "").lower():
        targets = [t for t in targets if _is_known_founder(t.get("full_name", ""), company)]

    for t in targets:
        t.pop("_raw_title", None)
        t.pop("_snippet", None)

    logger.info(
        "find_targets complete: %d verified profile(s) for '%s' at '%s'.",
        len(targets), job_title, company,
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
