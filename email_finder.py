"""
email_finder.py — Free email discovery via web search, company website
scraping, and GitHub commit emails.

Tries multiple free methods (NO signup required for any of them) to find a
person's *real* published email address before falling back to pattern
guessing.

Discovery order
---------------
1. **Web search dorking** — DuckDuckGo search for ``"firstname lastname" "@domain"``
   and regex-extracts matching emails from result snippets.
2. **Company website scraping** — Fetches common pages on the company domain
   (``/about``, ``/team``, ``/contact``, etc.) and extracts ``@domain`` emails.
3. **GitHub commit email** — Searches GitHub for the person, reads public events
   to find commit-author emails.

Public API
----------
    discover_email(first, last, domain, company) -> str | None
"""

from __future__ import annotations

import logging
import random
import re
import ssl
import urllib.parse
import urllib.request
import json
from typing import Optional

from ddgs import DDGS

import config

logger = logging.getLogger(__name__)

# ─── Regex for extracting emails ────────────────────────────────────────────

_EMAIL_RE = re.compile(
    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
)


def _emails_for_domain(text: str, domain: str) -> list[str]:
    """Return all email addresses in *text* whose domain matches *domain*."""
    domain_lower = domain.lower()
    return list({
        m.lower()
        for m in _EMAIL_RE.findall(text)
        if m.lower().endswith("@" + domain_lower)
    })


def _fetch_page(url: str, timeout: int = 8) -> str:
    """Fetch a web page and return its text content.  Returns '' on failure."""
    try:
        ua = random.choice(config.USER_AGENTS)
        req = urllib.request.Request(url, headers={"User-Agent": ua})
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            # Only read text pages, skip binary
            ctype = resp.headers.get("Content-Type", "")
            if "text" not in ctype and "html" not in ctype:
                return ""
            raw = resp.read(500_000)  # Cap at 500KB
            return raw.decode("utf-8", errors="replace")
    except Exception as exc:
        logger.debug("Failed to fetch %s: %s", url, exc)
        return ""


# ─────────────────────────────────────────────────────────────────────────────
# 1. Web search dorking (DuckDuckGo)
# ─────────────────────────────────────────────────────────────────────────────

def search_email_web(
    first_name: str,
    last_name: str,
    domain: str,
    company: str = "",
) -> str | None:
    """Search the public web for an email matching ``*@domain``.

    Queries DuckDuckGo with multiple dork patterns and extracts emails
    from result titles, snippets, AND the actual linked pages.
    Completely free and unlimited.
    """
    first = first_name.strip().lower()
    last = last_name.strip().lower()

    queries = [
        f'"{first_name} {last_name}" "@{domain}" email',
        f'"{first_name} {last_name}" "@{domain}"',
        f'"{first_name}.{last_name}@{domain}"',
        f'"{first_name[0]}{last_name}@{domain}"',
    ]
    if company:
        queries.insert(0, f'"{first_name} {last_name}" "{company}" email "@{domain}"')

    all_found: list[str] = []
    page_urls: list[str] = []

    try:
        ddgs = DDGS()
        for query in queries:
            logger.debug("Email dork query: %s", query)
            try:
                results = ddgs.text(query, max_results=10)
            except Exception as exc:
                logger.debug("DuckDuckGo query failed: %s", exc)
                continue

            for r in results:
                text_blob = f"{r.get('title', '')} {r.get('body', '')} {r.get('href', '')}"
                found = _emails_for_domain(text_blob, domain)
                all_found.extend(found)
                # Collect page URLs for deeper scraping
                href = r.get("href", "")
                if href and "linkedin.com" not in href:
                    page_urls.append(href)

            if all_found:
                break  # Found emails, no need for more queries

    except Exception as exc:
        logger.warning("Web email search failed: %s", exc)

    # If snippets didn't contain emails, try fetching the actual pages
    if not all_found and page_urls:
        logger.debug("Scraping %d result pages for emails...", min(len(page_urls), 5))
        for page_url in page_urls[:5]:
            html = _fetch_page(page_url)
            if html:
                found = _emails_for_domain(html, domain)
                all_found.extend(found)
                if all_found:
                    break

    if not all_found:
        logger.debug("Web dork: no emails found for %s %s @%s.", first_name, last_name, domain)
        return None

    # Prefer emails that contain the person's name
    for email in all_found:
        local = email.split("@")[0]
        if first in local or last in local:
            logger.info("Web dork FOUND: %s", email)
            return email

    # Otherwise return the first one
    winner = all_found[0]
    logger.info("Web dork FOUND (generic match): %s", winner)
    return winner


# ─────────────────────────────────────────────────────────────────────────────
# 2. Company website scraping (zero signup)
# ─────────────────────────────────────────────────────────────────────────────

# Common pages where companies list team/contact emails
_COMPANY_PATHS = [
    "/about", "/about-us", "/team", "/our-team", "/people",
    "/contact", "/contact-us", "/leadership", "/management",
    "/company", "/staff", "/directory",
]


def search_email_website(
    first_name: str,
    last_name: str,
    domain: str,
) -> str | None:
    """Crawl common pages on ``https://domain/`` looking for ``@domain`` emails.

    Checks pages like /about, /team, /contact for any published emails
    and returns the one best matching the person's name.  No API key or
    signup needed — just reads public web pages.
    """
    first = first_name.strip().lower()
    last = last_name.strip().lower()
    all_found: list[str] = []

    # First check the homepage
    base_url = f"https://{domain}"
    html = _fetch_page(base_url)
    if html:
        all_found.extend(_emails_for_domain(html, domain))

    # Then check common subpages
    if not all_found:
        for path in _COMPANY_PATHS:
            url = f"{base_url}{path}"
            html = _fetch_page(url)
            if html:
                found = _emails_for_domain(html, domain)
                all_found.extend(found)
            if all_found:
                break  # Found some, stop crawling

    if not all_found:
        logger.debug("Website scrape: no emails found on %s.", domain)
        return None

    # Deduplicate
    all_found = list(dict.fromkeys(all_found))

    # Filter out generic addresses (info@, support@, hello@, etc.)
    _GENERIC_PREFIXES = {
        "info", "support", "hello", "contact", "admin", "help",
        "sales", "marketing", "hr", "careers", "jobs", "noreply",
        "no-reply", "press", "media", "privacy", "legal", "abuse",
        "webmaster", "postmaster", "billing", "feedback",
    }
    personal = [
        e for e in all_found
        if e.split("@")[0] not in _GENERIC_PREFIXES
    ]

    candidates = personal if personal else all_found

    # Prefer emails that match the person's name
    for email in candidates:
        local = email.split("@")[0]
        if first in local or last in local:
            logger.info("Website scrape FOUND: %s", email)
            return email

    # Return first personal email even if name doesn't match
    # (still useful — reveals the company's email pattern)
    if personal:
        logger.info("Website scrape FOUND (pattern hint): %s", personal[0])
        return None  # Don't return someone else's email — just used for pattern

    logger.debug("Website scrape: only generic emails found on %s.", domain)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# 3. GitHub commit email (public API — no key needed)
# ─────────────────────────────────────────────────────────────────────────────

def search_email_github(
    first_name: str,
    last_name: str,
    company: str = "",
    domain: str = "",
) -> str | None:
    """Search GitHub for a user and extract their commit email from public events.

    The GitHub public API allows 60 requests/hour without auth.  This
    searches for users matching the name, then reads their recent public
    push events to find the author email on commits.
    """
    # Search for the user on GitHub
    query_parts = [f"{first_name} {last_name}"]
    if company:
        query_parts.append(company)
    q = " ".join(query_parts)

    search_url = (
        f"https://api.github.com/search/users?"
        f"{urllib.parse.urlencode({'q': q, 'per_page': '5'})}"
    )

    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "JobSearchAgent/1.0",
    }

    try:
        req = urllib.request.Request(search_url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())

        users = data.get("items", [])
        if not users:
            logger.debug("GitHub: no users found for '%s'.", q)
            return None

        # Check each user's public events for commit emails
        for user in users[:3]:  # Only check first 3 to save rate limit
            username = user.get("login", "")
            events_url = f"https://api.github.com/users/{username}/events/public?per_page=30"

            try:
                req = urllib.request.Request(events_url, headers=headers)
                with urllib.request.urlopen(req, timeout=10) as resp:
                    events = json.loads(resp.read().decode())
            except Exception:
                continue

            for event in events:
                if event.get("type") != "PushEvent":
                    continue

                commits = event.get("payload", {}).get("commits", [])
                for commit in commits:
                    author = commit.get("author", {})
                    email = author.get("email", "")
                    name = author.get("name", "").lower()

                    # Skip noreply and bot emails
                    if not email or "noreply" in email or "github.com" in email:
                        continue

                    # Check if the commit author name matches the person
                    first_lower = first_name.lower()
                    last_lower = last_name.lower()
                    if first_lower in name or last_lower in name:
                        # If we have a domain, prefer emails matching it
                        if domain and email.lower().endswith("@" + domain.lower()):
                            logger.info("GitHub FOUND (domain match): %s", email)
                            return email.lower()

                        # Store as fallback
                        logger.info("GitHub FOUND: %s", email)
                        return email.lower()

    except Exception as exc:
        logger.warning("GitHub search error: %s", exc)

    logger.debug("GitHub: no email found for %s %s.", first_name, last_name)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# 4. Orchestrator — tries all methods in order
# ─────────────────────────────────────────────────────────────────────────────

def discover_email(
    first_name: str,
    last_name: str,
    domain: str,
    company: str = "",
) -> str | None:
    """Try all free discovery methods and return the first real email found.

    Order: web dork → company website scrape → GitHub.
    Returns ``None`` if nothing is found (caller should fall through to
    SMTP validation or pattern guessing).
    """
    logger.info(
        "Discovering email for %s %s @ %s (%s)...",
        first_name, last_name, domain, company or "no company",
    )

    # Method 1: Web search dorking (free, unlimited)
    email = search_email_web(first_name, last_name, domain, company)
    if email:
        return email

    # Method 2: Company website scraping (free, zero signup)
    email = search_email_website(first_name, last_name, domain)
    if email:
        return email

    # Method 3: GitHub commit email (free, 60 req/hour)
    email = search_email_github(first_name, last_name, company, domain)
    if email:
        return email

    logger.info("No email discovered for %s %s — falling back to SMTP/guess.", first_name, last_name)
    return None


# ─── CLI smoke test ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )

    test_cases = [
        ("Sundar", "Pichai", "google.com", "Google"),
        ("Satya", "Nadella", "microsoft.com", "Microsoft"),
    ]

    print("\n" + "=" * 60)
    print("  email_finder.py — smoke test")
    print("=" * 60)

    for first, last, domain, company in test_cases:
        print(f"\n  Searching: {first} {last} @ {domain} ({company})")
        result = discover_email(first, last, domain, company)
        print(f"  Result: {result or '(not found)'}")

    print()
