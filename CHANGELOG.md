# Changelog

## v0.8.4 — 2026-09-29

**Migration:** none. **Breaking:** none.

Documents the **Phase 3 installer / UI-boundary contract** that already landed
on `main` (`docs/INSTALLER-CONTRACT.md`) and reconciles integration **pin
tables** with the upcoming skill-shell release wave:

| Component | Pin | Notes |
|---|---|---|
| curiosity-merge | **v0.8.4** (this release) | Docs + pin bump only |
| curiosity-engine | **v1.9.0** (pending tag) | Code tip `3018580` (`301858011097d6811801785380d056ecdd158dc9`); CE docs PR open for release notes |
| Switchbay | **v0.13.0** (pending) | Consumer of CE/CM/okstratr embeds — record when SB tags |

### Clarified

- **Installer contract:** bare-skill, Switchbay, and okbay install CM headlessly
  via `scripts/setup.sh`; pins are tag **and** full commit; no floating
  `main`/`latest` production pins.
- **UI boundary:** CM owns **no** product UI (no PWA/QML/HTML/rail/tabs/
  settings/daemon/proxy/registry). Shells present CM artifacts; CM returns
  exit status + filesystem outputs.
- Pin table previously froze CE at `v1.8.2` / CM at `v0.8.3`; product installers
  should move to the pair above once CE **v1.9.0** and CM **v0.8.4** are tagged.
  Until CE is tagged, record “pending CE v1.9.0” at tip `3018580` consistently.

### Unchanged

- Runtime merge/export/hydrate behavior (still v0.8.3 semantics).
- `docs/architecture.md` federation layout (no contradiction with Phase 3).

## v0.8.3 — 2026-09-05

Closes the receiving half of the out-of-band source contract, and fixes a
bug that made `hydrate-vault --apply` incapable of succeeding.

### `hydrate-vault --apply` could never succeed (bug)

`_local_ingest` invoked curiosity-engine's `local_ingest.py` with the raw
file as a **positional** argument. That positional is a *directory*, so
every call returned `not a directory` and every fetch path — arXiv,
bioRxiv/chemRxiv, open-access — ended in a failed ingest after a
successful download. It now passes `--file`.

This was invisible because no test exercised `--apply`; the two existing
hydrate tests covered dry-run categorization only. Verified against
curiosity-engine v1.5.0 and v1.6.1: the failure was identical on both, so
it predates the structured-dataset release.

`_local_ingest` now also returns the produced extraction's vault-relative
name rather than a bool, which is what makes adoption possible.

### Recovering links when sources travel separately

The safe sharing path is a bytes-free export plus an out-of-band source
transfer (`docs/trust-model.md` § Licensing). The publishing side of that
was implemented; the receiving side was not — nothing ever noticed that a
source had arrived, so stubs stayed `vault_missing: true` forever and
their citations never resolved.

`hydrate-vault` now **reconciles before it fetches**:

| State of the cited file | Outcome |
|---|---|
| present, sha256 matches the origin's record | flag cleared, `vault_provenance: verbatim` |
| present, no recorded sha to verify | flag cleared, `vault_provenance: present-unverified` |
| present, sha256 diverges | reported; **stub stays flagged** |

The divergent case is deliberate: right name with wrong bytes is threat
T3, not a hydration success, so it is surfaced rather than cleared.

### `--adopt PAGE=FILE` for independently re-acquired sources

A paywalled paper obtained through institutional access is a different
artifact from the origin's extraction — different bytes, different
filename — so no sha check can ever pass. Adoption ingests the receiver's
copy through curiosity-engine and repoints `(vault:<origin-name>)` at the
local extraction across every wiki page, reusing the citation-rewriting
convention `merge.py` already applies when an incoming vault file lands
under another name.

The stub records `vault_provenance: reacquired` and
`vault_reacquired_as`, and **keeps the origin's `vault_sha256`**. Nothing
is overwritten to make the graph look whole: the divergence stays
auditable. Repeatable, and a no-op in dry-run.

### `--include-vault all` documented accurately

The docs said "everything"; the implementation ships only vault files the
exported pages actually **cite**. The implementation is right — an export
is a subgraph, and pulling in uncited vault content would leak sources the
shard has no claim to — so the docs were corrected. `all` widens the
*licensing* filter, not the scope.

Stated explicitly for the first time: raw originals (the PDF behind an
extraction, or the `kept_as` file a curiosity-engine v1.6 structured
extraction replays from) are never cited, so they ride along in no mode.
They travel out of band, which is what the reconcile path above is for.

### Tests

Three added, all offline: verbatim reconcile, refusal to clear on sha
divergence, and adoption repointing every citing page (with a dry-run
no-mutation assertion). 207 pass.

## v0.8.2 — 2026-09-01

Unmerge now reverses identity `same_as` unions, including the multi-origin overlap the v0.6.0 plan left out of scope.

### Unmerge reverses identity reconciliation

`merge --apply` unions an incoming entity's `same_as` into a **receiver-native** page and `.curator/identifiers.db`. Unmerge previously only removed origin-tagged imports, so those unions leaked. Apply now records `incoming_same_as` (this origin's claim-set), `added_same_as`, `prior_page_same_as`, and `db_row_existed_before` on each `identity_reconciliations[]` entry — including apply-time rematches from the merge queue, so FIFO order cannot hide a union.

Unmerge subtracts that origin's pairs from the canonical page and db using a **last-claimer** rule, not the apply-time delta:

- A pair stays while any *active* origin still lists it in `incoming_same_as`, or it was on the page before any of these merges (earliest `prior_page_same_as`, including archived manifests so unmerging the introducer first does not freeze later origins' priors as "original").
- The last remaining claimer drops it.
- Incoming-wins key conflicts restore the remaining origin's (or original) value.
- A user edit of that authority is never clobbered (T5).
- Manifests that predate the new keys are left alone (`precise: false`); no guessing.
- `--keep-identity-same-as` keeps the mappings as factual knowledge.
- Unchanged identity review copies (`<stem>-from-<origin>.md`) are auto-removed; stem same-topic collisions stay manual.

Not reversed (deliberate, so v0.8.0/v0.8.1 shard-rejoin guards stay intact): `projects:` ACL, folded source-stub wikilinks, identical-body drops (`final_rel: null` is skipped, completing the v0.8.1 unmerge crash fix in `_classify_imports`). `--allow-iris` namespaced origins never unioned and are not claimers.

204 tests passing (was 189 at v0.8.1).

## v0.8.1 — 2026-09-01

Follow-on to the v0.8.0 shard-rejoin hardening: the identical-body skip covered stubs that came back byte-identical, but not the ones parallel CURATE had rewired.

### Source stubs differing only in wikilinks fold instead of forking

A `wiki/sources/<stem>.md` stub is a thin provenance page — title, citation, and the reciprocal `[[page]]` links back to whatever cites it. Parallel shard CURATE rewires exactly those links, so the same stub returns from N shards with N different link sets and nothing else changed. Body-identity said "different", the similarity heuristic said `same_topic`, and every shard forked the stub: ~N duplicate `sources/<stem>-from-<origin>.md` per shard flooding the parent on rejoin.

`classify_collision` now returns a fourth kind, `source_link_fold`, when both sides are source stubs (`type: source`, or a page under `sources/`) whose bodies match once `[[wikilinks]]`, their list markers and their bare section label (`Cited by:`) are stripped. Those stubs never stage — not as a live page, not as a review copy under `collisions/`. On apply the incoming links are appended to the receiver's stub as `- [[link]]` bullets, deduped against what the stub already carries, so N shards land one stub with the union of their links and zero `*-from-<origin>.md`.

The fold is additive and bounded:

- Only link bullets are added. The receiver's prose, frontmatter and existing links are untouched, which makes re-folding the same links a no-op.
- Only plain slug targets are accepted (`[A-Za-z0-9][A-Za-z0-9 ._/-]{0,127}`, no `..`). Folded links are the one piece of incoming text that reaches a live page without passing through staging, so the accepted alphabet is deliberately tight.
- Links land inside the untrusted frame when the receiving stub carries one.
- The apply path folds too (`_copy_wiki_avoiding_clobber`), so a stub that was new to the receiver when a shard staged, but landed from an earlier origin in the queue by the time it applied, folds rather than forking. Queue order does not change the result.
- The identity path (U1/U4) folds on the same rule, so a minted `iri:` on a stub cannot turn shard link-rewiring back into a fork.
- `--acl` applies to a fold exactly as to an identical-body drop; `keep-receiver` (the default) leaves `projects:` alone.

Real content collisions are unchanged: any other differing line on a stub, and every collision on entity / concept / analysis pages, still takes the existing `same_topic` / `different_topic` review-copy path. ACL, the apply queue, and the `--allow-iris` skip are untouched.

Folded stubs are listed in the audit under `## Page-name collisions → Source stubs — wikilinks folded` with the links folded in, and recorded in the manifest as `source_link_folds`.

### Fixes

`unmerge.py` no longer crashes on a manifest containing pages that never landed as files (`final_rel: null` — identical-body drops, and now folds).

189 tests passing (was 184 at v0.8.0).

## v0.8.0 — 2026-08-31

Federation hardening after an 8-way parallel-shard curate/rejoin.

### Never materialize `*-from-<origin>.md` when bodies are identical

`classify_collision` now keys `identical` on **body** identity (frontmatter-stripped; merge framing ignored), not whole-file sha256. Identity collapse skips the review copy when the incoming body matches the canonical page. `--apply` will not copy those files into live `wiki/`. Parallel shard curation routinely bumps `updated:` / `projects:` on pages it never rewrote; those no longer flood the parent as `stem-from-shardN.md`.

Different-body same-topic collisions and different-body identity matches still land a review copy, as before.

### Stem fallback warns; `--iri-required` refuses

Every stem collision (the un-minted fallback) prints `merge: stem fallback (no IRI): …` on stderr and is listed in the audit. `--iri-required` completes staging so the audit is readable, then exits 1 and refuses `--apply`.

### `--acl keep-receiver|union|intersect`

When two pages become one (identity collapse or identical-body drop), `projects:` on the survivor defaults to **keep-receiver** (enterprise-safe; no extra review copies). `--acl union` adds incoming tags. `--acl intersect` sets survivor = receiver ∩ incoming, but only on `type: analysis` pages. New pages with no collision keep their incoming tags either way.

### Merge queue + one parent rebuild

Successful stages append the origin to `.curator/merge-queue.json`. `merge.py --apply-queue` applies FIFO with dest-clobber guards and rebuilds kuzu **once**. `--no-rebuild` skips the per-`--apply` rebuild for hand-rolled batches.

### `--allow-iris FILE` (laptop → org trunk)

Listed IRIs may collapse onto / land at canonical trunk slugs. Unlisted IRIs are origin-namespaced (`<stem>-from-<origin>.md`) and do not union `same_as` into a trunk entity.

Vault sha256 dedup is unchanged.

184 tests passing (was 172 at v0.7.0).

## v0.7.0 — 2026-07-05

Three small, standalone upgrades to `subgraph_export.py`, motivated by
composition with external callers (a workbench app driving exports
programmatically) but each useful to CLI users on its own.

### `--pages-file` — explicit page-set scope

A fourth scope selector alongside `--project` / `--page` / `--origin`
(mutually exclusive with them). The file is a JSON list of page refs,
each either a bare stem (`"transformer"`) or a wiki-relative path with
optional `.md` (`"concepts/transformer"`). Refs resolve with the same
case-insensitive stem/path matching as `--page`.

- Deliberately **no 1-hop expansion** — the file IS the scope
  (`--include-1-hop` with `--pages-file` errors at parse time).
- Refs that match nothing fail the export with **every miss listed**;
  new `--skip-missing` reports misses on stderr and continues with the
  matched pages instead.
- Manifest `scope.kind` is `pages-file`; duplicates in the file
  de-dupe; order-preserving resolution.

### Figure assets ride along (fixes silently-broken images)

Exports previously shipped wiki pages but not the figure assets they
embed, so rendered/published pages arrived with broken images. Image
embeds are now collected the same way `(vault:...)` citations are:
every `figures/_assets/` file referenced from an in-scope page is
copied into the export at the same wiki-relative path.

- Covers both reference forms the wiki format uses (per
  curiosity-engine's `wiki_render.py`): markdown `![alt](path)` and
  Obsidian transclusion `![[path]]` / `![[path|alt]]`, with the same
  path normalization (`figures/_assets/X` / `_assets/X` / bare `X.png`).
- Missing assets **warn, never fail** — the embed was already broken in
  the source wiki, and assets are regenerable via curiosity-engine's
  `figures.py regen`. External image URLs and non-image transclusions
  are ignored (no copy, no warning).
- Embed paths get the same traversal guard as vault citations.
- New manifest key `scope_figures` (wiki-relative list, like
  `scope_pages` / `scope_vault`); additive, so the manifest schema
  version stays at 2. The stdout summary now reports the figure count.

### Headless/local invocation audited and documented

For non-interactive callers (local-to-local transfers where the
publish-oriented licensing/PII gates are wrong-purpose), the full
export code path was audited for prompts, TTY assumptions, and blocking
findings. Conclusion: **`--no-preflight --force` already fully covers
headless use** — no new flag needed.

- The interactive `[y/N/a]` prompt is the only stdin read on the export
  path and lives inside the preflight pass that `--no-preflight` skips
  entirely; `--force` removes the only other stop (non-empty
  destination). Every other exit is a clean nonzero error; nothing
  waits on a TTY. (`--clear-acks`' confirmation prompt is a separate
  management command, not an export path.)
- Documented in SKILL.md as the supported headless/local combination,
  with a regression test that runs an export with stdin closed
  (`stdin=DEVNULL`), PII-bearing content, and a non-empty destination.

### Tests

172 active tests passing (was 162 at v0.6.0). 10 new:

- `--pages-file` (6): exact-set export across all ref forms + no-1-hop,
  missing refs error lists all misses, `--skip-missing` continues,
  mutual exclusion with other scopes, `--include-1-hop` rejection,
  non-list JSON rejection.
- Figure embeds (4 incl. headless): all three embed path forms ship +
  out-of-scope figures stay behind, missing figure warns but succeeds,
  external URLs / non-image transclusions ignored, headless
  `--no-preflight --force` never prompts.

## v0.6.0 — 2026-06-02

IRI-keyed entity reconciliation and shard ingestion — the curiosity-merge
side of curiosity-engine v0.5.0's U1 (entity identity) and U4 (shard export)
contract. Reconciliation upgrades from slug-keyed to identity-keyed; the
stem queue becomes the fallback for un-minted pages.

### Identity reconciliation (U1)

New module `scripts/identity.py` (pure functions, mirrors `reconcile.py`).
Before the stem-collision pass, `merge` now matches incoming entity pages to
existing receiver identities by shared `iri` first, then by overlapping
`same_as` `authority:id` pair. Identity is read from both the receiver's
`.curator/identifiers.db` `entities` table and page frontmatter (`iri`,
`same_as`, `entity_class`) — exports ship the wiki tree, not the db, so
frontmatter is the reliable carrier.

- A matched entity collapses into the receiver's canonical page **regardless
  of slug**. It is not re-staged as a live page (its framed body is preserved
  under `collisions/<canonical-stem>-from-<origin>.md` for review), wikilinks
  to its slug are redirected to the canonical slug, and its `same_as` map is
  unioned into the receiver's page frontmatter and `entities` registry at
  apply time (mirroring curiosity-engine `write_entity`'s union semantics).
- The receiver `entities` table is read read-only via `PRAGMA query_only=ON`
  on a normal connection — NOT the `mode=ro` URI, which hangs on live
  WAL-mode dbs.
- **Backward-compatible by construction**: a wiki with no minted IRIs and no
  `iri:`/`same_as:` frontmatter yields an empty identity map, so the stem
  flow runs exactly as before.

### Shard ingestion (U4)

New `merge.py --import-shard <export.json> <shard-wiki> --as-origin <name>`.
Reads `seam_entities[].iri` from an `epoch_summary.py --shard` export and
reconciles on those seam IRIs — a seam entity the parent already holds
rejoins its canonical page instead of duplicating. Apply/abandon use the
normal merge verbs.

### Audit report

New `## Identity reconciliation` section, distinct from the stem-based
`## Page-name collisions`: which IRIs matched (by `iri` vs `same_as`), which
slugs collapsed, and — for shard imports — which were seam joins. New
manifest keys: `identity_reconciliations`, `is_shard_import`,
`shard_warnings`. Preserved across `--rerun-gates`.

### Tests

New `tests/test_identity.py`: 10 tests covering `identity.py`'s pure
functions in-process plus end-to-end same_as-overlap reconciliation,
same-IRI shard rejoin, `same_as` union into page + db on apply, and the
no-IRI backward-compat path. E2E fixtures mint real IRIs via
curiosity-engine's `identifier_cache.py mint-entity` so the reconciler reads
the genuine `entities` table.

## v0.5.0 — 2026-05-12

Two substantive Presidio enhancements from the v0.4.0 backlog.

### Combined-data inference detector (`gdpr_combined_inference`)

A new finding kind separate from `gdpr_likely_pii`. Detects entity
*combinations* that, together, identify a specific individual even
when no single entity alone would. Under GDPR Recital 26 (and similar
regimes), combined-data PII is independently actionable.

Three combination types:

| Combination | Window | What it catches |
|---|---|---|
| `PERSON_LOCATION_DATE` | 60 chars | Full identification: "Dr. Alice Johnson, born 1985, in Boston" |
| `PERSON_ORG` | 60 chars | Workplace ID: "Bob Smith works at Google" |
| `PERSON_AGE` | 40 chars | Age-tied ID: "Carol Davis, 51" |

**Windows are empirically tuned.** `tuning/inference_corpus.py` holds
267 labeled samples (positive / negative) across the three combination
types. `tuning/tune_inference_windows.py` sweeps a candidate range
(20–300 chars), measures precision/recall/F1 at each, and picks the
window maximizing F1. Re-run when changing the detector logic.

Result table from the v0.5.0 calibration:
- PERSON_LOCATION_DATE: window=60, F1=0.942, P=0.907, R=0.980
- PERSON_ORG: window=60, F1=0.715, P=0.611, R=0.863 (ORG NER is
  inherently noisier; surfaced as a signal, not gate)
- PERSON_AGE: window=40, F1=0.878, P=0.915, R=0.843

PERSON_LOCATION_DATE suppression rule: when a triple fires, the
constituent PERSON+DATE pair is suppressed from PERSON_AGE to avoid
double-counting the same identification.

PERSON_MEDICAL deferred: Presidio's MEDICAL_LICENSE recognizer matches
only the US DEA Certificate Number format. General medical license
patterns (NPI, RN-*, state MD-*) need custom recognizers we'd write
ourselves. Documented as a known limitation.

Severity follows the existing density model:
- Outside FETCHED markers → warn (user-typed content)
- Inside markers, ≥ 0.1 inference hits per 1000 chars → warn (dump)
- Inside markers, < 0.1 per 1000 chars → info (incidental)
- Region < 2000 chars → info (too short to trust density math)

Custom NLP config: Presidio's default mapping omits `ORG`, which
would silently kill PERSON_ORG detection. v0.5.0 configures
`NlpEngineProvider` with explicit `ORG → ORGANIZATION` mapping. ORG
gets a 0.4 confidence multiplier (vs 0.85 for PERSON/LOC) to reflect
that ORG NER is noisier.

### Multi-language Presidio support (opt-in per language)

- New `--presidio-language=CSV` flag on `subgraph_export.py`,
  `merge.py`, and `preflight.py` standalone CLI. Default `en`.
- Supported language codes mapped to recommended spaCy models:
  `en`, `fr`, `de`, `es`, `it`, `pt`, `nl`, `zh`, `ja`, `ru`.
- **Each language requires its corresponding spaCy model installed
  locally.** We deliberately do NOT auto-install (network + ~500MB
  per model — explicit consent matters). Unknown or uninstalled
  language → `is_available()` returns False with a clear install hint,
  and the CLI falls back to the regex GDPR detector with a stderr
  note.
- `setup.sh` now prints the per-language install commands after the
  English Presidio install offer. Users install only what they need.
- Multi-language analysis: when multiple languages are configured,
  the analyzer runs once per language and dedups results by
  `(start, end, entity_type)`. O(n_langs * region_len) cost; typically
  cheap for 1–2 languages.
- Cache key gains the language list (order-insensitive sort), so
  switching `--presidio-language` invalidates the cache properly.

### Manifest cache schema bump

The per-file Presidio cache now stores multiple findings per file
(`gdpr_likely_pii` + `gdpr_combined_inference` for the same file).
Cache schema bumped to v2 with backwards-compat for v1 entries
(`finding` singular hydrates as a one-element list). Samples still
never persisted.

### Tests

- 152 active tests passing (was 138 at v0.4.1). 14 new for:
  - Combined inference detection: PERSON+LOC+DATE, PERSON+ORG,
    PERSON_AGE suppression rule, density-aware severity (sparse=info,
    dense=warn).
  - Manifest-safety: combined-inference samples don't leak into
    manifest projection.
  - Multi-language: language model map coverage, cache key
    invalidates on language change, cache key order-insensitive,
    unknown language fails gracefully via `is_available()`.

### Documentation

- `docs/licensing.md` — Presidio section expanded with combined-
  inference table and multi-language install commands.
- `tuning/` directory committed for transparency: corpus + tuning
  script users can re-run to verify or recalibrate thresholds.

## v0.4.1 — 2026-05-08

Two small but real-user-relevant fixes from the v0.4.0 backlog.

### Manifest schema-version validation (receiver-side)

- `merge.py` now validates the incoming `_export-manifest.json`'s
  `schema_version` against `INCOMING_MANIFEST_KNOWN_VERSIONS = {1, 2}`.
  Unknown versions, missing `schema_version` field, missing required
  fields, corrupt JSON, or no manifest at all → all produce a
  warning on stderr and an entry under "Incoming manifest
  compatibility" in the audit report.
- **Best-effort, never refuses.** Python dict.get fallbacks already
  make the merge tolerant of unknown fields and missing fields; the
  validation just makes the situation visible to the user instead of
  silently muddling through.
- Required-field set: `schema_version`, `exported_at`, `scope_pages`.
  Missing any of these is a useful signal that something's wrong with
  the source export.

### License allowlist expansion

Added the genuinely-permissive licenses we'd left out:

- **GFDL** (`gfdl`, `gfdl-1.2`, `gfdl-1.3`) — Wikipedia content. Older
  Wikipedia articles are dual-licensed CC-BY-SA / GFDL; some
  derivatives are tagged GFDL only. Without this, every Wikipedia-
  derived vault file would silently fail `--include-vault=owned`.
- **Unlicense** (`unlicense`) — public-domain-equivalent declaration
  common on small open-source repos. Conceptually identical to CC0.
- **Zero-clause BSD** (`0bsd`, `bsd-0`) — public-domain-equivalent for
  code; same conceptual category as CC0/Unlicense.
- **Older CC versions** (`cc-by-1.0`, `-2.0`, `-2.5` and `cc-by-sa-1.0`,
  `-2.0`, `-2.5`) — for older content. We had only 3.0 and 4.0; older
  archives use earlier numbering.

GPL-family tokens deliberately remain excluded from the redistributable
allowlist to avoid contradictions with the GPL contagion detector;
users who genuinely want to ship GPL'd content can use
`redistributable: true` per file.

### Tests

- 138 active tests passing (was 129). 9 new across:
  - Manifest version validation (5): unknown version, missing fields,
    no manifest at all, corrupt manifest, known version no warning.
  - License allowlist (4): v0.4.1 tokens present, GFDL doesn't trip
    GPL detector, Unlicense doesn't trip GPL detector, GFDL/Unlicense/
    older-CC ride along under `--include-vault=owned`.

### Documentation

- `docs/licensing.md` now has a structured table of recognized open
  license tokens. Notes on what's deliberately NOT in the default
  allowlist (NC/ND/GPL family).

## v0.4.0 — 2026-05-06

Per-detector gating, persistent finding acks, license-symmetry, per-
citation quote density, and a real standalone audit CLI. The bundle
addresses the medium-priority items from the v0.3.0 critical-review
backlog.

### Per-detector gating policy

- **New `--refuse-on=VALUE` and `--accept-on=VALUE`** on `subgraph_export.py`.
  Each takes `all`, `none` (default), or a comma-separated kind list.
  Per-finding resolution: specific-kind in accept_on → accept; specific-
  kind in refuse_on → refuse; else `all` settings; else prompt.
  Contradictions (same kind in both CSVs, both `all`, unknown kind name,
  empty value) error at parse time.
- **Deprecated aliases**: `--strict` → `--refuse-on=all`,
  `--yes` → `--accept-on=all`. Still work; one-line stderr deprecation
  note when used. Mutually exclusive with their replacements.
- New `preflight.GatingPolicy` class encapsulates the resolution rule
  (testable in isolation; 11 unit tests).

### Persistent finding acks

- **`.curator/preflight-acks.json`** stores accepted findings keyed by
  sha256(file_sha256 + kind + summary). File-content drift invalidates
  acks naturally — re-review forced when the underlying content
  changes, no special handling needed.
- **Interactive prompt**: `[y/N/a]`. `a` = yes-and-remember.
- **`--remember-acks`** persists every accepted finding (whether via
  `--accept-on` or interactive `y`).
- Management: `--list-acks` and `--clear-acks` (interactive confirm
  unless paired with `--accept-on=all`).
- **Manifest-safe storage**: samples never persisted to the ack file.
  Verified by test: scan ack-file bytes for `@`, SSN, IBAN patterns
  → zero matches.

### License-consistency symmetry

- `check_license_consistency` now also flags **(restrictive license +
  URL on known OA domain)** as `info` severity. The user is being more
  conservative than necessary; nothing leaks; but the tag is probably
  wrong and they'd want to know.
- New `_OA_DOMAINS` list: arxiv, biorxiv, chemrxiv, medrxiv, plos, pmc,
  europepmc, openreview, aclanthology, doaj.
- Carve-out for arXiv URLs with empty license tag (the platform default
  is implicit; firing on every academic vault file would be noise).

### Quote-density per-citation

- Walk page in document order, attribute each block-quote to the
  nearest preceding `(vault:X)` citation.
- **Two thresholds, two findings**: `--quote-density-threshold` (single-
  source, default 0.25) and `--quote-density-page-threshold` (aggregate,
  default 0.50). Both warn-level. A page can produce both.
- Single-source finding uses the citation as `subject`, helping users
  see *which source* is over-quoted.
- Unattributed quotes (before any citation) bucketed separately with
  `(unattributed quotes)` subject label.

### Standalone preflight CLI

- **`preflight.py` is now a real audit command.** Read-only: never
  writes the export cache or ack file (those side effects belong to
  subgraph-export, not a one-shot audit).
- Full flag surface: `--workspace`, `--scope`, `--enable-presidio`,
  density thresholds, `--include-non-native`, `--show-acks`,
  `--clear-acks`, `--no-samples`, `--json`.
- **Exit codes for CI**: `0` clean or info-only, `1` any warn/block,
  `2` operational error.
- `--json` mode is always manifest-safe (samples never serialised).

### Tests

- 129 active tests passing (was 73 at start of v0.4.0, 81 at end of
  v0.3.0). 48 new across:
  - GatingPolicy unit tests (11): default/all/CSV/carve-in/carve-out/
    info-passthrough/conflict-overlap/conflict-double-all/unknown-kind/
    empty-value.
  - Per-citation quote density (4): attribution, unattributed bucket,
    page-level independent fire, clean-page no-fires.
  - License symmetry (3): reverse direction is info, arxiv-empty
    carve-out, no-url no-finding.
  - Ack store (8): id stability, save/load roundtrip, missing/corrupt
    file handling, attach_ack_ids, filter_acked, samples-not-persisted,
    content-drift invalidation.
  - Per-detector flags integration (8): refuse-on blocks, accept-on
    proceeds, refuse-on-other-kind doesn't block, carve-out works,
    deprecated aliases work + emit notice, conflict errors at parse,
    unknown kind errors at parse.
  - Ack persistence integration (5): roundtrip, content-change
    invalidation, samples never on disk, --list-acks, --clear-acks.
  - Standalone CLI (7): clean=0, findings=1, no-wiki=2, JSON strips
    samples, --scope filters, --show-acks empty, no cache writes.

### Documentation

- `docs/licensing.md` — new sections on gating policy, ack lifecycle,
  standalone audit. v0.3.0 sections reformatted to fit alongside.

## v0.3.0 — 2026-05-05

Optional Microsoft Presidio integration for ML-based PII detection
beyond what regex+density can catch.

### What's new

- **`--enable-presidio` flag** on `subgraph_export.py` and `merge.py`.
  When set (and Presidio is installed), substitutes Microsoft Presidio
  for the regex GDPR detector. Catches PERSON names, LOCATION,
  structured IDs (`US_DRIVER_LICENSE`, `US_PASSPORT`, `MEDICAL_LICENSE`,
  `IP_ADDRESS`), and GDPR special-category data (`NRP` — nationality,
  religion, political group).
- **`scripts/presidio_gate.py`** — soft-import wrapper. If
  `presidio-analyzer` isn't installed (default), the gate logs `skipped`
  and the regex baseline runs as fallback. AnalyzerEngine is cached at
  module level (3–5s load amortized across all files in one run).
- **Curated default entity list** (`presidio_gate.DEFAULT_ENTITIES`):
  PERSON, EMAIL_ADDRESS, PHONE_NUMBER, US_SSN, IBAN_CODE, CREDIT_CARD,
  MEDICAL_LICENSE, US_DRIVER_LICENSE, US_PASSPORT, IP_ADDRESS, NRP,
  LOCATION. Excluded by default: ORGANIZATION, DATE_TIME, URL (too
  noisy on academic content / already redacted separately). Override
  via `--presidio-entities`.
- **Density-aware severity for Presidio findings**, mirroring the regex
  detector. Outside FETCHED markers → warn. Inside markers, structured
  IDs → warn. Inside markers, relaxable entities (PERSON, EMAIL, PHONE,
  LOCATION, IP, NRP) → sparse=info, dense=warn at the same 0.5/1000
  threshold. Without this, every academic paper would fire warn-level
  PERSON findings on author names — the same UX disaster v0.2.1 had
  with author emails.
- **Per-file result cache** at `.curator/.preflight-cache/`. Keyed by
  `(file sha256, entity-list + confidence hash)`. Manifest-safe entries
  only (no samples ever — enforced by test). Bypass via
  `--no-preflight-cache`.

### Self-leak guarantee

Presidio's default analyzer uses spaCy NER + offline custom recognizers.
The AnalyzerEngine is initialized without any cloud-backed recognizers.
**All analysis runs on the local machine; no content leaves it.** This
is why we chose Presidio over an LLM-API approach: asking a third-party
LLM "is this private?" sends the very content the user is trying not to
leak. (`--enable-llm-pii-scan` is deliberately *not* in this release.)

### setup.sh

- New post-setup y/N: install Presidio + spaCy `en_core_web_lg` model.
  Default off; explicit disk (~500MB) and network callouts. Marker file
  prevents re-prompts. Two-step install with failure-tolerant messages
  (pip can fail; model download can fail; either path leaves a useful
  manual-install hint).

### Documentation

- `docs/licensing.md` — new "Optional Presidio gate" section: install
  instructions, entity list, severity rules, self-leak guarantee,
  caching behaviour, limitations (English-only, no combined-data
  inference, no quote-vs-published disambiguation).
- `docs/trust-model.md` cross-references the self-leak architecture.

### Tests

- 81 active + 4 skip-when-Presidio-absent tests (was 73).
- New: soft-import path (engine unavailable → regex fallback);
  cache hit skips re-analysis; cache invalidates on entity-list /
  confidence change; cache disabled when `cache_dir=None`; samples
  never persisted to cache files; cache config hash is order-
  insensitive.
- Real Presidio integration tests: PERSON in user content → warn;
  PERSON in fetched/sparse author block → info; manifest_safe strips
  sample entity values; "via Presidio" attribution in summaries.

### Limitations

- English-only (the bundled `en_core_web_lg` model). Non-English content
  gets poor NER; documented.
- Doesn't catch combined-data inference ("PERSON + LOCATION + DOB" as
  a quasi-identifier). Could be added in a future release as a
  Presidio post-processor.
- Doesn't disambiguate quoted-as-example vs published-as-contact. The
  density relaxation is the closest current proxy.

## v0.2.2 — 2026-05-05

PII detection now distinguishes between content the user typed and
content that came from a published source. Academic vault extractions
no longer dominate findings with benign author-block emails.

### Density-aware FETCHED-content severity

- The PII detector splits each scanned body into "fetched" (inside
  `<!-- BEGIN FETCHED CONTENT --> ... <!-- END FETCHED CONTENT -->`
  markers, written by curiosity-engine's local_ingest.py) and "user"
  (everything else, including frontmatter and prose above/below
  markers) regions.
- Severity rules:
  - Outside FETCHED markers: any PII match → **warn** (user-typed
    content gets close scrutiny).
  - Inside FETCHED markers, SSN/IBAN/payment-card-shaped: always
    **warn** (no legitimate published form even in a paper).
  - Inside FETCHED markers, email/phone: **density-scaled**.
    Threshold 0.5 matches per 1000 chars. Below → **info** (looks
    like author/contact block); above → **warn** (looks like a
    directory or DB dump). Floor of 2000 chars below which density
    math is suppressed and matches are warn.
- File-level severity = max across kinds, so a paper with sparse
  author emails (info) plus one IBAN (warn) lands as warn.
- Malformed FETCHED markers (BEGIN without END, mismatched counts) →
  scan everything as user content. Better to over-flag than under-
  flag tampered structure.

### Severity-aware export gating

- **Info-only findings no longer gate.** v0.2.1 refused on any
  finding in non-interactive mode; v0.2.1.1 emits a single-line
  stderr acknowledgement ("12 info-level finding(s) — typical for
  academic content; not flagged for review") and proceeds.
- Warn/block findings still prompt interactively, refuse in
  non-interactive mode without `--yes`, and refuse always under
  `--strict`.

### Why this matters

Real arXiv extraction (8 corresponding-author emails in 50K-char
body) was a v0.2.1 nightmare: every academic vault file produced a
warn-level finding, every export prompted, users were trained to hit
`--yes` reflexively. Density math separates A-class (papers, ~0.2
emails/1000 chars) from B-class (DB dumps, ≥5 emails/1000 chars) by
two orders of magnitude — the cleanest principled signal we found
without going to ML/NER (deferred to v0.2.2's planned Presidio gate).

### Tests

- 73 tests passing (was 60). 13 new: density math at sparse/dense/
  short-doc/multi-block scenarios; SSN/IBAN inside markers stay warn;
  user-region emails outside markers are warn even when fetched body
  is clean; malformed markers; file-level severity = max; info-only
  export proceeds without `--yes` in non-interactive subprocess;
  `--strict` allows info-only; dense PII still refuses.

## v0.2.1 — 2026-05-05

Tighten the v0.2.0 pre-flight detectors after a critical review surfaced
two privacy regressions, two false-positive disasters, and one design
oversight. Manifest schema bumped to `2`.

### Privacy regressions (fixed)

- **PII no longer leaks into the published manifest.** v0.2.0 embedded
  `Sample: alice@x.com, ...` directly inside `rationale` strings, which
  flowed into `_export-manifest.json` and got published. v0.2.1
  introduces a hard contract: every finding has a manifest-safe section
  (`kind`, `severity`, `subject`, `summary`, `rationale`) and a local-
  only `samples` list that is **always stripped** before any manifest
  write. Enforced by `preflight.manifest_safe()` and tested by
  regression tests that assert no `@`/SSN/IBAN patterns appear in
  published manifest bytes.
- **Manifest defaults to summary-only**: `preflight_summary: [{kind,
  severity, count}]`. No subjects, no rationales. Receivers see *what
  categories fired*, not *which files*. Prevents a `topic:curiosity-
  wiki` GitHub query from becoming a harvesting oracle. Per-finding
  records still available via `--include-preflight-in-manifest` (still
  no samples).

### False-positive fixes

- **Phone detector rebuilt as E.164-only**. v0.2.0's generic phone regex
  matched arXiv IDs (`2401.12345`), DOIs (`10.1038/s41586-021-03819-2`),
  ISBNs (`978-3-16-148410-0`), citation stems (`vaswani-2017-1706.03762`),
  year ranges (`(1942-2018)`) — useless on academic content. v0.2.1
  matches only `+`-prefix E.164 (8–15 digits). Documented limitation:
  real-people phones without `+` pass through.
- **Payment-card detector requires real issuer prefix**. Visa `4`,
  Mastercard `51-55`, Amex `34/37`, Discover `6011/65`. ISBN-13 numbers
  (`978`/`979`) no longer false-positive. Comment clarified: the regex
  is for *flagging* PII, not processing payments.
- **GPL detector tightened**. Now matches only: (a) frontmatter
  `license: GPL-*`, (b) SPDX identifier anywhere, (c) GPL keyword inside
  a triple-backtick fenced code block. Bare prose mentions of GPL or
  copyleft (e.g. a Stallman bio, license-comparison page) no longer
  fire.
- **Email regex broadened for RFC 6531 i18n**. `José@example.org`,
  `用户@邮件.中国` now match. Reserved-domain filter rebuilt: RFC 6761
  domains (`example.com/.org/.net`, `localhost`, `*.test`, `*.example`,
  `*.invalid`, `*.local`) filter as test data. The bogus v0.2.0 noise
  filter (`__init__`, `test_`) is gone.

### License allowlist tightened

- **`CC-BY-NC` and `CC-BY-ND` removed from default allowlist** for
  `--include-vault=owned`. NC forbids commercial use; ND forbids
  derivatives. The wiki's normal operation (extraction, classification,
  summarization, redistribution) may exceed both clauses. Users with a
  compliant use case can re-include via `--allow-license-class nc,nd`.

### Coverage extended

- **Pre-flight runs on merge stage**. Receivers now get the same
  detector pass on incoming staged content. Findings appear in the merge
  audit report; samples remain local-only there too. Informational —
  does not block apply.

### Documentation

- `docs/licensing.md` rewritten: manifest-safety contract, detector
  scopes, license allowlist policy, philosophy.
- `docs/trust-model.md` cross-references the manifest-safety contract.
- Manifest schema version bumped to `2`.

### Tests

- 60 tests passing (was 47). New: regression test asserting published
  manifests contain zero `@`/SSN/IBAN regex matches anywhere; per-
  detector tests for E.164-only phone, i18n email, reserved-domain
  filter, GPL prose non-match, GPL frontmatter/SPDX/fence match,
  manifest_safe/summary projection, NC/ND allowlist removal +
  `--allow-license-class` opt-in, merge-stage preflight integration
  + audit redaction.

## v0.2.0 — 2026-05-04

Sharing-safe defaults and licensing-aware merge.

### Premise
- **Share notes, not sources.** Most curiosity-engine vaults hold copyrighted material (preprints, paywalled papers, blogs) whose republishing is the user's own legal call. The publishing user's notes on top of those sources are their own work. v0.2 separates the two: wiki pages ship; vault metadata ships; vault bytes don't ship by default.

### subgraph-export
- New `--include-vault {none,owned,all}`, default `none`.
  - `none` — public-sharing safe; no bytes from vault/.
  - `owned` — bundles only files whose frontmatter has a redistributable `license:` (CC-*, MIT, Apache-2.0, BSD, public-domain, arxiv-non-exclusive) or `redistributable: true`, OR whose `source_url` is on a recognized preprint server (arxiv/biorxiv/chemrxiv/medrxiv).
  - `all` — every cited file. Personal transfer only; not safe for public sharing.
- `_export-manifest.json` now records full vault metadata (`sha256`, `source_url`, `source_type`, `title`, `license`, `redistributable`) for every cited file regardless of mode, so receivers can hydrate.

### merge
- Reads incoming `_export-manifest.json` if present; for any cited vault file not shipped or already in the receiver's vault, tags the corresponding source stub with `vault_missing: true` and propagates `source_url`, `source_type`, `vault_sha256`, and `license` from the manifest. The receiving user (and any agent) sees the tag immediately.
- Missing-vault summary added to the audit report with URLs and licenses.
- Now accepts source trees that have no `vault/` directory (sharing-safe exports).

### hydrate-vault (new verb)
- Walks source stubs tagged `vault_missing: true`, categorizes by URL domain (arxiv / biorxiv / chemrxiv / open_access / paywalled / unknown), dispatches to a fetcher per category.
- **AlphaXiv-preferred** for arXiv when the alphaxiv skill is installed (cleaner pre-extracted markdown); falls back to PDF + curiosity-engine `local_ingest`.
- Paywalled sources never auto-fetched; listed for manual institutional access.
- Default dry-run; `--apply` actually fetches; `--yes` auto-accepts confirmations; `--origin <name>` filters to one merge.
- Successful fetches drop the `vault_missing: true` flag.
- sha256 mismatch on fetched content is saved with a `.candidate` suffix and flagged — no silent substitution.

### setup.sh
- Post-setup offer to install alphaxiv (default off, interactive y/N). Doesn't break auto-install flow.
- Allowlist patterns extended to cover all v0.2 scripts (accept_bridges, unmerge, hydrate_vault).

### Documentation
- New `docs/licensing.md` — full model, recommended publishing patterns, per-category fetch strategies, recommendations for both publishers and receivers.
- `docs/trust-model.md` adds a Licensing/content provenance section connecting it to the security model.
- `SKILL.md` and `README.md` now document five verbs and the share-notes-not-sources premise up front.

### Pre-flight detectors (subgraph-export)
- New `scripts/preflight.py` runs on every export before write. Each detector returns findings with plain-language rationale; user accepts (`--yes` or interactive `y`), refuses (`--strict`), or skips (`--no-preflight`). Findings recorded in the manifest under `preflight_findings`.
- **non_native_page** — pages tagged `origin:` (previous merge) excluded by default; `--include-non-native` to override.
- **quote_density** — pages where ≥25% of body is in `>` block quotes; threshold tunable via `--quote-density-threshold`.
- **license_inconsistent** — vault file with declared open license but URL on a paywalled-publisher domain.
- **gpl_contagion** — GPL/AGPL/LGPL markers in vault or wiki content; rationale explains copyleft propagation risk.
- **gdpr_likely_pii** — emails, phones, SSN, IBAN, credit-card-like numbers; conservative filters (`@example.com`, low digit counts) to reduce noise; user verifies remaining matches.
- **URL redaction** — `source_url` query strings stripped in manifest by default (signed URLs, tokens, `utm_*`); `--keep-url-params` to preserve.

### Tests
- 47 pytest tests (was 12). New: 20 unit tests for each preflight detector (test_preflight.py); 8 integration tests for the new flags (--include-non-native, --keep-url-params, --strict, --no-preflight, --yes, non-interactive refusal); plus the prior 19 e2e tests.

## v0.1.0 — 2026-05-03

First release. Sharing/federation layer for curiosity-engine wikis.

### Verbs
- `subgraph-export` — extract a self-contained mini-wiki by `--project`, `--page` (with optional `--include-1-hop`), or `--origin`. Transitive vault collection via `(vault:...)` citations. Writes `_export-manifest.json`. Path-traversal guards reject `..` segments and destinations inside the workspace.
- `discover-bridges` — semantic-similarity sweep over wiki page pairs that aren't yet wikilinked, with `--across-origins` for post-merge cross-origin candidates. Cold-start guard when `embedding_enabled` is off or sentence-transformers is missing. Emits `[ ]`-marked review queue.
- `accept-bridges` — reads `[x]`-marked discover-bridges queue, writes wikilinks in both directions (idempotent), updates `accepted_bridges` in affected merge manifests for unmerge support.
- `merge` — combine another wiki into this one with vault sha256 dedup, source-stub stem reconciliation, page-name collision triage (identical / same-topic / different-topic), origin tagging, untrusted-content framing, citation alias rewriting. Stages to `.curator/.merge-staging/<origin>/`; `--apply` swaps atomically and writes `.curator/merges/<origin>.json` for unmerge; `--abandon` discards; `--rerun-gates` re-evaluates after fixes.
- `unmerge` — surgically undo a previous merge using the manifest + receiving wiki's current state. Three buckets: pure imports (removed), user-modified imports (preserved, flagged), already-de-imported (logged). Native pages with broken references after unmerge are annotated, not edited. Cross-origin bridges accepted at merge time are unwound.

### Trust model
- Required gates (always on): `scrub_check.py --mode wiki`/`vault`, frontmatter `ALLOWED_FM_KEYS` enforcement, sha256 citation validation, path-traversal rejection, never-silent-overwrite of page-name collisions.
- Optional gates (opt-in): `--enable-snyk-code`, `--enable-semgrep`, `--enable-clamav`, `--enable-secrets-scan`, `--enable-quality-lint`, or `--enable-all-scans`. Quarantines block apply until resolved.
- `config/semgrep-curiosity-merge.yml` — starter ruleset for prompt-injection, schema-override claims, shell-injection in markdown, encoded-payload blobs.

### Architecture
- Hard dependency on curiosity-engine; reuses `naming`, `sweep`, `projects`, `activity_log`, `graph`, `lint_scores`, `vault_index` via `CURIOSITY_ENGINE_SCRIPTS_DIR`. No forking.
- Hash-guarded by `merge_evolve_guard.sh` (named distinctly from curiosity-engine's `evolve_guard.sh` to disambiguate when both skills are installed; prints `[curiosity-merge guard]` to stderr on each run).
- Sub-wikis published as ordinary GitHub repos tagged with the topic `curiosity-wiki` for discovery.

### Tests
- 12 pytest e2e tests in `tests/` covering all five verbs end-to-end with two artificial wikis exercising vault dedup, page collisions, citation rewrites, three-bucket unmerge, accept-bridges idempotence, and rerun-gates manifest preservation.
