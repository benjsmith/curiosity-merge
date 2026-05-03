#!/usr/bin/env python3
"""reconcile.py — vault sha256 + page-stem reconciliation helpers.

Pure functions used by merge.py. No I/O outside of reading the files
the caller hands us. Keeps merge.py's main pipeline readable and lets
us unit-test the reconciliation rules in isolation.

Concepts:

  vault_index            dict[sha256 -> rel_path]   (one file per content)
  vault_alias_map        dict[incoming_rel -> final_rel]
                         every incoming vault file gets mapped to a final
                         relative path under the receiving vault. Identical
                         content is aliased to the existing receiver path;
                         different content under same name is renamed.

  page_collisions        list of dicts describing each page-name clash.
                         { stem, incoming_path, existing_path, kind }
                         kind ∈ {identical, same_topic, different_topic}.
                         The merge driver uses kind to decide write strategy.
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
from pathlib import Path

_ce_scripts = os.environ.get("CURIOSITY_ENGINE_SCRIPTS_DIR")
if _ce_scripts and _ce_scripts not in sys.path:
    sys.path.insert(0, _ce_scripts)
try:
    from naming import read_frontmatter  # type: ignore
except ImportError as e:
    sys.stderr.write(f"reconcile.py: cannot import naming.py ({e})\n")
    raise


# --- sha256 ----------------------------------------------------------------


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def index_vault(vault_dir: Path) -> dict[str, str]:
    """Map sha256 -> first relative path that hashes to it.

    Many vault dirs deduplicate identical content already; if not, the
    first wins (deterministic via sorted walk).
    """
    out: dict[str, str] = {}
    if not vault_dir.is_dir():
        return out
    for p in sorted(vault_dir.rglob("*")):
        if not p.is_file():
            continue
        rel = str(p.relative_to(vault_dir))
        if any(seg.startswith(".") for seg in rel.split(os.sep)):
            continue
        h = sha256_file(p)
        out.setdefault(h, rel)
    return out


# --- vault path reconciliation --------------------------------------------


def reconcile_vault(
    incoming_vault_dir: Path,
    receiver_index: dict[str, str],
    origin: str,
) -> dict:
    """Plan the vault merge.

    Returns:
        {
          "alias_map": {incoming_rel: final_rel, ...},
          "to_copy":   [(incoming_rel, final_rel), ...],
          "deduped":   [(incoming_rel, existing_final_rel), ...],
          "renamed":   [(incoming_rel, final_rel), ...],
        }

    Rules:
      - sha256 already in receiver → alias to receiver's existing path
        (no copy, deduped).
      - sha256 not in receiver, filename free → copy under same rel path.
      - sha256 not in receiver, filename collision (same rel path, diff
        content) → rename incoming to `<stem>.from-<origin><ext>`.
    """
    alias_map: dict[str, str] = {}
    to_copy: list[tuple[str, str]] = []
    deduped: list[tuple[str, str]] = []
    renamed: list[tuple[str, str]] = []

    # Track final paths claimed during *this* run so two incoming files
    # with the same target rel-path can't both win.
    claimed_final: set[str] = set(receiver_index.values())

    for p in sorted(incoming_vault_dir.rglob("*")):
        if not p.is_file():
            continue
        incoming_rel = str(p.relative_to(incoming_vault_dir))
        if any(seg.startswith(".") for seg in incoming_rel.split(os.sep)):
            continue
        h = sha256_file(p)
        if h in receiver_index:
            final_rel = receiver_index[h]
            alias_map[incoming_rel] = final_rel
            deduped.append((incoming_rel, final_rel))
            continue
        # Not deduped — need a final path.
        candidate = incoming_rel
        if candidate in claimed_final:
            stem, ext = os.path.splitext(incoming_rel)
            candidate = f"{stem}.from-{origin}{ext}"
            renamed.append((incoming_rel, candidate))
        alias_map[incoming_rel] = candidate
        to_copy.append((incoming_rel, candidate))
        claimed_final.add(candidate)

    return {
        "alias_map": alias_map,
        "to_copy": to_copy,
        "deduped": deduped,
        "renamed": renamed,
    }


# --- page-name collision classification -----------------------------------


_TOPIC_SAMENESS_THRESHOLD = 0.78  # cosine, used only when an embedder is supplied


def _body_text(text: str) -> str:
    _, body = read_frontmatter(text)
    body = re.sub(r"\s+", " ", body).strip()
    return body[:4000]


def classify_collision(
    incoming_path: Path,
    existing_path: Path,
    *,
    similarity_fn=None,
) -> dict:
    """Decide how to handle a page-name collision.

    Returns: {
      "stem": <stem>,
      "kind": "identical" | "same_topic" | "different_topic",
      "incoming_path": <Path>,
      "existing_path": <Path>,
      "similarity": <float | None>,
    }

    `similarity_fn(text_a, text_b) -> float` is optional. Without it we
    fall back to a length-and-overlap heuristic that's right most of the
    time but biased toward `same_topic` (better to ask the human than to
    silently pick wrong).
    """
    if sha256_file(incoming_path) == sha256_file(existing_path):
        return {
            "stem": incoming_path.stem,
            "kind": "identical",
            "incoming_path": incoming_path,
            "existing_path": existing_path,
            "similarity": 1.0,
        }
    text_a = _body_text(incoming_path.read_text(errors="replace"))
    text_b = _body_text(existing_path.read_text(errors="replace"))
    if similarity_fn is not None:
        sim = float(similarity_fn(text_a, text_b))
    else:
        # Heuristic: shared-token Jaccard over alphanumeric tokens.
        a_toks = set(re.findall(r"[a-z0-9]+", text_a.lower()))
        b_toks = set(re.findall(r"[a-z0-9]+", text_b.lower()))
        if not a_toks or not b_toks:
            sim = 0.0
        else:
            sim = len(a_toks & b_toks) / len(a_toks | b_toks)
    kind = "same_topic" if sim >= _TOPIC_SAMENESS_THRESHOLD else "different_topic"
    return {
        "stem": incoming_path.stem,
        "kind": kind,
        "incoming_path": incoming_path,
        "existing_path": existing_path,
        "similarity": sim,
    }


def find_page_collisions(
    incoming_wiki_dir: Path,
    receiver_wiki_dir: Path,
    *,
    similarity_fn=None,
) -> list[dict]:
    """Return classification dicts for every (incoming_stem, existing_stem)
    page-name match.

    Stem matching is by relative path (so `concepts/transformer.md` only
    collides with `concepts/transformer.md`, not with `entities/transformer.md`).
    """
    if not incoming_wiki_dir.is_dir() or not receiver_wiki_dir.is_dir():
        return []
    existing_by_rel: dict[str, Path] = {}
    for p in receiver_wiki_dir.rglob("*.md"):
        rel = str(p.relative_to(receiver_wiki_dir))
        if any(seg.startswith(".") for seg in rel.split(os.sep)):
            continue
        existing_by_rel[rel] = p
    out: list[dict] = []
    for p in incoming_wiki_dir.rglob("*.md"):
        rel = str(p.relative_to(incoming_wiki_dir))
        if any(seg.startswith(".") for seg in rel.split(os.sep)):
            continue
        if rel in existing_by_rel:
            out.append(
                classify_collision(p, existing_by_rel[rel],
                                   similarity_fn=similarity_fn)
            )
    return out


def collision_target_rel(rel: str, origin: str) -> str:
    """Filename to write a colliding incoming page under.

    `<dir>/<stem>.md` → `<dir>/<stem>-from-<origin>.md`.
    """
    p = Path(rel)
    return str(p.with_name(f"{p.stem}-from-{origin}{p.suffix}"))
