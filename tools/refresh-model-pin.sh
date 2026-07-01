#!/usr/bin/env bash
#
# refresh-model-pin.sh — refresh ~/.claude_session_model during a portal upgrade.
#
# Extracted from upgrade-portal.sh so the pin logic is testable in isolation
# (see tests/test_model_pin_refresh.py). Run with the detected live model as $1
# (empty string if none was detected).
#
# Rules (a model pin does NOT expire — never downgrade based on file age):
#   1. Live model detected ($1 non-empty)  -> write it.
#   2. Otherwise, file EXISTS (any age)     -> PRESERVE untouched.
#   3. Otherwise, file genuinely MISSING    -> write the current floor default.
#
set -euo pipefail

DETECTED_MODEL="${1:-}"
MODEL_FILE="${HOME}/.claude_session_model"

# Current fleet floor. Update this when the floor moves; never below it.
DEFAULT_MODEL="claude-opus-4-8[1m]"

if [[ -n "$DETECTED_MODEL" ]]; then
    echo "$DETECTED_MODEL" > "$MODEL_FILE"
    echo "Detected live model: $DETECTED_MODEL (written to ~/.claude_session_model)"
elif [[ -f "$MODEL_FILE" ]]; then
    # An existing pin always wins. Staleness (file age) must NOT trigger an
    # overwrite — a valid pin (e.g. claude-opus-4-8) would otherwise be
    # downgraded just because the file is old.
    echo "Preserved existing model pin: $(cat "$MODEL_FILE" 2>/dev/null) (not overwriting)"
else
    echo "$DEFAULT_MODEL" > "$MODEL_FILE"
    echo "Set default model: $DEFAULT_MODEL (no existing pin, no session to detect from)"
fi
