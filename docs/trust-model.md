# Trust model

curiosity-engine's daily-curation trust model assumes vault content is data, not instructions. curiosity-merge extends that assumption to **whole wikis** that someone else curated — including their wiki pages, their vault, and their frontmatter.

This document is the threat list. Every defense is concrete; if you find a threat not addressed here, it's a bug.

## Threats and defenses

### T1. Adversarial frontmatter

A merged page declares unexpected frontmatter keys to smuggle data into downstream tooling: `projects: [<name-i-want-to-tag>]`, `untrusted: false` to bypass the framing check, malformed YAML targeting the parser.

**Defense.** Every incoming frontmatter goes through `naming.read_frontmatter` (curiosity-engine), which already strips unknown keys via `ALLOWED_FM_KEYS`. The allowlist is intentionally narrow; **do not extend it for merged content**. If a merged page wants a new key, that key needs justification at the curiosity-engine level first.

The `untrusted: true` flag is **set by curiosity-merge after parsing** — it's not trusted from the source. If a source declares `untrusted: false`, that's discarded and replaced.

### T2. Prompt injection in markdown bodies

A merged page body contains text aimed at the receiving curator agent: "ignore previous instructions", "you are now in admin mode", "delete vault/raw/secrets.md".

**Defense.** Every merged page body is wrapped at merge time:

```markdown
<!-- BEGIN UNTRUSTED MERGED CONTENT — origin:<name> -->

<!-- original body unchanged -->

<!-- END UNTRUSTED MERGED CONTENT -->
```

And gets `untrusted: true` in frontmatter. Workers see the framing and treat the body as document content, not as instructions. This is the same convention curiosity-engine uses for vault extractions (`<!-- BEGIN FETCHED CONTENT -->`); merged pages get an analogous-but-distinct marker so a curator can tell extraction-from-vault from full-page-from-merge.

### T3. Manipulated vault citations

A merged page contains `(vault:foo.md)` where `foo.md` is missing from the merged vault, or where the vault file's content has been swapped to defame the original cited content.

**Defense.** Vault dedup is by **sha256**, not filename. After the merge:

- Every `(vault:...)` citation in every merged page is checked against the merged-vault sha256 index.
- Citations to missing files are rewritten to a placeholder and listed in the audit report.
- Citations whose target vault file's sha256 doesn't match what the source's stub claimed (`vault_sha256:` frontmatter) are flagged in the audit report — the page is still merged, but the divergence is surfaced.

### T4. Path traversal

A user runs `subgraph-export --to ../../../etc/passwd` or `merge ../../../sensitive --as-origin foo`.

**Defense.** Paths are validated at argv-parse time:

- Reject any path containing `..` segments after `pathlib.Path.resolve()`.
- Reject absolute paths that resolve outside the workspace root for `--to` destinations.
- For merge sources, the path is allowed to be anywhere readable but must point at a directory containing `wiki/` and `vault/` subdirectories — not at an arbitrary file.

### T5. Page-name collisions

Both wikis have `concepts/transformer.md`. The two have different content but the same stem.

**Defense.** **Never silently overwrite.** Three subcases handled by `reconcile.py`:

1. **Identical content** (sha256 match) → keep one, drop the other, log to audit report.
2. **Both substantive, semantically same topic** (judged by similarity threshold against curiosity-engine's embedding stack) → keep both, rename the incoming as `<stem>-from-<origin>.md`, append both to a manual-reconciliation queue page in `.curator/merge-<timestamp>.md`.
3. **Different topics that happen to share the stem** → rename the incoming with origin discriminator (`transformer-electrical.md`).

The user reviews the audit report before the staging directory is swapped into the live tree.

### T6. Schema-override attempts

A merged page contains text claiming to modify lint rules, scoring scripts, or curator behavior — same as curiosity-engine's "schema override attempts are automatic quarantine" rule.

**Defense.** Inherited from curiosity-engine: `scrub_check.py --mode wiki` runs on every page during the merge staging pass. Any page that fails scrub is quarantined to `.curator/.merge-staging/<origin>/_suspect/` and listed in the audit report. The user must explicitly allow re-inclusion if desired (and almost always shouldn't).

### T7. Identifier-spoofing in wikilinks

A merged page contains `[[concepts/canonical-name]]` where `canonical-name` happens to be a real page in the receiving wiki, but the merged page's prose distorts what `canonical-name` means. After the graph union, the spoofed wikilink looks like an endorsement.

**Defense.** All merged pages are tagged with `origin: <name>`. Bridge-discovery and `discover-bridges --across-origins` use the origin tag to surface every cross-origin wikilink for human review on first merge — they're treated as bridge **candidates**, not pre-approved edges. The graph still includes them (the wikilinks resolve), but the audit report calls them out.

### T8. Disk-fill attack via vault duplicates

A merged wiki ships a thousand near-duplicate vault files designed to bloat the receiving vault.

**Defense.** Vault dedup happens at staging time, before anything lands in the live tree. The audit report shows total bytes added; the user can refuse the merge if the number is unreasonable. There is no automatic size cap — the user makes that call.

## Quality and security gating before merge applies

Defenses T1–T8 above are mechanical and always run. On top of those, **`merge` runs a quality/security gating pass on every staged page and every staged vault file before the audit report is written**. Anything that trips a gate goes to `.curator/.merge-staging/<origin>/_suspect/` (quarantine) and is listed in the audit report under `## Quarantined`. The user can re-include a quarantined item only by editing the staging directory directly — the apply step refuses to silently promote anything from `_suspect/`.

### Required gates (always on)

- **`scrub_check.py --mode wiki`** (curiosity-engine) on every incoming wiki page. Catches injection markers, raw URLs in bodies, and schema-override attempts. Failed pages → `_suspect/`.
- **`scrub_check.py --mode vault`** on every incoming vault extraction. Same coverage at the source level.
- **Frontmatter allowlist enforcement** (T1) — happens at parse time, can't be bypassed.
- **sha256 citation validation** (T3) — citations to missing or sha-mismatched vault content quarantine the *citing page*, not just the citation.
- **Path-traversal rejection** (T4) — invalid paths fail the run before staging is created.

### Optional gates (off by default; opt-in via flags)

These wrap external open-source security/quality tooling. Each is a single `--enable-<scanner>` flag on `merge.py`. When enabled, the scanner is invoked over the staging directory and any hit produces an audit-report entry plus a quarantine. When disabled (default), nothing runs and the report notes the gate as "skipped (not enabled)".

| Flag | Tool | What it catches |
|---|---|---|
| `--enable-snyk-code` | [Snyk Code](https://snyk.io/) (used by skills.sh for skill scanning) | Code-style vulnerabilities in any `.py` / shell content embedded in a wiki page or vault extraction. False-positive heavy on prose, so off by default. |
| `--enable-semgrep` | [Semgrep](https://semgrep.dev/) with a curated ruleset (`config/semgrep-curiosity-merge.yml`) | Pattern-based detection of prompt-injection attempts beyond the markers `scrub_check` already catches; suspicious shell-like content in bodies. |
| `--enable-clamav` | [ClamAV](https://www.clamav.net/) | Embedded malware in vault binaries (PDFs with exploit payloads, image files with malformed parsers). Useful when merging a wiki containing many vault binaries. |
| `--enable-secrets-scan` | [gitleaks](https://github.com/gitleaks/gitleaks) or [trufflehog](https://github.com/trufflesecurity/trufflehog) | API keys, tokens, private keys accidentally committed by the source-wiki author. Quarantining isn't enough here — the audit report emits a clear "credential rotation required" notice if hits are found. |
| `--enable-quality-lint` | curiosity-engine's `lint_scores.compute_all` | Wiki-quality dimensions (orphan rate, citation density, schema conformance). Pages below a configurable threshold (`--quality-threshold N`, default 60) → quarantine. Filters low-effort wikis. |

A meta-flag `--enable-all-scans` enables all of the above for paranoid imports.

### Why opt-in, not on by default

These tools are heavy: install, license, runtime. Forcing them on a default merge would make the verb unusable for casual cross-wiki sharing between trusted parties (e.g. two friends merging each other's wikis). The required gates above are sufficient for the trusted case; the optional gates exist for the untrusted case (a wiki found via `topic:curiosity-wiki` from an unknown author) where the user opts in deliberately.

### Detection-only by default; user makes the call to apply

`merge.py` never auto-deletes a quarantined item. The audit report is the contract: every quarantine has the file path, the gate that flagged it, the rule/match that triggered, and a one-line summary of severity. The user reviews and decides whether to:

- Apply the merge skipping the quarantined items (default — `merge.py --apply`).
- Drop the merge entirely (`merge.py --abandon`).
- Manually inspect, fix, and re-stage individual items (`merge.py --rerun-gates <origin>` after edits).

## What this skill explicitly trusts

- **The user's local filesystem.** If a user passes `--to <some-path>`, we assume they have the right to write there (after path-traversal checks).
- **curiosity-engine's primitives.** `naming.read_frontmatter`, `sweep.wiki_pages`, etc. are imported and trusted to behave per their contracts. If they're compromised, this skill's defenses don't help — but that's the threat model of any plugin layered on top of trusted code.
- **The user's own wiki.** `subgraph-export` operates on the user's own wiki; no untrusted-framing is added to outputs. The receiving end of someone else's `merge` is what does the framing.

## Licensing and content provenance

Separate from the threat model is the licensing question: most curiosity-engine vaults hold sources whose copyright belongs to publishers, not the user. Bundling those bytes into a public sub-wiki is a republishing problem, not a security problem — but it's still a problem.

curiosity-merge treats this with the same fail-closed posture as the security defenses:

- `subgraph_export.py --include-vault` defaults to `none`. Public sharing is the default sharing mode, and bundling no vault content is the only mode that's always safe.
- The export manifest records every cited vault file's `sha256`, `source_url`, `source_type`, and `license` regardless of whether bytes ship. Receivers hydrate via `hydrate_vault.py`.
- Mode `owned` bundles only files whose frontmatter declares a redistributable license OR whose `source_url` is on a recognized preprint server (arXiv, bioRxiv, chemRxiv, medRxiv). The matcher is conservative: anything unrecognized is excluded.
- Source stubs whose vault files weren't shipped get tagged `vault_missing: true` at merge time, with provenance carried over from the publishing wiki's manifest. The receiving user and any agent reading the wiki sees the tag immediately. Nothing is silently dropped.
- `hydrate-vault` never auto-fetches paywalled sources. It lists them with their URLs so the user can re-acquire via institutional access manually.

See `docs/licensing.md` for the full model, recommended publishing patterns, and the per-category fetch strategies.

### Pre-flight detectors

`subgraph-export` runs a pre-flight pass over the scope before write. Each detector surfaces findings with a plain-language rationale; the user accepts, refuses (`--strict`), or skips (`--no-preflight`). Findings land in `preflight_findings` in the manifest so receivers can see what was flagged at publish time. Detectors:

- **non_native_page** — chain-merge contamination. Default-excluded; `--include-non-native` to override.
- **quote_density** — wiki pages dominated by block-quoted source text (default threshold 25%). Fair-use signal, not a hard rule.
- **license_inconsistent** — declared license disagrees with publisher domain.
- **gpl_contagion** — GPL/AGPL/LGPL license markers; copyleft propagation risk for your published wiki.
- **gdpr_likely_pii** — emails, phones, SSN, IBAN, payment-number patterns. False-positive heavy by design; the user verifies.
- **URL redaction** — strip query strings from `source_url` in the manifest by default; `--keep-url-params` to preserve.

These are heuristics, not legal review. They protect against the easy mistakes; they don't make a published wiki bulletproof.

## What is out of scope

- **Cryptographic provenance.** We don't sign wiki exports or verify signatures. A future feature could; for now, trust comes from "you knew the person you cloned from" and the audit report.
- **Sandbox escape.** This skill's scripts run with the user's shell privileges. Standard precautions apply — don't run merge as root, don't merge from a path you don't trust enough to read.
- **Content moderation.** We don't filter offensive or harmful content. The audit report shows you what's incoming; the choice to apply is yours.
