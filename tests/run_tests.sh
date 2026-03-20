#!/bin/bash
# PureBrain Portal Test Suite Runner
# Usage: bash tests/run_tests.sh
# Prerequisites: portal running at localhost:8097 (for API tests), token at .portal-token

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PORTAL_DIR="$(dirname "$SCRIPT_DIR")"
TESTS_DIR="$SCRIPT_DIR"
TOKEN_FILE="$PORTAL_DIR/.portal-token"

export PORTAL_DIR

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

echo -e "${BOLD}${CYAN}========================================${NC}"
echo -e "${BOLD}${CYAN}  PureBrain Portal Test Suite${NC}"
echo -e "${BOLD}${CYAN}========================================${NC}"
echo ""

# Pre-flight checks
if [ ! -f "$TOKEN_FILE" ]; then
    echo -e "${YELLOW}NOTE: No .portal-token found — API tests will be skipped${NC}"
fi

if ! curl -s --max-time 3 http://localhost:8097/health > /dev/null 2>&1; then
    echo -e "${YELLOW}NOTE: Portal not running — live API tests will be skipped${NC}"
    echo -e "${YELLOW}Static tests (frontend structure, syntax) will still run.${NC}"
fi
echo ""

# Track results
PASS=0
FAIL=0
ERRORS=()

run_test_file() {
    local file="$1"
    local name="$2"
    echo -e "${BOLD}--- $name ---${NC}"

    if [ ! -f "$TESTS_DIR/$file" ]; then
        echo -e "${YELLOW}  SKIP: $file not found${NC}"
        echo ""
        return
    fi

    output=$(cd "$PORTAL_DIR" && python3 -m unittest "$TESTS_DIR/$file" -v 2>&1)
    exit_code=$?

    passed=$(echo "$output" | grep -c "... ok" 2>/dev/null || echo 0)
    failed=$(echo "$output" | grep -cE "FAIL|ERROR" 2>/dev/null || echo 0)

    if [ $exit_code -eq 0 ]; then
        echo -e "${GREEN}  ✓ PASSED${NC} ($passed tests)"
        PASS=$((PASS + passed))
    else
        echo -e "${RED}  ✗ FAILED${NC}"
        echo "$output" | grep -E "FAIL|ERROR|AssertionError" | head -10 | sed 's/^/    /'
        FAIL=$((FAIL + 1))
        ERRORS+=("$name")
        PASS=$((PASS + passed))
    fi
    echo ""
}

# Run all test files
run_test_file "test_api.py"          "API Auth Tests (test_api.py)"
run_test_file "test_integration.py"  "Integration Tests (test_integration.py)"
run_test_file "test_frontend.py"     "Frontend Structure Tests (test_frontend.py)"
run_test_file "test_db.py"           "Database Integrity Tests (test_db.py)"

# Summary
echo -e "${BOLD}${CYAN}========================================${NC}"
echo -e "${BOLD}  RESULTS SUMMARY${NC}"
echo -e "${BOLD}${CYAN}========================================${NC}"

if [ ${#ERRORS[@]} -eq 0 ]; then
    echo -e "${GREEN}${BOLD}  ALL TESTS PASSED${NC}"
else
    echo -e "${RED}${BOLD}  FAILURES IN:${NC}"
    for err in "${ERRORS[@]}"; do
        echo -e "${RED}    - $err${NC}"
    done
fi

echo ""
echo -e "  Test files run: 4"
echo -e "  Test files failed: ${#ERRORS[@]}"
echo ""

[ ${#ERRORS[@]} -eq 0 ] && exit 0 || exit 1
