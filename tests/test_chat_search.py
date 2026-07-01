"""Tests for chat search button functionality.

Ensures the search button works across all view states (chat, inbox, CC)
and survives dynamic HTML recreation by switchChatView.
"""
import re
from pathlib import Path

import pytest

PORTAL_DIR = Path(__file__).parent.parent


class TestChatSearchButton:
    """Verify the chat search button has onclick handlers everywhere."""

    def test_static_search_button_has_onclick(self):
        """The static .ch-search button in the HTML must have onclick='toggleChatSearch()'."""
        html = (PORTAL_DIR / "portal-pb-styled.html").read_text()
        # Find the static search button in the chat header (not in JS strings)
        # It should have onclick="toggleChatSearch()"
        pattern = r'<button class="ch-search" onclick="toggleChatSearch\(\)">'
        assert re.search(pattern, html), (
            "Static .ch-search button missing onclick='toggleChatSearch()' — "
            "search button will not work on initial page load"
        )

    def test_toggleChatSearch_is_global_function(self):
        """toggleChatSearch must be a global function, not inside an IIFE."""
        html = (PORTAL_DIR / "portal-pb-styled.html").read_text()
        assert "function toggleChatSearch()" in html, (
            "toggleChatSearch must be a global function declaration"
        )
        # Must NOT be inside an IIFE
        iife_pattern = r'\(function\(\)\s*\{[^}]*toggleChatSearch'
        assert not re.search(iife_pattern, html), (
            "toggleChatSearch must NOT be inside an IIFE — "
            "dynamically created buttons cannot reach it"
        )

    def test_dynamic_search_buttons_have_onclick(self):
        """All dynamically created .ch-search buttons in inbox.js must have onclick."""
        inbox_js = (PORTAL_DIR / "static/js/features/inbox.js").read_text()
        # Find all ch-search button creations
        search_buttons = re.findall(r'<button class="ch-search"[^>]*>', inbox_js)
        assert len(search_buttons) > 0, "No .ch-search buttons found in inbox.js"
        for btn in search_buttons:
            assert "onclick=" in btn, (
                f"Dynamic .ch-search button missing onclick handler: {btn[:80]}... — "
                f"search will break after switchChatView recreates the header"
            )

    def test_no_search_button_without_onclick(self):
        """No .ch-search button anywhere should lack an onclick handler."""
        inbox_js = (PORTAL_DIR / "static/js/features/inbox.js").read_text()
        html = (PORTAL_DIR / "portal-pb-styled.html").read_text()
        combined = html + inbox_js
        # Find all ch-search button tags
        all_buttons = re.findall(r'<button class="ch-search"[^>]*>', combined)
        broken = [b for b in all_buttons if "onclick=" not in b]
        assert not broken, (
            f"{len(broken)} .ch-search button(s) without onclick handler — "
            f"these will be dead buttons after view switching: {broken}"
        )

    def test_search_bar_filters_messages(self):
        """toggleChatSearch must filter .msg elements by .msg-bubble text content."""
        html = (PORTAL_DIR / "portal-pb-styled.html").read_text()
        assert ".msg-bubble" in html and "indexOf(q)" in html, (
            "Chat search must filter messages by .msg-bubble text content"
        )

    def test_search_highlights_matches(self):
        """Search must highlight matching text with <mark> tags."""
        html = (PORTAL_DIR / "portal-pb-styled.html").read_text()
        assert "_highlightMatches" in html, (
            "Chat search must call _highlightMatches to highlight matching text"
        )
        assert "search-hl" in html, (
            "Highlighted matches must use the 'search-hl' CSS class"
        )

    def test_search_clears_highlights_on_close(self):
        """Closing search must remove all highlight marks."""
        html = (PORTAL_DIR / "portal-pb-styled.html").read_text()
        assert "_clearSearchHighlights" in html, (
            "Chat search must call _clearSearchHighlights when closing"
        )
        # Must be called both on close button click AND on toggle-off
        count = html.count("_clearSearchHighlights()")
        assert count >= 3, (
            f"_clearSearchHighlights must be called on close, toggle-off, and before re-highlight "
            f"(found {count} calls, expected at least 3)"
        )

    def test_search_bar_has_close_button(self):
        """The search bar must have a close button that clears the filter."""
        html = (PORTAL_DIR / "portal-pb-styled.html").read_text()
        # The close button resets display on all .msg elements
        assert "_chatSearchOpen=false" in html, (
            "Search bar close must reset _chatSearchOpen state"
        )

    def test_inbox_search_uses_searchInbox(self):
        """Inbox view search buttons must use searchInbox(), not toggleChatSearch()."""
        inbox_js = (PORTAL_DIR / "static/js/features/inbox.js").read_text()
        # Find search buttons that are in inbox view context (near 'inbox' references)
        inbox_search_buttons = re.findall(
            r'onclick="searchInbox\(\)".*?Search', inbox_js
        )
        assert len(inbox_search_buttons) > 0, (
            "Inbox view must have search buttons with onclick='searchInbox()'"
        )
