# Phase 6 Orchestrator Update — Pull from PB2 Repo
**Status**: PLAN (not yet implemented)
**Target file**: `/home/aiciv/civ/tools/birth_orchestrator_v4.sh` — Phase 6 (setup_portal function)
**Prerequisite**: Tasks #1 and #5 complete ✅

---

## Current Behavior (as of 2026-03-06)

Phase 6 copies portal files FROM the `witness` container:
```bash
docker cp witness:/home/aiciv/purebrain_portal/portal_server.py /tmp/ps_${CONTAINER}.py
docker cp witness:/home/aiciv/purebrain_portal/portal-pb-stripped.html /tmp/ph_${CONTAINER}.html
# Then patches CIV_NAME via sed:
sed -i 's/CIV_NAME = "witness"/CIV_NAME = "${CIV_NAME_LOWER}"/' /tmp/ps_${CONTAINER}.py
```

**Problems with this approach:**
1. `witness` container must be running and have the latest portal files
2. Copying from `witness` means deployed portals lag behind any improvements
3. `sed` CIV_NAME patch is fragile — breaks if the line format changes
4. No `portal_owner.json` provisioning (referral/payout system can't work without it)
5. Uses `portal-pb-stripped.html` (fleet-panel removed) instead of the clean PB2 HTML

---

## Proposed New Behavior

Pull portal files directly from the **PB2 repo** (`aiciv-comms-hub/packages/purebrain-portal/`).

The PB2 `portal_server.py` auto-detects `CIV_NAME` from `~/.aiciv-identity.json` —
no `sed` patching needed.

### Source → Destination Mapping

| Source (PB2 repo on Hetzner host) | Destination (in customer container) |
|---|---|
| `packages/purebrain-portal/portal-server/portal_server.py` | `/home/aiciv/purebrain_portal/portal_server.py` |
| `packages/purebrain-portal/portal-server/portal-pb-styled.html` | `/home/aiciv/purebrain_portal/portal-pb-styled.html` |
| Generated from seed data | `/home/aiciv/purebrain_portal/portal_owner.json` |
| `packages/purebrain-portal/portal-server/assets/` (if exists) | `/home/aiciv/purebrain_portal/assets/` |

---

## Implementation: Bash Changes to birth_orchestrator_v4.sh

Replace the portal file deployment section in `setup_portal()`:

### OLD CODE (lines ~1698-1734):
```bash
# Extract portal_server.py and HTML from witness container
docker cp witness:/home/aiciv/purebrain_portal/portal_server.py /tmp/ps_${CONTAINER}.py
docker cp witness:/home/aiciv/purebrain_portal/portal-pb-stripped.html /tmp/ph_${CONTAINER}.html
# Patch CIV_NAME (witness → actual civ name)
sed -i 's/CIV_NAME = "witness"/CIV_NAME = "${CIV_NAME_LOWER}"/' /tmp/ps_${CONTAINER}.py
```

### NEW CODE:
```bash
# Pull portal files from PB2 source repo (source of truth for client portal)
PB2_PORTAL_SRC="/home/aiciv/aiciv-comms-hub/packages/purebrain-portal/portal-server"

# Verify PB2 repo is available
if [[ ! -f "${PB2_PORTAL_SRC}/portal_server.py" ]]; then
    die 5 "PB2 portal_server.py not found at ${PB2_PORTAL_SRC} — is aiciv-comms-hub cloned?"
fi
if [[ ! -f "${PB2_PORTAL_SRC}/portal-pb-styled.html" ]]; then
    die 5 "PB2 portal-pb-styled.html not found at ${PB2_PORTAL_SRC}"
fi

log_info "Deploying portal files from PB2 repo to ${CONTAINER}..."

# Copy files directly from repo to host /tmp, then into container
cp "${PB2_PORTAL_SRC}/portal_server.py" /tmp/ps_${CONTAINER}.py
cp "${PB2_PORTAL_SRC}/portal-pb-styled.html" /tmp/ph_${CONTAINER}.html

# NOTE: CIV_NAME is NOT patched via sed — PB2 auto-detects from ~/.aiciv-identity.json
# which is already written by Phase 5 (start_aiciv)

# Create portal dir in target container
docker exec ${CONTAINER} bash -c 'mkdir -p /home/aiciv/purebrain_portal'

# Copy portal_server.py and HTML
docker cp /tmp/ps_${CONTAINER}.py ${CONTAINER}:/home/aiciv/purebrain_portal/portal_server.py
docker cp /tmp/ph_${CONTAINER}.html ${CONTAINER}:/home/aiciv/purebrain_portal/portal-pb-styled.html

# Provision portal_owner.json with customer data (enables referral/payout system)
# Uses CIV_NAME_LOWER (AI name) and HUMAN_FIRST_NAME derived from HUMAN_NAME
HUMAN_FIRST="${HUMAN_NAME%% *}"   # first word of human name
REFERRAL_CODE="${CIV_NAME_LOWER}${HUMAN_FIRST,,}"   # e.g. "keenjared"
cat > /tmp/owner_${CONTAINER}.json <<EOF
{
  "name": "${HUMAN_NAME}",
  "email": "${HUMAN_EMAIL}",
  "referral_code": "${REFERRAL_CODE}"
}
EOF
docker cp /tmp/owner_${CONTAINER}.json ${CONTAINER}:/home/aiciv/purebrain_portal/portal_owner.json

# Copy assets if present in PB2 repo
if [[ -d "${PB2_PORTAL_SRC}/../assets" ]]; then
    docker exec ${CONTAINER} bash -c 'mkdir -p /home/aiciv/purebrain_portal/assets'
    docker cp "${PB2_PORTAL_SRC}/../assets/." ${CONTAINER}:/home/aiciv/purebrain_portal/assets/
    # Also copy favicons to portal root (served from SCRIPT_DIR = portal directory)
    for ico in favicon.ico favicon-32.png apple-touch-icon.png; do
        [[ -f "${PB2_PORTAL_SRC}/../assets/${ico}" ]] && \
            docker cp "${PB2_PORTAL_SRC}/../assets/${ico}" \
                      ${CONTAINER}:/home/aiciv/purebrain_portal/${ico}
    done
    echo 'portal assets deployed from PB2 repo'
else
    echo 'no assets directory in PB2 repo (skipping)'
fi

# Fix permissions
docker exec ${CONTAINER} chown -R aiciv:aiciv /home/aiciv/purebrain_portal/

# Cleanup
rm -f /tmp/ps_${CONTAINER}.py /tmp/ph_${CONTAINER}.html /tmp/owner_${CONTAINER}.json
echo 'portal files deployed from PB2 repo'
```

---

## Additional Notes

### Why No sed Patch
PB2 `portal_server.py` auto-detects `CIV_NAME`:
```python
_identity_file = Path.home() / ".aiciv-identity.json"
CIV_NAME = json.loads(_identity_file.read_text()).get("civ_id", "witness")
```
Phase 5 (`start_aiciv`) already writes `~/.aiciv-identity.json` inside the container.
No sed needed.

### portal_owner.json — New Birth Step
The PB2 referral panel reads `portal_owner.json`. Without it, the panel shows "Portal User"
as the customer name and referral features don't work.

The referral code format matches Aether's: `{ainame}{humanfirstname}` (e.g. `keenjared`).
This must match the subdomain that Aether provisions on their side.

### Fallback for Dev Testing
If `aiciv-comms-hub` is not available on the fleet host, Phase 6 should fall back to
copying from `witness` container (old behavior) with a warning. This prevents a hard
failure during development.

### When to Implement
This plan should be implemented when:
1. PB2 portal_server.py is validated in a test container ✅ (done with this PR)
2. `aiciv-comms-hub` repo is confirmed cloned on the fleet host (37.27.237.109)
3. The birth pipeline team has signed off on the portal_owner.json referral_code format

---

*Plan authored by infra-lead, 2026-03-06*
*Implementation target: birth_orchestrator_v4.sh setup_portal() function*
