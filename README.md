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

The supported install is a **bare skill install** alongside an existing
[curiosity-engine](https://github.com/benjsmith/curiosity-engine) workspace.
For the Switchbay and okbay integration pins, use curiosity-merge **v0.8.3**
(commit `4a533425f39a56bb23668a5d3831bf4df64fa0e8`). The exact consumer
contract is in [`docs/INSTALLER-CONTRACT.md`](docs/INSTALLER-CONTRACT.md).

```bash
# Convenience install (latest published skill; pin the tag/SHA in a product installer).
npx skills add -g -y benjsmith/curiosity-merge

# Run from the target curiosity-engine workspace.
bash <skill_path>/scripts/setup.sh
```

`setup.sh` takes no positional arguments. Its input is the current workspace,
plus the optional `CURIOSITY_ENGINE_SCRIPTS_DIR` override. It requires `git`,
Python 3.9+, `uv`, and a curiosity-engine `scripts/` directory containing
`naming.py` and `sweep.py`; it refuses to continue when those prerequisites are
missing. It writes `.curator/.curiosity-merge-env` in the workspace and
`~/.config/curiosity-merge/env`, and prints the host allowlist patterns needed
for this skill. It does not install a daemon, open a port, or modify wiki/vault
content. Optional alphaxiv and Presidio installs are prompted only in an
interactive terminal and are not required for the core skill.

`setup.sh` cannot modify the caller's environment. Source the generated
workspace env file (or export `CURIOSITY_ENGINE_SCRIPTS_DIR`) before invoking a
script. For CI or a product installer, set
`CURIOSITY_MERGE_NONINTERACTIVE=1` to suppress optional prompts.

### Product UI boundary

curiosity-merge (CM) owns **no product UI**: no PWA, QML, HTML, rail, tabs,
settings screen, daemon, reverse proxy, or model/harness registry. It owns the
merge and sanitization skill and produces filesystem artifacts, staging trees,
manifests, audit reports, and exit codes. Switchbay and okbay own their UI and
invoke CM as a headless dependency; they must not copy CM UI into their shells.

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

# Rejoin a U4 shard on its seam IRIs (identity-keyed, not slug-keyed)
uv run python3 <skill_path>/scripts/merge.py \
    --import-shard /tmp/shard-export.json /tmp/shard-wiki --as-origin shard1

# Parallel shards: stage each, then apply FIFO with one graph rebuild
uv run python3 <skill_path>/scripts/merge.py --apply-queue
```

Merge reconciles entities by stable IRI (curiosity-engine U1) when pages
carry one — same-IRI or overlapping-`same_as` pages collapse into one
canonical page regardless of slug. Wikis with no minted IRIs merge by
filename stem exactly as before.

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
│   ├── publishing.md
│   └── INSTALLER-CONTRACT.md      # bare, Switchbay, and okbay install contract
├── scripts/
│   ├── setup.sh
│   ├── subgraph_export.py
│   ├── discover_bridges.py
│   ├── accept_bridges.py
│   ├── merge.py
│   ├── unmerge.py
│   ├── hydrate_vault.py
│   ├── reconcile.py            # vault sha256 + page-stem collision (stem fallback)
│   ├── identity.py            # IRI-keyed entity reconciliation (U1) + shard seam joins (U4)
│   ├── preflight.py            # detectors (chain-merge, quote-density, license, GPL, regex PII)
│   ├── presidio_gate.py        # optional Presidio NER+ML PII detector (v0.3.0)
│   └── merge_evolve_guard.sh   # hash-guard (named distinctly from curiosity-engine's)
└── template/
    └── prompts.md
```

## License

MIT — see `LICENSE`.
