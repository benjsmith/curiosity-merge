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


# --- standalone preflight CLI (v0.4.0) -----------------------------------


def test_preflight_cli_clean_workspace_exits_zero(env_with_ce, tmp_path: Path):
    ws = tmp_path / "ws-clean"
    (ws / "wiki" / "concepts").mkdir(parents=True)
    (ws / "wiki" / "projects").mkdir(parents=True)
    (ws / "vault").mkdir(parents=True)
    (ws / ".curator").mkdir(parents=True)
    (ws / "wiki" / "projects" / "p.md").write_text(
        "---\ntitle: P\ntype: project\n---\n"
    )
    (ws / "wiki" / "concepts" / "c.md").write_text(
        "---\ntitle: C\ntype: concept\nprojects: [p]\n---\n"
        "Pure prose with no PII or quotes.\n"
    )
    res = run_script(
        "preflight.py", "--workspace", str(ws),
        env=env_with_ce,
    )
    assert res.returncode == 0
    assert "no issues" in res.stdout


def test_preflight_cli_findings_exit_one(env_with_ce, tmp_path: Path):
    ws = tmp_path / "ws-find"
    _build_quote_density_only_fixture(ws)
    res = run_script(
        "preflight.py", "--workspace", str(ws),
        env=env_with_ce, check=False,
    )
    assert res.returncode == 1
    assert "quote_density" in res.stdout


def test_preflight_cli_no_wiki_exits_two(env_with_ce, tmp_path: Path):
    res = run_script(
        "preflight.py", "--workspace", str(tmp_path / "nope"),
        env=env_with_ce, check=False,
    )
    assert res.returncode == 2
    assert "no wiki" in (res.stderr + res.stdout)


def test_preflight_cli_json_output_strips_samples(
        env_with_ce, tmp_path: Path):
    ws = tmp_path / "ws-json"
    (ws / "wiki" / "concepts").mkdir(parents=True)
    (ws / "wiki" / "projects").mkdir(parents=True)
    (ws / "vault").mkdir(parents=True)
    (ws / ".curator").mkdir(parents=True)
    (ws / "wiki" / "projects" / "p.md").write_text(
        "---\ntitle: P\ntype: project\n---\n"
    )
    (ws / "wiki" / "concepts" / "c.md").write_text(
        "---\ntitle: C\ntype: concept\nprojects: [p]\n---\n"
        "Author email: alice@somecompany.com.\n"
    )
    res = run_script(
        "preflight.py", "--workspace", str(ws), "--json",
        env=env_with_ce, check=False,
    )
    payload = json.loads(res.stdout)
    assert isinstance(payload, list)
    # No sample values in any finding.
    assert "alice@somecompany.com" not in res.stdout
    for f in payload:
        assert "samples" not in f


def test_preflight_cli_scope_restricts_to_specific_files(
        env_with_ce, tmp_path: Path):
    """--scope should limit analysis to listed files, ignoring others."""
    ws = tmp_path / "ws-scope"
    (ws / "wiki" / "concepts").mkdir(parents=True)
    (ws / "wiki" / "projects").mkdir(parents=True)
    (ws / "vault").mkdir(parents=True)
    (ws / ".curator").mkdir(parents=True)
    (ws / "wiki" / "projects" / "p.md").write_text(
        "---\ntitle: P\ntype: project\n---\n"
    )
    clean = ws / "wiki" / "concepts" / "clean.md"
    clean.write_text(
        "---\ntitle: clean\ntype: concept\nprojects: [p]\n---\nNo issues.\n"
    )
    dirty = ws / "wiki" / "concepts" / "dirty.md"
    dirty.write_text(
        "---\ntitle: dirty\ntype: concept\nprojects: [p]\n---\n"
        "(vault:src.md)\n\n"
        + "> heavy quote\n" * 30
    )
    (ws / "vault" / "src.md").write_text(
        "---\ntitle: x\nsource_url: https://arxiv.org/x\n---\n"
    )
    # Scope only `clean` → no findings.
    res = run_script(
        "preflight.py", "--workspace", str(ws),
        "--scope", str(clean),
        env=env_with_ce,
    )
    assert res.returncode == 0
    # Scope only `dirty` → quote_density fires.
    res2 = run_script(
        "preflight.py", "--workspace", str(ws),
        "--scope", str(dirty),
        env=env_with_ce, check=False,
    )
    assert res2.returncode == 1


def test_preflight_cli_show_acks_empty(env_with_ce, tmp_path: Path):
    ws = tmp_path / "ws"
    _build_quote_density_only_fixture(ws)
    res = run_script(
        "preflight.py", "--workspace", str(ws), "--show-acks",
        env=env_with_ce,
    )
    assert "no acks" in res.stdout


def test_preflight_cli_does_not_write_cache(env_with_ce, tmp_path: Path):
    """Read-only audit: scanning a workspace must not create the
    Presidio cache directory or the ack file."""
    ws = tmp_path / "ws-readonly"
    _build_quote_density_only_fixture(ws)
    cache_dir = ws / ".curator" / ".preflight-cache"
    ack_file = ws / ".curator" / "preflight-acks.json"
    run_script(
        "preflight.py", "--workspace", str(ws),
        env=env_with_ce, check=False,
    )
    assert not cache_dir.exists()
    assert not ack_file.exists()


# --- persistent finding acks (v0.4.0) ------------------------------------


def test_remember_acks_persists_and_suppresses_next_run(
        env_with_ce, tmp_path: Path):
    """First run with --accept-on=quote_density --remember-acks
    persists the ack. Second run without --accept-on auto-suppresses
    the same finding."""
    ws = tmp_path / "ws"
    _build_quote_density_only_fixture(ws)
    out1 = tmp_path / "out1"
    res1 = run_script(
        "subgraph_export.py",
        "--project", "p", "--to", str(out1),
        "--accept-on", "quote_density",
        "--remember-acks",
        "--workspace", str(ws),
        env=env_with_ce,
    )
    assert "persisted" in res1.stderr
    ack_file = ws / ".curator" / "preflight-acks.json"
    assert ack_file.is_file()
    acks = json.loads(ack_file.read_text())
    assert len(acks["acks"]) >= 1
    # Second run: no --accept-on, but ack should suppress.
    out2 = tmp_path / "out2"
    res2 = run_script(
        "subgraph_export.py",
        "--project", "p", "--to", str(out2),
        "--workspace", str(ws),
        env=env_with_ce,
    )
    assert "suppressed by previous acks" in res2.stderr
    assert (out2 / "_export-manifest.json").is_file()


def test_acks_invalidated_by_file_content_change(
        env_with_ce, tmp_path: Path):
    """After acking a finding, edit the underlying wiki page → ack
    invalidates → next run sees the finding again."""
    ws = tmp_path / "ws"
    _build_quote_density_only_fixture(ws)
    out1 = tmp_path / "out1"
    run_script(
        "subgraph_export.py",
        "--project", "p", "--to", str(out1),
        "--accept-on", "quote_density",
        "--remember-acks",
        "--workspace", str(ws),
        env=env_with_ce,
    )
    # Edit the file → sha256 changes → ack invalidated.
    page = ws / "wiki" / "concepts" / "c.md"
    page.write_text(page.read_text() + "\n\n## extra heading\n")

    out2 = tmp_path / "out2"
    res2 = run_script(
        "subgraph_export.py",
        "--project", "p", "--to", str(out2),
        "--workspace", str(ws),
        env=env_with_ce, check=False,
    )
    # No --accept-on, no --remember-acks; finding now live but not
    # interactive (no TTY) → should refuse via "needs decision" path.
    assert res2.returncode != 0
    assert "need decision" in (res2.stderr + res2.stdout)


def test_acks_never_contain_samples_on_disk(
        env_with_ce, tmp_path: Path):
    """Build a fixture with PII; ack it; assert the persisted ack file
    contains zero @ characters anywhere."""
    ws = tmp_path / "ws-pii"
    (ws / "wiki" / "concepts").mkdir(parents=True)
    (ws / "wiki" / "projects").mkdir(parents=True)
    (ws / "vault").mkdir(parents=True)
    (ws / ".curator").mkdir(parents=True)
    (ws / "wiki" / "projects" / "p.md").write_text(
        "---\ntitle: P\ntype: project\n---\n"
    )
    (ws / "wiki" / "concepts" / "c.md").write_text(
        "---\ntitle: C\ntype: concept\nprojects: [p]\n---\n"
        "Author email: alice@somecompany.com. SSN 987-65-4321.\n"
    )
    out = tmp_path / "out"
    run_script(
        "subgraph_export.py",
        "--project", "p", "--to", str(out),
        "--accept-on", "all",
        "--remember-acks",
        "--workspace", str(ws),
        env=env_with_ce,
    )
    ack_file = ws / ".curator" / "preflight-acks.json"
    assert ack_file.is_file()
    text = ack_file.read_text()
    assert "@" not in text
    assert "987-65-4321" not in text


def test_list_acks_command(env_with_ce, tmp_path: Path):
    ws = tmp_path / "ws"
    _build_quote_density_only_fixture(ws)
    # First, populate acks.
    run_script(
        "subgraph_export.py",
        "--project", "p", "--to", str(tmp_path / "out"),
        "--accept-on", "all",
        "--remember-acks",
        "--workspace", str(ws),
        env=env_with_ce,
    )
    # Now list.
    res = run_script(
        "subgraph_export.py",
        "--list-acks",
        "--workspace", str(ws),
        env=env_with_ce,
    )
    assert "ack(s)" in res.stdout
    assert "ack_id" in res.stdout


def test_list_acks_empty(env_with_ce, tmp_path: Path):
    ws = tmp_path / "ws"
    _build_quote_density_only_fixture(ws)
    res = run_script(
        "subgraph_export.py",
        "--list-acks",
        "--workspace", str(ws),
        env=env_with_ce,
    )
    assert "no acks" in res.stdout


def test_clear_acks_with_auto_yes(env_with_ce, tmp_path: Path):
    ws = tmp_path / "ws"
    _build_quote_density_only_fixture(ws)
    run_script(
        "subgraph_export.py",
        "--project", "p", "--to", str(tmp_path / "out"),
        "--accept-on", "all",
        "--remember-acks",
        "--workspace", str(ws),
        env=env_with_ce,
    )
    ack_file = ws / ".curator" / "preflight-acks.json"
    assert ack_file.is_file()
    res = run_script(
        "subgraph_export.py",
        "--clear-acks",
        "--accept-on", "all",  # auto-confirm via accept-on=all
        "--workspace", str(ws),
        env=env_with_ce,
    )
    assert "cleared" in res.stdout
    assert not ack_file.exists()


# --- per-detector gating flags (v0.4.0) ----------------------------------


def _build_quote_density_only_fixture(ws: Path) -> None:
    """Wiki where the only finding is quote_density (no PII, no GPL).
    Lets us test per-kind gating without other findings interfering."""
    (ws / "wiki" / "concepts").mkdir(parents=True)
    (ws / "wiki" / "projects").mkdir(parents=True)
    (ws / "vault").mkdir(parents=True)
    (ws / ".curator").mkdir(parents=True)
    (ws / "wiki" / "projects" / "p.md").write_text(
        "---\ntitle: P\ntype: project\n---\n"
    )
    (ws / "wiki" / "concepts" / "c.md").write_text(
        "---\ntitle: C\ntype: concept\nprojects: [p]\n---\n\n"
        "(vault:src.extracted.md)\n\n"
        + "> heavy block quote line for fair use review\n" * 30
    )
    (ws / "vault" / "src.extracted.md").write_text(
        "---\ntitle: Source\nsource_url: https://arxiv.org/abs/x\n---\n"
        "Body.\n"
    )


def test_refuse_on_specific_kind_blocks(env_with_ce, tmp_path: Path):
    ws = tmp_path / "ws"
    _build_quote_density_only_fixture(ws)
    res = run_script(
        "subgraph_export.py",
        "--project", "p", "--to", str(tmp_path / "out"),
        "--refuse-on", "quote_density",
        "--workspace", str(ws),
        env=env_with_ce, check=False,
    )
    assert res.returncode != 0
    assert "refused by policy" in (res.stderr + res.stdout)


def test_accept_on_specific_kind_proceeds(env_with_ce, tmp_path: Path):
    ws = tmp_path / "ws-accept"
    _build_quote_density_only_fixture(ws)
    out = tmp_path / "out-accept"
    res = run_script(
        "subgraph_export.py",
        "--project", "p", "--to", str(out),
        "--accept-on", "quote_density",
        "--workspace", str(ws),
        env=env_with_ce,
    )
    # Proceeded successfully; auto-acceptance reported in stderr.
    assert (out / "_export-manifest.json").is_file()
    assert "auto-accepted by policy" in res.stderr


def test_refuse_on_other_kind_does_not_block(env_with_ce, tmp_path: Path):
    """quote_density finding present, but --refuse-on=gpl_contagion
    only blocks on gpl. Falls through to non-interactive refuse path
    because the finding is unhandled (PROMPT in non-tty)."""
    ws = tmp_path / "ws-other"
    _build_quote_density_only_fixture(ws)
    res = run_script(
        "subgraph_export.py",
        "--project", "p", "--to", str(tmp_path / "out"),
        "--refuse-on", "gpl_contagion",
        "--workspace", str(ws),
        env=env_with_ce, check=False,
    )
    # quote_density falls through to PROMPT, which without TTY refuses.
    assert res.returncode != 0
    assert "need decision" in (res.stderr + res.stdout)


def test_carve_out_refuse_all_accept_one(env_with_ce, tmp_path: Path):
    """--refuse-on=all --accept-on=quote_density → quote_density
    auto-accepted, others would refuse. Our fixture only has
    quote_density, so the export should proceed."""
    ws = tmp_path / "ws-carve"
    _build_quote_density_only_fixture(ws)
    out = tmp_path / "out-carve"
    run_script(
        "subgraph_export.py",
        "--project", "p", "--to", str(out),
        "--refuse-on", "all",
        "--accept-on", "quote_density",
        "--workspace", str(ws),
        env=env_with_ce,
    )
    assert (out / "_export-manifest.json").is_file()


def test_strict_alias_still_works_with_deprecation(env_with_ce, tmp_path: Path):
    ws = tmp_path / "ws-strict-alias"
    _build_quote_density_only_fixture(ws)
    res = run_script(
        "subgraph_export.py",
        "--project", "p", "--to", str(tmp_path / "out"),
        "--strict",  # deprecated, should still refuse
        "--workspace", str(ws),
        env=env_with_ce, check=False,
    )
    assert res.returncode != 0
    assert "deprecated" in res.stderr
    assert "refused by policy" in (res.stderr + res.stdout)


def test_yes_alias_still_works_with_deprecation(env_with_ce, tmp_path: Path):
    ws = tmp_path / "ws-yes-alias"
    _build_quote_density_only_fixture(ws)
    out = tmp_path / "out-yes-alias"
    res = run_script(
        "subgraph_export.py",
        "--project", "p", "--to", str(out),
        "--yes",  # deprecated, should still auto-accept
        "--workspace", str(ws),
        env=env_with_ce,
    )
    assert (out / "_export-manifest.json").is_file()
    assert "deprecated" in res.stderr


def test_strict_and_refuse_on_together_errors(env_with_ce, tmp_path: Path):
    ws = tmp_path / "ws-conflict"
    _build_quote_density_only_fixture(ws)
    res = run_script(
        "subgraph_export.py",
        "--project", "p", "--to", str(tmp_path / "out"),
        "--strict", "--refuse-on", "gpl_contagion",
        "--workspace", str(ws),
        env=env_with_ce, check=False,
    )
    assert res.returncode != 0
    assert "mutually exclusive" in (res.stderr + res.stdout)


def test_unknown_kind_errors_at_parse_time(env_with_ce, tmp_path: Path):
    ws = tmp_path / "ws-typo"
    _build_quote_density_only_fixture(ws)
    res = run_script(
        "subgraph_export.py",
        "--project", "p", "--to", str(tmp_path / "out"),
        "--refuse-on", "quotedensity",  # typo: should be quote_density
        "--workspace", str(ws),
        env=env_with_ce, check=False,
    )
    assert res.returncode != 0
    combined = (res.stderr + res.stdout).lower()
    assert "unknown kind" in combined


# --- incoming manifest schema-version validation (v0.4.1) ----------------


def _build_minimal_export_for_merge(src: Path) -> None:
    (src / "wiki" / "concepts").mkdir(parents=True)
    (src / "wiki" / "projects").mkdir(parents=True)
    (src / "vault").mkdir(parents=True)
    (src / "wiki" / "projects" / "p.md").write_text(
        "---\ntitle: P\ntype: project\n---\n"
    )
    (src / "wiki" / "concepts" / "c.md").write_text(
        "---\ntitle: C\ntype: concept\nprojects: [p]\n---\nclean prose.\n"
    )


def test_merge_warns_on_unknown_schema_version(
        wiki_a: Path, env_with_ce, tmp_path: Path):
    src = tmp_path / "future-source"
    _build_minimal_export_for_merge(src)
    (src / "_export-manifest.json").write_text(json.dumps({
        "schema_version": 99,  # far in the future
        "exported_at": "2099-01-01T00:00:00Z",
        "scope_pages": ["concepts/c.md"],
        "vault_metadata": [],
    }))
    res = run_script(
        "merge.py", str(src), "--as-origin", "future",
        "--workspace", str(wiki_a),
        env=env_with_ce,
    )
    assert "manifest compatibility" in res.stderr
    assert "schema_version=99" in res.stderr
    # Merge should still succeed — best-effort.
    staging = wiki_a / ".curator" / ".merge-staging" / "future"
    assert (staging / "audit-report.md").is_file()
    audit = (staging / "audit-report.md").read_text()
    assert "Incoming manifest compatibility" in audit
    assert "schema_version=99" in audit


def test_merge_warns_on_missing_required_fields(
        wiki_a: Path, env_with_ce, tmp_path: Path):
    src = tmp_path / "missing-fields"
    _build_minimal_export_for_merge(src)
    # Manifest with only schema_version — missing exported_at + scope_pages.
    (src / "_export-manifest.json").write_text(json.dumps({
        "schema_version": 2,
    }))
    res = run_script(
        "merge.py", str(src), "--as-origin", "missing",
        "--workspace", str(wiki_a),
        env=env_with_ce,
    )
    assert "manifest compatibility" in res.stderr
    assert "missing expected fields" in res.stderr
    assert (wiki_a / ".curator" / ".merge-staging" / "missing"
            / "audit-report.md").is_file()


def test_merge_warns_on_missing_manifest(
        wiki_a: Path, env_with_ce, tmp_path: Path):
    src = tmp_path / "no-manifest"
    _build_minimal_export_for_merge(src)
    # No _export-manifest.json at all.
    res = run_script(
        "merge.py", str(src), "--as-origin", "manual",
        "--workspace", str(wiki_a),
        env=env_with_ce,
    )
    assert "no _export-manifest.json" in res.stderr
    assert (wiki_a / ".curator" / ".merge-staging" / "manual"
            / "audit-report.md").is_file()


def test_merge_warns_on_corrupt_manifest(
        wiki_a: Path, env_with_ce, tmp_path: Path):
    src = tmp_path / "corrupt-manifest"
    _build_minimal_export_for_merge(src)
    (src / "_export-manifest.json").write_text("not valid json {{{")
    res = run_script(
        "merge.py", str(src), "--as-origin", "broken",
        "--workspace", str(wiki_a),
        env=env_with_ce,
    )
    assert "unreadable" in res.stderr
    # Merge still proceeds.
    assert (wiki_a / ".curator" / ".merge-staging" / "broken"
            / "audit-report.md").is_file()


def test_merge_known_schema_version_no_compatibility_warning(
        wiki_a: Path, env_with_ce, tmp_path: Path):
    src = tmp_path / "good-manifest"
    _build_minimal_export_for_merge(src)
    (src / "_export-manifest.json").write_text(json.dumps({
        "schema_version": 2,
        "exported_at": "2026-05-07T00:00:00Z",
        "scope_pages": ["concepts/c.md"],
        "vault_metadata": [],
    }))
    res = run_script(
        "merge.py", str(src), "--as-origin", "good",
        "--workspace", str(wiki_a),
        env=env_with_ce,
    )
    # No "manifest compatibility" warnings on stderr.
    assert "manifest compatibility" not in res.stderr


# --- info-only findings don't gate export (v0.2.1.1) ---------------------


def _build_arxiv_like_fixture(ws: Path) -> None:
    """A wiki with one vault file shaped like an arXiv extraction:
    sparse author-block emails inside FETCHED CONTENT markers. Should
    produce only info-severity findings."""
    (ws / "wiki" / "concepts").mkdir(parents=True)
    (ws / "wiki" / "projects").mkdir(parents=True)
    (ws / "vault").mkdir(parents=True)
    (ws / ".curator").mkdir(parents=True)
    (ws / "wiki" / "projects" / "p.md").write_text(
        "---\ntitle: P\ntype: project\n---\n"
    )
    (ws / "wiki" / "concepts" / "c.md").write_text(
        "---\ntitle: C\ntype: concept\nprojects: [p]\n---\n"
        "(vault:paper.extracted.md)\n"
    )
    body = (
        "Authors: avaswani@google.com noam@google.com nikip@google.com "
        "usz@google.com llion@google.com aidan@cs.toronto.edu "
        "lukaszkaiser@google.com illia.polosukhin@gmail.com\n\n"
        "Abstract: " + ("padding text " * 4500)
    )
    (ws / "vault" / "paper.extracted.md").write_text(
        "---\ntitle: Paper\nsource_url: https://arxiv.org/abs/1706.03762\n"
        "license: arxiv-non-exclusive\n---\n\n"
        "<!-- BEGIN FETCHED CONTENT -->\n\n"
        + body
        + "\n\n<!-- END FETCHED CONTENT -->\n"
    )


def test_arxiv_like_export_proceeds_without_yes_in_noninteractive(
        env_with_ce, tmp_path: Path):
    """v0.2.1.1: an arXiv-style extraction (sparse author emails inside
    FETCHED markers) yields only info findings, which don't require
    --yes in a non-interactive subprocess. v0.2.1 would have refused."""
    ws = tmp_path / "ws-arxiv"
    _build_arxiv_like_fixture(ws)
    out = tmp_path / "exp-arxiv"
    res = run_script(
        "subgraph_export.py",
        "--project", "p",
        "--include-vault", "owned",
        "--to", str(out),
        "--workspace", str(ws),
        env=env_with_ce,
        # NO --yes flag — non-interactive subprocess. Should succeed
        # because findings are info-only.
    )
    # Stderr should mention info-level findings.
    assert "info-level finding" in res.stderr
    # Manifest should record the info-severity count.
    manifest = json.loads((out / "_export-manifest.json").read_text())
    summary = manifest["preflight_summary"]
    pii_entries = [s for s in summary if s["kind"] == "gdpr_likely_pii"]
    assert pii_entries, "expected PII entry in summary"
    assert all(e["severity"] == "info" for e in pii_entries)


def test_arxiv_like_export_strict_does_not_refuse_on_info(
        env_with_ce, tmp_path: Path):
    """--strict refuses on warn/block but allows info-only findings."""
    ws = tmp_path / "ws-arxiv-strict"
    _build_arxiv_like_fixture(ws)
    out = tmp_path / "exp-arxiv-strict"
    run_script(
        "subgraph_export.py",
        "--project", "p",
        "--include-vault", "owned",
        "--to", str(out),
        "--strict",
        "--workspace", str(ws),
        env=env_with_ce,
    )
    # Export should have succeeded — manifest exists.
    assert (out / "_export-manifest.json").is_file()


def test_dense_pii_export_still_refuses_in_noninteractive(
        env_with_ce, tmp_path: Path):
    """Conversely, a DB-dump-shaped vault file produces warn findings,
    which still refuse without --yes in non-interactive."""
    ws = tmp_path / "ws-dump"
    (ws / "wiki" / "concepts").mkdir(parents=True)
    (ws / "wiki" / "projects").mkdir(parents=True)
    (ws / "vault").mkdir(parents=True)
    (ws / ".curator").mkdir(parents=True)
    (ws / "wiki" / "projects" / "p.md").write_text(
        "---\ntitle: P\ntype: project\n---\n"
    )
    (ws / "wiki" / "concepts" / "c.md").write_text(
        "---\ntitle: C\ntype: concept\nprojects: [p]\n---\n"
        "(vault:dump.extracted.md)\n"
    )
    rows = "\n".join(
        f"row{i},customer{i}@somecompany.com,more padding text"
        for i in range(1000)
    )
    (ws / "vault" / "dump.extracted.md").write_text(
        "---\ntitle: Dump\nsource_url: https://example.org/leak\n"
        "license: arxiv-non-exclusive\n---\n\n"
        "<!-- BEGIN FETCHED CONTENT -->\n" + rows
        + "\n<!-- END FETCHED CONTENT -->\n"
    )
    out = tmp_path / "exp-dump"
    res = run_script(
        "subgraph_export.py",
        "--project", "p",
        "--include-vault", "owned",
        "--to", str(out),
        "--workspace", str(ws),
        env=env_with_ce, check=False,
    )
    assert res.returncode != 0
    combined = res.stderr + res.stdout
    assert "warn/block" in combined or "not interactive" in combined


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


def test_export_owned_includes_gfdl_and_unlicense(
        env_with_ce, tmp_path: Path):
    """v0.4.1: GFDL (Wikipedia content) and Unlicense (public-domain-
    equivalent code) ride along under --include-vault=owned."""
    ws = tmp_path / "ws-v041"
    (ws / "wiki" / "concepts").mkdir(parents=True)
    (ws / "wiki" / "projects").mkdir(parents=True)
    (ws / "vault").mkdir(parents=True)
    (ws / ".curator").mkdir(parents=True)
    (ws / "wiki" / "projects" / "p.md").write_text(
        "---\ntitle: P\ntype: project\n---\n"
    )
    (ws / "wiki" / "concepts" / "c.md").write_text(
        "---\ntitle: C\ntype: concept\nprojects: [p]\n---\n"
        "(vault:wikipedia.md) (vault:tool.md) (vault:old.md)\n"
    )
    (ws / "vault" / "wikipedia.md").write_text(
        "---\ntitle: Wiki\nlicense: gfdl-1.3\n"
        "source_url: https://en.wikipedia.org/wiki/X\n---\nbody\n"
    )
    (ws / "vault" / "tool.md").write_text(
        "---\ntitle: Tool\nlicense: unlicense\n"
        "source_url: https://github.com/x/y\n---\nbody\n"
    )
    (ws / "vault" / "old.md").write_text(
        "---\ntitle: Old\nlicense: cc-by-2.5\n"
        "source_url: https://somewhere.invalid/\n---\nbody\n"
    )
    out = tmp_path / "exp"
    run_script(
        "subgraph_export.py",
        "--project", "p", "--to", str(out),
        "--include-vault", "owned",
        "--workspace", str(ws),
        env=env_with_ce,
    )
    bundled = sorted(p.name for p in (out / "vault").iterdir())
    assert bundled == ["old.md", "tool.md", "wikipedia.md"]


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
