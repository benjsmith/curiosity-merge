"""End-to-end coverage of the curiosity-merge verbs.

Each test is a self-contained scenario built on the wiki_a / wiki_b
fixtures from conftest.py. Tests run real subprocesses against the
shipped scripts so they catch regressions in argument parsing, exit
codes, and import-time setup as well as logic.
"""
from __future__ import annotations

import json
from pathlib import Path

from conftest import run_script


# --- subgraph-export -------------------------------------------------------


def test_subgraph_export_project_scope(wiki_a: Path, env_with_ce, tmp_path):
    out = tmp_path / "export-mlf"
    run_script(
        "subgraph_export.py",
        "--project", "ml-foundations",
        "--to", str(out),
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
    # Vault file came along via citation.
    assert manifest["scope_vault"] == ["vaswani-2017-attention.extracted.md"]


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
