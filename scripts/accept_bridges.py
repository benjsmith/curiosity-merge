#!/usr/bin/env python3
"""accept_bridges.py — apply user-accepted bridge candidates from a
discover-bridges review queue.

Workflow:
  1. `discover_bridges.py` writes `.curator/bridges-<ts>.md` with each
     candidate prefixed by `## [ ] N. <a> ↔ <b>`.
  2. User edits the file, changes `[ ]` to `[x]` for pairs they accept.
  3. `accept_bridges.py --queue <file>` reads the marks, writes
     wikilinks into both pages of each accepted pair, and (for
     cross-origin pairs from a merge) appends the pair to the relevant
     manifest's `accepted_bridges` so unmerge can unwind.

Idempotent: re-running on the same queue is a no-op when wikilinks
already exist; manifest entries dedupe.

The script never deletes a candidate from the queue. After acceptance
it appends a `<!-- accepted: <ts> -->` comment under each chosen pair
so re-runs are obvious in diffs.
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
except ImportError as e:
    sys.stderr.write(f"ERROR: cannot import curiosity-engine helpers ({e})\n")
    sys.exit(2)


HEADING_RE = re.compile(
    r"^## \[(?P<mark>[ xX])\] \d+\. (?P<a>\S.*?) ↔ (?P<b>\S.*?)\s*$",
    re.MULTILINE,
)


def _page_origin(text: str) -> str:
    fm, _ = read_frontmatter(text)
    val = fm.get("origin", "")
    if isinstance(val, list):
        val = val[0] if val else ""
    return val.strip() if isinstance(val, str) else ""


def _add_wikilink_at_end(text: str, target_stem: str) -> tuple[str, bool]:
    """Append a `See also: [[<stem>]]` line if not already present.

    Returns (new_text, changed). We deliberately don't try to inline-link
    inside the page's prose — that's a content judgment a script
    shouldn't make. A trailing "See also" reference is unambiguous and
    easy for the user to relocate if they want.
    """
    target_stem_lower = target_stem.lower()
    for m in WIKILINK_RE.finditer(text):
        existing = m.group(1).strip().lower().replace(" ", "-")
        if existing.rsplit("/", 1)[-1] == target_stem_lower:
            return text, False
    block = f"\n\n## See also\n\n- [[{target_stem}]]\n"
    # If a "## See also" already exists, append to it.
    m = re.search(r"(?m)^## See also\s*$", text)
    if m:
        return text[:m.end()] + f"\n\n- [[{target_stem}]]\n" + text[m.end():], True
    return text.rstrip() + block, True


def _resolve_page(wiki_dir: Path, rel: str) -> Path | None:
    p = wiki_dir / rel
    if p.is_file():
        return p
    return None


def _update_manifest(workspace: Path, origin: str,
                     pairs: list[tuple[str, str]]) -> None:
    """Append accepted (native, imported) pairs to the merge manifest.

    `pairs` is given as (stem_a, stem_b) regardless of which is native.
    We store both orderings keyed by stem so unmerge can match.
    """
    manifest_path = workspace / ".curator" / "merges" / f"{origin}.json"
    if not manifest_path.is_file():
        return
    manifest = json.loads(manifest_path.read_text())
    existing = {tuple(p) for p in manifest.get("accepted_bridges", []) if isinstance(p, list)}
    for a, b in pairs:
        existing.add((a, b))
    manifest["accepted_bridges"] = sorted(existing)
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="accept_bridges.py",
        description="Apply checked candidates from a discover-bridges queue.",
    )
    ap.add_argument("--queue", required=True,
                    help="path to a .curator/bridges-<ts>.md file with [x] marks")
    ap.add_argument("--workspace", default=".")
    ap.add_argument("--dry-run", action="store_true",
                    help="report which pairs would be applied; write nothing")
    args = ap.parse_args(argv)

    workspace = Path(args.workspace).resolve()
    wiki_dir = workspace / "wiki"
    queue = Path(args.queue).resolve()
    if not queue.is_file():
        raise SystemExit(f"queue not found: {queue}")
    text = queue.read_text()

    accepted: list[tuple[str, str]] = []
    for m in HEADING_RE.finditer(text):
        if m.group("mark").lower() != "x":
            continue
        a_rel = m.group("a").strip().rstrip(".")
        b_rel = m.group("b").strip().rstrip(".")
        accepted.append((a_rel, b_rel))

    if not accepted:
        sys.stdout.write("no [x]-marked pairs in queue; nothing to do.\n")
        return 0

    ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    applied: list[tuple[str, str]] = []
    cross_origin_by_origin: dict[str, list[tuple[str, str]]] = {}
    skipped: list[str] = []

    for a_rel, b_rel in accepted:
        page_a = _resolve_page(wiki_dir, a_rel)
        page_b = _resolve_page(wiki_dir, b_rel)
        if not page_a or not page_b:
            skipped.append(f"{a_rel} ↔ {b_rel}: page(s) not found")
            continue
        text_a = page_a.read_text(errors="replace")
        text_b = page_b.read_text(errors="replace")
        new_a, changed_a = _add_wikilink_at_end(text_a, page_b.stem)
        new_b, changed_b = _add_wikilink_at_end(text_b, page_a.stem)
        if not (changed_a or changed_b):
            skipped.append(f"{a_rel} ↔ {b_rel}: already linked")
            continue
        if not args.dry_run:
            if changed_a:
                page_a.write_text(new_a)
            if changed_b:
                page_b.write_text(new_b)
        applied.append((a_rel, b_rel))

        # Cross-origin tracking — both for the manifest update and so
        # unmerge can unwind these. We key by *each* origin involved so a
        # later unmerge of either side picks it up.
        origin_a = _page_origin(text_a)
        origin_b = _page_origin(text_b)
        if origin_a and origin_b and origin_a != origin_b:
            for o in (origin_a, origin_b):
                cross_origin_by_origin.setdefault(o, []).append(
                    (page_a.stem, page_b.stem)
                )
        elif origin_a and not origin_b:
            cross_origin_by_origin.setdefault(origin_a, []).append(
                (page_b.stem, page_a.stem)
            )
        elif origin_b and not origin_a:
            cross_origin_by_origin.setdefault(origin_b, []).append(
                (page_a.stem, page_b.stem)
            )

    if not args.dry_run:
        # Annotate the queue file: append "<!-- accepted: ts -->" under
        # each chosen heading so re-runs are visible in diffs and we
        # don't re-link on a second run that misses the dedupe (which
        # the link-existence check already provides, but belt-and-braces).
        annotated = HEADING_RE.sub(
            lambda m: (
                f"## [{m.group('mark')}] " +
                m.group(0).split('. ', 1)[1] +
                (f"\n\n<!-- accepted: {ts} -->" if m.group("mark").lower() == "x" else "")
            ),
            text,
            count=0,
        )
        # Simple form: append a one-line marker under each accepted heading
        # without rewriting the heading itself.
        out_lines = []
        for line in text.splitlines():
            out_lines.append(line)
            mh = HEADING_RE.match(line)
            if mh and mh.group("mark").lower() == "x":
                out_lines.append("")
                out_lines.append(f"<!-- accepted: {ts} -->")
        queue.write_text("\n".join(out_lines) + "\n")

        # Update each affected merge manifest.
        for origin, pairs in cross_origin_by_origin.items():
            _update_manifest(workspace, origin, pairs)

    sys.stdout.write(
        f"accept-bridges: {'(dry run) ' if args.dry_run else ''}"
        f"applied {len(applied)} pair(s), skipped {len(skipped)}\n"
    )
    for s in skipped:
        sys.stdout.write(f"  - skipped: {s}\n")
    for o, pairs in cross_origin_by_origin.items():
        sys.stdout.write(f"  - manifest updated for origin `{o}`: "
                         f"{len(pairs)} pair(s)\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
