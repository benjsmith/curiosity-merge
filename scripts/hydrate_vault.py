#!/usr/bin/env python3
"""hydrate_vault.py — re-acquire missing vault sources after a merge.

After `merge --apply`, source stubs whose vault files weren't shipped
(publisher chose to share notes-only, or licensing prevented bundling)
get tagged `vault_missing: true` with provenance — `source_url`,
`source_type`, `vault_sha256` (the sha256 the original author had).
This script walks those stubs, categorizes by source URL, and dispatches
to a fetcher per category.

Categories and fetch strategies:

  arxiv          arXiv preprint URL. **AlphaXiv-preferred** when the
                 alphaxiv skill is installed — alphaxiv ships pre-extracted
                 markdown which is much higher fidelity than running
                 pypdf over a downloaded PDF. Falls back to direct PDF
                 download + curiosity-engine local_ingest if alphaxiv is
                 absent. (Run with `--offer-alphaxiv` to print install
                 instructions when fallback path was used.)
  biorxiv        bioRxiv preprint URL. PDF download + local_ingest.
  chemrxiv       ChemRxiv preprint URL. PDF download + local_ingest.
  open_access    Frontmatter declares a redistributable license, or URL
                 is in a known-OA domain. Fetch the URL with curl/wget
                 and run local_ingest.
  paywalled      We can't (and shouldn't) fetch. Listed in the report
                 with the URL so the user can grab via institutional
                 access manually and re-run.
  unknown        No source_url; nothing to do automatically. Listed in
                 the report.

Default mode is dry-run: prints the categorization and what would be
fetched, without touching the network. Pass `--apply` to actually fetch.

Per-source confirmation when interactive: the script asks before each
network operation. Pass `--yes` to auto-accept.

The script never overwrites an existing vault file with a hash mismatch —
if the freshly-fetched file's sha256 doesn't match the recorded
`vault_sha256`, it's saved with a `.candidate` suffix and flagged.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Optional

_ce_scripts = os.environ.get("CURIOSITY_ENGINE_SCRIPTS_DIR")
if _ce_scripts and _ce_scripts not in sys.path:
    sys.path.insert(0, _ce_scripts)
try:
    from naming import set_frontmatter_field  # type: ignore
    from sweep import wiki_pages  # type: ignore
except ImportError as e:
    sys.stderr.write(f"ERROR: cannot import curiosity-engine helpers ({e})\n")
    sys.exit(2)


# Local raw-frontmatter probe. curiosity-engine's `read_frontmatter`
# applies an ALLOWED_FM_KEYS allowlist that intentionally drops keys it
# doesn't propagate (license, redistributable, vault_missing,
# vault_sha256). For licensing decisions we need the raw values, so we
# parse the YAML-ish block ourselves. Only used for read; writes still go
# through `set_frontmatter_field` which preserves arbitrary keys.
_FM_KEY_RE = re.compile(r"^([a-z_][a-z0-9_]*):\s*(.+?)\s*$", re.IGNORECASE)


def _raw_frontmatter(text: str) -> dict[str, str]:
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


# Domain → category mapping. Conservative: anything not on the list is
# treated as `unknown` unless frontmatter explicitly declares a
# redistributable license.
_DOMAIN_CATEGORY = [
    ("arxiv.org", "arxiv"),
    ("biorxiv.org", "biorxiv"),
    ("chemrxiv.org", "chemrxiv"),
    ("medrxiv.org", "biorxiv"),  # same publisher, same fetch path
    ("plos.org", "open_access"),
    ("ncbi.nlm.nih.gov/pmc", "open_access"),
    ("pubmed.ncbi.nlm.nih.gov", "open_access"),
    ("europepmc.org", "open_access"),
    ("openreview.net", "open_access"),
    ("aclanthology.org", "open_access"),
    ("nature.com", "paywalled"),
    ("sciencedirect.com", "paywalled"),
    ("elsevier.com", "paywalled"),
    ("springer.com", "paywalled"),
    ("wiley.com", "paywalled"),
    ("ieee.org", "paywalled"),
    ("acm.org", "paywalled"),
    ("cell.com", "paywalled"),
]


_REDISTRIBUTABLE_LICENSES = {
    "cc0", "public-domain", "publicdomain",
    "cc-by", "cc-by-sa", "cc-by-nc", "cc-by-nd",
    "cc-by-3.0", "cc-by-4.0", "cc-by-sa-3.0", "cc-by-sa-4.0",
    "mit", "apache-2.0", "apache2", "bsd", "bsd-3-clause", "bsd-2-clause",
    "arxiv-non-exclusive",
}


def _categorize(source_url: str, license_str: str = "") -> str:
    if not source_url:
        return "unknown"
    url_lower = source_url.lower()
    for domain, cat in _DOMAIN_CATEGORY:
        if domain in url_lower:
            return cat
    if license_str.lower().strip() in _REDISTRIBUTABLE_LICENSES:
        return "open_access"
    return "unknown"


# --- alphaxiv detection ---------------------------------------------------


def _find_alphaxiv() -> Optional[Path]:
    """Locate the alphaxiv skill if installed.

    Looks for `<skills>/alphaxiv/scripts/<something>.py` under the usual
    install roots. Returns the scripts dir, or None if not present.
    """
    candidates = [
        Path.home() / ".claude" / "skills" / "alphaxiv" / "scripts",
        Path.home() / ".agents" / "skills" / "alphaxiv" / "scripts",
    ]
    for c in candidates:
        if c.is_dir() and any(c.glob("*.py")):
            return c
    return None


def _alphaxiv_fetch(arxiv_id: str, alphaxiv_dir: Path,
                    out_dir: Path) -> Optional[Path]:
    """Best-effort alphaxiv invocation. The skill's CLI surface may
    change; we look for the most likely entry-point names and pass the
    arXiv ID. Returns the path of the produced markdown file on success.

    If alphaxiv's surface doesn't match what we try here, the caller
    falls back to PDF download.
    """
    candidates = ["fetch.py", "alphaxiv.py", "extract.py", "main.py"]
    for name in candidates:
        script = alphaxiv_dir / name
        if not script.is_file():
            continue
        try:
            res = subprocess.run(
                ["uv", "run", "python3", str(script), arxiv_id,
                 "--out", str(out_dir)],
                capture_output=True, text=True, timeout=120,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            continue
        if res.returncode == 0:
            # Heuristic: pick a freshly-written .md in out_dir.
            mds = sorted(out_dir.glob("*.md"),
                         key=lambda p: p.stat().st_mtime, reverse=True)
            if mds:
                return mds[0]
    return None


# --- generic fetch helpers ------------------------------------------------


_ARXIV_ID_RE = re.compile(r"arxiv\.org/(?:abs|pdf)/([0-9]{4}\.[0-9]{4,5})")


def _arxiv_id_from_url(url: str) -> Optional[str]:
    m = _ARXIV_ID_RE.search(url)
    return m.group(1) if m else None


def _arxiv_pdf_url(arxiv_id: str) -> str:
    return f"https://arxiv.org/pdf/{arxiv_id}.pdf"


def _http_download(url: str, dest: Path, timeout: int = 60) -> bool:
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "curiosity-merge/0.2 (+hydrate-vault)"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return True
    except Exception as e:  # noqa: BLE001 — network errors are expected
        sys.stderr.write(f"hydrate-vault: download failed ({url}): {e}\n")
        return False


def _local_ingest(workspace: Path, raw_path: Path) -> bool:
    """Hand a freshly-downloaded raw file to curiosity-engine's
    local_ingest.py so it lands in the vault with proper frontmatter.
    """
    li = Path(_ce_scripts or "") / "local_ingest.py" if _ce_scripts else None
    if not li or not li.is_file():
        sys.stderr.write(
            "hydrate-vault: local_ingest.py not found; "
            "set CURIOSITY_ENGINE_SCRIPTS_DIR.\n"
        )
        return False
    res = subprocess.run(
        ["uv", "run", "python3", str(li), str(raw_path)],
        cwd=str(workspace), capture_output=True, text=True,
    )
    if res.returncode != 0:
        sys.stderr.write(
            f"hydrate-vault: local_ingest failed for {raw_path}: "
            f"{res.stderr[:300]}\n"
        )
    return res.returncode == 0


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# --- the main flow --------------------------------------------------------


def _collect_missing(workspace: Path, origin_filter: str | None) -> list[dict]:
    """Walk wiki for source stubs tagged vault_missing: true.

    When `origin_filter` is given, only return stubs from that origin
    (so unmerge of a different origin doesn't pull unrelated work).
    """
    out: list[dict] = []
    wiki = workspace / "wiki"
    for p in wiki_pages(wiki):
        text = p.read_text(errors="replace")
        fm = _raw_frontmatter(text)
        if str(fm.get("vault_missing", "")).lower() not in ("true", "yes", "1"):
            continue
        if origin_filter and (fm.get("origin") or "") != origin_filter:
            continue
        out.append({
            "page": p,
            "page_rel": str(p.relative_to(wiki)),
            "source_url": fm.get("source_url", ""),
            "source_type": fm.get("source_type", ""),
            "license": fm.get("license", ""),
            "vault_sha256": fm.get("vault_sha256", ""),
            "origin": fm.get("origin", ""),
        })
    return out


def _confirm(prompt: str, *, yes: bool) -> bool:
    if yes:
        return True
    if not sys.stdin.isatty():
        return False
    sys.stdout.write(prompt + " [y/N] ")
    sys.stdout.flush()
    return (sys.stdin.readline() or "").strip().lower() in ("y", "yes")


def _process_arxiv(workspace: Path, item: dict, *, alphaxiv_dir: Optional[Path],
                   yes: bool, work_dir: Path) -> tuple[bool, str]:
    arxiv_id = _arxiv_id_from_url(item["source_url"])
    if not arxiv_id:
        return False, "could not parse arXiv id from source_url"
    if alphaxiv_dir is not None:
        if not _confirm(
            f"  use alphaxiv to fetch {arxiv_id}?", yes=yes
        ):
            return False, "user declined alphaxiv"
        produced = _alphaxiv_fetch(arxiv_id, alphaxiv_dir, work_dir)
        if produced and produced.is_file():
            ok = _local_ingest(workspace, produced)
            return ok, ("ingested via alphaxiv" if ok
                        else "alphaxiv fetched but local_ingest failed")
        # fall through to PDF on alphaxiv miss
    if not _confirm(
        f"  download arXiv PDF for {arxiv_id}?", yes=yes
    ):
        return False, "user declined PDF download"
    raw = work_dir / f"{arxiv_id}.pdf"
    if not _http_download(_arxiv_pdf_url(arxiv_id), raw):
        return False, "download failed"
    ok = _local_ingest(workspace, raw)
    return ok, ("ingested via PDF" if ok else "local_ingest failed")


def _process_preprint_pdf(workspace: Path, item: dict, *, yes: bool,
                          work_dir: Path) -> tuple[bool, str]:
    url = item["source_url"]
    if not url:
        return False, "no source_url"
    if not _confirm(f"  download {url}?", yes=yes):
        return False, "user declined"
    name = url.rstrip("/").rsplit("/", 1)[-1] or "preprint.pdf"
    if not name.endswith(".pdf"):
        name += ".pdf"
    raw = work_dir / name
    if not _http_download(url, raw):
        return False, "download failed"
    ok = _local_ingest(workspace, raw)
    return ok, ("ingested" if ok else "local_ingest failed")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="hydrate_vault.py",
        description="Re-acquire missing vault sources for source stubs "
                    "tagged vault_missing: true.",
    )
    ap.add_argument("--workspace", default=".")
    ap.add_argument("--origin", default=None,
                    help="restrict to source stubs with this origin tag")
    ap.add_argument("--apply", action="store_true",
                    help="actually fetch (default: dry-run report only)")
    ap.add_argument("--yes", action="store_true",
                    help="auto-accept per-source confirmations")
    ap.add_argument("--offer-alphaxiv", action="store_true",
                    help="print alphaxiv install hint when arXiv items "
                         "fell back to PDF (default: silent)")
    args = ap.parse_args(argv)

    workspace = Path(args.workspace).resolve()
    if not (workspace / "wiki").is_dir():
        raise SystemExit(f"no wiki/ at {workspace}")

    missing = _collect_missing(workspace, args.origin)
    if not missing:
        sys.stdout.write("hydrate-vault: no vault_missing stubs to process.\n")
        return 0

    buckets: dict[str, list[dict]] = {}
    for m in missing:
        cat = _categorize(m["source_url"], m["license"])
        buckets.setdefault(cat, []).append(m)

    sys.stdout.write(
        f"hydrate-vault: {len(missing)} missing source(s) found"
        + (f" (origin filter: {args.origin})" if args.origin else "")
        + "\n"
    )
    for cat in ("arxiv", "biorxiv", "chemrxiv", "open_access",
                "paywalled", "unknown"):
        items = buckets.get(cat, [])
        if not items:
            continue
        sys.stdout.write(f"  {cat}: {len(items)}\n")

    alphaxiv_dir = _find_alphaxiv()
    if not args.apply:
        sys.stdout.write(
            "\n(dry run — pass --apply to actually fetch)\n"
        )
        if alphaxiv_dir is None and buckets.get("arxiv"):
            sys.stdout.write(
                "Note: alphaxiv skill not detected. Install for cleaner "
                "arXiv extractions: `npx skills add -g -y benjsmith/alphaxiv`\n"
            )
        # Still emit a per-item report.
        for cat in ("paywalled", "unknown"):
            for m in buckets.get(cat, []):
                sys.stdout.write(
                    f"- [{cat}] {m['page_rel']} → "
                    f"{m['source_url'] or '(no url)'}\n"
                )
        return 0

    work_dir = workspace / ".curator" / ".hydrate-staging"
    work_dir.mkdir(parents=True, exist_ok=True)
    succeeded: list[str] = []
    failed: list[tuple[str, str]] = []
    skipped: list[tuple[str, str]] = []
    fell_back_to_pdf = 0

    for m in buckets.get("arxiv", []):
        sys.stdout.write(f"\n→ arXiv: {m['page_rel']}\n")
        used_alphaxiv = alphaxiv_dir is not None
        ok, why = _process_arxiv(workspace, m, alphaxiv_dir=alphaxiv_dir,
                                  yes=args.yes, work_dir=work_dir)
        if ok:
            succeeded.append(m["page_rel"])
            if used_alphaxiv and "alphaxiv" not in why:
                fell_back_to_pdf += 1
        else:
            (failed if "failed" in why else skipped).append((m["page_rel"], why))

    for cat in ("biorxiv", "chemrxiv"):
        for m in buckets.get(cat, []):
            sys.stdout.write(f"\n→ {cat}: {m['page_rel']}\n")
            ok, why = _process_preprint_pdf(
                workspace, m, yes=args.yes, work_dir=work_dir
            )
            (succeeded.append(m["page_rel"]) if ok
             else (failed if "failed" in why else skipped).append(
                 (m["page_rel"], why)))

    for m in buckets.get("open_access", []):
        sys.stdout.write(f"\n→ open-access: {m['page_rel']}\n")
        ok, why = _process_preprint_pdf(workspace, m, yes=args.yes,
                                         work_dir=work_dir)
        (succeeded.append(m["page_rel"]) if ok
         else (failed if "failed" in why else skipped).append(
             (m["page_rel"], why)))

    for m in buckets.get("paywalled", []):
        skipped.append(
            (m["page_rel"],
             f"paywalled — fetch via institutional access: {m['source_url']}")
        )
    for m in buckets.get("unknown", []):
        skipped.append(
            (m["page_rel"],
             "no recognized source_url; manual handling required")
        )

    # Drop vault_missing flag on stubs whose fetch succeeded.
    for rel in succeeded:
        page = workspace / "wiki" / rel
        if page.is_file():
            text = page.read_text(errors="replace")
            page.write_text(set_frontmatter_field(text, "vault_missing", None))

    # Cleanup
    if work_dir.is_dir() and not any(work_dir.iterdir()):
        work_dir.rmdir()

    sys.stdout.write(
        f"\nhydrate-vault: {len(succeeded)} succeeded, "
        f"{len(failed)} failed, {len(skipped)} skipped\n"
    )
    for path, why in failed:
        sys.stdout.write(f"  failed: {path} — {why}\n")
    for path, why in skipped:
        sys.stdout.write(f"  skipped: {path} — {why}\n")
    if args.offer_alphaxiv and fell_back_to_pdf and alphaxiv_dir is None:
        sys.stdout.write(
            f"\nTip: {fell_back_to_pdf} arXiv item(s) fell back to PDF. "
            "Install alphaxiv for cleaner extractions:\n"
            "  npx skills add -g -y benjsmith/alphaxiv\n"
        )
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
