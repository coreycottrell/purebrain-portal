#!/bin/bash
# Telegram send utility - v2 (Feature 3: --caption, --html, --reply flags)
#
# Usage:
#   ./tools/tg_send.sh "message text"
#   ./tools/tg_send.sh --html "<b>bold</b> message"
#   ./tools/tg_send.sh --reply 12345 "replying to message 12345"
#   ./tools/tg_send.sh --photo /path/to/image.jpg "optional caption"
#   ./tools/tg_send.sh --photo /path/to/image.jpg --caption "caption text"
#   ./tools/tg_send.sh --file /path/to/file.pdf "optional caption"
#   ./tools/tg_send.sh --file /path/to/file.pdf --caption "caption text"
#   ./tools/tg_send.sh --file /path/to/file.pdf --html --caption "<b>bold</b> caption"

BOT_TOKEN="8559081952:AAHcLiEcC3GtQCAHRu5yc86BByiiLDqyjz0"
CHAT_ID="548906264"

# ─────────────────────────────────────────
# Parse arguments
# ─────────────────────────────────────────
MODE="text"         # text | photo | file
PARSE_MODE=""       # empty | HTML | Markdown
REPLY_TO_MSG_ID=""  # empty or a message_id integer
CAPTION=""          # caption for photo/file
TEXT=""             # message text
FILE_PATH=""        # path for photo/file

while [[ $# -gt 0 ]]; do
    case "$1" in
        --photo)
            MODE="photo"
            shift
            if [[ -n "$1" && "$1" != --* ]]; then
                FILE_PATH="$1"
                shift
            fi
            ;;
        --file)
            MODE="file"
            shift
            if [[ -n "$1" && "$1" != --* ]]; then
                FILE_PATH="$1"
                shift
            fi
            ;;
        --html)
            PARSE_MODE="HTML"
            shift
            ;;
        --markdown)
            PARSE_MODE="Markdown"
            shift
            ;;
        --caption)
            shift
            CAPTION="$1"
            shift
            ;;
        --reply)
            shift
            REPLY_TO_MSG_ID="$1"
            shift
            ;;
        *)
            # Positional: text message or trailing caption for photo/file (backward compat)
            if [[ "$MODE" == "text" ]]; then
                TEXT="$1"
            else
                if [[ -z "$CAPTION" ]]; then
                    CAPTION="$1"
                fi
            fi
            shift
            ;;
    esac
done

# ─────────────────────────────────────────
# Send
# ─────────────────────────────────────────
if [[ "$MODE" == "photo" ]]; then
    if [[ ! -f "$FILE_PATH" ]]; then
        echo "Error: photo file not found: $FILE_PATH" >&2
        exit 1
    fi
    curl -s -X POST "https://api.telegram.org/bot$BOT_TOKEN/sendPhoto" \
        -F "chat_id=$CHAT_ID" \
        -F "photo=@$FILE_PATH" \
        ${CAPTION:+-F "caption=$CAPTION"} \
        ${PARSE_MODE:+-F "parse_mode=$PARSE_MODE"} \
        ${REPLY_TO_MSG_ID:+-F "reply_to_message_id=$REPLY_TO_MSG_ID"}

elif [[ "$MODE" == "file" ]]; then
    if [[ ! -f "$FILE_PATH" ]]; then
        echo "Error: file not found: $FILE_PATH" >&2
        exit 1
    fi
    curl -s -X POST "https://api.telegram.org/bot$BOT_TOKEN/sendDocument" \
        -F "chat_id=$CHAT_ID" \
        -F "document=@$FILE_PATH" \
        ${CAPTION:+-F "caption=$CAPTION"} \
        ${PARSE_MODE:+-F "parse_mode=$PARSE_MODE"} \
        ${REPLY_TO_MSG_ID:+-F "reply_to_message_id=$REPLY_TO_MSG_ID"}

else
    # Text message
    if [[ -z "$TEXT" ]]; then
        echo "Error: no message text provided" >&2
        echo "Usage: tg_send.sh \"message\" | --photo file | --file file [--caption text] [--html] [--reply msg_id]" >&2
        exit 1
    fi
    curl -s -X POST "https://api.telegram.org/bot$BOT_TOKEN/sendMessage" \
        -d "chat_id=$CHAT_ID" \
        --data-urlencode "text=$TEXT" \
        ${PARSE_MODE:+-d "parse_mode=$PARSE_MODE"} \
        ${REPLY_TO_MSG_ID:+-d "reply_to_message_id=$REPLY_TO_MSG_ID"}
fi
