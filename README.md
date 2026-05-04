# curiosity-merge

Sharing/federation layer for [curiosity-engine](https://github.com/benjsmith/curiosity-engine) wikis.

Five verbs:

- **`subgraph-export`** — extract a self-contained mini-wiki (project, page neighborhood, or origin) for sharing. Defaults to bytes-free vault export (`--include-vault=none`); receivers re-hydrate with their own access. See `docs/licensing.md`.
- **`discover-bridges`** + **`accept-bridges`** — surface high-similarity page pairs that aren't yet wikilinked; apply user-checked candidates and update merge manifests so unmerge can unwind.
- **`merge`** — combine someone else's wiki into your own with full provenance, collision handling, untrusted-content framing, and `vault_missing: true` tagging for sources whose bytes weren't shipped.
- **`unmerge`** — surgically undo a previous merge using the manifest + receiving wiki state. Three-bucket classification preserves user curation.
- **`hydrate-vault`** — re-acquire missing sources tagged `vault_missing: true`. AlphaXiv-preferred for arXiv; per-category dispatch for preprints, open access, paywalled (manual), unknown.

This is a separate skill (not part of curiosity-engine) because it ingests external data and has a different trust model and release cadence. Most curiosity-engine users don't need it; install when you want to share or absorb wikis.

## Install

Requires curiosity-engine to be installed in the same workspace.

```bash
npx skills add -g -y benjsmith/curiosity-merge
bash <skill_path>/scripts/setup.sh
```

`setup.sh` checks for curiosity-engine and refuses to proceed without it.

## Usage

See `SKILL.md` for the full reference. Quick examples:

```bash
# Share a project as a self-contained mini-wiki
uv run python3 <skill_path>/scripts/subgraph_export.py \
    --project ai-safety --to /tmp/ai-safety-wiki

# Find unwritten cross-page links in your wiki
uv run python3 <skill_path>/scripts/discover_bridges.py --limit 50

# Absorb someone else's wiki
git clone https://github.com/someone/their-wiki /tmp/their-wiki
uv run python3 <skill_path>/scripts/merge.py /tmp/their-wiki --as-origin someone
# review .curator/merge-<timestamp>.md, then approve the staged swap
```

## Publishing a sub-wiki

After `subgraph-export`:

1. `cd <export-path> && git init && git add . && git commit -m "Initial export"`
2. Create a public GitHub repo and push
3. **Settings → Topics → add `curiosity-wiki`** so others can discover it via [github.com/topics/curiosity-wiki](https://github.com/topics/curiosity-wiki)

See `docs/publishing.md` for the full recipe.

## Layout

```
curiosity-merge/
├── SKILL.md                     # canonical reference
├── README.md                    # this file
├── PLAN.md                      # implementation plan
├── CHANGELOG.md
├── docs/
│   ├── architecture.md
│   ├── trust-model.md
│   ├── licensing.md
│   └── publishing.md
├── scripts/
│   ├── setup.sh
│   ├── subgraph_export.py
│   ├── discover_bridges.py
│   ├── accept_bridges.py
│   ├── merge.py
│   ├── unmerge.py
│   ├── hydrate_vault.py
│   ├── reconcile.py
│   └── merge_evolve_guard.sh   # hash-guard (named distinctly from curiosity-engine's)
└── template/
    └── prompts.md
```

## License

MIT — see `LICENSE`.
