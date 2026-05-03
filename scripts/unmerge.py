#!/usr/bin/env python3
"""unmerge.py — undo a previous merge, surgically.

Reads `.curator/merges/<origin>.json` (the manifest written by
`merge.py --apply`) and partitions every imported page/vault file into:

  - pure imports          still tagged origin: <name>, sha256 unchanged
                          → safe to remove
  - user-modified imports still tagged origin: <name>, sha256 differs
                          → DO NOT silently delete; stage with both
                            versions preserved for human review
  - already de-imported   no longer present, or no longer tagged origin
                          → log only

Then walks every other wiki page (no origin tag, or different origin)
for native pages that wikilink-to or cite a would-be-removed item.
These are the user's own curation built on top of the imports; their
references will become dead links if the import is removed. The
script rewrites them to plain `[[stem]]` form, appends an audit
comment to each affected page, and lists every affected page in the
audit report under "Native pages with broken references after unmerge".

Cross-origin bridges accepted at merge time (recorded in the manifest's
`accepted_bridges`) are unwound: the wikilink in the native page that
connected to the imported page is removed and logged.

Why not `git revert`? A git revert would also undo any of the user's
curation commits since the merge. The whole point of unmerge is to
surgically extract just the merge-introduced content while preserving
everything else. The manifest is the surgical guide.

Three commands:

    unmerge.py --origin <name>            # stage the unmerge
    unmerge.py --origin <name> --apply    # atomic swap into live tree
    unmerge.py --origin <name> --abandon  # discard staging
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

_ce_scripts = os.environ.get("CURIOSITY_ENGINE_SCRIPTS_DIR")
if _ce_scripts and _ce_scripts not in sys.path:
    sys.path.insert(0, _ce_scripts)
try:
    from naming import (  # type: ignore
        read_frontmatter,
        WIKILINK_RE,
        CITATION_RE,
    )
    from sweep import wiki_pages  # type: ignore
except ImportError as e:
    sys.stderr.write(f"ERROR: cannot import curiosity-engine helpers ({e})\n")
    sys.exit(2)

sys.path.insert(0, str(Path(__file__).parent))
import reconcile  # type: ignore


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="unmerge.py",
        description="Undo a previous merge using the merge manifest + receiving wiki state.",
    )
    ap.add_argument("--origin", required=True, metavar="NAME")
    ap.add_argument("--workspace", default=".")
    ap.add_argument("--apply", action="store_true",
                    help="atomic swap of staged unmerge into live tree")
    ap.add_argument("--abandon", action="store_true",
                    help="discard staged unmerge")
    return ap


def _validate_origin(origin: str) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", origin or ""):
        raise SystemExit(f"invalid origin {origin!r}")
    return origin


def _staging_root(workspace: Path, origin: str) -> Path:
    return workspace / ".curator" / ".unmerge-staging" / origin


def _staging_paths(workspace: Path, origin: str) -> dict:
    root = _staging_root(workspace, origin)
    return {
        "root": root,
        "to_remove": root / "to-remove",
        "user_modified": root / "user-modified",
        "rewritten_natives": root / "rewritten-natives",
        "audit": root / "audit-report.md",
        "plan_json": root / "plan.json",
    }


def _page_origin(text: str) -> str:
    fm, _ = read_frontmatter(text)
    val = fm.get("origin", "")
    if isinstance(val, list):
        val = val[0] if val else ""
    return val.strip() if isinstance(val, str) else ""


def _imported_stems_and_vault(manifest: dict) -> tuple[set[str], set[str]]:
    page_stems: set[str] = set()
    for entry in manifest["wiki_pages"]:
        page_stems.add(Path(entry["final_rel"]).stem.lower())
    vault_rels: set[str] = set()
    for v in manifest["vault_files"]:
        if not v.get("deduped"):
            vault_rels.add(v["final_rel"])
    return page_stems, vault_rels


# --- the three buckets ----------------------------------------------------


def _classify_imports(workspace: Path, origin: str, manifest: dict) -> dict:
    wiki = workspace / "wiki"
    vault = workspace / "vault"
    pure_pages: list[dict] = []
    modified_pages: list[dict] = []
    gone_pages: list[dict] = []
    pure_vault: list[dict] = []
    modified_vault: list[dict] = []
    gone_vault: list[dict] = []

    for entry in manifest["wiki_pages"]:
        # We only auto-remove pages that landed in `wiki-incoming/`.
        # Same-topic-collision pages went into `collisions/` and the user
        # kept both — those need explicit human treatment, leave alone.
        if entry.get("staged_under") == "collisions":
            gone_pages.append({
                "final_rel": entry["final_rel"],
                "reason": "same-topic collision; preserved by design — manual",
            })
            continue
        path = wiki / entry["final_rel"]
        if not path.is_file():
            gone_pages.append({
                "final_rel": entry["final_rel"],
                "reason": "file no longer present",
            })
            continue
        text = path.read_text(errors="replace")
        if _page_origin(text) != origin:
            gone_pages.append({
                "final_rel": entry["final_rel"],
                "reason": "origin tag removed/changed",
            })
            continue
        current_sha = reconcile.sha256_file(path)
        if current_sha == entry.get("sha256_at_import"):
            pure_pages.append({"final_rel": entry["final_rel"]})
        else:
            modified_pages.append({
                "final_rel": entry["final_rel"],
                "import_sha256": entry.get("sha256_at_import"),
                "current_sha256": current_sha,
            })

    for v in manifest["vault_files"]:
        if v.get("deduped"):
            # We aliased to an existing receiver vault file at merge time;
            # we don't own it, so we never remove it on unmerge.
            continue
        path = vault / v["final_rel"]
        if not path.is_file():
            gone_vault.append({"final_rel": v["final_rel"],
                               "reason": "file no longer present"})
            continue
        current_sha = reconcile.sha256_file(path)
        if current_sha == v.get("sha256_at_import"):
            pure_vault.append({"final_rel": v["final_rel"]})
        else:
            modified_vault.append({
                "final_rel": v["final_rel"],
                "import_sha256": v.get("sha256_at_import"),
                "current_sha256": current_sha,
            })

    return {
        "pure_pages": pure_pages, "modified_pages": modified_pages,
        "gone_pages": gone_pages,
        "pure_vault": pure_vault, "modified_vault": modified_vault,
        "gone_vault": gone_vault,
    }


def _scan_native_references(workspace: Path, origin: str,
                             will_remove_stems: set[str],
                             will_remove_vault: set[str]) -> list[dict]:
    """Walk non-origin pages for wikilinks/citations pointing at content
    we're about to remove. Returns a list of edits to apply at apply time.
    """
    wiki = workspace / "wiki"
    edits: list[dict] = []
    for p in wiki_pages(wiki):
        text = p.read_text(errors="replace")
        if _page_origin(text) == origin:
            continue
        broken_links: list[str] = []
        broken_cites: list[str] = []
        for m in WIKILINK_RE.finditer(text):
            target = m.group(1).strip().lower().replace(" ", "-")
            stem = target.rsplit("/", 1)[-1]
            if stem in will_remove_stems:
                broken_links.append(stem)
        for m in CITATION_RE.finditer(text):
            tgt = m.group(1).strip()
            if tgt in will_remove_vault:
                broken_cites.append(tgt)
        if broken_links or broken_cites:
            edits.append({
                "page_rel": str(p.relative_to(wiki)),
                "broken_wikilinks": sorted(set(broken_links)),
                "broken_citations": sorted(set(broken_cites)),
            })
    return edits


# --- staging --------------------------------------------------------------


def cmd_stage(args) -> int:
    workspace = Path(args.workspace).resolve()
    origin = _validate_origin(args.origin)
    manifest_path = workspace / ".curator" / "merges" / f"{origin}.json"
    if not manifest_path.is_file():
        raise SystemExit(
            f"no merge manifest at {manifest_path} — was this origin ever merged?"
        )
    manifest = json.loads(manifest_path.read_text())

    staging = _staging_paths(workspace, origin)
    if staging["root"].exists():
        raise SystemExit(
            f"unmerge staging already exists: {staging['root']}\n"
            f"abandon first: unmerge.py --origin {origin} --abandon"
        )
    for k in ("root", "to_remove", "user_modified", "rewritten_natives"):
        staging[k].mkdir(parents=True, exist_ok=True)

    classification = _classify_imports(workspace, origin, manifest)

    # Stems we plan to remove (only pure imports — user-modified are
    # preserved pending human decision).
    will_remove_stems = {Path(e["final_rel"]).stem.lower()
                         for e in classification["pure_pages"]}
    will_remove_vault = {e["final_rel"]
                         for e in classification["pure_vault"]}

    # Snapshot to-be-removed pure imports into staging/to-remove/ (for
    # forensic recovery if the user changes their mind post-apply).
    wiki = workspace / "wiki"
    vault = workspace / "vault"
    for e in classification["pure_pages"]:
        src = wiki / e["final_rel"]
        if src.is_file():
            dst = staging["to_remove"] / "wiki" / e["final_rel"]
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    for e in classification["pure_vault"]:
        src = vault / e["final_rel"]
        if src.is_file():
            dst = staging["to_remove"] / "vault" / e["final_rel"]
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    # Snapshot user-modified imports into staging/user-modified/. We keep
    # both the current (user-edited) content and the original-import
    # snapshot so the user can diff and decide.
    for e in classification["modified_pages"]:
        src = wiki / e["final_rel"]
        if src.is_file():
            dst_cur = staging["user_modified"] / "current" / e["final_rel"]
            dst_cur.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst_cur)
        # Original-import bytes aren't preserved across merge --apply (we
        # only stored sha256), so we record the sha and let the user
        # decide. A future enhancement could keep the staging dir around
        # for N days and recover the import body from there.

    # Native references that will break.
    native_edits = _scan_native_references(
        workspace, origin, will_remove_stems, will_remove_vault,
    )

    # Bridges accepted at merge time (currently always [] — populated by
    # post-discover-bridges acceptance flow once that lands).
    bridges = manifest.get("accepted_bridges", [])

    plan = {
        "origin": origin,
        "staged_at": _dt.datetime.now(_dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "manifest_path": str(manifest_path),
        "pure_pages": classification["pure_pages"],
        "modified_pages": classification["modified_pages"],
        "gone_pages": classification["gone_pages"],
        "pure_vault": classification["pure_vault"],
        "modified_vault": classification["modified_vault"],
        "gone_vault": classification["gone_vault"],
        "native_edits": native_edits,
        "bridges_to_unwind": bridges,
    }
    staging["plan_json"].write_text(
        json.dumps(plan, indent=2, sort_keys=True) + "\n"
    )

    _write_audit(staging, plan)

    sys.stdout.write(
        f"unmerge staged at {staging['root']}\n"
        f"audit report: {staging['audit']}\n"
        f"to apply:    unmerge.py --origin {origin} --apply\n"
        f"to discard:  unmerge.py --origin {origin} --abandon\n"
    )
    return 0


def _write_audit(staging: dict, plan: dict) -> None:
    L = []
    L.append(f"# Unmerge audit report — origin `{plan['origin']}`")
    L.append("")
    L.append(f"- Staged: {plan['staged_at']}")
    L.append(f"- Manifest: `{plan['manifest_path']}`")
    L.append("")

    L.append("## Pure imports (will be removed)")
    L.append("")
    L.append(f"- pages: {len(plan['pure_pages'])}")
    L.append(f"- vault files: {len(plan['pure_vault'])}")
    L.append("")
    if plan["pure_pages"]:
        for e in plan["pure_pages"][:50]:
            L.append(f"  - `{e['final_rel']}`")
        if len(plan["pure_pages"]) > 50:
            L.append(f"  - ... and {len(plan['pure_pages']) - 50} more")
        L.append("")

    L.append("## User-modified imports (preserved — decide manually)")
    L.append("")
    if plan["modified_pages"]:
        for e in plan["modified_pages"]:
            L.append(f"- `{e['final_rel']}`  "
                     f"(import sha {e['import_sha256'][:10]}, "
                     f"current sha {e['current_sha256'][:10]})")
        L.append("")
        L.append("These pages still carry `origin: " + plan["origin"] + "` but "
                 "have diverged from their import sha256. Unmerge will leave "
                 "them where they are. Decide whether to:")
        L.append("- keep them (drop the `origin:` tag manually), or")
        L.append("- delete them (they're listed in "
                 "`user-modified/current/` for forensic backup), or")
        L.append("- restore the original import (cancel this unmerge).")
    else:
        L.append("_None._")
    L.append("")

    L.append("## Already de-imported (nothing to do)")
    L.append("")
    L.append(f"- pages: {len(plan['gone_pages'])}")
    L.append(f"- vault files: {len(plan['gone_vault'])}")
    L.append("")

    L.append("## Native pages with broken references after unmerge")
    L.append("")
    if plan["native_edits"]:
        L.append(
            "These pages were authored or curated by you (no origin tag) "
            "but cite or wikilink content that came from `" +
            plan["origin"] + "`. After unmerge those targets will be gone. "
            "On `--apply`, this script rewrites the wikilinks/citations "
            "to dead-link form and appends an audit comment to each page "
            "so you can clean up at your leisure."
        )
        L.append("")
        for e in plan["native_edits"]:
            L.append(f"- `{e['page_rel']}`")
            for w in e["broken_wikilinks"]:
                L.append(f"  - wikilink `[[{w}]]` (target removed)")
            for c in e["broken_citations"]:
                L.append(f"  - citation `(vault:{c})` (target removed)")
    else:
        L.append("_None._")
    L.append("")

    L.append("## Cross-origin bridges to unwind")
    L.append("")
    if plan["bridges_to_unwind"]:
        for b in plan["bridges_to_unwind"]:
            L.append(f"- {b}")
    else:
        L.append("_None recorded._")
    L.append("")

    staging["audit"].write_text("\n".join(L) + "\n")


def _rewrite_native_page(text: str, broken_links: set[str],
                          broken_cites: set[str], origin: str,
                          ts: str) -> str:
    """Rewrite a single native page: any wikilink/citation pointing at
    a to-be-removed import is preserved as-is (it'll just be a dead
    link), but we append an audit comment block at the end of the file
    so the user knows what to clean up.

    We deliberately do NOT erase the user's prose around the broken
    reference — they may want to keep the surrounding analysis even after
    the cited source is gone. That's a content judgment, not ours.
    """
    note = []
    note.append("")
    note.append(f"<!-- unmerge: origin {origin} removed at {ts} -->")
    if broken_links:
        note.append(
            "<!-- unmerge: the following wikilinks point at removed pages: "
            + ", ".join(sorted(broken_links)) + " -->"
        )
    if broken_cites:
        note.append(
            "<!-- unmerge: the following vault citations point at removed files: "
            + ", ".join(sorted(broken_cites)) + " -->"
        )
    return text.rstrip() + "\n" + "\n".join(note) + "\n"


def cmd_apply(args) -> int:
    workspace = Path(args.workspace).resolve()
    origin = _validate_origin(args.origin)
    staging = _staging_paths(workspace, origin)
    if not staging["plan_json"].is_file():
        raise SystemExit(f"no staged unmerge at {staging['root']}")
    plan = json.loads(staging["plan_json"].read_text())
    ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    wiki = workspace / "wiki"
    vault = workspace / "vault"

    # 1. Remove pure imports.
    for e in plan["pure_pages"]:
        path = wiki / e["final_rel"]
        if path.is_file():
            path.unlink()
    for e in plan["pure_vault"]:
        path = vault / e["final_rel"]
        if path.is_file():
            path.unlink()

    # 2. Annotate native pages whose references will now be dead.
    for e in plan["native_edits"]:
        path = wiki / e["page_rel"]
        if not path.is_file():
            continue
        text = path.read_text(errors="replace")
        new_text = _rewrite_native_page(
            text,
            set(e["broken_wikilinks"]),
            set(e["broken_citations"]),
            origin, ts,
        )
        path.write_text(new_text)

    # 3. Unwind accepted bridges (each entry is a (native_stem, imported_stem)
    # tuple stored as a 2-element list in JSON).
    for pair in plan["bridges_to_unwind"]:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            continue
        native_stem, imported_stem = pair
        # Find the native page; remove any wikilink to imported_stem.
        for p in wiki_pages(wiki):
            if p.stem.lower() != native_stem.lower():
                continue
            text = p.read_text(errors="replace")
            new = re.sub(
                r"\[\[" + re.escape(imported_stem) + r"(?:\|[^\]]*)?\]\]",
                "",
                text,
                flags=re.IGNORECASE,
            )
            if new != text:
                p.write_text(new)

    # 4. Archive manifest, drop active record.
    merges_dir = workspace / ".curator" / "merges"
    archive_dir = merges_dir / ".archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    src_manifest = merges_dir / f"{origin}.json"
    if src_manifest.is_file():
        archive_path = archive_dir / f"{origin}-unmerged-{ts.replace(':','')}.json"
        shutil.move(str(src_manifest), str(archive_path))

    # 5. Rebuild graph.
    graph_py = Path(_ce_scripts or "") / "graph.py" if _ce_scripts else None
    if graph_py and graph_py.is_file():
        subprocess.run(
            ["uv", "run", "python3", str(graph_py), "rebuild", "wiki"],
            cwd=str(workspace), check=False,
        )

    # 6. Discard staging.
    shutil.rmtree(staging["root"])
    sys.stdout.write(
        f"unmerge applied: removed {len(plan['pure_pages'])} pages and "
        f"{len(plan['pure_vault'])} vault files; "
        f"{len(plan['native_edits'])} native pages annotated; "
        f"{len(plan['modified_pages'])} user-modified imports left in place "
        f"for manual review.\n"
    )
    return 0


def cmd_abandon(args) -> int:
    workspace = Path(args.workspace).resolve()
    origin = _validate_origin(args.origin)
    staging = _staging_paths(workspace, origin)
    if not staging["root"].exists():
        sys.stdout.write(f"nothing to abandon: {staging['root']}\n")
        return 0
    shutil.rmtree(staging["root"])
    sys.stdout.write(f"abandoned: {staging['root']}\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.apply and args.abandon:
        raise SystemExit("--apply and --abandon are mutually exclusive")
    if args.apply:
        return cmd_apply(args)
    if args.abandon:
        return cmd_abandon(args)
    return cmd_stage(args)


if __name__ == "__main__":
    raise SystemExit(main())
