#!/bin/bash
# portal_send_file.sh — Send a file into the portal chat as an Aether message
# Usage: ./portal_send_file.sh /path/to/file.md "Optional caption message"
# Usage: ./portal_send_file.sh --text "Just a text message, no file"
#
# FIXED 2026-03-08: Uses /api/deliverable endpoint instead of direct JSONL write
# Prevents duplicate entries and ensures correct PORTAL_FILE card rendering

set -euo pipefail

PORTAL_DIR="$HOME/purebrain_portal"
TOKEN_FILE="$PORTAL_DIR/.portal-token"
PORTAL_URL="http://localhost:8097"
CHAT_JSONL="$PORTAL_DIR/portal-chat.jsonl"

if [ ! -f "$TOKEN_FILE" ]; then
  echo "Error: Portal token file not found at $TOKEN_FILE" >&2
  exit 1
fi
TOKEN=$(cat "$TOKEN_FILE")

# Text-only mode
if [ "${1:-}" = "--text" ]; then
  shift
  TEXT="$*"
  TIMESTAMP=$(date +%s)
  ID="portal-text-${TIMESTAMP}-${RANDOM}"
  python3 /tmp/_portal_write_text.py "$CHAT_JSONL" "$TIMESTAMP" "$ID" "$TEXT" 2>/dev/null || \
    python3 -c "
import json,sys
entry={'role':'assistant','text':sys.argv[4],'timestamp':int(sys.argv[2]),'id':sys.argv[3]}
open(sys.argv[1],'a').write(json.dumps(entry,ensure_ascii=False)+chr(10))
" "$CHAT_JSONL" "$TIMESTAMP" "$ID" "$TEXT"
  echo "Sent text to portal chat"
  exit 0
fi

FILE_PATH="${1:?Usage: portal_send_file.sh /path/to/file [caption]}"
CAPTION="${2:-}"

if [ ! -f "$FILE_PATH" ]; then
  echo "Error: File not found: $FILE_PATH" >&2
  exit 1
fi

ORIGINAL_NAME=$(basename "$FILE_PATH")

# Build JSON payload safely via python3
PAYLOAD=$(python3 -c "
import json,sys
print(json.dumps({'path':sys.argv[1],'name':sys.argv[2],'message':sys.argv[3]}))
" "$FILE_PATH" "$ORIGINAL_NAME" "$CAPTION")

# POST to /api/deliverable — server handles file copy + JSONL write
# Retry up to 3 times with 2s delay to avoid broken fallback rendering
for _attempt in 1 2 3; do
  RESULT=$(curl -sf -X POST "$PORTAL_URL/api/deliverable" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d "$PAYLOAD" 2>/dev/null) || RESULT='{"ok":false}'
  if echo "$RESULT" | python3 -c "import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get('ok') else 1)" 2>/dev/null; then
    break
  fi
  [ "$_attempt" -lt 3 ] && sleep 2
done

if echo "$RESULT" | python3 -c "import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get('ok') else 1)" 2>/dev/null; then
  STORED=$(echo "$RESULT" | python3 -c "import json,sys; print(json.load(sys.stdin).get('filename','unknown'))" 2>/dev/null || echo "unknown")
  echo "File sent to portal chat: ${ORIGINAL_NAME} (stored as ${STORED})"
else
  # Fallback: copy file manually and write to JSONL directly
  echo "API unavailable, using fallback..." >&2
  UPLOADS_DIR="$HOME/portal_uploads"
  mkdir -p "$UPLOADS_DIR"
  STORED_NAME="$(date +%s%3N)_${ORIGINAL_NAME}"
  cp "$FILE_PATH" "${UPLOADS_DIR}/${STORED_NAME}"
  EXT="${ORIGINAL_NAME##*.}"
  case "$EXT" in
    md)   ICON="📄" ;;
    html) ICON="🌐" ;;
    pdf)  ICON="📕" ;;
    png|jpg|jpeg|gif|webp) ICON="🖼️" ;;
    csv)  ICON="📊" ;;
    json) ICON="📋" ;;
    *)    ICON="📎" ;;
  esac
  TIMESTAMP=$(date +%s)
  ID="portal-file-${TIMESTAMP}-${RANDOM}"
  python3 -c "
import json,sys
cap,icon,orig,stored,ts,mid,jpath=sys.argv[1:]
text=(cap+'\n\n' if cap else '')+icon+' **'+orig+'**\n[PORTAL_FILE:'+stored+':'+orig+']'
entry={'role':'assistant','text':text,'timestamp':int(ts),'id':mid}
open(jpath,'a').write(json.dumps(entry,ensure_ascii=False)+chr(10))
print('File sent to portal (fallback): '+orig)
" "$CAPTION" "$ICON" "$ORIGINAL_NAME" "$STORED_NAME" "$TIMESTAMP" "$ID" "$CHAT_JSONL"
fi
