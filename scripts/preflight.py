#!/usr/bin/env python3
"""preflight.py — pre-publication detectors for subgraph-export.

Each detector is a pure function: takes the inputs it needs, returns a
list of `Finding` dicts. No I/O outside of reading files the caller hands
us. subgraph_export.py runs these before write and surfaces them with
rationale; the user accepts/refuses/overrides interactively.

Findings:
    {
      "kind":     <detector id>,
      "severity": "info" | "warn" | "block",
      "subject":  <path or url or stem>,
      "summary":  <one-line human description>,
      "rationale": <why this matters, plain-language>,
    }

Detectors here are deliberately conservative — false positives are
recoverable (user overrides), false negatives are not (a missed GDPR
issue ships to the public).

Detectors:
  - find_non_native_pages      — chain-merge contamination
  - measure_quote_density      — pages dominated by quoted source text
  - check_license_consistency  — declared license disagrees with URL domain
  - redact_url                 — strip query strings from source URLs
  - find_gpl_contagion         — GPL/AGPL/LGPL license markers in content
  - find_gdpr_likely_pii       — email/phone/SSN/etc patterns

The last two ship as best-effort heuristics. They surface candidates
with rationale; they don't auto-redact. Real legal review is the user's.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse, urlunparse

_ce_scripts = os.environ.get("CURIOSITY_ENGINE_SCRIPTS_DIR")
if _ce_scripts and _ce_scripts not in sys.path:
    sys.path.insert(0, _ce_scripts)


# Local raw-frontmatter probe (curiosity-engine's read_frontmatter
# strips unknown keys, which include `license`/`origin` for our purposes
# in some contexts). Keep this self-contained.
_FM_KEY_RE = re.compile(r"^([a-z_][a-z0-9_]*):\s*(.+?)\s*$", re.IGNORECASE)


def _raw_fm(text: str) -> dict[str, str]:
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


# --- 1. chain-merge detection ---------------------------------------------


def find_non_native_pages(scope_pages: list[Path]) -> list[dict]:
    """Pages with a non-empty `origin:` tag came from a previous merge.
    Re-publishing them in our own subgraph-export is a chain-merge
    propagation problem the original publisher of those pages may not
    have consented to."""
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
                    f"This page came from a previous merge of `{origin}`'s "
                    "wiki, not your own curation. Re-publishing it in your "
                    "subgraph-export propagates someone else's content "
                    "through your wiki. They may not have consented to "
                    "wider distribution. Default behaviour is to exclude "
                    "non-native pages; pass --include-non-native to ship "
                    "them anyway."
                ),
            })
    return out


# --- 2. quote-density lint ------------------------------------------------


_BLOCKQUOTE_RE = re.compile(r"^>+\s?(.*)$", re.MULTILINE)


def measure_quote_density(scope_pages: list[Path],
                           threshold: float = 0.25) -> list[dict]:
    """Pages where >threshold of body characters are inside markdown
    block quotes (lines starting with `>`).

    A high ratio suggests the page is mostly quoted source text rather
    than the user's own analysis. Republishing such a page raises
    fair-use questions even if the source bytes don't ship.
    """
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
                    "republishing it (even without the source PDF) can be a "
                    "republication of the publisher's prose. Fair use "
                    "analysis is jurisdiction-specific; review whether your "
                    "quotations are minimal, transformative, and properly "
                    "attributed before publishing. Threshold for this "
                    f"warning is {threshold:.0%}."
                ),
            })
    return out


# --- 3. license consistency ----------------------------------------------


_PAYWALLED_DOMAINS = (
    "nature.com", "sciencedirect.com", "elsevier.com", "springer.com",
    "wiley.com", "ieee.org", "acm.org", "cell.com", "tandfonline.com",
    "sagepub.com", "jstor.org",
)
_OPEN_LICENSE_TOKENS = {
    "cc0", "public-domain", "publicdomain",
    "cc-by", "cc-by-sa", "cc-by-nc", "cc-by-nd",
    "cc-by-3.0", "cc-by-4.0", "cc-by-sa-3.0", "cc-by-sa-4.0",
    "mit", "apache-2.0", "apache2", "bsd", "bsd-3-clause", "bsd-2-clause",
    "arxiv-non-exclusive",
}


def check_license_consistency(vault_files: list[Path]) -> list[dict]:
    """Flag vault files whose declared license disagrees with the
    publisher domain in their source_url. Either the license tag is
    wrong or the URL is mislabeled — user picks.
    """
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
                        f"declared license `{license_str}` but URL is on "
                        f"`{dom}` (typically paywalled)"
                    ),
                    "rationale": (
                        "An open license declaration on a paywalled-"
                        "publisher URL is suspicious. Either the article is "
                        "genuinely OA at this publisher (some are; verify "
                        "and keep the tag), or the license tag is wrong "
                        "and the file should not be bundled. With "
                        "--include-vault=owned this file would be shipped; "
                        "double-check before publishing."
                    ),
                })
                break
    return out


# --- 4. URL redaction ----------------------------------------------------


_SAFE_QUERY_KEYS = {"id", "v", "version", "doi"}


def redact_url(url: str, keep_params: bool = False) -> str:
    """Strip query strings and fragments from a source_url unless the
    caller explicitly opts in to keeping them, OR the only query keys
    are well-known canonical-id keys (`?v=2`, `?doi=...`).

    Reason: signed S3 URLs, paywalled-with-token URLs, and tracking
    parameters (`utm_*`, `gclid`) all leak data when published. The
    canonical paper URL is usually fine without query.
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
    # Keep query if every key is a known-safe canonical-id key.
    if parsed.query:
        kvs = [kv.split("=", 1)[0] for kv in parsed.query.split("&") if kv]
        if kvs and all(k.lower() in _SAFE_QUERY_KEYS for k in kvs):
            return urlunparse(parsed._replace(fragment=""))
    return urlunparse(parsed._replace(query="", fragment=""))


# --- 5. GPL contagion detection ------------------------------------------


_GPL_PATTERNS = [
    re.compile(r"\bGNU\s+(?:Lesser\s+|Affero\s+)?General\s+Public\s+License\b",
               re.IGNORECASE),
    re.compile(r"\bSPDX-License-Identifier:\s*(?:A?GPL|LGPL)[\w.-]*", re.IGNORECASE),
    re.compile(r"\b(?:^|[^A-Za-z])(?:AGPL|LGPL|GPL)v?\d+(?:\.\d+)?(?:[-+]only|[-+]or-later)?\b"),
    re.compile(r"\bcopyleft\b", re.IGNORECASE),
]


def find_gpl_contagion(files: list[Path]) -> list[dict]:
    """Scan body text for GPL/AGPL/LGPL license markers.

    Why this matters: GPL-family licenses have copyleft / share-alike
    requirements. If you redistribute GPL'd content as part of your
    wiki, the wiki itself may inherit a GPL-like obligation depending on
    jurisdiction and how integrated the content is. AGPL is especially
    aggressive (network use counts as distribution). Flag, surface
    rationale, let the user decide.
    """
    out: list[dict] = []
    for p in files:
        try:
            text = p.read_text(errors="replace")
        except OSError:
            continue
        for pat in _GPL_PATTERNS:
            m = pat.search(text)
            if m:
                snippet = text[max(0, m.start() - 30): m.end() + 30].replace(
                    "\n", " "
                )
                out.append({
                    "kind": "gpl_contagion",
                    "severity": "warn",
                    "subject": str(p),
                    "summary": f"GPL-family marker: `{m.group(0)}`",
                    "rationale": (
                        "GPL/AGPL/LGPL content has copyleft (share-alike) "
                        "requirements. If you republish this content in a "
                        "wiki licensed differently (e.g. CC-BY), you may be "
                        "violating the GPL. AGPL is particularly aggressive "
                        "— network use of an AGPL'd component can trigger "
                        "the share-alike clause. Either: (a) license your "
                        "published wiki compatibly (GPL/AGPL), (b) remove "
                        "the GPL'd content, or (c) confirm fair-use "
                        "exception applies to your quotation. "
                        f"Match context: ...{snippet}..."
                    ),
                })
                break  # one match per file is enough to flag
    return out


# --- 6. GDPR-likely PII detection ----------------------------------------


# Conservative patterns. We err toward false positives (user reviews) over
# false negatives (PII ships).
_EMAIL_RE = re.compile(
    r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b"
)
# Generic phone: 7+ digits with separators. Excludes years and common 4-digit
# numbers. Heuristic — flags some non-phones, misses some phones.
_PHONE_RE = re.compile(
    r"(?:\+\d{1,3}[\s.-]?)?(?:\(?\d{2,4}\)?[\s.-]?){2,4}\d{2,4}\b"
)
_PHONE_DIGIT_FLOOR = 8  # below this many digits, drop the match
# US SSN: ddd-dd-dddd. Rare to appear by accident.
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
# Credit-card-ish: 13–19 digits with optional separators. Luhn check
# would cut false positives but is overkill at this layer.
_CC_RE = re.compile(r"\b(?:\d[\s-]?){12,18}\d\b")
# IBAN: 2 letters + 2 digits + 11–30 alphanum. Distinctive enough.
_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")

# Skip whole files that look like obvious source-code references for a
# common framework. Reduces noise on technical wikis.
_NOISE_HINTS = ("__init__", "test_", "example.com", "127.0.0.1", "localhost")


def _phone_digit_count(s: str) -> int:
    return sum(1 for c in s if c.isdigit())


def find_gdpr_likely_pii(files: list[Path]) -> list[dict]:
    """Scan body text for patterns that may be personal data: emails,
    phones, SSN, IBAN, credit-card-ish.

    Heavy false-positive rate by design. Surfaces hits with rationale;
    the user verifies whether the matches are real-people contact info
    (GDPR/CCPA-relevant) or technical noise (`@example.com`, `localhost`,
    a phone-number example in a paper).
    """
    out: list[dict] = []
    for p in files:
        try:
            text = _body(p.read_text(errors="replace"))
        except OSError:
            continue

        emails = [m.group(0) for m in _EMAIL_RE.finditer(text)
                   if not any(h in m.group(0).lower() for h in _NOISE_HINTS)]
        # Strip emails from text before phone match to avoid double-counting.
        text_no_emails = _EMAIL_RE.sub("", text)
        phones = [m.group(0) for m in _PHONE_RE.finditer(text_no_emails)
                   if _phone_digit_count(m.group(0)) >= _PHONE_DIGIT_FLOOR]
        ssns = [m.group(0) for m in _SSN_RE.finditer(text)]
        ibans = [m.group(0) for m in _IBAN_RE.finditer(text)]
        ccs = [m.group(0) for m in _CC_RE.finditer(text)
                if _phone_digit_count(m.group(0)) >= 13]

        hits: list[tuple[str, list[str]]] = []
        if emails:
            hits.append(("email", emails))
        if phones:
            hits.append(("phone-like", phones))
        if ssns:
            hits.append(("US SSN", ssns))
        if ibans:
            hits.append(("IBAN", ibans))
        if ccs:
            hits.append(("credit-card-like", ccs))
        if not hits:
            continue

        summary = ", ".join(f"{n}×{kind}" for kind, lst in hits
                             for n in [len(lst)])
        sample = "; ".join(
            f"{kind}: " + ", ".join(lst[:3]) + ("..." if len(lst) > 3 else "")
            for kind, lst in hits
        )
        out.append({
            "kind": "gdpr_likely_pii",
            "severity": "warn",
            "subject": str(p),
            "summary": f"possible personal data ({summary})",
            "rationale": (
                "These patterns may be real people's personal data "
                "(emails, phones, SSN, IBAN, payment numbers). Publishing "
                "personal data without consent can trigger GDPR (EU), "
                "CCPA (California), or equivalent regimes. False-positive "
                "rate is high — `@example.com` and `localhost` are "
                "filtered, but a paper that quotes a phone number as an "
                "example will still match. Review the matches and either: "
                "(a) confirm they're not real-person data and override, "
                "(b) redact before publishing, or (c) drop the source. "
                f"Sample: {sample[:300]}"
            ),
        })
    return out


# --- aggregate runner ----------------------------------------------------


def run_all(*, scope_pages: list[Path], vault_files: list[Path],
            include_non_native: bool = False,
            quote_density_threshold: float = 0.25) -> list[dict]:
    """Convenience: run every detector and return a flat findings list.

    Caller passes `include_non_native=True` to suppress the chain-merge
    finding when they're deliberately exporting non-native content
    (e.g. personal transfer with --include-vault=all).
    """
    findings: list[dict] = []
    if not include_non_native:
        findings.extend(find_non_native_pages(scope_pages))
    findings.extend(measure_quote_density(scope_pages,
                                            threshold=quote_density_threshold))
    findings.extend(check_license_consistency(vault_files))
    # GPL + GDPR run on both wiki pages and vault files — either can be
    # the source of redistribution risk.
    targets = list(scope_pages) + list(vault_files)
    findings.extend(find_gpl_contagion(targets))
    findings.extend(find_gdpr_likely_pii(targets))
    return findings


def format_findings(findings: list[dict]) -> str:
    """Human-readable summary for terminal display."""
    if not findings:
        return "preflight: no issues found.\n"
    by_kind: dict[str, list[dict]] = {}
    for f in findings:
        by_kind.setdefault(f["kind"], []).append(f)
    lines = [
        f"preflight: {len(findings)} finding(s) across "
        f"{len(by_kind)} detector(s):\n",
    ]
    for kind, items in by_kind.items():
        lines.append(f"  [{kind}] {len(items)} hit(s):")
        for f in items[:10]:
            lines.append(f"    - {f['subject']}: {f['summary']}")
        if len(items) > 10:
            lines.append(f"    ... and {len(items) - 10} more")
        # Show rationale once per detector kind (it's the same per kind).
        lines.append(f"    why: {items[0]['rationale']}")
        lines.append("")
    return "\n".join(lines)
