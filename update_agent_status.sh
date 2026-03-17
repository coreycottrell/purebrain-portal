#!/bin/bash
# update_agent_status.sh — mark an agent active or idle in the PureBrain portal
#
# Usage:
#   ./update_agent_status.sh <agent-id> active [task description]
#   ./update_agent_status.sh <agent-id> idle
#   ./update_agent_status.sh <agent-id> working [task description]
#
# Examples:
#   ./update_agent_status.sh dept-accounting-finance active "Building Q1 financial model"
#   ./update_agent_status.sh browser-vision-tester working "Auditing pay-test-2 flow"
#   ./update_agent_status.sh dept-accounting-finance idle
#
# The portal must be running on localhost:8097 (or set PORTAL_URL env var).

PORTAL_URL="${PORTAL_URL:-http://localhost:8097}"
AGENT_ID="${1}"
STATUS="${2:-idle}"
TASK="${3:-}"

if [[ -z "$AGENT_ID" ]]; then
  echo "Usage: $0 <agent-id> <active|idle|working|offline> [task description]" >&2
  exit 1
fi

if [[ "$STATUS" != "active" && "$STATUS" != "idle" && "$STATUS" != "working" && "$STATUS" != "offline" ]]; then
  echo "Error: status must be one of: active idle working offline" >&2
  exit 1
fi

# Build JSON payload
if [[ -n "$TASK" ]]; then
  PAYLOAD=$(printf '{"agent":"%s","status":"%s","task":"%s"}' "$AGENT_ID" "$STATUS" "$TASK")
else
  PAYLOAD=$(printf '{"agent":"%s","status":"%s"}' "$AGENT_ID" "$STATUS")
fi

RESPONSE=$(curl -s -w "\n%{http_code}" \
  -X POST \
  "${PORTAL_URL}/api/agents/status" \
  -H "Content-Type: application/json" \
  -d "$PAYLOAD")

HTTP_BODY=$(echo "$RESPONSE" | head -n -1)
HTTP_CODE=$(echo "$RESPONSE" | tail -n 1)

if [[ "$HTTP_CODE" == "200" ]]; then
  echo "[agent-status] $AGENT_ID -> $STATUS${TASK:+ ($TASK)}"
else
  echo "[agent-status] ERROR ($HTTP_CODE): $HTTP_BODY" >&2
  exit 1
fi
