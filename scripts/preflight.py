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


_BLOCKQUOTE_LINE_RE = re.compile(r"^>+\s?.*$", re.MULTILINE)
_CITATION_INLINE_RE = re.compile(r"\(vault:([^)]+)\)")


def _attribute_blockquotes(body: str) -> tuple[dict[str, int], int]:
    """Walk page body in document order, attributing each block-quote
    line to the nearest preceding `(vault:X)` citation. Returns:
      ({citation_path → quoted_chars}, total_quoted_chars)

    Blockquotes that appear before any citation in the page go into the
    `__unattributed__` bucket. Attribution scope is the entire page —
    a citation at the top of the page captures every subsequent
    blockquote until the next citation, which fits the common pattern
    of `## Vaswani 2017\\n(vault:...)\\n\\n> long quote` regardless of
    section/paragraph boundaries.
    """
    by_citation: dict[str, int] = {}
    # Build sorted list of (offset, kind, value):
    #   kind="cite" → value is the citation path
    #   kind="bq"   → value is the line length
    events: list[tuple[int, str, object]] = []
    for m in _CITATION_INLINE_RE.finditer(body):
        events.append((m.start(), "cite", m.group(1).strip()))
    for m in _BLOCKQUOTE_LINE_RE.finditer(body):
        events.append((m.start(), "bq", len(m.group(0))))
    events.sort(key=lambda e: (e[0], 0 if e[1] == "cite" else 1))

    current_citation = "__unattributed__"
    total = 0
    for _, kind, value in events:
        if kind == "cite":
            current_citation = str(value)
        else:  # bq
            length = int(value)  # type: ignore[arg-type]
            by_citation[current_citation] = (
                by_citation.get(current_citation, 0) + length
            )
            total += length
    return by_citation, total


def measure_quote_density(scope_pages: list[Path],
                           threshold: float = 0.25,
                           page_threshold: float = 0.50) -> list[dict]:
    """Two thresholds, two finding kinds:

    1. **Single-source concentration** — any one citation (or the
       unattributed bucket) contributes >= `threshold` of the page
       body. Subject is the citation path; one finding per offending
       citation per page. This catches `>25% from a single paper` even
       on a page with mixed sources where the page-level total looks
       fine.

    2. **Page-level aggregate** — total quoted chars >= `page_threshold`
       of body, across all citations. Subject is the page path. Catches
       `60% of body is quoted text` regardless of how many sources it's
       spread across.

    Both are warn-level. A page can produce both findings on the same
    run; the user sees them as separate concerns.
    """
    out: list[dict] = []
    for p in scope_pages:
        try:
            text = _body(p.read_text(errors="replace"))
        except OSError:
            continue
        body_len = max(1, len(text.strip()))
        by_citation, total_quoted = _attribute_blockquotes(text)

        # Per-citation findings.
        for citation, quoted_len in sorted(by_citation.items()):
            ratio = quoted_len / body_len
            if ratio < threshold:
                continue
            shown_source = (
                "(unattributed quotes)" if citation == "__unattributed__"
                else f"vault:{citation}"
            )
            out.append({
                "kind": "quote_density",
                "severity": "warn",
                "subject": f"{p} — {shown_source}",
                "summary": (
                    f"{ratio:.0%} of {p.name} body block-quoted from "
                    f"{shown_source}"
                ),
                "rationale": (
                    "A wiki page that quotes heavily from a single "
                    "source republishes that source's prose. Fair-use "
                    "analysis is jurisdiction-specific; review whether "
                    "your quotation is minimal, transformative, and "
                    "properly attributed before publishing. Single-"
                    f"source threshold for this warning is {threshold:.0%}."
                ),
                "samples": [],  # quoted text not sampled — published anyway
            })

        # Page-level aggregate finding (independent of single-source).
        page_ratio = total_quoted / body_len
        if page_ratio >= page_threshold:
            out.append({
                "kind": "quote_density",
                "severity": "warn",
                "subject": str(p),
                "summary": (
                    f"{page_ratio:.0%} of body block-quoted "
                    f"(across all sources)"
                ),
                "rationale": (
                    "Even when no single source dominates, a page that "
                    "is mostly quoted text from many sources is still "
                    "mostly quoted text. Review whether the user's own "
                    "synthesis is sufficient or whether the page is "
                    "essentially a quotation collage. Page-level "
                    f"threshold for this warning is {page_threshold:.0%}."
                ),
                "samples": [],
            })
    return out


# --- 3. license consistency ----------------------------------------------


_PAYWALLED_DOMAINS = (
    "nature.com", "sciencedirect.com", "elsevier.com", "springer.com",
    "wiley.com", "ieee.org", "acm.org", "cell.com", "tandfonline.com",
    "sagepub.com", "jstor.org",
)
# Known open-access publisher domains. The reverse check (restrictive
# license declared + URL on one of these) flags as info-severity: the
# user is being more conservative than necessary, the tag is probably
# wrong. Conservative list — better to miss than to be wrong about
# whether something is OA.
_OA_DOMAINS = (
    "arxiv.org", "biorxiv.org", "chemrxiv.org", "medrxiv.org",
    "plos.org",
    "ncbi.nlm.nih.gov/pmc", "europepmc.org",
    "openreview.net", "aclanthology.org",
    "doaj.org",
)
# Tokens that signal "the author hasn't declared an open license" or
# explicitly restrictive. Empty string also counts (no `license:`
# field at all).
_RESTRICTIVE_LICENSE_TOKENS = {
    "", "all-rights-reserved", "all rights reserved",
    "proprietary", "copyrighted", "©", "(c)", "unknown",
    "none", "n/a",
}
# Open licenses that permit unrestricted redistribution. NOTE (v0.2.1):
# CC-BY-NC and CC-BY-ND have been removed from the default list. NC
# forbids commercial use; ND forbids derivatives. The wiki's normal
# operation (extraction, classification, summarization, redistribution
# inside curiosity-engine workflows) may exceed both. Users with a
# specific use case that complies can opt back in via
# subgraph_export.py --allow-license-class.
_OPEN_LICENSE_TOKENS = {
    # Mirrors subgraph_export._REDISTRIBUTABLE_LICENSES — kept in sync
    # because the license-consistency check uses this set to decide
    # "the license tag claims open access" before flagging on a paywalled
    # URL. If the two diverge intentionally one day, document why.
    "cc0", "public-domain", "publicdomain",
    "unlicense",
    "0bsd", "bsd-0",
    "cc-by", "cc-by-sa",
    "cc-by-1.0", "cc-by-2.0", "cc-by-2.5",
    "cc-by-3.0", "cc-by-4.0",
    "cc-by-sa-1.0", "cc-by-sa-2.0", "cc-by-sa-2.5",
    "cc-by-sa-3.0", "cc-by-sa-4.0",
    "gfdl", "gfdl-1.2", "gfdl-1.3",
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


def _domain_match(source_url: str, domains: tuple[str, ...]) -> str | None:
    """Return the matching domain string, or None."""
    for dom in domains:
        if dom in source_url:
            return dom
    return None


def check_license_consistency(vault_files: list[Path]) -> list[dict]:
    """Flag vault files whose declared license disagrees with the
    publisher domain in their `source_url`.

    Two directions:

    1. **Open license + paywalled domain** (warn, the riskier case).
       Declared CC-BY etc. but URL is on Nature/Elsevier/Wiley/etc.
       Either the URL is mislabeled or the license tag is wrong; with
       --include-vault=owned this file would be shipped under a
       potentially incorrect entitlement.

    2. **Restrictive license + OA domain** (info, the safer case).
       Declared `all-rights-reserved` or no license at all, but URL
       is on arXiv/PLOS/PMC/etc. The user is being more conservative
       than necessary; nothing leaks, but the tag is probably wrong.
       Surfacing as info lets them fix the metadata without gating
       the export.
    """
    out: list[dict] = []
    for p in vault_files:
        try:
            fm = _raw_fm(p.read_text(errors="replace"))
        except OSError:
            continue
        license_str = (fm.get("license") or "").lower().strip()
        source_url = (fm.get("source_url") or "").lower()
        if not source_url:
            continue

        # Direction 1: open license declared + paywalled domain → warn.
        if license_str in _OPEN_LICENSE_TOKENS:
            dom = _domain_match(source_url, _PAYWALLED_DOMAINS)
            if dom:
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
                    "samples": [],
                })
                continue  # one finding per file

        # Direction 2: restrictive (or absent) license + OA domain → info.
        # Carve out arxiv-non-exclusive on arXiv: that's the correct,
        # author-retained license for an arXiv preprint, not a tagging
        # mistake. Same for biorxiv/chemrxiv/medrxiv URLs without an
        # explicit license — the platform default is implicit.
        looks_restrictive = (
            license_str in _RESTRICTIVE_LICENSE_TOKENS
            or (license_str
                and license_str not in _OPEN_LICENSE_TOKENS)
        )
        # Don't flag empty-license arXiv URLs — empty is the common case
        # and "info: probably arxiv-non-exclusive" would fire on every
        # academic vault file. Restrict the empty-license flag to
        # paywalled domains (already caught above) or non-preprint OA.
        if license_str == "" and _domain_match(
            source_url, ("arxiv.org", "biorxiv.org", "chemrxiv.org",
                          "medrxiv.org")
        ):
            continue
        if looks_restrictive:
            dom = _domain_match(source_url, _OA_DOMAINS)
            if dom:
                shown_license = license_str if license_str else "(none)"
                out.append({
                    "kind": "license_inconsistent",
                    "severity": "info",
                    "subject": str(p),
                    "summary": (
                        f"declared license `{shown_license}` but URL "
                        f"domain `{dom}` is open-access"
                    ),
                    "rationale": (
                        "The source URL is on a known open-access "
                        "publisher, but the license tag is missing or "
                        "restrictive. Nothing in your export leaks "
                        "(you're being more conservative than the "
                        "license requires), but the metadata is "
                        "probably wrong — fixing it would let "
                        "--include-vault=owned ship this file. Verify "
                        "the actual license at the source and update "
                        "the file's `license:` frontmatter."
                    ),
                    "samples": [],
                })
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


# --- known-kind registry + flag parsing (v0.4.0) -------------------------


# Authoritative list of detector kinds emitted by run_all(). Used to
# validate --refuse-on / --accept-on values at argparse time so typos
# fail loudly instead of silently disabling gating. Update this when
# adding a new detector.
KNOWN_FINDING_KINDS = (
    "non_native_page",
    "quote_density",
    "license_inconsistent",
    "gpl_contagion",
    "gdpr_likely_pii",
    "gdpr_combined_inference",  # v0.5.0: Presidio combined-data inference
)


class GatingPolicy:
    """Resolved gating policy from --refuse-on / --accept-on.

    The two value spaces (`all` / `none` / CSV-of-kinds) are compiled
    here once at startup and consulted per finding. Conflicts (same
    kind in both refuse and accept CSVs, or both `all`) are caught
    during construction and raise SystemExit at argparse time so the
    error surfaces before any work happens.
    """

    REFUSE = "refuse"
    ACCEPT = "accept"
    PROMPT = "prompt"

    def __init__(self, *, refuse_value: str, accept_value: str):
        self._refuse_all, self._refuse_kinds = self._compile(
            refuse_value, "refuse-on"
        )
        self._accept_all, self._accept_kinds = self._compile(
            accept_value, "accept-on"
        )
        # Conflict 1: same kind in both refuse and accept CSVs.
        overlap = self._refuse_kinds & self._accept_kinds
        if overlap:
            raise SystemExit(
                "preflight: --refuse-on and --accept-on both contain "
                f"{sorted(overlap)}. Pick one disposition per kind."
            )
        # Conflict 2: both set to all.
        if self._refuse_all and self._accept_all:
            raise SystemExit(
                "preflight: --refuse-on=all and --accept-on=all are "
                "contradictory. Pick one."
            )
        # Carve-out / carve-in cases (e.g. --refuse-on=all
        # --accept-on=k1) are valid; the resolution rule handles them
        # via the "more specific wins" preference encoded in decide().

    @staticmethod
    def _compile(value: str, flag_name: str) -> tuple[bool, set[str]]:
        """Parse one flag value. Returns (is_all, kind_set).

        - 'none' (default) → (False, set())
        - 'all' → (True, set())
        - 'k1,k2,...' → (False, {k1, k2, ...}) with kind validation
        - empty string → error
        """
        v = (value or "").strip()
        if not v:
            raise SystemExit(
                f"preflight: --{flag_name} requires a value "
                "(`all`, `none`, or a comma-separated kind list)"
            )
        if v.lower() == "none":
            return False, set()
        if v.lower() == "all":
            return True, set()
        kinds = {k.strip() for k in v.split(",") if k.strip()}
        if not kinds:
            raise SystemExit(
                f"preflight: --{flag_name}={value!r} parses to no kinds"
            )
        unknown = kinds - set(KNOWN_FINDING_KINDS)
        if unknown:
            raise SystemExit(
                f"preflight: --{flag_name} got unknown kind(s) "
                f"{sorted(unknown)}. Known kinds: "
                f"{', '.join(KNOWN_FINDING_KINDS)}"
            )
        return False, kinds

    def decide(self, finding: dict) -> str:
        """Return one of REFUSE / ACCEPT / PROMPT for a single finding.

        Severity gating: info findings are never refused or auto-
        accepted via the policy — they're treated as prompt-able only,
        and the caller's normal severity-aware UX (info-only proceeds
        without prompt) handles them. Refuse/accept apply to warn/block
        only.
        """
        if finding.get("severity") not in ("warn", "block"):
            return self.PROMPT
        kind = finding.get("kind", "")
        # Specific-kind decisions take precedence over `all`. This is
        # how carve-out (--refuse-on=all --accept-on=k1) and carve-in
        # (--refuse-on=k1 --accept-on=all) work intuitively.
        if kind in self._accept_kinds:
            return self.ACCEPT
        if kind in self._refuse_kinds:
            return self.REFUSE
        if self._refuse_all:
            return self.REFUSE
        if self._accept_all:
            return self.ACCEPT
        return self.PROMPT

    def is_default(self) -> bool:
        """True when no gating is configured — every warn/block finding
        falls through to PROMPT."""
        return (not self._refuse_all and not self._refuse_kinds
                and not self._accept_all and not self._accept_kinds)


# --- finding ack store (v0.4.0) ------------------------------------------


# Persisted at .curator/preflight-acks.json. The ack key is
# sha256(file_sha256 + kind + summary). File-content drift invalidates
# acks naturally (the file's sha256 changes). Detector summary changes
# (e.g. count differs) also invalidate. Both are correct behaviours:
# the user reviewed a specific situation; if either the content or the
# detector's reading of it changes, re-review is warranted.

ACK_FILE_NAME = "preflight-acks.json"
ACK_FILE_SCHEMA_VERSION = 1


def ack_id(file_sha256: str, kind: str, summary: str) -> str:
    """Stable identifier for a (file content, detector kind, summary)
    triple. Used as the dedup key in the ack store."""
    payload = "".join((file_sha256, kind, summary))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _ack_path(workspace: Path) -> Path:
    return workspace / ".curator" / ACK_FILE_NAME


def load_acks(workspace: Path) -> dict[str, dict]:
    """Return acks indexed by ack_id. Empty dict if no ack file or
    file is corrupt (logged warning, treated as no-acks)."""
    p = _ack_path(workspace)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError) as e:
        sys.stderr.write(
            f"preflight: ack file at {p} is unreadable ({e}); "
            "treating as no-acks. Inspect or delete to clear.\n"
        )
        return {}
    return {entry["ack_id"]: entry
             for entry in data.get("acks", [])
             if isinstance(entry, dict) and entry.get("ack_id")}


def save_acks(workspace: Path, acks: dict[str, dict]) -> None:
    """Atomically write the ack store. Best-effort — any write failure
    is logged but doesn't propagate (the user's export shouldn't fail
    because we couldn't persist an ack)."""
    p = _ack_path(workspace)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": ACK_FILE_SCHEMA_VERSION,
        "acks": sorted(acks.values(), key=lambda a: a.get("acked_at", "")),
    }
    try:
        # Write to a sibling temp file then rename, so a crashed write
        # doesn't truncate the existing ack file.
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        tmp.replace(p)
    except OSError as e:
        sys.stderr.write(
            f"preflight: failed to persist acks at {p}: {e}\n"
        )


def file_sha256(path: Path) -> str:
    """sha256 of a file's bytes. Returns empty string on read failure
    (which causes the ack-key mechanism to skip this file — preferable
    to silent collisions on '')."""
    return _file_sha256(path)


def attach_ack_ids(findings: list[dict]) -> list[dict]:
    """Annotate each finding with an `ack_id` field computed from
    (file sha256 of subject, kind, summary). Returns the same list,
    mutated in place. Findings with empty subjects are skipped (no
    ack key)."""
    for f in findings:
        subj = f.get("subject", "")
        # Subjects may include a trailing "— vault:..." marker for
        # quote_density per-citation findings; the underlying file is
        # the part before " — ".
        path_str = subj.split(" — ", 1)[0] if " — " in subj else subj
        if not path_str:
            f["ack_id"] = ""
            continue
        sha = _file_sha256(Path(path_str))
        if not sha:
            f["ack_id"] = ""
            continue
        f["ack_id"] = ack_id(sha, f.get("kind", ""), f.get("summary", ""))
    return findings


def filter_acked(findings: list[dict],
                  acks: dict[str, dict]) -> tuple[list[dict], list[dict]]:
    """Split findings into (live, suppressed). A finding is suppressed
    if its `ack_id` is present in the acks dict.

    Caller is expected to have run `attach_ack_ids` first; findings
    without `ack_id` (no readable subject path) are always live.
    """
    live: list[dict] = []
    suppressed: list[dict] = []
    for f in findings:
        aid = f.get("ack_id", "")
        if aid and aid in acks:
            suppressed.append(f)
        else:
            live.append(f)
    return live, suppressed


def record_ack(acks: dict[str, dict], finding: dict, *,
                ack_reason: str = "") -> None:
    """Add a finding to the acks dict (in place). The finding must
    already have an `ack_id` (via attach_ack_ids). The ack stores
    only manifest-safe metadata — no samples — so the persisted file
    can never become a side-channel for matched values.
    """
    aid = finding.get("ack_id", "")
    if not aid:
        return
    subj = finding.get("subject", "")
    path_str = subj.split(" — ", 1)[0] if " — " in subj else subj
    sha = _file_sha256(Path(path_str)) if path_str else ""
    import datetime as _dt
    acks[aid] = {
        "ack_id": aid,
        "subject": subj,
        "file_sha256": sha,
        "kind": finding.get("kind", ""),
        "summary": finding.get("summary", ""),
        "severity": finding.get("severity", ""),
        "acked_at": _dt.datetime.now(_dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "ack_reason": ack_reason,
    }


# --- aggregate runner + manifest-safety ----------------------------------


def run_all(*, scope_pages: list[Path], vault_files: list[Path],
            include_non_native: bool = False,
            quote_density_threshold: float = 0.25,
            enable_presidio: bool = False,
            presidio_entities: tuple[str, ...] | list[str] | None = None,
            presidio_confidence: float = 0.6,
            presidio_languages: tuple[str, ...] | list[str] | None = None,
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
        languages = tuple(presidio_languages) if presidio_languages else \
            presidio_gate.DEFAULT_LANGUAGES
        available, reason = presidio_gate.is_available(languages)
        if available:
            entities = (presidio_entities
                        if presidio_entities is not None
                        else presidio_gate.DEFAULT_ENTITIES)
            findings.extend(_run_presidio_with_cache(
                presidio_gate, targets,
                entities=tuple(entities),
                confidence=presidio_confidence,
                languages=languages,
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
                              languages: tuple[str, ...] | None = None,
                              cache_dir: Path | None = None) -> list[dict]:
    """Run Presidio per file with optional per-file result caching.

    Cache key is (file sha256, entity-list+confidence+languages hash).
    Cache value is a manifest-safe finding (or sentinel for "no
    findings"). Samples are NEVER cached — they're discarded after the
    local terminal sees them, and the cache must not become a
    side-channel for them. On cache hit, the per-file finding has empty
    `samples`.

    If `cache_dir` is None, no caching; just analyze every file.
    """
    langs = languages or presidio_gate_mod.DEFAULT_LANGUAGES
    if cache_dir is None:
        return presidio_gate_mod.analyze_files(
            targets, entities=entities, confidence=confidence,
            languages=langs,
        )

    cache_dir.mkdir(parents=True, exist_ok=True)
    cfg_hash = presidio_gate_mod.cache_config_hash(
        entities, confidence, languages=langs,
    )

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
                # Cache schema v2 (v0.5.0): a file can produce multiple
                # findings (e.g. gdpr_likely_pii + gdpr_combined_inference).
                # Backwards-compat: old payloads with `finding` (singular)
                # are loaded as a one-element list.
                fi = payload.get("findings")
                if fi is None and payload.get("found"):
                    fi = [payload["finding"]]
                for f in fi or []:
                    # Hydrate with empty samples (cache never stores them).
                    cached.append({**f, "samples": []})
                continue
            except (json.JSONDecodeError, OSError):
                pass  # corrupt cache → re-analyze
        to_analyze.append(p)

    if to_analyze:
        fresh = presidio_gate_mod.analyze_files(
            to_analyze, entities=entities, confidence=confidence,
            languages=langs,
        )
    else:
        fresh = []

    # Group findings by file path (subject path; combined-inference
    # findings carry the file path as their subject too).
    fresh_by_subject: dict[str, list[dict]] = {}
    for f in fresh:
        subj = f["subject"]
        # Strip the "— citation" suffix the quote-density detector uses;
        # cache is keyed by file, not citation. Presidio findings don't
        # use that pattern, so this is defensive only.
        path_part = subj.split(" — ", 1)[0]
        fresh_by_subject.setdefault(path_part, []).append(f)

    # Write cache entries for every analyzed file (hit and miss alike).
    for p in to_analyze:
        cache_path = cache_paths.get(p)
        if cache_path is None:
            continue
        findings_for_file = fresh_by_subject.get(str(p), [])
        payload = {
            "findings": [
                {k: v for k, v in f.items() if k != "samples"}
                for f in findings_for_file
            ],
        }
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


def _cli_main(argv: list[str] | None = None) -> int:
    """Standalone audit command for preflight.

    Read-only: scans a workspace, prints findings, never writes to the
    cache or ack store. Subgraph-export and merge are the canonical
    paths for those side effects; this command is for "does my wiki
    have any issues right now?" before pushing.

    Exit codes:
      0 — no findings, or only info-level (clean enough to ship)
      1 — at least one warn/block finding (suitable for CI gating)
      2 — operational error (no wiki/, etc.)
    """
    import argparse
    ap = argparse.ArgumentParser(
        prog="preflight.py",
        description="Audit a curiosity-engine workspace for licensing / "
                    "PII / quote-density issues. Read-only; no cache or "
                    "ack writes (use subgraph_export.py for those).",
    )
    ap.add_argument("--workspace", default=".",
                    help="workspace root containing wiki/ and vault/ "
                         "(default: cwd)")
    ap.add_argument("--scope", action="append", default=None, metavar="PATH",
                    help="restrict analysis to these specific files "
                         "(repeat for multiple). If omitted, scans every "
                         "file under wiki/ and vault/.")
    ap.add_argument("--include-non-native", action="store_true",
                    help="include pages with origin: tags in scope "
                         "(default: included for audit, since the user "
                         "wants to see the full picture)")
    ap.add_argument("--quote-density-threshold", type=float, default=0.25,
                    help="single-source quote density threshold (default 0.25)")
    ap.add_argument("--quote-density-page-threshold", type=float, default=0.50,
                    help="page-level aggregate quote density threshold "
                         "(default 0.50)")
    ap.add_argument("--enable-presidio", action="store_true",
                    help="run Microsoft Presidio (NER + ML PII detection) "
                         "instead of the regex baseline. Requires "
                         "presidio-analyzer installed.")
    ap.add_argument("--presidio-entities", default="", metavar="CSV",
                    help="Presidio entity types (default: curated PII set)")
    ap.add_argument("--presidio-confidence", type=float, default=0.6,
                    help="Presidio score threshold (default 0.6)")
    ap.add_argument("--presidio-language", default="en", metavar="CSV",
                    help="comma-separated language codes (default: en). "
                         "Each requires a spaCy model installed locally.")
    ap.add_argument("--show-acks", action="store_true",
                    help="print the workspace ack table and exit")
    ap.add_argument("--clear-acks", action="store_true",
                    help="clear the ack file (with --confirm-clear)")
    ap.add_argument("--confirm-clear", action="store_true",
                    help="auto-confirm --clear-acks (no prompt; use in scripts)")
    ap.add_argument("--no-samples", action="store_true",
                    help="omit local-only sample values from output "
                         "(manifest-safe view; useful for sharing audit "
                         "logs with collaborators without leaking values)")
    ap.add_argument("--json", dest="as_json", action="store_true",
                    help="emit findings as JSON (manifest-safe; samples "
                         "are always stripped in this mode)")
    args = ap.parse_args(argv)

    ws = Path(args.workspace).resolve()

    # Management modes short-circuit.
    if args.show_acks:
        acks = load_acks(ws)
        if args.as_json:
            sys.stdout.write(json.dumps(
                list(acks.values()), indent=2, sort_keys=True,
            ) + "\n")
        else:
            if not acks:
                sys.stdout.write(f"preflight: no acks at {ws}\n")
            else:
                sys.stdout.write(f"preflight: {len(acks)} ack(s):\n\n")
                for entry in sorted(acks.values(),
                                     key=lambda a: a.get("acked_at", "")):
                    for k in ("ack_id", "subject", "kind",
                               "summary", "acked_at"):
                        sys.stdout.write(
                            f"  {k:9}: {entry.get(k, '')}\n"
                        )
                    sys.stdout.write("\n")
        return 0

    if args.clear_acks:
        ack_path = ws / ".curator" / ACK_FILE_NAME
        if not ack_path.is_file():
            sys.stdout.write(f"preflight: nothing to clear at {ack_path}\n")
            return 0
        n = len(load_acks(ws))
        if not args.confirm_clear:
            if not sys.stdin.isatty():
                sys.stderr.write(
                    f"preflight: would clear {n} ack(s); pass "
                    "--confirm-clear to proceed.\n"
                )
                return 2
            sys.stdout.write(f"Clear {n} ack(s) at {ack_path}? [y/N] ")
            sys.stdout.flush()
            ans = (sys.stdin.readline() or "").strip().lower()
            if ans not in ("y", "yes"):
                sys.stdout.write("preflight: cancelled\n")
                return 0
        try:
            ack_path.unlink()
        except OSError as e:
            sys.stderr.write(f"preflight: could not remove {ack_path}: {e}\n")
            return 2
        sys.stdout.write(f"preflight: cleared {n} ack(s)\n")
        return 0

    # Audit mode.
    wiki = ws / "wiki"
    vault = ws / "vault"
    if not wiki.is_dir():
        sys.stderr.write(f"preflight: no wiki/ at {ws}\n")
        return 2

    if args.scope:
        scope_pages = []
        scope_vault = []
        for raw in args.scope:
            p = Path(raw).resolve()
            if not p.is_file():
                sys.stderr.write(f"preflight: not a file: {raw}\n")
                return 2
            try:
                rel = p.relative_to(wiki)
                scope_pages.append(p)
                continue
            except ValueError:
                pass
            try:
                rel = p.relative_to(vault) if vault.is_dir() else None
                if rel is not None:
                    scope_vault.append(p)
                    continue
            except ValueError:
                pass
            sys.stderr.write(
                f"preflight: {raw} is not inside wiki/ or vault/\n"
            )
            return 2
        pages = scope_pages
        vfs = scope_vault
    else:
        pages = [p for p in wiki.rglob("*.md")
                  if not any(s.startswith(".")
                              for s in p.relative_to(wiki).parts)]
        vfs = ([p for p in vault.rglob("*")
                if p.is_file()
                and not any(s.startswith(".")
                             for s in p.relative_to(vault).parts)]
               if vault.is_dir() else [])

    presidio_entities = (
        tuple(e.strip() for e in args.presidio_entities.split(",")
              if e.strip())
        if args.presidio_entities else None
    )
    presidio_languages = tuple(
        lang.strip() for lang in args.presidio_language.split(",")
        if lang.strip()
    ) or ("en",)
    findings = run_all(
        scope_pages=pages, vault_files=vfs,
        include_non_native=args.include_non_native,
        quote_density_threshold=args.quote_density_threshold,
        enable_presidio=args.enable_presidio,
        presidio_entities=presidio_entities,
        presidio_confidence=args.presidio_confidence,
        presidio_languages=presidio_languages,
        cache_dir=None,  # read-only audit; never write the export cache
    )

    # Apply existing acks (read-only: never persist new acks here).
    acks = load_acks(ws)
    attach_ack_ids(findings)
    findings, suppressed = filter_acked(findings, acks)
    if suppressed and not args.as_json:
        sys.stderr.write(
            f"preflight: {len(suppressed)} finding(s) suppressed by "
            "previous acks\n"
        )

    if args.as_json:
        # JSON mode: always manifest-safe (samples never serialised).
        payload = [manifest_safe(f) for f in findings]
        sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(format_findings(
            findings, show_samples=not args.no_samples,
        ))

    # Exit code: 0 on clean/info-only, 1 on any warn/block.
    has_warn = any(f.get("severity") in ("warn", "block") for f in findings)
    return 1 if has_warn else 0


if __name__ == "__main__":
    sys.exit(_cli_main())
