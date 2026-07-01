"""
Shared configuration for PureBrain Portal tests.

Auto-detects paths relative to the portal root directory.
Override with environment variables if needed:
  PORTAL_DIR  - path to portal root (default: parent of tests/ dir)
  PORTAL_URL  - base URL of running server (default: http://localhost:8097)
"""

import os

# Auto-detect portal root: tests/ lives inside the portal directory
PORTAL_DIR = os.environ.get(
    "PORTAL_DIR",
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)

BASE_URL = os.environ.get("PORTAL_URL", "http://localhost:8097")

TOKEN_FILE = os.path.join(PORTAL_DIR, ".portal-token")
HTML_FILE = os.path.join(PORTAL_DIR, "portal-pb-styled.html")
SERVER_FILE = os.path.join(PORTAL_DIR, "portal_server.py")
AGENTS_DB = os.path.join(PORTAL_DIR, "agents.db")
REFERRALS_DB = os.path.join(PORTAL_DIR, "referrals.db")
CLIENTS_DB = os.path.join(PORTAL_DIR, "clients.db")


def load_token():
    """Read the portal auth token from disk. Returns empty string if not found."""
    if not os.path.exists(TOKEN_FILE):
        return ""
    with open(TOKEN_FILE) as f:
        return f.read().strip()


import pytest

@pytest.fixture(autouse=True)
def _clear_msg_cache():
    """Clear _parse_all_messages cache before each test to prevent cross-test leakage."""
    try:
        import portal_server
        if hasattr(portal_server, '_invalidate_msg_cache'):
            portal_server._invalidate_msg_cache()
    except (ImportError, Exception):
        pass
    yield
    try:
        import portal_server
        if hasattr(portal_server, '_invalidate_msg_cache'):
            portal_server._invalidate_msg_cache()
    except (ImportError, Exception):
        pass
