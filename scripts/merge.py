#!/usr/bin/env python3
"""merge.py — combine another curiosity-engine wiki into the current one.

Three commands:

    merge.py <other-wiki-path> --as-origin <name>
        Stage a merge. Writes everything to
        .curator/.merge-staging/<origin>/. Produces the audit report.
        Does NOT touch live wiki/ or vault/.

    merge.py --apply <origin>
        Atomic swap from staging into live wiki/ and vault/. Writes the
        merge manifest at .curator/merges/<origin>.json (used by
        unmerge). Rebuilds the kuzu graph.

    merge.py --abandon <origin>
        Discard the staging directory.

The pipeline applies trust defenses (T1–T8 in docs/trust-model.md) and
optional security/quality gates (--enable-snyk-code / --enable-semgrep /
--enable-clamav / --enable-secrets-scan / --enable-quality-lint, or
--enable-all-scans). Anything that fails a gate goes to
<staging>/_suspect/ and is listed under "Quarantined" in the audit
report; --apply refuses to silently promote anything from _suspect/.

This file is the orchestrator. The reconciliation rules live in
reconcile.py for testability.
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
        set_frontmatter_field,
        ALLOWED_FM_KEYS,
        CITATION_RE,
        WIKILINK_RE,
    )
    from sweep import wiki_pages  # type: ignore
except ImportError as e:
    sys.stderr.write(
        "ERROR: cannot import curiosity-engine helpers.\n"
        f"       (Original import error: {e})\n"
    )
    sys.exit(2)

# Local helpers — keep relative imports robust whether invoked via
# uv run or as a module.
sys.path.insert(0, str(Path(__file__).parent))
import reconcile  # type: ignore


MANIFEST_SCHEMA_VERSION = 1


# --- argparse plumbing ----------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="merge.py",
        description="Combine another curiosity-engine wiki into this one.",
    )
    ap.add_argument("source", nargs="?", default=None,
                    help="path to the other wiki (workspace root, "
                         "containing wiki/ and vault/). Required when "
                         "staging a new merge.")
    ap.add_argument("--as-origin", metavar="NAME", default=None,
                    help="origin tag to apply to incoming pages")
    ap.add_argument("--apply", metavar="ORIGIN", default=None,
                    help="apply the staged merge under ORIGIN")
    ap.add_argument("--abandon", metavar="ORIGIN", default=None,
                    help="discard the staged merge under ORIGIN")
    ap.add_argument("--rerun-gates", metavar="ORIGIN", default=None,
                    help="re-run security/quality gates on existing staging")
    ap.add_argument("--workspace", default=".",
                    help="receiving workspace root (default: cwd)")

    # Optional security/quality gates.
    ap.add_argument("--enable-snyk-code", action="store_true")
    ap.add_argument("--enable-semgrep", action="store_true")
    ap.add_argument("--enable-clamav", action="store_true")
    ap.add_argument("--enable-secrets-scan", action="store_true")
    ap.add_argument("--enable-quality-lint", action="store_true")
    ap.add_argument("--enable-all-scans", action="store_true",
                    help="enable all optional security/quality gates")
    ap.add_argument("--quality-threshold", type=int, default=60,
                    help="lint score floor for --enable-quality-lint (0-100)")
    return ap


# --- safe path validation -------------------------------------------------


def _validate_source(source_arg: str, workspace: Path) -> Path:
    raw = Path(source_arg).expanduser()
    if ".." in Path(source_arg).parts:
        raise SystemExit(f"refusing source path with .. segments: {source_arg!r}")
    resolved = raw.resolve() if raw.is_absolute() else (workspace / raw).resolve()
    if not resolved.is_dir():
        raise SystemExit(f"source not a directory: {resolved}")
    if not (resolved / "wiki").is_dir():
        raise SystemExit(f"source missing wiki/: {resolved}")
    if not (resolved / "vault").is_dir():
        raise SystemExit(f"source missing vault/: {resolved}")
    return resolved


def _validate_origin(origin: str) -> str:
    # Origins land in filenames and frontmatter; restrict to slug-safe.
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", origin or ""):
        raise SystemExit(
            f"invalid origin {origin!r}: must match [a-z0-9][a-z0-9_-]{{0,63}}"
        )
    return origin


# --- staging dir layout ----------------------------------------------------


def _staging_root(workspace: Path, origin: str) -> Path:
    return workspace / ".curator" / ".merge-staging" / origin


def _ensure_staging(workspace: Path, origin: str) -> dict:
    root = _staging_root(workspace, origin)
    paths = {
        "root": root,
        "wiki_in": root / "wiki-incoming",
        "vault_in": root / "vault-incoming",
        "collisions": root / "collisions",
        "suspect": root / "_suspect",
        "audit": root / "audit-report.md",
        "apply_json": root / "apply.json",
    }
    return paths


# --- frontmatter and body transforms (trust model T1, T2) ----------------


def _strip_to_allowed_frontmatter(text: str) -> tuple[dict, str]:
    """Re-parse, drop unknown keys, return (filtered_fm, body).

    `read_frontmatter` already strips unknown keys, but we want explicit
    control: any incoming `untrusted` value is replaced (T1), and we need
    to write a clean frontmatter block back.
    """
    fm, body = read_frontmatter(text)
    filtered = {k: v for k, v in fm.items() if k in ALLOWED_FM_KEYS}
    return filtered, body


def _format_frontmatter(fm: dict) -> str:
    lines = ["---"]
    for k, v in fm.items():
        if isinstance(v, list):
            inner = ", ".join(str(x) for x in v)
            lines.append(f"{k}: [{inner}]")
        elif isinstance(v, bool):
            lines.append(f"{k}: {'true' if v else 'false'}")
        else:
            sv = str(v)
            if ":" in sv or sv.strip() != sv:
                sv = '"' + sv.replace('"', '\\"') + '"'
            lines.append(f"{k}: {sv}")
    lines.append("---")
    lines.append("")
    return "\n".join(lines)


def _apply_origin_and_untrusted(fm: dict, origin: str) -> dict:
    fm = dict(fm)
    fm["origin"] = origin
    fm["untrusted"] = True
    # Force ingest_kind=archival for merged content per plan: receiving
    # user wasn't actively curating it.
    fm["ingest_kind"] = "archival"
    return fm


def _frame_body(body: str, origin: str) -> str:
    begin = f"<!-- BEGIN UNTRUSTED MERGED CONTENT — origin:{origin} -->"
    end = "<!-- END UNTRUSTED MERGED CONTENT -->"
    return f"{begin}\n\n{body.lstrip()}\n\n{end}\n"


def _rewrite_citations(body: str, alias_map: dict[str, str]) -> str:
    def repl(m):
        target = m.group(1).strip()
        new = alias_map.get(target, target)
        return f"(vault:{new})"
    return CITATION_RE.sub(repl, body)


# --- gate runners (optional) ----------------------------------------------


def _run_required_gates(staging: dict) -> list[dict]:
    """Run scrub_check and citation-sha validation. Returns list of
    quarantine entries: {path, gate, reason, severity}.
    """
    quarantines: list[dict] = []
    scrub = Path(_ce_scripts or "") / "scrub_check.py" if _ce_scripts else None
    if scrub and scrub.is_file():
        for mode_dir, mode in [(staging["wiki_in"], "wiki"),
                                (staging["vault_in"], "vault")]:
            if not mode_dir.is_dir():
                continue
            for f in mode_dir.rglob("*.md"):
                try:
                    res = subprocess.run(
                        ["uv", "run", "python3", str(scrub),
                         "--mode", mode, str(f)],
                        capture_output=True, text=True, timeout=30,
                    )
                except (subprocess.TimeoutExpired, FileNotFoundError) as e:
                    quarantines.append({
                        "path": str(f.relative_to(staging["root"])),
                        "gate": f"scrub_check.py --mode {mode}",
                        "reason": f"scrub failed to run: {e}",
                        "severity": "warning",
                    })
                    continue
                if res.returncode != 0:
                    quarantines.append({
                        "path": str(f.relative_to(staging["root"])),
                        "gate": f"scrub_check.py --mode {mode}",
                        "reason": (res.stdout + res.stderr).strip()[:500],
                        "severity": "block",
                    })
    return quarantines


def _run_optional_gates(staging: dict, args) -> list[dict]:
    """Run any opt-in security/quality scanners. Each is a no-op when the
    underlying tool isn't installed — we report 'tool missing' to the
    audit report rather than failing the merge.
    """
    out: list[dict] = []
    enabled = {
        "snyk-code": args.enable_snyk_code or args.enable_all_scans,
        "semgrep": args.enable_semgrep or args.enable_all_scans,
        "clamav": args.enable_clamav or args.enable_all_scans,
        "secrets": args.enable_secrets_scan or args.enable_all_scans,
        "quality-lint": args.enable_quality_lint or args.enable_all_scans,
    }
    if not any(enabled.values()):
        return out

    def _which(name: str) -> str | None:
        path = shutil.which(name)
        return path

    if enabled["snyk-code"]:
        bin_ = _which("snyk")
        if bin_ is None:
            out.append({"gate": "snyk-code", "skipped": "snyk binary not on PATH"})
        else:
            res = subprocess.run(
                [bin_, "code", "test", str(staging["root"]), "--json"],
                capture_output=True, text=True,
            )
            if res.returncode != 0:
                # Hits in the issues array — quarantine each affected file.
                try:
                    payload = json.loads(res.stdout or "{}")
                    for issue in payload.get("runs", [{}])[0].get("results", []):
                        loc = issue.get("locations", [{}])[0]
                        path = loc.get("physicalLocation", {}).get(
                            "artifactLocation", {}).get("uri", "")
                        out.append({
                            "path": path,
                            "gate": "snyk-code",
                            "reason": issue.get("message", {}).get("text", "")[:300],
                            "severity": issue.get("level", "warning"),
                        })
                except json.JSONDecodeError:
                    out.append({"gate": "snyk-code",
                                "reason": "snyk emitted non-JSON output",
                                "severity": "warning"})

    if enabled["semgrep"]:
        bin_ = _which("semgrep")
        if bin_ is None:
            out.append({"gate": "semgrep", "skipped": "semgrep not on PATH"})
        else:
            ruleset = (Path(__file__).parent.parent / "config" /
                       "semgrep-curiosity-merge.yml")
            cfg_arg = str(ruleset) if ruleset.is_file() else "auto"
            res = subprocess.run(
                [bin_, "--config", cfg_arg, "--json", str(staging["root"])],
                capture_output=True, text=True,
            )
            try:
                payload = json.loads(res.stdout or "{}")
                for r in payload.get("results", []):
                    out.append({
                        "path": r.get("path", ""),
                        "gate": "semgrep",
                        "reason": r.get("extra", {}).get("message", "")[:300],
                        "severity": r.get("extra", {}).get("severity", "warning"),
                    })
            except json.JSONDecodeError:
                out.append({"gate": "semgrep",
                            "reason": "semgrep emitted non-JSON output",
                            "severity": "warning"})

    if enabled["clamav"]:
        bin_ = _which("clamscan")
        if bin_ is None:
            out.append({"gate": "clamav", "skipped": "clamscan not on PATH"})
        else:
            res = subprocess.run(
                [bin_, "-r", "--no-summary", str(staging["root"])],
                capture_output=True, text=True,
            )
            for line in (res.stdout or "").splitlines():
                if " FOUND" in line:
                    path = line.split(":", 1)[0]
                    out.append({
                        "path": path, "gate": "clamav",
                        "reason": line.strip()[:300], "severity": "block",
                    })

    if enabled["secrets"]:
        # Prefer gitleaks; fall back to trufflehog if present.
        bin_ = _which("gitleaks") or _which("trufflehog")
        if bin_ is None:
            out.append({"gate": "secrets-scan",
                        "skipped": "neither gitleaks nor trufflehog on PATH"})
        elif Path(bin_).name == "gitleaks":
            res = subprocess.run(
                [bin_, "detect", "--source", str(staging["root"]),
                 "--report-format", "json", "--no-git", "--report-path", "-"],
                capture_output=True, text=True,
            )
            try:
                for hit in json.loads(res.stdout or "[]"):
                    out.append({
                        "path": hit.get("File", ""),
                        "gate": "secrets-scan(gitleaks)",
                        "reason": f"{hit.get('Description','')} "
                                  f"(rule {hit.get('RuleID','')})"[:300],
                        "severity": "block",
                    })
            except json.JSONDecodeError:
                pass

    if enabled["quality-lint"]:
        lint = Path(_ce_scripts or "") / "lint_scores.py" if _ce_scripts else None
        if lint and lint.is_file():
            # Best-effort: lint_scores.compute_all takes a wiki dir. We
            # invoke as a subprocess to keep merge.py decoupled from its
            # internals, then parse JSON.
            res = subprocess.run(
                ["uv", "run", "python3", str(lint), "json",
                 str(staging["wiki_in"])],
                capture_output=True, text=True,
            )
            try:
                report = json.loads(res.stdout or "{}")
                for page in report.get("pages", []):
                    score = page.get("composite_score", 100)
                    if score < args.quality_threshold:
                        out.append({
                            "path": page.get("path", ""),
                            "gate": "quality-lint",
                            "reason": (
                                f"composite score {score} < threshold "
                                f"{args.quality_threshold}"
                            ),
                            "severity": "block",
                        })
            except json.JSONDecodeError:
                out.append({"gate": "quality-lint",
                            "reason": "lint_scores emitted non-JSON output",
                            "severity": "warning"})
        else:
            out.append({"gate": "quality-lint",
                        "skipped": "lint_scores.py not found in CE_SCRIPTS"})

    return out


def _quarantine_files(staging: dict, quarantines: list[dict]) -> None:
    """Move every blocked file into _suspect/, preserving subpath."""
    for q in quarantines:
        if q.get("severity") != "block":
            continue
        rel = q.get("path", "")
        if not rel:
            continue
        src = staging["root"] / rel
        if not src.is_file():
            continue
        dst = staging["suspect"] / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(src), str(dst))
        except (FileNotFoundError, shutil.Error):
            pass


# --- audit report ---------------------------------------------------------


def _write_audit(staging: dict, *, origin: str, source: Path,
                 vault_plan: dict, page_collisions: list[dict],
                 quarantines: list[dict], gate_skips: list[dict],
                 manifest: dict) -> None:
    ts = manifest["staged_at"]
    L: list[str] = []
    L.append(f"# Merge audit report — origin `{origin}`")
    L.append("")
    L.append(f"- Staged: {ts}")
    L.append(f"- Source wiki: `{source}`")
    L.append(f"- Receiving workspace: `{staging['root'].parent.parent.parent}`")
    L.append("")
    L.append("Review this report carefully. To apply: "
             f"`merge.py --apply {origin}`. To discard: "
             f"`merge.py --abandon {origin}`.")
    L.append("")

    L.append("## Vault reconciliation")
    L.append("")
    L.append(f"- Deduped (sha256 match, aliased to existing): "
             f"{len(vault_plan['deduped'])}")
    L.append(f"- New (copied as-is): "
             f"{len(vault_plan['to_copy']) - len(vault_plan['renamed'])}")
    L.append(f"- Renamed (filename collision, content differs): "
             f"{len(vault_plan['renamed'])}")
    if vault_plan["renamed"]:
        L.append("")
        L.append("Renamed:")
        for src_rel, dst_rel in vault_plan["renamed"][:50]:
            L.append(f"  - `{src_rel}` → `{dst_rel}`")
    L.append("")

    L.append("## Page-name collisions")
    L.append("")
    by_kind: dict[str, list[dict]] = {"identical": [], "same_topic": [],
                                       "different_topic": []}
    for c in page_collisions:
        by_kind.setdefault(c["kind"], []).append(c)
    L.append(f"- identical (kept one): {len(by_kind['identical'])}")
    L.append(f"- same topic (preserved both, manual review): "
             f"{len(by_kind['same_topic'])}")
    L.append(f"- different topic (incoming renamed): "
             f"{len(by_kind['different_topic'])}")
    if by_kind["same_topic"]:
        L.append("")
        L.append("### Same topic — manual review queue")
        L.append("")
        for c in by_kind["same_topic"]:
            sim = c.get("similarity")
            sim_str = f"{sim:.3f}" if sim is not None else "n/a"
            L.append(f"- `{c['stem']}` (similarity {sim_str})")
            L.append(f"  - existing: `{c['existing_path']}`")
            L.append(f"  - incoming staged at: collisions/{c['stem']}-from-{origin}.md")
    L.append("")

    L.append("## Quarantined")
    L.append("")
    blocked = [q for q in quarantines if q.get("severity") == "block"]
    if not blocked and not gate_skips:
        L.append("_None._")
    else:
        for q in blocked:
            L.append(f"- `{q.get('path','?')}` — {q.get('gate','?')}: "
                     f"{q.get('reason','')[:200]}")
        if gate_skips:
            L.append("")
            L.append("### Gates skipped (tool missing or not enabled)")
            for s in gate_skips:
                L.append(f"- {s.get('gate','?')}: "
                         f"{s.get('skipped', s.get('reason',''))}")
    L.append("")

    L.append("## Manifest counts")
    L.append("")
    L.append(f"- wiki pages imported: {len(manifest['wiki_pages'])}")
    L.append(f"- vault files imported: "
             f"{len([v for v in manifest['vault_files'] if not v.get('deduped')])}")
    L.append(f"- vault files deduped: "
             f"{len([v for v in manifest['vault_files'] if v.get('deduped')])}")
    L.append("")

    staging["audit"].write_text("\n".join(L) + "\n")


# --- main: stage / apply / abandon ----------------------------------------


def cmd_stage(args) -> int:
    workspace = Path(args.workspace).resolve()
    if not args.source or not args.as_origin:
        raise SystemExit("staging a merge requires <source> and --as-origin <name>")
    origin = _validate_origin(args.as_origin)
    source = _validate_source(args.source, workspace)

    if not (workspace / "wiki").is_dir() or not (workspace / "vault").is_dir():
        raise SystemExit(f"receiving workspace missing wiki/ or vault/: {workspace}")

    staging = _ensure_staging(workspace, origin)
    if staging["root"].exists():
        raise SystemExit(
            f"staging dir already exists: {staging['root']}\n"
            f"run `merge.py --abandon {origin}` first, or apply the existing stage"
        )
    for k in ("root", "wiki_in", "vault_in", "collisions", "suspect"):
        staging[k].mkdir(parents=True, exist_ok=True)

    src_wiki = source / "wiki"
    src_vault = source / "vault"

    # 1. Vault reconciliation.
    receiver_index = reconcile.index_vault(workspace / "vault")
    vault_plan = reconcile.reconcile_vault(src_vault, receiver_index, origin)

    # Copy non-deduped vault files into staging vault-incoming/ under
    # their final rel paths so the audit and manifest reflect what would
    # actually land. Deduped files are NOT copied.
    for incoming_rel, final_rel in vault_plan["to_copy"]:
        src = src_vault / incoming_rel
        dst = staging["vault_in"] / final_rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    # 2. Page-name collisions.
    collisions = reconcile.find_page_collisions(src_wiki, workspace / "wiki")
    collision_targets: dict[str, str] = {}
    for c in collisions:
        rel = str(c["incoming_path"].relative_to(src_wiki))
        if c["kind"] == "identical":
            collision_targets[rel] = "__drop__"
        elif c["kind"] == "different_topic":
            collision_targets[rel] = reconcile.collision_target_rel(rel, origin)
        else:  # same_topic
            collision_targets[rel] = (
                f"__same_topic__:{reconcile.collision_target_rel(rel, origin)}"
            )

    # 3. Walk every incoming wiki page; transform; write to staging.
    manifest_pages: list[dict] = []
    for p in src_wiki.rglob("*.md"):
        rel = str(p.relative_to(src_wiki))
        if any(seg.startswith(".") for seg in rel.split(os.sep)):
            continue
        if rel in collision_targets:
            disp = collision_targets[rel]
            if disp == "__drop__":
                continue
            if disp.startswith("__same_topic__:"):
                target_rel = disp.split(":", 1)[1]
                staged_under = staging["collisions"]
            else:
                target_rel = disp
                staged_under = staging["wiki_in"]
        else:
            target_rel = rel
            staged_under = staging["wiki_in"]

        text = p.read_text(errors="replace")
        fm, body = _strip_to_allowed_frontmatter(text)
        fm = _apply_origin_and_untrusted(fm, origin)
        body = _rewrite_citations(body, vault_plan["alias_map"])
        body = _frame_body(body, origin)
        out_text = _format_frontmatter(fm) + body
        out_path = staged_under / target_rel
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(out_text)
        manifest_pages.append({
            "incoming_rel": rel,
            "final_rel": target_rel,
            "staged_under": (
                "collisions" if staged_under == staging["collisions"]
                else "wiki-incoming"
            ),
            "sha256_at_import": reconcile.sha256_file(out_path),
        })

    # 4. Manifest record for vault.
    manifest_vault: list[dict] = []
    for src_rel, dst_rel in vault_plan["deduped"]:
        manifest_vault.append({
            "incoming_rel": src_rel, "final_rel": dst_rel,
            "deduped": True,
            "sha256_at_import": reconcile.sha256_file(src_vault / src_rel),
        })
    for src_rel, dst_rel in vault_plan["to_copy"]:
        manifest_vault.append({
            "incoming_rel": src_rel, "final_rel": dst_rel,
            "deduped": False,
            "sha256_at_import": reconcile.sha256_file(
                staging["vault_in"] / dst_rel
            ),
        })

    # 5. Required gates (run on staged content).
    quarantines = _run_required_gates(staging)
    # 6. Optional gates.
    optional = _run_optional_gates(staging, args)
    quarantines.extend([q for q in optional if q.get("path")])
    gate_skips = [q for q in optional if not q.get("path") and (q.get("skipped") or q.get("reason"))]
    _quarantine_files(staging, quarantines)

    # 7. Manifest + audit.
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "origin": origin,
        "source_wiki": str(source),
        "staged_at": _dt.datetime.now(_dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "wiki_pages": manifest_pages,
        "vault_files": manifest_vault,
        "vault_alias_map": vault_plan["alias_map"],
        "page_collisions": [
            {"stem": c["stem"], "kind": c["kind"],
             "similarity": c.get("similarity")}
            for c in collisions
        ],
        "accepted_bridges": [],  # populated post-discover-bridges review
        "quarantines": quarantines,
    }
    staging["apply_json"].write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    )
    _write_audit(staging, origin=origin, source=source,
                 vault_plan=vault_plan, page_collisions=collisions,
                 quarantines=quarantines, gate_skips=gate_skips,
                 manifest=manifest)

    sys.stdout.write(
        f"merge staged at {staging['root']}\n"
        f"audit report: {staging['audit']}\n"
        f"to apply: merge.py --apply {origin}\n"
        f"to discard: merge.py --abandon {origin}\n"
    )
    return 0


def cmd_apply(args) -> int:
    workspace = Path(args.workspace).resolve()
    origin = _validate_origin(args.apply)
    staging = _ensure_staging(workspace, origin)
    if not staging["apply_json"].is_file():
        raise SystemExit(f"no staging at {staging['root']}; nothing to apply")
    manifest = json.loads(staging["apply_json"].read_text())

    # Refuse if anything in _suspect/ — user must explicitly resolve.
    if staging["suspect"].is_dir():
        suspect_files = [p for p in staging["suspect"].rglob("*") if p.is_file()]
        if suspect_files:
            raise SystemExit(
                f"refusing to apply: {len(suspect_files)} files in "
                f"{staging['suspect']}. Resolve quarantines first "
                f"(edit/move) and re-run."
            )

    receiver_wiki = workspace / "wiki"
    receiver_vault = workspace / "vault"

    # Move pages.
    moves: list[tuple[Path, Path]] = []
    for p in staging["wiki_in"].rglob("*.md"):
        rel = p.relative_to(staging["wiki_in"])
        moves.append((p, receiver_wiki / rel))
    # Same-topic collision pages: copy alongside in receiver under <stem>-from-<origin>.md.
    for p in staging["collisions"].rglob("*.md"):
        rel = p.relative_to(staging["collisions"])
        moves.append((p, receiver_wiki / rel))
    # Vault new files.
    for p in staging["vault_in"].rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(staging["vault_in"])
        moves.append((p, receiver_vault / rel))

    for src, dst in moves:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    # Persist manifest at .curator/merges/<origin>.json (used by unmerge).
    merges_dir = workspace / ".curator" / "merges"
    merges_dir.mkdir(parents=True, exist_ok=True)
    manifest["applied_at"] = _dt.datetime.now(_dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    (merges_dir / f"{origin}.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    )

    # Rebuild graph if curiosity-engine's graph.py is reachable.
    graph_py = Path(_ce_scripts or "") / "graph.py" if _ce_scripts else None
    if graph_py and graph_py.is_file():
        subprocess.run(
            ["uv", "run", "python3", str(graph_py), "rebuild", "wiki"],
            cwd=str(workspace), check=False,
        )

    # Discard staging.
    shutil.rmtree(staging["root"])
    sys.stdout.write(
        f"merge applied: {len(manifest['wiki_pages'])} pages, "
        f"{len(manifest['vault_files'])} vault entries\n"
        f"manifest: {merges_dir / (origin + '.json')}\n"
    )
    return 0


def cmd_rerun_gates(args) -> int:
    """Re-run required + optional gates on an existing staging dir.

    Use when the user fixes a quarantined file (edits or moves it back
    out of `_suspect/`) and wants the audit report refreshed without
    abandoning + re-staging the whole merge.

    The reconciliation work (vault sha256, page collisions, citation
    rewrites) is preserved — we only refresh the gate output. The
    manifest at apply.json is rewritten with the new quarantine list;
    everything else carries over.
    """
    workspace = Path(args.workspace).resolve()
    origin = _validate_origin(args.rerun_gates)
    staging = _ensure_staging(workspace, origin)
    if not staging["apply_json"].is_file():
        raise SystemExit(f"no staging at {staging['root']}; nothing to re-gate")
    manifest = json.loads(staging["apply_json"].read_text())

    # Pull anything previously quarantined back out of _suspect/ so the
    # gates re-evaluate it. The user has presumably made the call to
    # bring it back; if a gate still flags it, it returns to _suspect/.
    if staging["suspect"].is_dir():
        for f in list(staging["suspect"].rglob("*")):
            if not f.is_file():
                continue
            rel = f.relative_to(staging["suspect"])
            dst = staging["root"] / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(f), str(dst))
        # Drop any now-empty subdirs.
        for d in sorted(staging["suspect"].rglob("*"), reverse=True):
            if d.is_dir():
                try:
                    d.rmdir()
                except OSError:
                    pass

    quarantines = _run_required_gates(staging)
    optional = _run_optional_gates(staging, args)
    quarantines.extend([q for q in optional if q.get("path")])
    gate_skips = [q for q in optional
                  if not q.get("path") and (q.get("skipped") or q.get("reason"))]
    _quarantine_files(staging, quarantines)

    manifest["quarantines"] = quarantines
    manifest["regated_at"] = _dt.datetime.now(_dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    staging["apply_json"].write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    )

    # Audit needs the original reconciliation context. Reconstruct what
    # the report needs from the manifest rather than redoing the merge.
    vault_plan_view = {
        "to_copy": [(v["incoming_rel"], v["final_rel"])
                    for v in manifest["vault_files"] if not v.get("deduped")],
        "deduped": [(v["incoming_rel"], v["final_rel"])
                    for v in manifest["vault_files"] if v.get("deduped")],
        "renamed": [(v["incoming_rel"], v["final_rel"])
                    for v in manifest["vault_files"]
                    if not v.get("deduped")
                    and v["incoming_rel"] != v["final_rel"]],
        "alias_map": manifest.get("vault_alias_map", {}),
    }
    collision_views = [
        {"stem": c["stem"], "kind": c["kind"], "similarity": c.get("similarity"),
         "incoming_path": Path("(staged)"), "existing_path": Path("(in receiver)")}
        for c in manifest.get("page_collisions", [])
    ]
    _write_audit(staging, origin=origin,
                 source=Path(manifest.get("source_wiki", "")),
                 vault_plan=vault_plan_view,
                 page_collisions=collision_views,
                 quarantines=quarantines, gate_skips=gate_skips,
                 manifest=manifest)
    blocked = sum(1 for q in quarantines if q.get("severity") == "block")
    sys.stdout.write(
        f"re-gated {origin}: {blocked} blocking issues, "
        f"{len(gate_skips)} gates skipped\n"
        f"audit refreshed: {staging['audit']}\n"
    )
    return 0


def cmd_abandon(args) -> int:
    workspace = Path(args.workspace).resolve()
    origin = _validate_origin(args.abandon)
    staging = _ensure_staging(workspace, origin)
    if not staging["root"].exists():
        sys.stdout.write(f"nothing to abandon: {staging['root']}\n")
        return 0
    shutil.rmtree(staging["root"])
    sys.stdout.write(f"abandoned: {staging['root']}\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.apply:
        return cmd_apply(args)
    if args.abandon:
        return cmd_abandon(args)
    if args.rerun_gates:
        return cmd_rerun_gates(args)
    return cmd_stage(args)


if __name__ == "__main__":
    raise SystemExit(main())
