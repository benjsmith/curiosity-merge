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
    assert r["incoming_same_as"] == {"pubchem": "CID2244", "wikidata": "Q18216"}


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


# --- same_as reversal planning (pure) -------------------------------------


def test_plan_same_as_reversal_last_claimer_and_restore(identity_mod):
    incoming = {"pubchem": "CID2244", "wikidata": "Q2"}
    current = {"pubchem": "CID2244", "wikidata": "Q2"}
    original = {"pubchem": "CID2244"}
    remaining = {"wikidata": "Q1"}
    d = identity_mod.plan_same_as_reversal(
        incoming_same_as=incoming,
        added_same_as={"wikidata": "Q2"},
        current=current,
        original=original,
        remaining=remaining,
        remaining_claimers={"wikidata:Q1": ["labb"]},
    )
    assert d["remove"] == {}
    assert d["restore"] == {"wikidata": "Q1"}
    assert d["keep_original"] == [{"key": "pubchem", "value": "CID2244"}]
    assert d["precise"] is True

    d2 = identity_mod.plan_same_as_reversal(
        incoming_same_as={"wikidata": "Q18216"},
        added_same_as={"wikidata": "Q18216"},
        current={"pubchem": "CID2244", "wikidata": "Q18216"},
        original={"pubchem": "CID2244"},
        remaining={},
    )
    assert d2["remove"] == {"wikidata": "Q18216"}

    d3 = identity_mod.plan_same_as_reversal(
        incoming_same_as={"wikidata": "Q18216"},
        added_same_as={},
        current={"pubchem": "CID2244", "wikidata": "Q18216"},
        original={"pubchem": "CID2244"},
        remaining={"wikidata": "Q18216"},
        remaining_claimers={"wikidata:Q18216": ["labc"]},
    )
    assert d3["remove"] == {}
    assert d3["keep_claimed"][0]["by"] == ["labc"]

    d4 = identity_mod.plan_same_as_reversal(
        incoming_same_as={"wikidata": "Q18216"},
        added_same_as={"wikidata": "Q18216"},
        current={"pubchem": "CID2244", "wikidata": "Q999"},
        original={"pubchem": "CID2244"},
        remaining={},
    )
    assert d4["remove"] == {}
    assert d4["skip_user_edit"][0]["current"] == "Q999"

    d5 = identity_mod.plan_same_as_reversal(
        incoming_same_as={"wikidata": "Q18216"},
        added_same_as=None,
        current={"wikidata": "Q18216"},
        original=None,
        remaining={},
    )
    assert d5["precise"] is False
    assert d5["remove"] == {}
    assert d5["skip_imprecise"]


def test_apply_same_as_reversal_respects_live_value(identity_mod):
    new = identity_mod.apply_same_as_reversal(
        {"pubchem": "CID2244", "wikidata": "Q2"},
        incoming_same_as={"wikidata": "Q2"},
        remove={},
        restore={"wikidata": "Q1"},
    )
    assert new == {"pubchem": "CID2244", "wikidata": "Q1"}
    # User changed the value between stage and apply: leave it.
    skipped = identity_mod.apply_same_as_reversal(
        {"pubchem": "CID2244", "wikidata": "Q999"},
        incoming_same_as={"wikidata": "Q2"},
        remove={"wikidata": "Q2"},
        restore={},
    )
    assert skipped["wikidata"] == "Q999"


def test_collect_remaining_claims_skips_old_manifests(identity_mod):
    others = [
        ("old", {"applied_at": "2026-01-01T00:00:00Z",
                 "identity_reconciliations": [{
                     "canonical_iri": "ce:x",
                     "union_same_as": {"wikidata": "Q1"},
                 }]}),
        ("new", {"applied_at": "2026-02-01T00:00:00Z",
                 "identity_reconciliations": [{
                     "canonical_iri": "ce:x",
                     "incoming_same_as": {"wikidata": "Q1", "drugbank": "DB1"},
                 }]}),
    ]
    remaining, claimers = identity_mod.collect_remaining_claims(
        others, canonical_iri="ce:x", canonical_rel=None)
    assert remaining == {"wikidata": "Q1", "drugbank": "DB1"}
    assert claimers["wikidata:Q1"] == ["new"]


def test_original_same_as_uses_earliest_prior_including_archive(identity_mod):
    manifests = [
        ("labc-unmerged-1", {
            "applied_at": "2026-02-01T00:00:00Z",
            "identity_reconciliations": [{
                "canonical_iri": "ce:x",
                "prior_page_same_as": {"pubchem": "CID2244", "wikidata": "Q1"},
            }],
        }),
        ("labb", {
            "applied_at": "2026-01-01T00:00:00Z",
            "identity_reconciliations": [{
                "canonical_iri": "ce:x",
                "prior_page_same_as": {"pubchem": "CID2244"},
            }],
        }),
    ]
    assert identity_mod.original_same_as(
        manifests, canonical_iri="ce:x", canonical_rel=None) == \
        {"pubchem": "CID2244"}


# --- unmerge reverses identity same_as ------------------------------------


def _page_iri(path: Path) -> str:
    for line in path.read_text().splitlines():
        if line.startswith("iri:"):
            return line.split(":", 1)[1].strip()
    raise AssertionError(f"no iri in {path}")


def _db_same_as(workspace: Path, iri: str):
    import sqlite3
    db = workspace / ".curator" / "identifiers.db"
    if not db.is_file():
        return None
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT same_as_json FROM entities WHERE iri = ?", (iri,)
        ).fetchone()
    finally:
        conn.close()
    return json.loads(row[0]) if row else None


def test_unmerge_round_trip_same_as(minted_pair, env_with_ce):
    wsa, wsb = minted_pair
    run_script("merge.py", str(wsb), "--as-origin", "labb",
               "--workspace", str(wsa), env=env_with_ce)
    run_script("merge.py", "--apply", "labb",
               "--workspace", str(wsa), env=env_with_ce)
    page = wsa / "wiki" / "entities" / "aspirin.md"
    iri = _page_iri(page)
    assert "wikidata:Q18216" in page.read_text()
    persisted = json.loads(
        (wsa / ".curator" / "merges" / "labb.json").read_text())
    rec = persisted["identity_reconciliations"][0]
    assert rec["incoming_same_as"]["wikidata"] == "Q18216"
    assert rec["added_same_as"]["wikidata"] == "Q18216"
    assert rec["prior_page_same_as"] == {"pubchem": "CID2244"}
    assert rec["db_row_existed_before"] is True
    review = wsa / "wiki" / "entities" / "aspirin-from-labb.md"
    assert review.is_file()

    run_script("unmerge.py", "--origin", "labb",
               "--workspace", str(wsa), env=env_with_ce)
    plan = json.loads(
        (wsa / ".curator" / ".unmerge-staging" / "labb" / "plan.json").read_text())
    rev = plan["identity_reversals"][0]
    assert rev["remove"] == {"wikidata": "Q18216"}
    audit = (wsa / ".curator" / ".unmerge-staging" / "labb"
             / "audit-report.md").read_text()
    assert "Identity reconciliations to reverse" in audit
    assert "wikidata:Q18216" in audit

    run_script("unmerge.py", "--origin", "labb", "--apply",
               "--workspace", str(wsa), env=env_with_ce)
    text = page.read_text()
    assert "wikidata:Q18216" not in text
    assert "pubchem:CID2244" in text
    assert not review.exists()
    assert _db_same_as(wsa, iri) == {"pubchem": "CID2244"}
    # Native page that linked [[aspirin]] is untouched (no unmerge comment).
    native = (wsa / "wiki" / "projects" / "pharma.md").read_text()
    assert "[[aspirin]]" in native
    assert "<!-- unmerge:" not in native


def test_unmerge_keep_identity_same_as(minted_pair, env_with_ce):
    wsa, wsb = minted_pair
    run_script("merge.py", str(wsb), "--as-origin", "labb",
               "--workspace", str(wsa), env=env_with_ce)
    run_script("merge.py", "--apply", "labb",
               "--workspace", str(wsa), env=env_with_ce)
    run_script("unmerge.py", "--origin", "labb", "--keep-identity-same-as",
               "--workspace", str(wsa), env=env_with_ce)
    run_script("unmerge.py", "--origin", "labb", "--apply",
               "--workspace", str(wsa), env=env_with_ce)
    text = (wsa / "wiki" / "entities" / "aspirin.md").read_text()
    assert "wikidata:Q18216" in text
    assert "pubchem:CID2244" in text


def test_unmerge_preserves_user_edited_pair(minted_pair, env_with_ce):
    wsa, wsb = minted_pair
    run_script("merge.py", str(wsb), "--as-origin", "labb",
               "--workspace", str(wsa), env=env_with_ce)
    run_script("merge.py", "--apply", "labb",
               "--workspace", str(wsa), env=env_with_ce)
    page = wsa / "wiki" / "entities" / "aspirin.md"
    page.write_text(page.read_text().replace("wikidata:Q18216", "wikidata:Q999"))
    run_script("unmerge.py", "--origin", "labb",
               "--workspace", str(wsa), env=env_with_ce)
    run_script("unmerge.py", "--origin", "labb", "--apply",
               "--workspace", str(wsa), env=env_with_ce)
    text = page.read_text()
    assert "wikidata:Q999" in text
    assert "wikidata:Q18216" not in text
    assert "pubchem:CID2244" in text


def test_unmerge_last_claimer_across_two_origins(minted_pair, env_with_ce, tmp_path):
    wsa, wsb = minted_pair
    wsc = tmp_path / "wsc"
    _scaffold(wsc)
    _write(wsc / "wiki" / "entities" / "asa.md",
           "---\ntitle: ASA\ntype: concept\nentity_class: chemical\n"
           "iri: ce:chemical:wsc:asa\n"
           "same_as: [pubchem:CID2244, wikidata:Q18216]\nprojects: [c]\n"
           "---\n\nThird shard, same pair.\n")
    run_script("merge.py", str(wsb), "--as-origin", "labb",
               "--workspace", str(wsa), env=env_with_ce)
    run_script("merge.py", "--apply", "labb",
               "--workspace", str(wsa), env=env_with_ce)
    run_script("merge.py", str(wsc), "--as-origin", "labc",
               "--workspace", str(wsa), env=env_with_ce)
    run_script("merge.py", "--apply", "labc",
               "--workspace", str(wsa), env=env_with_ce)
    page = wsa / "wiki" / "entities" / "aspirin.md"
    assert "wikidata:Q18216" in page.read_text()

    # Introducer first: the second origin still claims the pair.
    run_script("unmerge.py", "--origin", "labb",
               "--workspace", str(wsa), env=env_with_ce)
    run_script("unmerge.py", "--origin", "labb", "--apply",
               "--workspace", str(wsa), env=env_with_ce)
    assert "wikidata:Q18216" in page.read_text()
    assert "pubchem:CID2244" in page.read_text()

    run_script("unmerge.py", "--origin", "labc",
               "--workspace", str(wsa), env=env_with_ce)
    run_script("unmerge.py", "--origin", "labc", "--apply",
               "--workspace", str(wsa), env=env_with_ce)
    text = page.read_text()
    assert "wikidata:Q18216" not in text
    assert "pubchem:CID2244" in text


def test_unmerge_restores_overwritten_authority(minted_pair, env_with_ce, tmp_path):
    wsa, wsb = minted_pair
    wsc = tmp_path / "wsc"
    _scaffold(wsc)
    _write(wsc / "wiki" / "entities" / "asa.md",
           "---\ntitle: ASA\ntype: concept\nentity_class: chemical\n"
           "iri: ce:chemical:wsc:asa\n"
           "same_as: [pubchem:CID2244, wikidata:Q2]\nprojects: [c]\n"
           "---\n\nConflicting wikidata id.\n")
    run_script("merge.py", str(wsb), "--as-origin", "labb",
               "--workspace", str(wsa), env=env_with_ce)
    run_script("merge.py", "--apply", "labb",
               "--workspace", str(wsa), env=env_with_ce)
    run_script("merge.py", str(wsc), "--as-origin", "labc",
               "--workspace", str(wsa), env=env_with_ce)
    run_script("merge.py", "--apply", "labc",
               "--workspace", str(wsa), env=env_with_ce)
    page = wsa / "wiki" / "entities" / "aspirin.md"
    assert "wikidata:Q2" in page.read_text()
    assert "wikidata:Q18216" not in page.read_text()

    run_script("unmerge.py", "--origin", "labc",
               "--workspace", str(wsa), env=env_with_ce)
    run_script("unmerge.py", "--origin", "labc", "--apply",
               "--workspace", str(wsa), env=env_with_ce)
    text = page.read_text()
    assert "wikidata:Q18216" in text
    assert "wikidata:Q2" not in text


def test_unmerge_old_manifest_does_not_guess(minted_pair, env_with_ce):
    wsa, wsb = minted_pair
    run_script("merge.py", str(wsb), "--as-origin", "labb",
               "--workspace", str(wsa), env=env_with_ce)
    run_script("merge.py", "--apply", "labb",
               "--workspace", str(wsa), env=env_with_ce)
    path = wsa / ".curator" / "merges" / "labb.json"
    m = json.loads(path.read_text())
    for r in m["identity_reconciliations"]:
        r.pop("incoming_same_as", None)
        r.pop("added_same_as", None)
        r.pop("prior_page_same_as", None)
    path.write_text(json.dumps(m, indent=2) + "\n")
    run_script("unmerge.py", "--origin", "labb",
               "--workspace", str(wsa), env=env_with_ce)
    audit = (wsa / ".curator" / ".unmerge-staging" / "labb"
             / "audit-report.md").read_text()
    assert "predates precise-reversal tracking" in audit
    run_script("unmerge.py", "--origin", "labb", "--apply",
               "--workspace", str(wsa), env=env_with_ce)
    assert "wikidata:Q18216" in (
        wsa / "wiki" / "entities" / "aspirin.md").read_text()


def test_unmerge_deletes_db_row_this_merge_created(tmp_path, env_with_ce):
    wsa = tmp_path / "wsa"
    wsb = tmp_path / "wsb"
    _scaffold(wsa)
    _scaffold(wsb)
    iri = "ce:chemical:wsa:aspirin"
    _write(wsa / "wiki" / "entities" / "aspirin.md",
           f"---\ntitle: Aspirin\ntype: concept\nentity_class: chemical\n"
           f"iri: {iri}\nprojects: [pharma]\n"
           f"---\n\nAspirin, a salicylate.\n")
    _write(wsb / "wiki" / "entities" / "asa.md",
           f"---\ntitle: ASA\ntype: concept\nentity_class: chemical\n"
           f"iri: {iri}\nsame_as: [wikidata:Q18216]\nprojects: [chem]\n"
           f"---\n\nSame molecule.\n")
    run_script("merge.py", str(wsb), "--as-origin", "labb",
               "--workspace", str(wsa), env=env_with_ce)
    run_script("merge.py", "--apply", "labb",
               "--workspace", str(wsa), env=env_with_ce)
    persisted = json.loads(
        (wsa / ".curator" / "merges" / "labb.json").read_text())
    assert persisted["identity_reconciliations"][0]["db_row_existed_before"] is False
    assert _db_same_as(wsa, iri) == {"wikidata": "Q18216"}
    run_script("unmerge.py", "--origin", "labb",
               "--workspace", str(wsa), env=env_with_ce)
    run_script("unmerge.py", "--origin", "labb", "--apply",
               "--workspace", str(wsa), env=env_with_ce)
    assert _db_same_as(wsa, iri) is None
    text = (wsa / "wiki" / "entities" / "aspirin.md").read_text()
    assert "wikidata:Q18216" not in text
    assert "same_as:" not in text.split("---", 2)[1]


def test_unmerge_identical_body_identity_does_not_crash(
        minted_pair, env_with_ce, tmp_path):
    """v0.8.0/v0.8.1: final_rel null must not crash unmerge; no review copy
    is invented; same_as still reverses."""
    wsa, _ = minted_pair
    parent = (wsa / "wiki" / "entities" / "aspirin.md").read_text()
    body = parent.split("---", 2)[-1]
    iri = parent.split("iri: ", 1)[1].split("\n", 1)[0].strip()
    shard = tmp_path / "shard-ident"
    _scaffold(shard)
    _write(shard / "wiki" / "entities" / "asa.md",
           "---\ntitle: ASA\ntype: concept\nentity_class: chemical\n"
           f"iri: {iri}\nsame_as: [pubchem:CID2244, wikidata:Q18216]\n"
           f"projects: [s]\n---{body}")
    export = tmp_path / "ident-export.json"
    export.write_text(json.dumps({
        "seed": "entities/asa.md",
        "shard_size": 1,
        "pages": ["entities/asa.md"],
        "iri_entities_in_shard": 1,
        "seam_entities": [{
            "page": "entities/asa.md", "iri": iri,
            "external_linkers": []}],
    }))
    run_script("merge.py", "--import-shard", str(export), str(shard),
               "--as-origin", "shard1", "--workspace", str(wsa),
               env=env_with_ce)
    run_script("merge.py", "--apply", "shard1",
               "--workspace", str(wsa), env=env_with_ce)
    wiki_ents = wsa / "wiki" / "entities"
    assert not (wiki_ents / "aspirin-from-shard1.md").exists()
    assert "wikidata:Q18216" in (wiki_ents / "aspirin.md").read_text()
    run_script("unmerge.py", "--origin", "shard1",
               "--workspace", str(wsa), env=env_with_ce)
    run_script("unmerge.py", "--origin", "shard1", "--apply",
               "--workspace", str(wsa), env=env_with_ce)
    text = (wiki_ents / "aspirin.md").read_text()
    assert "wikidata:Q18216" not in text
    assert "pubchem:CID2244" in text
    assert not (wiki_ents / "aspirin-from-shard1.md").exists()


def test_unmerge_does_not_unfold_source_stub_links(
        wiki_a: Path, env_with_ce, tmp_path):
    """v0.8.1 fold must survive identity unmerge (frontmatter-only)."""
    iri = "https://example.org/id/source/vaswani-2017-attention"
    stub = wiki_a / "wiki" / "sources" / "vaswani-2017-attention.md"
    head, fm, body = stub.read_text().split("---", 2)
    stub.write_text(f"---{fm}iri: {iri}\n---{body}")
    src = tmp_path / "minted-shard"
    (src / "wiki" / "sources").mkdir(parents=True)
    (src / "vault").mkdir(parents=True)
    (src / ".curator").mkdir(parents=True)
    (src / "wiki" / "sources" / "attention-paper.md").write_text(
        f"---{fm}iri: {iri}\n---{body.rstrip()}\n\n"
        "Cited by:\n- [[diffusion]]\n"
    )
    run_script("merge.py", str(src), "--as-origin", "shardm",
               "--workspace", str(wiki_a), env=env_with_ce)
    run_script("merge.py", "--apply", "shardm",
               "--workspace", str(wiki_a), env=env_with_ce)
    assert "[[diffusion]]" in stub.read_text()
    run_script("unmerge.py", "--origin", "shardm",
               "--workspace", str(wiki_a), env=env_with_ce)
    run_script("unmerge.py", "--origin", "shardm", "--apply",
               "--workspace", str(wiki_a), env=env_with_ce)
    assert "[[diffusion]]" in stub.read_text()
    sources = wiki_a / "wiki" / "sources"
    assert sorted(p.name for p in sources.glob("*.md")) == \
        ["vaswani-2017-attention.md"]


def test_unmerge_does_not_reverse_acl_union(minted_pair, env_with_ce):
    wsa, wsb = minted_pair
    run_script("merge.py", str(wsb), "--as-origin", "labb",
               "--acl", "union", "--workspace", str(wsa), env=env_with_ce)
    run_script("merge.py", "--apply", "labb",
               "--workspace", str(wsa), env=env_with_ce)
    run_script("unmerge.py", "--origin", "labb",
               "--workspace", str(wsa), env=env_with_ce)
    run_script("unmerge.py", "--origin", "labb", "--apply",
               "--workspace", str(wsa), env=env_with_ce)
    fm = (wsa / "wiki" / "entities" / "aspirin.md").read_text().split("---", 2)[1]
    assert "pharma" in fm
    assert "chem" in fm
    assert "wikidata:Q18216" not in fm


def test_allow_iris_namespaced_origin_is_not_a_claimer(
        minted_pair, env_with_ce, tmp_path):
    """An --allow-iris-namespaced origin never unions, so it must not
    keep a pair alive after the origin that actually unioned is unmerged."""
    wsa, wsb = minted_pair
    parent = (wsa / "wiki" / "entities" / "aspirin.md").read_text()
    listed = parent.split("iri: ", 1)[1].split("\n", 1)[0].strip()
    allow = tmp_path / "allow.txt"
    allow.write_text(f"{listed}\n")
    # wsb's aspirin match is listed (collapses); ibuprofen is unlisted.
    run_script("merge.py", str(wsb), "--as-origin", "labb",
               "--allow-iris", str(allow), "--workspace", str(wsa),
               env=env_with_ce)
    run_script("merge.py", "--apply", "labb",
               "--workspace", str(wsa), env=env_with_ce)
    persisted = json.loads(
        (wsa / ".curator" / "merges" / "labb.json").read_text())
    iris = [r.get("canonical_iri") or r.get("incoming_iri")
            for r in persisted["identity_reconciliations"]]
    assert listed in iris
    assert not any(i and "ibuprofen" in i for i in iris)
    run_script("unmerge.py", "--origin", "labb",
               "--workspace", str(wsa), env=env_with_ce)
    run_script("unmerge.py", "--origin", "labb", "--apply",
               "--workspace", str(wsa), env=env_with_ce)
    text = (wsa / "wiki" / "entities" / "aspirin.md").read_text()
    assert "wikidata:Q18216" not in text
    assert (wsa / "wiki" / "entities" / "ibuprofen-from-labb.md").is_file() is False
    # namespaced first-seen ibuprofen is a pure import, removed.
