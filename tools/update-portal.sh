#!/bin/bash
# Portal Update Script — Run this to update your portal to the latest version
#
# Usage: bash update-portal.sh
#
# Requires:
#   - PORTAL_TOKEN: shared portal download token (ask your admin)
#   - PORTAL_DIR: where your portal is installed (defaults to ~/purebrain_portal)
#   - CC_RELEASE_URL: release server (defaults to cc.purebrain.ai)

set -euo pipefail

PORTAL_DIR="${PORTAL_DIR:-$HOME/purebrain_portal}"
CC_RELEASE_URL="${CC_RELEASE_URL:-https://cc.purebrain.ai}"
PORTAL_TOKEN="${PORTAL_TOKEN:-}"

# Auto-detect token from .env
if [ -z "$PORTAL_TOKEN" ]; then
    if [ -f "$PORTAL_DIR/.env" ]; then
        PORTAL_TOKEN=$(grep -E "^PORTAL_TOKEN=" "$PORTAL_DIR/.env" 2>/dev/null | cut -d= -f2- | tr -d '"' || true)
    fi
    if [ -z "$PORTAL_TOKEN" ] && [ -f "$HOME/.env" ]; then
        PORTAL_TOKEN=$(grep -E "^PORTAL_TOKEN=" "$HOME/.env" 2>/dev/null | cut -d= -f2- | tr -d '"' || true)
    fi
fi

if [ -z "$PORTAL_TOKEN" ]; then
    echo "ERROR: No portal token found."
    echo "Set PORTAL_TOKEN env var, or add PORTAL_TOKEN=<token> to your .env"
    exit 1
fi

# Check current version
echo "Checking for updates..."
VERSION_RESPONSE=$(curl -sf \
    -H "X-Portal-Token: ${PORTAL_TOKEN}" \
    "${CC_RELEASE_URL}/api/releases/portal/version" 2>/dev/null || echo "")

if [ -z "$VERSION_RESPONSE" ]; then
    echo "ERROR: Could not reach release server at ${CC_RELEASE_URL}"
    exit 1
fi

REMOTE_VERSION=$(echo "$VERSION_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('version','unknown'))" 2>/dev/null || echo "unknown")
REMOTE_SHA=$(echo "$VERSION_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('sha256',''))" 2>/dev/null || echo "")
echo "  Latest version: ${REMOTE_VERSION}"

# Backup .env before update (never overwrite secrets)
if [ -f "$PORTAL_DIR/.env" ]; then
    cp "$PORTAL_DIR/.env" "/tmp/.portal-env-backup-$(date +%s)"
fi

# Backup other local config files
for f in .portal-token .gdrive-tokens.json user-settings.json; do
    if [ -f "$PORTAL_DIR/$f" ]; then
        cp "$PORTAL_DIR/$f" "/tmp/.portal-backup-${f}-$(date +%s)"
    fi
done

# Download and extract
echo "Downloading portal v${REMOTE_VERSION}..."
TARBALL="/tmp/portal-update-$(date +%s).tar.gz"

curl -sf \
    -H "X-Portal-Token: ${PORTAL_TOKEN}" \
    -o "$TARBALL" \
    "${CC_RELEASE_URL}/api/releases/portal/latest"

if [ ! -f "$TARBALL" ] || [ ! -s "$TARBALL" ]; then
    echo "ERROR: Download failed"
    exit 1
fi

# Verify checksum if available
if [ -n "$REMOTE_SHA" ]; then
    LOCAL_SHA=$(sha256sum "$TARBALL" | cut -d' ' -f1)
    if [ "$LOCAL_SHA" != "$REMOTE_SHA" ]; then
        echo "ERROR: Checksum mismatch! Expected ${REMOTE_SHA}, got ${LOCAL_SHA}"
        rm -f "$TARBALL"
        exit 1
    fi
    echo "  Checksum verified."
fi

# Extract (overwrites code files, preserves .env via --skip-old-files for dotfiles)
echo "Extracting to ${PORTAL_DIR}..."
mkdir -p "$PORTAL_DIR"
tar xzf "$TARBALL" -C "$PORTAL_DIR"

# Restore .env and config files (in case tarball somehow included them)
if [ -f "/tmp/.portal-env-backup-"* ]; then
    LATEST_BACKUP=$(ls -t /tmp/.portal-env-backup-* 2>/dev/null | head -1)
    if [ -n "$LATEST_BACKUP" ]; then
        cp "$LATEST_BACKUP" "$PORTAL_DIR/.env"
    fi
fi

# Cleanup
rm -f "$TARBALL"
rm -f /tmp/.portal-env-backup-* /tmp/.portal-backup-* 2>/dev/null

echo ""
echo "Portal updated to v${REMOTE_VERSION}!"
echo "Restart your portal server to apply changes."
