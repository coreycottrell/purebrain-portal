#!/usr/bin/env bash
# warn-image-read.sh - PreToolUse hook for Read tool
# Exits non-zero (blocking the read) if the file is an image.
# This prevents the "image exceeds dimension limit for many-image requests" error
# that crashes Claude Code sessions when too many images accumulate in context.

FILE=$(echo "$TOOL_INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('file_path',''))" 2>/dev/null)

if echo "$FILE" | grep -qiE '\.(png|jpg|jpeg|gif|webp|bmp|tiff|svg)$'; then
    echo "WARNING: Reading image files into context causes dimension limit errors."
    echo "File: $FILE"
    echo "Instead: report the file path and let the human view it, or use a sub-agent."
    echo "To clean up stale images: bash tools/cleanup-context-images.sh"
    # Exit 1 to block the read
    exit 1
fi

exit 0
