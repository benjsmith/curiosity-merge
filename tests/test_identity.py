"""Identity-keyed reconciliation (U1) and shard ingestion (U4).

Covers identity.py's pure functions in-process and the merge.py identity
flow end-to-end via real subprocesses, mirroring test_e2e.py's style. The
e2e fixtures mint real IRIs with curiosity-engine's identifier_cache.py so
the receiver `.curator/identifiers.db` is the genuine `entities` table the
reconciler reads, not a hand-rolled stand-in.

The backward-compat guarantee — wikis with no minted IRIs still merge by
stem exactly as before — is exercised both here (assert no reconciliations)
and across the whole existing test_e2e.py suite (those fixtures carry no
`iri:`).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import run_script, SCRIPTS


# --- in-process loader for identity.py's pure functions -------------------


@pytest.fixture
def identity_mod(ce_scripts):
    """Import identity.py with curiosity-engine helpers on the path.

    identity.py does `from naming import read_frontmatter` at import time,
    so curiosity-engine's scripts dir must precede the import.
    """
    for p in (str(ce_scripts), str(SCRIPTS)):
        if p not in sys.path:
            sys.path.insert(0, p)
    import identity  # type: ignore
    return identity


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _mint(ce_scripts, env, workspace: Path, *, entity_class: str, title: str,
          page_path: str, same_as: list[str], ws_id: str) -> str:
    """Mint a real IRI into workspace/.curator/identifiers.db. Returns the iri."""
    res = subprocess.run(
        ["uv", "run", "python3", str(ce_scripts / "identifier_cache.py"),
         "mint-entity", "--entity-class", entity_class, "--title", title,
         "--page-path", page_path, "--same-as", json.dumps(same_as),
         "--workspace", ws_id],
        env=env, cwd=str(workspace), capture_output=True, text=True,
    )
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)["iri"]


def _scaffold(root: Path) -> None:
    for d in ("wiki/entities", "wiki/concepts", "wiki/projects", "vault",
              ".curator"):
        (root / d).mkdir(parents=True, exist_ok=True)


# --- pure-function unit tests ---------------------------------------------


def test_parse_same_as_accepts_list_dict_and_json(identity_mod):
    assert identity_mod.parse_same_as(["pubchem:CID2244", "wikidata:Q1"]) == \
        {"pubchem": "CID2244", "wikidata": "Q1"}
    assert identity_mod.parse_same_as({"pubchem": "CID2244"}) == \
        {"pubchem": "CID2244"}
    assert identity_mod.parse_same_as('{"pubchem":"CID2244"}') == \
        {"pubchem": "CID2244"}
    assert identity_mod.parse_same_as(None) == {}
    assert identity_mod.parse_same_as("") == {}


def test_union_same_as_merges_and_drops_none(identity_mod):
    assert identity_mod.union_same_as(
        {"pubchem": "CID1"}, {"wikidata": "Q1", "drug": None}) == \
        {"pubchem": "CID1", "wikidata": "Q1"}


def test_format_same_as_list_is_sorted_bracket_form(identity_mod):
    assert identity_mod.format_same_as_list(
        {"wikidata": "Q1", "pubchem": "CID1"}) == "[pubchem:CID1, wikidata:Q1]"


def test_match_by_iri_and_by_same_as(identity_mod, tmp_path):
    recv = tmp_path / "recv"
    (recv / "entities").mkdir(parents=True)
    _write(recv / "entities" / "aspirin.md",
           "---\ntitle: Aspirin\niri: ce:chemical:wsa:aspirin\n"
           "same_as: [pubchem:CID2244]\nentity_class: chemical\n---\nbody\n")
    receiver_index = identity_mod.build_receiver_index(
        recv, tmp_path / "nodb.db")

    inc = tmp_path / "inc"
    (inc / "entities").mkdir(parents=True)
    # Different slug, matches by shared same_as pair.
    _write(inc / "entities" / "asa.md",
           "---\ntitle: ASA\niri: ce:chemical:wsb:asa\n"
           "same_as: [pubchem:CID2244, wikidata:Q18216]\n"
           "entity_class: chemical\n---\nbody\n")
    recs = identity_mod.match_identities(inc, receiver_index)
    assert len(recs) == 1
    r = recs[0]
    assert r["match_kind"] == "same_as"
    assert r["canonical_slug"] == "aspirin"
    assert r["incoming_slug"] == "asa"
    assert r["union_same_as"] == {"pubchem": "CID2244", "wikidata": "Q18216"}


def test_no_identity_returns_no_matches(identity_mod, tmp_path):
    recv = tmp_path / "recv"
    (recv / "concepts").mkdir(parents=True)
    _write(recv / "concepts" / "transformer.md",
           "---\ntitle: T\ntype: concept\n---\nplain\n")
    receiver_index = identity_mod.build_receiver_index(recv, tmp_path / "x.db")
    inc = tmp_path / "inc"
    (inc / "concepts").mkdir(parents=True)
    _write(inc / "concepts" / "transformer.md",
           "---\ntitle: T2\ntype: concept\n---\nplain\n")
    assert identity_mod.match_identities(inc, receiver_index) == []


# --- end-to-end: same_as-overlap reconciliation ---------------------------


@pytest.fixture
def minted_pair(tmp_path: Path, ce_scripts, env_with_ce):
    """Receiver `wsa` with a minted aspirin entity; source `wsb` whose
    acetylsalicylic-acid shares the pubchem pair under a different slug,
    plus an un-minted page that must fall back to stem matching."""
    wsa = tmp_path / "wsa"
    wsb = tmp_path / "wsb"
    _scaffold(wsa)
    _scaffold(wsb)

    iri_a = _mint(ce_scripts, env_with_ce, wsa, entity_class="chemical",
                  title="Aspirin", page_path="entities/aspirin.md",
                  same_as=["pubchem:CID2244"], ws_id="wsa")
    _write(wsa / "wiki" / "entities" / "aspirin.md",
           f"---\ntitle: Aspirin\ntype: concept\nentity_class: chemical\n"
           f"iri: {iri_a}\nsame_as: [pubchem:CID2244]\nprojects: [pharma]\n"
           f"---\n\nAspirin, a salicylate.\n")
    _write(wsa / "wiki" / "concepts" / "transformer.md",
           "---\ntitle: Transformer\ntype: concept\nprojects: [pharma]\n---\n"
           "\nUn-minted concept.\n")
    _write(wsa / "wiki" / "projects" / "pharma.md",
           "---\ntitle: Pharma\ntype: project\n---\n\n[[aspirin]]\n")

    _write(wsb / "wiki" / "entities" / "acetylsalicylic-acid.md",
           "---\ntitle: Acetylsalicylic Acid\ntype: concept\n"
           "entity_class: chemical\niri: ce:chemical:wsb:acetylsalicylic-acid\n"
           "same_as: [pubchem:CID2244, wikidata:Q18216]\nprojects: [chem]\n"
           "---\n\nSame molecule. Link: [[ibuprofen]].\n")
    _write(wsb / "wiki" / "entities" / "ibuprofen.md",
           "---\ntitle: Ibuprofen\ntype: concept\nentity_class: chemical\n"
           "iri: ce:chemical:wsb:ibuprofen\nsame_as: [pubchem:CID3672]\n"
           "projects: [chem]\n---\n\nNSAID. See [[acetylsalicylic-acid]].\n")
    _write(wsb / "wiki" / "concepts" / "transformer.md",
           "---\ntitle: Transformer (electrical)\ntype: concept\n"
           "projects: [chem]\n---\n\nDifferent un-minted page.\n")
    _write(wsb / "wiki" / "projects" / "chem.md",
           "---\ntitle: Chem\ntype: project\n---\n\n"
           "[[acetylsalicylic-acid]], [[transformer]]\n")
    return wsa, wsb


def test_same_as_overlap_collapses_to_canonical_slug(minted_pair, env_with_ce):
    wsa, wsb = minted_pair
    run_script("merge.py", str(wsb), "--as-origin", "labb",
               "--workspace", str(wsa), env=env_with_ce)
    staging = wsa / ".curator" / ".merge-staging" / "labb"
    manifest = json.loads((staging / "apply.json").read_text())

    recs = manifest["identity_reconciliations"]
    assert len(recs) == 1
    r = recs[0]
    assert r["match_kind"] == "same_as"
    assert r["shared_pairs"] == ["pubchem:CID2244"]
    assert r["canonical_rel"] == "entities/aspirin.md"
    assert r["incoming_slug"] == "acetylsalicylic-acid"

    # The reconciled entity is NOT staged as a live page under its own slug;
    # it is preserved for review under collisions/.
    assert not (staging / "wiki-incoming" / "entities"
                / "acetylsalicylic-acid.md").exists()
    assert (staging / "collisions" / "entities"
            / "aspirin-from-labb.md").is_file()

    # Un-minted ibuprofen lands normally; its wikilink to the collapsed slug
    # is redirected to the canonical slug.
    ibu = (staging / "wiki-incoming" / "entities" / "ibuprofen.md").read_text()
    assert "[[aspirin]]" in ibu
    assert "[[acetylsalicylic-acid]]" not in ibu

    # Un-minted transformer is a stem collision (fallback), NOT an identity.
    assert not any(r["incoming_rel"] == "concepts/transformer.md"
                   for r in recs)
    pages = {p["incoming_rel"]: p["final_rel"]
             for p in manifest["wiki_pages"]}
    assert pages["concepts/transformer.md"] == \
        "concepts/transformer-from-labb.md"


def test_apply_unions_same_as_into_page_and_db(minted_pair, env_with_ce):
    wsa, wsb = minted_pair
    run_script("merge.py", str(wsb), "--as-origin", "labb",
               "--workspace", str(wsa), env=env_with_ce)
    run_script("merge.py", "--apply", "labb",
               "--workspace", str(wsa), env=env_with_ce)

    # Canonical page frontmatter gained the incoming authority.
    text = (wsa / "wiki" / "entities" / "aspirin.md").read_text()
    assert "wikidata:Q18216" in text
    assert "pubchem:CID2244" in text
    # No duplicate entity landed under the incoming slug.
    assert not (wsa / "wiki" / "entities"
                / "acetylsalicylic-acid.md").exists()

    # Registry unioned too.
    import sqlite3
    conn = sqlite3.connect(str(wsa / ".curator" / "identifiers.db"))
    try:
        row = conn.execute(
            "SELECT same_as_json FROM entities WHERE iri = ?",
            ("ce:chemical:wsa:aspirin",)).fetchone()
    finally:
        conn.close()
    same_as = json.loads(row[0])
    assert same_as == {"pubchem": "CID2244", "wikidata": "Q18216"}


# --- end-to-end: shard ingestion (same-IRI seam join) ---------------------


def test_import_shard_rejoins_parent_on_seam_iri(
        minted_pair, env_with_ce, tmp_path):
    wsa, _ = minted_pair  # wsa already holds ce:chemical:wsa:aspirin
    shard = tmp_path / "shard"
    _scaffold(shard)
    # Shard carries the PARENT's iri under a shard-local slug.
    _write(shard / "wiki" / "entities" / "asa.md",
           "---\ntitle: ASA\ntype: concept\nentity_class: chemical\n"
           "iri: ce:chemical:wsa:aspirin\n"
           "same_as: [pubchem:CID2244, drugbank:DB00945]\nprojects: [s]\n"
           "---\n\nParent entity, shard slug. [[salicylate]]\n")
    _write(shard / "wiki" / "concepts" / "salicylate.md",
           "---\ntitle: Salicylate\ntype: concept\nprojects: [s]\n---\n"
           "\nNew concept. [[asa]]\n")
    _write(shard / "wiki" / "projects" / "s.md",
           "---\ntitle: S\ntype: project\n---\n\n[[asa]], [[salicylate]]\n")
    export = tmp_path / "shard-export.json"
    export.write_text(json.dumps({
        "seed": "entities/asa.md",
        "shard_size": 3,
        "pages": ["entities/asa.md", "concepts/salicylate.md",
                  "projects/s.md"],
        "iri_entities_in_shard": 1,
        "seam_entities": [{
            "page": "entities/asa.md", "iri": "ce:chemical:wsa:aspirin",
            "external_linkers": ["concepts/outsider.md"]}],
        "note": "shard",
    }))

    run_script("merge.py", "--import-shard", str(export), str(shard),
               "--as-origin", "shard1", "--workspace", str(wsa),
               env=env_with_ce)
    staging = wsa / ".curator" / ".merge-staging" / "shard1"
    manifest = json.loads((staging / "apply.json").read_text())

    assert manifest["is_shard_import"] is True
    recs = manifest["identity_reconciliations"]
    assert len(recs) == 1
    r = recs[0]
    assert r["match_kind"] == "iri"
    assert r["is_seam"] is True
    assert r["canonical_slug"] == "aspirin"

    # Seam entity collapses (no duplicate); the genuinely new concept lands.
    assert not (staging / "wiki-incoming" / "entities" / "asa.md").exists()
    assert (staging / "wiki-incoming" / "concepts"
            / "salicylate.md").is_file()

    # Audit calls out the shard import and the seam join distinctly.
    audit = (staging / "audit-report.md").read_text()
    assert "U4 shard import" in audit
    assert "Seam joins" in audit
    assert "ce:chemical:wsa:aspirin" in audit


def test_import_shard_rejects_missing_export(minted_pair, env_with_ce, tmp_path):
    wsa, wsb = minted_pair
    res = run_script("merge.py", "--import-shard",
                     str(tmp_path / "nope.json"), str(wsb),
                     "--as-origin", "x", "--workspace", str(wsa),
                     env=env_with_ce, check=False)
    assert res.returncode != 0
    assert "shard export not found" in (res.stderr + res.stdout)


# --- backward compatibility -----------------------------------------------


def test_identical_body_seam_does_not_land_from_origin(
        minted_pair, env_with_ce, tmp_path):
    """Point 2: a shard seam whose body matches the parent must not
    materialize entities/<stem>-from-<origin>.md into live wiki/."""
    wsa, _ = minted_pair
    parent = (wsa / "wiki" / "entities" / "aspirin.md").read_text()
    # Keep the parent's body; only frontmatter (projects) differs — the
    # parallel-shard smoke case.
    body = parent.split("---", 2)[-1]
    shard = tmp_path / "shard-ident"
    _scaffold(shard)
    iri = parent.split("iri: ", 1)[1].split("\n", 1)[0].strip()
    _write(shard / "wiki" / "entities" / "asa.md",
           "---\ntitle: ASA\ntype: concept\nentity_class: chemical\n"
           f"iri: {iri}\nsame_as: [pubchem:CID2244]\nprojects: [s]\n"
           f"---{body}")
    _write(shard / "wiki" / "concepts" / "only-in-shard.md",
           "---\ntitle: Only\ntype: concept\nprojects: [s]\n---\n\nNew.\n")
    export = tmp_path / "ident-export.json"
    export.write_text(json.dumps({
        "seed": "entities/asa.md",
        "shard_size": 2,
        "pages": ["entities/asa.md", "concepts/only-in-shard.md"],
        "iri_entities_in_shard": 1,
        "seam_entities": [{
            "page": "entities/asa.md", "iri": iri,
            "external_linkers": ["concepts/outsider.md"]}],
    }))
    run_script("merge.py", "--import-shard", str(export), str(shard),
               "--as-origin", "shard1", "--workspace", str(wsa),
               env=env_with_ce)
    staging = wsa / ".curator" / ".merge-staging" / "shard1"
    recs = json.loads((staging / "apply.json").read_text())[
        "identity_reconciliations"]
    assert recs and recs[0]["bodies_identical"] is True
    assert recs[0]["review_copy_rel"] is None
    assert not (staging / "collisions").exists() or not any(
        (staging / "collisions").rglob("*-from-shard1.md"))
    run_script("merge.py", "--apply", "shard1",
               "--workspace", str(wsa), env=env_with_ce)
    wiki_ents = wsa / "wiki" / "entities"
    assert (wiki_ents / "aspirin.md").is_file()
    assert not (wiki_ents / "asa.md").exists()
    assert not (wiki_ents / "aspirin-from-shard1.md").exists()
    assert not (wiki_ents / "asa-from-shard1.md").exists()
    assert (wsa / "wiki" / "concepts" / "only-in-shard.md").is_file()


def test_apply_keep_receiver_does_not_add_incoming_projects(
        minted_pair, env_with_ce):
    """Point 3 default: identity collapse does not mutate canonical
    `projects:` (keep-receiver)."""
    wsa, wsb = minted_pair
    run_script("merge.py", str(wsb), "--as-origin", "labb",
               "--workspace", str(wsa), env=env_with_ce)
    run_script("merge.py", "--apply", "labb",
               "--workspace", str(wsa), env=env_with_ce)
    text = (wsa / "wiki" / "entities" / "aspirin.md").read_text()
    fm = text.split("---", 2)[1]
    assert "projects: [pharma]" in fm
    assert "projects: [chem]" not in fm
    assert "projects: [chem, pharma]" not in fm
    assert "projects: [pharma, chem]" not in fm


def test_acl_union_adds_incoming_projects(minted_pair, env_with_ce):
    wsa, wsb = minted_pair
    run_script("merge.py", str(wsb), "--as-origin", "labb",
               "--acl", "union", "--workspace", str(wsa), env=env_with_ce)
    run_script("merge.py", "--apply", "labb",
               "--workspace", str(wsa), env=env_with_ce)
    fm = (wsa / "wiki" / "entities" / "aspirin.md").read_text().split("---", 2)[1]
    assert "pharma" in fm
    assert "chem" in fm


def test_allow_iris_namespaces_unlisted(
        minted_pair, env_with_ce, tmp_path):
    """Point 5: unlisted first-seen IRIs land as <stem>-from-<origin>.md,
    never at the trunk slug. Listed matches still collapse."""
    wsa, wsb = minted_pair
    parent = (wsa / "wiki" / "entities" / "aspirin.md").read_text()
    listed = parent.split("iri: ", 1)[1].split("\n", 1)[0].strip()
    allow = tmp_path / "allow.txt"
    allow.write_text(f"# org trunk\n{listed}\n")
    run_script("merge.py", str(wsb), "--as-origin", "labb",
               "--allow-iris", str(allow), "--workspace", str(wsa),
               env=env_with_ce)
    run_script("merge.py", "--apply", "labb",
               "--workspace", str(wsa), env=env_with_ce)
    ents = wsa / "wiki" / "entities"
    assert (ents / "aspirin.md").is_file()
    assert not (ents / "acetylsalicylic-acid.md").exists()
    assert not (ents / "ibuprofen.md").exists()
    assert (ents / "ibuprofen-from-labb.md").is_file()


def test_no_iri_merge_records_no_reconciliations(
        wiki_a: Path, wiki_b: Path, env_with_ce):
    """The stock fixtures carry no `iri:` — identity reconciliation must be
    a no-op and the stem flow must behave exactly as before."""
    run_script("merge.py", str(wiki_b), "--as-origin", "bob",
               "--workspace", str(wiki_a), env=env_with_ce)
    manifest = json.loads(
        (wiki_a / ".curator" / ".merge-staging" / "bob"
         / "apply.json").read_text())
    assert manifest["identity_reconciliations"] == []
    assert manifest["is_shard_import"] is False
    # Stem collision still renders the classic rename.
    pages = {p["incoming_rel"]: p["final_rel"]
             for p in manifest["wiki_pages"]}
    assert pages["concepts/transformer.md"] == \
        "concepts/transformer-from-bob.md"


def test_source_stub_with_iri_folds_instead_of_forking(
        wiki_a: Path, env_with_ce, tmp_path):
    """A minted source stub takes the same fold path as an un-minted one:
    identity reconciliation must not turn shard link-rewiring back into a
    `sources/<stem>-from-<origin>.md` fork."""
    iri = "https://example.org/id/source/vaswani-2017-attention"
    stub = wiki_a / "wiki" / "sources" / "vaswani-2017-attention.md"
    head, fm, body = stub.read_text().split("---", 2)
    stub.write_text(f"---{fm}iri: {iri}\n---{body}")

    src = tmp_path / "minted-shard"
    (src / "wiki" / "sources").mkdir(parents=True)
    (src / "vault").mkdir(parents=True)
    (src / ".curator").mkdir(parents=True)
    # Same stub under a different slug (identity, not stem, is the join),
    # with one added reciprocal link.
    (src / "wiki" / "sources" / "attention-paper.md").write_text(
        f"---{fm}iri: {iri}\n---{body.rstrip()}\n\n"
        "Cited by:\n- [[diffusion]]\n"
    )
    run_script("merge.py", str(src), "--as-origin", "shardm",
               "--workspace", str(wiki_a), env=env_with_ce)
    manifest = json.loads(
        (wiki_a / ".curator" / ".merge-staging" / "shardm"
         / "apply.json").read_text())
    assert [f["existing_rel"] for f in manifest["source_link_folds"]] == \
        ["sources/vaswani-2017-attention.md"]
    assert manifest["source_link_folds"][0]["links"] == ["diffusion"]
    rec = manifest["identity_reconciliations"][0]
    assert rec["is_source_link_fold"] is True
    assert rec["review_copy_rel"] is None

    run_script("merge.py", "--apply", "shardm",
               "--workspace", str(wiki_a), env=env_with_ce)
    sources = wiki_a / "wiki" / "sources"
    assert sorted(p.name for p in sources.glob("*.md")) == \
        ["vaswani-2017-attention.md"]
    assert "[[diffusion]]" in stub.read_text()
