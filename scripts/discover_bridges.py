#!/usr/bin/env python3
"""discover_bridges.py — surface high-similarity wiki page pairs that
aren't already wikilinked.

Useful within a single wiki (find concept pages that should be cross-
linked) and across origins after a merge (`--across-origins` filters to
pairs where the two pages have different `origin:` tags).

Cold-start guard: if `embedding_enabled` isn't true in `.curator/
config.json`, or sentence-transformers isn't importable, we exit cleanly
with a clear message rather than producing noise. curiosity-engine
embeds vault sources, not wiki pages — pages are short enough to embed
on-the-fly per run, so we do that here instead of writing a new sqlite
table.

Output: `.curator/bridges-<timestamp>.md` — a human-readable review
queue. Sorted by similarity descending; capped by `--limit` (default
50). Each entry shows the two pages, their similarity score, their
origin tags (when relevant), and a one-line context excerpt from each.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import sys
from pathlib import Path

_ce_scripts = os.environ.get("CURIOSITY_ENGINE_SCRIPTS_DIR")
if _ce_scripts and _ce_scripts not in sys.path:
    sys.path.insert(0, _ce_scripts)
try:
    from naming import read_frontmatter, WIKILINK_RE  # type: ignore
    from sweep import wiki_pages  # type: ignore
except ImportError as e:
    sys.stderr.write(
        "ERROR: cannot import curiosity-engine helpers.\n"
        "       Set CURIOSITY_ENGINE_SCRIPTS_DIR or run scripts/setup.sh.\n"
        f"       (Original import error: {e})\n"
    )
    sys.exit(2)


CONFIG_PATH = Path(".curator/config.json")
DEFAULT_EMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
DEFAULT_THRESHOLD = 0.55
DEFAULT_LIMIT = 50
# Embedding input cap. Pages can be long; we want the gist, not the prose.
# 4000 chars is enough to capture the page's topic sentences plus context
# without blowing out the encoder's input window.
EMBED_INPUT_CAP = 4000


def _load_config(workspace: Path) -> dict:
    cfg = workspace / CONFIG_PATH
    if not cfg.is_file():
        return {}
    try:
        return json.loads(cfg.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _strip_for_embedding(text: str) -> str:
    """Frontmatter + wikilinks markup removed; collapse whitespace."""
    _, body = read_frontmatter(text)
    # Wikilink-target text is usually meaningful — keep it but drop brackets.
    body = re.sub(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]", r"\1", body)
    body = re.sub(r"\(vault:[^)]+\)", "", body)
    body = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)
    body = re.sub(r"\s+", " ", body).strip()
    return body[:EMBED_INPUT_CAP]


def _page_origin(text: str) -> str:
    fm, _ = read_frontmatter(text)
    val = fm.get("origin", "")
    if isinstance(val, list):
        val = val[0] if val else ""
    return val.strip() if isinstance(val, str) else ""


def _page_excerpt(text: str, n: int = 140) -> str:
    body = _strip_for_embedding(text)
    return (body[:n] + "...") if len(body) > n else body


def _build_link_set(pages: list[Path]) -> set[tuple[str, str]]:
    """Set of (a, b) pairs where a wikilink exists from a to b (lowercase
    stems, alphabetized so the membership check is order-insensitive).
    """
    by_stem = {p.stem.lower(): p for p in pages}
    edges: set[tuple[str, str]] = set()
    for p in pages:
        own = p.stem.lower()
        text = p.read_text(errors="replace")
        for m in WIKILINK_RE.finditer(text):
            tgt = m.group(1).strip().lower().replace(" ", "-").rsplit("/", 1)[-1]
            if tgt in by_stem and tgt != own:
                a, b = sorted((own, tgt))
                edges.add((a, b))
    return edges


def _cosine(a, b):
    # Both vectors are L2-normalized by the encoder, so dot product == cosine.
    return float(sum(x * y for x, y in zip(a, b)))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="discover_bridges.py",
        description="Surface high-similarity wiki pages that aren't yet wikilinked.",
    )
    ap.add_argument("--workspace", default=".",
                    help="curiosity-engine workspace root (default: cwd)")
    ap.add_argument("--across-origins", action="store_true",
                    help="restrict to pairs whose origin: tags differ "
                         "(only meaningful after a merge)")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                    help=f"max bridge candidates to emit (default {DEFAULT_LIMIT})")
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                    help=f"min cosine similarity (default {DEFAULT_THRESHOLD})")
    ap.add_argument("--out", default=None,
                    help="output path (default: .curator/bridges-<ts>.md)")
    args = ap.parse_args(argv)

    workspace = Path(args.workspace).resolve()
    wiki_dir = workspace / "wiki"
    if not wiki_dir.is_dir():
        raise SystemExit(f"no wiki/ at {workspace}")

    cfg = _load_config(workspace)
    if not cfg.get("embedding_enabled"):
        sys.stderr.write(
            "discover-bridges: embedding_enabled is not set in "
            ".curator/config.json — nothing to do. Enable embeddings "
            "and rebuild the vault index, then re-run.\n"
        )
        return 0

    try:
        from sentence_transformers import SentenceTransformer  # type: ignore
    except ImportError:
        sys.stderr.write(
            "discover-bridges: sentence-transformers not installed. "
            "Run `uv pip install sentence-transformers` in the workspace "
            "and retry.\n"
        )
        return 0

    pages = wiki_pages(wiki_dir)
    if len(pages) < 5:
        sys.stderr.write(
            f"discover-bridges: only {len(pages)} pages — too few to "
            "anchor against, exiting (cold-start guard).\n"
        )
        return 0

    # Encode every page body (not frontmatter, no wikilink markup, capped).
    model_name = cfg.get("embedding_model", DEFAULT_EMBED_MODEL)
    sys.stderr.write(f"discover-bridges: loading {model_name}...\n")
    model = SentenceTransformer(model_name)
    texts = [_strip_for_embedding(p.read_text(errors="replace")) for p in pages]
    # Drop pages with empty post-strip bodies — embedding noise.
    keep = [(p, t) for p, t in zip(pages, texts) if t]
    if len(keep) < 5:
        sys.stderr.write("discover-bridges: not enough non-empty pages.\n")
        return 0
    pages_kept, texts_kept = zip(*keep)
    sys.stderr.write(f"discover-bridges: embedding {len(texts_kept)} pages...\n")
    vecs = model.encode(list(texts_kept), normalize_embeddings=True,
                        show_progress_bar=False).tolist()

    existing_links = _build_link_set(list(pages_kept))

    # Per-page metadata (origin, excerpt) precomputed for the report.
    origins = [_page_origin(p.read_text(errors="replace")) for p in pages_kept]
    excerpts = [_page_excerpt(p.read_text(errors="replace")) for p in pages_kept]

    candidates: list[tuple[float, int, int]] = []
    n = len(pages_kept)
    for i in range(n):
        for j in range(i + 1, n):
            if args.across_origins and origins[i] == origins[j]:
                continue
            a_stem = pages_kept[i].stem.lower()
            b_stem = pages_kept[j].stem.lower()
            edge = tuple(sorted((a_stem, b_stem)))
            if edge in existing_links:
                continue
            sim = _cosine(vecs[i], vecs[j])
            if sim >= args.threshold:
                candidates.append((sim, i, j))

    candidates.sort(key=lambda r: -r[0])
    candidates = candidates[: args.limit]

    ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = (
        Path(args.out) if args.out
        else workspace / ".curator" / f"bridges-{ts}.md"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    title = ("Cross-origin bridge candidates" if args.across_origins
             else "Bridge candidates")
    lines.append(f"# {title}")
    lines.append("")
    lines.append(f"Generated: {ts}")
    lines.append(f"Threshold: {args.threshold}  Limit: {args.limit}  "
                 f"Pages embedded: {len(pages_kept)}")
    if args.across_origins:
        lines.append("Filter: pages with different `origin:` tags only")
    lines.append("")
    lines.append("Each pair is a high-similarity, currently-unlinked bridge candidate.")
    lines.append("Review and either add a wikilink in one or both directions, "
                 "or move the pair to a `dismissed` list if the similarity is "
                 "incidental.")
    lines.append("")
    if not candidates:
        lines.append("_No candidates above threshold._")
    lines.append("To accept a candidate, change `[ ]` to `[x]` next to the "
                 "pair's heading, then run:")
    lines.append("")
    lines.append("    uv run python3 <skill_path>/scripts/accept_bridges.py "
                 "--queue <this-file>")
    lines.append("")
    for rank, (sim, i, j) in enumerate(candidates, 1):
        a_rel = pages_kept[i].relative_to(wiki_dir)
        b_rel = pages_kept[j].relative_to(wiki_dir)
        lines.append(f"## [ ] {rank}. {a_rel} ↔ {b_rel}")
        lines.append("")
        lines.append(f"- **similarity**: {sim:.3f}")
        if origins[i] or origins[j]:
            lines.append(
                f"- **origins**: `{origins[i] or 'native'}`  ↔  "
                f"`{origins[j] or 'native'}`"
            )
        lines.append(f"- **{a_rel}**: {excerpts[i]}")
        lines.append(f"- **{b_rel}**: {excerpts[j]}")
        lines.append("")

    out_path.write_text("\n".join(lines) + "\n")
    sys.stdout.write(
        f"discover-bridges: {len(candidates)} candidates → {out_path}\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
