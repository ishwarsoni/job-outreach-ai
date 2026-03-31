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
import re
import traceback
from pathlib import Path

from fastapi import FastAPI, Request, Response
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
    title="Zora",
    description="Signal-based outreach system API",
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


def _norm_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def _is_known_founder(name: str, company: str) -> bool:
    known = _KNOWN_FOUNDERS.get(_norm_text(company))
    if not known:
        return True
    n = _norm_text(name)
    if not n:
        return False
    return any(k in n for k in known)


def _apply_result_guardrails(profiles: list[dict], title: str, company: str) -> list[dict]:
    """Final API-layer precision guardrails before returning profiles to UI."""
    out = profiles

    if "founder" in (title or "").lower():
        out = [p for p in out if _is_known_founder(p.get("full_name", ""), company)]

    # Stable dedupe to avoid repeated names/URLs.
    seen: set[tuple[str, str]] = set()
    deduped: list[dict] = []
    for p in out:
        key = (
            _norm_text(p.get("full_name", "")),
            (p.get("profile_url", "") or "").strip().lower(),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(p)
    return deduped

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


import uuid
from fastapi import BackgroundTasks

# ── In-Memory Job Storage ────────────────────────────────────────────────────
jobs: dict[str, dict] = {}

def _record_event(job_id: str, event: str, data: dict):
    if job_id in jobs:
        jobs[job_id]["progress"].append({"event": event, "data": data})

def _record_error(job_id: str, stage: str, reason: str, details: str):
    if job_id in jobs:
        jobs[job_id]["errors"].append({
            "stage": stage,
            "reason": reason,
            "error": details
        })
        logger.error(f"Job {job_id} | Stage: {stage} | Reason: {reason} | Details: {details}")


# ── Worker Task ──────────────────────────────────────────────────────────────

async def _run_pipeline_worker(job_id: str, company: str, title: str, domain: str, max_results: int, dry_run: bool):
    """Executes the pipeline completely in the background, updating the jobs dictionary."""
    loop = asyncio.get_running_loop()
    profiles = []
    
    try:
        # ── Step 1: Find targets ─────────────────────────────────────
        _record_event(job_id, "step", {
            "step": 1, "title": "Target Identification", "status": "running",
            "message": f"Searching for {title} at {company}..."
        })

        try:
            profiles = await loop.run_in_executor(
                None, lambda: find_targets(company=company, job_title=title, max_results=max_results)
            )
            profiles = _apply_result_guardrails(profiles, title, company)
        except Exception as e:
            _record_error(job_id, "find_targets", "API failure or timeout", str(e))
            jobs[job_id]["status"] = "failed"
            return

        for p in profiles:
            p.setdefault("domain", domain)
            p.setdefault("validated_email", "")
            p.setdefault("email_body", "")
            p.setdefault("email_confidence", "")

        _record_event(job_id, "step", {
            "step": 1, "title": "Target Identification", "status": "done",
            "message": f"Found {len(profiles)} profile(s)", "count": len(profiles)
        })
        _record_event(job_id, "profiles", {"profiles": profiles})
        jobs[job_id]["result"] = profiles

        if not profiles:
            _record_event(job_id, "complete", {"message": "No profiles found.", "total": 0})
            jobs[job_id]["status"] = "completed"
            return

        if dry_run:
            _record_event(job_id, "complete", {"message": "Dry-run complete.", "total": len(profiles), "dry_run": True})
            jobs[job_id]["status"] = "completed"
            return

        # ── Step 2: Email discovery & validation ─────────────────────
        _record_event(job_id, "step", {
            "step": 2, "title": "Email Discovery", "status": "running",
            "message": "Searching for real emails (web, GitHub)..."
        })

        if not config.ENABLE_SMTP_VALIDATION:
            _record_event(job_id, "step", {
                "step": 2,
                "title": "Email Discovery",
                "status": "running",
                "message": "SMTP validation disabled in this environment for faster runs.",
            })
        validation_semaphore = asyncio.Semaphore(config.VALIDATION_CONCURRENCY)

        async def _validate_one(profile_index: int, profile: dict) -> tuple[int, dict]:
            first = profile["first_name"]
            last = profile["last_name"]
            p_domain = profile["domain"]

            # Mark and skip obviously bad rows fast.
            if _looks_like_title(first) or _looks_like_title(last):
                profile["validated_email"] = ""
                profile["email_confidence"] = "skipped"
                return profile_index, profile

            clean_first = _clean_for_email(first)
            clean_last = _clean_for_email(last)

            async with validation_semaphore:
                # 1) Free discovery methods.
                try:
                    discovered = await asyncio.wait_for(
                        loop.run_in_executor(
                            None,
                            lambda f=first, l=last, d=p_domain, c=profile.get("company", ""): discover_email(f, l, d, c),
                        ),
                        timeout=config.DISCOVERY_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    _record_error(
                        job_id,
                        "email_discovery",
                        "Timed out",
                        f"No result within {config.DISCOVERY_TIMEOUT_SECONDS}s for {profile['full_name']}",
                    )
                    discovered = None
                except Exception as exc:
                    _record_error(job_id, "email_discovery", "API or search timeout", str(exc))
                    discovered = None

                if discovered:
                    profile["validated_email"] = discovered
                    profile["email_confidence"] = "found"
                    return profile_index, profile

                # 2) SMTP validation fallback (optional in cloud).
                if config.ENABLE_SMTP_VALIDATION:
                    try:
                        candidates = await asyncio.wait_for(
                            loop.run_in_executor(
                                None,
                                lambda f=clean_first, l=clean_last, d=p_domain: validate_emails(f, l, d),
                            ),
                            timeout=config.SMTP_VALIDATION_TIMEOUT_SECONDS,
                        )
                        winner = best_email(candidates)
                        if winner:
                            profile["validated_email"] = winner
                            best_result = next((c for c in candidates if c.address == winner), None)
                            profile["email_confidence"] = "verified" if best_result and best_result.result == ValidationResult.VALID else "likely"
                        else:
                            profile["validated_email"] = f"{clean_first}.{clean_last}@{p_domain}"
                            profile["email_confidence"] = "guessed"
                    except asyncio.TimeoutError:
                        _record_error(
                            job_id,
                            "smtp_validation",
                            "Timed out",
                            f"No result within {config.SMTP_VALIDATION_TIMEOUT_SECONDS}s for {profile['full_name']}",
                        )
                        profile["validated_email"] = f"{clean_first}.{clean_last}@{p_domain}"
                        profile["email_confidence"] = "guessed"
                    except Exception as exc:
                        _record_error(job_id, "smtp_validation", "Timeout or network restriction", str(exc))
                        profile["validated_email"] = f"{clean_first}.{clean_last}@{p_domain}"
                        profile["email_confidence"] = "guessed"
                else:
                    profile["validated_email"] = f"{clean_first}.{clean_last}@{p_domain}"
                    profile["email_confidence"] = "guessed"

            return profile_index, profile

        tasks = [
            asyncio.create_task(_validate_one(i, p))
            for i, p in enumerate(profiles)
        ]

        completed = 0
        for done in asyncio.as_completed(tasks):
            profile_index, updated_profile = await done
            profiles[profile_index] = updated_profile
            completed += 1

            _record_event(job_id, "validation_progress", {
                "index": completed - 1,
                "total": len(profiles),
                "name": updated_profile.get("full_name", ""),
                "email": updated_profile.get("validated_email", ""),
                "confidence": updated_profile.get("email_confidence", ""),
            })

        _TRUSTWORTHY = {"found", "verified", "likely"}
        validated_count = sum(
            1 for p in profiles if p.get("email_confidence") in _TRUSTWORTHY
        )

        _record_event(job_id, "step", {
            "step": 2, "title": "Email Discovery", "status": "done",
            "message": f"Found/validated {validated_count} email(s)"
        })

        # Filter trustworthy emails
        accurate = [p for p in profiles if p.get("email_confidence") in _TRUSTWORTHY]
        
        for p in profiles:
            if p.get("email_confidence") not in _TRUSTWORTHY:
                p["validated_email"] = ""
                p["email_body"] = ""

        _record_event(job_id, "profiles", {"profiles": profiles})
        jobs[job_id]["result"] = profiles

        # ── Step 3: Email drafting ───────────────────────────────────
        _record_event(job_id, "step", {
            "step": 3, "title": "Email Drafting", "status": "running",
            "message": f"Drafting emails for {len(accurate)} verified contact(s)..."
        })

        drafted_count = 0
        for i, p in enumerate(accurate):
            try:
                email_body = await loop.run_in_executor(
                    None,
                    lambda n=p["full_name"], r=p["job_title"], c=p["company"]: draft_email(
                        target_name=n, target_role=r, target_company=c, tech_skills=config.TECH_SKILLS
                    )
                )
                p["email_body"] = email_body or ""
                if email_body: drafted_count += 1
            except Exception as e:
                _record_error(job_id, "draft_emails", "LLM API failure", str(e))
                p["email_body"] = ""

            _record_event(job_id, "draft_progress", {
                "index": profiles.index(p), "total": len(profiles),
                "name": p["full_name"], "has_draft": bool(p["email_body"])
            })
            await asyncio.sleep(1)

        _record_event(job_id, "step", {
            "step": 3, "title": "Email Drafting", "status": "done", "message": f"Drafted {drafted_count} email(s)"
        })
        _record_event(job_id, "profiles", {"profiles": profiles})
        jobs[job_id]["result"] = profiles

        # ── Step 4: Data Export ──────────────────────────────────────
        _record_event(job_id, "step", {
            "step": 4, "title": "Data Export", "status": "running", "message": "Exporting results to CSV..."
        })

        exportable = [p for p in profiles if p.get("validated_email")]
        if exportable:
            try:
                await loop.run_in_executor(None, lambda: export_to_csv(exportable))
            except Exception as e:
                _record_error(job_id, "data_export", "File IO error", str(e))

        _record_event(job_id, "step", {
            "step": 4, "title": "Data Export", "status": "done", "message": f"Exported {len(exportable)} record(s)"
        })

        # ── Pipeline Complete ────────────────────────────────────────
        _record_event(job_id, "complete", {
            "message": "Pipeline complete!", "total": len(profiles),
            "validated": validated_count, "drafted": drafted_count, "exported": len(exportable)
        })
        jobs[job_id]["status"] = "completed"

    except Exception as exc:
        _record_error(job_id, "pipeline_fatal", "Unexpected critical error", f"{exc}\n{traceback.format_exc()}")
        jobs[job_id]["status"] = "failed"


# ── Routes ───────────────────────────────────────────────────────────────────

@app.get("/")
async def serve_index():
    return FileResponse(str(FRONTEND_DIR / "index.html"))


@app.head("/")
async def serve_index_head():
    return Response(status_code=200)

@app.get("/api/health")
async def health_check():
    return {"status": "ok", "version": "1.0.0"}


@app.head("/api/health")
async def health_check_head():
    return Response(status_code=200)

@app.post("/api/search")
async def search_pipeline(request: Request, background_tasks: BackgroundTasks):
    body = await request.json()
    company = body.get("company", "").strip()
    title = body.get("title", "").strip()
    domain = body.get("domain", "").strip()
    max_results = min(int(body.get("max_results", 3)), 10)  # Capped for safety and performance
    dry_run = body.get("dry_run", False)

    if not company or not title:
        return JSONResponse(status_code=400, content={"error": "Company and job title are required."})

    if not domain:
        domain = f"{company.lower().replace(' ', '')}.com"

    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        "status": "running",
        "progress": [],
        "errors": [],
        "result": []
    }

    background_tasks.add_task(_run_pipeline_worker, job_id, company, title, domain, max_results, dry_run)
    return {"job_id": job_id}


@app.get("/api/status/{job_id}")
async def get_status(job_id: str):
    job = jobs.get(job_id)
    if not job:
        return JSONResponse(status_code=404, content={"error": "Job not found"})
    return job


@app.get("/api/download")
async def download_csv():
    csv_path = Path(config.OUTPUT_CSV).resolve()
    if not csv_path.exists():
        return JSONResponse(status_code=404, content={"error": "No results file found."})
    return FileResponse(path=str(csv_path), filename="outreach_results.csv", media_type="text/csv")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, log_level="info")
