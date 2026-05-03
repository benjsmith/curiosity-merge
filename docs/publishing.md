# Publishing a sub-wiki

`subgraph-export` produces a self-contained mini-wiki at a destination path. Publishing it on GitHub for others to discover is three steps.

## 1. Export

Pick a scope. The three options:

```bash
# Project: every page tagged projects: [<name>] plus their cited vault files
uv run python3 <skill_path>/scripts/subgraph_export.py \
    --project ai-safety --to /tmp/ai-safety-wiki

# Page neighborhood: a single page plus its 1-hop wikilink neighbors
uv run python3 <skill_path>/scripts/subgraph_export.py \
    --page concepts/transformer --include-1-hop --to /tmp/transformer-wiki

# Origin: every page tagged origin: <name> (only useful after a previous merge)
uv run python3 <skill_path>/scripts/subgraph_export.py \
    --origin alice --to /tmp/alice-redux
```

Inspect the output. The destination is a normal curiosity-engine wiki layout (`vault/`, `wiki/`, `.curator/projects.json`) plus an `_export-manifest.json`.

## 2. Init and push

```bash
cd /tmp/ai-safety-wiki
git init
git add .
git commit -m "Initial export of ai-safety sub-wiki"

gh repo create ai-safety-wiki --public --source=. --push
```

(Or use the GitHub UI if you don't have `gh` installed.)

## 3. Tag with `curiosity-wiki`

In the GitHub UI:

> Repository → ⚙️ (next to "About") → **Topics** → add `curiosity-wiki`

Or via API:

```bash
gh api -X PUT repos/<you>/<repo>/topics -f names[]=curiosity-wiki
```

That's it. Your sub-wiki now appears in the [github.com/topics/curiosity-wiki](https://github.com/topics/curiosity-wiki) discovery feed.

## What the receiving end does

Someone who finds your wiki and wants to absorb it:

```bash
git clone https://github.com/<you>/<repo> /tmp/your-wiki
cd <their-curiosity-engine-workspace>
uv run python3 <skill_path>/scripts/merge.py /tmp/your-wiki \
    --as-origin <your-handle>
# review .curator/merge-<timestamp>.md
uv run python3 <skill_path>/scripts/merge.py --apply <your-handle>
git -C wiki add -A
git -C wiki commit -m "merge: absorb <your-handle>'s wiki"
```

Their wiki keeps full provenance: every page they imported has `origin: <your-handle>` in frontmatter, and the audit report records every reconciliation, every collision, and every cross-origin bridge candidate.

## What to put in the README of a published sub-wiki

Suggested template:

```markdown
# <topic> — a curiosity-wiki

Exported from <your-personal-wiki> on YYYY-MM-DD.

## Scope

- Project: `<project>`
- Pages: <count>
- Vault sources: <count>

## How to use

This is a [curiosity-engine](https://github.com/benjsmith/curiosity-engine) wiki. To absorb it into your own:

\`\`\`bash
git clone <this-repo> /tmp/clone
cd <your-workspace>
uv run python3 <skill_path>/scripts/merge.py /tmp/clone --as-origin <handle>
\`\`\`

(Requires [curiosity-merge](https://github.com/benjsmith/curiosity-merge) installed.)

To browse standalone, install curiosity-engine in this directory:

\`\`\`bash
bash <skill_path>/scripts/setup.sh
bash <skill_path>/scripts/viewer.sh
\`\`\`
```

## Discovery beyond the topic tag

A future curated index of high-quality curiosity-wikis lives at `benjsmith/curiosity-wikis-index`. PRs welcome.

A future `curiosity browse` command may hit the GitHub API for `topic:curiosity-wiki` repos directly, ranked by recency or stars. Deferred until there are enough wikis to justify it.

## Updating a published sub-wiki

After more curation in your main wiki, re-running `subgraph-export` to the same destination overwrites the export. The destination's `.git` is preserved, so:

```bash
uv run python3 <skill_path>/scripts/subgraph_export.py \
    --project ai-safety --to /tmp/ai-safety-wiki

cd /tmp/ai-safety-wiki
git add -A
git commit -m "Sync from $(date -I)"
git push
```

The `_export-manifest.json` records `exported_at`, so subscribers can tell from the manifest when the export is fresh.
