---
name: curiosity-merge
description: "Sharing/federation layer for curiosity-engine wikis. Use when the user mentions 'merge wiki', 'combine wikis', 'share a sub-wiki', 'export project', 'subgraph export', 'discover bridges', 'cross-wiki', 'absorb someone else's wiki', 'federate', 'import a shard', 'reconcile by IRI/identity', or wants to publish/ingest a curiosity-wiki-tagged repo. Verbs: merge (slug- and IRI-keyed, plus --import-shard), subgraph-export, discover-bridges, unmerge, hydrate-vault. Requires curiosity-engine installed in the same workspace."
---

# Curiosity Merge

Cross-wiki operations for [curiosity-engine](https://github.com/benjsmith/curiosity-engine) workspaces. Daily curation lives in curiosity-engine; this skill adds the verbs you reach for when you want to **combine wikis, extract sub-wikis for sharing, or surface link candidates** between regions of one or many wikis.

This is a deliberately separate skill because it ingests external data (someone else's wiki). The trust model is different from daily curation, the audience is smaller, and the release cadence is independent.

## Dependency

Requires `curiosity-engine` installed in the same workspace. `setup.sh` verifies this and refuses to proceed if it's missing. Scripts import shared helpers via the `CURIOSITY_ENGINE_SCRIPTS_DIR` env var (or `<skill_path>` substitution under Claude Code).

```bash
# install (alongside an existing curiosity-engine install)
npx skills add -g -y benjsmith/curiosity-merge
bash <skill_path>/scripts/setup.sh
```

## Sharing and licensing — share notes, not sources

Most curiosity-engine vaults hold sources whose copyright doesn't belong to the user (arXiv preprints, paywalled papers, copyrighted blogs). Notes written *on top of* those sources do. curiosity-merge separates them.

`subgraph_export.py --include-vault {none,owned,all}` controls which vault files ride along:
- `none` (default) — bytes-free export. Wiki pages ship; vault metadata (sha256, source_url, license) is recorded in the manifest; receivers hydrate. **Always safe for public sharing.**
- `owned` — bundles only files whose frontmatter declares a redistributable license OR whose URL is on a preprint server (arXiv/bioRxiv/chemRxiv).
- `all` — everything. Personal transfer only; not safe for public sharing.

When a receiver merges, source stubs whose vault files weren't shipped get tagged `vault_missing: true` with provenance. `hydrate_vault.py` walks those stubs, categorizes by URL (arxiv / preprint / open_access / paywalled / unknown), and re-acquires what it can with per-source confirmation. AlphaXiv-preferred for arXiv when installed.

**Pre-flight detectors** run on every `subgraph-export` before write: chain-merge contamination (non-native pages excluded by default), quote-density (single-source + page-level thresholds), license-consistency (open-license-on-paywalled-URL warn + restrictive-license-on-OA-URL info), GPL contagion, GDPR-likely PII (regex+density baseline), URL redaction. Each finding has a `severity` (`info`/`warn`/`block`); info-only findings proceed without prompt.

**Gating policy** (v0.4.0+): `--refuse-on=VALUE` and `--accept-on=VALUE` each accept `all`, `none` (default), or a comma-separated kind list. Carve-out (`--refuse-on=all --accept-on=quote_density`) and carve-in (`--refuse-on=gpl_contagion --accept-on=all`) are intuitive. Conflicts error at parse time. Deprecated aliases: `--strict` and `--yes` for backwards compat.

**Persistent acks**: `--remember-acks` persists accepted findings to `.curator/preflight-acks.json`; subsequent runs auto-suppress unchanged findings. Interactive prompt is `[y/N/a]` where `a` = yes-and-remember. `--list-acks` / `--clear-acks` for management.

**Standalone audit**: `uv run python3 <skill_path>/scripts/preflight.py --workspace .` for a read-only CI-friendly audit (exit codes: 0 clean / 1 warn+ / 2 error; `--json` mode for machine output).

**Optional `--enable-presidio`** (v0.3.0+): Microsoft Presidio's local NER+ML pass replaces the regex GDPR detector. Catches PERSON names, LOCATION, structured IDs (driver license, passport, medical license), and GDPR special-category data (NRP). All analysis local; no content leaves the machine. Install: `uv pip install presidio-analyzer && uv run python -m spacy download en_core_web_lg` (~500MB; `setup.sh` offers this prompt). Per-file result cache at `.curator/.preflight-cache/`.

See `docs/licensing.md` for the full table, manifest-safety contract, density rules, and Presidio reference.

## The five verbs

### `subgraph-export` — extract a self-contained mini-wiki

```
uv run python3 <skill_path>/scripts/subgraph_export.py \
    --project <name> --to <path>
uv run python3 <skill_path>/scripts/subgraph_export.py \
    --page <stem> --include-1-hop --to <path>
uv run python3 <skill_path>/scripts/subgraph_export.py \
    --origin <name> --to <path>
uv run python3 <skill_path>/scripts/subgraph_export.py \
    --pages-file <refs.json> [--skip-missing] --to <path>
```

Writes a normal curiosity-engine wiki layout at `<path>` (`vault/`, `wiki/`, `.curator/projects.json`) plus an `_export-manifest.json` recording the scope. The destination is suitable for `git init && git push` to a public repo for sharing — tag the repo with `curiosity-wiki` for discovery.

**`--pages-file`** (v0.7.0) exports an explicit page set: the file is a JSON list of page refs, each either a bare stem (`"transformer"`) or a wiki-relative path with optional `.md` (`"concepts/transformer"`). Refs resolve with the same case-insensitive stem/path matching as `--page`, but there is deliberately no 1-hop expansion — the file IS the scope (the caller computed it, e.g. a workbench UI selection). Refs that match nothing fail the export with every miss listed; pass `--skip-missing` to report misses on stderr and export the matched pages anyway.

Vault files are included transitively: every `(vault:...)` citation reachable from an in-scope wiki page brings the cited file along. A receiving workspace runs `curiosity-merge merge ./that-clone --as-origin <name>` to absorb it.

**Figure assets ride along too** (v0.7.0): every image embed reachable from an in-scope page — markdown `![alt](figures/_assets/x.png)` and Obsidian `![[x.png]]` / `![[_assets/x.png]]` forms, normalized the same way curiosity-engine's `wiki_render.py` normalizes them — is copied into the export at the same wiki-relative path (`wiki/figures/_assets/...`) and listed under `scope_figures` in the manifest. A missing asset warns on stderr but never fails the export (assets are regenerable via curiosity-engine's `figures.py regen`). External image URLs and non-image transclusions are ignored.

**Headless / local invocation** (audited v0.7.0): `--no-preflight --force` is the supported never-prompt combination for programmatic callers (e.g. local-to-local transfers where the publish-oriented licensing/PII gates are wrong-purpose). `--no-preflight` skips the entire preflight pass — the interactive `[y/N/a]` prompt is the only stdin read on the export path, and it lives inside that pass; `--force` removes the only other stop (the non-empty-destination refusal). Everything else either succeeds or exits nonzero with a message; nothing reads stdin or waits on a TTY. (Even without `--no-preflight`, a prompt-needing finding with no TTY exits nonzero rather than hanging — but headless callers should skip the gates explicitly.) For a local page move you'll typically also want `--include-vault=all` and, if origin-tagged pages are in the set, `--include-non-native`.

### `discover-bridges` — find unwritten cross-page links

```
uv run python3 <skill_path>/scripts/discover_bridges.py \
    [--across-origins] [--limit N]
```

Semantic-similarity sweep over page pairs that aren't yet wikilinked. Returns a review queue at `.curator/bridges-<timestamp>.md`. With `--across-origins`, restricted to pairs where the two pages have different `origin:` audit tags (only meaningful after a merge).

Useful within a single wiki even before any merge: it surfaces concept pages that should be cross-linked but aren't.

Reuses curiosity-engine's embedding stack (sentence-transformers + sqlite-vec, behind `embedding_enabled: true`). The cold-start guard is the same as curiosity-engine's wave-4 classifier: if there aren't enough embedded pages to anchor against, the script reports that condition and exits cleanly rather than producing noise.

### `merge` — combine another wiki into this one

```
uv run python3 <skill_path>/scripts/merge.py \
    <other-wiki-path> --as-origin <name>
uv run python3 <skill_path>/scripts/merge.py \
    --import-shard <export.json> <shard-wiki> --as-origin <name>
uv run python3 <skill_path>/scripts/merge.py --apply-queue
```

Combines `<other-wiki-path>` into the current workspace's wiki. The pipeline:

1. **Vault sha256 reconciliation** — identical content under different filenames is deduplicated; same filename, different content is renamed with an origin discriminator. Do not change this path: shard rejoins routinely dedupe the whole vault (measured: 0 new / tens of thousands deduped per apply).
2. **Source-stub stem reconciliation** — stubs pointing at the same vault file are collapsed; stubs are re-stemmed via curiosity-engine's `naming.citation_stem`.
3. **Identity reconciliation (IRI-keyed, U1)** — runs *before* stem collisions. Entity pages carrying a curiosity-engine `iri:` (or a `same_as:` map) are matched across the two wikis by shared `iri` first, then by any overlapping `authority:id` pair — read from the receiver's `.curator/identifiers.db` `entities` table and from page frontmatter. A matched entity collapses into the receiver's canonical page **regardless of slug**: it is not re-staged as a live page, wikilinks to its slug are redirected to the canonical slug, and its `same_as` map is unioned into the receiver's page frontmatter and `entities` registry on apply. If the incoming **body** (frontmatter-stripped, merge-framing ignored) is identical to the canonical page, no `<stem>-from-<origin>.md` review copy is written into live `wiki/` — only `same_as` is unioned. Different bodies still land a review copy under `collisions/` for human review. Source stubs that differ only in their reciprocal `[[wikilinks]]` fold rather than fork (step 4). Pages with no minted IRI fall through to the stem queue (step 4).
4. **Page-name collision queue (stem fallback)** — pages with the same stem are NEVER silently overwritten. **Identical bodies** (not whole-file sha256 — frontmatter may differ after parallel shard curation) drop the incoming page; it is not materialized as `<stem>-from-<origin>.md`. **Source stubs whose bodies differ only in their `[[wikilinks]]`** (`type: source` or a page under `sources/`) fold: the incoming links are added to the receiver's stub on apply, nothing else about it changes, and no `<stem>-from-<origin>.md` is created. Parallel shard CURATE rewires reciprocal links on stubs it never rewrote; without the fold each shard forks the stub and the parent floods with ~N duplicate source pages. Folded link targets are restricted to plain slugs (`[A-Za-z0-9][A-Za-z0-9 ._/-]*`, no `..`) — they are the only incoming text that reaches a live page without passing through staging. Same topic / both substantive go to a manual-reconciliation queue with both versions preserved as `<stem>.md` and `<stem>-from-<origin>.md`; different topics that happen to share a stem are renamed with an origin discriminator. Every stem fallback prints `merge: stem fallback (no IRI): …` on stderr and is listed in the audit; `--iri-required` refuses the stage (exit 1, staging left for the audit) when this list is non-empty.
5. **Origin tagging** — every page from the other wiki gains an `origin: <name>` audit field in addition to its existing `projects:` set.
6. **`projects:` ACL** — `--acl keep-receiver` (default): when two pages become one (identity collapse, identical-body drop, or source-stub wikilink fold), the survivor's `projects:` is unchanged (incoming tags are logged, not applied). `--acl union` adds incoming tags. `--acl intersect` sets survivor = receiver ∩ incoming, but only on `type: analysis` pages (other types stay keep-receiver). New pages with no collision keep their incoming tags either way.
7. **`--allow-iris FILE`** — laptop → org trunk. One IRI per line (`#` comments ok). Listed IRIs may collapse onto / land at canonical trunk slugs. Unlisted IRIs never occupy a trunk slug: they are origin-namespaced as `<stem>-from-<origin>.md` and do not union `same_as` into a trunk entity. Omit the flag for no gating.
8. **Untrusted framing** — every merged page body is wrapped in `<!-- BEGIN UNTRUSTED MERGED CONTENT — origin:<name> -->` framing and gets `untrusted: true` in frontmatter so future curator workers treat the content as data, not instructions.
9. **Graph union** — the kuzu graph is rebuilt across the merged wiki via curiosity-engine's `graph.py rebuild wiki`. `--no-rebuild` skips it. Successful stages append the origin to `.curator/merge-queue.json`; `merge.py --apply-queue` applies every queued origin FIFO with dest-clobber guards (identical body → skip; dest taken → rename) and rebuilds the graph **once**.
10. **Audit report** — `.curator/.merge-staging/<origin>/audit-report.md` summarizes every reconciliation, every collision, every identical-body drop, every folded source stub (with the links folded in), and every stem fallback. Identity reconciliations are under `## Identity reconciliation`, separate from the stem-based `## Page-name collisions`. The user reviews this before any commit lands.

All work is staged in `.curator/.merge-staging/<origin>/` first. The atomic swap into `wiki/` and `vault/` only happens after the user reviews the audit report and explicitly approves (`--apply <origin>` or `--apply-queue`). The receiving wiki's `.git` is untouched until the user runs their own `git -C wiki commit`.

#### `merge --import-shard` — rejoin a U4 shard on its seam IRIs

```
uv run python3 <skill_path>/scripts/merge.py \
    --import-shard <export.json> <shard-wiki-path> --as-origin <name>
```

Ingests a bounded sub-wiki **shard** — the output of curiosity-engine's `epoch_summary.py --shard <seed-page>` (the `export.json`) plus the shard's exported wiki tree (`<shard-wiki-path>`). The shard's `seam_entities[].iri` are the federation join keys: IRI-bearing entity pages inside the shard that are linked from outside it. Import runs the same identity reconciliation as a plain merge, with the seam IRIs flagged — so a seam entity the parent already holds reconciles into the parent's canonical page (no duplicate) rather than landing as a new page, and the audit's `## Identity reconciliation` section lists those seam joins distinctly. Identical-body seam pages do not spawn `<stem>-from-<origin>.md` in live `wiki/`. Stage each parallel shard under its own `--as-origin`, then `merge.py --apply-queue` to apply FIFO and rebuild the parent graph once.

### `unmerge` — undo a previous merge

```
uv run python3 <skill_path>/scripts/unmerge.py --origin <name>
uv run python3 <skill_path>/scripts/unmerge.py --origin <name> --apply
uv run python3 <skill_path>/scripts/unmerge.py --origin <name> --abandon
```

Reverses an earlier `merge`, surgically. Reads `.curator/merges/<origin>.json` (the manifest written by `merge.py --apply`) and partitions every imported page/vault file into three buckets:

- **Pure imports** — still tagged `origin: <name>`, sha256 unchanged since import. Safe to remove.
- **User-modified imports** — still tagged but sha256 differs. User has curated this page since the merge. **Never silently deleted**; staged for review with the original-import version preserved alongside.
- **Already de-imported** — user already pruned it. Logged, no action.

Walks the rest of the wiki for **native pages that reference imported content** (wikilinks `[[<imported-stem>]]` or citations `(vault:<imported-rel>)`); these are the user's own curation built on top of the imports. Their references will become dead links after unmerge — the script rewrites them to plain `[[stem]]` form, appends an audit comment, and lists every affected page in the audit report so the user can decide what to do.

Cross-origin bridges accepted during the original merge are unwound: the wikilink that connected the native page to the imported page is removed, and the action is logged.

All work is staged to `.curator/.unmerge-staging/<origin>/`. The user reviews the audit report and runs `--apply` (atomic swap into `wiki/` and `vault/`, then `graph.py rebuild wiki`) or `--abandon` (discard staging).

**Cannot rely on the source wiki's remote.** The source may have been curated since you merged. The receiving wiki's own git history + the per-origin manifest is the only authoritative record of what came from the merge — and that's what unmerge uses.

### `hydrate-vault` — re-acquire missing sources after a merge

```
uv run python3 <skill_path>/scripts/hydrate_vault.py --origin <name>
uv run python3 <skill_path>/scripts/hydrate_vault.py --origin <name> --apply
```

Walks source stubs tagged `vault_missing: true`, categorizes by URL, and dispatches to a fetcher per category. Default is dry-run; `--apply` actually fetches. Per-source confirmation in interactive mode (or `--yes` to auto-accept). Successful fetches clear `vault_missing: true` from the stub.

| Category | Strategy |
|---|---|
| `arxiv` | AlphaXiv-preferred if installed (clean pre-extracted markdown); falls back to PDF + curiosity-engine `local_ingest`. |
| `biorxiv` / `chemrxiv` / `medrxiv` | PDF download + `local_ingest`. |
| `open_access` | Direct fetch + `local_ingest`. |
| `paywalled` | Listed for manual fetch via institutional access. Never auto-downloaded. |
| `unknown` | Listed; manual handling required. |

`--origin <name>` filters to stubs from that merge; without it, every `vault_missing` stub in the wiki is processed regardless of origin.

If alphaxiv isn't installed and an arXiv source needed PDF fallback, the script can offer the install hint with `--offer-alphaxiv` (or you can skip it; the setup.sh prompt also offers).

## Trust model

`merge` is the only verb that ingests external data, and that's where the trust model lives. The defenses are concrete:

- **Adversarial frontmatter** — bypass attempts via `projects:`, malformed YAML targeting parser issues. **Defence**: every incoming frontmatter goes through `naming.read_frontmatter`, which already strips unknown keys via `ALLOWED_FM_KEYS`. Don't extend that allowlist for merged content.
- **Prompt injection in markdown bodies** aimed at the receiving curator agent. **Defence**: every merged page body is wrapped in `<!-- BEGIN UNTRUSTED MERGED CONTENT — origin:<name> -->` framing and has `untrusted: true` in frontmatter. Workers see the framing and treat content as data.
- **Manipulated `(vault:...)` citations** pointing at non-existent or wrong-content vault files. **Defence**: every vault file referenced from merged pages must exist in the merged-vault index by sha256; citations to missing or sha-mismatched content get rewritten or flagged in the audit report.
- **Path traversal in CLI args** (`--to ../../../etc/passwd`). **Defence**: paths containing `..` segments or absolute paths outside the workspace are rejected at argv-parse time.
- **Page-name collisions on substantive pages** (both wikis have `concepts/transformer.md` with different content). **Defence**: NEVER silently overwrite. Always queue for human review with both versions preserved. Identical **bodies** (frontmatter may differ) drop the incoming page — they do not land as `<stem>-from-<origin>.md` in live `wiki/`. Source stubs differing only in `[[wikilinks]]` fold their links into the receiver's stub instead of forking; the fold is additive (link bullets only, from a restricted slug alphabet) and never rewrites the receiver's prose or frontmatter.
- **Identity reconciliation respects the same posture** — when an incoming entity matches a receiver identity by `iri`/`same_as`, the receiver's page stays canonical and is never overwritten by untrusted incoming content. Different incoming bodies are preserved under `collisions/` for review; identical bodies are not copied into live `wiki/`. Only the additive `same_as` union is written into the receiver (`projects:` stays keep-receiver unless `--acl union`). A spoofed incoming `iri` can at most attach extra `same_as` pairs to an entity the receiver already owns (visible in the audit) — it cannot replace the canonical page or its content.

See `docs/trust-model.md` for the full threat list and decision rationale.

## Publishing and discovery

Sub-wikis are shared as ordinary GitHub repos. Tag them with the topic `curiosity-wiki` (Settings → Topics → add `curiosity-wiki`); discovery is then a one-line GitHub query:

```
https://github.com/topics/curiosity-wiki
```

A future curated index lives at `benjsmith/curiosity-wikis-index`. See `docs/publishing.md` for the full publishing recipe.

## Bash discipline

This skill inherits curiosity-engine's discipline: only `uv run python3 <skill_path>/scripts/<named_script>.py ...` for script invocations, only `git -C wiki ...` for git, only `bash <skill_path>/scripts/merge_evolve_guard.sh ...` for hash-guarding. No compound shell, no pipes, no `$(...)`. The same allowlist install protocol applies — `setup.sh` extends curiosity-engine's allowlist with this skill's script paths.

**Hash-guard naming.** This skill's hash-guard is `merge_evolve_guard.sh`, not `evolve_guard.sh`, because curiosity-engine ships its own `evolve_guard.sh` guarding a different file list. With both skills under `~/.claude/skills/`, a generic name would invite an agent to call the wrong one. Always invoke this skill's guard by its full path; it prints `[curiosity-merge guard]` to stderr on every run for log clarity.

## Quality and security gating before merge applies

The mechanical defenses in `docs/trust-model.md` (T1–T8) always run on `merge`. On top of those, **`merge` runs a quality/security gating pass on every staged page and every staged vault file before the audit report is written**. Anything flagged goes to `.curator/.merge-staging/<origin>/_suspect/` (quarantine) and is listed in the audit report under `## Quarantined`. The user can re-include a quarantined item only by editing the staging directory directly — `--apply` refuses to silently promote anything from `_suspect/`.

Required gates (always on): `scrub_check.py --mode wiki` / `--mode vault` from curiosity-engine, frontmatter allowlist enforcement, sha256 citation validation, path-traversal rejection.

Optional gates (off by default; opt-in via `merge.py --enable-<scanner>`): Snyk Code (the same scanner skills.sh uses for its skill index), Semgrep with curiosity-tuned rules, ClamAV for vault binaries, gitleaks/trufflehog for accidentally-committed secrets, and curiosity-engine's `lint_scores.compute_all` for low-quality-wiki filtering. Or `--enable-all-scans` for paranoid imports.

See `docs/trust-model.md` for the full gate list, the rationale for opt-in defaults, and the apply/abandon/rerun-gates workflow.

## Verb naming — `merge` vs `rename`

`merge` (this skill) is **cross-wiki**, heavy, with external trust concerns.
`rename` (curiosity-engine, `projects.py rename`) is **within one wiki** — mechanical project-tag absorption. Never use "merge" for the project-rename case.

## Implementation status

| Verb | Status |
|---|---|
| `subgraph-export` | shipped (v0.1) |
| `discover-bridges` + `accept-bridges` | shipped (v0.1) |
| `merge` (IRI-keyed, shard import, queue, ACL, allow-iris) | shipped (v0.1, vault-missing v0.2, identity v0.6, federation v0.8) |
| `unmerge` | shipped (v0.1) |
| `hydrate-vault` | shipped (v0.2) |

Each verb is independently shippable; `subgraph-export` is useful immediately even without the other two.
