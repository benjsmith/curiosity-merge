# Changelog

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
