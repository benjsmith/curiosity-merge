#!/usr/bin/env python3
"""preflight.py — pre-publication detectors for subgraph-export and merge.

Each detector is a pure function: takes the inputs it needs, returns a
list of `Finding` dicts. No I/O outside of reading files the caller hands
us. subgraph_export.py and merge.py run these before write/apply and
surface findings with rationale; the user accepts/refuses/overrides.

# Finding shape (v0.2.1)

    {
      "kind":     <detector id>,
      "severity": "info" | "warn" | "block",
      "subject":  <path>,                  # safe to publish
      "summary":  <one-line description>,  # counts only, no user data
      "rationale": <why this matters>,     # safe to publish, NEVER contains samples
      "samples":  [<actual matches>],      # LOCAL ONLY — never written to manifest
    }

The `samples` key holds the actual matched strings (emails, SSNs, GPL
context snippets). It is shown to the user during interactive review and
is **stripped before any finding lands in a published manifest**. This
is enforced by the manifest-write path, which calls `manifest_safe(f)`
on every finding.

# Why the split

v0.2.0 embedded `Sample: alice@x.com, bob@y.org...` directly inside
`rationale`. Findings flowed into `_export-manifest.json`, which is
published. So a detector designed to prevent PII leaks ended up
broadcasting matched PII in the public artifact. v0.2.1 fixes this with
a hard schema boundary.

# Detectors

  - find_non_native_pages      chain-merge contamination
  - measure_quote_density      pages dominated by quoted source text
  - check_license_consistency  declared license disagrees with URL domain
  - redact_url                 strip query strings from source URLs
  - find_gpl_contagion         GPL/AGPL/LGPL license markers (strict)
  - find_gdpr_likely_pii       email/SSN/IBAN/payment patterns + E.164 phone

False positives are recoverable (user overrides). False negatives are
not (PII ships to the public). When in doubt, flag — but with samples
shown locally only, never written to the published artifact.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse, urlunparse

_ce_scripts = os.environ.get("CURIOSITY_ENGINE_SCRIPTS_DIR")
if _ce_scripts and _ce_scripts not in sys.path:
    sys.path.insert(0, _ce_scripts)


# --- frontmatter / body helpers ------------------------------------------


_FM_KEY_RE = re.compile(r"^([a-z_][a-z0-9_]*):\s*(.+?)\s*$", re.IGNORECASE)


def _raw_fm(text: str) -> dict[str, str]:
    """Parse top-level frontmatter without curiosity-engine's allowlist
    filter. Returns lowercased keys → string values."""
    out: dict[str, str] = {}
    if not text.startswith("---"):
        return out
    end = text.find("\n---", 3)
    if end == -1:
        return out
    for line in text[3:end].splitlines():
        if not line or line[0] in (" ", "\t"):
            continue
        m = _FM_KEY_RE.match(line)
        if m:
            k, v = m.group(1).strip().lower(), m.group(2).strip()
            if v and v[0] in ('"', "'") and v[-1] == v[0]:
                v = v[1:-1]
            out[k] = v
    return out


def _body(text: str) -> str:
    if not text.startswith("---"):
        return text
    end = text.find("\n---", 3)
    if end == -1:
        return text
    return text[end + 4:]


_FENCE_RE = re.compile(r"```[\s\S]*?```", re.MULTILINE)


def _fenced_blocks(body: str) -> list[str]:
    """Return the contents of every triple-backtick code fence in body."""
    return _FENCE_RE.findall(body)


# curiosity-engine's local_ingest.py wraps every vault extraction in
# `<!-- BEGIN FETCHED CONTENT --> ... <!-- END FETCHED CONTENT -->`. Text
# inside is the publisher's content (paper body, archived blog, etc.);
# text outside is the user's own provenance frontmatter, plus any notes
# they typed above or below the fetched block.
#
# The PII detector treats these regions asymmetrically (see
# `find_gdpr_likely_pii`): email/phone in fetched regions get density-
# scaled severity (a paper's author block is sparse → info; a leaked DB
# is dense → warn), while user regions are always warn (anything the
# user typed deserves close scrutiny). SSN/IBAN/payment-card detectors
# stay warn everywhere — those have no legitimate published form even
# inside an extraction.
_FETCHED_BLOCK_RE = re.compile(
    r"<!--\s*BEGIN FETCHED CONTENT\b.*?-->"
    r"(?P<inner>[\s\S]*?)"
    r"<!--\s*END FETCHED CONTENT\b.*?-->",
    re.MULTILINE,
)


def _split_fetched_user(text: str) -> tuple[str, str]:
    """Return (fetched_concat, user_concat) regions of body text.

    `fetched_concat` is every region inside FETCHED CONTENT markers,
    concatenated. `user_concat` is everything else (the body with
    fetched regions excised, plus the frontmatter — frontmatter is
    user-provenance, not source content).

    Malformed markers (BEGIN without END) cause us to *skip the
    fetched-region split entirely* and treat the whole body as user
    content. Better to over-flag than under-flag if the structure
    looks tampered with.
    """
    body_text = _body(text)
    # Reject malformed: count BEGIN vs END markers.
    begins = len(re.findall(r"<!--\s*BEGIN FETCHED CONTENT", body_text))
    ends = len(re.findall(r"<!--\s*END FETCHED CONTENT", body_text))
    if begins != ends:
        return "", body_text

    fetched_parts: list[str] = []
    user_parts: list[str] = []
    last_end = 0
    for m in _FETCHED_BLOCK_RE.finditer(body_text):
        user_parts.append(body_text[last_end:m.start()])
        fetched_parts.append(m.group("inner"))
        last_end = m.end()
    user_parts.append(body_text[last_end:])
    return "\n".join(fetched_parts), "\n".join(user_parts)


# --- 1. chain-merge detection -------------------------------------------


def find_non_native_pages(scope_pages: list[Path]) -> list[dict]:
    """Pages with a non-empty `origin:` tag came from a previous merge."""
    out: list[dict] = []
    for p in scope_pages:
        try:
            fm = _raw_fm(p.read_text(errors="replace"))
        except OSError:
            continue
        origin = fm.get("origin", "").strip()
        if origin:
            out.append({
                "kind": "non_native_page",
                "severity": "warn",
                "subject": str(p),
                "summary": f"page tagged origin: {origin}",
                "rationale": (
                    "This page came from a previous merge, not your own "
                    "curation. Re-publishing it in your subgraph-export "
                    "propagates someone else's content through your wiki. "
                    "They may not have consented to wider distribution. "
                    "Default behaviour is to exclude non-native pages; "
                    "pass --include-non-native to ship them anyway."
                ),
                "samples": [origin],
            })
    return out


# --- 2. quote-density lint ------------------------------------------------


_BLOCKQUOTE_RE = re.compile(r"^>+\s?(.*)$", re.MULTILINE)


def measure_quote_density(scope_pages: list[Path],
                           threshold: float = 0.25) -> list[dict]:
    out: list[dict] = []
    for p in scope_pages:
        try:
            text = _body(p.read_text(errors="replace"))
        except OSError:
            continue
        body_len = max(1, len(text.strip()))
        quoted_len = sum(len(m.group(0)) for m in _BLOCKQUOTE_RE.finditer(text))
        ratio = quoted_len / body_len
        if ratio >= threshold:
            out.append({
                "kind": "quote_density",
                "severity": "warn",
                "subject": str(p),
                "summary": f"{ratio:.0%} of body inside block quotes",
                "rationale": (
                    "When a wiki page is mostly quoted source text, "
                    "republishing it (even without the source PDF) can be "
                    "a republication of the publisher's prose. Fair use "
                    "analysis is jurisdiction-specific; review whether "
                    "your quotations are minimal, transformative, and "
                    "properly attributed before publishing. Threshold for "
                    f"this warning is {threshold:.0%}."
                ),
                "samples": [],  # ratio is in summary; no need to leak quoted text
            })
    return out


# --- 3. license consistency ----------------------------------------------


_PAYWALLED_DOMAINS = (
    "nature.com", "sciencedirect.com", "elsevier.com", "springer.com",
    "wiley.com", "ieee.org", "acm.org", "cell.com", "tandfonline.com",
    "sagepub.com", "jstor.org",
)
# Open licenses that permit unrestricted redistribution. NOTE (v0.2.1):
# CC-BY-NC and CC-BY-ND have been removed from the default list. NC
# forbids commercial use; ND forbids derivatives. The wiki's normal
# operation (extraction, classification, summarization, redistribution
# inside curiosity-engine workflows) may exceed both. Users with a
# specific use case that complies can opt back in via
# subgraph_export.py --allow-license-class.
_OPEN_LICENSE_TOKENS = {
    "cc0", "public-domain", "publicdomain",
    "cc-by", "cc-by-sa",
    "cc-by-3.0", "cc-by-4.0", "cc-by-sa-3.0", "cc-by-sa-4.0",
    "mit", "apache-2.0", "apache2", "bsd", "bsd-3-clause", "bsd-2-clause",
    "mpl-2.0",
    "arxiv-non-exclusive",
}
_NC_ND_TOKENS = {
    "cc-by-nc", "cc-by-nd", "cc-by-nc-sa", "cc-by-nc-nd",
    "cc-by-nc-3.0", "cc-by-nc-4.0",
    "cc-by-nd-3.0", "cc-by-nd-4.0",
    "cc-by-nc-sa-3.0", "cc-by-nc-sa-4.0",
    "cc-by-nc-nd-3.0", "cc-by-nc-nd-4.0",
}


def check_license_consistency(vault_files: list[Path]) -> list[dict]:
    """Flag vault files whose declared license disagrees with the
    publisher domain in their source_url."""
    out: list[dict] = []
    for p in vault_files:
        try:
            fm = _raw_fm(p.read_text(errors="replace"))
        except OSError:
            continue
        license_str = (fm.get("license") or "").lower().strip()
        source_url = (fm.get("source_url") or "").lower()
        if not license_str or not source_url:
            continue
        if license_str not in _OPEN_LICENSE_TOKENS:
            continue
        for dom in _PAYWALLED_DOMAINS:
            if dom in source_url:
                out.append({
                    "kind": "license_inconsistent",
                    "severity": "warn",
                    "subject": str(p),
                    "summary": (
                        f"declared license `{license_str}` but URL domain "
                        f"`{dom}` is typically paywalled"
                    ),
                    "rationale": (
                        "An open license declaration on a paywalled-"
                        "publisher URL is suspicious. Either the article "
                        "is genuinely OA at this publisher (some are; "
                        "verify and keep the tag), or the license tag is "
                        "wrong and the file should not be bundled. Review "
                        "before publishing."
                    ),
                    "samples": [],  # license + domain are already in summary
                })
                break
    return out


# --- 4. URL redaction ----------------------------------------------------


_SAFE_QUERY_KEYS = {"id", "v", "version", "doi"}


def redact_url(url: str, keep_params: bool = False) -> str:
    """Strip query strings and fragments from a source_url unless the
    caller opts in to keeping them, OR every query key is a known-safe
    canonical-id key (`?v=2`, `?doi=...`).
    """
    if not url:
        return url
    if keep_params:
        return url
    try:
        parsed = urlparse(url)
    except ValueError:
        return url
    if not parsed.query and not parsed.fragment:
        return url
    if parsed.query:
        kvs = [kv.split("=", 1)[0] for kv in parsed.query.split("&") if kv]
        if kvs and all(k.lower() in _SAFE_QUERY_KEYS for k in kvs):
            return urlunparse(parsed._replace(fragment=""))
    return urlunparse(parsed._replace(query="", fragment=""))


# --- 5. GPL contagion detection (strict, v0.2.1) -------------------------


# Two paths to a hit:
#   (a) SPDX identifier anywhere in the file. SPDX is a structured marker
#       authors emit deliberately; very low false-positive rate.
#   (b) GPL keyword inside a fenced code block. Code fences are how
#       authors paste in actual license headers from upstream code.
#
# We deliberately do NOT match GPL keywords in prose. v0.2.0 did, and it
# false-positived on every wiki page that *discusses* the GPL as a topic
# (Stallman bio, free-software history, license-comparison page). The
# rationale for tightening is that prose mentions don't trigger the
# copyleft obligation — only actual GPL'd content does, and that content
# almost always arrives via SPDX header or pasted code fence.

_SPDX_RE = re.compile(
    r"\bSPDX-License-Identifier:\s*(?P<id>(?:A?GPL|LGPL)[\w.\-+]*)",
    re.IGNORECASE,
)
_GPL_KW_RE = re.compile(
    r"\b(?:AGPL|LGPL|GPL)v?\d+(?:\.\d+)?(?:[-+]only|[-+]or-later)?\b"
)
_GPL_FULLNAME_RE = re.compile(
    r"\bGNU\s+(?:Lesser\s+|Affero\s+)?General\s+Public\s+License\b",
    re.IGNORECASE,
)


def find_gpl_contagion(files: list[Path]) -> list[dict]:
    """Detect GPL-family licensing markers that imply *applied* (not
    merely discussed) GPL-licensed content.

    Strategy:
      1. Frontmatter `license:` field with GPL-family value → strong hit.
      2. SPDX identifier anywhere → strong hit.
      3. GPL keyword inside a fenced code block → likely-real hit
         (someone pasted a license header).
      4. GPL keyword or full name in plain prose → IGNORED. Common in
         articles *about* free software; not evidence of contagion.
    """
    out: list[dict] = []
    for p in files:
        try:
            text = p.read_text(errors="replace")
        except OSError:
            continue

        fm = _raw_fm(text)
        body = _body(text)
        matched: list[str] = []
        evidence: str | None = None

        # (1) Frontmatter license field.
        fm_license = (fm.get("license") or "").lower().strip()
        if fm_license and (
            fm_license.startswith(("gpl", "agpl", "lgpl"))
            or fm_license in {"gpl-2.0", "gpl-3.0", "agpl-3.0",
                               "lgpl-2.1", "lgpl-3.0"}
        ):
            matched.append(fm_license)
            evidence = "frontmatter license:"

        # (2) SPDX identifier.
        for m in _SPDX_RE.finditer(text):
            matched.append(m.group(0))
            evidence = evidence or "SPDX identifier"
            break  # one SPDX is enough to trigger

        # (3) GPL keyword inside fenced code blocks.
        if not matched:
            for fence in _fenced_blocks(body):
                m = _GPL_KW_RE.search(fence) or _GPL_FULLNAME_RE.search(fence)
                if m:
                    matched.append(m.group(0))
                    evidence = "fenced code block"
                    break

        if not matched:
            continue

        out.append({
            "kind": "gpl_contagion",
            "severity": "warn",
            "subject": str(p),
            "summary": f"GPL-family license marker ({evidence})",
            "rationale": (
                "GPL/AGPL/LGPL content has copyleft (share-alike) "
                "requirements. If you republish this content in a wiki "
                "licensed differently (e.g. CC-BY), you may be violating "
                "the GPL. AGPL is particularly aggressive — network use "
                "of an AGPL'd component can trigger the share-alike "
                "clause. Either: (a) license your published wiki "
                "compatibly (GPL/AGPL), (b) remove the GPL'd content, "
                "or (c) confirm fair-use exception applies to your "
                "quotation. This rule fires only on SPDX identifiers, "
                "frontmatter license fields, or GPL markers inside "
                "fenced code blocks; bare prose mentions are ignored."
            ),
            "samples": matched,
        })
    return out


# --- 6. PII detection (v0.2.1: i18n email, E.164-only phone) -------------


# Email: broad enough for RFC 6531 internationalised addresses. We avoid
# unicode classes since the stdlib regex flavour is limited; instead we
# accept any non-whitespace, non-delimiter character as part of the
# local-part and domain.
_EMAIL_RE = re.compile(
    r"(?<![\w.+-])"
    r"[^\s<>\"'\(\)\[\],;:@]+"
    r"@"
    r"[^\s<>\"'\(\)\[\],;:@]+"
    r"\.[^\s<>\"'\(\)\[\],;:@]{2,}"
)
# RFC 6761 reserved test domains. Filter emails whose domain ends with
# any of these (or is exactly one of these). NOT a substring match —
# v0.2.0's filter was buggy (`__init__` was treated as an email noise
# hint, which made no sense).
_RESERVED_TEST_DOMAINS = (
    "example.com", "example.org", "example.net",
    "test.com", "test.local", "invalid", "localhost",
    "localhost.localdomain",
)
_RESERVED_TEST_TLDS = ("test", "example", "invalid", "localhost", "local")


def _is_test_email(addr: str) -> bool:
    domain = addr.rsplit("@", 1)[-1].lower().rstrip(".")
    for d in _RESERVED_TEST_DOMAINS:
        if domain == d or domain.endswith("." + d):
            return True
    tld = domain.rsplit(".", 1)[-1]
    if tld in _RESERVED_TEST_TLDS:
        return True
    return False


# E.164 international phone format: leading `+`, 8–15 digits, optional
# spaces/dashes between groups. v0.2.0's generic phone regex matched
# arXiv IDs, DOIs, ISBNs, and citation stems — useless on academic
# content. E.164 is the only globally unambiguous phone signal; local
# formats are inherently confusable with academic identifiers, so we
# drop them entirely. Documented limitation.
_PHONE_E164_RE = re.compile(
    r"(?<![\w+])"
    r"\+\d(?:[\s\-]?\d){7,14}"
    r"(?!\w)"
)
# US SSN: ddd-dd-dddd. Distinctive enough; rare to appear by accident.
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
# IBAN: 2 letters + 2 digits + 11–30 alphanum.
_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")
# Detection regex for digit strings *shaped like* a credit card number,
# so we can FLAG them as possible-PII before they ship in a published
# manifest. This skill never accepts, transmits, or stores payment data
# of any kind — the patterns below exist only to catch a number that
# looks like a card so the user is warned.
#
# We require a recognised issuer prefix (Visa 4, Mastercard 51–55,
# Amex 34/37, Discover 6011/65) so the regex doesn't false-positive on
# ISBN-13 (978-/979-) and other long numeric identifiers that appear in
# academic content. Total length is 13–19 digits with optional spaces
# or dashes between groups.
_CC_RE = re.compile(
    r"\b(?:"
    r"4[\s\-]?(?:\d[\s\-]?){12,18}"
    r"|5[1-5][\s\-]?(?:\d[\s\-]?){12,17}"
    r"|3[47][\s\-]?(?:\d[\s\-]?){11,16}"
    r"|6(?:011|5\d{2})[\s\-]?(?:\d[\s\-]?){8,13}"
    r")\d\b"
)


def _digit_count(s: str) -> int:
    return sum(1 for c in s if c.isdigit())


# Density threshold: matches per character. Above this, content looks
# like a directory/dump rather than an author/contact block.
#
# Calibration (v0.2.1):
#   - Standard 50K-char arXiv paper, 8 corresponding emails: 0.16/1000
#   - Big-collab 100K paper, 20 emails: 0.20/1000
#   - Conference TOC, 50 emails in 10K chars: 5.0/1000
#   - Leaked DB, 5000 emails in 100K chars: 50/1000
# 0.5/1000 sits comfortably between A-class (~0.2) and B-class (>=5).
_DENSITY_THRESHOLD_PER_CHAR = 0.5 / 1000.0
# Below this region length, density math is too noisy to trust — treat
# any match conservatively (warn). Prevents a 200-char extract with one
# email from getting a free pass via the density quirk.
_DENSITY_FLOOR_CHARS = 2000


def _is_dense(match_count: int, region_len: int) -> bool:
    if region_len < _DENSITY_FLOOR_CHARS:
        return True  # short region → conservative
    return (match_count / region_len) > _DENSITY_THRESHOLD_PER_CHAR


def _gdpr_scan_region(region: str) -> dict:
    """Run the per-region scans. Returns counts + samples per kind.
    Strips emails before phone scan so `+1` in `user+1@x.com` doesn't
    double-count.
    """
    emails = [m.group(0) for m in _EMAIL_RE.finditer(region)
               if not _is_test_email(m.group(0))]
    region_no_emails = _EMAIL_RE.sub("", region)
    phones = [m.group(0) for m in _PHONE_E164_RE.finditer(region_no_emails)]
    ssns = [m.group(0) for m in _SSN_RE.finditer(region)]
    ibans = [m.group(0) for m in _IBAN_RE.finditer(region)]
    ccs = [m.group(0) for m in _CC_RE.finditer(region)
            if _digit_count(m.group(0)) >= 13]
    return {
        "email": emails, "phone": phones,
        "ssn": ssns, "iban": ibans, "cc": ccs,
    }


def find_gdpr_likely_pii(files: list[Path]) -> list[dict]:
    """Scan body text for patterns that may be personal data.

    Detection categories (v0.2.1.1):
      - email   (RFC 6531-compatible local-part + domain;
                 reserved test domains filtered)
      - phone   (E.164 only — `+` prefix, 8–15 digits)
      - SSN     (US format ddd-dd-dddd)
      - IBAN    (international bank account number)
      - cc      (digit string SHAPED LIKE a payment card — used to
                 flag possible-PII; this skill never accepts/processes
                 card data)

    Severity model (new in v0.2.1.1 — fetched-content density awareness):

      Outside FETCHED CONTENT markers (user-typed content):
        Any match → WARN (always).

      Inside FETCHED CONTENT markers (publisher's text):
        SSN / IBAN / cc           → WARN (no legitimate published form)
        email / phone, dense      → WARN (looks like a directory/dump)
        email / phone, sparse     → INFO (looks like author/contact
                                          block in an academic paper)

    `dense` = (matches / region_len) > 0.5 per 1000 chars, with a
    2000-char floor below which we treat anything as dense (short
    regions don't get a free pass on a noisy ratio).

    Severity at the file level is the MAX across kinds — a paper with
    sparse author emails (info) plus one IBAN (warn) lands as warn.

    Why this design: an arXiv paper's corresponding-author block is
    published-by-consent and shows up on every academic vault file. A
    blanket "skip inside FETCHED markers" would produce 100% false-
    negative rate on actual leaks; a blanket "scan everywhere" produces
    100% noise rate on academic content. Density is the principled
    separator that lines up with the underlying privacy intuition (a
    handful of contact emails ≠ a directory dump).
    """
    out: list[dict] = []
    for p in files:
        try:
            text = p.read_text(errors="replace")
        except OSError:
            continue
        fetched, user = _split_fetched_user(text)

        user_hits = _gdpr_scan_region(user)
        fetched_hits = _gdpr_scan_region(fetched)
        fetched_len = len(fetched)

        # Build per-kind hit summaries with severity. Each entry is
        # (label, severity, count, samples).
        hit_lines: list[tuple[str, str, int, list[str]]] = []

        # email + phone: density-scaled in fetched, always-warn in user
        for kind in ("email", "phone"):
            u = user_hits[kind]
            f_ = fetched_hits[kind]
            if u:
                hit_lines.append((
                    kind, "warn", len(u), u[:5],
                ))
            if f_:
                if _is_dense(len(f_), fetched_len):
                    hit_lines.append((
                        f"{kind} (dense, in fetched content)",
                        "warn", len(f_), f_[:5],
                    ))
                else:
                    hit_lines.append((
                        f"{kind} (sparse, in author/contact block)",
                        "info", len(f_), f_[:5],
                    ))

        # SSN / IBAN / cc: always warn, anywhere.
        for kind, label in (("ssn", "US SSN"), ("iban", "IBAN"),
                             ("cc", "payment-card-like")):
            combined = user_hits[kind] + fetched_hits[kind]
            if combined:
                hit_lines.append((
                    label, "warn", len(combined), combined[:5],
                ))

        if not hit_lines:
            continue

        max_severity = "info"
        for _, sev, _, _ in hit_lines:
            if sev == "warn":
                max_severity = "warn"
                break

        # Summary uses counts + per-kind context (no values).
        summary = ", ".join(f"{c}×{label}" for label, _, c, _ in hit_lines)
        # Samples are local-only; manifest_safe() strips them.
        samples_flat: list[str] = []
        for label, _, _, samples in hit_lines:
            for s in samples:
                samples_flat.append(f"{label}: {s}")

        out.append({
            "kind": "gdpr_likely_pii",
            "severity": max_severity,
            "subject": str(p),
            "summary": f"possible personal data ({summary})",
            "rationale": (
                "These patterns may be real people's personal data "
                "(emails, phones, SSN, IBAN, payment-card-shaped). "
                "Severity model: matches outside FETCHED CONTENT "
                "markers are always WARN (user-typed content). Inside "
                "markers (publisher's text), SSN/IBAN/payment-card "
                "stay WARN; email/phone scale by density — sparse "
                "patterns (author/contact block in an academic paper) "
                "are INFO, dense patterns (directory or DB dump) are "
                "WARN. Density threshold is 0.5 matches per 1000 "
                "chars. Reserved test domains and academic identifiers "
                "(arXiv IDs, DOIs, ISBNs) are excluded by construction. "
                "Examples appear in local terminal output only and are "
                "NOT written to the export manifest."
            ),
            "samples": samples_flat,
        })
    return out


# --- aggregate runner + manifest-safety ----------------------------------


def run_all(*, scope_pages: list[Path], vault_files: list[Path],
            include_non_native: bool = False,
            quote_density_threshold: float = 0.25,
            enable_presidio: bool = False,
            presidio_entities: tuple[str, ...] | list[str] | None = None,
            presidio_confidence: float = 0.6,
            cache_dir: Path | None = None) -> list[dict]:
    """Convenience: run every detector and return a flat findings list.

    `include_non_native=True` suppresses the chain-merge finding for
    callers that legitimately ship non-native content (personal
    transfer, or merge-stage preflight where everything is foreign by
    definition).

    `enable_presidio=True` (v0.3.0) runs Microsoft Presidio's NER+ML
    pass *instead of* the regex GDPR detector — Presidio covers the
    same kinds (email, phone, SSN, IBAN, CC) plus richer entities
    (PERSON, LOCATION, driver license, passport, NRP). Skipping the
    regex detector when Presidio is on avoids double-counting; if
    Presidio isn't installed, the regex detector runs as fallback.
    """
    findings: list[dict] = []
    if not include_non_native:
        findings.extend(find_non_native_pages(scope_pages))
    findings.extend(measure_quote_density(scope_pages,
                                            threshold=quote_density_threshold))
    findings.extend(check_license_consistency(vault_files))
    targets = list(scope_pages) + list(vault_files)
    findings.extend(find_gpl_contagion(targets))

    presidio_used = False
    if enable_presidio:
        # Local import to avoid import-time spaCy load when not needed.
        sys.path.insert(0, str(Path(__file__).parent))
        import presidio_gate  # type: ignore
        available, reason = presidio_gate.is_available()
        if available:
            entities = (presidio_entities
                        if presidio_entities is not None
                        else presidio_gate.DEFAULT_ENTITIES)
            findings.extend(_run_presidio_with_cache(
                presidio_gate, targets,
                entities=tuple(entities),
                confidence=presidio_confidence,
                cache_dir=cache_dir,
            ))
            presidio_used = True
        else:
            sys.stderr.write(
                f"preflight: --enable-presidio set but unavailable "
                f"({reason}). Falling back to regex GDPR detector.\n"
            )

    if not presidio_used:
        # Regex baseline. Stays as the always-available fallback.
        findings.extend(find_gdpr_likely_pii(targets))

    return findings


# --- presidio caching helper ---------------------------------------------


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    try:
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def _run_presidio_with_cache(presidio_gate_mod, targets: list[Path],
                              *, entities: tuple[str, ...],
                              confidence: float,
                              cache_dir: Path | None) -> list[dict]:
    """Run Presidio per file with optional per-file result caching.

    Cache key is (file sha256, entity-list+confidence hash). Cache value
    is a manifest-safe finding (or sentinel for "no findings"). Samples
    are NEVER cached — they're discarded after the local terminal sees
    them, and the cache must not become a side-channel for them. On
    cache hit, the per-file finding has empty `samples`.

    If `cache_dir` is None, no caching; just analyze every file.
    """
    if cache_dir is None:
        return presidio_gate_mod.analyze_files(
            targets, entities=entities, confidence=confidence,
        )

    cache_dir.mkdir(parents=True, exist_ok=True)
    cfg_hash = presidio_gate_mod.cache_config_hash(entities, confidence)

    cached: list[dict] = []
    to_analyze: list[Path] = []
    cache_paths: dict[Path, Path] = {}

    for p in targets:
        sha = _file_sha256(p)
        if not sha:
            to_analyze.append(p)
            continue
        cache_path = cache_dir / f"{sha}-{cfg_hash}.json"
        cache_paths[p] = cache_path
        if cache_path.is_file():
            try:
                payload = json.loads(cache_path.read_text())
                if payload.get("found"):
                    # Hydrate as a finding with empty samples (cache
                    # never stores samples; rationale + summary are
                    # safe and stable).
                    cached.append({**payload["finding"], "samples": []})
                # else: cached miss, no finding to add
                continue
            except (json.JSONDecodeError, OSError):
                pass  # corrupt cache → re-analyze
        to_analyze.append(p)

    if to_analyze:
        fresh = presidio_gate_mod.analyze_files(
            to_analyze, entities=entities, confidence=confidence,
        )
    else:
        fresh = []
    fresh_by_subject = {f["subject"]: f for f in fresh}

    # Write cache entries for every analyzed file (hit and miss alike).
    for p in to_analyze:
        cache_path = cache_paths.get(p)
        if cache_path is None:
            continue
        finding = fresh_by_subject.get(str(p))
        if finding:
            payload = {
                "found": True,
                "finding": {k: v for k, v in finding.items()
                             if k != "samples"},
            }
        else:
            payload = {"found": False}
        try:
            cache_path.write_text(json.dumps(payload) + "\n")
        except OSError:
            pass  # cache write failures are non-fatal

    return cached + fresh


# --- manifest projection -------------------------------------------------


def manifest_safe(finding: dict) -> dict:
    """Strip local-only fields from a finding before manifest write.

    Returns a dict with only manifest-safe fields. The `samples` key is
    always removed. `summary` and `rationale` are passed through as-is —
    detectors are responsible for keeping those PII-free (the contract
    is enforced by tests).
    """
    return {
        "kind": finding.get("kind", ""),
        "severity": finding.get("severity", ""),
        "subject": finding.get("subject", ""),
        "summary": finding.get("summary", ""),
        "rationale": finding.get("rationale", ""),
    }


def manifest_summary(findings: list[dict]) -> list[dict]:
    """Aggregate findings into per-kind counts (no subjects, no samples).

    This is the default what-goes-in-the-manifest projection. Callers
    who want richer manifest data pass each finding through
    `manifest_safe()` instead.
    """
    counts: dict[tuple[str, str], int] = {}
    for f in findings:
        key = (f.get("kind", ""), f.get("severity", "warn"))
        counts[key] = counts.get(key, 0) + 1
    return [
        {"kind": k, "severity": s, "count": n}
        for (k, s), n in sorted(counts.items())
    ]


# --- formatter for local terminal display --------------------------------


def format_findings(findings: list[dict], *, show_samples: bool = True,
                    sample_limit: int = 5) -> str:
    """Human-readable summary for terminal display.

    `show_samples=True` includes matched values (emails, etc.) — only
    safe in a local terminal context, never for output that gets
    persisted alongside the export. `show_samples=False` mode is used
    in batch contexts (merge audit report) where samples might leak.
    """
    if not findings:
        return "preflight: no issues found.\n"
    by_kind: dict[str, list[dict]] = {}
    for f in findings:
        by_kind.setdefault(f["kind"], []).append(f)
    lines = [
        f"preflight: {len(findings)} finding(s) across "
        f"{len(by_kind)} detector(s):",
        "",
    ]
    for kind, items in by_kind.items():
        lines.append(f"  [{kind}] {len(items)} hit(s):")
        for f in items[:10]:
            lines.append(f"    - {f['subject']}: {f['summary']}")
            if show_samples and f.get("samples"):
                shown = f["samples"][:sample_limit]
                more = max(0, len(f["samples"]) - sample_limit)
                tail = f"  ... and {more} more" if more else ""
                lines.append(f"      examples (local only): "
                             + "; ".join(shown) + tail)
        if len(items) > 10:
            lines.append(f"    ... and {len(items) - 10} more")
        lines.append(f"    why: {items[0]['rationale']}")
        lines.append("")
    return "\n".join(lines)


# --- CLI for standalone audits (added v0.2.2; stub here) -----------------


if __name__ == "__main__":
    # Minimal CLI: scan a workspace, print findings, exit 0.
    # Full CLI surface (--workspace, --scope-pages, etc.) lands in v0.2.2.
    import argparse
    ap = argparse.ArgumentParser(
        prog="preflight.py",
        description="Run preflight detectors over a curiosity-engine workspace.",
    )
    ap.add_argument("--workspace", default=".")
    ap.add_argument("--quote-density-threshold", type=float, default=0.25)
    ap.add_argument("--no-samples", action="store_true",
                    help="omit local-only sample values (manifest-safe view)")
    args = ap.parse_args()
    ws = Path(args.workspace).resolve()
    wiki = ws / "wiki"
    vault = ws / "vault"
    if not wiki.is_dir():
        sys.stderr.write(f"no wiki/ at {ws}\n")
        sys.exit(2)
    pages = [p for p in wiki.rglob("*.md")
              if not any(s.startswith(".") for s in p.relative_to(wiki).parts)]
    vfs = [p for p in vault.rglob("*")
            if p.is_file()
            and not any(s.startswith(".") for s in p.relative_to(vault).parts)] \
            if vault.is_dir() else []
    findings = run_all(scope_pages=pages, vault_files=vfs,
                        quote_density_threshold=args.quote_density_threshold)
    sys.stdout.write(format_findings(findings, show_samples=not args.no_samples))
    sys.exit(0)
