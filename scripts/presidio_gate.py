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
    from presidio_analyzer.nlp_engine import NlpEngineProvider  # type: ignore
    _PRESIDIO_AVAILABLE = True
    _IMPORT_ERROR: Optional[str] = None
except ImportError as e:
    AnalyzerEngine = None  # type: ignore
    NlpEngineProvider = None  # type: ignore
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

# Default language(s). English-only by default; users add via
# --presidio-language for multilingual workspaces. Each language
# requires a corresponding spaCy model installed locally.
DEFAULT_LANGUAGES = ("en",)


# Map of language code → spaCy model name we recommend. Used by setup
# hints and by _get_engine() to construct the NLP config.
LANGUAGE_MODEL_MAP = {
    "en": "en_core_web_lg",
    "fr": "fr_core_news_lg",
    "de": "de_core_news_lg",
    "es": "es_core_news_lg",
    "it": "it_core_news_lg",
    "pt": "pt_core_news_lg",
    "nl": "nl_core_news_lg",
    "zh": "zh_core_web_lg",
    "ja": "ja_core_news_lg",
    "ru": "ru_core_news_lg",
}


# --- combined-inference detector (v0.5.0) -------------------------------
#
# Co-occurring entity combinations that, together, identify a specific
# individual even when no single match would. Windows are *char distances*
# (max-end minus min-start across the combo) — entities within `window`
# chars of each other count as co-occurring.
#
# Windows tuned empirically against the labeled corpus in
# `tuning/inference_corpus.py` via `tuning/tune_inference_windows.py`.
# Re-run that script if you change the corpus or the detector logic.

INFERENCE_COMBINATIONS = {
    # Full identification: name + place + when.
    # Tuned: window=60 (F1=0.94, P=0.91, R=0.98). One of the strongest
    # signals we have for "this is a real person".
    "PERSON_LOCATION_DATE": [{"PERSON"}, {"LOCATION"}, {"DATE_TIME"}],
    # Workplace identification: name + employer.
    # Tuned: window=60 (F1=0.72, P=0.61, R=0.86). Lower precision than
    # the others because spaCy ORG NER is noisier; treated as a
    # surfacing-only signal — the user reviews.
    "PERSON_ORG": [{"PERSON"}, {"ORGANIZATION"}],
    # Age-tied identification: name + age / birth year.
    # Tuned: window=40 (F1=0.88, P=0.92, R=0.84). DATE_TIME in Presidio
    # covers age expressions ("42", "age 33"), birth dates ("born 1985"),
    # and full dates.
    "PERSON_AGE": [{"PERSON"}, {"DATE_TIME"}],
}

INFERENCE_WINDOWS = {
    "PERSON_LOCATION_DATE": 60,
    "PERSON_ORG": 60,
    "PERSON_AGE": 40,
}

# Entities Presidio must surface for the inference detector to function.
# Note: lone ORGANIZATION findings are NOT reported as gdpr_likely_pii
# (every paper mentions Google/MIT — too noisy). ORGANIZATION is only
# used as input to combined-inference detection.
_INFERENCE_REQUIRED_ENTITIES = frozenset({
    "PERSON", "LOCATION", "DATE_TIME", "ORGANIZATION",
})

# PERSON_AGE and PERSON_LOCATION_DATE both need DATE_TIME. If
# PERSON_AGE alone fires (no LOCATION in window) AND PERSON_LOCATION_DATE
# also fires (LOCATION+DATE close to the same PERSON), we'd emit two
# overlapping findings for the same identification. Resolve by
# precedence: if PERSON_LOCATION_DATE fires for a tuple, suppress
# PERSON_AGE for the PERSON involved. Implemented in
# `_find_combined_inference()`.


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

# Engine keyed by sorted-tuple of languages. Multiple languages requires
# the spaCy model installed for each. Switching language sets across
# subgraph-export runs creates separate cached engines.
_ANALYZER_ENGINES: dict[tuple[str, ...], object] = {}


def _build_nlp_config(languages: tuple[str, ...]) -> dict:
    """Build the NlpEngineProvider config for the requested languages.

    Critically: maps spaCy `ORG` → Presidio `ORGANIZATION`. Presidio's
    default mapping omits ORG entirely, which kills the PERSON_ORG
    inference detector. ORG gets a 0.4 confidence multiplier (vs the
    PERSON/LOC default of 0.85) to reflect that ORG NER is inherently
    noisier; downstream detectors can still find it because we set
    the analyzer score threshold low enough to admit it.
    """
    return {
        "nlp_engine_name": "spacy",
        "models": [
            {"lang_code": lang,
             "model_name": LANGUAGE_MODEL_MAP.get(lang, f"{lang}_core_news_lg")}
            for lang in languages
        ],
        "ner_model_configuration": {
            "model_to_presidio_entity_mapping": {
                "PER": "PERSON", "PERSON": "PERSON",
                "LOC": "LOCATION", "GPE": "LOCATION", "FAC": "LOCATION",
                "ORG": "ORGANIZATION",
                "DATE": "DATE_TIME", "TIME": "DATE_TIME",
                "NORP": "NRP",
                "MISC": "NRP",
            },
            "low_confidence_score_multiplier": 0.4,
            "low_score_entity_names": ["ORGANIZATION"],
        },
    }


def _get_engine(languages: tuple[str, ...] = DEFAULT_LANGUAGES):
    """Lazily initialize and cache the AnalyzerEngine for a language
    set. Returns None if Presidio isn't available or a spaCy model is
    missing for one of the requested languages.

    The engine is offline by construction (spaCy NLP + offline custom
    recognizers; no cloud-backed recognizers ever instantiated).
    """
    global _ANALYZER_ENGINES
    if not _PRESIDIO_AVAILABLE:
        return None
    key = tuple(sorted(languages))
    if key not in _ANALYZER_ENGINES:
        try:
            nlp_engine = NlpEngineProvider(
                nlp_configuration=_build_nlp_config(key)
            ).create_engine()
            _ANALYZER_ENGINES[key] = AnalyzerEngine(
                nlp_engine=nlp_engine,
                supported_languages=list(key),
            )
        # Catch BaseException because Presidio internally calls
        # sys.exit() when a model can't be loaded (which raises
        # SystemExit, not a regular Exception). We always want a
        # graceful is_available()=False rather than the whole CLI
        # crashing because the user asked for an uninstalled language.
        except BaseException as e:  # noqa: BLE001
            missing = [
                f"  uv run python -m spacy download "
                f"{LANGUAGE_MODEL_MAP.get(lang, f'{lang}_core_news_lg')}"
                for lang in key
            ]
            sys.stderr.write(
                f"presidio_gate: failed to initialize AnalyzerEngine "
                f"for languages {list(key)}: {e}\n"
                "  Required spaCy models (install whichever is missing):\n"
                + "\n".join(missing) + "\n"
            )
            return None
    return _ANALYZER_ENGINES[key]


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


def is_available(languages: tuple[str, ...] = DEFAULT_LANGUAGES
                  ) -> tuple[bool, Optional[str]]:
    """Check if Presidio is importable AND the analyzer can initialize
    for the requested languages. Returns (available, reason)."""
    if not _PRESIDIO_AVAILABLE:
        return False, (
            f"presidio-analyzer not importable: {_IMPORT_ERROR}. "
            "Install via: uv pip install presidio-analyzer && "
            "uv run python -m spacy download en_core_web_lg"
        )
    if _get_engine(languages) is None:
        models_needed = [
            LANGUAGE_MODEL_MAP.get(lang, f"{lang}_core_news_lg")
            for lang in languages
        ]
        return False, (
            f"presidio-analyzer imported but AnalyzerEngine failed for "
            f"languages {list(languages)} (likely missing spaCy model). "
            f"Run: uv run python -m spacy download <model>  for each of: "
            + ", ".join(models_needed)
        )
    return True, None


def _analyze_region(region: str, entities: tuple[str, ...] | list[str],
                     confidence: float,
                     languages: tuple[str, ...] = DEFAULT_LANGUAGES
                     ) -> list[dict]:
    """Run Presidio over a single region of text. Returns a list of
    `{entity_type, start, end, score, value}` dicts. Empty if Presidio
    unavailable or region empty.

    When multiple languages are configured, we run analyze() once per
    language and merge — Presidio's single-call API takes one `language`
    argument. This is O(n_langs * region_len) for NER; for typical
    workspaces with one or two languages it's fine.
    """
    engine = _get_engine(languages)
    if engine is None or not region.strip():
        return []
    out: list[dict] = []
    seen_spans: set[tuple[int, int, str]] = set()
    for lang in languages:
        try:
            results = engine.analyze(
                text=region, entities=list(entities),
                language=lang, score_threshold=confidence,
            )
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(
                f"presidio_gate: analyze failed for lang={lang}: {e}\n"
            )
            continue
        for r in results:
            key = (r.start, r.end, r.entity_type)
            if key in seen_spans:
                continue
            seen_spans.add(key)
            out.append({
                "entity_type": r.entity_type,
                "start": r.start,
                "end": r.end,
                "score": float(r.score),
                "value": region[r.start:r.end],
            })
    return out


# --- combined-inference detection ----------------------------------------


def _find_combined_inference(entities: list[dict]) -> dict[str, list[dict]]:
    """For each combination type, return a list of hit dicts.

    A "hit" is a tuple of co-occurring entities (one per required group)
    whose span (max-end minus min-start) is within the tuned window.

    Returns dict keyed by combination name, each mapping to a list of
    `{start, end, kinds, values}` hit records. Empty list when no
    qualifying tuples found.

    Resolution rule (PERSON_LOCATION_DATE vs PERSON_AGE precedence):
    if a PERSON+LOC+DATE triple fires, its constituent PERSON+DATE pair
    is suppressed from PERSON_AGE — otherwise we'd report two findings
    for one identification. PERSON_ORG never overlaps with the others.
    """
    from itertools import product

    out: dict[str, list[dict]] = {}
    for combo_name, groups in INFERENCE_COMBINATIONS.items():
        window = INFERENCE_WINDOWS[combo_name]
        buckets: list[list[dict]] = [[] for _ in groups]
        for ent in entities:
            for i, group in enumerate(groups):
                if ent["entity_type"] in group:
                    buckets[i].append(ent)
        if not all(buckets):
            out[combo_name] = []
            continue
        hits: list[dict] = []
        seen: set[tuple[int, int]] = set()
        for combo in product(*buckets):
            starts = [e["start"] for e in combo]
            ends = [e["end"] for e in combo]
            min_s, max_e = min(starts), max(ends)
            span = max_e - min_s
            if span > window:
                continue
            key = (min_s, max_e)
            if key in seen:
                continue
            seen.add(key)
            hits.append({
                "start": min_s,
                "end": max_e,
                "kinds": [e["entity_type"] for e in combo],
                "values": [e["value"] for e in combo],
            })
        out[combo_name] = hits

    # Suppress PERSON_AGE hits whose PERSON+DATE span is enclosed by a
    # PERSON_LOCATION_DATE hit. Avoids double-counting the same triple.
    if out.get("PERSON_LOCATION_DATE") and out.get("PERSON_AGE"):
        suppressed = []
        for age_hit in out["PERSON_AGE"]:
            ranges = [(h["start"], h["end"])
                       for h in out["PERSON_LOCATION_DATE"]]
            covered = any(
                s <= age_hit["start"] and age_hit["end"] <= e
                for s, e in ranges
            )
            if not covered:
                suppressed.append(age_hit)
        out["PERSON_AGE"] = suppressed

    return out


def _format_inference_samples(hits: list[dict], *, kind_label: str,
                               cap: int = 5) -> list[str]:
    """Build local-only sample strings for the finding's samples list.
    Each shows the entity types and values of one inference tuple."""
    out: list[str] = []
    for h in hits[:cap]:
        kinds = "+".join(h["kinds"])
        values = " / ".join(h["values"])
        out.append(f"{kind_label} [{kinds}]: {values}")
    return out


def analyze_files(
    files: list[Path],
    *,
    entities: tuple[str, ...] | list[str] = DEFAULT_ENTITIES,
    confidence: float = DEFAULT_CONFIDENCE,
    languages: tuple[str, ...] = DEFAULT_LANGUAGES,
) -> list[dict]:
    """Analyze each file with Presidio. Returns a list of findings:

    - One `gdpr_likely_pii` finding per file with hits (entity matches
      in the user-requested `entities` list, density-scaled severity).
    - One `gdpr_combined_inference` finding per file where co-occurring
      entity combinations (PERSON+LOC+DATE, PERSON+ORG, PERSON+AGE)
      identify a specific individual.

    Findings carry the manifest-safe contract: `samples` is local-only
    and stripped at manifest-write time.

    Severity rules (apply to both finding kinds):
      - Outside FETCHED markers → warn (user-typed content)
      - Inside markers, always-warn entities (SSN/IBAN/CC/passport/
        driver-license/medical) → warn
      - Inside markers, density-relaxable kinds (PERSON/EMAIL/PHONE/
        LOCATION/IP/NRP) → density-scaled (sparse=info, dense=warn)
      - Inside markers, combined-inference hits → count-density-scaled
        with 0.1/1000-char threshold and a 2000-char floor below which
        severity is info (incidental mention in short docs)

    File-level severity within each finding = max across kinds.

    Languages: each language requires a corresponding spaCy model. Runs
    analyze() once per language and merges deduped results.
    """
    if _get_engine(languages) is None:
        return []

    # Always request the union of user-requested entities and the ones
    # needed by the combined-inference detector. ORGANIZATION rides
    # along for inference even when not in `entities` — but lone ORG
    # findings are filtered back out of the gdpr_likely_pii output.
    requested = set(entities)
    actual_entities = tuple(requested | _INFERENCE_REQUIRED_ENTITIES)

    out: list[dict] = []
    for p in files:
        try:
            text = p.read_text(errors="replace")
        except OSError:
            continue

        fetched, user = _split_fetched_user(text)
        # Use the union (entities + inference-required) for the analyze
        # call, but bucket-filter back to user-requested entities when
        # building the gdpr_likely_pii finding.
        user_hits_full = _analyze_region(
            user, actual_entities, confidence, languages,
        )
        fetched_hits_full = _analyze_region(
            fetched, actual_entities, confidence, languages,
        )
        user_hits = [h for h in user_hits_full
                     if h["entity_type"] in requested]
        fetched_hits = [h for h in fetched_hits_full
                         if h["entity_type"] in requested]
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

        # gdpr_likely_pii is only emitted when individual-entity buckets
        # produced lines. combined-inference (below) runs independently
        # so a file with only inference signals still surfaces.
        if hit_lines:
            max_severity = "info"
            for _, sev, _, _ in hit_lines:
                if sev == "warn":
                    max_severity = "warn"
                    break

            summary = ", ".join(
                f"{c}×{label}" for label, _, c, _ in hit_lines
            )
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
                    "Microsoft Presidio detected named-entity PII "
                    "patterns. Coverage extends beyond the regex "
                    "baseline: PERSON names, LOCATION, structured IDs "
                    "(driver license, passport, medical license), and "
                    "country-specific IDs. Severity model: matches "
                    "outside FETCHED CONTENT markers are always WARN. "
                    "Inside markers, structured-ID kinds (SSN, IBAN, "
                    "credit-card, driver license, passport, medical "
                    "license) stay WARN; density-relaxable kinds "
                    "(PERSON, EMAIL, PHONE, LOCATION, IP, NRP) scale "
                    "by density — sparse author/contact-block patterns "
                    "are INFO, dense patterns (directory or DB dump) "
                    "are WARN. Density threshold is 0.5 matches per "
                    "1000 chars. All analysis runs on the local "
                    "machine; no content leaves it. Examples appear "
                    "in local terminal output only and are NOT "
                    "written to the export manifest."
                ),
                "samples": samples_flat,
            })

        # ---- combined-inference (gdpr_combined_inference) -----------
        # Use the FULL hit lists (including ORGANIZATION, regardless of
        # what the user requested in `entities`). PERSON+ORG inference
        # is the whole point of including ORG in the analyzer call.
        user_inference = _find_combined_inference(user_hits_full)
        fetched_inference = _find_combined_inference(fetched_hits_full)

        # Severity model for combined-inference:
        # - Any hit outside FETCHED markers → warn (user-typed content)
        # - Inside markers, count-density: > 0.1 per 1000 chars → warn;
        #   otherwise info. Floor: < 2000 chars → info regardless
        #   (incidental mention in short docs shouldn't be a directory-
        #   dump signal).
        inference_lines: list[tuple[str, str, int, list[str]]] = []
        for combo_name, hits in user_inference.items():
            if hits:
                inference_lines.append((
                    f"{combo_name} (via Presidio)", "warn", len(hits),
                    _format_inference_samples(hits, kind_label=combo_name),
                ))
        for combo_name, hits in fetched_inference.items():
            if not hits:
                continue
            if fetched_len < 2000:
                sev = "info"
                label = (f"{combo_name} (via Presidio, short fetched "
                          "region; incidental)")
            elif (len(hits) / fetched_len) > 0.1 / 1000:
                sev = "warn"
                label = (f"{combo_name} (via Presidio, dense in fetched "
                          "content)")
            else:
                sev = "info"
                label = (f"{combo_name} (via Presidio, sparse "
                          "incidental mention)")
            inference_lines.append((
                label, sev, len(hits),
                _format_inference_samples(hits, kind_label=combo_name),
            ))

        if not inference_lines:
            continue

        inf_max_severity = "info"
        for _, sev, _, _ in inference_lines:
            if sev == "warn":
                inf_max_severity = "warn"
                break

        inf_summary = ", ".join(
            f"{c}×{label}" for label, _, c, _ in inference_lines
        )
        inf_samples: list[str] = []
        for label, _, _, samples in inference_lines:
            for s in samples:
                inf_samples.append(f"{label}: {s}")

        out.append({
            "kind": "gdpr_combined_inference",
            "severity": inf_max_severity,
            "subject": str(p),
            "summary": f"co-occurring entity combinations ({inf_summary})",
            "rationale": (
                "Presidio detected entity combinations that, together, "
                "identify a specific individual: PERSON co-occurring "
                "with LOCATION+DATE_TIME (full identification), with "
                "ORGANIZATION (workplace identification), or with "
                "DATE_TIME (age/birth-year identification). Under GDPR "
                "Recital 26 and similar regimes, combined-data PII is "
                "actionable even when no single entity alone would be. "
                "Detection windows are empirically tuned: 60 chars for "
                "LOCATION_DATE and ORG, 40 chars for AGE. To redact: "
                "remove or generalize ONE of the identifying components "
                "(e.g., drop the birth year, anonymize the location). "
                "Severity follows the same density rule as other PII "
                "findings — sparse incidental mentions inside FETCHED "
                "markers are INFO; dense patterns and any user-typed "
                "match are WARN. Examples shown locally only; never "
                "written to the export manifest."
            ),
            "samples": inf_samples,
        })

    return out


# --- cache helpers (used by callers) -------------------------------------


def cache_config_hash(entities: tuple[str, ...] | list[str],
                      confidence: float,
                      languages: tuple[str, ...] | list[str] | None = None
                      ) -> str:
    """Stable hash of the analyzer config so cache invalidates when the
    user changes entities, confidence, or language set.

    Doesn't include the Presidio version; if Presidio is upgraded and
    behaviour changes, users should clear the cache manually.
    """
    langs = sorted(languages) if languages else list(DEFAULT_LANGUAGES)
    payload = json.dumps(
        {
            "entities": sorted(entities),
            "confidence": confidence,
            "languages": langs,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]
