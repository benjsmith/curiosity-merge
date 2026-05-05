#!/usr/bin/env python3
"""presidio_gate.py — optional Microsoft Presidio integration.

Soft-imports `presidio-analyzer`. If the package isn't installed (the
default state), `analyze_files()` returns an empty list of findings plus
a `skipped` reason. Caller logs and proceeds.

Why Presidio over rolling our own NER:
  - MIT-licensed, runs locally (no network during analysis).
  - Detectors for ~50 PII entity types out of the box: PERSON, EMAIL,
    PHONE, SSN, IBAN, CREDIT_CARD, IP, MEDICAL_LICENSE, US_DRIVER_LICENSE,
    US_PASSPORT, NRP, LOCATION, plus country-specific IDs.
  - Closes the regex+density gaps from v0.2.x: named-entity PII
    ("John Smith, born March 12 1985, lives at 123 Main St") and
    structured IDs we don't have regex for (driver licenses, passports,
    medical licenses, etc.).
  - Confidence-scored output gives us a tunable threshold rather than a
    hard regex match.

Self-leak guarantee:
  Presidio's default analyzer uses spaCy NER + custom recognizers. All
  computation runs on the local machine; no content leaves it. We
  explicitly initialize the AnalyzerEngine without any cloud-backed
  recognizers to keep that property. Verified by:
    - using the default `RecognizerRegistry()` (which is offline)
    - never instantiating Azure / OpenAI / cloud recognizers
    - documented in docs/licensing.md

Architecture decisions:
  - Curated default entity list. PERSON/EMAIL/PHONE/SSN/IBAN/CC/
    MEDICAL_LICENSE/US_DRIVER_LICENSE/US_PASSPORT/IP_ADDRESS/NRP/LOCATION.
    Excluded by default: ORGANIZATION (every paper mentions Google/MIT/...),
    DATE_TIME (every paper has a date), URL (we redact separately).
    User can override via --presidio-entities.
  - AnalyzerEngine cached at module level. First-call cost (~3-5s for
    model load); subsequent calls reuse the loaded engine.
  - English-only for v0.3.0. The model is en_core_web_lg. Non-English
    content gets poor NER; documented limitation.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Optional

# --- soft import ---------------------------------------------------------

try:
    from presidio_analyzer import AnalyzerEngine  # type: ignore
    _PRESIDIO_AVAILABLE = True
    _IMPORT_ERROR: Optional[str] = None
except ImportError as e:
    AnalyzerEngine = None  # type: ignore
    _PRESIDIO_AVAILABLE = False
    _IMPORT_ERROR = str(e)


# --- defaults -------------------------------------------------------------

DEFAULT_ENTITIES = (
    "PERSON",
    "EMAIL_ADDRESS",
    "PHONE_NUMBER",
    "US_SSN",
    "IBAN_CODE",
    "CREDIT_CARD",
    "MEDICAL_LICENSE",
    "US_DRIVER_LICENSE",
    "US_PASSPORT",
    "IP_ADDRESS",
    "NRP",          # nationality / religious / political group — GDPR Art. 9
    "LOCATION",
)

DEFAULT_CONFIDENCE = 0.6


# Subset of entity kinds where existence anywhere is always serious. We
# never apply density-relaxation to these — even one in a paper is a
# concern. Mirrors the v0.2.2 regex rules for SSN/IBAN/CC.
_ALWAYS_WARN_ENTITIES = frozenset({
    "US_SSN", "IBAN_CODE", "CREDIT_CARD",
    "MEDICAL_LICENSE", "US_DRIVER_LICENSE", "US_PASSPORT",
})

# Subset where density relaxation makes sense — these are the kinds that
# legitimately appear in published academic content (author blocks,
# institutional affiliations, contact info). PERSON is the dominant
# concern: every paper has author names.
_DENSITY_RELAXABLE_ENTITIES = frozenset({
    "PERSON", "EMAIL_ADDRESS", "PHONE_NUMBER",
    "LOCATION", "IP_ADDRESS", "NRP",
})


# Density threshold: matches per character. Same value as the regex
# detector's density rule (kept consistent so users tuning one see
# coherent behaviour from the other).
_DENSITY_THRESHOLD_PER_CHAR = 0.5 / 1000.0
_DENSITY_FLOOR_CHARS = 2000


# --- engine cache --------------------------------------------------------

_ANALYZER_ENGINE = None


def _get_engine():
    """Lazily initialize and cache the AnalyzerEngine. Returns None if
    Presidio isn't available."""
    global _ANALYZER_ENGINE
    if not _PRESIDIO_AVAILABLE:
        return None
    if _ANALYZER_ENGINE is None:
        # AnalyzerEngine() with no args uses the default offline
        # configuration: spaCy NLP + the built-in recognizer registry,
        # all local. No cloud recognizers are instantiated.
        try:
            _ANALYZER_ENGINE = AnalyzerEngine()
        except Exception as e:  # noqa: BLE001 — model load failures vary
            sys.stderr.write(
                f"presidio_gate: failed to initialize AnalyzerEngine: {e}\n"
                "  Common cause: spaCy model not downloaded. Run\n"
                "    uv run python -m spacy download en_core_web_lg\n"
            )
            return None
    return _ANALYZER_ENGINE


# --- helpers (mirror preflight.py for region split + density) ------------


_FETCHED_BLOCK_RE = re.compile(
    r"<!--\s*BEGIN FETCHED CONTENT\b.*?-->"
    r"(?P<inner>[\s\S]*?)"
    r"<!--\s*END FETCHED CONTENT\b.*?-->",
    re.MULTILINE,
)


def _body(text: str) -> str:
    if not text.startswith("---"):
        return text
    end = text.find("\n---", 3)
    if end == -1:
        return text
    return text[end + 4:]


def _split_fetched_user(text: str) -> tuple[str, str]:
    """Same logic as preflight._split_fetched_user — duplicated here so
    presidio_gate stays importable without preflight.

    Mismatched BEGIN/END counts → treat the whole body as user content
    (conservative)."""
    body_text = _body(text)
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


def _is_dense(match_count: int, region_len: int) -> bool:
    if region_len < _DENSITY_FLOOR_CHARS:
        return True
    return (match_count / region_len) > _DENSITY_THRESHOLD_PER_CHAR


# --- analysis ------------------------------------------------------------


def is_available() -> tuple[bool, Optional[str]]:
    """Check if Presidio is importable AND the analyzer can initialize
    (which requires the spaCy model). Returns (available, reason).
    """
    if not _PRESIDIO_AVAILABLE:
        return False, (
            f"presidio-analyzer not importable: {_IMPORT_ERROR}. "
            "Install via: uv pip install presidio-analyzer && "
            "uv run python -m spacy download en_core_web_lg"
        )
    if _get_engine() is None:
        return False, (
            "presidio-analyzer imported but AnalyzerEngine failed to "
            "initialize (likely missing spaCy model). Run: "
            "uv run python -m spacy download en_core_web_lg"
        )
    return True, None


def _analyze_region(region: str, entities: tuple[str, ...] | list[str],
                     confidence: float) -> list[dict]:
    """Run Presidio over a single region of text. Returns a list of
    `{entity_type, start, end, score, value}` dicts. Empty if Presidio
    unavailable or region empty."""
    engine = _get_engine()
    if engine is None or not region.strip():
        return []
    try:
        results = engine.analyze(text=region, entities=list(entities),
                                  language="en", score_threshold=confidence)
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"presidio_gate: analyze failed: {e}\n")
        return []
    return [
        {
            "entity_type": r.entity_type,
            "start": r.start,
            "end": r.end,
            "score": float(r.score),
            "value": region[r.start:r.end],
        }
        for r in results
    ]


def analyze_files(
    files: list[Path],
    *,
    entities: tuple[str, ...] | list[str] = DEFAULT_ENTITIES,
    confidence: float = DEFAULT_CONFIDENCE,
) -> list[dict]:
    """Analyze each file with Presidio. Returns a list of findings in
    the curiosity-merge `Finding` shape (one per file with hits).

    Findings have the same shape as `preflight.find_gdpr_likely_pii`:
      kind="gdpr_likely_pii", severity, subject, summary, rationale,
      samples (local-only).

    Severity rules:
      - Match outside FETCHED markers (user-typed content) → warn
      - Inside markers, always-warn entity (SSN/IBAN/CC/passport/
        driver-license/medical) → warn
      - Inside markers, density-relaxable entity (PERSON/EMAIL/PHONE/
        LOCATION/IP/NRP):
          dense  (>0.5/1000 chars) → warn
          sparse (≤0.5/1000 chars) → info
      - Sub-2000-char fetched region → density math suppressed → warn

    File-level severity = max across kinds.

    Caller is expected to short-circuit when Presidio isn't available
    (see is_available()); this function will return an empty list in
    that case rather than raising.
    """
    if _get_engine() is None:
        return []

    out: list[dict] = []
    for p in files:
        try:
            text = p.read_text(errors="replace")
        except OSError:
            continue

        fetched, user = _split_fetched_user(text)
        user_hits = _analyze_region(user, entities, confidence)
        fetched_hits = _analyze_region(fetched, entities, confidence)
        fetched_len = len(fetched)

        # Bucket by entity type so we can apply density per kind.
        user_by_kind: dict[str, list[dict]] = {}
        fetched_by_kind: dict[str, list[dict]] = {}
        for h in user_hits:
            user_by_kind.setdefault(h["entity_type"], []).append(h)
        for h in fetched_hits:
            fetched_by_kind.setdefault(h["entity_type"], []).append(h)

        # Compose hit lines: (label, severity, count, samples).
        hit_lines: list[tuple[str, str, int, list[str]]] = []

        # User-region hits: always warn, regardless of kind.
        for kind, hits in sorted(user_by_kind.items()):
            hit_lines.append((
                f"{kind} (via Presidio)", "warn", len(hits),
                [h["value"] for h in hits[:5]],
            ))

        # Fetched-region hits: severity per kind.
        for kind, hits in sorted(fetched_by_kind.items()):
            if kind in _ALWAYS_WARN_ENTITIES:
                sev = "warn"
                label = f"{kind} (via Presidio)"
            elif kind in _DENSITY_RELAXABLE_ENTITIES:
                if _is_dense(len(hits), fetched_len):
                    sev = "warn"
                    label = f"{kind} (via Presidio, dense in fetched content)"
                else:
                    sev = "info"
                    label = f"{kind} (via Presidio, sparse in author/contact block)"
            else:
                # Unknown entity (user-customised list) — default to warn.
                sev = "warn"
                label = f"{kind} (via Presidio)"
            hit_lines.append((
                label, sev, len(hits), [h["value"] for h in hits[:5]],
            ))

        if not hit_lines:
            continue

        max_severity = "info"
        for _, sev, _, _ in hit_lines:
            if sev == "warn":
                max_severity = "warn"
                break

        summary = ", ".join(f"{c}×{label}" for label, _, c, _ in hit_lines)
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
                "Microsoft Presidio detected named-entity PII patterns. "
                "Coverage extends beyond the regex baseline: PERSON "
                "names, LOCATION, structured IDs (driver license, "
                "passport, medical license), and country-specific IDs. "
                "Severity model: matches outside FETCHED CONTENT markers "
                "are always WARN. Inside markers, structured-ID kinds "
                "(SSN, IBAN, credit-card, driver license, passport, "
                "medical license) stay WARN; density-relaxable kinds "
                "(PERSON, EMAIL, PHONE, LOCATION, IP, NRP) scale by "
                "density — sparse author/contact-block patterns are "
                "INFO, dense patterns (directory or DB dump) are WARN. "
                "Density threshold is 0.5 matches per 1000 chars. "
                "All analysis runs on the local machine; no content "
                "leaves it. Examples appear in local terminal output "
                "only and are NOT written to the export manifest."
            ),
            "samples": samples_flat,
        })
    return out


# --- cache helpers (used by callers) -------------------------------------


def cache_config_hash(entities: tuple[str, ...] | list[str],
                      confidence: float) -> str:
    """Stable hash of the analyzer config so cache invalidates when the
    user changes entities or confidence.

    Doesn't include the Presidio version; if Presidio is upgraded and
    behaviour changes, users should clear the cache manually. We could
    include the version but importing it adds another failure path.
    """
    payload = json.dumps(
        {"entities": sorted(entities), "confidence": confidence},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]
