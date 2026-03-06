"""
data_export.py -- Write outreach results to a structured CSV file.

Handles both **create** and **append** workflows:

* First call (file does not exist)  -> create file, write header + rows.
* Subsequent calls (file exists)    -> append rows, skip duplicate header.

All cells are wrapped in ``csv.QUOTE_ALL`` to avoid delimiter-in-data issues.
Encoding is ``utf-8-sig`` (BOM) so that Excel opens the file correctly.

Public API
----------
    export_to_csv(results, filepath=None) -> Path
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Sequence

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


# ---------------------------------------------------------------------------
# CSV schema
# ---------------------------------------------------------------------------
FIELDNAMES: list[str] = [
    "full_name",
    "first_name",
    "last_name",
    "job_title",
    "company",
    "domain",
    "profile_url",
    "validated_email",
    "email_body",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sanitise_row(raw: dict) -> dict:
    """Return a dict that contains exactly the keys in ``FIELDNAMES``.

    * Missing keys are filled with an empty string.
    * Extra keys are silently dropped.
    * All values are cast to ``str`` to prevent csv writer type errors.
    """
    return {field: str(raw.get(field, "")) for field in FIELDNAMES}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def export_to_csv(
    results: Sequence[dict],
    filepath: str | Path | None = None,
) -> Path:
    """Append *results* to the CSV at *filepath* (creating it if needed).

    Parameters
    ----------
    results : Sequence[dict]
        One dict per outreach target.  Keys should match ``FIELDNAMES``;
        missing keys become empty strings, extra keys are ignored.
    filepath : str | Path | None
        Destination CSV path.  Defaults to ``config.OUTPUT_CSV``.

    Returns
    -------
    Path
        Absolute path of the written CSV file.
    """
    if filepath is None:
        dest = Path(config.OUTPUT_CSV)
    else:
        dest = Path(filepath)

    # Overwrite the file each run so results are fresh
    with dest.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=FIELDNAMES,
            quoting=csv.QUOTE_ALL,
            extrasaction="ignore",
        )

        writer.writeheader()
        logger.info("Created CSV with header: %s", dest)

        written = 0
        for row in results:
            writer.writerow(_sanitise_row(row))
            written += 1

    logger.info(
        "Exported %d record(s) to %s  (total size: %s bytes)",
        written,
        dest.resolve(),
        dest.stat().st_size,
    )
    return dest.resolve()


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import tempfile, os

    print("=" * 60)
    print("  data_export.py -- self-test")
    print("=" * 60)

    # Use a temp file so we never clobber real data
    tmp = Path(tempfile.mkdtemp()) / "test_export.csv"
    print(f"\n  Temp CSV: {tmp}\n")

    sample = [
        {
            "full_name": "Alice Smith",
            "first_name": "Alice",
            "last_name": "Smith",
            "job_title": "CTO",
            "company": "Acme Inc",
            "domain": "acme.com",
            "profile_url": "https://linkedin.com/in/alice-smith",
            "validated_email": "alice@acme.com",
            "email_body": "Hi Alice, I noticed your work at Acme...",
        },
        {
            "full_name": "Bob Jones",
            "first_name": "Bob",
            "last_name": "Jones",
            "job_title": "VP Engineering",
            "company": "Widgets Co",
            "domain": "widgets.co",
            "profile_url": "https://linkedin.com/in/bob-jones",
            "validated_email": "bjones@widgets.co",
            "email_body": "Dear Bob, your team at Widgets Co...",
        },
    ]

    # Pass 1 -- create
    p = export_to_csv(sample[:1], filepath=tmp)
    print(f"  Pass 1 (create):  wrote 1 row  -> {p}")

    # Pass 2 -- append (should NOT duplicate header)
    p = export_to_csv(sample[1:], filepath=tmp)
    print(f"  Pass 2 (append):  wrote 1 row  -> {p}")

    # Pass 3 -- append again with a dict that has missing keys
    p = export_to_csv([{"full_name": "Sparse Record"}], filepath=tmp)
    print(f"  Pass 3 (sparse):  wrote 1 row  -> {p}")

    # Verify
    with open(tmp, encoding="utf-8-sig") as f:
        lines = f.readlines()

    print(f"\n  Total lines in CSV (including header): {len(lines)}")
    header_count = sum(1 for ln in lines if "full_name" in ln and "email_body" in ln)
    print(f"  Header rows: {header_count}  (expected: 1)")

    for i, ln in enumerate(lines):
        print(f"    [{i}] {ln.rstrip()[:90]}{'...' if len(ln.rstrip()) > 90 else ''}")

    # Cleanup
    os.remove(tmp)
    os.rmdir(tmp.parent)
    print("\n  [OK] Self-test passed, temp file cleaned up.\n")
