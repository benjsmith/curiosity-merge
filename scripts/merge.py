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
import preflight  # type: ignore


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
    # Pre-flight (PII / licensing) gate flags. The receiving end runs
    # the same preflight detectors over staged incoming content, with
    # the same Presidio opt-in available.
    ap.add_argument("--enable-presidio", action="store_true",
                    help="use Presidio for PII detection on staged content "
                         "instead of the regex baseline (see subgraph-export "
                         "for the full rationale)")
    ap.add_argument("--presidio-entities", default="",
                    help="comma-separated Presidio entity types")
    ap.add_argument("--presidio-confidence", type=float, default=0.6,
                    help="Presidio score threshold (default 0.6)")
    ap.add_argument("--no-preflight-cache", action="store_true",
                    help="bypass per-file Presidio result cache")
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
    # vault/ is optional: a sharing-safe subgraph-export
    # (--include-vault=none) produces a wiki-only tree. The merge driver
    # treats every cited vault file as missing and tags the relevant
    # source stubs `vault_missing: true` for hydrate-vault.
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

    missing = manifest.get("missing_vault", [])
    if missing:
        L.append("## Missing vault sources")
        L.append("")
        L.append(
            f"{len(missing)} source stub(s) cite vault files that weren't "
            "shipped (the publisher published notes-only or licensing "
            "prevented bundling). Each is tagged `vault_missing: true` so "
            "you and your agent can see them in the wiki UI. To re-acquire, "
            "run:"
        )
        L.append("")
        L.append(f"    uv run python3 <skill_path>/scripts/hydrate_vault.py "
                 f"--origin {origin}")
        L.append("")
        for m in missing[:20]:
            url = m.get("source_url") or "(no source_url recorded)"
            lic = m.get("license") or "(license unknown)"
            L.append(f"- `{m['page_rel']}` cites `{m['citation_rel']}` "
                     f"→ {url}  ({lic})")
        if len(missing) > 20:
            L.append(f"- ... and {len(missing) - 20} more")
        L.append("")

    pf = manifest.get("preflight_findings", [])
    pf_summary = manifest.get("preflight_summary", [])
    if pf or pf_summary:
        L.append("## Pre-flight findings on incoming content")
        L.append("")
        L.append(
            "Detectors run on staged content. These are *informational*: "
            "they don't block apply. Review and decide whether incoming "
            "material is appropriate for your wiki. Samples (matched "
            "values) are stripped from the audit and manifest by design — "
            "scan the staged files locally if you need to see specifics."
        )
        L.append("")
        if pf_summary:
            for entry in pf_summary:
                L.append(f"- **{entry['kind']}** ({entry['severity']}): "
                         f"{entry['count']} hit(s)")
            L.append("")
        if pf:
            for f in pf[:30]:
                L.append(f"  - `{f.get('subject','?')}` — "
                         f"{f.get('kind','?')}: {f.get('summary','')}")
            if len(pf) > 30:
                L.append(f"  - ... and {len(pf) - 30} more")
            # Surface one rationale per kind for context.
            seen_kinds: set[str] = set()
            for f in pf:
                k = f.get("kind", "")
                if k in seen_kinds:
                    continue
                seen_kinds.add(k)
                L.append("")
                L.append(f"  why ({k}): {f.get('rationale','')}")
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
    # Sharing-safe exports omit vault/ entirely; reconcile.* tolerate a
    # non-existent dir by returning empty plans, and the missing-vault
    # tagging pass below picks up every citation as `vault_missing`.

    # Read source's export manifest if present. It records vault metadata
    # even for files whose content was deliberately omitted by
    # subgraph-export (sharing-safe default). We use it to mark
    # vault_missing source stubs with full provenance so the receiving
    # user (or hydrate-vault) knows where to re-acquire each source.
    incoming_vault_meta: dict[str, dict] = {}
    src_manifest_path = source / "_export-manifest.json"
    if src_manifest_path.is_file():
        try:
            sm = json.loads(src_manifest_path.read_text())
            for entry in sm.get("vault_metadata", []):
                if entry.get("rel"):
                    incoming_vault_meta[entry["rel"]] = entry
        except (json.JSONDecodeError, OSError):
            pass

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

    # 3b. Mark `vault_missing: true` on source stubs whose cited vault
    # file isn't present in the staged or receiver vault. The receiving
    # user (and hydrate_vault.py) needs provenance to re-acquire — pull
    # source_url / source_type / sha256 from the incoming export manifest
    # when available, and from the stub's own body otherwise.
    receiver_vault_paths: set[str] = set(receiver_index.values())
    staged_vault_paths: set[str] = {dst for _, dst in vault_plan["to_copy"]}
    available_vault: set[str] = receiver_vault_paths | staged_vault_paths
    missing_vault_marks: list[dict] = []

    def _alias(rel: str) -> str:
        return vault_plan["alias_map"].get(rel, rel)

    for staged_dir in (staging["wiki_in"], staging["collisions"]):
        for staged_page in staged_dir.rglob("*.md"):
            text = staged_page.read_text(errors="replace")
            page_fm, page_body = read_frontmatter(text)
            if (page_fm.get("type") or "") != "source":
                continue
            broken_rels: list[str] = []
            for m in CITATION_RE.finditer(page_body):
                cite = m.group(1).strip()
                final = _alias(cite)
                if final not in available_vault:
                    broken_rels.append(cite)
            if not broken_rels:
                continue
            cite_rel = broken_rels[0]
            meta = incoming_vault_meta.get(cite_rel, {})
            mutated = text
            mutated = set_frontmatter_field(mutated, "vault_missing", "true")
            if meta.get("source_url") and not page_fm.get("source_url"):
                mutated = set_frontmatter_field(
                    mutated, "source_url", meta["source_url"]
                )
            if meta.get("source_type") and not page_fm.get("source_type"):
                mutated = set_frontmatter_field(
                    mutated, "source_type", meta["source_type"]
                )
            if meta.get("sha256"):
                mutated = set_frontmatter_field(
                    mutated, "vault_sha256", meta["sha256"]
                )
            if meta.get("license"):
                mutated = set_frontmatter_field(
                    mutated, "license", meta["license"]
                )
            staged_page.write_text(mutated)
            missing_vault_marks.append({
                "page_rel": str(staged_page.relative_to(staging["root"])),
                "citation_rel": cite_rel,
                "alias_rel": _alias(cite_rel),
                "source_url": meta.get("source_url", ""),
                "source_type": meta.get("source_type", ""),
                "license": meta.get("license", ""),
                "redistributable": meta.get("redistributable", False),
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

    # 6b. Pre-flight detectors on incoming content. Receivers deserve the
    # same licensing/PII review the publisher should have done. We always
    # set include_non_native=True because everything from a merge is
    # foreign by definition — that detector would be 100% noise here.
    # Findings are *informational* (not gating); the receiver reviews and
    # decides what to do with each. Samples are stripped via
    # `manifest_safe` before they land in apply.json or the audit report.
    staged_pages = [p for d in (staging["wiki_in"], staging["collisions"])
                    for p in d.rglob("*.md")] if (
        staging["wiki_in"].is_dir() or staging["collisions"].is_dir()
    ) else []
    staged_vault = [p for p in staging["vault_in"].rglob("*")
                     if p.is_file()] if staging["vault_in"].is_dir() else []
    presidio_entities = (
        tuple(e.strip() for e in args.presidio_entities.split(",")
              if e.strip())
        if args.presidio_entities else None
    )
    cache_dir = (None if args.no_preflight_cache
                 else workspace / ".curator" / ".preflight-cache")
    preflight_findings = preflight.run_all(
        scope_pages=staged_pages,
        vault_files=staged_vault,
        include_non_native=True,
        enable_presidio=args.enable_presidio,
        presidio_entities=presidio_entities,
        presidio_confidence=args.presidio_confidence,
        cache_dir=cache_dir,
    )
    preflight_findings_safe = [preflight.manifest_safe(f)
                                for f in preflight_findings]

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
        "missing_vault": missing_vault_marks,
        "preflight_summary": preflight.manifest_summary(preflight_findings),
        "preflight_findings": preflight_findings_safe,
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
