# Changelog

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
