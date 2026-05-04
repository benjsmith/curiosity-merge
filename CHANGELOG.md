# Changelog

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

### Tests
- 19 pytest e2e tests (was 12). New coverage: each `--include-vault` mode, mixed-license source fixture (arxiv / Nature / CC-BY blog), merge tagging vault_missing with provenance, hydrate-vault categorization without network.

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
