# Licensing model for shared wikis

Most curiosity-engine vaults end up holding sources whose copyright doesn't belong to the user — arXiv preprints, paywalled journal articles, copyrighted blog posts, news articles. The notes the user wrote *on top of* those sources are their own work. The two have very different sharing rules.

curiosity-merge separates them: **share notes, not sources**. Receivers re-acquire sources themselves, using `hydrate-vault` and whatever access they have.

## The premise

When you publish a sub-wiki for others to clone:

- **Wiki pages (your notes, analyses, summaries, citations)** → safe to publish under whatever license you choose for your own writing (default suggestion: CC-BY).
- **Vault files (the source PDFs, archived HTML, paper extractions)** → **not bundled by default**. The export manifest records every cited vault file's `sha256`, `source_url`, `source_type`, and `license` so receivers know exactly what to fetch and from where, but the bytes don't ship.

When someone merges your wiki into theirs:

- The wiki pages land in their wiki with `origin: <you>` and `untrusted: true` framing (the standard merge defenses).
- Source stubs whose vault files weren't shipped get tagged `vault_missing: true` with the recorded provenance. The receiving user (and any agent reading the wiki) sees `vault_missing: true` immediately and can run `hydrate-vault` to re-acquire — using their own institutional access for paywalled sources, or open download for preprints.

This puts the sharing question on the right footing: **you're sharing your reading and synthesis, not the publisher's text.** Republishing the publisher's text is what creates licensing problems; sharing your notes generally does not.

## subgraph-export modes

`subgraph_export.py` has `--include-vault {none,owned,all}`:

| Mode | Bundles | When to use |
|---|---|---|
| `none` (default) | nothing from vault/ | **Public sharing on GitHub.** Always safe; works regardless of source mix. |
| `owned` | only files whose frontmatter declares a redistributable license, OR whose `source_url` is on arXiv / bioRxiv / chemRxiv (whose default licenses permit redistribution) | Sharing where you want preprints to ride along but paywalled content to stay back. |
| `all` | every cited vault file | **Personal transfer only** — moving your own work between two of your own machines, or sharing privately with someone who already has the same access rights. Not safe for public publishing. |

A vault file is treated as "redistributable" when its frontmatter has either:

- `redistributable: true` — explicit declaration, set by the user
- `license: <ID>` where `<ID>` matches one of the recognized open licenses: `CC0`, `CC-BY`, `CC-BY-SA`, `CC-BY-NC`, `CC-BY-ND`, `MIT`, `Apache-2.0`, `BSD-*`, `arxiv-non-exclusive`, `public-domain`
- `source_url` on a known preprint domain (`arxiv.org`, `biorxiv.org`, `chemrxiv.org`, `medrxiv.org`)

Anything else is treated as not-known-redistributable and excluded under `--include-vault=owned`. Conservative by design: defaults that aren't safe must fail closed.

## What the manifest records

Every cited vault file appears in `_export-manifest.json` under `vault_metadata`, regardless of include mode:

```json
{
  "rel": "vaswani-2017-attention.extracted.md",
  "sha256": "e2e6329e...",
  "source_url": "https://arxiv.org/abs/1706.03762",
  "source_type": "preprint",
  "title": "Attention Is All You Need",
  "license": "arxiv-non-exclusive",
  "redistributable": true
}
```

The receiver can see at a glance which sources will need hydration and from where. Privacy note: this means the manifest reveals which sources you've read in this scope. If the URL itself is sensitive (e.g. a private working paper), redact it before publishing.

## hydrate-vault flow

After a receiver merges your sub-wiki into theirs:

```
uv run python3 <skill_path>/scripts/hydrate_vault.py --origin <your-name>
# dry run — categorizes and reports

uv run python3 <skill_path>/scripts/hydrate_vault.py --origin <your-name> --apply
# fetches per category, with per-source confirmation
```

Categorization by `source_url` domain:

| Category | Strategy |
|---|---|
| `arxiv` | **AlphaXiv preferred** when installed (cleaner extractions); falls back to PDF download + curiosity-engine `local_ingest`. |
| `biorxiv` / `chemrxiv` / `medrxiv` | PDF download + `local_ingest`. |
| `open_access` | PLOS, PMC, OpenReview, ACL Anthology, anything with redistributable license declared in frontmatter — direct fetch + `local_ingest`. |
| `paywalled` | Nature, Elsevier, Springer, IEEE, ACM, Cell, etc. — **not auto-fetched**; reported with the URL so the user can grab via institutional access manually. |
| `unknown` | No recognized URL or license — listed for manual handling. |

The fetched file's sha256 is compared against the manifest's recorded `vault_sha256`. A mismatch means the source has changed since the publishing user read it (revised arXiv preprint, edited blog post). The fetched file is saved with a `.candidate` suffix and flagged for the user — never silently substituted, since downstream notes may rely on the original wording.

After successful hydration, the source stub's `vault_missing: true` flag is cleared.

## Recommendations for publishing wikis

- **License your notes.** A short `LICENSE.md` in the published repo (CC-BY-4.0 is a common choice) tells receivers what they can do with your prose.
- **Don't redact your `source_url`s.** They're how receivers re-acquire. If a URL is itself sensitive, the right move is to drop the source entirely from the export, not to publish a stub pointing at a redacted URL.
- **Annotate your sources' licenses.** When you ingest a paper, set `license:` in the vault file's frontmatter. The `--include-vault=owned` mode rewards this; without it, no preprint rides along even when it legally could.
- **Use `--include-vault=owned` for science-heavy wikis.** Preprints (arXiv/bioRxiv/chemRxiv) ship; paywalled papers stay back; the receiver only has to manually fetch the closed-access subset. Big quality-of-life improvement over `--include-vault=none`.
- **Use `--include-vault=none` when in doubt.** Always safe.

## Recommendations for merging others' wikis

- **Skim the audit report.** It lists every `vault_missing` source with URL + license. You'll know up-front what you're committing to re-acquire.
- **Run `hydrate-vault --origin <name>` early.** Don't wait until you're reading a page and discover citations that go nowhere. Dry-run first to see the breakdown.
- **Install alphaxiv for clean arXiv extractions.** The setup.sh prompt offers it; if you skipped, install via `npx skills add -g -y benjsmith/alphaxiv`. Worth the one command.
- **Paywalled sources are your problem.** The script lists them with URLs; you fetch via your institution's access and re-run.

## Frontmatter keys this skill reads

For licensing decisions, curiosity-merge reads these frontmatter keys from vault files (in addition to `source_url`, which curiosity-engine already tracks):

| Key | Type | Meaning |
|---|---|---|
| `license` | string | An identifier like `CC-BY`, `MIT`, `arxiv-non-exclusive`. Free-form; the matcher is case-insensitive against the recognized list. |
| `redistributable` | bool | Explicit override. `true` forces inclusion under `--include-vault=owned`; `false` forces exclusion. |

These are *not* (currently) in curiosity-engine's `ALLOWED_FM_KEYS` allowlist — curiosity-engine ignores them at parse time. curiosity-merge reads them via a separate raw frontmatter probe specifically for licensing decisions, so the values are honored even though curiosity-engine doesn't propagate them to the rest of its pipeline.

If/when curiosity-engine adds these to its allowlist, the values will additionally surface in lint reports, the viewer UI, etc. — purely additive.

## Out of scope

- **License auto-detection from text.** We don't try to infer the license of a source by scanning its body for "© Elsevier 2024" or "CC-BY-4.0". The user marks the license at ingest time if they want; otherwise it's unknown.
- **Cryptographic provenance.** We don't sign exports or verify signatures of clones. Trust comes from "you knew the person you cloned from" and the audit report.
- **DRM-respecting fetchers.** `hydrate-vault` follows the URLs in the manifest with `urllib`. Sites that require login, JS-rendered redirects, or institutional VPN won't auto-fetch. Those land in the paywalled bucket — manual hydration is the only correct outcome.
