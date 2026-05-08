"""Unit tests for the preflight detectors.

These exercise scripts/preflight.py directly (no subprocess) so each
detector's logic is testable in isolation.
"""
from __future__ import annotations

import json
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
    assert preflight.measure_quote_density([page], threshold=0.1,
                                             page_threshold=0.99)
    # Both thresholds loose → no findings.
    assert not preflight.measure_quote_density([page], threshold=0.99,
                                                 page_threshold=0.99)


def test_quote_density_per_citation_attribution(tmp_path: Path):
    """v0.4.0: quotes attributed to nearest preceding citation. A page
    with one heavy citation (>25%) and one light one fires only on the
    heavy one."""
    page = _w(tmp_path / "p.md", "---\ntitle: x\n---\n\n"
              "## Vaswani 2017\n\n"
              "(vault:vaswani-2017.extracted.md)\n\n"
              + ("> heavy quote line about transformers\n" * 30)
              + "\n## Brown 2020\n\n"
              "(vault:brown-2020.extracted.md)\n\n"
              "> short quote.\n\n"
              "Original analysis.\n")
    findings = preflight.measure_quote_density([page], threshold=0.25,
                                                 page_threshold=0.99)
    # One single-source finding for the heavy citation.
    single_source = [f for f in findings if "vaswani-2017" in f["subject"]]
    assert single_source
    other = [f for f in findings if "brown-2020" in f["subject"]]
    assert not other  # short quote is below 25% threshold


def test_quote_density_unattributed_quotes(tmp_path: Path):
    """Quotes appearing before any (vault:...) citation go into the
    unattributed bucket and surface with that label."""
    page = _w(tmp_path / "p.md", "---\nt: x\n---\n\n"
              + ("> quote with no citation above\n" * 40))
    findings = preflight.measure_quote_density([page], threshold=0.25,
                                                 page_threshold=0.99)
    assert findings
    # At least one finding is the unattributed bucket.
    assert any("unattributed" in f["subject"] for f in findings)


def test_quote_density_page_threshold_independent_of_per_citation(
        tmp_path: Path):
    """A page where no single source dominates but the aggregate is
    high should still fire the page-level finding."""
    # 5 citations × 10% each → no single-source hit, but 50% page-level
    page_text = "---\nt: x\n---\n\n"
    for i in range(5):
        page_text += f"## Source {i}\n(vault:src-{i}.md)\n\n"
        page_text += "> ten percent quoted text per source\n" * 6
        page_text += "\noriginal padding text " * 4 + "\n\n"
    page = _w(tmp_path / "p.md", page_text)
    findings = preflight.measure_quote_density(
        [page], threshold=0.25, page_threshold=0.50,
    )
    # Page-level finding should fire (subject is the page path, no citation).
    page_level = [f for f in findings
                   if f["subject"] == str(page) and "across all" in f["summary"]]
    assert page_level


def test_quote_density_clean_page_no_findings(tmp_path: Path):
    page = _w(tmp_path / "p.md", "---\nt: x\n---\n\n"
              "(vault:src.md)\n\n"
              "Original analysis with no block quotes at all. "
              "More original content. Even more.\n"
              "> single short quote\n"
              "And lots more analysis.\n")
    assert preflight.measure_quote_density([page]) == []


# --- license_inconsistent -------------------------------------------------


def test_license_inconsistent_open_license_paywalled_domain(tmp_path: Path):
    bad = _w(tmp_path / "bad.md", "---\nlicense: CC-BY-4.0\n"
             "source_url: https://nature.com/articles/x\n---\n")
    fine = _w(tmp_path / "fine.md", "---\nlicense: CC-BY-4.0\n"
              "source_url: https://somecorp.invalid/blog\n---\n")
    paywalled_no_open_decl = _w(tmp_path / "ok.md", "---\nlicense: all-rights-reserved\n"
                                 "source_url: https://elsevier.com/x\n---\n")
    findings = preflight.check_license_consistency(
        [bad, fine, paywalled_no_open_decl]
    )
    by_subject = {f["subject"]: f for f in findings}
    assert str(bad) in by_subject
    assert by_subject[str(bad)]["severity"] == "warn"
    assert str(fine) not in by_subject
    assert str(paywalled_no_open_decl) not in by_subject


def test_license_inconsistent_restrictive_on_oa_domain_is_info(tmp_path: Path):
    """v0.4.0: reverse direction — declared restrictive license on a
    known OA domain → info-severity flag (likely tag is wrong, but
    nothing leaks since we're being more conservative than allowed)."""
    plos = _w(tmp_path / "plos.md", "---\nlicense: all-rights-reserved\n"
              "source_url: https://plos.org/article/123\n---\n")
    pmc = _w(tmp_path / "pmc.md", "---\nlicense: proprietary\n"
             "source_url: https://www.ncbi.nlm.nih.gov/pmc/articles/PMC1\n---\n")
    findings = preflight.check_license_consistency([plos, pmc])
    by_subject = {f["subject"]: f for f in findings}
    assert by_subject[str(plos)]["severity"] == "info"
    assert by_subject[str(pmc)]["severity"] == "info"
    assert "open-access" in by_subject[str(plos)]["summary"]


def test_license_inconsistent_arxiv_empty_license_not_flagged(tmp_path: Path):
    """Common case: vault file from arXiv with no explicit license tag.
    arxiv-non-exclusive is the implicit default; firing 'license tag
    missing' on every academic vault file would be noise."""
    f = _w(tmp_path / "arxiv.md", "---\ntitle: x\n"
           "source_url: https://arxiv.org/abs/1706.03762\n---\n")
    assert preflight.check_license_consistency([f]) == []


def test_license_inconsistent_no_url_no_finding(tmp_path: Path):
    """No source_url → can't compare; no finding either direction."""
    f = _w(tmp_path / "no-url.md", "---\nlicense: all-rights-reserved\n---\n")
    assert preflight.check_license_consistency([f]) == []


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


# --- fetched-content density (v0.2.1.1) ----------------------------------


def _wrap_fetched(body: str) -> str:
    return (
        "---\nsource_url: x\n---\n\n"
        "<!-- BEGIN FETCHED CONTENT -->\n\n"
        + body
        + "\n\n<!-- END FETCHED CONTENT -->\n"
    )


def test_pii_in_fetched_sparse_is_info(tmp_path: Path):
    """Academic-paper-shaped: 8 emails in 50K chars of fetched content
    → density 0.16/1000 → info."""
    body = ("Authors: avaswani@google.com noam@google.com nikip@google.com "
            "usz@google.com llion@google.com aidan@cs.toronto.edu "
            "lukaszkaiser@google.com illia.polosukhin@gmail.com\n\n"
            "Abstract: " + ("padding text " * 4500))
    f = _w(tmp_path / "paper.md", _wrap_fetched(body))
    findings = preflight.find_gdpr_likely_pii([f])
    assert findings
    assert findings[0]["severity"] == "info"
    assert "sparse" in findings[0]["summary"]
    assert "author/contact block" in findings[0]["summary"]


def test_pii_in_fetched_dense_is_warn(tmp_path: Path):
    """Database-dump-shaped: 1000 emails in 100K chars → density 10/1000
    → warn."""
    rows = "\n".join(
        f"row{i},customer{i}@somecompany.com,more padding"
        for i in range(1000)
    )
    f = _w(tmp_path / "dump.md", _wrap_fetched(rows))
    findings = preflight.find_gdpr_likely_pii([f])
    assert findings
    assert findings[0]["severity"] == "warn"
    assert "dense" in findings[0]["summary"]


def test_pii_in_fetched_short_doc_is_warn(tmp_path: Path):
    """< 2000-char fetched region → density math suppressed, treated as
    warn. A short extract with one email shouldn't get a free pass."""
    body = "Tiny extract. Contact alice@somecompany.com."
    f = _w(tmp_path / "short.md", _wrap_fetched(body))
    findings = preflight.find_gdpr_likely_pii([f])
    assert findings
    assert findings[0]["severity"] == "warn"


def test_pii_outside_fetched_is_warn(tmp_path: Path):
    """User notes ABOVE the FETCHED block are user-typed → warn even
    when the fetched body is clean."""
    text = (
        "---\nt: x\n---\n\n"
        "My private contact: alice@privateaddress.com\n\n"
        "<!-- BEGIN FETCHED CONTENT -->\n"
        + ("Clean paper text. " * 500)
        + "\n<!-- END FETCHED CONTENT -->\n"
    )
    f = _w(tmp_path / "user-above.md", text)
    findings = preflight.find_gdpr_likely_pii([f])
    assert findings
    assert findings[0]["severity"] == "warn"


def test_ssn_in_fetched_always_warn(tmp_path: Path):
    """SSN/IBAN/payment-card stay warn even inside FETCHED markers,
    regardless of density. They have no legitimate published form."""
    big_body = ("padding text " * 1000) + "\nSSN 123-45-6789\n" + (
        "more padding " * 1000
    )
    f = _w(tmp_path / "ssn.md", _wrap_fetched(big_body))
    findings = preflight.find_gdpr_likely_pii([f])
    assert findings
    assert findings[0]["severity"] == "warn"
    assert "SSN" in findings[0]["summary"]


def test_iban_in_fetched_always_warn(tmp_path: Path):
    big_body = ("padding " * 2000) + "\nIBAN DE89370400440532013000\n"
    f = _w(tmp_path / "iban.md", _wrap_fetched(big_body))
    findings = preflight.find_gdpr_likely_pii([f])
    assert findings
    assert findings[0]["severity"] == "warn"
    assert "IBAN" in findings[0]["summary"]


def test_file_severity_is_max_across_kinds(tmp_path: Path):
    """A paper with sparse author emails (info) plus one IBAN (warn)
    lands as warn — file-level severity = max."""
    body = ("Authors: a@uni.edu b@uni.edu c@uni.edu d@uni.edu e@uni.edu\n\n"
            + ("padding text " * 4000)
            + "\nReference IBAN DE89370400440532013000\n")
    f = _w(tmp_path / "mixed.md", _wrap_fetched(body))
    findings = preflight.find_gdpr_likely_pii([f])
    assert findings
    assert findings[0]["severity"] == "warn"  # IBAN dominates
    # Both kinds reported in summary.
    assert "email" in findings[0]["summary"]
    assert "IBAN" in findings[0]["summary"]


def test_malformed_markers_treated_conservatively(tmp_path: Path):
    """BEGIN without END → can't trust the fetched-region split → scan
    everything as user content (warn) rather than info."""
    text = (
        "---\nt: x\n---\n\n"
        "<!-- BEGIN FETCHED CONTENT -->\n"
        "alice@somecompany.com bob@othercompany.com\n"
        # no END marker
    )
    f = _w(tmp_path / "bad.md", text)
    findings = preflight.find_gdpr_likely_pii([f])
    assert findings
    assert findings[0]["severity"] == "warn"


def test_split_fetched_user_handles_multiple_blocks(tmp_path: Path):
    """Multiple FETCHED blocks in one file: concat fetched parts; user
    parts are everything else."""
    text = (
        "---\nt: x\n---\n\n"
        "First user note.\n"
        "<!-- BEGIN FETCHED CONTENT -->\nfetched 1\n<!-- END FETCHED CONTENT -->\n"
        "Second user note.\n"
        "<!-- BEGIN FETCHED CONTENT -->\nfetched 2\n<!-- END FETCHED CONTENT -->\n"
        "Trailing user note.\n"
    )
    fetched, user = preflight._split_fetched_user(text)
    assert "fetched 1" in fetched
    assert "fetched 2" in fetched
    assert "First user note" in user
    assert "Second user note" in user
    assert "Trailing user note" in user
    assert "fetched 1" not in user
    assert "fetched 2" not in user


def test_clean_fetched_content_no_findings(tmp_path: Path):
    """Paper with no PII at all → no findings."""
    f = _w(tmp_path / "clean.md",
           _wrap_fetched("Abstract. " + ("Lorem ipsum " * 500)))
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


# --- GatingPolicy (v0.4.0) -----------------------------------------------


def _f(kind: str, severity: str = "warn") -> dict:
    return {"kind": kind, "severity": severity, "subject": "x.md",
            "summary": "y", "rationale": "z", "samples": []}


def test_gating_policy_default_prompts():
    p = preflight.GatingPolicy(refuse_value="none", accept_value="none")
    assert p.is_default()
    assert p.decide(_f("gpl_contagion")) == p.PROMPT


def test_gating_policy_refuse_all():
    p = preflight.GatingPolicy(refuse_value="all", accept_value="none")
    assert p.decide(_f("quote_density")) == p.REFUSE
    assert p.decide(_f("gdpr_likely_pii")) == p.REFUSE


def test_gating_policy_accept_all():
    p = preflight.GatingPolicy(refuse_value="none", accept_value="all")
    assert p.decide(_f("quote_density")) == p.ACCEPT


def test_gating_policy_csv_specific_kinds():
    p = preflight.GatingPolicy(
        refuse_value="quote_density,gpl_contagion", accept_value="none",
    )
    assert p.decide(_f("quote_density")) == p.REFUSE
    assert p.decide(_f("gpl_contagion")) == p.REFUSE
    # Other kinds fall through.
    assert p.decide(_f("gdpr_likely_pii")) == p.PROMPT


def test_gating_policy_carve_out_refuse_all_accept_specific():
    """--refuse-on=all --accept-on=k → strict everywhere except k."""
    p = preflight.GatingPolicy(
        refuse_value="all", accept_value="quote_density",
    )
    assert p.decide(_f("quote_density")) == p.ACCEPT
    assert p.decide(_f("gpl_contagion")) == p.REFUSE
    assert p.decide(_f("gdpr_likely_pii")) == p.REFUSE


def test_gating_policy_carve_in_refuse_specific_accept_all():
    """--refuse-on=k --accept-on=all → permissive except for k."""
    p = preflight.GatingPolicy(
        refuse_value="gpl_contagion", accept_value="all",
    )
    assert p.decide(_f("gpl_contagion")) == p.REFUSE
    assert p.decide(_f("quote_density")) == p.ACCEPT


def test_gating_policy_info_findings_never_gated():
    """info-severity findings always return PROMPT regardless of policy
    (info findings don't gate; the caller's UX shows them as one-line ack)."""
    p = preflight.GatingPolicy(refuse_value="all", accept_value="none")
    assert p.decide(_f("gdpr_likely_pii", severity="info")) == p.PROMPT


def test_gating_policy_overlapping_kinds_errors():
    with pytest.raises(SystemExit) as exc:
        preflight.GatingPolicy(
            refuse_value="quote_density",
            accept_value="quote_density,gpl_contagion",
        )
    assert "both contain" in str(exc.value)


def test_gating_policy_both_all_errors():
    with pytest.raises(SystemExit) as exc:
        preflight.GatingPolicy(refuse_value="all", accept_value="all")
    assert "contradictory" in str(exc.value)


def test_gating_policy_unknown_kind_errors():
    with pytest.raises(SystemExit) as exc:
        preflight.GatingPolicy(
            refuse_value="not_a_real_kind", accept_value="none",
        )
    assert "unknown kind" in str(exc.value).lower()


def test_gating_policy_empty_value_errors():
    with pytest.raises(SystemExit):
        preflight.GatingPolicy(refuse_value="", accept_value="none")


# --- finding ack store (v0.4.0) ------------------------------------------


def test_ack_id_stable_across_calls():
    a = preflight.ack_id("abc123", "gdpr_likely_pii", "8×email")
    b = preflight.ack_id("abc123", "gdpr_likely_pii", "8×email")
    assert a == b


def test_ack_id_differs_by_content():
    a = preflight.ack_id("abc123", "gdpr_likely_pii", "8×email")
    b = preflight.ack_id("abc123", "gdpr_likely_pii", "9×email")
    c = preflight.ack_id("abc123", "gpl_contagion", "8×email")
    d = preflight.ack_id("def456", "gdpr_likely_pii", "8×email")
    assert len({a, b, c, d}) == 4  # all distinct


def test_save_load_acks_roundtrip(tmp_path: Path):
    (tmp_path / ".curator").mkdir()
    acks = {
        "abc123": {
            "ack_id": "abc123",
            "subject": "wiki/foo.md",
            "file_sha256": "deadbeef",
            "kind": "gdpr_likely_pii",
            "summary": "8×email",
            "severity": "warn",
            "acked_at": "2026-05-06T00:00:00Z",
            "ack_reason": "",
        },
    }
    preflight.save_acks(tmp_path, acks)
    loaded = preflight.load_acks(tmp_path)
    assert "abc123" in loaded
    assert loaded["abc123"]["kind"] == "gdpr_likely_pii"


def test_load_acks_missing_file(tmp_path: Path):
    """No ack file yet → empty dict, no error."""
    assert preflight.load_acks(tmp_path) == {}


def test_load_acks_corrupt_file(tmp_path: Path, capsys):
    """Corrupt JSON in ack file → empty dict + warning, no crash."""
    (tmp_path / ".curator").mkdir()
    (tmp_path / ".curator" / preflight.ACK_FILE_NAME).write_text(
        "not valid json {{{"
    )
    out = preflight.load_acks(tmp_path)
    assert out == {}
    captured = capsys.readouterr()
    assert "unreadable" in captured.err


def test_attach_ack_ids_populates_field(tmp_path: Path):
    f = _w(tmp_path / "p.md", "content")
    findings = [{
        "kind": "gpl_contagion", "severity": "warn",
        "subject": str(f), "summary": "x",
        "rationale": "y", "samples": [],
    }]
    preflight.attach_ack_ids(findings)
    assert findings[0]["ack_id"]
    assert len(findings[0]["ack_id"]) == 16  # 16 hex chars


def test_filter_acked_separates_live_and_suppressed(tmp_path: Path):
    f1 = _w(tmp_path / "live.md", "a")
    f2 = _w(tmp_path / "acked.md", "b")
    findings = [
        {"kind": "k", "severity": "warn", "subject": str(f1),
         "summary": "s1", "rationale": "", "samples": []},
        {"kind": "k", "severity": "warn", "subject": str(f2),
         "summary": "s2", "rationale": "", "samples": []},
    ]
    preflight.attach_ack_ids(findings)
    acks = {findings[1]["ack_id"]: {"ack_id": findings[1]["ack_id"]}}
    live, suppressed = preflight.filter_acked(findings, acks)
    assert len(live) == 1 and live[0]["subject"] == str(f1)
    assert len(suppressed) == 1 and suppressed[0]["subject"] == str(f2)


def test_record_ack_does_not_persist_samples(tmp_path: Path):
    """Acks must be manifest-safe — samples must never end up in the
    persisted ack record."""
    f = _w(tmp_path / "p.md", "content")
    findings = [{
        "kind": "gdpr_likely_pii", "severity": "warn",
        "subject": str(f), "summary": "1×email",
        "rationale": "x",
        "samples": ["alice@privatecorp.com"],
    }]
    preflight.attach_ack_ids(findings)
    acks: dict = {}
    preflight.record_ack(acks, findings[0])
    persisted = list(acks.values())[0]
    assert "samples" not in persisted
    # Belt-and-braces: the entire serialised record contains no email.
    assert "alice@privatecorp.com" not in json.dumps(persisted)


def test_ack_invalidates_when_file_content_changes(tmp_path: Path):
    """Edit the file → sha256 changes → ack_id changes → finding no
    longer suppressed → re-review forced."""
    f = _w(tmp_path / "p.md", "original content")
    findings1 = [{
        "kind": "gpl_contagion", "severity": "warn",
        "subject": str(f), "summary": "x",
        "rationale": "y", "samples": [],
    }]
    preflight.attach_ack_ids(findings1)
    acks: dict = {}
    preflight.record_ack(acks, findings1[0])

    # Edit the file.
    f.write_text("edited content")
    findings2 = [{
        "kind": "gpl_contagion", "severity": "warn",
        "subject": str(f), "summary": "x",
        "rationale": "y", "samples": [],
    }]
    preflight.attach_ack_ids(findings2)
    live, suppressed = preflight.filter_acked(findings2, acks)
    assert len(live) == 1, "ack should be invalidated by content change"
    assert len(suppressed) == 0


# --- license allowlist (v0.2.1 tightening) -------------------------------


def test_open_license_tokens_excludes_nc_nd():
    assert "cc-by-nc" not in preflight._OPEN_LICENSE_TOKENS
    assert "cc-by-nd" not in preflight._OPEN_LICENSE_TOKENS
    assert "cc-by-nc-sa" not in preflight._OPEN_LICENSE_TOKENS
    # But the unrestricted CC family stays.
    assert "cc-by" in preflight._OPEN_LICENSE_TOKENS
    assert "cc-by-sa" in preflight._OPEN_LICENSE_TOKENS


def test_open_license_tokens_includes_v041_additions():
    """v0.4.1: GFDL (Wikipedia), Unlicense, 0BSD, older CC versions."""
    expected = {
        "gfdl", "gfdl-1.2", "gfdl-1.3",
        "unlicense",
        "0bsd", "bsd-0",
        "cc-by-1.0", "cc-by-2.0", "cc-by-2.5",
        "cc-by-sa-1.0", "cc-by-sa-2.0", "cc-by-sa-2.5",
    }
    missing = expected - preflight._OPEN_LICENSE_TOKENS
    assert not missing, f"expected open license tokens missing: {missing}"


def test_gfdl_does_not_trip_gpl_contagion(tmp_path: Path):
    """GFDL is a documentation copyleft, not a software license, and we
    deliberately treat it as redistributable. The GPL detector should
    not mistakenly flag a `license: gfdl` frontmatter as contagion."""
    f = _w(tmp_path / "wikipedia.md",
           "---\ntitle: x\nlicense: gfdl-1.3\n---\n"
           "Wikipedia-derived content.\n")
    findings = preflight.find_gpl_contagion([f])
    assert findings == []


def test_unlicense_does_not_trip_gpl_contagion(tmp_path: Path):
    f = _w(tmp_path / "u.md",
           "---\ntitle: x\nlicense: unlicense\n---\nbody\n")
    assert preflight.find_gpl_contagion([f]) == []


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
