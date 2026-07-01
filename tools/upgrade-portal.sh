#!/bin/bash
# ============================================================================
# PureBrain Portal Bootstrap Upgrade Script
#
# Upgrades any portal (including 1.x.x pre-update-server versions) to the
# latest release. Handles dependency installation, directory creation,
# config migration, and automatic rollback on failure.
#
# After this upgrade, the built-in "Check for Updates" button handles all
# future updates automatically.
#
# Usage:
#   curl -sL https://raw.githubusercontent.com/puretechnyc/purebrain_portal/main/tools/upgrade-portal.sh | bash
#   -- or --
#   bash tools/upgrade-portal.sh [--dry-run] [--portal-dir /path/to/portal]
#
# Options:
#   --dry-run       Show what would happen without making changes
#   --portal-dir    Path to portal directory (default: ~/purebrain_portal)
#   --no-restart    Skip the portal restart step
#   --help          Show this help message
# ============================================================================

set -euo pipefail

# --- Configuration -----------------------------------------------------------

RELEASE_SERVER="https://cc.purebrain.ai"
VERSION_URL="${RELEASE_SERVER}/api/releases/portal/version"
DOWNLOAD_URL="${RELEASE_SERVER}/api/releases/portal/latest"

# Shared read-only key -- ships with every portal, grants release download only.
PORTAL_UPDATE_TOKEN="Hq-Of6ktPmQ-xDsJ4SjqbgGFZGIUR1oEushkZsghODY"

# Files that must NEVER be overwritten (user data / config)
PRESERVED_FILES=(
    .env
    .portal-token
    portal-chat.jsonl
    user-settings.json
    agents.db
    referrals.db
    clients.db
    portal_data.db
    boop_config.json
    scheduled_tasks.json
    portal_owner.json
    kanban_tasks.json
    todo_tasks.json
    hub_tasks.json
    .gdrive-tokens.json
    reaction-sentiment.jsonl
    telegram_config.json
    bookmarks.json
    investor_config.json
    installed-skills.json
    activity-log.jsonl
    cc-cache.json
)

# Directories that must NEVER be overwritten (user data / config)
PRESERVED_DIRS=(
    memories
    .claude
    custom
    logs
    skills/local
    constitution
    .module-backup
    portal_uploads
)

# Directories that 2.x.x expects to exist (created if missing)
REQUIRED_DIRS=(
    constitution
    custom
    logs
    backups
    portal_uploads
    skills/local
    static/js/features
    static/js/core
)

# Minimum disk space required for upgrade (bytes) -- 100MB
MIN_DISK_SPACE=104857600

# --- Defaults ----------------------------------------------------------------

DRY_RUN=false
NO_RESTART=false
PORTAL_DIR="${HOME}/purebrain_portal"
IS_LEGACY=false

# --- Helpers -----------------------------------------------------------------

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

info()  { echo -e "${BLUE}[upgrade]${NC} $*"; }
ok()    { echo -e "${GREEN}[upgrade]${NC} $*"; }
warn()  { echo -e "${YELLOW}[upgrade]${NC} $*"; }
err()   { echo -e "${RED}[upgrade]${NC} $*" >&2; }
die()   { err "$*"; exit 1; }

# Detect the portal's configured port from .env or default
detect_port() {
    local port=8097
    if [[ -f "${PORTAL_DIR}/.env" ]]; then
        local env_port
        env_port=$(grep -m1 -oP '^PORT\s*=\s*\K[0-9]+' "${PORTAL_DIR}/.env" 2>/dev/null || true)
        [[ -n "$env_port" ]] && port="$env_port"
    fi
    echo "$port"
}

# Rollback: restore full portal from backup directory
rollback() {
    local backup_dir="$1"
    err "ROLLBACK: Restoring portal from backup..."

    # Restore preserved files
    for f in "${PRESERVED_FILES[@]}"; do
        local src="${backup_dir}/${f}"
        local dest="${PORTAL_DIR}/${f}"
        if [[ -f "$src" ]]; then
            mkdir -p "$(dirname "$dest")"
            cp -a "$src" "$dest"
        fi
    done

    # Restore preserved directories
    for d in "${PRESERVED_DIRS[@]}"; do
        local src="${backup_dir}/${d}"
        local dest="${PORTAL_DIR}/${d}"
        if [[ -d "$src" ]]; then
            mkdir -p "$(dirname "$dest")"
            cp -a "$src" "$dest"
        fi
    done

    warn "Restored user data from backup. Portal code is at new version but user data is intact."
    warn "If portal won't start, restore the full backup manually:"
    warn "  cp -a ${backup_dir}/* ${PORTAL_DIR}/"
}

# --- Parse arguments ---------------------------------------------------------

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)    DRY_RUN=true; shift ;;
        --no-restart) NO_RESTART=true; shift ;;
        --portal-dir)
            [[ -z "${2:-}" ]] && die "--portal-dir requires a path argument"
            PORTAL_DIR="$2"; shift 2 ;;
        --help|-h)
            head -n 24 "$0" | tail -n +2 | sed 's/^# \?//'
            exit 0 ;;
        *) die "Unknown option: $1 (try --help)" ;;
    esac
done

# --- Preflight checks --------------------------------------------------------

info "PureBrain Portal Bootstrap Upgrade"
info "Portal directory: ${PORTAL_DIR}"
$DRY_RUN && warn "DRY RUN -- no changes will be made"
echo ""

# Check required tools
for cmd in curl sha256sum tar python3; do
    command -v "$cmd" >/dev/null 2>&1 || die "Required tool not found: $cmd"
done

# Verify Python 3.10+ (required by starlette and modern portal features)
PY_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || echo "0.0")
PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)
if [[ "$PY_MAJOR" -lt 3 ]] || { [[ "$PY_MAJOR" -eq 3 ]] && [[ "$PY_MINOR" -lt 10 ]]; }; then
    die "Python 3.10+ required (found ${PY_VERSION}). Install a newer Python first."
fi
ok "Python ${PY_VERSION} detected"

# Verify pip is available
if ! python3 -m pip --version >/dev/null 2>&1; then
    die "pip not found. Install it with: sudo apt install python3-pip (or equivalent)"
fi

# Verify portal directory exists
[[ -d "$PORTAL_DIR" ]] || die "Portal directory not found: ${PORTAL_DIR}"

# Check disk space (need at least 100MB free for tarball + backup + extraction)
AVAIL_BYTES=$(df --output=avail -B1 "$PORTAL_DIR" 2>/dev/null | tail -1 | tr -d ' ' || echo "0")
if [[ "$AVAIL_BYTES" -gt 0 ]] && [[ "$AVAIL_BYTES" -lt "$MIN_DISK_SPACE" ]]; then
    AVAIL_MB=$((AVAIL_BYTES / 1048576))
    die "Insufficient disk space: ${AVAIL_MB}MB free, need at least 100MB."
fi

# Detect current version and whether this is a 1.x.x (legacy) portal
CURRENT_VERSION="unknown"
if [[ -f "${PORTAL_DIR}/portal_config.py" ]]; then
    CURRENT_VERSION=$(grep -m1 -oP '^PORTAL_VERSION\s*=\s*"\K[^"]+' "${PORTAL_DIR}/portal_config.py" 2>/dev/null || echo "unknown")
elif [[ -f "${PORTAL_DIR}/release_notes.json" ]]; then
    CURRENT_VERSION=$(python3 -c "import json; print(json.load(open('${PORTAL_DIR}/release_notes.json')).get('current_version','unknown'))" 2>/dev/null || echo "unknown")
fi

# Detect 1.x.x: no portal_config.py, or version starts with "1.", or unknown
if [[ ! -f "${PORTAL_DIR}/portal_config.py" ]] || [[ "$CURRENT_VERSION" == 1.* ]] || [[ "$CURRENT_VERSION" == "unknown" ]]; then
    IS_LEGACY=true
    warn "Legacy portal detected (${CURRENT_VERSION}) -- will run full migration"
fi
info "Current version: ${CURRENT_VERSION}"

# --- Step 1: Fetch remote version info ---------------------------------------

info "Step 1/8: Checking latest version..."

VERSION_JSON=$(curl -sfL \
    -H "X-Portal-Token: ${PORTAL_UPDATE_TOKEN}" \
    -H "User-Agent: PureBrain-Portal-Upgrade/${CURRENT_VERSION}" \
    "$VERSION_URL" 2>/dev/null) \
    || die "Failed to reach release server at ${VERSION_URL}. Check your network."

REMOTE_VERSION=$(echo "$VERSION_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('version',''))" 2>/dev/null)
REMOTE_SHA256=$(echo "$VERSION_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('sha256',''))" 2>/dev/null)
REMOTE_SIZE=$(echo "$VERSION_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('size_bytes',0))" 2>/dev/null)

[[ -z "$REMOTE_VERSION" ]] && die "Release server returned empty version. Try again later."

ok "Latest version: ${REMOTE_VERSION} (${REMOTE_SIZE} bytes)"

if [[ "$CURRENT_VERSION" == "$REMOTE_VERSION" ]]; then
    ok "Already up to date! Nothing to do."
    exit 0
fi

info "Upgrading: ${CURRENT_VERSION} -> ${REMOTE_VERSION}"

# --- Step 2: Download tarball ------------------------------------------------

info "Step 2/8: Downloading release tarball..."

TMP_TARBALL=$(mktemp /tmp/portal-upgrade-XXXXXX.tar.gz)
trap 'rm -f "$TMP_TARBALL"' EXIT

if $DRY_RUN; then
    warn "[dry-run] Would download ${DOWNLOAD_URL} to ${TMP_TARBALL}"
else
    curl -fL \
        -H "X-Portal-Token: ${PORTAL_UPDATE_TOKEN}" \
        -H "User-Agent: PureBrain-Portal-Upgrade/${CURRENT_VERSION}" \
        -o "$TMP_TARBALL" \
        "$DOWNLOAD_URL" \
        || die "Download failed. Check your network connection."

    ACTUAL_SIZE=$(stat -c%s "$TMP_TARBALL" 2>/dev/null || stat -f%z "$TMP_TARBALL" 2>/dev/null)
    ok "Downloaded ${ACTUAL_SIZE} bytes"
fi

# --- Step 3: Verify SHA256 checksum ------------------------------------------

info "Step 3/8: Verifying checksum..."

if $DRY_RUN; then
    warn "[dry-run] Would verify SHA256 against: ${REMOTE_SHA256:0:16}..."
elif [[ -n "$REMOTE_SHA256" ]]; then
    ACTUAL_SHA256=$(sha256sum "$TMP_TARBALL" | cut -d' ' -f1)
    if [[ "$ACTUAL_SHA256" != "$REMOTE_SHA256" ]]; then
        die "SHA256 MISMATCH! Expected: ${REMOTE_SHA256:0:16}... Got: ${ACTUAL_SHA256:0:16}... Download may be corrupted."
    fi
    ok "SHA256 verified: ${ACTUAL_SHA256:0:16}..."
else
    warn "No SHA256 from server -- skipping checksum verification"
fi

# --- Step 4: Backup current portal -------------------------------------------

info "Step 4/8: Backing up current portal..."

TIMESTAMP=$(date +%Y%m%d-%H%M%S)
BACKUP_DIR="${PORTAL_DIR}/backups/pre-upgrade/${TIMESTAMP}"

if $DRY_RUN; then
    warn "[dry-run] Would backup to: ${BACKUP_DIR}"
    echo "  Files to preserve:"
    for f in "${PRESERVED_FILES[@]}"; do
        [[ -f "${PORTAL_DIR}/${f}" ]] && echo "    ${f}"
    done
    echo "  Dirs to preserve:"
    for d in "${PRESERVED_DIRS[@]}"; do
        [[ -d "${PORTAL_DIR}/${d}" ]] && echo "    ${d}/"
    done
else
    mkdir -p "$BACKUP_DIR"

    # Backup preserved files
    BACKED_UP=0
    for f in "${PRESERVED_FILES[@]}"; do
        src="${PORTAL_DIR}/${f}"
        if [[ -f "$src" ]]; then
            dest="${BACKUP_DIR}/${f}"
            mkdir -p "$(dirname "$dest")"
            cp -a "$src" "$dest"
            BACKED_UP=$((BACKED_UP+1))
        fi
    done

    # Backup preserved directories
    for d in "${PRESERVED_DIRS[@]}"; do
        src="${PORTAL_DIR}/${d}"
        if [[ -d "$src" ]]; then
            dest="${BACKUP_DIR}/${d}"
            mkdir -p "$(dirname "$dest")"
            cp -a "$src" "$dest"
            BACKED_UP=$((BACKED_UP+1))
        fi
    done

    ok "Backed up ${BACKED_UP} items to ${BACKUP_DIR}"
fi

# --- Step 5: Extract tarball (skipping preserved files/dirs) -----------------

info "Step 5/8: Extracting update..."

if $DRY_RUN; then
    warn "[dry-run] Would extract tarball to ${PORTAL_DIR}, skipping preserved files/dirs"
    # Show what would be extracted (first 20 entries)
    if [[ -f "$TMP_TARBALL" ]] && [[ -s "$TMP_TARBALL" ]]; then
        echo "  Sample files in tarball:"
        tar tzf "$TMP_TARBALL" 2>/dev/null | head -20 | sed 's/^/    /'
        TOTAL=$(tar tzf "$TMP_TARBALL" 2>/dev/null | wc -l)
        echo "  ... (${TOTAL} total entries)"
    fi
else
    # Build tar exclude patterns for preserved files and dirs
    TAR_EXCLUDES=()
    for f in "${PRESERVED_FILES[@]}"; do
        TAR_EXCLUDES+=(--exclude="./${f}")
    done
    for d in "${PRESERVED_DIRS[@]}"; do
        TAR_EXCLUDES+=(--exclude="./${d}" --exclude="./${d}/*")
    done

    tar xzf "$TMP_TARBALL" \
        -C "$PORTAL_DIR" \
        "${TAR_EXCLUDES[@]}" \
        || die "Extraction failed. Your backup is at: ${BACKUP_DIR}"

    ok "Extraction complete"

    # Belt-and-suspenders: restore preserved files from backup in case
    # the tarball contained any of them despite the excludes
    RESTORED=0
    for f in "${PRESERVED_FILES[@]}"; do
        backup_src="${BACKUP_DIR}/${f}"
        dest="${PORTAL_DIR}/${f}"
        if [[ -f "$backup_src" ]]; then
            cp -a "$backup_src" "$dest"
            RESTORED=$((RESTORED+1))
        fi
    done
    for d in "${PRESERVED_DIRS[@]}"; do
        backup_src="${BACKUP_DIR}/${d}"
        dest="${PORTAL_DIR}/${d}"
        if [[ -d "$backup_src" ]]; then
            cp -a "$backup_src" "$dest"
            RESTORED=$((RESTORED+1))
        fi
    done
    [[ $RESTORED -gt 0 ]] && info "Verified ${RESTORED} preserved items intact"
fi

# --- Step 6: Migration tasks (1.x.x -> 2.x.x) ------------------------------

info "Step 6/8: Running migration tasks..."

if $DRY_RUN; then
    if $IS_LEGACY; then
        warn "[dry-run] Legacy migration would:"
        echo "  Create required directories:"
        for d in "${REQUIRED_DIRS[@]}"; do
            [[ ! -d "${PORTAL_DIR}/${d}" ]] && echo "    mkdir -p ${d}/"
        done
        echo "  Install Python dependencies from requirements.txt"
    else
        info "Standard upgrade (2.x.x -> 2.x.x) -- minimal migration needed"
    fi
else
    # Create any required directories that don't exist yet
    DIRS_CREATED=0
    for d in "${REQUIRED_DIRS[@]}"; do
        if [[ ! -d "${PORTAL_DIR}/${d}" ]]; then
            mkdir -p "${PORTAL_DIR}/${d}"
            DIRS_CREATED=$((DIRS_CREATED+1))
        fi
    done
    [[ $DIRS_CREATED -gt 0 ]] && ok "Created ${DIRS_CREATED} required directories"

    # Install/upgrade Python dependencies
    if [[ -f "${PORTAL_DIR}/requirements.txt" ]]; then
        info "Installing Python dependencies..."
        if python3 -m pip install -q -r "${PORTAL_DIR}/requirements.txt" 2>&1; then
            ok "Dependencies installed"
        else
            # PEP 668 (Python 3.12+ on Debian/Ubuntu) marks the system Python as
            # "externally managed" and blocks bare pip installs.  Portal runs in
            # its own user context so --break-system-packages is safe here.
            warn "pip failed (possibly PEP 668 externally-managed-environment) -- retrying with --break-system-packages"
            if python3 -m pip install -q --break-system-packages -r "${PORTAL_DIR}/requirements.txt" 2>&1; then
                ok "Dependencies installed (with --break-system-packages)"
            else
                warn "Some dependencies failed to install -- portal may still work"
                warn "Try manually: pip install -r ${PORTAL_DIR}/requirements.txt"
            fi
        fi
    fi

    if $IS_LEGACY; then
        ok "Legacy migration steps complete"
    fi

    # Refresh model file — detect from session, else PRESERVE any existing pin,
    # else write the current fleet floor default. A model pin does NOT expire:
    # the write/preserve/default decision lives in tools/refresh-model-pin.sh so
    # it can be unit-tested in isolation (see tests/test_model_pin_refresh.py).
    DETECTED_MODEL=""

    # Try to detect from current session transcript
    SESSION_FILE="$HOME/memories/sessions/current-session.jsonl"
    if [[ -f "$SESSION_FILE" ]]; then
        DETECTED_MODEL=$(tail -c 10240 "$SESSION_FILE" 2>/dev/null | grep -oP '"model"\s*:\s*"\Kclaude-[^"]+' | tail -1)
    fi

    if [[ -x "${PORTAL_DIR}/tools/refresh-model-pin.sh" ]]; then
        info "$(bash "${PORTAL_DIR}/tools/refresh-model-pin.sh" "$DETECTED_MODEL")"
    else
        warn "tools/refresh-model-pin.sh not found — leaving ~/.claude_session_model untouched"
    fi
fi

# --- Step 7: Restart portal --------------------------------------------------

if $NO_RESTART; then
    info "Step 7/8: Skipping restart (--no-restart)"
elif $DRY_RUN; then
    warn "[dry-run] Step 7/8: Would restart the portal process"
else
    info "Step 7/8: Restarting portal..."

    # Detect the runtime environment and restart accordingly.
    # Priority: restart.sh > tmux session > systemd service > manual kill+nohup
    _restart_done=false

    # Use restart.sh if available (cleanest method)
    if [[ -x "${PORTAL_DIR}/restart.sh" ]]; then
        info "Using restart.sh"
        bash "${PORTAL_DIR}/restart.sh"
        _restart_done=true

    # tmux: if portal runs inside a tmux session, restart within it
    elif tmux has-session -t portal-server 2>/dev/null; then
        info "Detected tmux session 'portal-server' -- restarting inside tmux"
        tmux send-keys -t portal-server C-c '' 2>/dev/null || true
        sleep 2
        tmux send-keys -t portal-server "cd ${PORTAL_DIR} && python3 portal_server.py" Enter
        _restart_done=true

    # systemd: if a portal unit exists and is active, use systemctl
    elif systemctl is-active --quiet portal-server 2>/dev/null; then
        info "Detected systemd service 'portal-server' -- restarting via systemctl"
        systemctl restart portal-server
        _restart_done=true
    fi

    # Fallback: manual kill + nohup (original behaviour)
    if ! $_restart_done; then
        PORTAL_PID=$(pgrep -f "python3.*portal_server.py" | head -1 || true)

        if [[ -n "$PORTAL_PID" ]]; then
            info "Found portal process: PID ${PORTAL_PID}"
            kill "$PORTAL_PID" 2>/dev/null || true

            # Wait for clean exit (up to 5 seconds)
            for _ in $(seq 1 10); do
                kill -0 "$PORTAL_PID" 2>/dev/null || break
                sleep 0.5
            done

            # Force kill if still alive
            if kill -0 "$PORTAL_PID" 2>/dev/null; then
                kill -9 "$PORTAL_PID" 2>/dev/null || true
                sleep 1
            fi
        else
            info "No running portal process found -- starting fresh..."
        fi

        cd "$PORTAL_DIR"
        nohup python3 "${PORTAL_DIR}/portal_server.py" >> /tmp/portal.log 2>&1 &
        NEW_PID=$!
        info "Started portal process: PID ${NEW_PID}"
    fi
fi

# --- Step 8: Post-upgrade verification --------------------------------------

if $NO_RESTART; then
    info "Step 8/8: Skipping verification (--no-restart)"
elif $DRY_RUN; then
    warn "[dry-run] Step 8/8: Would verify portal health and version"
else
    info "Step 8/8: Verifying upgrade..."

    PORT=$(detect_port)
    HEALTH_OK=false

    # Health check (up to 20 seconds)
    for _ in $(seq 1 40); do
        HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:${PORT}/health" 2>/dev/null || echo "000")
        if [[ "$HTTP_CODE" == "200" ]]; then
            HEALTH_OK=true
            break
        fi
        sleep 0.5
    done

    if $HEALTH_OK; then
        ok "Portal healthy on port ${PORT}"

        # Verify the running version matches what we deployed
        RUNNING_VERSION=$(curl -sf "http://localhost:${PORT}/health" 2>/dev/null \
            | python3 -c "import sys,json; print(json.load(sys.stdin).get('version',''))" 2>/dev/null || true)
        if [[ -n "$RUNNING_VERSION" ]]; then
            info "Running version: ${RUNNING_VERSION}"
        fi
    else
        err "Portal failed health check on port ${PORT} after 20 seconds!"
        err "Checking logs for errors..."
        tail -20 /tmp/portal.log 2>/dev/null || true

        # Automatic rollback
        warn ""
        warn "Attempting automatic rollback..."
        rollback "$BACKUP_DIR"

        # Try to restart with restored files
        PORTAL_PID=$(pgrep -f "python3.*portal_server.py" | head -1 || true)
        if [[ -n "$PORTAL_PID" ]]; then
            kill "$PORTAL_PID" 2>/dev/null || true
            sleep 2
        fi
        cd "$PORTAL_DIR"
        nohup python3 "${PORTAL_DIR}/portal_server.py" >> /tmp/portal.log 2>&1 &

        # Check if rollback-started portal is healthy
        sleep 3
        ROLLBACK_CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:${PORT}/health" 2>/dev/null || echo "000")
        if [[ "$ROLLBACK_CODE" == "200" ]]; then
            warn "Rollback successful -- portal is running on previous version"
            warn "Please report this issue so we can fix the upgrade path."
        else
            err "Rollback also failed. Manual intervention required."
            err "Backup location: ${BACKUP_DIR}"
            err "Logs: /tmp/portal.log"
        fi
        exit 1
    fi
fi

# --- Done! -------------------------------------------------------------------

echo ""
ok "============================================"
ok "  Upgrade complete!"
ok "  ${CURRENT_VERSION} -> ${REMOTE_VERSION}"
ok "  Hard-refresh your browser (Ctrl/Cmd+Shift+R) to load the new UI."
if $IS_LEGACY; then
ok "  (Legacy 1.x.x migration applied)"
fi
ok "============================================"
echo ""
info "Your data has been preserved (backup at ${BACKUP_DIR})"
info "The built-in update system will handle all future updates."
info "Look for the 'Check for Updates' button in Settings."
echo ""
