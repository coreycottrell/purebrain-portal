// dock.js — Dock state machine (3 states: UNDOCKED | DOCKED | DOCKED_EXPANDED)
// Depends on: PanelManager (window.PanelManager)

(function() {
  'use strict';

  var PM = window.PanelManager;
  var DEFAULT_HEIGHT = 280;

  // --- State queries ---

  function getState() {
    var app = document.querySelector('.app');
    if (!app.classList.contains('chat-docked')) return 'UNDOCKED';
    if (app.classList.contains('dock-expanded')) return 'DOCKED_EXPANDED';
    return 'DOCKED';
  }

  // --- Button updates ---

  function _updateButtons(docked) {
    var chatBtn = document.getElementById('dockChatBtn');
    var topBtn = document.getElementById('topnavDockBtn');
    var resetBtn = document.getElementById('dockResetBtn');
    var dockIcon = '<svg viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="2"/><line x1="3" y1="15" x2="21" y2="15"/></svg> ';
    var undockIcon = '<svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg> ';
    var chatBubble = '<svg viewBox="0 0 24 24"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg> ';
    if (chatBtn) chatBtn.innerHTML = (docked ? undockIcon : dockIcon) + (docked ? 'Undock' : 'Dock');
    if (topBtn) {
      topBtn.innerHTML = (docked ? undockIcon : chatBubble) + (docked ? 'Undock Chat' : 'Dock Chat');
      topBtn.title = docked ? 'Undock chat (Ctrl+Shift+D)' : 'Dock chat to bottom (Ctrl+Shift+D)';
      topBtn.classList.toggle('docked', docked);
    }
    if (resetBtn) resetBtn.style.display = docked ? '' : 'none';
  }

  // --- State transitions ---

  function _enterDocked(showTab) {
    var app = document.querySelector('.app');
    var chatArea = document.getElementById('chatArea');
    app.classList.add('chat-docked');
    chatArea.style.display = 'flex';
    _updateButtons(true);
    localStorage.setItem('portal_chat_docked', '1');
    if (showTab === 'chat') {
      // Dock straight into expanded (fullscreen chat)
      app.classList.add('dock-expanded');
      PM._setDockPadding(0);
      PM._hideAllExceptChat();
      PM._highlightSidebar('chat');
      PM.setActivePanel('chat');
    } else if (showTab === '__subview__') {
      // Dock with a sub-view (inbox/cc) about to be promoted above
      // Set up the dock at saved height; switchChatView will handle the rest
      app.classList.remove('dock-expanded');
      var savedH = parseInt(localStorage.getItem('portal_dock_height')) || DEFAULT_HEIGHT;
      chatArea.style.height = savedH + 'px';
      PM._setDockPadding(savedH);
      PM.setActivePanel('chat');
    } else {
      // Dock with a panel above
      app.classList.remove('dock-expanded');
      var savedH = parseInt(localStorage.getItem('portal_dock_height')) || DEFAULT_HEIGHT;
      chatArea.style.height = savedH + 'px';
      PM._setDockPadding(savedH);
      PM._hideAllExceptChat();
      PM._showPanelElement(showTab);
      PM._highlightSidebar(showTab);
      PM.setActivePanel(showTab);
      PM._fireTabCallbacks(showTab);
    }
  }

  function expand() {
    var app = document.querySelector('.app');
    app.classList.add('dock-expanded');
    PM._setDockPadding(0);
    // Hide all non-chat panels, show chat fullscreen
    PM._hideAllExceptChat();
    var chatArea = document.getElementById('chatArea');
    if (chatArea) chatArea.style.display = 'flex';
    PM._highlightSidebar('chat');
    PM.setActivePanel('chat');
  }

  function collapse() {
    var app = document.querySelector('.app');
    app.classList.remove('dock-expanded');
    var savedH = parseInt(localStorage.getItem('portal_dock_height')) || DEFAULT_HEIGHT;
    var chatArea = document.getElementById('chatArea');
    if (chatArea) chatArea.style.height = savedH + 'px';
    PM._setDockPadding(savedH);
  }

  function restoreHeight() {
    var savedH = parseInt(localStorage.getItem('portal_dock_height')) || DEFAULT_HEIGHT;
    var chatArea = document.getElementById('chatArea');
    if (chatArea) chatArea.style.height = savedH + 'px';
    PM._setDockPadding(savedH);
  }

  function _exitDock() {
    var app = document.querySelector('.app');
    var chatArea = document.getElementById('chatArea');
    // Remember what panel is currently active (highlighted in sidebar)
    var panelToShow = PM.getActivePanel() || 'chat';
    // Check if a sub-view (inbox/cc) was promoted above the dock
    var wasSubView = _getActiveChatSubView();
    // Restore any promoted inbox/cc panels back into chatArea before undocking
    if (window._restorePromotedPanels) window._restorePromotedPanels();
    app.classList.remove('chat-docked');
    app.classList.remove('dock-expanded');
    chatArea.style.height = '';
    PM._setDockPadding(0);
    _updateButtons(false);
    // Stay on whatever panel was active
    PM._hideAllPanels();
    PM._showPanelElement(panelToShow);
    PM._highlightSidebar(panelToShow);
    PM.setActivePanel(panelToShow);
    localStorage.removeItem('portal_chat_docked');
    // If inbox/cc was the active sub-view, restore it inside chatArea
    if (panelToShow === 'chat' && wasSubView && wasSubView !== 'chat') {
      if (window.switchChatView) window.switchChatView(wasSubView);
    }
  }

  // --- Public API ---

  function toggle() {
    var state = getState();
    if (state === 'UNDOCKED') {
      if (window.innerWidth <= 1024) return; // mobile guard
      // Check if a chat sub-view (inbox/cc) is currently showing
      var currentPanel = PM.getActivePanel() || 'chat';
      var activeSubView = _getActiveChatSubView();
      if (currentPanel === 'chat' && activeSubView && activeSubView !== 'chat') {
        // Dock with inbox/cc promoted above the dock
        _enterDocked('__subview__');
        // Trigger the sub-view switch to properly promote it
        if (window.switchChatView) window.switchChatView(activeSubView);
      } else {
        _enterDocked(currentPanel);
      }
    } else {
      _exitDock();
    }
  }

  function _getActiveChatSubView() {
    var inbox = document.getElementById('inboxView');
    var ccView = document.getElementById('ccView');
    if (inbox && (inbox.style.display === 'flex' || inbox.style.display === 'block')) return 'inbox';
    if (ccView && ccView.classList.contains('visible')) return 'cc';
    var activeBtn = document.querySelector('.chat-subtab.active');
    if (activeBtn) {
      var text = activeBtn.textContent.trim();
      if (text.indexOf('Inbox') === 0) return 'inbox';
      if (text.indexOf('CC') === 0) return 'cc';
    }
    return 'chat';
  }

  function resetHeight() {
    localStorage.removeItem('portal_dock_height');
    if (getState() !== 'UNDOCKED') {
      var chatArea = document.getElementById('chatArea');
      chatArea.style.height = DEFAULT_HEIGHT + 'px';
      PM._setDockPadding(DEFAULT_HEIGHT);
    }
  }

  // --- Resize handler ---

  function _initResize() {
    var resizeEl = document.getElementById('dockResize');
    if (!resizeEl) return;
    var dragging = false;
    var startY = 0, startH = 0;
    resizeEl.addEventListener('mousedown', function(e) {
      if (getState() === 'UNDOCKED') return;
      dragging = true;
      startY = e.clientY;
      startH = document.getElementById('chatArea').offsetHeight;
      document.body.style.cursor = 'row-resize';
      document.body.style.userSelect = 'none';
      e.preventDefault();
    });
    document.addEventListener('mousemove', function(e) {
      if (!dragging) return;
      var delta = startY - e.clientY;
      var newH = startH + delta;
      var maxH = window.innerHeight * 0.6;
      if (newH < 150) newH = 150;
      if (newH > maxH) newH = Math.floor(maxH);
      document.getElementById('chatArea').style.height = newH + 'px';
      PM._setDockPadding(newH);
      localStorage.setItem('portal_dock_height', String(newH));
    });
    document.addEventListener('mouseup', function() {
      if (dragging) {
        dragging = false;
        document.body.style.cursor = '';
        document.body.style.userSelect = '';
      }
    });
  }

  // --- Auto-restore & auto-undock ---

  function _initAutoRestore() {
    if (localStorage.getItem('portal_chat_docked') === '1' && window.innerWidth > 1024) {
      setTimeout(function() { toggle(); }, 500);
    }
  }

  function _initAutoUndock() {
    window.addEventListener('resize', function() {
      if (window.innerWidth <= 1024 && getState() !== 'UNDOCKED') {
        _exitDock();
      }
    });
  }

  // --- Keyboard shortcut ---

  function _initKeyboard() {
    document.addEventListener('keydown', function(e) {
      if (e.ctrlKey && e.shiftKey && e.key === 'D') {
        e.preventDefault();
        toggle();
      }
    });
  }

  // --- Expose on window ---

  window.DockManager = {
    getState: getState,
    toggle: toggle,
    expand: expand,
    collapse: collapse,
    restoreHeight: restoreHeight,
    resetHeight: resetHeight,
    init: function() {
      _initResize();
      _initAutoRestore();
      _initAutoUndock();
      _initKeyboard();
    }
  };

  // Legacy global for onclick handlers in HTML
  window.toggleDockChat = toggle;
  window.resetDockHeight = resetHeight;

})();
