# Architecture

curiosity-merge is a thin federation layer on top of [curiosity-engine](https://github.com/benjsmith/curiosity-engine). It owns three verbs and **none** of the primitives — every frontmatter operation, citation graph walk, vault index build, and graph rebuild is delegated to curiosity-engine's scripts via Python imports.

## Why a separate skill

1. **Different trust model.** Daily curation operates on user-controlled vault content. `merge` ingests entire wikis from elsewhere on the internet. The defenses (untrusted framing, sha256 citation validation, frontmatter allowlist enforcement) only matter for the cross-wiki case and would be dead weight in the daily-curation hot path.
2. **Different audience.** Most curiosity-engine users will never run `merge`. They have one wiki and they curate it. Bundling these verbs into the daily skill bloats SKILL.md and the bash allowlist for everyone.
3. **Different release cadence.** Federation features (publishing conventions, discovery indices, GitHub topic conventions) evolve faster than the curation primitives. Keeping them in their own repo lets that iteration happen without churning curiosity-engine's stable surface.

## Dependency on curiosity-engine

This skill imports — never duplicates — these curiosity-engine modules:

| Module | What we use |
|---|---|
| `naming` | `read_frontmatter`, `set_frontmatter_field`, `citation_stem`, `parse_source_meta`, `TYPE_PREFIX`, `STEM_PREFIX`, `ALLOWED_FM_KEYS`, `WIKILINK_RE`, `CITATION_RE` |
| `sweep` | `wiki_pages`, `scan_wikilinks` |
| `projects` | project-lifecycle ops (rename, delete) when merge needs them |
| `activity_log` | `log_event(...)` to emit ingest events for newly-imported vault files (default `ingest_kind=archival`) |
| `graph` | `rebuild wiki` after merge |
| `lint_scores` | scoring the merged wiki |
| `vault_index` | re-indexing the merged vault for FTS5 + embeddings |

### Path resolution

`setup.sh` exports `CURIOSITY_ENGINE_SCRIPTS_DIR` — the absolute path to curiosity-engine's `scripts/` directory in the same workspace. Each script in this skill begins with:

```python
import os, sys
ce_scripts = os.environ.get("CURIOSITY_ENGINE_SCRIPTS_DIR")
if ce_scripts:
    sys.path.insert(0, ce_scripts)
from naming import read_frontmatter, citation_stem  # etc.
```

If `CURIOSITY_ENGINE_SCRIPTS_DIR` is unset (e.g. a script is run directly without setup having configured the env), the import fails with a clear "curiosity-engine not on path" message and an instruction to run setup.

## File layout

```
curiosity-merge/
├── SKILL.md
├── README.md
├── PLAN.md
├── CHANGELOG.md
├── docs/
│   ├── architecture.md      # this file
│   ├── trust-model.md
│   └── publishing.md
├── scripts/
│   ├── setup.sh             # dep check, allowlist install, env var export
│   ├── subgraph_export.py
│   ├── discover_bridges.py
│   ├── merge.py
│   ├── reconcile.py         # vault sha256 + page-stem collision helpers
│   └── merge_evolve_guard.sh # hash-guard (named distinctly from curiosity-engine's evolve_guard.sh)
└── template/
    └── prompts.md           # any worker prompt templates
```

## Script invocation discipline

Inherits curiosity-engine's bash rules:

- All Python via `uv run python3 <skill_path>/scripts/<name>.py ...` so the workspace `.venv` is auto-discovered.
- All git via `git -C wiki ...`.
- No compound shell, no pipes, no `$(...)`.

`setup.sh` extends the host allowlist (Claude Code, Codex, Gemini, Copilot) with this skill's script paths, mirroring curiosity-engine's protocol. Marker file `.curator/.allowlist-installed-curiosity-merge-<host>` prevents re-prompts.

## Hash guarding

`merge_evolve_guard.sh` records sha256 of every script in this skill and refuses to run if a file changed unexpectedly. Same protocol as curiosity-engine's `evolve_guard.sh`, but **deliberately named distinctly** — both skills are typically installed side-by-side under `~/.claude/skills/`, and a shared filename would invite an agent to invoke the wrong guard. The script also prints `[curiosity-merge guard]` to stderr on every run so log review is unambiguous.

The two are not interchangeable: each guard's `GUARDED` array enumerates only its own skill's scripts. If curiosity-engine ever generalizes its guard to take an external file list as input, this skill's guard could become a thin wrapper that defers to it; until then, the duplication is local (~70 lines of stdlib bash) and the naming carries the disambiguation.

## Subgraph export artifact

Output of `subgraph-export` is a normal curiosity-engine wiki layout plus one extra file:

```
<export-path>/
├── vault/
├── wiki/
│   └── projects/<project>.md   # if --project scope
├── .curator/
│   └── projects.json
└── _export-manifest.json
```

`_export-manifest.json` schema:

```json
{
  "schema_version": 1,
  "exported_at": "2026-05-03T22:00:00Z",
  "origin_wiki": "<absolute path of source wiki>",
  "origin_label": "<optional human label, e.g. 'ben-personal'>",
  "scope": {
    "kind": "project | page | origin",
    "value": "<project-name | page-stem | origin-name>",
    "include_1_hop": true
  },
  "scope_pages": ["entities/transformer.md", "..."],
  "scope_vault": ["vault/vaswani-2017.extracted.md", "..."],
  "schema_notes": "..."
}
```

The receiving workspace's `merge` command reads `_export-manifest.json` if present, but does not require it — a plain wiki layout (without a manifest) is also a valid merge source.

## Merge staging

`merge.py` writes everything to `.curator/.merge-staging/<origin>/`:

```
.curator/.merge-staging/<origin>/
├── wiki-incoming/         # remapped pages from the other wiki
├── vault-incoming/        # remapped vault files
├── collisions/            # un-resolved page-name collisions
├── audit-report.md        # human-readable summary
└── apply.json             # machine-readable swap manifest
```

The atomic swap is a separate command (`merge.py --apply <origin>`) that:

1. Verifies the staging directory's sha256 hasn't changed since the report was written
2. Moves files from staging into the live `wiki/` and `vault/`
3. Runs `graph.py rebuild wiki`
4. Logs to `.curator/log.md`

If the user wants to abandon the merge: `merge.py --abandon <origin>` removes the staging directory.

## What this skill explicitly does not do

- No fork of curiosity-engine's codebase.
- No reimplementation of frontmatter parsing or mutation — always import.
- No separate vault. The merged vault belongs to the receiving wiki's vault tree.
- No separate graph DB. Rebuild into `.curator/graph.kuzu`.
- No auto-applying of bridge candidates. Always queue for review.
- No silent overwrite of page-name collisions. Always flag for human.
- No expansion of `ALLOWED_FM_KEYS` for merged content.
- No following of path traversal in CLI args.
