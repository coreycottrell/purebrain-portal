// panel-manager.js — Single source of truth for panel visibility
// Handles built-in panels (data-tab) AND custom/injected panels (data-panel)
// Dock-aware: all panel switches go through switchTo() which respects dock state

(function() {
  'use strict';

  // Built-in panels: tab name -> element ID
  var BUILTIN = {
    'letstalk': 'letstalkArea',
    'chat': 'chatArea',
    'tasks': 'tasksArea',
    'files': 'filesArea',
    'teams': 'teamsArea',
    'todo': 'todoArea',
    'agents': 'agentsArea',
    'agent-roster': 'agent-rosterArea',
    'refer': 'referArea',
    'about': 'aboutArea',
    'hub': 'hubArea',
    'constitution': 'constitutionArea',
    'deployments': 'deploymentsArea',
    'payments': 'paymentsArea',
    'settings': 'settingsArea',
    'clients': 'clientsArea',
    'skills': 'skillsArea'
  };

  // Legacy allPanels array (kept for backward compat with any code referencing it)
  var allPanelIds = Object.keys(BUILTIN).map(function(k) { return BUILTIN[k]; });

  // Custom panels registered at runtime via injection system
  var _customPanels = {}; // name -> { elementId, handler }

  // Current state
  var _activePanel = 'chat';
  var _lastNonChatTab = null;

  // --- Internal helpers ---

  function _isCustom(panelId) {
    return panelId in _customPanels;
  }

  function _getElementId(panelId) {
    if (BUILTIN[panelId]) return BUILTIN[panelId];
    if (_customPanels[panelId]) return _customPanels[panelId].elementId;
    return null;
  }

  function _hideAllPanels() {
    // Hide built-in
    Object.keys(BUILTIN).forEach(function(key) {
      var el = document.getElementById(BUILTIN[key]);
      if (!el) return;
      if (key === 'chat') el.style.display = 'none';
      else el.classList.remove('visible');
    });
    // Hide custom
    Object.keys(_customPanels).forEach(function(key) {
      var el = document.getElementById(_customPanels[key].elementId);
      if (el) el.classList.remove('visible');
    });
  }

  function _hideAllExceptChat() {
    Object.keys(BUILTIN).forEach(function(key) {
      if (key === 'chat') return;
      var el = document.getElementById(BUILTIN[key]);
      if (el) el.classList.remove('visible');
    });
    Object.keys(_customPanels).forEach(function(key) {
      var el = document.getElementById(_customPanels[key].elementId);
      if (el) el.classList.remove('visible');
    });
  }

  function _showPanelElement(panelId) {
    if (BUILTIN[panelId]) {
      var el = document.getElementById(BUILTIN[panelId]);
      if (!el) return;
      if (panelId === 'chat') el.style.display = 'flex';
      else el.classList.add('visible');
    } else if (_customPanels[panelId]) {
      var el = document.getElementById(_customPanels[panelId].elementId);
      if (el) el.classList.add('visible');
    }
  }

  function _highlightSidebar(panelId) {
    document.querySelectorAll('.sidebar-item').forEach(function(i) { i.classList.remove('active'); });
    document.querySelectorAll('.nav-item[data-panel]').forEach(function(i) { i.classList.remove('active'); });
    // Try built-in sidebar item
    var el = document.querySelector('.sidebar-item[data-tab="' + panelId + '"]');
    if (el) { el.classList.add('active'); return; }
    // Try custom nav item
    var customEl = document.querySelector('.nav-item[data-panel="' + panelId + '"]');
    if (customEl) customEl.classList.add('active');
  }

  function _fireTabCallbacks(tab) {
    if (tab === 'tasks' && window._portalTasks) window._portalTasks.loadTasks();
    if (tab === 'todo' && window._portalTasks) window._portalTasks.loadHubTasks();
    if (tab === 'hub' && window._portalHub) window._portalHub.refresh();
    if (tab === 'settings' && window._portalSettings) window._portalSettings.load();
    if (tab === 'settings' && window._loadProfile) window._loadProfile();
    if ((tab === 'agents' || tab === 'agent-roster') && window._loadAgentsPanel) window._loadAgentsPanel();
    if (tab === 'files' && window._portalFiles) window._portalFiles.load();
    if (tab === 'refer' && window._loadReferrals) window._loadReferrals();
    if (tab === 'clients' && window._loadClients) window._loadClients();
    if (tab === 'constitution') {
      var activeBtn = document.querySelector('#constitutionArea .const-subtab.active');
      if (activeBtn) activeBtn.click();
      else if (typeof loadConstitutionRules === 'function') loadConstitutionRules();
    }
    if (tab === 'deployments' && window.initDeployments) window.initDeployments();
    if (tab === 'skills' && window._portalSkills) window._portalSkills.load();
    if (tab === 'letstalk' && typeof openHmiVoiceOverlay === 'function') openHmiVoiceOverlay();
    // Custom panel handlers
    if (_customPanels[tab] && _customPanels[tab].handler) _customPanels[tab].handler();
    if (window._customPanelHandlers && window._customPanelHandlers[tab]) window._customPanelHandlers[tab]();
  }

  function _setDockPadding(height) {
    var pad = height ? (height + 8) + 'px' : '';
    Object.keys(BUILTIN).forEach(function(key) {
      if (key === 'chat') return;
      var el = document.getElementById(BUILTIN[key]);
      if (el) el.style.paddingBottom = pad;
    });
    // Pad custom panels (registered + any unregistered panel-* elements)
    document.querySelectorAll('[id^="panel-"]').forEach(function(el) {
      el.style.paddingBottom = pad;
    });
  }

  // --- Main API ---

  // switchTo: unified panel switching, dock-aware
  // This is THE entry point for all panel switches (built-in and custom)
  function switchTo(panelId) {
    // Restore any promoted inbox/cc panels before switching (prevents ghost panels)
    if (window._restorePromotedPanels) window._restorePromotedPanels();
    // Close voice overlay when leaving Let's Talk
    if (_activePanel === 'letstalk' && panelId !== 'letstalk' && typeof closeHmiVoiceOverlay === 'function') closeHmiVoiceOverlay();
    var dock = window.DockManager;
    var state = dock ? dock.getState() : 'UNDOCKED';

    if (state === 'UNDOCKED') {
      _hideAllPanels();
      _showPanelElement(panelId);
      _highlightSidebar(panelId);
    } else if (state === 'DOCKED') {
      if (panelId === 'chat') {
        // Expand dock to show chat fullscreen
        if (dock) dock.expand();
        _highlightSidebar('chat');
      } else {
        // Show panel above the dock, keep chat docked at bottom
        _hideAllExceptChat();
        _showPanelElement(panelId);
        _highlightSidebar(panelId);
        if (dock) dock.restoreHeight();
      }
    } else if (state === 'DOCKED_EXPANDED') {
      if (panelId === 'chat') {
        return; // already fullscreen chat
      }
      // Collapse back to DOCKED, show the selected panel above
      if (dock) dock.collapse();
      _hideAllExceptChat();
      _showPanelElement(panelId);
      _highlightSidebar(panelId);
    }

    _activePanel = panelId;
    if (panelId !== 'chat') _lastNonChatTab = panelId;
    // Notify 3D brain animation to pause/resume based on chat visibility
    window.dispatchEvent(new CustomEvent('brain-visibility', { detail: panelId === 'chat' }));
    _fireTabCallbacks(panelId);
  }

  // Register a custom panel (called by injection bridge)
  function registerPanel(id, opts) {
    opts = opts || {};
    _customPanels[id] = {
      elementId: opts.elementId || ('panel-' + id),
      handler: opts.handler || null
    };
  }

  // Auto-discover custom panels from DOM (called after injection)
  function discoverCustomPanels() {
    var navItems = document.querySelectorAll('.nav-item[data-panel]');
    if (!navItems.length) return;
    var grp = document.getElementById('custom-panels-group');
    if (grp) grp.style.display = '';
    navItems.forEach(function(item) {
      var id = item.getAttribute('data-panel');
      if (!_customPanels[id]) {
        registerPanel(id);
      }
    });
  }

  // Bind click handlers for built-in sidebar items
  function _bindBuiltinTabs() {
    document.querySelectorAll('.sidebar-item[data-tab]').forEach(function(item) {
      item.addEventListener('click', function(e) {
        e.preventDefault();
        var tab = this.getAttribute('data-tab');
        switchTo(tab);
      });
    });
  }

  // Bind click handlers for custom nav items
  function _bindCustomTabs() {
    document.querySelectorAll('.nav-item[data-panel]').forEach(function(item) {
      item.addEventListener('click', function(e) {
        e.preventDefault();
        var panelId = this.getAttribute('data-panel');
        switchTo(panelId);
      });
    });
  }

  // --- Expose on window ---

  window.PanelManager = {
    switchTo: switchTo,
    registerPanel: registerPanel,
    discoverCustomPanels: discoverCustomPanels,
    getActivePanel: function() { return _activePanel; },
    setActivePanel: function(id) { _activePanel = id; },
    getLastNonChatTab: function() { return _lastNonChatTab; },
    setLastNonChatTab: function(id) { _lastNonChatTab = id; },
    // Internals exposed for DockManager
    _hideAllPanels: _hideAllPanels,
    _hideAllExceptChat: _hideAllExceptChat,
    _showPanelElement: _showPanelElement,
    _highlightSidebar: _highlightSidebar,
    _fireTabCallbacks: _fireTabCallbacks,
    _setDockPadding: _setDockPadding,
    _getElementId: _getElementId,
    _isCustom: _isCustom,
    _bindBuiltinTabs: _bindBuiltinTabs,
    _bindCustomTabs: _bindCustomTabs,
    // Legacy compat
    _builtinPanels: BUILTIN,
    _customPanels: _customPanels,
    _allPanelIds: allPanelIds
  };

  // Also expose the legacy allPanels array for any code that references it
  window.allPanels = allPanelIds;

})();
