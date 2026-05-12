#!/usr/bin/env bash
# setup.sh — curiosity-merge installer.
#
# Verifies curiosity-engine is present in the workspace and exports
# CURIOSITY_ENGINE_SCRIPTS_DIR so this skill's Python scripts can import
# shared helpers (naming, sweep, projects, activity_log, graph,
# lint_scores, vault_index).
#
# Allowlist install (so the bash surface is pre-approved on Codex,
# Gemini, Copilot, Cursor) follows curiosity-engine's protocol but
# extends with this skill's script paths. We delegate to curiosity-
# engine's setup.sh for the host-detection and approval prompt; here
# we only append the per-host marker file path so re-runs are quiet.

set -e

echo "=== Curiosity Merge Setup ==="

_is_interactive() {
    [ "${CURIOSITY_MERGE_NONINTERACTIVE:-0}" != "1" ] && [ -t 0 ] && [ -t 1 ]
}

# Resolve our own scripts dir. Same logical/physical dance as curiosity-
# engine — Claude Code's <skill_path> may be a symlink; allowlist needs
# both forms when they differ.
_src_dir="$(dirname "$0")"
SCRIPT_DIR_LOGICAL="$(cd "$_src_dir" && pwd)"
SCRIPT_DIR_PHYSICAL="$(cd "$_src_dir" && pwd -P)"
SKILL_ROOT_LOGICAL="$(dirname "$SCRIPT_DIR_LOGICAL")"
SKILL_ROOT_PHYSICAL="$(dirname "$SCRIPT_DIR_PHYSICAL")"

# Pre-flight: hard requirements. git (the wiki is a git repo), python3
# >= 3.9, uv (canonical Python invocation is `uv run python3 ...`).
if ! command -v git >/dev/null 2>&1; then
    echo "ERROR: git not found on PATH."
    exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: python3 not found on PATH. Install Python 3.9 or newer first."
    exit 1
fi
_py_major=$(python3 -c "import sys; print(sys.version_info.major)")
_py_minor=$(python3 -c "import sys; print(sys.version_info.minor)")
if [ "$_py_major" -lt 3 ] || { [ "$_py_major" -eq 3 ] && [ "$_py_minor" -lt 9 ]; }; then
    echo "ERROR: Python ${_py_major}.${_py_minor} found; needs 3.9+."
    exit 1
fi
if ! command -v uv >/dev/null 2>&1; then
    echo "ERROR: uv not found. Install from https://astral.sh/uv (curiosity-engine's setup.sh installs it)."
    exit 1
fi

# Hard dependency: curiosity-engine must be installed somewhere this
# workspace can find it. Probe candidates in order:
#   1. CURIOSITY_ENGINE_SCRIPTS_DIR already set by the caller
#   2. <skill_path>/../curiosity-engine/scripts (sibling install)
#   3. ~/.claude/skills/curiosity-engine/scripts (Claude Code default)
#   4. ~/.agents/skills/curiosity-engine/scripts (npx-skills physical)
#
# A "valid" curiosity-engine scripts dir contains naming.py + sweep.py.
_validate_ce_scripts() {
    [ -f "$1/naming.py" ] && [ -f "$1/sweep.py" ]
}

CE_SCRIPTS=""
if [ -n "${CURIOSITY_ENGINE_SCRIPTS_DIR:-}" ] && _validate_ce_scripts "$CURIOSITY_ENGINE_SCRIPTS_DIR"; then
    CE_SCRIPTS="$CURIOSITY_ENGINE_SCRIPTS_DIR"
fi
if [ -z "$CE_SCRIPTS" ]; then
    for cand in \
        "$(dirname "$SKILL_ROOT_PHYSICAL")/curiosity-engine/scripts" \
        "$(dirname "$SKILL_ROOT_LOGICAL")/curiosity-engine/scripts" \
        "$HOME/.claude/skills/curiosity-engine/scripts" \
        "$HOME/.agents/skills/curiosity-engine/scripts"; do
        if _validate_ce_scripts "$cand"; then
            CE_SCRIPTS="$cand"
            break
        fi
    done
fi

if [ -z "$CE_SCRIPTS" ]; then
    echo ""
    echo "ERROR: curiosity-engine not found."
    echo ""
    echo "curiosity-merge depends on curiosity-engine being installed in"
    echo "the same environment. Install it first:"
    echo ""
    echo "  npx skills add -g -y benjsmith/curiosity-engine"
    echo "  bash <skill_path>/scripts/setup.sh   # in your workspace"
    echo ""
    echo "Then re-run this setup."
    exit 1
fi

echo "Found curiosity-engine at: $CE_SCRIPTS"

# Persist the path. Two surfaces:
#   - .curator/.curiosity-merge-env   (workspace-scoped, sourced by callers
#     that aren't Claude Code)
#   - ~/.config/curiosity-merge/env   (user-scoped fallback)
#
# Claude Code's `<skill_path>` substitution doesn't help here because we
# need the *curiosity-engine* path, not our own. The env-var approach is
# the durable answer.
WORKSPACE_DIR="$(pwd)"
mkdir -p "$WORKSPACE_DIR/.curator"
ENV_FILE="$WORKSPACE_DIR/.curator/.curiosity-merge-env"
{
    echo "# curiosity-merge environment (written by setup.sh)"
    echo "export CURIOSITY_ENGINE_SCRIPTS_DIR=\"$CE_SCRIPTS\""
} > "$ENV_FILE"
echo "Wrote $ENV_FILE"

USER_ENV_DIR="$HOME/.config/curiosity-merge"
mkdir -p "$USER_ENV_DIR"
{
    echo "# curiosity-merge environment (user-scoped fallback)"
    echo "export CURIOSITY_ENGINE_SCRIPTS_DIR=\"$CE_SCRIPTS\""
} > "$USER_ENV_DIR/env"
echo "Wrote $USER_ENV_DIR/env"

# Workspace must be a curiosity-engine wiki to be useful. Check for the
# .curator directory and a wiki/ subdir; warn but don't fail if absent
# (someone may be running setup before initializing a wiki).
if [ ! -d "$WORKSPACE_DIR/wiki" ] || [ ! -d "$WORKSPACE_DIR/.curator" ]; then
    echo ""
    echo "WARNING: current directory does not look like a curiosity-engine"
    echo "         workspace (missing wiki/ or .curator/)."
    echo "         curiosity-merge commands operate on the current workspace's"
    echo "         wiki/ tree — make sure you run them from the workspace root."
fi

# Allowlist install. We don't reimplement curiosity-engine's host-detection;
# we just emit the patterns this skill needs, and the user (or curiosity-
# engine's setup) installs them. Print the patterns to stdout so they're
# auditable; the actual install is host-specific and handled at runtime
# by the agent following the protocol in SKILL.md.
echo ""
echo "Allowlist patterns to add (host-specific install handled by your CLI):"
for SCRIPT in subgraph_export.py discover_bridges.py accept_bridges.py merge.py unmerge.py reconcile.py hydrate_vault.py; do
    echo "  Bash(uv run python3 $SCRIPT_DIR_PHYSICAL/$SCRIPT:*)"
    if [ "$SCRIPT_DIR_LOGICAL" != "$SCRIPT_DIR_PHYSICAL" ]; then
        echo "  Bash(uv run python3 $SCRIPT_DIR_LOGICAL/$SCRIPT:*)"
    fi
done
echo "  Bash(bash $SCRIPT_DIR_PHYSICAL/merge_evolve_guard.sh:*)"
if [ "$SCRIPT_DIR_LOGICAL" != "$SCRIPT_DIR_PHYSICAL" ]; then
    echo "  Bash(bash $SCRIPT_DIR_LOGICAL/merge_evolve_guard.sh:*)"
fi

echo ""
echo "=== curiosity-merge ready ==="
echo ""
echo "Next steps:"
echo "  source $ENV_FILE                       # load env into current shell"
echo "  uv run python3 $SCRIPT_DIR_PHYSICAL/subgraph_export.py --help"
echo ""

# Optional companion: alphaxiv. hydrate_vault.py prefers alphaxiv's
# pre-extracted markdown for arXiv papers when re-acquiring sources
# after a merge. Default off — installing extra skills is a deliberate
# choice. Detect first; only offer when not already installed and only
# in interactive sessions. Setup proceeds either way; this is purely
# additive.
_alphaxiv_installed=0
for d in "$HOME/.claude/skills/alphaxiv" "$HOME/.agents/skills/alphaxiv"; do
    if [ -d "$d" ]; then
        _alphaxiv_installed=1
        break
    fi
done

if [ "$_alphaxiv_installed" -eq 0 ] && _is_interactive; then
    echo "Optional: the alphaxiv skill produces clean, pre-extracted"
    echo "markdown for arXiv papers. hydrate_vault.py will use it"
    echo "automatically if installed (otherwise it falls back to PDF"
    echo "download + pypdf, which is noisier)."
    printf "Shall I install alphaxiv for you now? [y/N] "
    read -r reply_alphaxiv || reply_alphaxiv="n"
    case "$reply_alphaxiv" in
        y|Y|yes|YES)
            if command -v npx >/dev/null 2>&1; then
                echo "  Installing benjsmith/alphaxiv via npx skills ..."
                npx skills add -g -y benjsmith/alphaxiv \
                    || echo "  (install failed — re-run later: npx skills add -g -y benjsmith/alphaxiv)"
            else
                echo "  npx not found. Install later: npx skills add -g -y benjsmith/alphaxiv"
            fi
            ;;
        *)
            echo "  Skipping alphaxiv. Install anytime: npx skills add -g -y benjsmith/alphaxiv"
            ;;
    esac
    echo ""
fi

# Optional companion: Microsoft Presidio. Adds NER + ML-based PII
# detection on top of the regex baseline. Catches named-entity PII
# (PERSON names, addresses, structured IDs) that regex can't see.
# Default off — Presidio + spaCy model is ~500MB on disk and the model
# download requires a network connection. setup.sh proceeds either way.
#
# Self-leak note for the curious: Presidio's default analyzer uses
# spaCy NER + offline custom recognizers. All analysis runs locally;
# no content leaves the machine. Documented in docs/licensing.md.
_presidio_marker="$WORKSPACE_DIR/.curator/.presidio-prompted"
if [ ! -f "$_presidio_marker" ] && _is_interactive; then
    if uv run python -c "import presidio_analyzer" >/dev/null 2>&1; then
        # already installed
        :
    else
        echo "Optional: Microsoft Presidio adds NER + ML-based PII detection"
        echo "  to subgraph-export and merge preflight. Catches named-entity"
        echo "  PII (PERSON, LOCATION, addresses, structured IDs) that the"
        echo "  regex baseline can't see. Runs entirely locally."
        echo ""
        echo "  Cost: ~500MB disk (Presidio + spaCy en_core_web_lg model)."
        echo "  Network needed once for model download."
        printf "Install Presidio now? [y/N] "
        read -r reply_presidio || reply_presidio="n"
        case "$reply_presidio" in
            y|Y|yes|YES)
                echo "  Installing presidio-analyzer ..."
                if uv pip install presidio-analyzer >/dev/null 2>&1; then
                    echo "  Downloading spaCy en_core_web_lg model ..."
                    if uv run python -m spacy download en_core_web_lg >/dev/null 2>&1; then
                        echo "  Done. Use --enable-presidio on subgraph_export.py / merge.py."
                    else
                        echo "  Model download failed. Run later:"
                        echo "    uv run python -m spacy download en_core_web_lg"
                    fi
                else
                    echo "  Install failed. Run later:"
                    echo "    uv pip install presidio-analyzer"
                    echo "    uv run python -m spacy download en_core_web_lg"
                fi
                echo ""
                echo "  Non-English wikis: add more languages on demand."
                echo "  Default is English only (~500MB). Each additional"
                echo "  language is its own spaCy model with its own disk"
                echo "  cost — install ONLY what you need:"
                echo "    French   : uv run python -m spacy download fr_core_news_lg"
                echo "    German   : uv run python -m spacy download de_core_news_lg"
                echo "    Spanish  : uv run python -m spacy download es_core_news_lg"
                echo "    Italian  : uv run python -m spacy download it_core_news_lg"
                echo "    Portuguese: uv run python -m spacy download pt_core_news_lg"
                echo "    Dutch    : uv run python -m spacy download nl_core_news_lg"
                echo "    Russian  : uv run python -m spacy download ru_core_news_lg"
                echo "    Chinese  : uv run python -m spacy download zh_core_web_lg"
                echo "    Japanese : uv run python -m spacy download ja_core_news_lg"
                echo "    (full list: https://spacy.io/usage/models)"
                echo ""
                echo "  Then enable per-export:"
                echo "    --enable-presidio --presidio-language en,fr"
                ;;
            *)
                echo "  Skipping Presidio. Install anytime:"
                echo "    uv pip install presidio-analyzer"
                echo "    uv run python -m spacy download en_core_web_lg"
                ;;
        esac
        echo ""
    fi
    # Touch marker so we don't re-prompt on every setup.sh re-run.
    touch "$_presidio_marker" 2>/dev/null || true
fi
