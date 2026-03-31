"""
server.py — FastAPI backend for the Job Search Agent web UI.

Wraps the existing pipeline modules (target_finder, email_validator,
email_drafter, data_export) as REST endpoints with Server-Sent Events (SSE)
for real-time progress streaming.

Usage
-----
    python server.py              # Starts on http://localhost:8000
    uvicorn server:app --reload   # Dev mode with hot-reload
"""

from __future__ import annotations

import asyncio
import json
import logging
import traceback
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import StreamingResponse

import config
from target_finder import find_targets
from email_validator import validate_emails, best_email, ValidationResult
from email_finder import discover_email
from email_drafter import draft_email
from data_export import export_to_csv

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("server")

# Suppress noisy third-party loggers
for _noisy in ("primp", "rustls", "h2", "hyper_util", "httpx", "httpcore",
               "hpack", "cookie_store"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)


# ── FastAPI app ──────────────────────────────────────────────────────────────
app = FastAPI(
    title="Job Search Agent",
    description="AI-Powered Outreach Agent API",
    version="1.0.0",
)

# CORS — allow frontend dev server
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve frontend static files
FRONTEND_DIR = Path(__file__).resolve().parent / "frontend"
app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


# ── Helpers ──────────────────────────────────────────────────────────────────

import re as _re

# Words that indicate a job-title was scraped instead of a real name
_TITLE_WORDS = {"lead", "recruiter", "manager", "engineer", "director",
                "specialist", "coordinator", "consultant", "analyst",
                "founder", "co-founder", "ceo", "cto", "cfo", "vp",
                "president", "head", "senior", "junior", "principal",
                "associate", "intern", "architect", "developer"}


def _looks_like_title(name: str) -> bool:
    """Return True if *name* contains a known job-title keyword."""
    return bool(_TITLE_WORDS & {w.lower() for w in name.split()})


def _clean_name(raw: str) -> str:
    """Strip scraped junk from a name fragment.

    Handles patterns like:
      'Spencer – Co-Founder at Yellow.ai'  →  'Spencer'
      'Jane, Sr. Engineer'                 →  'Jane'
      'Bob (He/Him)'                       →  'Bob'
    """
    # Cut at common separators that indicate a role/company follows
    for sep in (" – ", " - ", " at ", " | ", "(", ","):
        if sep in raw:
            raw = raw.split(sep, 1)[0]
    # Remove anything that isn't a letter, space, or apostrophe
    raw = _re.sub(r"[^a-zA-Z ']", "", raw)
    # Take only the first word (the actual name)
    parts = raw.strip().split()
    return parts[0] if parts else raw.strip()


def _clean_for_email(text: str) -> str:
    """Normalise a name fragment for use in an email local-part."""
    cleaned = _clean_name(text)
    out = cleaned.lower()
    for ch in (" ", "'", ",", "-", "/", "\\", "|", "."):
        out = out.replace(ch, "")
    return out.strip(".")


def _sse_event(event: str, data: dict) -> str:
    """Format a Server-Sent Event message."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


# ── Routes ───────────────────────────────────────────────────────────────────

@app.get("/")
async def serve_index():
    """Serve the main frontend page."""
    return FileResponse(str(FRONTEND_DIR / "index.html"))


@app.get("/api/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "ok", "version": "1.0.0"}


@app.post("/api/search")
async def search_pipeline(request: Request):
    """
    Run the full outreach pipeline and stream results via SSE.

    Request body:
        { "company": str, "title": str, "domain": str,
          "max_results": int, "dry_run": bool }
    """
    body = await request.json()
    company = body.get("company", "").strip()
    title = body.get("title", "").strip()
    domain = body.get("domain", "").strip()
    max_results = body.get("max_results", config.MAX_SEARCH_RESULTS)
    dry_run = body.get("dry_run", False)

    # Validate required fields
    if not company or not title:
        return JSONResponse(
            status_code=400,
            content={"error": "Company and job title are required."},
        )

    # Auto-guess domain if not provided
    if not domain:
        domain = f"{company.lower().replace(' ', '')}.com"

    async def event_stream():
        """Generator that runs each pipeline step and yields SSE events."""
        try:
            # ── Step 1: Find targets ─────────────────────────────────────
            yield _sse_event("step", {
                "step": 1,
                "title": "Target Identification",
                "status": "running",
                "message": f"Searching for {title} at {company}...",
            })

            # Run blocking I/O in a thread pool
            loop = asyncio.get_running_loop()
            profiles = await loop.run_in_executor(
                None,
                lambda: find_targets(
                    company=company,
                    job_title=title,
                    max_results=max_results,
                ),
            )

            # Inject domain into profiles
            for p in profiles:
                p.setdefault("domain", domain)
                p.setdefault("validated_email", "")
                p.setdefault("email_body", "")
                p.setdefault("email_confidence", "")

            yield _sse_event("step", {
                "step": 1,
                "title": "Target Identification",
                "status": "done",
                "message": f"Found {len(profiles)} profile(s)",
                "count": len(profiles),
            })

            # Send found profiles
            yield _sse_event("profiles", {
                "profiles": profiles,
            })

            if not profiles:
                yield _sse_event("complete", {
                    "message": "No profiles found. Try broadening the job title.",
                    "total": 0,
                })
                return

            if dry_run:
                yield _sse_event("complete", {
                    "message": "Dry-run complete — skipped validation & drafting.",
                    "total": len(profiles),
                    "dry_run": True,
                })
                return

            # ── Step 2: Email discovery & validation ─────────────────────
            yield _sse_event("step", {
                "step": 2,
                "title": "Email Discovery",
                "status": "running",
                "message": "Searching for real emails (web, GitHub)...",
            })

            validated_count = 0
            for i, p in enumerate(profiles):
                first = p["first_name"]
                last = p["last_name"]
                p_domain = p["domain"]

                # Skip rows where a job title was scraped as a name
                if _looks_like_title(first) or _looks_like_title(last):
                    p["validated_email"] = ""
                    p["email_confidence"] = ""
                    continue

                # Clean scraped junk from names before using them
                clean_first = _clean_for_email(first)
                clean_last = _clean_for_email(last)

                # ── Try free email discovery first ───────────────────
                try:
                    discovered = await loop.run_in_executor(
                        None,
                        lambda f=first, l=last, d=p_domain, c=p.get("company", ""): discover_email(f, l, d, c),
                    )
                except Exception as exc:
                    logger.warning("Email discovery error for %s: %s", p["full_name"], exc)
                    discovered = None

                if discovered:
                    p["validated_email"] = discovered
                    p["email_confidence"] = "found"
                    validated_count += 1
                else:
                    # ── Fall back to SMTP validation ─────────────────
                    try:
                        candidates = await loop.run_in_executor(
                            None,
                            lambda f=clean_first, l=clean_last, d=p_domain: validate_emails(f, l, d),
                        )
                        winner = best_email(candidates)
                        if winner:
                            p["validated_email"] = winner
                            best_result = next(
                                (c for c in candidates if c.address == winner), None
                            )
                            if best_result and best_result.result == ValidationResult.VALID:
                                p["email_confidence"] = "verified"
                            else:
                                p["email_confidence"] = "likely"
                            validated_count += 1
                        else:
                            p["validated_email"] = f"{clean_first}.{clean_last}@{p_domain}"
                            p["email_confidence"] = "guessed"
                            validated_count += 1
                    except Exception as exc:
                        logger.error("Validation failed for %s: %s", p["full_name"], exc)
                        p["validated_email"] = f"{clean_first}.{clean_last}@{p_domain}"
                        p["email_confidence"] = "guessed"
                        validated_count += 1

                # Stream progress update for this profile
                yield _sse_event("validation_progress", {
                    "index": i,
                    "total": len(profiles),
                    "name": p["full_name"],
                    "email": p["validated_email"],
                    "confidence": p.get("email_confidence", ""),
                })

            yield _sse_event("step", {
                "step": 2,
                "title": "Email Discovery",
                "status": "done",
                "message": f"Found/validated {validated_count} email(s)",
            })

            # ── Filter: keep only profiles with real emails ──────────────
            _TRUSTWORTHY = {"found", "verified", "likely"}
            accurate = [
                p for p in profiles
                if p.get("email_confidence") in _TRUSTWORTHY
            ]

            # Clear email/body on guessed profiles so they show as "no email"
            for p in profiles:
                if p.get("email_confidence") not in _TRUSTWORTHY:
                    p["validated_email"] = ""
                    p["email_body"] = ""

            logger.info(
                "Filtered to %d accurate email(s) out of %d total profiles.",
                len(accurate), len(profiles),
            )

            # Send updated profiles (guessed ones shown without email)
            yield _sse_event("profiles", {
                "profiles": profiles,
            })

            # ── Step 3: Email drafting (only for accurate emails) ────────
            yield _sse_event("step", {
                "step": 3,
                "title": "Email Drafting",
                "status": "running",
                "message": f"Drafting emails for {len(accurate)} verified contact(s)...",
            })

            draftable = accurate
            drafted_count = 0

            for i, p in enumerate(draftable):
                try:
                    email_body = await loop.run_in_executor(
                        None,
                        lambda n=p["full_name"], r=p["job_title"],
                               c=p["company"]: draft_email(
                            target_name=n,
                            target_role=r,
                            target_company=c,
                            tech_skills=config.TECH_SKILLS,
                        ),
                    )
                    p["email_body"] = email_body or ""
                    if email_body:
                        drafted_count += 1
                except Exception as e:
                    logger.error("Draft failed for %s: %s", p["full_name"], e)
                    p["email_body"] = ""

                yield _sse_event("draft_progress", {
                    "index": i,
                    "total": len(draftable),
                    "name": p["full_name"],
                    "has_draft": bool(p["email_body"]),
                })

                # Brief pause between API calls
                await asyncio.sleep(1)

            yield _sse_event("step", {
                "step": 3,
                "title": "Email Drafting",
                "status": "done",
                "message": f"Drafted {drafted_count} email(s)",
            })

            # Send final profiles with emails
            yield _sse_event("profiles", {
                "profiles": profiles,
            })

            # ── Step 4: Export CSV ───────────────────────────────────────
            yield _sse_event("step", {
                "step": 4,
                "title": "Data Export",
                "status": "running",
                "message": "Exporting results to CSV...",
            })

            exportable = [p for p in profiles if p.get("validated_email")]
            if exportable:
                await loop.run_in_executor(
                    None,
                    lambda: export_to_csv(exportable),
                )

            yield _sse_event("step", {
                "step": 4,
                "title": "Data Export",
                "status": "done",
                "message": f"Exported {len(exportable)} record(s)",
            })

            # ── Pipeline complete ────────────────────────────────────────
            yield _sse_event("complete", {
                "message": "Pipeline complete!",
                "total": len(profiles),
                "validated": validated_count,
                "drafted": drafted_count,
                "exported": len(exportable),
            })

        except Exception as exc:
            logger.error("Pipeline error: %s\n%s", exc, traceback.format_exc())
            yield _sse_event("error", {
                "message": str(exc),
                "step": "pipeline",
            })

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/download")
async def download_csv():
    """Download the latest outreach_results.csv."""
    csv_path = Path(config.OUTPUT_CSV).resolve()
    if not csv_path.exists():
        return JSONResponse(
            status_code=404,
            content={"error": "No results file found. Run a search first."},
        )
    return FileResponse(
        path=str(csv_path),
        filename="outreach_results.csv",
        media_type="text/csv",
    )


# ── Entry point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
