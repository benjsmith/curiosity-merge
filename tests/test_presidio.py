"""Presidio integration tests.

Two tiers:
  - Soft-import tests: run unconditionally. Verify the gate
    short-circuits cleanly when Presidio is unavailable.
  - Real Presidio tests: skip via `pytest.importorskip` when the
    package + spaCy model aren't installed. Verify the integration
    when they are.

Cache tests don't require Presidio — they exercise the cache
plumbing in `preflight._run_presidio_with_cache` with a fake
analyzer that returns deterministic findings.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import preflight  # type: ignore  # noqa: E402
import presidio_gate  # type: ignore  # noqa: E402


def _w(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


# --- soft-import behaviour (runs always) ---------------------------------


def test_is_available_returns_reason_when_missing(monkeypatch):
    """When the underlying package isn't importable, is_available()
    returns False with a useful reason. Simulate by monkeypatching the
    module's flag."""
    monkeypatch.setattr(presidio_gate, "_PRESIDIO_AVAILABLE", False)
    monkeypatch.setattr(presidio_gate, "_IMPORT_ERROR",
                         "no module named 'presidio_analyzer'")
    available, reason = presidio_gate.is_available()
    assert not available
    assert reason and "presidio-analyzer" in reason


def test_analyze_files_returns_empty_when_engine_unavailable(
        monkeypatch, tmp_path: Path):
    """analyze_files() short-circuits with empty list when engine
    isn't initialized — never raises."""
    monkeypatch.setattr(presidio_gate, "_get_engine", lambda: None)
    f = _w(tmp_path / "p.md", "Some text with alice@somecompany.com\n")
    out = presidio_gate.analyze_files([f])
    assert out == []


def test_run_all_falls_back_to_regex_when_presidio_unavailable(
        monkeypatch, tmp_path: Path):
    """`run_all(enable_presidio=True)` with Presidio unavailable should
    log to stderr, fall through to the regex GDPR detector, and still
    return findings."""
    monkeypatch.setattr(presidio_gate, "_PRESIDIO_AVAILABLE", False)
    monkeypatch.setattr(presidio_gate, "_IMPORT_ERROR", "simulated")
    monkeypatch.setattr(presidio_gate, "_get_engine", lambda: None)
    f = _w(tmp_path / "leak.md",
           "---\nt: x\n---\nContact alice@somecompany.com.\n")
    findings = preflight.run_all(
        scope_pages=[f], vault_files=[],
        enable_presidio=True,
    )
    # Regex baseline should still fire.
    assert any(fi["kind"] == "gdpr_likely_pii" for fi in findings)


def test_cache_config_hash_stable_across_runs():
    h1 = presidio_gate.cache_config_hash(("PERSON", "EMAIL_ADDRESS"), 0.6)
    h2 = presidio_gate.cache_config_hash(("EMAIL_ADDRESS", "PERSON"), 0.6)
    h3 = presidio_gate.cache_config_hash(("PERSON",), 0.6)
    assert h1 == h2  # order-insensitive
    assert h1 != h3  # different entity set → different hash


# --- caching plumbing (no Presidio needed) -------------------------------


def _fake_presidio_module(findings_by_subject: dict[str, dict]):
    """Build a stand-in for presidio_gate that returns the given findings
    for matching subjects, and supports the cache hash + analyze_files
    API. Used to exercise preflight._run_presidio_with_cache without
    loading the real Presidio."""
    mod = MagicMock()
    mod.cache_config_hash = lambda entities, conf: "testhash"

    def analyze(targets, *, entities, confidence):
        out = []
        for p in targets:
            if str(p) in findings_by_subject:
                out.append(findings_by_subject[str(p)])
        return out

    mod.analyze_files = analyze
    return mod


def test_cache_hits_skip_reanalysis(tmp_path: Path):
    """Second call with same file content + config should not invoke
    the analyzer."""
    f = _w(tmp_path / "p.md", "static content")
    finding = {
        "kind": "gdpr_likely_pii", "severity": "warn",
        "subject": str(f), "summary": "fake hit",
        "rationale": "test", "samples": ["alice@somecompany.com"],
    }
    cache_dir = tmp_path / "cache"

    call_count = {"n": 0}

    def make_module():
        mod = _fake_presidio_module({str(f): finding})
        orig = mod.analyze_files

        def counting(targets, **kw):
            call_count["n"] += 1
            return orig(targets, **kw)
        mod.analyze_files = counting
        return mod

    mod1 = make_module()
    out1 = preflight._run_presidio_with_cache(
        mod1, [f],
        entities=("PERSON",), confidence=0.6, cache_dir=cache_dir,
    )
    assert len(out1) == 1
    assert call_count["n"] == 1

    mod2 = make_module()
    call_count["n"] = 0
    out2 = preflight._run_presidio_with_cache(
        mod2, [f],
        entities=("PERSON",), confidence=0.6, cache_dir=cache_dir,
    )
    assert len(out2) == 1
    assert call_count["n"] == 0  # cache hit; no analyze call


def test_cache_does_not_persist_samples(tmp_path: Path):
    """Cache file must not contain raw matched values — those are
    local-only and the cache must not become a side-channel."""
    f = _w(tmp_path / "p.md", "static")
    finding = {
        "kind": "gdpr_likely_pii", "severity": "warn",
        "subject": str(f), "summary": "1×email",
        "rationale": "test",
        "samples": ["email: alice@privatecorp.com"],  # would be a leak
    }
    cache_dir = tmp_path / "cache"
    mod = _fake_presidio_module({str(f): finding})

    preflight._run_presidio_with_cache(
        mod, [f], entities=("EMAIL_ADDRESS",),
        confidence=0.6, cache_dir=cache_dir,
    )
    # Inspect every cache file written; assert the email sample never
    # appears.
    for cache_file in cache_dir.iterdir():
        text = cache_file.read_text()
        assert "alice@privatecorp.com" not in text


def test_cache_invalidates_on_config_change(tmp_path: Path):
    """Same file content but different entity list → different cache
    key → re-analyze."""
    f = _w(tmp_path / "p.md", "static")
    finding = {
        "kind": "gdpr_likely_pii", "severity": "warn",
        "subject": str(f), "summary": "hit", "rationale": "x",
        "samples": [],
    }
    cache_dir = tmp_path / "cache"

    # Two configs that should produce different hashes.
    mod = MagicMock()
    mod.cache_config_hash = (
        lambda entities, conf: "h1" if "PERSON" in entities else "h2"
    )

    call_count = {"n": 0}

    def analyze(targets, **kw):
        call_count["n"] += 1
        return [finding] if targets else []
    mod.analyze_files = analyze

    preflight._run_presidio_with_cache(
        mod, [f], entities=("PERSON",),
        confidence=0.6, cache_dir=cache_dir,
    )
    preflight._run_presidio_with_cache(
        mod, [f], entities=("EMAIL_ADDRESS",),
        confidence=0.6, cache_dir=cache_dir,
    )
    assert call_count["n"] == 2  # both configs analyzed; no cross-hit


def test_cache_disabled_when_dir_is_none(tmp_path: Path):
    """cache_dir=None → no cache I/O, every call analyzes fresh."""
    f = _w(tmp_path / "p.md", "static")
    mod = _fake_presidio_module({str(f): {
        "kind": "gdpr_likely_pii", "severity": "warn",
        "subject": str(f), "summary": "x", "rationale": "x",
        "samples": [],
    }})

    call_count = {"n": 0}
    orig = mod.analyze_files

    def counting(targets, **kw):
        call_count["n"] += 1
        return orig(targets, **kw)
    mod.analyze_files = counting

    preflight._run_presidio_with_cache(
        mod, [f], entities=("PERSON",), confidence=0.6, cache_dir=None,
    )
    preflight._run_presidio_with_cache(
        mod, [f], entities=("PERSON",), confidence=0.6, cache_dir=None,
    )
    assert call_count["n"] == 2


# --- real Presidio integration (skipped when not installed) --------------


def _presidio_ready() -> bool:
    """Probe that the package is importable AND the analyzer engine
    initializes (which requires the spaCy model). Cheaper than running
    the analyzer."""
    try:
        import presidio_analyzer  # noqa: F401
    except ImportError:
        return False
    available, _ = presidio_gate.is_available()
    return available


pytestmark_real = pytest.mark.skipif(
    not _presidio_ready(),
    reason="presidio-analyzer + spaCy model not installed",
)


@pytestmark_real
def test_real_presidio_finds_person_entity(tmp_path: Path):
    """Real Presidio detects a clear PERSON name in user-typed content
    (outside FETCHED markers) → warn severity."""
    f = _w(tmp_path / "user-note.md",
           "---\nt: x\n---\n\nAlice Johnson called yesterday.\n")
    findings = presidio_gate.analyze_files(
        [f], entities=("PERSON",), confidence=0.6,
    )
    assert findings
    assert findings[0]["severity"] == "warn"


@pytestmark_real
def test_real_presidio_arxiv_author_block_is_info(tmp_path: Path):
    """Sparse PERSON entities inside FETCHED markers (author block of a
    paper) → info severity. The whole point of the integration."""
    body = (
        "Authors: Alice Vaswani  Bob Shazeer  Carol Parmar  "
        "Dave Uszkoreit  Eve Jones\n\n"
        "Abstract: " + ("padding text " * 4500)
    )
    text = (
        "---\nt: x\nsource_url: arxiv\n---\n\n"
        "<!-- BEGIN FETCHED CONTENT -->\n\n"
        + body
        + "\n\n<!-- END FETCHED CONTENT -->\n"
    )
    f = _w(tmp_path / "paper.md", text)
    findings = presidio_gate.analyze_files(
        [f], entities=("PERSON",), confidence=0.6,
    )
    assert findings
    assert findings[0]["severity"] == "info"
    assert "sparse" in findings[0]["summary"]


@pytestmark_real
def test_real_presidio_finding_strips_samples_via_manifest_safe(
        tmp_path: Path):
    """Manifest-safe projection drops the samples list; rationale and
    summary contain no raw entity values."""
    f = _w(tmp_path / "p.md",
           "Alice Johnson, alice@somecompany.com.\n")
    findings = presidio_gate.analyze_files(
        [f], entities=("PERSON", "EMAIL_ADDRESS"), confidence=0.6,
    )
    assert findings
    safe = preflight.manifest_safe(findings[0])
    assert "samples" not in safe
    assert "alice@somecompany.com" not in safe["rationale"]
    assert "Alice Johnson" not in safe["rationale"]
    assert "alice@somecompany.com" not in safe["summary"]


@pytestmark_real
def test_real_presidio_summary_carries_via_presidio_label(tmp_path: Path):
    """Findings clearly attribute themselves to Presidio so the user
    can tell baseline regex hits from Presidio hits."""
    f = _w(tmp_path / "p.md",
           "---\nt: x\n---\n\nAlice Johnson called.\n")
    findings = presidio_gate.analyze_files(
        [f], entities=("PERSON",), confidence=0.6,
    )
    assert findings
    assert "via Presidio" in findings[0]["summary"]
