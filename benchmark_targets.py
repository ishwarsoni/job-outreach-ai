from __future__ import annotations

import json
import time

from target_finder import find_targets

TEST_CASES = [
    ("Google", "Recruiter"),
    ("Microsoft", "Recruiter"),
    ("Amazon", "Recruiter"),
    ("Meta", "Recruiter"),
    ("Apple", "Recruiter"),
    ("Netflix", "Recruiter"),
    ("NVIDIA", "Recruiter"),
    ("OpenAI", "Recruiter"),
    ("Scale AI", "Recruiter"),
    ("Databricks", "Recruiter"),
]


def run() -> None:
    summary = []
    for company, title in TEST_CASES:
        started = time.time()
        targets = find_targets(company=company, job_title=title, max_results=5)
        elapsed = round(time.time() - started, 2)
        names = [t.get("full_name", "") for t in targets]
        summary.append(
            {
                "company": company,
                "title": title,
                "count": len(targets),
                "seconds": elapsed,
                "names": names,
            }
        )
        print(f"{company:12s} | {title:10s} | count={len(targets):2d} | {elapsed:5.2f}s")
        for t in targets:
            print(f"  - {t.get('full_name','')} :: {t.get('profile_url','')}")
        print()

    print("JSON_SUMMARY_START")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    run()
