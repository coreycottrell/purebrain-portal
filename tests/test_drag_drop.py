"""Tests for drag-and-drop file upload selector fix."""
import pytest


def test_chat_area_class_exists_in_html():
    """The drag-drop target element must use .chat-area class (not .chat-panel)."""
    from pathlib import Path
    html = (Path(__file__).parent.parent / "portal-pb-styled.html").read_text()
    assert 'class="chat-area"' in html or "class='chat-area'" in html, \
        "chat-area class not found in HTML — drag-drop target will fail"
    # Ensure we're NOT still referencing the wrong class
    chat_js = (Path(__file__).parent.parent / "static/js/features/chat.js").read_text()
    if "drag" in chat_js:
        # Check the 200 chars before "drag" for .chat-panel reference
        drag_idx = chat_js.index("drag")
        context = chat_js[max(0, drag_idx - 200):drag_idx]
        assert 'chat-panel' not in context, \
            "chat.js drag code still references .chat-panel (wrong class)"


def test_drag_over_css_targets_chat_area():
    """The drag-over CSS must target .chat-area, not .chat-panel."""
    from pathlib import Path
    css = (Path(__file__).parent.parent / "static/css/panels.css").read_text()
    assert ".chat-area.drag-over" in css, \
        "CSS drag-over rule should target .chat-area.drag-over"
    assert ".chat-panel.drag-over" not in css, \
        "CSS should NOT reference .chat-panel.drag-over (class doesn't exist)"


def test_chat_js_uses_chatArea_id():
    """The drag-drop code should find the element via getElementById('chatArea')."""
    from pathlib import Path
    js = (Path(__file__).parent.parent / "static/js/features/chat.js").read_text()
    assert "getElementById('chatArea')" in js or 'getElementById("chatArea")' in js, \
        "Drag-drop should use getElementById('chatArea') to find the drop zone"
