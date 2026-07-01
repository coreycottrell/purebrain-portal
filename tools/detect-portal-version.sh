#!/bin/bash
# Detect the portal release version from portal_config.py.
#
# Usage: bash tools/detect-portal-version.sh [PORTAL_DIR]
#   PORTAL_DIR: directory containing portal_config.py (defaults to repo root)
#
# Prints PORTAL_VERSION on stdout and exits 0 on success.
# Exits NON-ZERO (and prints nothing on stdout) if portal_config.py is missing
# or has no PORTAL_VERSION. Callers MUST treat a non-zero exit as a hard
# failure — never fall back to a fabricated version, which would mislabel a
# release and make fleet CIVs skip the update.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PORTAL_DIR="${1:-$(dirname "$SCRIPT_DIR")}"

# Heredoc-piped Python (not `python3 -c "..."`) so we avoid nested-quote
# escaping in a bash double-quoted string. The regex uses a triple-quoted
# literal so the ['"] quote class needs no escaping at all.
python3 << PYEOF
import re, sys
try:
    text = open("${PORTAL_DIR}/portal_config.py").read()
except Exception:
    sys.exit(1)
m = re.search(r'''PORTAL_VERSION\s*=\s*['"]([^'"]+)['"]''', text)
if m:
    print(m.group(1))
else:
    sys.exit(1)
PYEOF
