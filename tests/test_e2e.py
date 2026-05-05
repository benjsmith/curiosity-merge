"""End-to-end coverage of the curiosity-merge verbs.

Each test is a self-contained scenario built on the wiki_a / wiki_b
fixtures from conftest.py. Tests run real subprocesses against the
shipped scripts so they catch regressions in argument parsing, exit
codes, and import-time setup as well as logic.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from conftest import run_script


# --- subgraph-export -------------------------------------------------------


def test_subgraph_export_project_scope(wiki_a: Path, env_with_ce, tmp_path):
    out = tmp_path / "export-mlf"
    run_script(
        "subgraph_export.py",
        "--project", "ml-foundations",
        "--to", str(out),
        "--include-vault", "all",
        "--workspace", str(wiki_a),
        env=env_with_ce,
    )
    assert (out / "_export-manifest.json").is_file()
    manifest = json.loads((out / "_export-manifest.json").read_text())
    assert manifest["scope"]["kind"] == "project"
    assert manifest["scope"]["value"] == "ml-foundations"
    # Project home page is included even if not directly tagged.
    pages = set(manifest["scope_pages"])
    assert "concepts/transformer.md" in pages
    assert "concepts/attention.md" in pages
    assert "projects/ml-foundations.md" in pages
    # With --include-vault=all, the cited file rides along.
    assert manifest["scope_vault"] == ["vaswani-2017-attention.extracted.md"]
    # Metadata recorded regardless of mode.
    assert manifest["vault_metadata"][0]["rel"] == \
           "vaswani-2017-attention.extracted.md"


def test_subgraph_export_default_is_bytes_free(
        wiki_a: Path, env_with_ce, tmp_path):
    """Default --include-vault=none ships no vault content but records
    metadata. This is the always-safe public-sharing default."""
    out = tmp_path / "export-default"
    run_script(
        "subgraph_export.py",
        "--project", "ml-foundations",
        "--to", str(out),
        "--workspace", str(wiki_a),
        env=env_with_ce,
    )
    manifest = json.loads((out / "_export-manifest.json").read_text())
    assert manifest["include_vault_mode"] == "none"
    assert manifest["scope_vault"] == []
    # vault metadata still recorded for receiver hydration.
    assert any(e["rel"] == "vaswani-2017-attention.extracted.md"
               for e in manifest["vault_metadata"])


def test_subgraph_export_rejects_destination_inside_workspace(
        wiki_a: Path, env_with_ce):
    res = run_script(
        "subgraph_export.py",
        "--project", "ml-foundations",
        "--to", str(wiki_a / "subdir"),
        "--workspace", str(wiki_a),
        env=env_with_ce, check=False,
    )
    assert res.returncode != 0
    assert "inside workspace" in (res.stderr + res.stdout)


def test_subgraph_export_rejects_path_traversal(wiki_a: Path, env_with_ce):
    res = run_script(
        "subgraph_export.py",
        "--project", "ml-foundations",
        "--to", "../../../tmp/escape",
        "--workspace", str(wiki_a),
        env=env_with_ce, check=False,
    )
    assert res.returncode != 0
    assert ".." in (res.stderr + res.stdout)


# --- merge -----------------------------------------------------------------


def test_merge_stage_dedupes_vault_and_renames_collision(
        wiki_a: Path, wiki_b: Path, env_with_ce):
    run_script(
        "merge.py", str(wiki_b), "--as-origin", "bob",
        "--workspace", str(wiki_a),
        env=env_with_ce,
    )
    staging = wiki_a / ".curator" / ".merge-staging" / "bob"
    assert (staging / "audit-report.md").is_file()
    manifest = json.loads((staging / "apply.json").read_text())

    # Vault dedupe: byte-identical extractions aliased, no copy.
    deduped = [v for v in manifest["vault_files"] if v.get("deduped")]
    new_copies = [v for v in manifest["vault_files"] if not v.get("deduped")]
    assert len(deduped) == 1
    assert len(new_copies) == 0
    assert manifest["vault_alias_map"]["attention-paper.extracted.md"] == \
           "vaswani-2017-attention.extracted.md"

    # Collision: incoming transformer renamed.
    pages = {p["incoming_rel"]: p["final_rel"] for p in manifest["wiki_pages"]}
    assert pages["concepts/transformer.md"] == "concepts/transformer-from-bob.md"
    # Diffusion has no collision, lands at its original path.
    assert pages["concepts/diffusion.md"] == "concepts/diffusion.md"


def test_merge_stage_applies_untrusted_framing(
        wiki_a: Path, wiki_b: Path, env_with_ce):
    run_script(
        "merge.py", str(wiki_b), "--as-origin", "bob",
        "--workspace", str(wiki_a),
        env=env_with_ce,
    )
    staged_page = (
        wiki_a / ".curator" / ".merge-staging" / "bob"
        / "wiki-incoming" / "concepts" / "transformer-from-bob.md"
    )
    text = staged_page.read_text()
    assert "origin: bob" in text
    assert "untrusted: true" in text
    assert "ingest_kind: archival" in text
    assert "BEGIN UNTRUSTED MERGED CONTENT" in text
    assert "END UNTRUSTED MERGED CONTENT" in text
    # Citation rewritten to the canonical receiver vault path.
    assert "(vault:vaswani-2017-attention.extracted.md)" in text
    assert "(vault:attention-paper.extracted.md)" not in text


def test_merge_apply_writes_manifest_and_lands_files(
        wiki_a: Path, wiki_b: Path, env_with_ce):
    run_script("merge.py", str(wiki_b), "--as-origin", "bob",
               "--workspace", str(wiki_a), env=env_with_ce)
    run_script("merge.py", "--apply", "bob",
               "--workspace", str(wiki_a), env=env_with_ce)
    # Live tree updated.
    assert (wiki_a / "wiki" / "concepts" / "transformer-from-bob.md").is_file()
    assert (wiki_a / "wiki" / "concepts" / "diffusion.md").is_file()
    assert (wiki_a / "wiki" / "concepts" / "transformer.md").is_file()  # original kept
    # Manifest persisted for unmerge.
    manifest_path = wiki_a / ".curator" / "merges" / "bob.json"
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text())
    assert manifest["origin"] == "bob"
    assert "applied_at" in manifest
    # Staging directory removed after apply.
    assert not (wiki_a / ".curator" / ".merge-staging" / "bob").exists()


def test_merge_abandon_clears_staging(wiki_a: Path, wiki_b: Path, env_with_ce):
    run_script("merge.py", str(wiki_b), "--as-origin", "bob",
               "--workspace", str(wiki_a), env=env_with_ce)
    assert (wiki_a / ".curator" / ".merge-staging" / "bob").exists()
    run_script("merge.py", "--abandon", "bob",
               "--workspace", str(wiki_a), env=env_with_ce)
    assert not (wiki_a / ".curator" / ".merge-staging" / "bob").exists()
    # No manifest written on abandon.
    assert not (wiki_a / ".curator" / "merges" / "bob.json").exists()


def test_merge_rejects_invalid_origin(wiki_a: Path, wiki_b: Path, env_with_ce):
    res = run_script(
        "merge.py", str(wiki_b), "--as-origin", "Invalid Origin!",
        "--workspace", str(wiki_a),
        env=env_with_ce, check=False,
    )
    assert res.returncode != 0


# --- unmerge ---------------------------------------------------------------


def _post_merge_curation(wiki_a: Path) -> None:
    """User edits a pure import (creates user-modified bucket) and adds
    a native page with wikilinks/citations into the imports.
    """
    diffusion = wiki_a / "wiki" / "concepts" / "diffusion.md"
    diffusion.write_text(diffusion.read_text() +
                          "\n## My notes\n\nUser-added content.\n")
    (wiki_a / "wiki" / "concepts" / "my-genai-notes.md").write_text("""\
---
title: My GenAI Notes
type: note
projects: [ml-foundations]
---

- [[transformer-from-bob]] has bob's framing
- See (vault:vaswani-2017-attention.extracted.md)
""")


def test_unmerge_three_buckets_and_native_annotation(
        wiki_a: Path, wiki_b: Path, env_with_ce):
    run_script("merge.py", str(wiki_b), "--as-origin", "bob",
               "--workspace", str(wiki_a), env=env_with_ce)
    run_script("merge.py", "--apply", "bob",
               "--workspace", str(wiki_a), env=env_with_ce)
    _post_merge_curation(wiki_a)

    run_script("unmerge.py", "--origin", "bob",
               "--workspace", str(wiki_a), env=env_with_ce)
    plan = json.loads(
        (wiki_a / ".curator" / ".unmerge-staging" / "bob" / "plan.json").read_text()
    )
    pure_rels = {e["final_rel"] for e in plan["pure_pages"]}
    modified_rels = {e["final_rel"] for e in plan["modified_pages"]}
    assert "concepts/transformer-from-bob.md" in pure_rels
    assert "concepts/diffusion.md" in modified_rels
    # Native page flagged.
    native_pages = {e["page_rel"] for e in plan["native_edits"]}
    assert "concepts/my-genai-notes.md" in native_pages

    run_script("unmerge.py", "--origin", "bob", "--apply",
               "--workspace", str(wiki_a), env=env_with_ce)

    # Pure imports gone.
    assert not (wiki_a / "wiki" / "concepts" / "transformer-from-bob.md").is_file()
    assert not (wiki_a / "wiki" / "sources" / "attention-paper.md").is_file()
    # User-modified import preserved with user edits intact.
    diffusion_text = (wiki_a / "wiki" / "concepts" / "diffusion.md").read_text()
    assert "User-added content" in diffusion_text
    # Native page annotated; user prose untouched.
    notes_text = (wiki_a / "wiki" / "concepts" / "my-genai-notes.md").read_text()
    assert "[[transformer-from-bob]]" in notes_text  # original wikilink preserved
    assert "<!-- unmerge:" in notes_text
    assert "transformer-from-bob" in notes_text.split("<!-- unmerge:", 1)[1]
    # Manifest archived.
    assert not (wiki_a / ".curator" / "merges" / "bob.json").is_file()
    archive = list((wiki_a / ".curator" / "merges" / ".archive").iterdir())
    assert any("bob-unmerged-" in p.name for p in archive)


# --- accept-bridges --------------------------------------------------------


def test_accept_bridges_writes_links_and_updates_manifest(
        wiki_a: Path, wiki_b: Path, env_with_ce):
    run_script("merge.py", str(wiki_b), "--as-origin", "bob",
               "--workspace", str(wiki_a), env=env_with_ce)
    run_script("merge.py", "--apply", "bob",
               "--workspace", str(wiki_a), env=env_with_ce)

    # Hand-craft a queue with one accepted pair (cross-origin).
    queue = wiki_a / ".curator" / "bridges-test.md"
    queue.write_text("""\
# Bridge candidates

## [x] 1. concepts/attention.md ↔ concepts/transformer-from-bob.md

- **similarity**: 0.82
- **origins**: `native`  ↔  `bob`

## [ ] 2. concepts/transformer.md ↔ concepts/diffusion.md

- **similarity**: 0.61
""")

    run_script("accept_bridges.py", "--queue", str(queue),
               "--workspace", str(wiki_a), env=env_with_ce)

    # Wikilinks added in both directions.
    attn = (wiki_a / "wiki" / "concepts" / "attention.md").read_text()
    tbob = (wiki_a / "wiki" / "concepts" / "transformer-from-bob.md").read_text()
    assert "[[transformer-from-bob]]" in attn
    assert "[[attention]]" in tbob

    # Unaccepted pair was not applied.
    diffusion = (wiki_a / "wiki" / "concepts" / "diffusion.md").read_text()
    assert "[[transformer]]" in diffusion  # already there from fixture
    # No new See also block referencing it.

    # Manifest updated with accepted bridge.
    manifest = json.loads(
        (wiki_a / ".curator" / "merges" / "bob.json").read_text()
    )
    assert manifest["accepted_bridges"], "accepted_bridges should be non-empty"
    flat = {tuple(p) for p in manifest["accepted_bridges"]}
    # Either ordering acceptable.
    assert (("attention", "transformer-from-bob") in flat
            or ("transformer-from-bob", "attention") in flat)


def test_accept_bridges_idempotent(wiki_a: Path, wiki_b: Path, env_with_ce):
    run_script("merge.py", str(wiki_b), "--as-origin", "bob",
               "--workspace", str(wiki_a), env=env_with_ce)
    run_script("merge.py", "--apply", "bob",
               "--workspace", str(wiki_a), env=env_with_ce)
    queue = wiki_a / ".curator" / "bridges-test.md"
    queue.write_text(
        "# q\n\n## [x] 1. concepts/attention.md ↔ concepts/diffusion.md\n\n"
        "- s: 0.7\n"
    )
    run_script("accept_bridges.py", "--queue", str(queue),
               "--workspace", str(wiki_a), env=env_with_ce)
    first = (wiki_a / "wiki" / "concepts" / "attention.md").read_text()
    # Second run should not duplicate the wikilink.
    run_script("accept_bridges.py", "--queue", str(queue),
               "--workspace", str(wiki_a), env=env_with_ce)
    second = (wiki_a / "wiki" / "concepts" / "attention.md").read_text()
    assert first.count("[[diffusion]]") == 1
    assert second.count("[[diffusion]]") == 1


# --- subgraph-export preflight integration -------------------------------


def test_export_excludes_non_native_pages_by_default(
        wiki_a: Path, wiki_b: Path, env_with_ce, tmp_path):
    run_script("merge.py", str(wiki_b), "--as-origin", "bob",
               "--workspace", str(wiki_a), env=env_with_ce)
    run_script("merge.py", "--apply", "bob",
               "--workspace", str(wiki_a), env=env_with_ce)
    out = tmp_path / "exp"
    run_script(
        "subgraph_export.py",
        "--project", "ml-foundations",
        "--to", str(out),
        "--workspace", str(wiki_a),
        env=env_with_ce,
    )
    manifest = json.loads((out / "_export-manifest.json").read_text())
    for rel in manifest["scope_pages"]:
        text = (out / "wiki" / rel).read_text()
        assert "origin: bob" not in text


def test_export_with_include_non_native_keeps_origin_pages(
        wiki_a: Path, wiki_b: Path, env_with_ce, tmp_path):
    run_script("merge.py", str(wiki_b), "--as-origin", "bob",
               "--workspace", str(wiki_a), env=env_with_ce)
    run_script("merge.py", "--apply", "bob",
               "--workspace", str(wiki_a), env=env_with_ce)
    out = tmp_path / "exp-bob"
    run_script(
        "subgraph_export.py",
        "--project", "generative-models",
        "--to", str(out),
        "--include-non-native",
        "--yes",
        "--workspace", str(wiki_a),
        env=env_with_ce,
    )
    manifest = json.loads((out / "_export-manifest.json").read_text())
    assert manifest["scope_pages"]


def test_export_strips_url_query_by_default(env_with_ce, tmp_path: Path):
    ws = tmp_path / "ws"
    (ws / "wiki" / "concepts").mkdir(parents=True)
    (ws / "wiki" / "projects").mkdir(parents=True)
    (ws / "vault").mkdir(parents=True)
    (ws / ".curator").mkdir(parents=True)
    (ws / "wiki" / "projects" / "p.md").write_text(
        "---\ntitle: P\ntype: project\n---\nhome\n"
    )
    (ws / "wiki" / "concepts" / "c.md").write_text(
        "---\ntitle: C\ntype: concept\nprojects: [p]\n---\n(vault:s.md)\n"
    )
    (ws / "vault" / "s.md").write_text(
        "---\ntitle: S\nsource_url: https://example.com/p?session=abc&utm_source=tw\n"
        "license: CC-BY-4.0\n---\nbody\n"
    )
    out = tmp_path / "exp-redact"
    run_script(
        "subgraph_export.py",
        "--project", "p", "--to", str(out),
        "--workspace", str(ws),
        env=env_with_ce,
    )
    manifest = json.loads((out / "_export-manifest.json").read_text())
    urls = [m.get("source_url", "") for m in manifest["vault_metadata"]]
    assert "https://example.com/p" in urls
    assert all("session=" not in u for u in urls)


def test_export_keep_url_params_preserves_everything(
        env_with_ce, tmp_path: Path):
    ws = tmp_path / "ws-keep"
    (ws / "wiki" / "concepts").mkdir(parents=True)
    (ws / "wiki" / "projects").mkdir(parents=True)
    (ws / "vault").mkdir(parents=True)
    (ws / ".curator").mkdir(parents=True)
    (ws / "wiki" / "projects" / "p.md").write_text(
        "---\ntitle: P\ntype: project\n---\n"
    )
    (ws / "wiki" / "concepts" / "c.md").write_text(
        "---\ntitle: C\ntype: concept\nprojects: [p]\n---\n(vault:s.md)\n"
    )
    (ws / "vault" / "s.md").write_text(
        "---\ntitle: S\nsource_url: https://example.com/p?token=abc\n"
        "license: CC-BY-4.0\n---\n"
    )
    out = tmp_path / "exp-keep"
    run_script(
        "subgraph_export.py",
        "--project", "p", "--to", str(out),
        "--keep-url-params",
        "--workspace", str(ws),
        env=env_with_ce,
    )
    manifest = json.loads((out / "_export-manifest.json").read_text())
    urls = [m.get("source_url", "") for m in manifest["vault_metadata"]]
    assert any("token=abc" in u for u in urls)


def test_export_strict_refuses_when_findings_present(
        env_with_ce, tmp_path: Path):
    ws = tmp_path / "ws-strict"
    (ws / "wiki" / "concepts").mkdir(parents=True)
    (ws / "wiki" / "projects").mkdir(parents=True)
    (ws / "vault").mkdir(parents=True)
    (ws / ".curator").mkdir(parents=True)
    (ws / "wiki" / "projects" / "p.md").write_text(
        "---\ntitle: P\ntype: project\n---\n"
    )
    (ws / "wiki" / "concepts" / "c.md").write_text(
        "---\ntitle: C\ntype: concept\nprojects: [p]\n---\n"
        + "> heavy quote line for fair use review\n" * 30
    )
    out = tmp_path / "exp-strict"
    res = run_script(
        "subgraph_export.py",
        "--project", "p", "--to", str(out),
        "--strict",
        "--workspace", str(ws),
        env=env_with_ce, check=False,
    )
    assert res.returncode != 0
    assert "preflight" in (res.stderr + res.stdout).lower()


def test_export_no_preflight_skips_checks(env_with_ce, tmp_path: Path):
    ws = tmp_path / "ws-skip"
    (ws / "wiki" / "concepts").mkdir(parents=True)
    (ws / "wiki" / "projects").mkdir(parents=True)
    (ws / "vault").mkdir(parents=True)
    (ws / ".curator").mkdir(parents=True)
    (ws / "wiki" / "projects" / "p.md").write_text(
        "---\ntitle: P\ntype: project\n---\n"
    )
    (ws / "wiki" / "concepts" / "c.md").write_text(
        "---\ntitle: C\ntype: concept\nprojects: [p]\n---\n"
        + "> heavy quote\n" * 30
    )
    out = tmp_path / "exp-skip"
    run_script(
        "subgraph_export.py",
        "--project", "p", "--to", str(out),
        "--no-preflight",
        "--workspace", str(ws),
        env=env_with_ce,
    )
    manifest = json.loads((out / "_export-manifest.json").read_text())
    # v0.2.1: manifest defaults to summary-only. With --no-preflight no
    # detectors ran, so the summary is empty.
    assert manifest["preflight_summary"] == []


def test_export_yes_in_noninteractive_proceeds_with_findings(
        env_with_ce, tmp_path: Path):
    ws = tmp_path / "ws-yes"
    (ws / "wiki" / "concepts").mkdir(parents=True)
    (ws / "wiki" / "projects").mkdir(parents=True)
    (ws / "vault").mkdir(parents=True)
    (ws / ".curator").mkdir(parents=True)
    (ws / "wiki" / "projects" / "p.md").write_text(
        "---\ntitle: P\ntype: project\n---\n"
    )
    (ws / "wiki" / "concepts" / "c.md").write_text(
        "---\ntitle: C\ntype: concept\nprojects: [p]\n---\n"
        + "> heavy\n" * 30
    )
    out = tmp_path / "exp-yes"
    run_script(
        "subgraph_export.py",
        "--project", "p", "--to", str(out),
        "--yes",
        "--workspace", str(ws),
        env=env_with_ce,
    )
    manifest = json.loads((out / "_export-manifest.json").read_text())
    # v0.2.1: default manifest is summary-only. Findings recorded as counts.
    summary = manifest["preflight_summary"]
    assert summary
    assert any(s["kind"] == "quote_density" for s in summary)
    # Default mode does NOT include per-finding records.
    assert "preflight_findings" not in manifest


def test_export_noninteractive_without_yes_refuses_on_findings(
        env_with_ce, tmp_path: Path):
    ws = tmp_path / "ws-refuse"
    (ws / "wiki" / "concepts").mkdir(parents=True)
    (ws / "wiki" / "projects").mkdir(parents=True)
    (ws / "vault").mkdir(parents=True)
    (ws / ".curator").mkdir(parents=True)
    (ws / "wiki" / "projects" / "p.md").write_text(
        "---\ntitle: P\ntype: project\n---\n"
    )
    (ws / "wiki" / "concepts" / "c.md").write_text(
        "---\ntitle: C\ntype: concept\nprojects: [p]\n---\n"
        + "> q\n" * 30
    )
    out = tmp_path / "exp-refuse"
    res = run_script(
        "subgraph_export.py",
        "--project", "p", "--to", str(out),
        "--workspace", str(ws),
        env=env_with_ce, check=False,
    )
    assert res.returncode != 0


# --- manifest must never leak PII (v0.2.1 regression test) ---------------


# Patterns that would prove a leak. We check the published manifest
# *bytes* (json text) for any of these, not just structural fields,
# because rationale strings or paths could embed them anywhere.
_PII_FORBIDDEN_PATTERNS = [
    # Real-shaped emails (after the reserved-test filter would have run).
    re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"),
    # SSN and IBAN shapes.
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b"),
]


def _build_pii_fixture(ws: Path) -> None:
    (ws / "wiki" / "concepts").mkdir(parents=True)
    (ws / "wiki" / "projects").mkdir(parents=True)
    (ws / "vault").mkdir(parents=True)
    (ws / ".curator").mkdir(parents=True)
    (ws / "wiki" / "projects" / "p.md").write_text(
        "---\ntitle: P\ntype: project\n---\nhome\n"
    )
    # Page with real-shaped email + SSN + IBAN baked into its body.
    # If the manifest emits these anywhere, the test fails.
    (ws / "wiki" / "concepts" / "leaky.md").write_text(
        "---\ntitle: Leaky\ntype: concept\nprojects: [p]\n---\n"
        "Contact alice@somecompany.com or +1 555-0142. "
        "SSN 987-65-4321. IBAN DE89370400440532013000.\n"
    )


def test_manifest_never_contains_pii_in_default_mode(
        env_with_ce, tmp_path: Path):
    """v0.2.0 leaked matched samples into manifest rationale; v0.2.1
    defaults to summary-only output. This test would have caught the
    v0.2.0 regression and locks the new behaviour in."""
    ws = tmp_path / "ws-pii-default"
    _build_pii_fixture(ws)
    out = tmp_path / "exp-pii-default"
    run_script(
        "subgraph_export.py",
        "--project", "p", "--to", str(out),
        "--yes",  # bypass the prompt; the test isn't about UX
        "--workspace", str(ws),
        env=env_with_ce,
    )
    text = (out / "_export-manifest.json").read_text()
    for pat in _PII_FORBIDDEN_PATTERNS:
        m = pat.search(text)
        assert m is None, (
            f"manifest leaked PII: pattern {pat.pattern!r} "
            f"matched {m.group(0)!r}\n"
            f"manifest content:\n{text}"
        )


def test_manifest_never_contains_pii_with_findings_included(
        env_with_ce, tmp_path: Path):
    """Even with --include-preflight-in-manifest, the per-finding records
    are stripped of `samples` — they may include subjects (file paths)
    and rationales but NOT raw matched values."""
    ws = tmp_path / "ws-pii-included"
    _build_pii_fixture(ws)
    out = tmp_path / "exp-pii-included"
    run_script(
        "subgraph_export.py",
        "--project", "p", "--to", str(out),
        "--yes",
        "--include-preflight-in-manifest",
        "--workspace", str(ws),
        env=env_with_ce,
    )
    manifest = json.loads((out / "_export-manifest.json").read_text())
    # Per-finding records present — and PII-free.
    assert manifest.get("preflight_findings"), \
        "expected findings list when --include-preflight-in-manifest set"
    for f in manifest["preflight_findings"]:
        assert "samples" not in f, "samples must never reach the manifest"
    text = (out / "_export-manifest.json").read_text()
    for pat in _PII_FORBIDDEN_PATTERNS:
        m = pat.search(text)
        assert m is None, (
            f"manifest leaked PII: pattern {pat.pattern!r} "
            f"matched {m.group(0)!r}\n"
            f"manifest content:\n{text}"
        )


def test_manifest_summary_records_pii_finding_count(
        env_with_ce, tmp_path: Path):
    """Even though raw values don't leak, the count is published — the
    receiver sees `gdpr_likely_pii: 1` so they know to look locally."""
    ws = tmp_path / "ws-pii-count"
    _build_pii_fixture(ws)
    out = tmp_path / "exp-pii-count"
    run_script(
        "subgraph_export.py",
        "--project", "p", "--to", str(out),
        "--yes",
        "--workspace", str(ws),
        env=env_with_ce,
    )
    manifest = json.loads((out / "_export-manifest.json").read_text())
    summary = manifest["preflight_summary"]
    pii_kinds = {s["kind"] for s in summary}
    assert "gdpr_likely_pii" in pii_kinds


# --- subgraph-export vault-sharing modes ----------------------------------


def test_subgraph_export_default_omits_all_vault_records_metadata(
        wiki_a_with_paywalled: Path, env_with_ce, tmp_path):
    out = tmp_path / "exp-none"
    run_script(
        "subgraph_export.py",
        "--project", "mixed",
        "--to", str(out),
        "--workspace", str(wiki_a_with_paywalled),
        env=env_with_ce,
    )
    manifest = json.loads((out / "_export-manifest.json").read_text())
    assert manifest["include_vault_mode"] == "none"
    # Bytes excluded.
    assert manifest["scope_vault"] == []
    assert not (out / "vault").exists() or not list((out / "vault").iterdir())
    # Metadata recorded for every cited file.
    rels = {e["rel"] for e in manifest["vault_metadata"]}
    assert rels == {
        "arxiv-paper.extracted.md",
        "nature-paper.extracted.md",
        "openblog.extracted.md",
    }
    # Redistributability assessed.
    by_rel = {e["rel"]: e for e in manifest["vault_metadata"]}
    assert by_rel["arxiv-paper.extracted.md"]["redistributable"] is True
    assert by_rel["openblog.extracted.md"]["redistributable"] is True
    assert by_rel["nature-paper.extracted.md"]["redistributable"] is False


def test_subgraph_export_owned_includes_only_redistributable(
        wiki_a_with_paywalled: Path, env_with_ce, tmp_path):
    out = tmp_path / "exp-owned"
    run_script(
        "subgraph_export.py",
        "--project", "mixed",
        "--to", str(out),
        "--include-vault", "owned",
        "--workspace", str(wiki_a_with_paywalled),
        env=env_with_ce,
    )
    bundled = sorted(p.name for p in (out / "vault").iterdir())
    assert "arxiv-paper.extracted.md" in bundled
    assert "openblog.extracted.md" in bundled
    assert "nature-paper.extracted.md" not in bundled
    manifest = json.loads((out / "_export-manifest.json").read_text())
    assert manifest["include_vault_mode"] == "owned"


def test_subgraph_export_all_includes_everything(
        wiki_a_with_paywalled: Path, env_with_ce, tmp_path):
    out = tmp_path / "exp-all"
    run_script(
        "subgraph_export.py",
        "--project", "mixed",
        "--to", str(out),
        "--include-vault", "all",
        "--workspace", str(wiki_a_with_paywalled),
        env=env_with_ce,
    )
    bundled = sorted(p.name for p in (out / "vault").iterdir())
    assert len(bundled) == 3


# --- merge marks vault_missing -------------------------------------------


def test_merge_marks_vault_missing_for_omitted_sources(
        wiki_a_with_paywalled: Path, wiki_a: Path,
        env_with_ce, tmp_path):
    """Export wiki_a_with_paywalled with --include-vault=none, then merge
    that export into a fresh receiving wiki. Source stubs should land
    with vault_missing: true and source_url propagated.
    """
    export = tmp_path / "shared"
    run_script(
        "subgraph_export.py",
        "--project", "mixed",
        "--to", str(export),
        "--workspace", str(wiki_a_with_paywalled),
        env=env_with_ce,
    )
    # Use wiki_a as receiver (it has its own unrelated content).
    run_script(
        "merge.py", str(export), "--as-origin", "shared",
        "--workspace", str(wiki_a),
        env=env_with_ce,
    )
    staging = wiki_a / ".curator" / ".merge-staging" / "shared"
    manifest = json.loads((staging / "apply.json").read_text())
    missing_pages = {m["page_rel"] for m in manifest["missing_vault"]}
    # All three source stubs were tagged because vault content was omitted.
    assert any("arxiv-paper" in p for p in missing_pages)
    assert any("nature-paper" in p for p in missing_pages)
    assert any("openblog" in p for p in missing_pages)
    # Inspect a staged source stub directly.
    arxiv_stub = (staging / "wiki-incoming" / "sources" / "arxiv-paper.md").read_text()
    assert "vault_missing: true" in arxiv_stub
    assert "arxiv.org" in arxiv_stub  # source_url propagated


# --- hydrate-vault categorization ----------------------------------------


def test_hydrate_vault_dry_run_categorizes_correctly(
        wiki_a_with_paywalled: Path, wiki_a: Path,
        env_with_ce, tmp_path):
    export = tmp_path / "shared2"
    run_script(
        "subgraph_export.py",
        "--project", "mixed",
        "--to", str(export),
        "--workspace", str(wiki_a_with_paywalled),
        env=env_with_ce,
    )
    run_script(
        "merge.py", str(export), "--as-origin", "shared",
        "--workspace", str(wiki_a),
        env=env_with_ce,
    )
    run_script(
        "merge.py", "--apply", "shared",
        "--workspace", str(wiki_a),
        env=env_with_ce,
    )
    res = run_script(
        "hydrate_vault.py",
        "--workspace", str(wiki_a),
        "--origin", "shared",
        env=env_with_ce,
    )
    out = res.stdout
    # Each category surfaces with the right count.
    assert "arxiv: 1" in out
    assert "paywalled: 1" in out  # nature-paper
    # openblog has CC-BY → open_access
    assert "open_access: 1" in out
    # Default is dry-run.
    assert "dry run" in out


def test_hydrate_vault_no_missing_returns_clean(
        wiki_a: Path, env_with_ce):
    res = run_script(
        "hydrate_vault.py",
        "--workspace", str(wiki_a),
        env=env_with_ce,
    )
    assert "no vault_missing stubs" in res.stdout


# --- merge runs preflight on incoming content (v0.2.1) -------------------


def test_merge_stage_runs_preflight_on_incoming(
        wiki_a: Path, env_with_ce, tmp_path: Path):
    """A merge source whose pages contain real-shaped emails should
    surface a gdpr_likely_pii finding in the merge audit, with samples
    stripped from manifest + audit."""
    src = tmp_path / "leaky-src"
    (src / "wiki" / "concepts").mkdir(parents=True)
    (src / "wiki" / "projects").mkdir(parents=True)
    (src / "vault").mkdir(parents=True)
    (src / "wiki" / "projects" / "leaky-proj.md").write_text(
        "---\ntitle: Leaky\ntype: project\n---\nhome\n"
    )
    (src / "wiki" / "concepts" / "leaky-page.md").write_text(
        "---\ntitle: Leak\ntype: concept\nprojects: [leaky-proj]\n---\n"
        "Author email: alice@somecompany.com. SSN 987-65-4321.\n"
    )
    run_script(
        "merge.py", str(src), "--as-origin", "ext",
        "--workspace", str(wiki_a),
        env=env_with_ce,
    )
    staging = wiki_a / ".curator" / ".merge-staging" / "ext"
    manifest = json.loads((staging / "apply.json").read_text())
    # Findings recorded; samples stripped.
    summary = manifest["preflight_summary"]
    assert any(s["kind"] == "gdpr_likely_pii" for s in summary)
    for f in manifest.get("preflight_findings", []):
        assert "samples" not in f
    # Audit report must not leak the email or SSN.
    audit = (staging / "audit-report.md").read_text()
    assert "alice@somecompany.com" not in audit
    assert "987-65-4321" not in audit
    # But audit explicitly mentions the kind.
    assert "gdpr_likely_pii" in audit


def test_merge_stage_preflight_does_not_block_apply(
        wiki_a: Path, env_with_ce, tmp_path: Path):
    """Pre-flight at merge stage is informational, not gating."""
    src = tmp_path / "src-info"
    (src / "wiki" / "concepts").mkdir(parents=True)
    (src / "wiki" / "projects").mkdir(parents=True)
    (src / "vault").mkdir(parents=True)
    (src / "wiki" / "projects" / "info-proj.md").write_text(
        "---\ntitle: P\ntype: project\n---\n"
    )
    (src / "wiki" / "concepts" / "p.md").write_text(
        "---\ntitle: C\ntype: concept\nprojects: [info-proj]\n---\n"
        "Contact +1 555-0142.\n"
    )
    run_script("merge.py", str(src), "--as-origin", "info",
               "--workspace", str(wiki_a), env=env_with_ce)
    # Apply should succeed despite the finding.
    run_script("merge.py", "--apply", "info",
               "--workspace", str(wiki_a), env=env_with_ce)
    assert (wiki_a / ".curator" / "merges" / "info.json").is_file()


# --- license allowlist (v0.2.1 tightening) --------------------------------


def test_export_owned_excludes_cc_by_nc_by_default(
        env_with_ce, tmp_path: Path):
    """v0.2.1: CC-BY-NC removed from default --include-vault=owned set."""
    ws = tmp_path / "ws-nc"
    (ws / "wiki" / "concepts").mkdir(parents=True)
    (ws / "wiki" / "projects").mkdir(parents=True)
    (ws / "vault").mkdir(parents=True)
    (ws / ".curator").mkdir(parents=True)
    (ws / "wiki" / "projects" / "p.md").write_text(
        "---\ntitle: P\ntype: project\n---\n"
    )
    (ws / "wiki" / "concepts" / "c.md").write_text(
        "---\ntitle: C\ntype: concept\nprojects: [p]\n---\n(vault:nc.md)\n"
    )
    (ws / "vault" / "nc.md").write_text(
        "---\ntitle: NC\nlicense: CC-BY-NC-4.0\n"
        "source_url: https://example.org/p\n---\nbody\n"
    )
    out = tmp_path / "exp-nc-default"
    run_script(
        "subgraph_export.py",
        "--project", "p", "--to", str(out),
        "--include-vault", "owned",
        "--workspace", str(ws),
        env=env_with_ce,
    )
    assert not (out / "vault").exists() or not list((out / "vault").iterdir())


def test_export_owned_with_allow_nc_includes_cc_by_nc(
        env_with_ce, tmp_path: Path):
    """The opt-in flag re-includes CC-BY-NC for users whose use case
    is genuinely non-commercial."""
    ws = tmp_path / "ws-allow-nc"
    (ws / "wiki" / "concepts").mkdir(parents=True)
    (ws / "wiki" / "projects").mkdir(parents=True)
    (ws / "vault").mkdir(parents=True)
    (ws / ".curator").mkdir(parents=True)
    (ws / "wiki" / "projects" / "p.md").write_text(
        "---\ntitle: P\ntype: project\n---\n"
    )
    (ws / "wiki" / "concepts" / "c.md").write_text(
        "---\ntitle: C\ntype: concept\nprojects: [p]\n---\n(vault:nc.md)\n"
    )
    (ws / "vault" / "nc.md").write_text(
        "---\ntitle: NC\nlicense: CC-BY-NC-4.0\n"
        "source_url: https://example.org/p\n---\nbody\n"
    )
    out = tmp_path / "exp-allow-nc"
    run_script(
        "subgraph_export.py",
        "--project", "p", "--to", str(out),
        "--include-vault", "owned",
        "--allow-license-class", "nc",
        "--workspace", str(ws),
        env=env_with_ce,
    )
    bundled = sorted(p.name for p in (out / "vault").iterdir())
    assert bundled == ["nc.md"]


# --- merge --rerun-gates --------------------------------------------------


def test_rerun_gates_refreshes_audit_without_redoing_reconciliation(
        wiki_a: Path, wiki_b: Path, env_with_ce):
    run_script("merge.py", str(wiki_b), "--as-origin", "bob",
               "--workspace", str(wiki_a), env=env_with_ce)
    staging = wiki_a / ".curator" / ".merge-staging" / "bob"
    audit_before = (staging / "audit-report.md").read_text()
    manifest_before = json.loads((staging / "apply.json").read_text())

    run_script("merge.py", "--rerun-gates", "bob",
               "--workspace", str(wiki_a), env=env_with_ce)
    audit_after = (staging / "audit-report.md").read_text()
    manifest_after = json.loads((staging / "apply.json").read_text())

    # Reconciliation preserved.
    assert manifest_after["wiki_pages"] == manifest_before["wiki_pages"]
    assert manifest_after["vault_files"] == manifest_before["vault_files"]
    assert manifest_after["page_collisions"] == manifest_before["page_collisions"]
    # Re-run timestamp recorded.
    assert "regated_at" in manifest_after
    # Audit report still well-formed (presence of major sections).
    for section in ("# Merge audit report",
                    "## Vault reconciliation",
                    "## Page-name collisions",
                    "## Quarantined"):
        assert section in audit_after
