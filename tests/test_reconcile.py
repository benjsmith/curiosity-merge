"""Unit tests for reconcile.py body-identity (the shard-rejoin smoke bug)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from conftest import SCRIPTS


@pytest.fixture
def reconcile_mod(ce_scripts):
    for p in (str(ce_scripts), str(SCRIPTS)):
        if p not in sys.path:
            sys.path.insert(0, p)
    import reconcile  # type: ignore
    return reconcile


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def test_body_sha256_ignores_frontmatter(reconcile_mod, tmp_path):
    a = tmp_path / "a.md"
    b = tmp_path / "b.md"
    _write(a, "---\ntitle: A\nprojects: [pharma]\n---\n\nSame prose.\n")
    _write(b, "---\ntitle: B\nprojects: [chem]\nupdated: 2026-08-01\n"
              "---\n\nSame prose.\n")
    assert reconcile_mod.body_sha256(a) == reconcile_mod.body_sha256(b)
    assert reconcile_mod.sha256_file(a) != reconcile_mod.sha256_file(b)


def test_body_sha256_strips_untrusted_framing(reconcile_mod, tmp_path):
    raw = tmp_path / "raw.md"
    framed = tmp_path / "framed.md"
    _write(raw, "---\ntitle: T\n---\n\nProse here.\n")
    _write(framed,
           "---\ntitle: T\norigin: shard1\nuntrusted: true\n---\n\n"
           "<!-- BEGIN UNTRUSTED MERGED CONTENT — origin:shard1 -->\n\n"
           "Prose here.\n\n"
           "<!-- END UNTRUSTED MERGED CONTENT -->\n")
    assert reconcile_mod.body_sha256(raw) == reconcile_mod.body_sha256(framed)


def test_classify_collision_identical_body_different_fm(reconcile_mod, tmp_path):
    inc = tmp_path / "inc" / "concepts" / "transformer.md"
    exist = tmp_path / "recv" / "concepts" / "transformer.md"
    _write(inc, "---\ntitle: T\nprojects: [shard]\n---\n\nUnchanged body.\n")
    _write(exist, "---\ntitle: T\nprojects: [parent]\n---\n\nUnchanged body.\n")
    c = reconcile_mod.classify_collision(inc, exist)
    assert c["kind"] == "identical"
    assert c["similarity"] == 1.0


def test_classify_collision_different_body_is_not_identical(
        reconcile_mod, tmp_path):
    inc = tmp_path / "inc" / "concepts" / "transformer.md"
    exist = tmp_path / "recv" / "concepts" / "transformer.md"
    _write(inc, "---\ntitle: T\n---\n\nShard-curated rewrite.\n")
    _write(exist, "---\ntitle: T\n---\n\nParent original prose.\n")
    c = reconcile_mod.classify_collision(inc, exist)
    assert c["kind"] != "identical"
