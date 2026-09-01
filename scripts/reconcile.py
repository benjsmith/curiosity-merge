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
                         kind ∈ {identical, source_link_fold, same_topic,
                         different_topic}.
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
    from naming import read_frontmatter, WIKILINK_RE  # type: ignore
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


# Untrusted-merge framing written by merge.py `_frame_body`. Apply-time
# comparisons must ignore it: a staged page is always framed, so a
# whole-body hash would never match the receiver (or a previously-applied
# framed page from another origin).
_UNTRUSTED_FRAME_RE = re.compile(
    r"<!-- BEGIN UNTRUSTED MERGED CONTENT — origin:[a-z0-9][a-z0-9_-]{0,63} -->\s*"
    r"(.*?)"
    r"\s*<!-- END UNTRUSTED MERGED CONTENT -->",
    re.DOTALL,
)


def page_body(text: str) -> str:
    """Frontmatter-stripped body, with merge framing removed if present.

    Leading/trailing whitespace is stripped so an otherwise-identical
    page whose export only differs in YAML (`updated:`, `projects:`,
    a newly minted `iri:`) still hashes as identical. Internal
    whitespace is preserved — a curator rewrite is a real edit.
    """
    _, body = read_frontmatter(text)
    m = _UNTRUSTED_FRAME_RE.search(body)
    if m:
        body = m.group(1)
    return body.strip()


def body_sha256(path: Path) -> str:
    """sha256 of `page_body` for the file at `path`."""
    return hashlib.sha256(
        page_body(path.read_text(errors="replace")).encode("utf-8")
    ).hexdigest()


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

    # Sharing-safe exports omit vault/ entirely. Treat that as "no
    # incoming files" rather than raising — the merge driver's
    # vault_missing tagging pass picks up every citation.
    if not incoming_vault_dir.is_dir():
        return {"alias_map": alias_map, "to_copy": to_copy,
                "deduped": deduped, "renamed": renamed}

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


# --- source stubs: wikilink-only differences -------------------------------
#
# A `wiki/sources/<stem>.md` stub is a thin provenance page: title, a
# citation, and the reciprocal `[[page]]` links back to whatever cites it.
# Parallel shard CURATE rewires exactly those links, so the same stub comes
# back from N shards with N different link sets and nothing else changed.
# Forking those into `sources/<stem>-from-<origin>.md` floods the parent
# wiki with duplicate source pages. The signature below is what the stub
# says *apart from* its links; when two stubs agree on it, the links get
# folded together instead of forking.

_LIST_MARKER_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
# The heading a reciprocal-link block gets written under. CURATE adds the
# label and the links together, so a stub that gained its first backlink
# gained this line too; ignoring the label keeps that a fold rather than a
# fork. Deliberately a closed list — any other added prose is real content
# and must still fork.
_LINK_LABEL_RE = re.compile(
    r"^#{0,6}\s*(?:cited by|cites|citing pages|related|related pages|"
    r"links|linked from|backlinks|references|referenced by|see also)$",
    re.IGNORECASE,
)
_SEPARATOR_RUN_RE = re.compile(r"\s*[,;|·•]+\s*")
_TRAILING_PUNCT_RE = re.compile(r"[\s,;:|·•—–-]+$")
_ALNUM_RE = re.compile(r"[0-9A-Za-z]")

# Conservative slug shape for a folded link target. Folded links are the one
# thing an origin gets to write into a live receiver page without passing
# through staging, so keep the accepted alphabet tight: no traversal, no
# markup, no newlines.
_SAFE_LINK_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._/-]{0,127}$")


def link_target(token: str) -> str:
    """`[[stem|display]]` / `[[stem#heading]]` → `stem`."""
    inner = token[2:-2]
    inner = inner.split("|", 1)[0].split("#", 1)[0]
    return inner.strip()


def wikilink_targets(body: str) -> list[str]:
    """Ordered, case-insensitively deduped `[[link]]` targets in `body`."""
    out: list[str] = []
    seen: set[str] = set()
    for m in WIKILINK_RE.finditer(body):
        t = link_target(m.group(0))
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


def safe_link_target(target: str) -> bool:
    """True when `target` is a plain wiki slug we're willing to fold."""
    return bool(_SAFE_LINK_RE.match(target)) and ".." not in target


def stub_signature(text: str) -> str:
    """What a page says with its wikilinks removed.

    Wikilinks are dropped, then list markers, separator punctuation and
    whitespace are normalized away, then lines left with no alphanumeric
    content at all (a bare `- [[link]]` bullet, say) are discarded, along
    with the bare label a link block sits under (`Cited by:`). Two stubs
    whose signatures match differ only in which pages they link to.
    """
    lines: list[str] = []
    for line in page_body(text).splitlines():
        line = WIKILINK_RE.sub(" ", line)
        line = _LIST_MARKER_RE.sub("", line)
        line = _SEPARATOR_RUN_RE.sub(" ", line)
        line = re.sub(r"\s+", " ", line).strip()
        line = _TRAILING_PUNCT_RE.sub("", line)
        if not _ALNUM_RE.search(line):
            continue
        if _LINK_LABEL_RE.match(line):
            continue
        lines.append(line)
    return "\n".join(lines)


def is_source_page(path: Path, *, rel: str | None = None,
                   text: str | None = None) -> bool:
    """True for `sources/` pages and for anything with `type: source`."""
    parts = Path(rel).parts if rel else (path.parent.name,)
    if "sources" in parts:
        return True
    if text is None:
        try:
            text = path.read_text(errors="replace")
        except OSError:
            return False
    fm, _ = read_frontmatter(text)
    return (fm.get("type") or "") == "source"


def is_source_link_fold(incoming_text: str, existing_text: str) -> bool:
    """True when two source stubs differ only in their wikilinks.

    Callers must have established that both pages are source stubs; this
    only judges the bodies. Identical bodies are *not* a fold — those are
    already handled as an identical-body drop.
    """
    if page_body(incoming_text) == page_body(existing_text):
        return False
    return stub_signature(incoming_text) == stub_signature(existing_text)


def missing_wikilinks(canonical_text: str, links: list[str]) -> list[str]:
    """`links` (in order) that the canonical page does not already carry."""
    have = {t.lower() for t in wikilink_targets(page_body(canonical_text))}
    out: list[str] = []
    for t in links:
        key = t.lower()
        if key in have or not safe_link_target(t):
            continue
        have.add(key)
        out.append(t)
    return out


_FRAME_END = "<!-- END UNTRUSTED MERGED CONTENT -->"


def fold_wikilinks(canonical_text: str,
                   links: list[str]) -> tuple[str, list[str]]:
    """Add the links the canonical stub is missing. Returns (text, added).

    Each added link becomes its own `- [[link]]` bullet at the end of the
    body — inside the untrusted frame when the canonical page carries one,
    so merged content stays framed. Nothing else about the page is touched,
    which keeps the fold idempotent: re-folding the same links is a no-op.
    """
    added = missing_wikilinks(canonical_text, links)
    if not added:
        return canonical_text, []
    block = "\n".join(f"- [[{t}]]" for t in added)
    idx = canonical_text.rfind(_FRAME_END)
    head = (canonical_text if idx == -1 else canonical_text[:idx]).rstrip("\n")
    tail = "" if idx == -1 else canonical_text[idx:]
    last = head.splitlines()[-1] if head.splitlines() else ""
    # Continue an existing link list rather than opening a loose one.
    joiner = ("\n" if _LIST_MARKER_RE.match(last) and WIKILINK_RE.search(last)
              else "\n\n")
    if idx == -1:
        return head + joiner + block + "\n", added
    return f"{head}{joiner}{block}\n\n{tail}", added


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
    rel: str | None = None,
    similarity_fn=None,
) -> dict:
    """Decide how to handle a page-name collision.

    Returns: {
      "stem": <stem>,
      "kind": "identical" | "source_link_fold" | "same_topic"
              | "different_topic",
      "incoming_path": <Path>,
      "existing_path": <Path>,
      "similarity": <float | None>,
      "fold_links": [<target>, ...],   # source_link_fold only
    }

    `similarity_fn(text_a, text_b) -> float` is optional. Without it we
    fall back to a length-and-overlap heuristic that's right most of the
    time but biased toward `same_topic` (better to ask the human than to
    silently pick wrong).

    `identical` is **body** identity, not whole-file identity. Two pages
    whose prose matches but whose frontmatter differs (`updated:`,
    `projects:`, a minted `iri:`) are dropped, not staged as
    `<stem>-from-<origin>.md`. Whole-file sha256 is a fast path only.

    `source_link_fold` is the source-stub case one step out from that:
    both sides are source stubs saying the same thing, and only their
    `[[wikilinks]]` differ. The incoming links fold into the canonical
    stub — no `<stem>-from-<origin>.md` for that either.
    """
    if sha256_file(incoming_path) == sha256_file(existing_path):
        return {
            "stem": incoming_path.stem,
            "kind": "identical",
            "incoming_path": incoming_path,
            "existing_path": existing_path,
            "similarity": 1.0,
        }
    # Body-only: the shard-rejoin smoke case. Parallel curation bumps
    # frontmatter on pages it never rewrote; those must not land in live
    # wiki/ as review copies.
    if body_sha256(incoming_path) == body_sha256(existing_path):
        return {
            "stem": incoming_path.stem,
            "kind": "identical",
            "incoming_path": incoming_path,
            "existing_path": existing_path,
            "similarity": 1.0,
        }
    inc_text = incoming_path.read_text(errors="replace")
    exi_text = existing_path.read_text(errors="replace")
    # Source stubs differing only in reciprocal wikilinks: fold, never fork.
    if (is_source_page(incoming_path, rel=rel, text=inc_text)
            and is_source_page(existing_path, rel=rel, text=exi_text)
            and is_source_link_fold(inc_text, exi_text)):
        return {
            "stem": incoming_path.stem,
            "kind": "source_link_fold",
            "incoming_path": incoming_path,
            "existing_path": existing_path,
            "similarity": 1.0,
            "fold_links": missing_wikilinks(
                exi_text, wikilink_targets(page_body(inc_text))),
        }
    text_a = _body_text(inc_text)
    text_b = _body_text(exi_text)
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
                classify_collision(p, existing_by_rel[rel], rel=rel,
                                   similarity_fn=similarity_fn)
            )
    return out


def collision_target_rel(rel: str, origin: str) -> str:
    """Filename to write a colliding incoming page under.

    `<dir>/<stem>.md` → `<dir>/<stem>-from-<origin>.md`.
    """
    p = Path(rel)
    return str(p.with_name(f"{p.stem}-from-{origin}{p.suffix}"))
