#!/usr/bin/env python3
"""subgraph_export.py — extract a self-contained mini-wiki from a curiosity-
engine workspace.

Three scopes:
  --project <name>    pages tagged projects: [<name>]
  --page <stem>       a single page; with --include-1-hop adds wikilink neighbors
  --origin <name>     pages tagged origin: <name> (only meaningful post-merge)

The destination is a normal curiosity-engine wiki layout (vault/, wiki/,
.curator/projects.json) plus _export-manifest.json. Suitable for git push
to a public repo for sharing — tag with the GitHub topic `curiosity-wiki`
for discovery.

Vault files are included transitively: every (vault:...) citation reachable
from an in-scope wiki page brings the cited file along.

Operates on the user's own wiki, so no untrusted-framing is added — the
receiving end of `merge` is what does the framing.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import shutil
import sys
from pathlib import Path

# Resolve curiosity-engine helpers via CURIOSITY_ENGINE_SCRIPTS_DIR (set by
# setup.sh) or the Claude Code <skill_path> env var if present.
_ce_scripts = os.environ.get("CURIOSITY_ENGINE_SCRIPTS_DIR")
if _ce_scripts and _ce_scripts not in sys.path:
    sys.path.insert(0, _ce_scripts)
try:
    from naming import (  # type: ignore
        read_frontmatter,
        CITATION_RE,
        WIKILINK_RE,
    )
    from sweep import wiki_pages  # type: ignore
except ImportError as e:
    sys.stderr.write(
        "ERROR: cannot import curiosity-engine helpers.\n"
        "       Set CURIOSITY_ENGINE_SCRIPTS_DIR to the absolute path of\n"
        "       curiosity-engine/scripts, or run scripts/setup.sh first.\n"
        f"       (Original import error: {e})\n"
    )
    sys.exit(2)


SCHEMA_VERSION = 1


# --- path-traversal guards --------------------------------------------------


def _safe_destination(dest_arg: str, workspace: Path) -> Path:
    """Reject `..` segments and absolute paths inside the workspace.

    The destination must be either an absolute path outside the workspace
    or a relative path that resolves outside it. Writing inside the
    workspace would clobber the live wiki.
    """
    raw = Path(dest_arg).expanduser()
    resolved = raw.resolve() if raw.is_absolute() else (workspace / raw).resolve()
    # Forbid `..` traversal in the original argument string for clarity.
    if ".." in Path(dest_arg).parts:
        raise SystemExit(f"refusing path with .. segments: {dest_arg!r}")
    # Forbid writing into the workspace tree itself.
    try:
        resolved.relative_to(workspace.resolve())
        raise SystemExit(
            f"refusing to write export inside workspace: {resolved}\n"
            f"choose a path outside {workspace}"
        )
    except ValueError:
        pass  # outside workspace — good
    return resolved


# --- frontmatter scope filters ---------------------------------------------


def _page_projects(text: str) -> list[str]:
    fm, _ = read_frontmatter(text)
    val = fm.get("projects", [])
    if isinstance(val, str):
        val = [val]
    return [v.strip() for v in val if isinstance(v, str) and v.strip()]


def _page_origin(text: str) -> str:
    fm, _ = read_frontmatter(text)
    val = fm.get("origin", "")
    if isinstance(val, list):
        val = val[0] if val else ""
    return val.strip() if isinstance(val, str) else ""


# --- scope resolution ------------------------------------------------------


def _resolve_scope_pages(args, all_pages: list[Path], wiki_dir: Path) -> list[Path]:
    if args.project:
        return [
            p for p in all_pages
            if args.project in _page_projects(p.read_text(errors="replace"))
        ]
    if args.origin:
        return [
            p for p in all_pages
            if _page_origin(p.read_text(errors="replace")) == args.origin
        ]
    if args.page:
        # Match by stem (case-insensitive, hyphenated like wikilink targets).
        target = args.page.strip().lower().replace(" ", "-")
        # Allow either bare stem or stem-with-subdir.
        match = next(
            (p for p in all_pages if p.stem.lower() == target
             or str(p.relative_to(wiki_dir)).lower().replace(".md", "") == target),
            None,
        )
        if not match:
            raise SystemExit(f"page not found: {args.page!r}")
        seeds = [match]
        if args.include_1_hop:
            seeds.extend(_one_hop_neighbors(match, all_pages, wiki_dir))
        # de-dupe while preserving order
        seen = set()
        out: list[Path] = []
        for p in seeds:
            if p not in seen:
                out.append(p)
                seen.add(p)
        return out
    raise SystemExit("must pass one of --project / --page / --origin")


def _one_hop_neighbors(page: Path, all_pages: list[Path], wiki_dir: Path) -> list[Path]:
    by_stem = {p.stem.lower(): p for p in all_pages}
    text = page.read_text(errors="replace")
    out: list[Path] = []
    for m in WIKILINK_RE.finditer(text):
        target = m.group(1).strip().lower().replace(" ", "-")
        # Wikilink targets may include subdirectory (`concepts/transformer`);
        # match both the bare stem and the path form.
        bare = target.rsplit("/", 1)[-1]
        if bare in by_stem and by_stem[bare] != page:
            out.append(by_stem[bare])
    # Plus inbound wikilinks from any other page that points at this one.
    own_stem = page.stem.lower()
    for p in all_pages:
        if p == page:
            continue
        text = p.read_text(errors="replace")
        for m in WIKILINK_RE.finditer(text):
            tgt = m.group(1).strip().lower().replace(" ", "-").rsplit("/", 1)[-1]
            if tgt == own_stem:
                out.append(p)
                break
    return out


# --- vault collection ------------------------------------------------------


def _collect_cited_vault(scope_pages: list[Path], vault_dir: Path) -> list[Path]:
    """Resolve every (vault:<rel>) citation in scope pages to a vault file.

    Returns the list of existing vault files (deduplicated, sorted).
    Citations to non-existent files are silently skipped here — they get
    surfaced when the merge counterpart validates citations on import.
    """
    cited_rel: set[str] = set()
    for p in scope_pages:
        for m in CITATION_RE.finditer(p.read_text(errors="replace")):
            cited_rel.add(m.group(1).strip())
    out: list[Path] = []
    seen: set[Path] = set()
    for rel in sorted(cited_rel):
        # Defense in depth: refuse traversal in citation paths.
        if ".." in Path(rel).parts or Path(rel).is_absolute():
            continue
        candidate = (vault_dir / rel).resolve()
        try:
            candidate.relative_to(vault_dir.resolve())
        except ValueError:
            continue  # citation escaped vault dir — drop
        if candidate.is_file() and candidate not in seen:
            out.append(candidate)
            seen.add(candidate)
        else:
            # Some installs co-locate raw + extracted; if the citation
            # points at `foo.extracted.md` and the file is at `foo.md`,
            # don't paper over it — leave it missing for the audit.
            pass
    return out


# --- copy + manifest -------------------------------------------------------


def _copy_pages(scope_pages: list[Path], wiki_dir: Path, dest_wiki: Path) -> list[str]:
    rels: list[str] = []
    for p in scope_pages:
        rel = p.relative_to(wiki_dir)
        target = dest_wiki / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, target)
        rels.append(str(rel))
    return sorted(rels)


def _copy_vault(vault_files: list[Path], vault_dir: Path, dest_vault: Path) -> list[str]:
    rels: list[str] = []
    for f in vault_files:
        rel = f.relative_to(vault_dir)
        target = dest_vault / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, target)
        rels.append(str(rel))
    return sorted(rels)


def _filter_projects_json(workspace: Path, dest_curator: Path,
                          scope_kind: str, scope_value: str,
                          scope_projects: set[str]) -> None:
    src = workspace / ".curator" / "projects.json"
    if not src.is_file():
        return
    try:
        data = json.loads(src.read_text())
    except Exception:
        return
    # Best-effort filter: keep entries whose key is in scope_projects (when
    # exporting by project) or whose value's `origin` matches (when by
    # origin). Falls through to a copy when scope is by --page.
    if isinstance(data, dict):
        if scope_kind == "project":
            data = {k: v for k, v in data.items() if k in scope_projects}
        elif scope_kind == "origin":
            data = {
                k: v for k, v in data.items()
                if isinstance(v, dict) and v.get("origin") == scope_value
            }
    dest_curator.mkdir(parents=True, exist_ok=True)
    (dest_curator / "projects.json").write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n"
    )


def _write_manifest(dest: Path, *, scope_kind: str, scope_value: str,
                    include_1_hop: bool, origin_wiki: Path,
                    origin_label: str | None,
                    scope_pages_rel: list[str], scope_vault_rel: list[str]) -> None:
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "exported_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "origin_wiki": str(origin_wiki),
        "origin_label": origin_label or "",
        "scope": {
            "kind": scope_kind,
            "value": scope_value,
            "include_1_hop": include_1_hop,
        },
        "scope_pages": scope_pages_rel,
        "scope_vault": scope_vault_rel,
    }
    (dest / "_export-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    )


# --- entry point -----------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="subgraph_export.py",
        description="Extract a self-contained mini-wiki from a curiosity-engine workspace.",
    )
    scope = ap.add_mutually_exclusive_group(required=True)
    scope.add_argument("--project", metavar="NAME",
                       help="export pages tagged projects: [NAME]")
    scope.add_argument("--page", metavar="STEM",
                       help="export a single page (stem or path/stem)")
    scope.add_argument("--origin", metavar="NAME",
                       help="export pages tagged origin: NAME")
    ap.add_argument("--include-1-hop", action="store_true",
                    help="for --page, also include wikilink neighbors (1 hop)")
    ap.add_argument("--to", metavar="PATH", required=True,
                    help="destination directory (must not exist or must be empty)")
    ap.add_argument("--workspace", metavar="PATH", default=".",
                    help="curiosity-engine workspace root (default: cwd)")
    ap.add_argument("--label", metavar="STR", default=None,
                    help="optional human label for origin_wiki in manifest")
    ap.add_argument("--force", action="store_true",
                    help="overwrite existing files at destination")
    args = ap.parse_args(argv)

    if args.include_1_hop and not args.page:
        ap.error("--include-1-hop only makes sense with --page")

    workspace = Path(args.workspace).resolve()
    wiki_dir = workspace / "wiki"
    vault_dir = workspace / "vault"
    if not wiki_dir.is_dir():
        raise SystemExit(f"no wiki/ at {workspace}")
    if not vault_dir.is_dir():
        raise SystemExit(f"no vault/ at {workspace}")

    dest = _safe_destination(args.to, workspace)
    if dest.exists() and any(dest.iterdir()) and not args.force:
        raise SystemExit(
            f"destination is non-empty: {dest}\n"
            f"pass --force to overwrite, or pick a fresh path"
        )
    dest.mkdir(parents=True, exist_ok=True)

    all_pages = wiki_pages(wiki_dir)
    scope_pages = _resolve_scope_pages(args, all_pages, wiki_dir)
    if not scope_pages:
        raise SystemExit("scope matched zero pages")

    # If exporting by project, also include the project home page if it
    # exists (and isn't already in scope).
    if args.project:
        home = wiki_dir / "projects" / f"{args.project}.md"
        if home.is_file() and home not in scope_pages:
            scope_pages.append(home)

    vault_files = _collect_cited_vault(scope_pages, vault_dir)

    dest_wiki = dest / "wiki"
    dest_vault = dest / "vault"
    dest_curator = dest / ".curator"

    pages_rel = _copy_pages(scope_pages, wiki_dir, dest_wiki)
    vault_rel = _copy_vault(vault_files, vault_dir, dest_vault)

    if args.project:
        kind, value = "project", args.project
        scope_projects = {args.project}
    elif args.page:
        kind, value = "page", args.page
        scope_projects = set()
        for p in scope_pages:
            scope_projects.update(_page_projects(p.read_text(errors="replace")))
    else:
        kind, value = "origin", args.origin
        scope_projects = set()

    _filter_projects_json(workspace, dest_curator, kind, value, scope_projects)

    _write_manifest(
        dest,
        scope_kind=kind,
        scope_value=value,
        include_1_hop=args.include_1_hop,
        origin_wiki=workspace,
        origin_label=args.label,
        scope_pages_rel=pages_rel,
        scope_vault_rel=vault_rel,
    )

    sys.stdout.write(
        f"exported {len(pages_rel)} pages and {len(vault_rel)} vault files to {dest}\n"
        f"manifest: {dest / '_export-manifest.json'}\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
