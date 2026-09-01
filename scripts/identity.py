#!/usr/bin/env python3
"""identity.py — IRI-keyed entity reconciliation helpers (U1 / U4).

Pure functions used by merge.py, mirroring reconcile.py's split: the
reconciliation *rules* live here so they can be unit-tested in isolation,
and merge.py stays the orchestrator.

Where reconcile.py keys pages by filename stem (relative path), this module
keys *entities* by their stable workspace IRI (curiosity-engine U1). Two
entity pages are the same real-world entity when they share an `iri:`, OR
when their `same_as` maps share at least one `authority:id` pair. That lets
two wikis reconcile an entity that lives under different slugs, and lets a
shard (U4) rejoin its parent on its seam IRIs.

The IRI registry is curiosity-engine's `.curator/identifiers.db` `entities`
table:

    entities(iri PK, entity_class, page_path, same_as_json, status, resolved_at)

but the *reliable* carrier across a merge is entity-page frontmatter
(`iri`, `same_as`, `entity_class`) — sharing-safe exports ship the wiki
tree, not the db. We therefore read identity from both: frontmatter always,
the db when present.

Backward-compatible by construction: a wiki with no minted IRIs and no
`iri:`/`same_as:` frontmatter yields an empty identity index, so merge.py's
stem-based flow runs exactly as before. Identity reconciliation is additive.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

_ce_scripts = os.environ.get("CURIOSITY_ENGINE_SCRIPTS_DIR")
if _ce_scripts and _ce_scripts not in sys.path:
    sys.path.insert(0, _ce_scripts)
try:
    from naming import read_frontmatter  # type: ignore
except ImportError as e:
    sys.stderr.write(f"identity.py: cannot import naming.py ({e})\n")
    raise


# --- same_as parsing / formatting -----------------------------------------


def parse_same_as(value) -> dict:
    """Normalise a same_as value to an ``{authority: id}`` dict.

    Accepts the three shapes that reach us:
      - frontmatter bracket-list parsed by naming.read_frontmatter →
        ``["pubchem:CID2244", "wikidata:Q18253"]``
      - a JSON-object string from the db's ``same_as_json`` column →
        ``'{"pubchem":"CID2244"}'``
      - an already-parsed dict (pass-through)

    Mirrors curiosity-engine identifier_cache._parse_same_as so both sides
    agree on the wire form. Returns {} for None/empty/garbage.
    """
    if not value:
        return {}
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items() if v is not None}
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return {}
        # A db same_as_json column is a JSON object; a bare frontmatter
        # scalar like "pubchem:CID2244" is not JSON.
        if s.startswith("{") or s.startswith("["):
            try:
                return parse_same_as(json.loads(s))
            except (json.JSONDecodeError, ValueError):
                return {}
        return _pairs_to_dict([s])
    if isinstance(value, (list, tuple)):
        return _pairs_to_dict(value)
    return {}


def _pairs_to_dict(items) -> dict:
    out: dict = {}
    for item in items:
        k, _, v = str(item).partition(":")
        if k.strip() and v.strip():
            out[k.strip()] = v.strip()
    return out


def union_same_as(a: dict | None, b: dict | None) -> dict:
    """Union two same_as maps, dropping None values.

    Matches curiosity-engine write_entity's merge semantics: existing first,
    then update with the incoming map (incoming wins on key conflict).
    """
    merged = dict(a or {})
    if b:
        merged.update({k: v for k, v in b.items() if v is not None})
    return merged


def format_same_as_list(same_as: dict) -> str:
    """Render a same_as dict as a frontmatter bracket-list string.

    ``{"pubchem": "CID2244", "wikidata": "Q18253"}`` →
    ``"[pubchem:CID2244, wikidata:Q18253]"`` (sorted, deterministic).
    """
    pairs = sorted(f"{k}:{v}" for k, v in same_as.items())
    return "[" + ", ".join(pairs) + "]"


# --- loading identity from pages and from the db --------------------------


def _slug(rel: str) -> str:
    return Path(rel).stem


def load_page_entities(wiki_dir: Path) -> list[dict]:
    """Scan a wiki tree for entity pages carrying identity frontmatter.

    An entity page is any page with an `iri:` or a non-empty `same_as:`.
    Returns records:
        {rel, slug, iri, same_as (dict), entity_class}
    """
    out: list[dict] = []
    if not wiki_dir.is_dir():
        return out
    for p in sorted(wiki_dir.rglob("*.md")):
        rel = str(p.relative_to(wiki_dir))
        if any(seg.startswith(".") for seg in rel.split(os.sep)):
            continue
        try:
            fm, _ = read_frontmatter(p.read_text(errors="replace"))
        except OSError:
            continue
        iri = (fm.get("iri") or "").strip() if isinstance(fm.get("iri"), str) \
            else fm.get("iri")
        same_as = parse_same_as(fm.get("same_as"))
        if not iri and not same_as:
            continue
        out.append({
            "rel": rel,
            "slug": _slug(rel),
            "iri": iri or None,
            "same_as": same_as,
            "entity_class": (fm.get("entity_class") or "concept"),
        })
    return out


def load_db_entities(db_path: Path) -> list[dict]:
    """Read the `entities` table read-only.

    Opens a normal connection and sets ``PRAGMA query_only=ON`` — NOT the
    ``mode=ro`` URI, which hangs on live WAL-mode dbs (curiosity-engine hit
    this). Tolerates a missing db or missing table → returns [].
    """
    if not db_path.is_file():
        return []
    out: list[dict] = []
    conn = None
    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA query_only=ON")
        cur = conn.execute(
            "SELECT iri, entity_class, page_path, same_as_json "
            "FROM entities"
        )
        for iri, entity_class, page_path, same_as_json in cur.fetchall():
            out.append({
                "rel": page_path or None,
                "slug": _slug(page_path) if page_path else None,
                "iri": iri,
                "same_as": parse_same_as(same_as_json),
                "entity_class": entity_class or "concept",
            })
    except sqlite3.DatabaseError:
        return []
    finally:
        if conn is not None:
            conn.close()
    return out


def build_receiver_index(wiki_dir: Path, db_path: Path) -> dict:
    """Build the receiver-side identity index from db + page frontmatter.

    Returns:
        {
          "by_iri":  {iri: record},
          "by_pair": {"auth:id": record},
          "records": [record, ...],
        }

    A record is canonicalised to the on-disk page when one exists:
        {iri, rel, slug, same_as (dict, unioned), entity_class}

    Page frontmatter wins for `rel`/`slug` (it is the file that actually
    exists); the db contributes any same_as pairs the frontmatter lacks and
    fills in identities whose page wasn't scanned.
    """
    by_iri: dict[str, dict] = {}

    def _merge_record(rec: dict, *, prefer_rel: bool) -> None:
        iri = rec.get("iri")
        if not iri:
            return
        cur = by_iri.get(iri)
        if cur is None:
            by_iri[iri] = {
                "iri": iri,
                "rel": rec.get("rel"),
                "slug": rec.get("slug"),
                "same_as": dict(rec.get("same_as") or {}),
                "entity_class": rec.get("entity_class") or "concept",
            }
            return
        cur["same_as"] = union_same_as(cur["same_as"], rec.get("same_as"))
        if prefer_rel and rec.get("rel"):
            cur["rel"] = rec["rel"]
            cur["slug"] = rec["slug"]
        elif not cur.get("rel") and rec.get("rel"):
            cur["rel"] = rec["rel"]
            cur["slug"] = rec["slug"]

    # db first (lower precedence for rel), then pages (authoritative rel).
    for rec in load_db_entities(db_path):
        _merge_record(rec, prefer_rel=False)
    for rec in load_page_entities(wiki_dir):
        _merge_record(rec, prefer_rel=True)

    by_pair: dict[str, dict] = {}
    for rec in by_iri.values():
        for k, v in rec["same_as"].items():
            by_pair.setdefault(f"{k}:{v}", rec)

    return {"by_iri": by_iri, "by_pair": by_pair,
            "records": list(by_iri.values())}


# --- matching incoming entities against the receiver ----------------------


def match_identities(
    incoming_wiki_dir: Path,
    receiver_index: dict,
    *,
    incoming_db_path: Path | None = None,
) -> list[dict]:
    """Reconcile incoming entity pages against the receiver identity index.

    Match priority: shared `iri` first, then any overlapping `same_as`
    pair. Only incoming entities that resolve to an *existing* receiver
    identity are returned (a first-seen entity has nothing to collapse into
    and flows through the normal page path).

    Each reconciliation:
        {
          incoming_rel, incoming_slug, incoming_iri,
          canonical_rel, canonical_slug, canonical_iri,
          match_kind: "iri" | "same_as",
          shared_pairs: ["auth:id", ...],   # for same_as matches
          incoming_same_as: {auth: id, ...},  # this origin's claim-set
          union_same_as: {auth: id, ...},
          entity_class,
        }

    The db is consulted only to enrich an incoming page's same_as when the
    incoming side shipped one; the *match* is always anchored on incoming
    page frontmatter, because that is what carries into the staged tree.
    """
    by_iri = receiver_index["by_iri"]
    by_pair = receiver_index["by_pair"]

    # Optional: incoming db enriches incoming same_as by iri.
    incoming_db_by_iri: dict[str, dict] = {}
    if incoming_db_path is not None:
        for rec in load_db_entities(incoming_db_path):
            if rec.get("iri"):
                incoming_db_by_iri[rec["iri"]] = rec

    out: list[dict] = []
    for inc in load_page_entities(incoming_wiki_dir):
        inc_iri = inc.get("iri")
        inc_same_as = dict(inc.get("same_as") or {})
        if inc_iri and inc_iri in incoming_db_by_iri:
            inc_same_as = union_same_as(
                incoming_db_by_iri[inc_iri].get("same_as"), inc_same_as)

        match = None
        match_kind = None
        shared_pairs: list[str] = []

        if inc_iri and inc_iri in by_iri:
            match = by_iri[inc_iri]
            match_kind = "iri"
        if match is None and inc_same_as:
            for k, v in inc_same_as.items():
                key = f"{k}:{v}"
                if key in by_pair:
                    match = by_pair[key]
                    match_kind = "same_as"
                    break
        if match is None:
            continue

        if match_kind == "same_as" or match.get("same_as"):
            shared = set(inc_same_as.items()) & set(match["same_as"].items())
            shared_pairs = sorted(f"{k}:{v}" for k, v in shared)

        out.append({
            "incoming_rel": inc["rel"],
            "incoming_slug": inc["slug"],
            "incoming_iri": inc_iri,
            "canonical_rel": match.get("rel"),
            "canonical_slug": match.get("slug"),
            "canonical_iri": match.get("iri"),
            "match_kind": match_kind,
            "shared_pairs": shared_pairs,
            "incoming_same_as": dict(inc_same_as),
            "union_same_as": union_same_as(match.get("same_as"), inc_same_as),
            "entity_class": (match.get("entity_class")
                             or inc.get("entity_class") or "concept"),
        })
    return out


# --- apply-time receiver mutation -----------------------------------------


def upsert_entity_union(db_path: Path, iri: str, *, entity_class: str,
                        page_path: str | None, same_as: dict,
                        status: str = "ok") -> None:
    """Union `same_as` into the receiver `entities` table for `iri`.

    Mirrors curiosity-engine write_entity: read existing, union same_as
    (existing first, incoming wins), INSERT OR REPLACE. Creates the db and
    table if absent so a receiver that never used the identifier features
    still persists reconciled identity for future merges. Writes use a
    normal (writable) connection — query_only is for reads only.
    """
    import datetime as _dt
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=5)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS entities ("
            "iri TEXT PRIMARY KEY, entity_class TEXT NOT NULL, "
            "page_path TEXT, same_as_json TEXT, status TEXT NOT NULL, "
            "resolved_at TEXT NOT NULL)"
        )
        cur = conn.execute(
            "SELECT page_path, same_as_json FROM entities WHERE iri = ?",
            (iri,))
        row = cur.fetchone()
        merged = {}
        if row is not None:
            merged = parse_same_as(row[1])
            if page_path is None:
                page_path = row[0]
        merged = union_same_as(merged, same_as)
        now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            "INSERT OR REPLACE INTO entities(iri, entity_class, page_path, "
            "same_as_json, status, resolved_at) VALUES (?, ?, ?, ?, ?, ?)",
            (iri, entity_class, page_path,
             json.dumps(merged, separators=(",", ":"), sort_keys=True),
             status, now))
        conn.commit()
    finally:
        conn.close()


def read_db_entity(db_path: Path, iri: str) -> dict | None:
    """Read one `entities` row read-only. None if missing/unreadable.

    Uses ``PRAGMA query_only=ON`` on a normal connection (not ``mode=ro``,
    which hangs on live WAL-mode dbs).
    """
    if not iri or not db_path.is_file():
        return None
    conn = None
    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA query_only=ON")
        cur = conn.execute(
            "SELECT iri, entity_class, page_path, same_as_json "
            "FROM entities WHERE iri = ?",
            (iri,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        iri_v, entity_class, page_path, same_as_json = row
        return {
            "rel": page_path or None,
            "slug": _slug(page_path) if page_path else None,
            "iri": iri_v,
            "same_as": parse_same_as(same_as_json),
            "entity_class": entity_class or "concept",
        }
    except sqlite3.DatabaseError:
        return None
    finally:
        if conn is not None:
            conn.close()


def write_entity_same_as(db_path: Path, iri: str, same_as: dict, *,
                         delete_if_empty: bool = False) -> str:
    """Replace an `entities` row's ``same_as_json``.

    Returns ``updated``, ``deleted``, or ``missing``. Does not create a
    db or row — unmerge only mutates what merge wrote.
    """
    if not iri or not db_path.is_file():
        return "missing"
    import datetime as _dt
    conn = sqlite3.connect(str(db_path), timeout=5)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        cur = conn.execute(
            "SELECT iri FROM entities WHERE iri = ?", (iri,))
        if cur.fetchone() is None:
            return "missing"
        if delete_if_empty and not same_as:
            conn.execute("DELETE FROM entities WHERE iri = ?", (iri,))
            conn.commit()
            return "deleted"
        now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            "UPDATE entities SET same_as_json = ?, resolved_at = ? "
            "WHERE iri = ?",
            (json.dumps(same_as, separators=(",", ":"), sort_keys=True),
             now, iri),
        )
        conn.commit()
        return "updated"
    except sqlite3.DatabaseError:
        return "missing"
    finally:
        conn.close()


# --- unmerge reversal (claim-set / last-claimer) --------------------------


def rec_matches_entity(r: dict, *, canonical_iri: str | None,
                       canonical_rel: str | None) -> bool:
    """True if identity-reconciliation `r` is about this canonical entity."""
    if not isinstance(r, dict):
        return False
    iri = r.get("canonical_iri") or ""
    if canonical_iri and iri and iri == canonical_iri:
        return True
    rel = r.get("canonical_rel") or ""
    if canonical_rel and rel and rel == canonical_rel:
        return True
    return False


def _read_manifest_file(path: Path) -> dict | None:
    try:
        m = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return m if isinstance(m, dict) else None


def load_active_merge_manifests(
        merges_dir: Path, *,
        exclude_origin: str | None = None) -> list[tuple[str, dict]]:
    """Load `.curator/merges/*.json` (not `.archive/`)."""
    out: list[tuple[str, dict]] = []
    if not merges_dir.is_dir():
        return out
    for p in sorted(merges_dir.glob("*.json")):
        origin = p.stem
        if exclude_origin and origin == exclude_origin:
            continue
        m = _read_manifest_file(p)
        if m is not None:
            out.append((origin, m))
    return out


def load_archived_merge_manifests(merges_dir: Path) -> list[tuple[str, dict]]:
    """Load `.curator/merges/.archive/*.json` (already-unmerged origins).

    Used only to recover the pre-federation ``prior_page_same_as``; archived
    origins are not remaining claimers.
    """
    out: list[tuple[str, dict]] = []
    archive = merges_dir / ".archive"
    if not archive.is_dir():
        return out
    for p in sorted(archive.glob("*.json")):
        m = _read_manifest_file(p)
        if m is not None:
            out.append((p.stem, m))
    return out


def collect_remaining_claims(
        other_manifests: list[tuple[str, dict]], *,
        canonical_iri: str | None,
        canonical_rel: str | None) -> tuple[dict, dict]:
    """Union remaining origins' ``incoming_same_as`` (apply-order, incoming-wins).

    Returns ``(remaining_map, claimers)`` where ``claimers`` maps
    ``"auth:id"`` to origin names. Manifests that predate
    ``incoming_same_as`` are skipped — they are not claimers.
    """
    recs: list[tuple[str, str, dict]] = []
    for origin, m in other_manifests:
        applied = m.get("applied_at") or ""
        for r in m.get("identity_reconciliations") or []:
            if not rec_matches_entity(
                    r, canonical_iri=canonical_iri,
                    canonical_rel=canonical_rel):
                continue
            inc = r.get("incoming_same_as")
            if not isinstance(inc, dict):
                continue
            recs.append((applied, origin, inc))
    recs.sort(key=lambda t: t[0])
    remaining: dict = {}
    claimers: dict[str, list[str]] = {}
    for _, origin, inc in recs:
        remaining = union_same_as(remaining, inc)
        for k, v in inc.items():
            key = f"{k}:{v}"
            claimed = claimers.setdefault(key, [])
            if origin not in claimed:
                claimed.append(origin)
    return remaining, claimers


def original_same_as(
        manifests: list[tuple[str, dict]], *,
        canonical_iri: str | None,
        canonical_rel: str | None) -> dict | None:
    """Earliest ``prior_page_same_as`` for this entity, or None if unknown."""
    candidates: list[tuple[str, dict]] = []
    for _, m in manifests:
        applied = m.get("applied_at") or ""
        for r in m.get("identity_reconciliations") or []:
            if not rec_matches_entity(
                    r, canonical_iri=canonical_iri,
                    canonical_rel=canonical_rel):
                continue
            prior = r.get("prior_page_same_as")
            if isinstance(prior, dict):
                candidates.append((applied, prior))
    if not candidates:
        return None
    candidates.sort(key=lambda t: t[0])
    return dict(candidates[0][1])


def plan_same_as_reversal(
        *,
        incoming_same_as: dict,
        added_same_as: dict | None,
        current: dict,
        original: dict | None,
        remaining: dict,
        remaining_claimers: dict | None = None) -> dict:
    """Decide which ``same_as`` pairs to drop, restore, or leave.

    Last-claimer rule, inverse of ``union_same_as`` (incoming-wins):

    For each pair this origin independently claimed (``incoming_same_as``):
      - live page no longer has that exact k→v → skip (user edit / already gone)
      - a remaining origin still claims the same k→v → keep
      - a remaining origin claims a different value for k → restore it
      - the pre-federation receiver had this k→v → keep
      - the pre-federation receiver had a different value for k → restore it
      - otherwise remove (this origin is the last claimer)
      - original unknown: only remove if ``added_same_as`` shows this
        origin introduced the pair; otherwise skip (imprecise)

    ``projects:`` / folded wikilinks / page bodies are out of scope.
    """
    remaining = remaining or {}
    current = current or {}
    incoming_same_as = incoming_same_as or {}
    claimers = remaining_claimers or {}
    precise = True
    remove: dict = {}
    restore: dict = {}
    keep_claimed: list[dict] = []
    keep_original: list[dict] = []
    skip_user_edit: list[dict] = []
    skip_imprecise: list[dict] = []

    for k, v in incoming_same_as.items():
        cur = current.get(k)
        if cur != v:
            skip_user_edit.append({
                "key": k, "incoming": v, "current": cur,
            })
            continue
        rem = remaining.get(k)
        if rem == v:
            keep_claimed.append({
                "key": k, "value": v,
                "by": list(claimers.get(f"{k}:{v}") or []),
            })
            continue
        if rem is not None:
            restore[k] = rem
            continue
        if original is not None:
            orig = original.get(k)
            if orig == v:
                keep_original.append({"key": k, "value": v})
            elif orig is not None:
                restore[k] = orig
            else:
                remove[k] = v
            continue
        if added_same_as is not None and added_same_as.get(k) == v:
            remove[k] = v
        else:
            precise = False
            skip_imprecise.append({"key": k, "value": v})

    return {
        "precise": precise,
        "remove": remove,
        "restore": restore,
        "keep_claimed": keep_claimed,
        "keep_original": keep_original,
        "skip_user_edit": skip_user_edit,
        "skip_imprecise": skip_imprecise,
    }


def apply_same_as_reversal(
        current: dict, *,
        incoming_same_as: dict,
        remove: dict,
        restore: dict) -> dict:
    """Apply a reversal plan to a live ``same_as`` map.

    Re-checks the live values so an edit between stage and apply is not
    clobbered: a remove/restore only fires when ``current[k]`` still
    equals this origin's incoming value.
    """
    new = dict(current or {})
    incoming_same_as = incoming_same_as or {}
    for k, v in (remove or {}).items():
        if new.get(k) == v:
            del new[k]
    for k, to_val in (restore or {}).items():
        if_val = incoming_same_as.get(k)
        if new.get(k) == if_val:
            new[k] = to_val
    return new
