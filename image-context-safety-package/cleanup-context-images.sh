#!/usr/bin/env bash
# cleanup-context-images.sh
# Removes stale screenshot/image files from /tmp to prevent Claude Code
# "image exceeds dimension limit for many-image requests" errors.
#
# Usage:
#   ./tools/cleanup-context-images.sh          # delete files older than 60 min
#   ./tools/cleanup-context-images.sh --all    # delete ALL /tmp image files
#   ./tools/cleanup-context-images.sh --age 30 # delete files older than 30 min
#
# Safe to run from cron, agents, or manually.

set -euo pipefail

AGE_MINUTES=60
DELETE_ALL=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --all)
            DELETE_ALL=true
            shift
            ;;
        --age)
            AGE_MINUTES="$2"
            shift 2
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

DELETED=0

cleanup_pattern() {
    local pattern="$1"
    if [ "$DELETE_ALL" = true ]; then
        for f in /tmp/${pattern}; do
            [ -f "$f" ] || continue
            rm -f "$f"
            DELETED=$((DELETED + 1))
        done
    else
        while IFS= read -r f; do
            [ -f "$f" ] || continue
            rm -f "$f"
            DELETED=$((DELETED + 1))
        done < <(find /tmp -maxdepth 1 -name "$pattern" -mmin +"$AGE_MINUTES" 2>/dev/null)
    fi
}

cleanup_pattern "*.png"
cleanup_pattern "*.jpg"
cleanup_pattern "*.jpeg"
cleanup_pattern "*.webp"
cleanup_pattern "*.gif"
cleanup_pattern "screenshot_*.png"
cleanup_pattern "puresurf_*.png"

if [ "$DELETED" -gt 0 ]; then
    echo "[cleanup-context-images] Deleted $DELETED stale image file(s) from /tmp"
else
    echo "[cleanup-context-images] No stale image files found in /tmp"
fi
