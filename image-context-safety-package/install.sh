#!/usr/bin/env bash
# install.sh - One-command installer for Image Context Safety package
# Protects Claude Code sessions from context overflow crashes caused by image reads.
#
# Usage: bash install.sh [--civ-root /path/to/project]
#
# If --civ-root is not provided, uses the current working directory.

set -euo pipefail

CIV_ROOT="$(pwd)"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --civ-root) CIV_ROOT="$2"; shift 2 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== Image Context Safety Installer ==="
echo "Installing to: $CIV_ROOT"
echo ""

# --- Step 1: Copy scripts ---
mkdir -p "$CIV_ROOT/tools"

cp "$SCRIPT_DIR/warn-image-read.sh" "$CIV_ROOT/tools/warn-image-read.sh"
chmod +x "$CIV_ROOT/tools/warn-image-read.sh"
echo "[1/5] Copied warn-image-read.sh to tools/"

cp "$SCRIPT_DIR/cleanup-context-images.sh" "$CIV_ROOT/tools/cleanup-context-images.sh"
chmod +x "$CIV_ROOT/tools/cleanup-context-images.sh"
echo "[2/5] Copied cleanup-context-images.sh to tools/"

# --- Step 2: Merge settings hook ---
# Check for settings.local.json first (takes priority), then settings.json
if [ -f "$CIV_ROOT/.claude/settings.local.json" ]; then
    SETTINGS_FILE="$CIV_ROOT/.claude/settings.local.json"
elif [ -f "$CIV_ROOT/.claude/settings.json" ]; then
    SETTINGS_FILE="$CIV_ROOT/.claude/settings.json"
else
    SETTINGS_FILE="$CIV_ROOT/.claude/settings.json"
fi

mkdir -p "$CIV_ROOT/.claude"

if [ -f "$SETTINGS_FILE" ]; then
    if grep -q "warn-image-read" "$SETTINGS_FILE" 2>/dev/null; then
        echo "[3/5] Hook already present in $(basename $SETTINGS_FILE) (skipped)"
    else
        python3 -c "
import json

with open('$SETTINGS_FILE', 'r') as f:
    settings = json.load(f)

hook_entry = {
    'matcher': 'Read',
    'hooks': [{
        'type': 'command',
        'command': 'bash $CIV_ROOT/tools/warn-image-read.sh'
    }]
}

settings.setdefault('hooks', {})
settings['hooks'].setdefault('PreToolUse', [])
settings['hooks']['PreToolUse'].append(hook_entry)

with open('$SETTINGS_FILE', 'w') as f:
    json.dump(settings, f, indent=2)
" 2>/dev/null && echo "[3/5] Merged Read hook into $(basename $SETTINGS_FILE)" || echo "[3/5] WARNING: Could not auto-merge settings. Add manually from settings-snippet.json"
    fi
else
    cat > "$SETTINGS_FILE" <<SETTINGS_EOF
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Read",
        "hooks": [
          {
            "type": "command",
            "command": "bash $CIV_ROOT/tools/warn-image-read.sh"
          }
        ]
      }
    ]
  }
}
SETTINGS_EOF
    echo "[3/5] Created settings.json with Read hook"
fi

# --- Step 3: Add cron job ---
CRON_CMD="*/30 * * * * $CIV_ROOT/tools/cleanup-context-images.sh >> /tmp/cleanup-context-images.log 2>&1"
EXISTING_CRON=$(crontab -l 2>/dev/null || true)

if echo "$EXISTING_CRON" | grep -q "cleanup-context-images" 2>/dev/null; then
    echo "[4/5] Cron job already exists (skipped)"
else
    (echo "$EXISTING_CRON"; echo "$CRON_CMD") | crontab -
    echo "[4/5] Added cron job (runs every 30 minutes)"
fi

# --- Step 4: Append CLAUDE.md section ---
CLAUDE_MD="$CIV_ROOT/CLAUDE.md"

if [ -f "$CLAUDE_MD" ]; then
    if grep -q "Image Context Safety" "$CLAUDE_MD" 2>/dev/null; then
        echo "[5/5] CLAUDE.md already has Image Context Safety section (skipped)"
    else
        echo "" >> "$CLAUDE_MD"
        cat "$SCRIPT_DIR/claude-md-snippet.md" >> "$CLAUDE_MD"
        echo "[5/5] Appended Image Context Safety section to CLAUDE.md"
    fi
else
    echo "[5/5] No CLAUDE.md found -- please manually add the section from claude-md-snippet.md"
fi

echo ""
echo "=== Installation Complete ==="
echo ""
echo "What was installed:"
echo "  - tools/warn-image-read.sh           (PreToolUse hook - blocks image reads)"
echo "  - tools/cleanup-context-images.sh    (Cron cleanup - removes stale /tmp images)"
echo "  - .claude/settings.json              (Hook registration)"
echo "  - Cron job every 30 min              (Automatic /tmp cleanup)"
echo "  - CLAUDE.md section                  (Agent behavioral rules)"
echo ""
echo "To verify: try reading any .png file with Claude Code -- it should be blocked."
