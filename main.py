#!/usr/bin/env python3
"""
main.py — Orchestrator for the AI-Powered Outreach Agent (NVIDIA Devstral).

Pipeline:
  1. Identify target hiring managers via web search.
  2. Generate & validate email permutations for each target.
  3. Draft personalized cold emails via NVIDIA Devstral LLM API.
  4. Export everything to a structured CSV.

Usage
-----
    # Interactive (prompted for inputs)
    python main.py

    # CLI arguments
    python main.py --company Google --title "Engineering Manager" --domain google.com

    # Dry-run (skip email validation & LLM drafting — useful for testing scraping)
    python main.py --company Google --title "Engineering Manager" --dry-run
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

import config
from target_finder import find_targets
from email_validator import validate_emails, best_email
from email_drafter import draft_email
from data_export import export_to_csv

# ── Logging setup ────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("outreach_agent")

# Suppress noisy third-party loggers from ddgs/primp/rustls
for _noisy in ("primp", "rustls", "h2", "hyper_util", "httpx", "httpcore",
               "hpack", "cookie_store"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)


# ── Email sanitization helper ────────────────────────────────────────────────
def clean_for_email(text: str) -> str:
    """Normalise a name fragment for use in an email local-part.

    1. Drop everything after the first comma (credentials like ", CPC").
    2. Lowercase.
    3. Strip: spaces, apostrophes, commas, hyphens, forward/back slashes, pipes.
    4. Strip leading/trailing periods.
    """
    # Drop post-comma credentials  ("Cook Shaw, CPC" -> "Cook Shaw")
    if "," in text:
        text = text.split(",", 1)[0]
    out = text.lower()
    for ch in (" ", "'", ",", "-", "/", "\\", "|"):
        out = out.replace(ch, "")
    return out.strip(".")


# ── CLI argument parser ─────────────────────────────────────────────────────
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="AI-Powered Outreach Agent — find, validate, and email hiring managers.",
    )
    parser.add_argument(
        "-c", "--company",
        help="Target company name (e.g. 'Google').",
    )
    parser.add_argument(
        "-t", "--title",
        help="Job title to search for (e.g. 'Engineering Manager').",
    )
    parser.add_argument(
        "-d", "--domain",
        default="",
        help="Corporate email domain (e.g. 'google.com'). Auto-guessed if omitted.",
    )
    parser.add_argument(
        "-n", "--max-results",
        type=int,
        default=None,
        help=f"Max profiles to return (default: {config.MAX_SEARCH_RESULTS}).",
    )
    parser.add_argument(
        "-o", "--output",
        default=config.OUTPUT_CSV,
        help=f"Output CSV path (default: {config.OUTPUT_CSV}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only run the search step; skip email validation and drafting.",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable DEBUG-level logging.",
    )
    return parser


# ── Interactive fallback ─────────────────────────────────────────────────────
def _prompt_inputs(args: argparse.Namespace) -> argparse.Namespace:
    """Ask for required fields interactively if not supplied via CLI."""
    if not args.company:
        args.company = input("🏢  Target company name: ").strip()
    if not args.title:
        args.title = input("💼  Job title to search: ").strip()
    if not args.domain:
        guess = f"{args.company.lower().replace(' ', '')}.com"
        domain_input = input(f"🌐  Email domain [{guess}]: ").strip()
        args.domain = domain_input or guess
    return args


# ── Pipeline steps ───────────────────────────────────────────────────────────
def step_find(
    company: str,
    title: str,
    domain: str,
    max_results: int | None,
) -> list[dict]:
    logger.info("═══ Step 1/4: Target Identification ═══")
    profiles = find_targets(
        company=company,
        job_title=title,
        max_results=max_results or config.MAX_SEARCH_RESULTS,
    )
    # Inject the domain into each profile dict for downstream steps
    for p in profiles:
        p.setdefault("domain", domain)
        p.setdefault("validated_email", "")
        p.setdefault("email_body", "")
    if not profiles:
        logger.warning("No profiles found. Try broadening the job title or checking the company name.")
    for p in profiles:
        logger.info("  → %s  %s", p["full_name"], p["profile_url"])
    return profiles


# Words that indicate a job-title was scraped instead of a real name
_TITLE_WORDS = {"lead", "recruiter", "manager", "engineer", "director",
                "specialist", "coordinator", "consultant", "analyst"}


def _looks_like_title(name: str) -> bool:
    """Return True if *name* contains a known job-title keyword."""
    return bool(_TITLE_WORDS & {w.lower() for w in name.split()})


def step_validate(profiles: list[dict]) -> list[dict]:
    logger.info("═══ Step 2/4: Email Permutation & Validation ═══")
    for p in profiles:
        first = p["first_name"]
        last = p["last_name"]
        domain = p["domain"]

        # Skip rows where a job title was mistakenly scraped as a name
        if _looks_like_title(first) or _looks_like_title(last):
            logger.warning(
                "  Skipping '%s %s' — name looks like a job title, not a person.",
                first, last,
            )
            p["validated_email"] = ""
            continue

        try:
            candidates = validate_emails(first, last, domain)
            winner = best_email(candidates)
            if winner:
                p["validated_email"] = winner
                logger.info("  ✔ %s → %s", p["full_name"], winner)
            else:
                # Fallback: use the most common pattern unvalidated
                clean_first = clean_for_email(first)
                clean_last = clean_for_email(last)
                p["validated_email"] = f"{clean_first}.{clean_last}@{domain}"
                logger.warning(
                    "  ⚠ No validated email for %s — using best-guess: %s",
                    p["full_name"],
                    p["validated_email"],
                )
        except Exception as exc:
            logger.error("  ✘ Validation failed for %s: %s", p["full_name"], exc)
            clean_first = clean_for_email(first)
            clean_last = clean_for_email(last)
            p["validated_email"] = f"{clean_first}.{clean_last}@{domain}"
    return profiles


def step_draft(profiles: list[dict]) -> list[dict]:
    logger.info("═══ Step 3/4: Personalized Email Drafting (NVIDIA Devstral) ═══")
    draftable = [p for p in profiles if p.get("validated_email")]
    if not draftable:
        logger.warning("No profiles with validated emails — skipping drafting.")
        return profiles

    for i, p in enumerate(draftable, 1):
        try:
            email_body = draft_email(
                target_name=p["full_name"],
                target_role=p["job_title"],
                target_company=p["company"],
                tech_skills=config.TECH_SKILLS,
            )
            if not email_body:
                logger.warning(
                    "  Draft %d/%d for %s returned empty — LLM may have refused.",
                    i, len(draftable), p["full_name"],
                )
                p["email_body"] = ""
            else:
                p["email_body"] = email_body
                logger.info(
                    "  Draft %d/%d for %s (%d chars)",
                    i, len(draftable), p["full_name"], len(email_body),
                )
        except Exception as e:
            logger.error("NVIDIA API Error for %s: %s", p["full_name"], str(e))
            p["email_body"] = ""
        # Brief pause between API calls
        time.sleep(1)
    return profiles


def step_export(profiles: list[dict], output: str) -> None:
    logger.info("═══ Step 4/4: Data Export ═══")
    exportable = [p for p in profiles if p.get("validated_email")]
    if not exportable:
        logger.warning("No exportable profiles (all missing validated emails).")
        return
    path = export_to_csv(exportable, filepath=output)
    logger.info("Results saved → %s  (%d rows)", path, len(exportable))


# ── Main ─────────────────────────────────────────────────────────────────────
def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    args = _prompt_inputs(args)

    if not args.company or not args.title:
        logger.error("Company and job title are required.")
        return 1

    logger.info(
        "Starting outreach pipeline: '%s' at '%s' (domain: %s)",
        args.title,
        args.company,
        args.domain,
    )

    # Step 1 — Find targets
    profiles = step_find(args.company, args.title, args.domain, args.max_results)
    if not profiles:
        return 0

    if args.dry_run:
        logger.info("Dry-run mode — skipping validation, drafting, and export.")
        for p in profiles:
            print(f"  {p['full_name']:<30s}  {p['profile_url']}")
        return 0

    # Step 2 — Validate emails
    profiles = step_validate(profiles)

    # Step 3 — Draft emails
    profiles = step_draft(profiles)

    # Step 4 — Export
    step_export(profiles, args.output)

    logger.info("Pipeline complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
