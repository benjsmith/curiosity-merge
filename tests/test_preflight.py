"""Unit tests for the preflight detectors.

These exercise scripts/preflight.py directly (no subprocess) so each
detector's logic is testable in isolation.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import preflight  # type: ignore  # noqa: E402


def _w(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


# --- non_native_page ------------------------------------------------------


def test_non_native_page_flags_origin_tagged(tmp_path: Path):
    native = _w(tmp_path / "n.md", "---\ntitle: N\n---\nbody\n")
    foreign = _w(tmp_path / "f.md", "---\ntitle: F\norigin: bob\n---\nbody\n")
    findings = preflight.find_non_native_pages([native, foreign])
    assert len(findings) == 1
    assert findings[0]["subject"] == str(foreign)
    assert "bob" in findings[0]["summary"]
    rationale = findings[0]["rationale"].lower()
    assert "merge" in rationale and "non-native" in rationale


# --- quote_density --------------------------------------------------------


def test_quote_density_flags_high_quote_ratio(tmp_path: Path):
    heavy = _w(tmp_path / "heavy.md", "---\ntitle: H\n---\n"
               + "> a long block quote line\n" * 20
               + "small original note.\n")
    light = _w(tmp_path / "light.md", "---\ntitle: L\n---\n"
               + "Original analysis paragraph one.\n" * 10
               + "> brief quote\n")
    findings = preflight.measure_quote_density([heavy, light])
    subjects = {f["subject"] for f in findings}
    assert str(heavy) in subjects
    assert str(light) not in subjects


def test_quote_density_threshold_is_configurable(tmp_path: Path):
    page = _w(tmp_path / "p.md", "---\nt: x\n---\n"
              + "> q\n" * 5 + "original\n" * 5)
    # Strict threshold catches it; loose threshold doesn't.
    assert preflight.measure_quote_density([page], threshold=0.1)
    assert not preflight.measure_quote_density([page], threshold=0.99)


# --- license_inconsistent -------------------------------------------------


def test_license_inconsistent_open_license_paywalled_domain(tmp_path: Path):
    bad = _w(tmp_path / "bad.md", "---\nlicense: CC-BY-4.0\n"
             "source_url: https://nature.com/articles/x\n---\n")
    fine = _w(tmp_path / "fine.md", "---\nlicense: CC-BY-4.0\n"
              "source_url: https://example.org/blog\n---\n")
    paywalled_no_open_decl = _w(tmp_path / "ok.md", "---\nlicense: all-rights-reserved\n"
                                 "source_url: https://elsevier.com/x\n---\n")
    findings = preflight.check_license_consistency(
        [bad, fine, paywalled_no_open_decl]
    )
    subjects = {f["subject"] for f in findings}
    assert str(bad) in subjects
    assert str(fine) not in subjects
    assert str(paywalled_no_open_decl) not in subjects


# --- redact_url -----------------------------------------------------------


def test_redact_url_strips_query_and_fragment_by_default():
    assert preflight.redact_url(
        "https://example.com/p?token=abc&utm_source=tw#anchor"
    ) == "https://example.com/p"


def test_redact_url_preserves_canonical_id_keys():
    assert preflight.redact_url(
        "https://arxiv.org/abs/1706.03762?v=2"
    ) == "https://arxiv.org/abs/1706.03762?v=2"


def test_redact_url_keep_params_preserves_everything():
    url = "https://example.com/p?token=abc#anchor"
    assert preflight.redact_url(url, keep_params=True) == url


def test_redact_url_handles_empty():
    assert preflight.redact_url("") == ""


# --- gpl_contagion --------------------------------------------------------


def test_gpl_contagion_detects_frontmatter_license(tmp_path: Path):
    """v0.2.1: frontmatter `license: GPL-*` is the cleanest signal of
    actual GPL-licensed content."""
    f = _w(tmp_path / "p.md",
           "---\ntitle: P\nlicense: GPL-3.0\n---\ncontent\n")
    findings = preflight.find_gpl_contagion([f])
    assert findings and findings[0]["subject"] == str(f)
    assert "frontmatter" in findings[0]["summary"]


def test_gpl_contagion_detects_spdx(tmp_path: Path):
    f = _w(tmp_path / "p.md",
           "---\nt: x\n---\nSPDX-License-Identifier: AGPL-3.0-or-later\n")
    findings = preflight.find_gpl_contagion([f])
    assert findings
    assert "SPDX" in findings[0]["summary"]


def test_gpl_contagion_detects_in_fenced_code_block(tmp_path: Path):
    """GPL keyword inside a triple-backtick fence — likely a pasted
    license header from upstream code."""
    f = _w(tmp_path / "p.md", "---\nt: x\n---\n"
           "```c\n/* This file is licensed under GPLv3 only */\n"
           "int main(){}\n```\n")
    findings = preflight.find_gpl_contagion([f])
    assert findings
    assert "fenced code" in findings[0]["summary"]


def test_gpl_contagion_skips_prose_mentions(tmp_path: Path):
    """v0.2.1 explicitly does NOT match prose discussions of the GPL —
    that was the false-positive bug in v0.2.0."""
    prose = _w(tmp_path / "history.md", "---\ntitle: Free Software\n---\n"
               "Stallman's vision of copyleft, codified in the GNU General "
               "Public License, became the philosophical foundation. "
               "This wiki page is itself MIT-licensed.\n")
    assert preflight.find_gpl_contagion([prose]) == []


def test_gpl_contagion_skips_clean_content(tmp_path: Path):
    f = _w(tmp_path / "p.md", "---\nt: x\n---\nMIT-licensed code follows.\n")
    assert preflight.find_gpl_contagion([f]) == []


# --- gdpr_likely_pii ------------------------------------------------------


def test_gdpr_pii_finds_emails(tmp_path: Path):
    f = _w(tmp_path / "p.md", "---\nt: x\n---\n"
           "Contact: alice@aoltest.org or bob@somewhere.io for details.\n")
    findings = preflight.find_gdpr_likely_pii([f])
    assert findings
    assert "email" in findings[0]["summary"]


def test_gdpr_pii_filters_example_com(tmp_path: Path):
    f = _w(tmp_path / "p.md", "---\nt: x\n---\n"
           "Send to user@example.com only.\n")
    # `example.com` filtered; no other hits → no findings.
    assert preflight.find_gdpr_likely_pii([f]) == []


def test_gdpr_pii_finds_ssn_and_iban(tmp_path: Path):
    f = _w(tmp_path / "p.md", "---\nt: x\n---\n"
           "SSN 123-45-6789 IBAN DE89370400440532013000\n")
    findings = preflight.find_gdpr_likely_pii([f])
    assert findings
    summary = findings[0]["summary"]
    assert "SSN" in summary
    assert "IBAN" in summary


def test_gdpr_pii_phone_e164_only(tmp_path: Path):
    """v0.2.1: phone detection requires E.164 (`+` prefix). Local-format
    numbers, arXiv IDs, DOIs, ISBNs, citation stems all pass clean."""
    academic = _w(tmp_path / "academic.md", "---\nt: x\n---\n"
                   "See arxiv:2401.12345 and DOI: 10.1038/s41586-021-03819-2. "
                   "ISBN 978-3-16-148410-0. Stem vaswani-2017-1706.03762. "
                   "Range (1942-2018), and pages 100-1023.\n")
    assert preflight.find_gdpr_likely_pii([academic]) == []
    real_phone = _w(tmp_path / "phone.md", "---\nt: x\n---\n"
                     "Contact +1 555-0142 between 9-5.\n")
    findings = preflight.find_gdpr_likely_pii([real_phone])
    assert findings
    assert "phone" in findings[0]["summary"].lower()


def test_gdpr_pii_email_i18n_caught(tmp_path: Path):
    """RFC 6531 internationalised addresses match the v0.2.1 regex."""
    f = _w(tmp_path / "p.md", "---\nt: x\n---\n"
           "Contact 用户@邮件.中国 directly.\n")
    findings = preflight.find_gdpr_likely_pii([f])
    assert findings


def test_gdpr_pii_reserved_test_domains_filtered(tmp_path: Path):
    """RFC 6761 reserved test domains and TLDs filter out as noise."""
    f = _w(tmp_path / "p.md", "---\nt: x\n---\n"
           "Send to alice@example.org or bob@example.net or carol@test.local "
           "or dave@something.test. None are real.\n")
    assert preflight.find_gdpr_likely_pii([f]) == []


# --- manifest-safety projection ------------------------------------------


def test_manifest_safe_strips_samples(tmp_path: Path):
    f = _w(tmp_path / "p.md",
           "---\nt: x\n---\nContact alice@somecompany.com.\n")
    findings = preflight.find_gdpr_likely_pii([f])
    assert findings
    assert findings[0]["samples"]  # local-display samples populated
    safe = preflight.manifest_safe(findings[0])
    assert "samples" not in safe
    # Rationale must NOT contain the matched email.
    assert "alice@somecompany.com" not in safe["rationale"]
    assert "alice@somecompany.com" not in safe["summary"]


def test_manifest_summary_aggregates_counts(tmp_path: Path):
    findings = [
        {"kind": "gdpr_likely_pii", "severity": "warn",
         "subject": "a.md", "summary": "...", "rationale": "..."},
        {"kind": "gdpr_likely_pii", "severity": "warn",
         "subject": "b.md", "summary": "...", "rationale": "..."},
        {"kind": "quote_density", "severity": "warn",
         "subject": "c.md", "summary": "...", "rationale": "..."},
    ]
    s = preflight.manifest_summary(findings)
    by_kind = {e["kind"]: e["count"] for e in s}
    assert by_kind["gdpr_likely_pii"] == 2
    assert by_kind["quote_density"] == 1
    # Summary must NOT include subjects or any per-record data beyond counts.
    for entry in s:
        assert set(entry.keys()) == {"kind", "severity", "count"}


# --- license allowlist (v0.2.1 tightening) -------------------------------


def test_open_license_tokens_excludes_nc_nd():
    assert "cc-by-nc" not in preflight._OPEN_LICENSE_TOKENS
    assert "cc-by-nd" not in preflight._OPEN_LICENSE_TOKENS
    assert "cc-by-nc-sa" not in preflight._OPEN_LICENSE_TOKENS
    # But the unrestricted CC family stays.
    assert "cc-by" in preflight._OPEN_LICENSE_TOKENS
    assert "cc-by-sa" in preflight._OPEN_LICENSE_TOKENS


# --- run_all + format_findings -------------------------------------------


def test_run_all_aggregates(tmp_path: Path):
    page = _w(tmp_path / "wiki.md",
              "---\nt: x\n---\n" + "> quoted\n" * 30)
    vault = _w(tmp_path / "v.md",
               "---\nlicense: CC-BY-4.0\n"
               "source_url: https://nature.com/x\n---\n"
               "Contact alice@somecompany.com.\n")
    findings = preflight.run_all(scope_pages=[page], vault_files=[vault])
    kinds = {f["kind"] for f in findings}
    assert "quote_density" in kinds
    assert "license_inconsistent" in kinds
    assert "gdpr_likely_pii" in kinds


def test_run_all_skips_non_native_when_overridden(tmp_path: Path):
    page = _w(tmp_path / "p.md",
              "---\nt: x\norigin: bob\n---\nclean body\n")
    findings = preflight.run_all(
        scope_pages=[page], vault_files=[], include_non_native=True
    )
    kinds = {f["kind"] for f in findings}
    assert "non_native_page" not in kinds


def test_format_findings_groups_by_kind(tmp_path: Path):
    findings = [
        {"kind": "non_native_page", "severity": "warn",
         "subject": "a.md", "summary": "...", "rationale": "..."},
        {"kind": "non_native_page", "severity": "warn",
         "subject": "b.md", "summary": "...", "rationale": "..."},
        {"kind": "gpl_contagion", "severity": "warn",
         "subject": "c.md", "summary": "...", "rationale": "..."},
    ]
    out = preflight.format_findings(findings)
    assert "[non_native_page] 2 hit" in out
    assert "[gpl_contagion] 1 hit" in out


def test_format_findings_empty():
    assert "no issues" in preflight.format_findings([])
