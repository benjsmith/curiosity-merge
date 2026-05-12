"""tune_inference_windows.py — empirical threshold selection for the
combined-data inference detector.

Per combination type, run a candidate detector at a range of char-window
values over the labeled corpus. For each window, compute precision,
recall, F1. Pick the window that maximizes F1 (with precision tiebreaker)
and print a summary.

Output is a tuned-window dict; copy these values into
presidio_gate.INFERENCE_WINDOWS.

Run:
    uv run python tuning/tune_inference_windows.py
"""
from __future__ import annotations

import sys
from pathlib import Path

# Local imports
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from inference_corpus import CORPORA  # type: ignore
from presidio_analyzer import AnalyzerEngine  # type: ignore
from presidio_analyzer.nlp_engine import NlpEngineProvider  # type: ignore


# Custom NLP engine config that surfaces spaCy ORG → ORGANIZATION.
# Presidio's default mapping omits ORG; without this every PERSON_ORG
# detection fails outright. ORG gets a confidence-multiplier of 0.4
# (vs 0.85 default) to reflect that spaCy ORG NER is noisier than
# PERSON/LOCATION.
_NLP_CONFIG = {
    "nlp_engine_name": "spacy",
    "models": [{"lang_code": "en", "model_name": "en_core_web_lg"}],
    "ner_model_configuration": {
        "model_to_presidio_entity_mapping": {
            "PER": "PERSON", "PERSON": "PERSON",
            "LOC": "LOCATION", "GPE": "LOCATION", "FAC": "LOCATION",
            "ORG": "ORGANIZATION",
            "DATE": "DATE_TIME", "TIME": "DATE_TIME",
            "NORP": "NRP",
        },
        "low_confidence_score_multiplier": 0.4,
        "low_score_entity_names": ["ORGANIZATION"],
    },
}


# Window candidates to evaluate (chars between leftmost-start and
# rightmost-end of a candidate entity tuple).
WINDOWS = [20, 30, 40, 50, 60, 75, 100, 125, 150, 200, 300]


# Combination spec per type. Each entry says: which entities form the
# combination, and how many of them must co-occur within the window for
# a positive detection.
#
# DATE_TIME is shared between PERSON_LOCATION_DATE (full identification)
# and PERSON_AGE (age-tied identification) because Presidio surfaces
# ages, birth-years, and dates uniformly as DATE_TIME.
#
# PERSON_MEDICAL is OUT OF SCOPE for v0.5.0: Presidio's MEDICAL_LICENSE
# recognizer matches only the US DEA Certificate Number format
# (`[a-z][a-z]\d{7}`). General medical license patterns (NPI, RN-*,
# state-specific MD-* etc.) need custom recognizers we'd have to write
# ourselves. Documented as a known limitation.
COMBINATIONS = {
    "PERSON_LOCATION_DATE": {
        "required": [{"PERSON"}, {"LOCATION"}, {"DATE_TIME"}],
    },
    "PERSON_ORG": {
        "required": [{"PERSON"}, {"ORGANIZATION"}],
    },
    "PERSON_AGE": {
        "required": [{"PERSON"}, {"DATE_TIME"}],
    },
}

# Drop PERSON_MEDICAL from the corpus — see note above.
del CORPORA["PERSON_MEDICAL"]


def detect(entities: list, required_groups: list[set],
            window: int) -> bool:
    """Return True if the text has at least one match where one entity
    from each required_group appears within `window` chars (measured
    from leftmost start to rightmost end across the matched tuple).

    Implementation: for each combination of (one entity per group),
    check whether the span fits within the window. Brute force across
    small entity counts (<20) is fine.
    """
    # Bucket entities by group membership.
    groups: list[list] = [[] for _ in required_groups]
    for ent in entities:
        for i, g in enumerate(required_groups):
            if ent.entity_type in g:
                groups[i].append(ent)

    # Need at least one entity in each group.
    if not all(groups):
        return False

    # Cartesian product. For 3 groups with 5 entities each, that's 125
    # combos — fine. Worst case in our corpus is bounded.
    from itertools import product
    for combo in product(*groups):
        starts = [e.start for e in combo]
        ends = [e.end for e in combo]
        span = max(ends) - min(starts)
        if span <= window:
            return True
    return False


def precision_recall(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    p = tp / (tp + fp) if (tp + fp) else 0.0
    r = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * p * r / (p + r) if (p + r) else 0.0
    return p, r, f1


def main() -> int:
    print("loading Presidio AnalyzerEngine ...")
    nlp_engine = NlpEngineProvider(nlp_configuration=_NLP_CONFIG).create_engine()
    engine = AnalyzerEngine(
        nlp_engine=nlp_engine, supported_languages=["en"],
    )

    # Cache analyzer results per text — cheap memoization across windows.
    cache: dict[str, list] = {}

    def analyze(text: str) -> list:
        if text not in cache:
            # Pull all entity types relevant to any combination — superset
            # of what any single combination needs.
            entity_types = sorted(set().union(
                *[g for c in COMBINATIONS.values() for g in c["required"]]
            ))
            # Lower threshold than the default 0.6 because ORGANIZATION
            # gets a 0.4 confidence-multiplier (noisier NER) and its
            # final score (~0.34) would otherwise be filtered out. PERSON
            # / LOCATION / DATE_TIME default to 0.85 unaffected.
            cache[text] = engine.analyze(
                text=text, entities=entity_types, language="en",
                score_threshold=0.3,
            )
        return cache[text]

    chosen: dict[str, int] = {}

    for combo_name, samples in CORPORA.items():
        spec = COMBINATIONS[combo_name]
        required = spec["required"]
        print(f"\n=== {combo_name} ({len(samples)} samples, "
              f"{sum(1 for _, l in samples if l)} positive) ===")
        print(f"{'window':>8s}  {'precision':>10s}  {'recall':>8s}  "
              f"{'F1':>6s}  {'TP':>3s} {'FP':>3s} {'FN':>3s} {'TN':>3s}")

        results = []
        for window in WINDOWS:
            tp = fp = fn = tn = 0
            for text, expected in samples:
                ents = analyze(text)
                got = detect(ents, required, window)
                if got and expected:
                    tp += 1
                elif got and not expected:
                    fp += 1
                elif not got and expected:
                    fn += 1
                else:
                    tn += 1
            p, r, f1 = precision_recall(tp, fp, fn)
            print(f"{window:>8d}  {p:>10.3f}  {r:>8.3f}  {f1:>6.3f}  "
                  f"{tp:>3d} {fp:>3d} {fn:>3d} {tn:>3d}")
            results.append((window, p, r, f1, tp, fp, fn, tn))

        # Pick the window with highest F1; tiebreak on higher precision
        # (we prefer fewer false positives — false flags train users to
        # ignore findings).
        best = max(results, key=lambda r: (r[3], r[1], -r[0]))
        chosen[combo_name] = best[0]
        print(f"  → chosen window: {best[0]}  (F1={best[3]:.3f}, "
              f"P={best[1]:.3f}, R={best[2]:.3f})")

    print("\n" + "=" * 60)
    print("Tuned windows for presidio_gate.INFERENCE_WINDOWS:")
    print("=" * 60)
    for combo_name, w in chosen.items():
        print(f"    {combo_name!r}: {w},")

    return 0


if __name__ == "__main__":
    sys.exit(main())
