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


def find_gdpr_likely_pii(files: list[Path]) -> list[dict]:
    """Scan body text for patterns that may be personal data.

    Coverage in v0.2.1:
      - email   (RFC 6531-compatible local-part + domain; reserved
                 test domains filtered)
      - phone   (E.164 only — `+` prefix, 8–15 digits)
      - SSN     (US format ddd-dd-dddd)
      - IBAN    (international bank account number)
      - cc      (credit-card-shaped 13–19 digit sequences)

    False-positive rate: low for SSN/IBAN, low for email after reserved-
    domain filtering, low for E.164 phone, moderate for cc (Luhn check
    would help; not implemented). False-negative rate: high for
    non-E.164 phone numbers (out of scope), and for free-text PII
    (names + addresses) which would need NER.
    """
    out: list[dict] = []
    for p in files:
        try:
            text = _body(p.read_text(errors="replace"))
        except OSError:
            continue

        emails = [m.group(0) for m in _EMAIL_RE.finditer(text)
                   if not _is_test_email(m.group(0))]
        # Strip emails before phone scan so `+1` in `user+1@x.com` doesn't
        # double-count.
        text_no_emails = _EMAIL_RE.sub("", text)
        phones = [m.group(0) for m in _PHONE_E164_RE.finditer(text_no_emails)]
        ssns = [m.group(0) for m in _SSN_RE.finditer(text)]
        ibans = [m.group(0) for m in _IBAN_RE.finditer(text)]
        ccs = [m.group(0) for m in _CC_RE.finditer(text)
                if _digit_count(m.group(0)) >= 13]

        hits: list[tuple[str, list[str]]] = []
        if emails:
            hits.append(("email", emails))
        if phones:
            hits.append(("E.164 phone", phones))
        if ssns:
            hits.append(("US SSN", ssns))
        if ibans:
            hits.append(("IBAN", ibans))
        if ccs:
            hits.append(("payment-card-like", ccs))
        if not hits:
            continue

        # Summary uses counts only (manifest-safe).
        summary = ", ".join(f"{len(lst)}×{kind}" for kind, lst in hits)
        # Samples list is for local display only. The manifest-write path
        # strips this key.
        samples_flat: list[str] = []
        for kind, lst in hits:
            for s in lst[:5]:
                samples_flat.append(f"{kind}: {s}")

        out.append({
            "kind": "gdpr_likely_pii",
            "severity": "warn",
            "subject": str(p),
            "summary": f"possible personal data ({summary})",
            "rationale": (
                "These patterns may be real people's personal data "
                "(emails, phones, SSN, IBAN, payment numbers). "
                "Publishing personal data without consent can trigger "
                "GDPR (EU), CCPA (California), or equivalent regimes. "
                "Reserved test domains (example.com/.org/.net, "
                "localhost, .test) and academic identifiers (arXiv IDs, "
                "DOIs, ISBNs) are excluded by construction. Review the "
                "remaining matches: confirm they aren't real-person "
                "data and override, or redact before publishing. "
                "Examples are shown in the local terminal output; they "
                "are NOT written to the export manifest."
            ),
            "samples": samples_flat,
        })
    return out


# --- aggregate runner + manifest-safety ----------------------------------


def run_all(*, scope_pages: list[Path], vault_files: list[Path],
            include_non_native: bool = False,
            quote_density_threshold: float = 0.25) -> list[dict]:
    """Convenience: run every detector and return a flat findings list.

    `include_non_native=True` suppresses the chain-merge finding for
    callers that legitimately ship non-native content (personal
    transfer, or merge-stage preflight where everything is foreign by
    definition).
    """
    findings: list[dict] = []
    if not include_non_native:
        findings.extend(find_non_native_pages(scope_pages))
    findings.extend(measure_quote_density(scope_pages,
                                            threshold=quote_density_threshold))
    findings.extend(check_license_consistency(vault_files))
    targets = list(scope_pages) + list(vault_files)
    findings.extend(find_gpl_contagion(targets))
    findings.extend(find_gdpr_likely_pii(targets))
    return findings


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
