from __future__ import annotations

import json
import logging
import time

from target_finder import find_targets

COMPANIES = [
    "Google",
    "Microsoft",
    "Amazon",
    "Meta",
    "Apple",
    "Netflix",
    "NVIDIA",
    "OpenAI",
    "Scale AI",
    "Databricks",
]


def run_suite(job_title: str, max_results: int = 5) -> list[dict]:
    rows: list[dict] = []
    for company in COMPANIES:
        started = time.time()
        targets = find_targets(company=company, job_title=job_title, max_results=max_results)
        elapsed = round(time.time() - started, 2)
        names = [t.get("full_name", "") for t in targets]
        rows.append(
            {
                "company": company,
                "title": job_title,
                "count": len(targets),
                "seconds": elapsed,
                "names": names,
            }
        )
        print(f"{company:12s} | {job_title:10s} | count={len(targets):2d} | {elapsed:5.2f}s")
    return rows


def main() -> None:
    # Suppress noisy networking logs so benchmark output is readable.
    logging.getLogger().setLevel(logging.WARNING)
    for noisy in (
        "target_finder", "primp", "rustls", "h2", "hyper_util", "httpx",
        "httpcore", "hpack", "cookie_store", "openai", "dns", "urllib3",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    recruiter = run_suite("Recruiter")
    print()
    founder = run_suite("Founder")

    print("JSON_SUITE_START")
    print(json.dumps({"recruiter": recruiter, "founder": founder}, indent=2))


if __name__ == "__main__":
    main()
