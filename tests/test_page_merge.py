"""Cross-graph page merge, ontology alignment, and export ceilings."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import SCRIPTS


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def merge_mod():
    sys.path.insert(0, str(SCRIPTS))
    import page_merge
    return page_merge


@pytest.fixture
def align_mod():
    sys.path.insert(0, str(SCRIPTS))
    import ontology_align
    return ontology_align


def test_list_keys_union_and_one_sided_body(merge_mod):
    base = "---\ntitle: T\nsources: [a.md]\n---\n\nSame.\n"
    ours = "---\ntitle: T\nsources: [a.md, b.md]\n---\n\nSame.\n"
    theirs = "---\ntitle: T\nsources: [a.md, c.md]\n---\n\nSame.\nTheir note.\n"
    result = merge_mod.merge_texts(base, ours, theirs)
    assert result["merged"] is True
    assert "b.md" in result["content"]
    assert "c.md" in result["content"]
    assert "Their note." in result["content"]
    assert "sources" in result["union_keys"]


def test_body_conflict_keeps_both(merge_mod):
    base = "---\ntitle: T\n---\n\nBase.\n"
    ours = "---\ntitle: T\n---\n\nOurs.\n"
    theirs = "---\ntitle: T\n---\n\nTheirs.\n"
    result = merge_mod.merge_texts(base, ours, theirs)
    assert result["merged"] is False
    assert result["conflicts"][0]["kind"] == "body"
    assert "Ours." in result["conflicts"][0]["ours"]
    assert "Theirs." in result["conflicts"][0]["theirs"]
    assert result["content"] == ""


def test_ontology_conflict_is_not_auto_mapped(align_mod, tmp_path):
    local = tmp_path / "local.yaml"
    remote = tmp_path / "remote.yaml"
    _write(local, "nodes:\n  - type: concept\nedges:\n  - type: supports\n    source: [evidence]\n    target: [hypothesis]\n")
    _write(remote, "nodes:\n  - type: concept\nedges:\n  - type: supports\n    source: [note]\n    target: [concept]\n")
    result = align_mod.compare(align_mod._load(local), align_mod._load(remote))
    assert "concept" in result["identical"]
    assert result["conflicts"][0]["name"] == "supports"


def test_adapt_rewrites_unknown_type_and_records_it(align_mod):
    text = "---\ntype: lab_result\ntitle: Run\n---\n\nBody.\n"
    content, notes = align_mod.adapt_page(text, {"map": []}, {"concept", "evidence"})
    assert "type: concept" in content
    assert notes[0]["from"] == "lab_result"
    assert notes[0]["to"] == "concept"
    mapped, notes2 = align_mod.adapt_page(
        text, {"map": [{"remote": "lab_result", "local": "evidence"}]}, {"evidence"},
    )
    assert "type: evidence" in mapped
    assert notes2[0]["to"] == "evidence"


def test_export_skips_state_directories():
    sys.path.insert(0, str(SCRIPTS))
    import classification
    assert classification.export_excluded("captures/note.md")
    assert classification.export_excluded(".workbench/state/page.json")
    assert not classification.export_excluded("concepts/open.md")


def test_export_drops_pages_above_ceiling(tmp_path):
    wiki = tmp_path / "wiki"
    _write(wiki / "concepts" / "open.md", "---\ntitle: Open\ntype: concept\nclassification: public\n---\n\nPublic.\n")
    _write(wiki / "concepts" / "secret.md", "---\ntitle: Secret\ntype: concept\nclassification: confidential\n---\n\nHidden.\n")
    _write(wiki / "concepts" / "cites.md", "---\ntitle: Cites\ntype: concept\nclassification: public\n---\n\nSee [[secret]].\n")
    (tmp_path / "vault").mkdir()
    dest = tmp_path.parent / f"{tmp_path.name}-out"
    ce = Path(__file__).resolve().parents[2] / "curiosity-engine" / "skills" / "curiosity-engine" / "scripts"
    env = dict(**os.environ)
    env["CURIOSITY_ENGINE_SCRIPTS_DIR"] = str(ce)
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "subgraph_export.py"),
         "--workspace", str(tmp_path), "--pages-file", str(_pages_file(tmp_path)),
         "--to", str(dest), "--no-preflight", "--force",
         "--max-classification", "public"],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    exported = list((dest / "wiki").rglob("*.md"))
    names = {p.name for p in exported}
    assert "open.md" in names
    assert "secret.md" not in names
    assert "cites.md" not in names


def _pages_file(root: Path) -> Path:
    path = root / "pages.json"
    path.write_text(json.dumps([
        "concepts/open", "concepts/secret", "concepts/cites",
    ]), encoding="utf-8")
    return path
