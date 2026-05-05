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
import hashlib
import json
import os
import re
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

# Local helpers — keep relative imports robust whether invoked via
# uv run or as a module.
sys.path.insert(0, str(Path(__file__).parent))
import preflight  # type: ignore  # noqa: E402


SCHEMA_VERSION = 2


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


# Licenses + flags we treat as "owner has indicated this content is
# redistributable". Conservative: defaults that aren't redistributable
# (Elsevier, paywalled blogs, "all rights reserved") fail closed.
#
# v0.2.1: CC-BY-NC and CC-BY-ND removed from the default. NC forbids
# commercial use; ND forbids derivatives. The wiki's normal operation
# (extraction, classification, summarization, redistribution inside
# curiosity-engine workflows) may exceed both. Users with a specific
# use case that complies can opt in via --allow-license-class.
_REDISTRIBUTABLE_LICENSES = {
    "cc0", "public-domain", "publicdomain",
    "cc-by", "cc-by-sa",
    "cc-by-3.0", "cc-by-4.0", "cc-by-sa-3.0", "cc-by-sa-4.0",
    "mit", "apache-2.0", "apache2", "bsd", "bsd-3-clause", "bsd-2-clause",
    "mpl-2.0",
    "arxiv-non-exclusive",  # arXiv's default license permits redistribution;
                            # see docs/licensing.md for the third-party caveat
}
# Tokens added on demand via --allow-license-class.
_NC_LICENSE_TOKENS = {
    "cc-by-nc", "cc-by-nc-sa",
    "cc-by-nc-3.0", "cc-by-nc-4.0", "cc-by-nc-sa-3.0", "cc-by-nc-sa-4.0",
}
_ND_LICENSE_TOKENS = {
    "cc-by-nd", "cc-by-nc-nd",
    "cc-by-nd-3.0", "cc-by-nd-4.0", "cc-by-nc-nd-3.0", "cc-by-nc-nd-4.0",
}


def _effective_license_allowlist(allow_class_csv: str) -> set[str]:
    """Build the runtime allowlist by adding opt-in classes to the default."""
    out = set(_REDISTRIBUTABLE_LICENSES)
    classes = {c.strip().lower() for c in (allow_class_csv or "").split(",")
               if c.strip()}
    if "nc" in classes:
        out |= _NC_LICENSE_TOKENS
    if "nd" in classes:
        out |= _ND_LICENSE_TOKENS
    return out


_FM_KEY_RE = re.compile(r"^([a-z_][a-z0-9_]*):\s*(.+?)\s*$", re.IGNORECASE)


def _raw_frontmatter(text: str) -> dict[str, str]:
    """Read frontmatter without curiosity-engine's ALLOWED_FM_KEYS filter.

    We look at vault files' frontmatter for license/redistributable hints
    that the curator's allowlist intentionally drops. Returns a flat
    string-only dict; lists/multi-line values are joined for our purposes.
    """
    out: dict[str, str] = {}
    if not text.startswith("---"):
        return out
    end = text.find("\n---", 3)
    if end == -1:
        return out
    block = text[3:end].strip()
    for line in block.splitlines():
        if not line or line[0] in (" ", "\t"):
            continue
        m = _FM_KEY_RE.match(line)
        if m:
            key, val = m.group(1).strip().lower(), m.group(2).strip()
            if val and val[0] in ('"', "'") and val[-1] == val[0]:
                val = val[1:-1]
            out[key] = val
    return out


def _vault_redistributable(text: str,
                           allowlist: set[str] | None = None) -> bool:
    """Decide whether a vault file's frontmatter declares redistributable
    content. `allowlist` defaults to the conservative `_REDISTRIBUTABLE_LICENSES`
    set; callers can broaden via --allow-license-class.
    """
    if allowlist is None:
        allowlist = _REDISTRIBUTABLE_LICENSES
    fm = _raw_frontmatter(text)
    redistrib = fm.get("redistributable", "").lower()
    if redistrib in ("true", "yes", "1"):
        return True
    if redistrib in ("false", "no", "0"):
        return False
    license_str = fm.get("license", "").lower().strip()
    if license_str in allowlist:
        return True
    # arXiv URLs imply arXiv's non-exclusive license unless the author
    # explicitly relicensed; we treat them as redistributable for
    # subgraph-export purposes. Caveat: arXiv's license is granted to
    # *arXiv*, not third parties — third-party redistribution is common
    # practice but not a strict legal entitlement. See docs/licensing.md.
    src_url = fm.get("source_url", "").lower()
    if "arxiv.org" in src_url or "biorxiv.org" in src_url or "chemrxiv.org" in src_url:
        return True
    return False


def _vault_source_meta(path: Path) -> dict:
    """Pull lightweight provenance from a vault file's frontmatter for
    inclusion in the export manifest. Receivers use this to hydrate.
    """
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return {}
    fm = _raw_frontmatter(text)
    return {
        "source_url": fm.get("source_url", ""),
        "source_path": fm.get("source_path", ""),
        "source_type": fm.get("source_type", ""),
        "title": fm.get("title", ""),
        "license": fm.get("license", ""),
        "redistributable": _vault_redistributable(text),
    }


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


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


def _filter_vault_for_mode(vault_files: list[Path], mode: str,
                            allowlist: set[str] | None = None) -> list[Path]:
    """Apply --include-vault mode. Default `none` is sharing-safe."""
    if mode == "none":
        return []
    if mode == "all":
        return list(vault_files)
    if mode == "owned":
        if allowlist is None:
            allowlist = _REDISTRIBUTABLE_LICENSES
        out = []
        for p in vault_files:
            try:
                text = p.read_text(errors="replace")
            except OSError:
                continue
            if _vault_redistributable(text, allowlist=allowlist):
                out.append(p)
        return out
    raise SystemExit(f"unknown --include-vault mode: {mode!r}")


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
                    scope_pages_rel: list[str], scope_vault_rel: list[str],
                    vault_metadata: list[dict],
                    include_vault_mode: str,
                    preflight_findings: list[dict] | None = None,
                    include_preflight_in_manifest: bool = False) -> None:
    """Write `_export-manifest.json`.

    `vault_metadata` records every cited vault file regardless of whether
    its content was included — sha256, source_url, source_type, license.
    Receivers use this to hydrate missing sources after merge. Excluding
    bytes but recording metadata is the licensing-safe default.

    Pre-flight findings split (v0.2.1):
      - `preflight_summary` (always) — `[{kind, severity, count}]`. No
        subjects, no samples. Tells receivers what categories fired
        without revealing where or what.
      - `preflight_findings` (only when caller passes
        `include_preflight_in_manifest=True`) — manifest-safe finding
        records (kind/severity/subject/summary/rationale). Samples
        (`samples` field) are stripped at this boundary regardless,
        enforced by `preflight.manifest_safe(...)`.

    The default summary-only mode prevents two leaks:
      1. Sample data (emails, SSNs) embedded in rationale strings.
      2. Subject lists that act as a "where to harvest" map for scrapers
         of published wikis.
    """
    findings = preflight_findings or []
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
        "include_vault_mode": include_vault_mode,
        "scope_pages": scope_pages_rel,
        "scope_vault": scope_vault_rel,
        "vault_metadata": vault_metadata,
        "preflight_summary": preflight.manifest_summary(findings),
    }
    if include_preflight_in_manifest:
        manifest["preflight_findings"] = [
            preflight.manifest_safe(f) for f in findings
        ]
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
    ap.add_argument("--include-vault", choices=("none", "owned", "all"),
                    default="none",
                    help="which vault files to copy into the export "
                         "(default: none — sharing-safe; the manifest still "
                         "records sha256/source_url/license for every cited "
                         "vault file so receivers can hydrate). 'owned' = "
                         "files whose frontmatter declares a redistributable "
                         "license or arXiv-family preprint URL. 'all' = "
                         "include everything (only safe for personal "
                         "transfer, not public sharing).")
    ap.add_argument("--include-non-native", action="store_true",
                    help="ship pages whose `origin:` tag indicates they "
                         "came from a previous merge (default: exclude — "
                         "republishing someone else's content via your "
                         "own subgraph-export is a chain-merge propagation "
                         "risk).")
    ap.add_argument("--keep-url-params", action="store_true",
                    help="preserve query strings on source URLs in the "
                         "manifest (default: strip them — signed S3 URLs, "
                         "session tokens, and tracking parameters can leak "
                         "data when published).")
    ap.add_argument("--quote-density-threshold", type=float, default=0.25,
                    help="warn when a wiki page is >= this fraction of "
                         "block-quoted source text (default: 0.25)")
    ap.add_argument("--yes", action="store_true",
                    help="auto-accept preflight findings and proceed "
                         "(non-interactive). Otherwise: prompt y/N when "
                         "interactive, refuse when not.")
    ap.add_argument("--strict", action="store_true",
                    help="refuse to proceed if any preflight detector fires")
    ap.add_argument("--no-preflight", action="store_true",
                    help="skip all preflight checks (not recommended)")
    ap.add_argument("--include-preflight-in-manifest", action="store_true",
                    help="write full per-finding records to the manifest "
                         "(default: counts only — published manifests should "
                         "not name files that tripped GDPR/PII detection or "
                         "they become a harvesting oracle)")
    ap.add_argument("--allow-license-class", default="",
                    help="comma-separated license-class tokens to re-include "
                         "in --include-vault=owned (default: empty). Use "
                         "`nc` to allow CC-BY-NC, `nd` for CC-BY-ND. The "
                         "wiki's normal operation may exceed both clauses; "
                         "opt in only when you've confirmed your use case "
                         "complies.")
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

    # Chain-merge defense: drop pages whose `origin:` tag indicates they
    # came from a previous merge. The user can override with
    # --include-non-native (e.g. for personal transfer where they
    # genuinely own the rights to ship merged-in content).
    if not args.include_non_native:
        pre_count = len(scope_pages)
        scope_pages = [
            p for p in scope_pages
            if not _raw_frontmatter(p.read_text(errors="replace")).get("origin")
        ]
        excluded = pre_count - len(scope_pages)
        if excluded:
            sys.stderr.write(
                f"subgraph-export: excluded {excluded} non-native page(s) "
                "(--include-non-native to override)\n"
            )
        if not scope_pages:
            raise SystemExit(
                "scope matched only non-native pages; nothing to export "
                "(pass --include-non-native if you intend to ship them)"
            )

    cited_vault_files = _collect_cited_vault(scope_pages, vault_dir)

    # Build vault metadata (always recorded) BEFORE applying the
    # include-vault filter, so the manifest captures the full citation
    # graph even when bytes are omitted. Redact URL query strings unless
    # --keep-url-params; signed URLs / session tokens / tracking params
    # leak data when published.
    vault_metadata: list[dict] = []
    for p in cited_vault_files:
        rel = str(p.relative_to(vault_dir))
        meta = _vault_source_meta(p)
        if meta.get("source_url"):
            meta["source_url"] = preflight.redact_url(
                meta["source_url"], keep_params=args.keep_url_params
            )
        vault_metadata.append({
            "rel": rel,
            "sha256": _sha256(p),
            **meta,
        })

    license_allowlist = _effective_license_allowlist(args.allow_license_class)
    vault_files = _filter_vault_for_mode(
        cited_vault_files, args.include_vault, allowlist=license_allowlist
    )

    # Pre-flight: run detectors on what we're about to ship.
    #
    # Severity-aware UX (v0.2.1.1):
    #   - info-only findings  → one-line stderr ack, no prompt, proceed
    #     (typical for academic content with sparse author-block emails)
    #   - warn / block        → show full findings, prompt y/N (or refuse
    #                            in --strict / non-interactive without --yes)
    #
    # Why: density-aware GDPR detection produces a lot of info-level hits
    # on real academic vault content (corresponding-author emails inside
    # FETCHED CONTENT markers). Those are published-by-consent and should
    # not interrupt the export flow. Real concerns (warn/block) still
    # require deliberate confirmation.
    findings: list[dict] = []
    if not args.no_preflight:
        findings = preflight.run_all(
            scope_pages=scope_pages,
            vault_files=vault_files,  # only files that will actually ship
            include_non_native=args.include_non_native,
            quote_density_threshold=args.quote_density_threshold,
        )
        info_findings = [f for f in findings if f.get("severity") == "info"]
        warn_findings = [f for f in findings
                          if f.get("severity") in ("warn", "block")]

        if info_findings:
            sys.stderr.write(
                f"preflight: {len(info_findings)} info-level finding(s) "
                "(typical for academic content with sparse author-block "
                "emails — not flagged for review)\n"
            )

        if warn_findings:
            sys.stderr.write(preflight.format_findings(warn_findings))
            if args.strict:
                raise SystemExit(
                    "preflight: --strict and warn/block findings present; "
                    "refusing"
                )
            if not args.yes:
                if not sys.stdin.isatty():
                    raise SystemExit(
                        "preflight: warn/block findings present and not "
                        "interactive (no TTY). Pass --yes to auto-accept, "
                        "--strict to refuse, or --no-preflight to skip "
                        "checks."
                    )
                sys.stderr.write("Continue with export? [y/N] ")
                sys.stderr.flush()
                ans = (sys.stdin.readline() or "").strip().lower()
                if ans not in ("y", "yes"):
                    raise SystemExit("preflight: declined by user")

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
        vault_metadata=vault_metadata,
        include_vault_mode=args.include_vault,
        preflight_findings=findings,
        include_preflight_in_manifest=args.include_preflight_in_manifest,
    )

    omitted = len(vault_metadata) - len(vault_rel)
    msg = (
        f"exported {len(pages_rel)} pages and {len(vault_rel)} vault files "
        f"to {dest}\n"
        f"manifest: {dest / '_export-manifest.json'}\n"
    )
    if omitted > 0:
        msg += (
            f"note: {omitted} cited vault file(s) omitted "
            f"(--include-vault={args.include_vault}); receivers can "
            f"hydrate them via `hydrate_vault.py` after merge.\n"
        )
    sys.stdout.write(msg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
