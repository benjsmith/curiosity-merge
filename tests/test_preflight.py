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


def test_gpl_contagion_detects_full_name(tmp_path: Path):
    f = _w(tmp_path / "p.md",
           "---\nt: x\n---\nLicensed under the GNU General Public License.\n")
    findings = preflight.find_gpl_contagion([f])
    assert findings and findings[0]["subject"] == str(f)


def test_gpl_contagion_detects_spdx(tmp_path: Path):
    f = _w(tmp_path / "p.md",
           "---\nt: x\n---\nSPDX-License-Identifier: AGPL-3.0-or-later\n")
    findings = preflight.find_gpl_contagion([f])
    assert findings


def test_gpl_contagion_detects_short_form(tmp_path: Path):
    f = _w(tmp_path / "p.md",
           "---\nt: x\n---\nThis library is GPLv3 only.\n")
    findings = preflight.find_gpl_contagion([f])
    assert findings


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


def test_gdpr_pii_phone_digit_floor(tmp_path: Path):
    """Short numeric strings shouldn't match (e.g. years, page numbers)."""
    f = _w(tmp_path / "p.md",
           "---\nt: x\n---\nSee 2024 and table 3.14 for details.\n")
    assert preflight.find_gdpr_likely_pii([f]) == []


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
