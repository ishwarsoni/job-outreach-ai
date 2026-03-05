"""
email_validator.py — Corporate email permutation generator and SMTP-level
email validator for a cold-outreach pipeline.

Workflow
--------
1. ``generate_permutations()`` builds the 10 most common corporate email
   formats from a first name, last name, and domain.
2. ``check_catch_all()`` probes the mail server with a guaranteed-fake
   address; if the server accepts it the domain is flagged as a catch-all.
3. ``validate_emails()`` ties it all together: DNS MX lookup → catch-all
   check → SMTP ``HELO`` / ``MAIL FROM`` / ``RCPT TO`` for every permutation
   → ``QUIT``.  **No email is ever sent.**

Every connection is explicitly terminated with ``QUIT`` before the socket is
closed.  Failures (``SMTPServerDisconnected``, ``socket.timeout``, DNS
errors, blocked connections) are caught and surfaced as an ``"unknown"``
status so the pipeline never crashes.
"""

from __future__ import annotations

import logging
import random
import re
import smtplib
import socket
import string
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import dns.resolver
import dns.exception

import config

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Types
# ─────────────────────────────────────────────────────────────────────────────

class ValidationResult(Enum):
    """Possible outcomes for a single email-address probe."""
    VALID = "valid"           # server returned 250 for RCPT TO
    INVALID = "invalid"       # server returned 550 / 553 / etc.
    CATCH_ALL = "catch_all"   # 250, but domain accepts everything
    UNKNOWN = "unknown"       # timeout / blocked / DNS failure


@dataclass
class EmailCandidate:
    """Container for one email address and its validation outcome."""
    address: str
    result: ValidationResult = ValidationResult.UNKNOWN
    smtp_code: int = 0
    smtp_message: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _sanitize(name: str) -> str:
    """Strip everything except ASCII letters (``O'Brien`` → ``OBrien``)."""
    return re.sub(r"[^a-zA-Z]", "", name)


def _random_local_part(length: int = 18) -> str:
    """Generate a random local-part that is virtually guaranteed not to exist."""
    chars = string.ascii_lowercase + string.digits
    return "".join(random.choices(chars, k=length))


# ─────────────────────────────────────────────────────────────────────────────
# 1. Permutation generator
# ─────────────────────────────────────────────────────────────────────────────

def generate_permutations(
    first_name: str,
    last_name: str,
    domain: str,
) -> list[str]:
    """
    Return the **10 most common corporate email formats** for *first_name*,
    *last_name* at *domain*.

    Parameters
    ----------
    first_name : str   – e.g. ``"Jane"``
    last_name  : str   – e.g. ``"Doe"``
    domain     : str   – e.g. ``"google.com"``

    Returns
    -------
    list[str]
        De-duplicated, order-preserved list of candidate addresses.

    Raises
    ------
    ValueError
        If any of the three required inputs is empty after sanitisation.

    Examples
    --------
    >>> generate_permutations("Jane", "Doe", "acme.com")
    ['jane.doe@acme.com', 'janedoe@acme.com', 'jdoe@acme.com', ...]
    """
    f = _sanitize(first_name).lower()
    l = _sanitize(last_name).lower()

    if not f or not l or not domain:
        raise ValueError(
            f"Cannot generate permutations — "
            f"first_name={first_name!r}, last_name={last_name!r}, domain={domain!r}"
        )

    fi = f[0]   # first initial
    li = l[0]   # last initial

    # 10 most common corporate patterns
    raw = [
        f"{f}.{l}@{domain}",       #  1  jane.doe@acme.com
        f"{f}{l}@{domain}",         #  2  janedoe@acme.com
        f"{fi}{l}@{domain}",        #  3  jdoe@acme.com
        f"{f}@{domain}",            #  4  jane@acme.com
        f"{f}_{l}@{domain}",        #  5  jane_doe@acme.com
        f"{f}-{l}@{domain}",        #  6  jane-doe@acme.com
        f"{fi}.{l}@{domain}",       #  7  j.doe@acme.com
        f"{l}.{f}@{domain}",        #  8  doe.jane@acme.com
        f"{l}{fi}@{domain}",        #  9  doej@acme.com
        f"{f}{li}@{domain}",        # 10  janed@acme.com
    ]

    # De-duplicate while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for addr in raw:
        if addr not in seen:
            seen.add(addr)
            unique.append(addr)
    return unique


# ─────────────────────────────────────────────────────────────────────────────
# 2. DNS MX lookup
# ─────────────────────────────────────────────────────────────────────────────

def get_mx_hosts(domain: str) -> list[str]:
    """
    Resolve MX records for *domain*, sorted by preference (lowest first).

    Returns an empty list — rather than raising — on any DNS failure so the
    caller can degrade gracefully.
    """
    try:
        answers = dns.resolver.resolve(domain, "MX")
    except dns.resolver.NoAnswer:
        logger.warning("No MX records for %s (NoAnswer).", domain)
        return []
    except dns.resolver.NXDOMAIN:
        logger.warning("Domain %s does not exist (NXDOMAIN).", domain)
        return []
    except dns.resolver.NoNameservers:
        logger.warning("No name servers reachable for %s.", domain)
        return []
    except dns.exception.DNSException as exc:
        logger.error("DNS resolution failed for %s: %s", domain, exc)
        return []

    records = sorted(answers, key=lambda r: r.preference)
    hosts = [str(r.exchange).rstrip(".") for r in records]
    logger.debug("MX hosts for %s: %s", domain, hosts)
    return hosts


# ─────────────────────────────────────────────────────────────────────────────
# 3. SMTP RCPT-TO probe  (HELO → MAIL FROM → RCPT TO → QUIT)
# ─────────────────────────────────────────────────────────────────────────────

def _smtp_probe(
    email: str,
    mx_host: str,
    timeout: int | None = None,
) -> EmailCandidate:
    """
    Open an SMTP session to *mx_host*, issue ``HELO`` / ``MAIL FROM`` /
    ``RCPT TO`` for *email*, read the response code, then **always** send
    ``QUIT`` before disconnecting.

    Returns an :class:`EmailCandidate` with the result — **never raises**.
    """
    if timeout is None:
        timeout = config.SMTP_TIMEOUT

    candidate = EmailCandidate(address=email)
    smtp: smtplib.SMTP | None = None

    try:
        # ── Connect (resolve to a single IPv4 address first so the
        #    timeout applies once, not once-per-address) ──────────────────
        addrs = socket.getaddrinfo(mx_host, 25, socket.AF_INET, socket.SOCK_STREAM)
        if not addrs:
            raise OSError(f"No IPv4 address found for {mx_host}")
        ip = addrs[0][4][0]
        smtp = smtplib.SMTP(timeout=timeout)
        smtp.connect(ip, 25)

        # ── HELO ─────────────────────────────────────────────────────────
        smtp.helo("outreach-probe.local")

        # ── MAIL FROM ────────────────────────────────────────────────────
        smtp.mail(config.SMTP_FROM_ADDRESS)

        # ── RCPT TO  (the actual test) ───────────────────────────────────
        code, msg = smtp.rcpt(email)
        candidate.smtp_code = code
        candidate.smtp_message = msg.decode(errors="replace")

        if code == 250:
            candidate.result = ValidationResult.VALID
        elif code in (550, 551, 552, 553):
            candidate.result = ValidationResult.INVALID
        else:
            # 450/451/452 = temporary, 500+ = other reject
            candidate.result = ValidationResult.UNKNOWN

    # ── Error handling (never crash) ─────────────────────────────────────
    except smtplib.SMTPServerDisconnected:
        logger.debug(
            "Server %s disconnected during probe for %s.", mx_host, email
        )
        candidate.result = ValidationResult.UNKNOWN
        candidate.smtp_message = "server disconnected"

    except smtplib.SMTPConnectError as exc:
        logger.debug("SMTP connect error to %s: %s", mx_host, exc)
        candidate.result = ValidationResult.UNKNOWN
        candidate.smtp_message = f"connect error: {exc}"

    except smtplib.SMTPResponseException as exc:
        logger.debug(
            "SMTP response exception from %s for %s: %s", mx_host, email, exc
        )
        candidate.result = ValidationResult.UNKNOWN
        candidate.smtp_code = exc.smtp_code
        candidate.smtp_message = str(exc.smtp_error)

    except (socket.timeout, TimeoutError):
        logger.debug("Socket timeout connecting to %s for %s.", mx_host, email)
        candidate.result = ValidationResult.UNKNOWN
        candidate.smtp_message = "timeout"

    except ConnectionRefusedError:
        logger.debug("Connection refused by %s for %s.", mx_host, email)
        candidate.result = ValidationResult.UNKNOWN
        candidate.smtp_message = "connection refused"

    except OSError as exc:
        logger.debug(
            "OS error probing %s via %s: %s", email, mx_host, exc
        )
        candidate.result = ValidationResult.UNKNOWN
        candidate.smtp_message = str(exc)

    finally:
        # ── Always QUIT cleanly ──────────────────────────────────────────
        if smtp is not None:
            try:
                smtp.quit()
            except (smtplib.SMTPException, OSError):
                # If QUIT itself fails just close the socket
                try:
                    smtp.close()
                except OSError:
                    pass

    return candidate


# ─────────────────────────────────────────────────────────────────────────────
# 4. Catch-all detection
# ─────────────────────────────────────────────────────────────────────────────

def check_catch_all(mx_record: str, domain: str) -> bool:
    """
    Determine whether *domain* is a **catch-all** mail server.

    Sends an SMTP ``RCPT TO`` for a randomly-generated, guaranteed-fake
    address.  If the server responds with 250 (accepted), it accepts
    *everything* and individual 250 results are meaningless.

    Parameters
    ----------
    mx_record : str
        Hostname of the MX server to probe.
    domain : str
        The email domain (used to build the fake address).

    Returns
    -------
    bool
        ``True`` → the domain is a catch-all; ``False`` → it is not, *or*
        the check was inconclusive (timeout / error).
    """
    bogus_local = _random_local_part()
    bogus_address = f"{bogus_local}@{domain}"
    logger.debug("Catch-all probe: %s via %s", bogus_address, mx_record)

    result = _smtp_probe(bogus_address, mx_record)

    if result.result == ValidationResult.VALID:
        logger.info(
            "Domain %s is a catch-all (accepted %s).", domain, bogus_address
        )
        return True

    logger.debug(
        "Catch-all probe for %s -> %s (code %d).",
        domain, result.result.value, result.smtp_code,
    )
    return False


# ─────────────────────────────────────────────────────────────────────────────
# 5. Public API — full validation pipeline
# ─────────────────────────────────────────────────────────────────────────────

def validate_emails(
    first_name: str,
    last_name: str,
    domain: str,
) -> list[EmailCandidate]:
    """
    Generate corporate email permutations and validate each one via SMTP.

    Pipeline:
      1. Resolve MX records for *domain* (``dnspython``).
      2. Run ``check_catch_all`` against the primary MX.
      3. For each permutation: ``HELO`` → ``MAIL FROM`` → ``RCPT TO`` →
         ``QUIT`` (``smtplib``).
      4. Return results sorted best-first (VALID → CATCH_ALL → UNKNOWN →
         INVALID).

    Parameters
    ----------
    first_name : str  – target's first name
    last_name  : str  – target's last name
    domain     : str  – corporate email domain

    Returns
    -------
    list[EmailCandidate]
        Every permutation with its validation result.  If DNS fails or the
        server blocks us, every entry comes back as ``UNKNOWN`` — the
        pipeline will not crash.
    """
    permutations = generate_permutations(first_name, last_name, domain)
    logger.info(
        "Generated %d permutations for %s %s @ %s.",
        len(permutations), first_name, last_name, domain,
    )

    # ── Step 1: MX lookup ────────────────────────────────────────────────
    mx_hosts = get_mx_hosts(domain)
    if not mx_hosts:
        logger.warning(
            "No MX records for %s — all candidates marked UNKNOWN.", domain,
        )
        return [EmailCandidate(address=addr) for addr in permutations]

    primary_mx = mx_hosts[0]
    logger.info("Primary MX for %s -> %s", domain, primary_mx)

    # ── Step 2: catch-all check ──────────────────────────────────────────
    is_catch_all = check_catch_all(primary_mx, domain)
    if is_catch_all:
        logger.warning(
            "%s is a catch-all — SMTP 250 codes are unreliable.", domain,
        )

    # ── Step 3: probe each permutation ───────────────────────────────────
    candidates: list[EmailCandidate] = []
    for addr in permutations:
        cand = _smtp_probe(addr, primary_mx)

        # Down-grade 250 to CATCH_ALL when the server accepts everything
        if is_catch_all and cand.result == ValidationResult.VALID:
            cand.result = ValidationResult.CATCH_ALL

        candidates.append(cand)
        logger.debug(
            "  %-35s -> %-10s (code %d)",
            addr, cand.result.value, cand.smtp_code,
        )

    # ── Sort: VALID first, then CATCH_ALL, UNKNOWN, INVALID ──────────────
    _PRIORITY = {
        ValidationResult.VALID: 0,
        ValidationResult.CATCH_ALL: 1,
        ValidationResult.UNKNOWN: 2,
        ValidationResult.INVALID: 3,
    }
    candidates.sort(key=lambda c: _PRIORITY.get(c.result, 99))

    valid_count = sum(1 for c in candidates if c.result == ValidationResult.VALID)
    logger.info(
        "Validation complete: %d valid, %d catch-all, %d unknown, %d invalid.",
        valid_count,
        sum(1 for c in candidates if c.result == ValidationResult.CATCH_ALL),
        sum(1 for c in candidates if c.result == ValidationResult.UNKNOWN),
        sum(1 for c in candidates if c.result == ValidationResult.INVALID),
    )
    return candidates


# ─────────────────────────────────────────────────────────────────────────────
# 6. Convenience helper (used by main.py)
# ─────────────────────────────────────────────────────────────────────────────

def best_email(candidates: list[EmailCandidate]) -> str | None:
    """Return the first ``VALID`` address, falling back to ``CATCH_ALL``, else ``None``."""
    for c in candidates:
        if c.result == ValidationResult.VALID:
            return c.address
    for c in candidates:
        if c.result == ValidationResult.CATCH_ALL:
            return c.address
    return None


# ─────────────────────────────────────────────────────────────────────────────
# CLI smoke test
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )

    # Use a shorter timeout for the interactive test so we don't wait
    # 10 s × 11 connections if port 25 is blocked.
    config.SMTP_TIMEOUT = 5

    test_first = "Sundar"
    test_last = "Pichai"
    test_domain = "google.com"

    # ── Test 1: Permutation generation ───────────────────────────────────
    print("\n" + "=" * 60)
    print("  Email Validator - smoke test")
    print("=" * 60)

    print(f"\n  Target : {test_first} {test_last}")
    print(f"  Domain : {test_domain}\n")

    perms = generate_permutations(test_first, test_last, test_domain)
    print(f"  Generated {len(perms)} permutations:")
    for i, p in enumerate(perms, 1):
        print(f"    {i:2d}. {p}")

    # ── Test 2: MX lookup ────────────────────────────────────────────────
    print(f"\n  MX records for {test_domain}:")
    mx = get_mx_hosts(test_domain)
    if mx:
        for host in mx:
            print(f"    -> {host}")
    else:
        print("    (none found)")

    # ── Test 3: Full validation pipeline ─────────────────────────────────
    print(f"\n  Running SMTP validation (timeout={config.SMTP_TIMEOUT}s per probe)...")
    print(f"  Port 25 is often blocked by ISPs/cloud providers - if everything")
    print(f"  comes back 'unknown' that is expected behaviour.\n")

    try:
        results = validate_emails(test_first, test_last, test_domain)
    except KeyboardInterrupt:
        print("\n  [Interrupted by user]\n")
        sys.exit(0)

    print(f"  {'Address':<35s}  {'Status':<12s}  {'Code':<5s}  Message")
    print(f"  {'-' * 35}  {'-' * 12}  {'-' * 5}  {'-' * 30}")
    for c in results:
        print(
            f"  {c.address:<35s}  {c.result.value:<12s}  {c.smtp_code:<5d}  "
            f"{c.smtp_message[:50]}"
        )

    winner = best_email(results)
    print(f"\n  Best email -> {winner or '(none - port 25 likely blocked)'}")
    print()
