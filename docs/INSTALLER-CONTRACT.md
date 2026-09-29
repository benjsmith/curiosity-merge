# Installer contract

**Status:** Phase 3 contract, locked 2026-09-18 (pins refreshed 2026-09-29 for v0.8.4)  
**Owner:** curiosity-merge (CM)  
**Scope:** bare-skill, Switchbay, and okbay installation

This document is the integration contract for CM. It separates the headless
skill from the product shells that install and present it. The repository's
`README.md` is the short user-facing summary; `SKILL.md` is the agent-facing
reference.

## Version pins

A product installer must pin the CM tag **and** its commit. A tag is the
human-readable release name; the commit is the machine-verifiable content
pin. If they disagree, installation must stop rather than silently resolving
a floating branch.

| Component | Phase 3 pin | Commit | Consumers |
|---|---|---|---|
| curiosity-engine | `v1.9.0` (**pending tag**) | `301858011097d6811801785380d056ecdd158dc9` (origin/main tip at docs cut) | Switchbay, okbay, bare CM dependency |
| curiosity-merge | `v0.8.4` (**pending tag**; this release) | a644350e25e4e19cf1a9bac28392f8e22bb33801 | Switchbay and okbay |
| switchbay (consumer) | `v0.13.0` (**pending**) | — | Records CE+CM pins in its release metadata |

These are the current integration pins, not a promise that every CM command
requires this exact curiosity-engine release. CM imports curiosity-engine
helpers and therefore requires a compatible helper tree; product installers
must update and test the pair together.

When bumping either pin, update the pair in the consuming installer, record
both new tags and full SHAs in its release metadata, and run the CM smoke
command below from a clean target workspace. Do not use `main`, `master`,
`latest`, or an unqualified `npx skills add` resolution as a production pin.

## Common CM installer interface

### Inputs

The installer runs from the target curiosity-engine workspace and accepts:

- **Source:** a CM checkout or skill directory resolved to the pinned tag/SHA.
- **Workspace:** the current directory (`pwd`); `wiki/` and `.curator/` are
  recommended for a useful workspace, but setup warns rather than failing when
  they are absent.
- **Optional helper override:** `CURIOSITY_ENGINE_SCRIPTS_DIR`, pointing to a
  directory containing `naming.py` and `sweep.py`.
- **Optional prompt control:** `CURIOSITY_MERGE_NONINTERACTIVE=1`, which
  suppresses optional alphaxiv and Presidio prompts.

`scripts/setup.sh` has no positional arguments. Required host prerequisites
are `git`, Python 3.9 or newer, and `uv`.

### Outputs

On success, `scripts/setup.sh`:

1. validates the prerequisites and resolves the curiosity-engine helper path;
2. writes `<workspace>/.curator/.curiosity-merge-env`;
3. writes `~/.config/curiosity-merge/env` as a user-scoped fallback; and
4. prints the CM script and hash-guard allowlist patterns for the host CLI.

The script does **not** export variables into the calling shell. Callers must
source the workspace env file or set `CURIOSITY_ENGINE_SCRIPTS_DIR` in the
process that invokes CM. It does not start a CM daemon, open a network port,
install a UI, write a registry, or change `wiki/` or `vault/` content. Optional
companion installs are additive and occur only after an interactive user
accepts them.

Exit status is `0` when setup completes, and non-zero when a prerequisite or
the curiosity-engine dependency is missing. A missing `wiki/` or `.curator/`
directory is a warning, not an installer failure.

### Runtime outputs

CM commands are filesystem-oriented. Depending on the verb, they write an
export tree and `_export-manifest.json`, merge/unmerge staging trees, queue
and audit manifests, hydration metadata, and exit status. The exact command
contracts remain in `SKILL.md`; consumers should treat these files and status
codes as the API rather than importing CM internals.

## Switchbay contract

Switchbay owns the PWA shell, tabs/rail/settings UI, service lifecycle, and
same-origin reverse proxy. Its installer may bundle CM, but it must:

1. install the pinned CE + CM pair above;
2. run `bash <cm-skill-path>/scripts/setup.sh` from the selected workspace;
3. pass `CURIOSITY_MERGE_NONINTERACTIVE=1` for unattended installation;
4. make the generated helper environment available to the CM subprocess; and
5. expose CM version/tag/SHA in its diagnostics or release metadata.

Switchbay must invoke CM headlessly. CM must not be embedded as a page,
iframe, rail panel, tab, QML surface, or settings implementation. Switchbay
presents CM's status and artifacts; CM does not own that presentation.

## okbay contract

okbay owns the Omarchy plugin, QML/HTML mode, keybinds, service lifecycle, and
settings that write the okstratr registry. Its installer must apply the same
five requirements as Switchbay, using the same CE + CM pins and the same
noninteractive setup mode for automation.

okbay must invoke CM headlessly from its service/CLI integration. CM must not
add QML, HTML, a panel, a rail, an Omarchy plugin surface, a daemon, or a
registry writer. okbay presents CM's status and artifacts and remains
responsible for the QML/HTML mutex and all product UI.

## Bare-skill contract

A user may install CM without either shell:

```bash
npx skills add -g -y benjsmith/curiosity-merge
bash <skill_path>/scripts/setup.sh
source .curator/.curiosity-merge-env
uv run python3 <skill_path>/scripts/preflight.py --help
```

For reproducible bare-skill deployments, resolve the CM source to `v0.8.4`
(tag pending; use the merge commit of this release’s docs PR until tagged) and
install the matching CE pin (`v1.9.0` pending — tip `301858011097d6811801785380d056ecdd158dc9`). Until CE is
tagged, product installers must record **“pending CE v1.9.0”** at that tip
consistently rather than mixing v1.8.2 language. The final
`preflight.py --help` invocation is a no-write smoke check that proves the
helper path is usable without running a merge or touching user data.
