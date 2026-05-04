"""Test fixtures: build two minimal but realistic curiosity-engine wikis
that exercise vault sha256 dedup, page-name collisions, and citations
with rewrite-target paths.

Wiki A is the receiving wiki. Wiki B is the source. They share one
vault file by content (different filenames), have a same-stem
`concepts/transformer.md` collision, and each have unique pages.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
CE_SCRIPTS_DEFAULT = REPO_ROOT.parent / "curiosity-engine" / "scripts"


def _ce_scripts_path() -> Path:
    """Locate curiosity-engine's scripts dir for the test session.

    Order: env var override → sibling repo of curiosity-merge → user-
    scoped install fallbacks.
    """
    env = os.environ.get("CURIOSITY_ENGINE_SCRIPTS_DIR")
    if env and (Path(env) / "naming.py").is_file():
        return Path(env)
    candidates = [
        CE_SCRIPTS_DEFAULT,
        Path.home() / ".claude" / "skills" / "curiosity-engine" / "scripts",
        Path.home() / ".agents" / "skills" / "curiosity-engine" / "scripts",
    ]
    for c in candidates:
        if (c / "naming.py").is_file():
            return c
    pytest.skip(
        "curiosity-engine scripts not found; set CURIOSITY_ENGINE_SCRIPTS_DIR"
    )


@pytest.fixture(scope="session")
def ce_scripts() -> Path:
    return _ce_scripts_path()


@pytest.fixture
def env_with_ce(ce_scripts):
    env = os.environ.copy()
    env["CURIOSITY_ENGINE_SCRIPTS_DIR"] = str(ce_scripts)
    return env


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


@pytest.fixture
def wiki_a(tmp_path: Path) -> Path:
    """Receiving wiki. Has ml-foundations project with transformer +
    attention concepts, a source stub, and a vault extraction.
    """
    root = tmp_path / "wiki-a"
    (root / "wiki" / "concepts").mkdir(parents=True)
    (root / "wiki" / "projects").mkdir(parents=True)
    (root / "wiki" / "sources").mkdir(parents=True)
    (root / "vault").mkdir(parents=True)
    (root / ".curator").mkdir(parents=True)

    _write(root / "wiki" / "concepts" / "transformer.md", """\
---
title: Transformer
type: concept
projects: [ml-foundations]
sources: [vaswani-2017-attention.md]
---

A neural network architecture based on self-attention, introduced by
[[vaswani-2017-attention]]. Dispenses with recurrence entirely.

(vault:vaswani-2017-attention.extracted.md)
""")
    _write(root / "wiki" / "concepts" / "attention.md", """\
---
title: Attention
type: concept
projects: [ml-foundations]
sources: [vaswani-2017-attention.md]
---

Self-attention as used in [[transformer]] models.

(vault:vaswani-2017-attention.extracted.md)
""")
    _write(root / "wiki" / "sources" / "vaswani-2017-attention.md", """\
---
title: "Attention Is All You Need - Vaswani, 2017"
type: source
projects: [ml-foundations]
source_url: https://arxiv.org/abs/1706.03762
---

Source extraction. (vault:vaswani-2017-attention.extracted.md)
""")
    _write(root / "wiki" / "projects" / "ml-foundations.md", """\
---
title: ML Foundations
type: project
description: Foundational ML concepts
---

Anchor concepts: [[transformer]], [[attention]].
""")
    _write(root / "vault" / "vaswani-2017-attention.extracted.md", """\
---
title: "Attention Is All You Need"
authors: [Vaswani, Shazeer, Parmar]
date: 2017-06-12
source_url: https://arxiv.org/abs/1706.03762
---

<!-- BEGIN FETCHED CONTENT -->

# Attention Is All You Need (Vaswani, 2017)

A new architecture based on attention mechanisms.

<!-- END FETCHED CONTENT -->
""")
    return root


@pytest.fixture
def wiki_a_with_paywalled(tmp_path: Path) -> Path:
    """Variant of wiki_a with a mix of licensed sources for hydrate tests:
    one arXiv preprint (redistributable), one Nature paper (paywalled),
    one CC-BY blog post (open). Each cited from a wiki page.
    """
    root = tmp_path / "wiki-a-mixed"
    (root / "wiki" / "concepts").mkdir(parents=True)
    (root / "wiki" / "sources").mkdir(parents=True)
    (root / "wiki" / "projects").mkdir(parents=True)
    (root / "vault").mkdir(parents=True)
    (root / ".curator").mkdir(parents=True)

    _write(root / "wiki" / "projects" / "mixed.md", """\
---
title: Mixed Project
type: project
---

Mixed-license sources.
""")
    _write(root / "wiki" / "concepts" / "topic-a.md", """\
---
title: Topic A
type: concept
projects: [mixed]
---

(vault:arxiv-paper.extracted.md) (vault:nature-paper.extracted.md) (vault:openblog.extracted.md)
""")
    _write(root / "wiki" / "sources" / "arxiv-paper.md", """\
---
title: "ArXiv Source"
type: source
projects: [mixed]
source_url: https://arxiv.org/abs/1706.03762
---

(vault:arxiv-paper.extracted.md)
""")
    _write(root / "wiki" / "sources" / "nature-paper.md", """\
---
title: "Nature Source"
type: source
projects: [mixed]
source_url: https://nature.com/articles/example
---

(vault:nature-paper.extracted.md)
""")
    _write(root / "wiki" / "sources" / "openblog.md", """\
---
title: "Open Blog"
type: source
projects: [mixed]
source_url: https://example.org/blog
---

(vault:openblog.extracted.md)
""")
    # Vault files with explicit license markers.
    _write(root / "vault" / "arxiv-paper.extracted.md", """\
---
title: "ArXiv Paper"
source_url: https://arxiv.org/abs/1706.03762
license: arxiv-non-exclusive
---

Body.
""")
    _write(root / "vault" / "nature-paper.extracted.md", """\
---
title: "Nature Paper"
source_url: https://nature.com/articles/example
license: all-rights-reserved
---

Body.
""")
    _write(root / "vault" / "openblog.extracted.md", """\
---
title: "Open Blog Post"
source_url: https://example.org/blog
license: CC-BY-4.0
---

Body.
""")
    return root


@pytest.fixture
def wiki_b(tmp_path: Path, wiki_a: Path) -> Path:
    """Source wiki. Generative-models project with a transformer page
    that collides with wiki-a's, plus a unique diffusion concept. Vault
    extraction is byte-identical to wiki-a's (different filename → tests
    sha256 dedup).
    """
    root = tmp_path / "wiki-b"
    (root / "wiki" / "concepts").mkdir(parents=True)
    (root / "wiki" / "projects").mkdir(parents=True)
    (root / "wiki" / "sources").mkdir(parents=True)
    (root / "vault").mkdir(parents=True)
    (root / ".curator").mkdir(parents=True)

    _write(root / "wiki" / "concepts" / "transformer.md", """\
---
title: Transformer (Architecture)
type: concept
projects: [generative-models]
sources: [attention-paper.md]
---

The Transformer is a model architecture that eschews recurrence.
See [[attention-paper]] for the source. (vault:attention-paper.extracted.md)
""")
    _write(root / "wiki" / "concepts" / "diffusion.md", """\
---
title: Diffusion Models
type: concept
projects: [generative-models]
---

Diffusion models reverse a gradual noising process.
Related: [[transformer]] is often the denoising network.
""")
    _write(root / "wiki" / "sources" / "attention-paper.md", """\
---
title: "Attention Is All You Need"
type: source
projects: [generative-models]
source_url: https://arxiv.org/abs/1706.03762
---

Source extraction. (vault:attention-paper.extracted.md)
""")
    _write(root / "wiki" / "projects" / "generative-models.md", """\
---
title: Generative Models
type: project
description: Generative modeling
---

Anchors: [[transformer]], [[diffusion]].
""")
    # Byte-identical to wiki-a's vault file.
    shutil.copy2(
        wiki_a / "vault" / "vaswani-2017-attention.extracted.md",
        root / "vault" / "attention-paper.extracted.md",
    )
    return root


def run_script(name: str, *args: str, env: dict, cwd: Path | None = None,
               check: bool = True) -> subprocess.CompletedProcess:
    cmd = ["uv", "run", "python3", str(SCRIPTS / name), *args]
    res = subprocess.run(cmd, env=env, cwd=str(cwd) if cwd else None,
                         capture_output=True, text=True)
    if check and res.returncode != 0:
        raise AssertionError(
            f"{name} {' '.join(args)} failed (rc={res.returncode})\n"
            f"stdout: {res.stdout}\nstderr: {res.stderr}"
        )
    return res
