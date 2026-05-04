#!/usr/bin/env bash
# merge_evolve_guard.sh — hash-guard for curiosity-merge scripts.
#
# Deliberately named distinctly from curiosity-engine's evolve_guard.sh.
# Both skills install side-by-side (same workspace and/or globally under
# ~/.claude/skills/), so a generic name would invite an agent to call
# the wrong one. The filename + the banner below disambiguate.
#
# Same shape as curiosity-engine/scripts/evolve_guard.sh but guards a
# different file list (this skill's scripts). Records sha256 at wave start;
# compares at wave end. Drift aborts.
#
# Usage:
#   merge_evolve_guard.sh hash                    # print fingerprint
#   merge_evolve_guard.sh snapshot <outfile>      # write fingerprint
#   merge_evolve_guard.sh check <snapshotfile>    # compare; exit 0/1

set -e

# Self-identification banner (stderr, so it doesn't pollute the
# fingerprint stdout). Makes log review unambiguous when both guards
# are run in the same wave.
echo "[curiosity-merge guard]" >&2

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
GUARDED=(
    "$SCRIPT_DIR/merge_evolve_guard.sh"
    "$SCRIPT_DIR/setup.sh"
    "$SCRIPT_DIR/subgraph_export.py"
    "$SCRIPT_DIR/discover_bridges.py"
    "$SCRIPT_DIR/accept_bridges.py"
    "$SCRIPT_DIR/merge.py"
    "$SCRIPT_DIR/unmerge.py"
    "$SCRIPT_DIR/reconcile.py"
    "$SCRIPT_DIR/hydrate_vault.py"
    "$SCRIPT_DIR/preflight.py"
)

sha256_cmd() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | awk '{print $1}'
    else
        shasum -a 256 "$1" | awk '{print $1}'
    fi
}

fingerprint() {
    for f in "${GUARDED[@]}"; do
        if [ ! -f "$f" ]; then
            echo "MISSING:$(basename "$f")"
        else
            printf '%s:%s\n' "$(sha256_cmd "$f")" "$(basename "$f")"
        fi
    done
}

case "${1:-}" in
    hash)
        fingerprint
        ;;
    snapshot)
        if [ -z "${2:-}" ]; then
            echo "usage: merge_evolve_guard.sh snapshot <outfile>" >&2
            exit 2
        fi
        fingerprint > "$2"
        echo "wrote $2"
        ;;
    check)
        if [ -z "${2:-}" ] || [ ! -f "$2" ]; then
            echo "usage: merge_evolve_guard.sh check <snapshotfile>" >&2
            exit 2
        fi
        expected="$(cat "$2")"
        actual="$(fingerprint)"
        if [ "$expected" = "$actual" ]; then
            echo "ok"
            exit 0
        fi
        echo "DRIFT"
        echo "--- expected ---"
        echo "$expected"
        echo "--- actual ---"
        echo "$actual"
        exit 1
        ;;
    *)
        echo "usage: merge_evolve_guard.sh {hash|snapshot <file>|check <file>}" >&2
        exit 2
        ;;
esac
