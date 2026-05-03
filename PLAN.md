# curiosity-merge — implementation plan

This skill is the **sharing/federation layer** for `curiosity-engine`,
a knowledge-wiki skill that does project-aware curation of a personal
vault. Daily curation lives in curiosity-engine. This skill adds the
operations users invoke when they want to combine wikis, extract
sub-wikis, or surface cross-wiki connection candidates.

It depends on curiosity-engine being installed in the same workspace
and reuses its scripts via `CURIOSITY_ENGINE_SCRIPTS_DIR` — do NOT
fork or duplicate that codebase.

**Read first** (in curiosity-engine's repo at github.com/benjsmith/curiosity-engine):
- `docs/multi-project.md` — locked design for the verbs, soft-delete
  model, recency planner, classifier, merge protocol, subgraph-export.
  This file is authoritative on what the verbs do and why.
- `README.md` Roadmap section — where these verbs sit in the larger plan.
- `scripts/naming.py` — frontmatter helpers, citation-stem convention,
  TYPE_PREFIX. This skill must use these verbatim.
- `scripts/sweep.py` — `wiki_pages`, `scan_wikilinks`, `_set_projects_field`
  patterns. Reuse, don't duplicate.
- `scripts/projects.py` — project lifecycle and registry (.curator/projects.json).
- `scripts/activity_log.py` — `log_event(...)` library API.
- `scripts/planner.py` — wave allocator that consumes `epoch_summary.project_activity`.

## Scope (three commands)

| Command | What it does |
|---|---|
| `merge ../other-wiki/ --as-origin <name>` | Combines another wiki into the current one. Reconciles vault sha256, source-stub stems, page-name collisions. Tags every page from the other wiki with `origin: <name>`. Rebuilds graph. Runs `discover-bridges --across-origins` to surface cross-wiki link candidates. Writes audit report to `.curator/merge-<ts>.md`. Stages first; commits to wiki/.git only on user approval. |
| `subgraph-export --project X --to <path>` (also `--page X --include-1-hop`, `--origin X`) | Writes a self-contained mini-wiki with the named scope plus its transitively-cited vault sources. Manifest at `_export-manifest.json` describing scope and origin. Suitable for git push to GitHub for sharing. |
| `discover-bridges [--across-origins] [--limit N]` | Semantic-similarity sweep over page pairs that aren't yet wikilinked. Returns a review queue. With `--across-origins`, restricted to pairs where the two pages have different `origin:` tags. |
| `unmerge --origin <name>` | Undo a previous merge using the receiving wiki's own git + the per-origin merge manifest. Cannot rely on the source wiki's remote (it may have drifted). Distinguishes pure imports (safe remove), user-modified imports (preserve, flag), and native pages that reference imported content (rewrite to placeholders, flag). Stages everything; commits only on user approval. |

These verbs are locked in `curiosity-engine/docs/multi-project.md`. Use
verbatim. `merge` (cross-wiki, heavy) and `rename` (single-wiki,
mechanical project absorption — lives in curiosity-engine) are
deliberately distinct — never use "merge" for the project case.

## Trust model (this is the main reason for being a separate skill)

`merge` ingests external data. Threats to handle explicitly:

- **Adversarial frontmatter** — bypass attempts via `projects:`,
  malformed YAML targeting parser issues. Defence: route ALL incoming
  frontmatter through `naming.read_frontmatter`, which strips unknown
  keys via the existing ALLOWED_FM_KEYS allowlist. Don't add keys to
  that list lightly.
- **Prompt injection in markdown bodies** aimed at the receiving
  curator agent. Defence: wrap every merged page body in
  `<!-- BEGIN UNTRUSTED MERGED CONTENT — origin:<name> -->` framing
  and set `untrusted: true` in frontmatter (curiosity-engine convention
  for source extractions). Workers see the framing and treat content
  as data, not instructions.
- **Manipulated `(vault:...)` citations** pointing at non-existent or
  wrong-content vault files. Defence: every vault file referenced from
  merged pages must exist in the merged-vault index by sha256;
  citations to missing/different content get rewritten or flagged.
- **Path traversal** in CLI args (`--to ../../../etc/passwd`). Defence:
  reject paths containing `..` or absolute paths outside the workspace.
- **Page-name collisions** on substantive pages (both wikis have
  `concepts/transformer.md` with different content). Defence: NEVER
  silently overwrite. Always queue for human review with both versions
  preserved.

All merge work goes to `.curator/.merge-staging/<origin>/` first.
Atomic swap to live wiki only after the user reviews the audit report
and confirms.

## Architecture

- Own GitHub repo: `benjsmith/curiosity-merge`.
- `SKILL.md` describes the three verbs, trust model, staging flow, and
  discovery conventions.
- Declares dependency: requires `curiosity-engine` installed in the
  same workspace. `setup.sh` verifies and refuses without it.
- `setup.sh` reads `CURIOSITY_ENGINE_SCRIPTS_DIR` (or detects via
  Claude Code's `<skill_path>` of curiosity-engine) and exports it for
  this skill's scripts to import from.
- Scripts import shared helpers:
  ```python
  import os, sys
  ce_scripts = os.environ.get("CURIOSITY_ENGINE_SCRIPTS_DIR")
  if ce_scripts:
      sys.path.insert(0, ce_scripts)
  from naming import (read_frontmatter, set_frontmatter_field,
                      citation_stem, parse_source_meta,
                      TYPE_PREFIX, ALLOWED_FM_KEYS)
  from sweep import wiki_pages, scan_wikilinks
  ```
- Adds its own per-host allowlist entries (mirror curiosity-engine's
  pattern: dual-path .claude/settings.json, Codex/Gemini/Copilot
  config hooks, canary-based regen).
- Hash-guards its own scripts via its own `merge_evolve_guard.sh` (deliberately named distinctly from curiosity-engine's `evolve_guard.sh` so agents don't conflate the two when both skills are installed side-by-side; banner print on each run reinforces).

File layout

```
curiosity-merge/
├── SKILL.md
├── README.md
├── PLAN.md                          # this file
├── CHANGELOG.md
├── docs/
│   ├── architecture.md
│   ├── trust-model.md
│   └── publishing.md                # how to publish a sub-wiki: tag with curiosity-wiki, etc.
├── scripts/
│   ├── setup.sh                     # dep check, allowlist, host detection
│   ├── merge.py                     # merge command
│   ├── subgraph_export.py           # subgraph-export command
│   ├── discover_bridges.py          # discover-bridges command
│   ├── reconcile.py                 # vault sha256 + page-stem collision helpers
│   └── merge_evolve_guard.sh        # hash-guard (distinct name from curiosity-engine's evolve_guard.sh)
└── template/
    └── prompts.md                   # any worker prompt templates
```

Implementation order (each independently shippable)

1. **subgraph-export first.** Cleanest, no merge logic, no trust
   risk (operates on the user's own wiki). Define the export artifact:
   a normal curiosity-engine wiki layout (vault/, wiki/,
   .curator/projects.json) plus `_export-manifest.json` describing
   the scope (`{project: "ai-safety", exported_at: "...", origin_wiki: "...", scope_pages: [...], scope_vault: [...]}`).
   Useful immediately: "I want to share my project X."
2. **discover-bridges standalone.** Semantic-similarity sweep within
   one wiki, surfacing non-wikilinked high-similarity page pairs.
   Useful within a single wiki even before merge. Reuses
   curiosity-engine's embedding stack (sentence-transformers +
   sqlite-vec, behind `embedding_enabled: true`). Cold-start guard
   reuses the wave-4 pattern.
3. **merge.** Build on (1) for extraction semantics and (2) for
   cross-origin bridges. Stages: vault dedup → source-stub
   reconciliation → page-name collision queue → graph union → bridge
   discovery across origins → audit report → staged swap.
4. **unmerge.** Inverse of merge, but **not symmetric**. Cannot rely on
   the source wiki's remote being unchanged, and cannot blindly delete
   imported content because the user may have curated it after the merge.
   Approach:
   - At `merge.py --apply` time, write `.curator/merges/<origin>.json`:
     a manifest recording every imported wiki page (path + sha256 at
     import) and every imported vault file (path + sha256). This is the
     authoritative source of truth for what came from the merge.
   - At `unmerge.py --origin <name>` time, partition every manifest entry
     into three buckets using the receiving wiki's *current* state and
     `git -C wiki log` since the merge commit:
       a. **Pure imports** (`origin:` tag still present, sha256 unchanged
          since import) → safe to remove from staging, log to audit.
       b. **User-modified imports** (still tagged `origin:`, but sha256
          differs from the manifest) → user has curated this page since
          the merge; **never silently delete**. Stage for review with
          a copy of the original-import version alongside.
       c. **Already de-imported** (no longer present, or no longer tagged
          `origin:`) → user already pruned it; nothing to do, log only.
   - Walk the rest of the wiki for **native pages that reference imported
     content**: any wiki page (no `origin:` tag, or a different origin)
     that has `[[<imported-stem>]]` wikilinks or `(vault:<imported-rel>)`
     citations pointing at would-be-removed content. These are the
     user's own curation that *built on* the imported material; they
     can't be silently broken. Strategy:
       - Rewrite the wikilink/citation to a `[[<stem>]]` (raw, dead) or
         `(vault:<rel>)` (also dead), and append a comment block to the
         page: `<!-- unmerge: this page referenced <stem> from origin
         <name> at <unmerge-timestamp>; the target was removed -->`.
       - List every affected page in the audit report under
         `## Native pages with broken references after unmerge`.
       - User can then either restore the imports (cancel unmerge),
         delete those native pages, or rewrite the references manually.
   - Walk **accepted cross-origin bridges**: wikilinks the user accepted
     during merge's bridge-discovery pass, recorded in the manifest's
     `accepted_bridges` list. Each entry is a `(native_stem,
     imported_stem)` pair; on unmerge, the wikilink in `native_stem` is
     removed and logged.
   - Stage everything to `.curator/.unmerge-staging/<origin>/`. The
     audit report lists every safe-remove, every user-modified import
     awaiting decision, every native-page reference that will break,
     and every bridge that will be undone. `unmerge.py --apply` is the
     atomic swap; `--abandon` discards the staging directory.
   - Final step on apply: rebuild graph (`graph.py rebuild wiki`) and
     remove `.curator/merges/<origin>.json` (or move to `merges/.archive/`
     for forensics).
   - **Why not `git revert`?** A git revert would also undo any of the
     user's curation commits since the merge. The whole point of unmerge
     is to surgically extract just the merge-introduced content while
     preserving everything else. The manifest is the surgical guide.

## Use of curiosity-engine primitives — do, don't duplicate

DO use:
- `naming.citation_stem`, `naming.parse_source_meta`, `naming.read_frontmatter`,
  `naming.set_frontmatter_field`, `naming.TYPE_PREFIX`, `naming.STEM_PREFIX`,
  `naming.ALLOWED_FM_KEYS`.
- `sweep.wiki_pages` (already excludes `.deleted/` and dotfile-prefixed
  dirs).
- `sweep.scan_wikilinks` (citation graph parser).
- Run `graph.py rebuild wiki` after merge to regenerate kuzu graph.
- `lint_scores.compute_all` to score the merged wiki.
- `vault_index.py` to re-index merged vault for FTS5 + embeddings.
- `activity_log.log_event(...)` to emit ingest events for newly-imported
  vault files. Default `ingest_kind=archival` for merged content (the
  receiving user hasn't actively been working on it).
- `projects.py` for any project-lifecycle ops (rename, delete) the
  merge needs to perform. Don't reimplement.

DON'T:
- Fork curiosity-engine's codebase. Always import.
- Reimplement frontmatter parsing or mutation.
- Add a separate vault. The merged vault belongs to the receiving
  wiki's vault tree.
- Add a separate graph DB. Rebuild into `.curator/graph.kuzu`.
- Auto-apply bridge candidates. Always queue for review.
- Silently overwrite page-name collisions. Always flag for human.
- Trust frontmatter from external sources beyond ALLOWED_FM_KEYS.
- Follow path traversal in CLI args.

## Discovery convention

Public sub-wikis use the GitHub topic tag `curiosity-wiki`.

`README.md` (curiosity-merge) and `docs/publishing.md` document:
- How to tag your repo when publishing
  (Settings → Topics → add `curiosity-wiki`).
- Discovery: GitHub's `topic:curiosity-wiki` query.
- Curated index (when it lands at `benjsmith/curiosity-wikis-index`).
- Recipe: clone → `curiosity-merge merge <path> --as-origin <name>` →
  review audit report → commit.

A future `curiosity browse` command may hit the GitHub API for tagged
repos directly; deferred.

## Definition of done for first release

- `subgraph-export` works on a real curiosity-engine workspace,
  produces a valid mini-wiki that another curiosity-engine workspace
  can setup cleanly.
- `discover-bridges` runs on a real workspace with embeddings enabled,
  returns a sensible review queue.
- `merge` works on two real workspaces, produces a clean audit report,
  no silent collisions, no trust violations.
- `setup.sh` cleanly installs into a workspace with curiosity-engine
  already present; refuses cleanly when curiosity-engine is missing.
- README + docs document the publishing flow + topic convention.
- One example sub-wiki published to GitHub with the topic tag, demoed
  in README.

## Setup integration: optional-install hook in curiosity-engine

curiosity-engine's `setup.sh` already has the pattern, used today for caveman and semantic-search. Lines ~676–705 of `setup.sh` are the caveman block; the new prompt slots in right after, with the same shape:

```bash
# Optional: install curiosity-merge skill for cross-wiki operations
# (merge, subgraph-export, discover-bridges). Most users don't need
# this — only install when you want to combine wikis, share sub-wikis
# via GitHub, or absorb someone else's published wiki. Trust model is
# different (external data ingestion) so it's a deliberate opt-in.
if _is_interactive; then
    echo ""
    printf "Install curiosity-merge for cross-wiki sharing/merge ops? [y/N] "
    read -r reply_merge || reply_merge="n"
    case "$reply_merge" in
        y|Y|yes|YES)
            if command -v npx >/dev/null 2>&1; then
                echo "  Installing benjsmith/curiosity-merge via npx skills (global, symlinks) ..."
                npx skills add -g -y benjsmith/curiosity-merge \
                    || echo "  (install failed — re-run later: npx skills add -g -y benjsmith/curiosity-merge)"
            else
                echo "  npx not found. Install later: npx skills add -g -y benjsmith/curiosity-merge"
            fi
            ;;
        *)
            echo "  Skipping curiosity-merge. Install anytime: npx skills add -g -y benjsmith/curiosity-merge"
            ;;
    esac
fi
```

When to add it: not now, because the repo doesn't exist yet — the prompt would offer something that fails to install. Add it to curiosity-engine in the same session you create the curiosity-merge repo, ideally as the second commit after the new repo's first push. That way the prompt appears for any user who runs setup.sh after curiosity-merge becomes installable.

The cleanest way to coordinate this: when you start the new session, after the new repo is pushed, ask the agent to also add this hook to curiosity-engine.
