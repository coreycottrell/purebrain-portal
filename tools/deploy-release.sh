#!/bin/bash
# Portal Release Deploy Script
# Builds a tarball from the portal repo and SCP's it to the CC release server.
#
# Usage: bash tools/deploy-release.sh [version]
#        VERSION=2.1.0 bash tools/deploy-release.sh
#
# Version resolution precedence (highest first):
#   1. Positional arg $1                  -> bash tools/deploy-release.sh 2.3.71
#   2. VERSION environment variable       -> VERSION=2.3.71 bash tools/deploy-release.sh
#   3. Auto-detect from portal_config.py  -> bash tools/deploy-release.sh
#   4. (none of the above) -> hard-fail with a clear error.
#
# Alex's rule: "Don't just go up by 1, we run out of editions." Pass an
# explicit version (arg or env var) when you need a specific edition.
#
# Requires SSH access to CC VPS (root@178.156.247.35 (Hetzner))

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PORTAL_DIR="${PORTAL_DIR:-$(dirname "$SCRIPT_DIR")}"
SSH_KEY="${SSH_KEY:-}"
VPS_HOST="${DEPLOY_VPS_HOST:-root@178.156.247.35}"
RELEASES_PATH="${DEPLOY_RELEASES_PATH:-/home/cc-gateway/releases/portal}"

# Version resolution: positional $1 wins, else the VERSION env var, else
# auto-detect from portal_config.py via the dedicated, testable detection
# script. Read $1 into a temp first so it can't be clobbered by the env var,
# and use ${VERSION:-} so an unset env VERSION is safe under `set -u`.
ARG_VERSION="${1:-}"
VERSION="${ARG_VERSION:-${VERSION:-}}"
if [ -z "$VERSION" ]; then
    # NOTE: no `2>/dev/null` here on purpose — if detection fails we want the
    # error visible and we ABORT below. Silently falling back to a git-derived
    # version mislabels the release and makes fleet CIVs skip the update.
    if VERSION=$(bash "${SCRIPT_DIR}/detect-portal-version.sh" "$PORTAL_DIR"); then
        :
    else
        VERSION=""
    fi
fi
if [ -z "$VERSION" ]; then
    echo "ERROR: could not detect PORTAL_VERSION from ${PORTAL_DIR}/portal_config.py" >&2
    echo "       Pass an explicit version instead, e.g.:  bash tools/deploy-release.sh 2.3.4" >&2
    echo "       (Refusing to publish a git-derived/mislabeled version — fleet CIVs would skip it.)" >&2
    exit 1
fi

echo "Building portal release v${VERSION}..."
echo "  Source: ${PORTAL_DIR}"

# Build tarball excluding dev/secret/local files
TARBALL="/tmp/portal-release-${VERSION}.tar.gz"

tar czf "$TARBALL" \
    -C "$PORTAL_DIR" \
    --exclude='.git' \
    --exclude='.env' \
    --exclude='.portal-token' \
    --exclude='.gdrive-tokens.json' \
    --exclude='.module-backup' \
    --exclude='*.db' \
    --exclude='*.db-wal' \
    --exclude='*.db-journal' \
    --exclude='*.db-shm' \
    --exclude='*.sqlite' \
    --exclude='*.pyc' \
    --exclude='*.log' \
    --exclude='*.jsonl' \
    --exclude='__pycache__' \
    --exclude='node_modules' \
    --exclude='memories' \
    --exclude='.claude' \
    --exclude='custom/routes.py' \
    --exclude='custom/startup.py' \
    --exclude='custom/config.json' \
    --exclude='custom/quickfire.json' \
    --exclude='custom/panels/*.html' \
    --exclude='custom/__pycache__' \
    --exclude='logs' \
    --exclude='backups' \
    --exclude='portal_uploads' \
    --exclude='portal_owner.json' \
    --exclude='investor_config.json' \
    --exclude='kanban_tasks.json' \
    --exclude='todo_tasks.json' \
    --exclude='hub_tasks.json' \
    --exclude='boop_config.json' \
    --exclude='scheduled_tasks.json' \
    --exclude='user-settings.json' \
    --exclude='bookmarks.json' \
    --exclude='telegram_config.json' \
    --exclude='reaction-sentiment.jsonl' \
    --exclude='skills/local' \
    --exclude='constitution/audit-log.jsonl' \
    --exclude='constitution/overrides.json' \
    --exclude='constitution/rules.json' \
    --exclude='constitution/redact-terms.json' \
    --exclude='installed-skills.json' \
    --exclude='aether-infrastructure' \
    --exclude='react-portal' \
    --exclude='tg_send.sh' \
    --exclude='cc-gateway-staging' \
    --exclude='migrate_agents_departments.py' \
    --exclude='cloudflare' \
    --exclude='INFRA-NOTES.md' \
    --exclude='RECOVERY.md' \
    .

SIZE=$(du -h "$TARBALL" | cut -f1)
SHA=$(sha256sum "$TARBALL" | cut -d' ' -f1)
FILE_SIZE=$(stat -c%s "$TARBALL")
echo "  Tarball: ${SIZE}, SHA256: ${SHA:0:16}..."

# SCP to VPS — upload as both latest.tar.gz and versioned archive
echo "Uploading to ${VPS_HOST}..."
scp ${SSH_KEY:+-i "$SSH_KEY"} -q "$TARBALL" "${VPS_HOST}:${RELEASES_PATH}/latest.tar.gz"
scp ${SSH_KEY:+-i "$SSH_KEY"} -q "$TARBALL" "${VPS_HOST}:${RELEASES_PATH}/portal-${VERSION}.tar.gz"

UPLOADED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# Write version.json (always points to latest — this is what the update check reads)
ssh ${SSH_KEY:+-i "$SSH_KEY"} "$VPS_HOST" "cat > ${RELEASES_PATH}/version.json << VEOF
{
  \"version\": \"${VERSION}\",
  \"sha256\": \"${SHA}\",
  \"size_bytes\": ${FILE_SIZE},
  \"uploaded_at\": \"${UPLOADED_AT}\",
  \"filename\": \"latest.tar.gz\"
}
VEOF"

# Update versions.json manifest — append this version, keep full history
ssh ${SSH_KEY:+-i "$SSH_KEY"} "$VPS_HOST" "python3 << 'PYEOF'
import json, sys
manifest_path = '${RELEASES_PATH}/versions.json'
try:
    manifest = json.loads(open(manifest_path).read())
except:
    manifest = {'versions': []}
manifest['versions'] = [v for v in manifest['versions'] if v.get('version') != '${VERSION}']
manifest['versions'].insert(0, {
    'version': '${VERSION}',
    'sha256': '${SHA}',
    'size_bytes': ${FILE_SIZE},
    'uploaded_at': '${UPLOADED_AT}',
    'filename': 'portal-${VERSION}.tar.gz'
})
manifest['latest'] = '${VERSION}'
with open(manifest_path, 'w') as f:
    json.dump(manifest, f, indent=2)
print('versions.json updated: %d versions archived' % len(manifest['versions']))
PYEOF"

echo ""
echo "Deployed! v${VERSION} is now live."
echo "  Archived as: portal-${VERSION}.tar.gz"

# Show version history
ssh ${SSH_KEY:+-i "$SSH_KEY"} "$VPS_HOST" "python3 << 'PYEOF'
import json
try:
    m = json.loads(open('${RELEASES_PATH}/versions.json').read())
    vs = m.get('versions', [])
    print('  Version history (%d releases):' % len(vs))
    for v in vs[:5]:
        print('    v%s  %s  %s' % (v['version'], v['uploaded_at'], v['filename']))
    if len(vs) > 5:
        print('    ... and %d older versions' % (len(vs) - 5))
except: pass
PYEOF"

echo ""
echo "CIVs update with:"
echo "  bash update-portal.sh   (if they have the script)"
echo "  — or —"
echo "  curl -sH 'X-Portal-Token: <token>' https://cc.purebrain.ai/api/releases/portal/latest | tar xz -C ~/purebrain_portal/"

# Cleanup
rm -f "$TARBALL"
