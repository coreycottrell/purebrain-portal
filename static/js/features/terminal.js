(function(){
  'use strict';
  // ===== HELPERS =====
  // _safeJson provided by shared auth.js
  // _getToken: checks _portalChat first (chat IIFE may hold updated token)
  function _getToken() {
    return (window._portalChat && window._portalChat.getToken && window._portalChat.getToken()) || _tok();
  }

  // ===== STATE =====
  var termWs = null;
  var termReconnectTimer = null;
  var _termReconnectDelay = 2000;
  var claudeAlive = false;
  var statusInterval = null;
  var ctxInterval = null;
  var compactInterval = null;
  var wasCompacting = false;
  var _booted = false;
  var _activePaneId = null;  // currently selected tmux pane id
  var _paneList = [];        // cached pane list from /api/panes
  var _panePollingTimer = null;

  // ===== DOM REFS =====
  var termPaneContent = document.getElementById('teamsPaneContent');
  var teamsTabBar = document.getElementById('teamsTabBar');
  var ctxPill = document.querySelector('.ctx-pill');
  var ctxFill = document.querySelector('.ctx-fill');
  var onlineDot = document.querySelector('.online-dot');
  var onlinePill = document.querySelector('.online-pill');

  // ===== STRIP ANSI ESCAPE CODES =====
  function stripAnsi(text) {
    return text.replace(/\x1b\[[0-9;]*[a-zA-Z]/g, '').replace(/\x1b\][^\x07]*\x07/g, '');
  }

  // ===== SELECTION-SAFE PANE RENDERING =====
  // The pane is a read-only <div> that re-renders on every WS message / poll by
  // setting .textContent. Rewriting .textContent destroys and recreates the
  // text node, which CLEARS the user's active selection. That made it impossible
  // to select text and then copy it — by the time Ctrl+C / the Copy button ran,
  // a sub-second update had already wiped the selection. _renderPane() guards
  // against this: it skips updates while the user is actively selecting inside
  // the pane (buffering the latest content) and avoids needless churn when the
  // content hasn't changed.
  var _pendingPaneText = null; // latest content withheld during an active selection

  // True when there is a non-collapsed selection whose anchor or focus lives
  // inside the terminal pane.
  function _hasActiveSelectionInPane() {
    try {
      if (!termPaneContent) return false;
      var sel = window.getSelection ? window.getSelection()
              : (document.getSelection ? document.getSelection() : null);
      if (!sel || !sel.rangeCount || sel.isCollapsed) return false;
      if (sel.toString && !sel.toString()) return false;
      var a = sel.anchorNode, f = sel.focusNode;
      var inPane = (a && termPaneContent.contains && termPaneContent.contains(a)) ||
                   (f && termPaneContent.contains && termPaneContent.contains(f));
      return !!inPane;
    } catch (e) { return false; }
  }

  // Update the pane content without clobbering an in-progress selection.
  function _renderPane(text) {
    if (!termPaneContent) return;
    // Hold back updates while the user is selecting inside the pane; remember
    // the most recent content so we can flush it once the selection clears.
    if (_hasActiveSelectionInPane()) {
      _pendingPaneText = text;
      return;
    }
    if (termPaneContent.textContent === text) return; // no-op: avoid DOM churn
    termPaneContent.textContent = text;
    _pendingPaneText = null;
    termPaneContent.scrollTop = termPaneContent.scrollHeight;
  }

  // ===== LOAD PANE TABS FROM /api/panes =====
  var _lastPaneIds = '';
  function loadPaneTabs() {
    var token = _getToken();
    if (!token || !teamsTabBar) return;
    fetch('/api/panes', { headers: { 'Authorization': 'Bearer ' + token } })
    .then(function(r) { return _safeJson(r); })
    .then(function(data) {
      var panes = data.panes || [];
      if (!panes.length) return;
      // Skip DOM rebuild if pane list hasn't changed
      var newIds = panes.map(function(p) { return p.id; }).join(',');
      if (newIds === _lastPaneIds) return;
      _lastPaneIds = newIds;
      _paneList = panes;
      // Preserve active pane if still exists, else default to first
      var activeStillExists = panes.some(function(p) { return p.id === _activePaneId; });
      if (!_activePaneId || !activeStillExists) _activePaneId = panes[0].id;
      // Build tabs
      teamsTabBar.innerHTML = '';
      panes.forEach(function(p) {
        var btn = document.createElement('button');
        btn.className = 'teams-tab' + (p.id === _activePaneId ? ' active' : '');
        btn.setAttribute('data-pane', p.id);
        var civName = (window._portalChat && window._portalChat.getCivName()) || '';
        var label = p.title || p.target || p.id;
        // Clean up label: remove session prefix, show just the useful part
        if (label.indexOf(':') >= 0) {
          var parts = label.split(':');
          label = parts[parts.length - 1];
        }
        // Replace process names like "claude", "claude-code", "bash" with civ name for primary pane
        var labelLower = label.toLowerCase();
        if (labelLower === 'claude' || labelLower === 'claude-code' || labelLower === 'claude code' ||
            labelLower.match(/^%?\d+(\.\d+)?$/) || labelLower === 'bash' || labelLower === 'zsh') {
          label = civName || 'Primary';
        }
        btn.innerHTML = '<span class="pane-dot live"></span>' + escHtml(label);
        btn.addEventListener('click', function() {
          _activePaneId = p.id;
          teamsTabBar.querySelectorAll('.teams-tab').forEach(function(t) { t.classList.remove('active'); });
          btn.classList.add('active');
          if (termPaneContent) termPaneContent.textContent = 'Loading pane...';
          pollSelectedPane();
        });
        teamsTabBar.appendChild(btn);
      });
    })
    .catch(function() {});
  }

  // ===== POLL SELECTED PANE CONTENT =====
  function pollSelectedPane() {
    if (!_activePaneId || !termPaneContent) return;
    var token = _getToken();
    if (!token) return;
    // Use /api/panes to get content for the selected pane
    fetch('/api/panes', { headers: { 'Authorization': 'Bearer ' + token } })
    .then(function(r) { return _safeJson(r); })
    .then(function(data) {
      var panes = data.panes || [];
      var found = panes.find(function(p) { return p.id === _activePaneId; });
      if (found && found.content) {
        var clean = stripAnsi(found.content).replace(/\s+$/, '');
        _renderPane(clean);
      }
      // Update tab list if panes changed
      if (panes.length !== _paneList.length) {
        _paneList = panes;
        loadPaneTabs();
      }
    })
    .catch(function() {});
  }

  // ===== TERMINAL WEBSOCKET =====
  function connectTerminalWS() {
    var token = _getToken();
    if (!token || !termPaneContent) return;
    if (termWs) { try { termWs.close(); } catch(e) {} }

    // Load pane tabs on first connect
    loadPaneTabs();

    var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    var ws = new WebSocket(proto + '//' + location.host + '/ws/terminal?token=' + encodeURIComponent(token));
    termWs = ws;

    ws.onopen = function() {
      _termReconnectDelay = 2000;
      if (termReconnectTimer) { clearTimeout(termReconnectTimer); termReconnectTimer = null; }
      // Start polling pane tabs + selected pane content
      if (_panePollingTimer) clearInterval(_panePollingTimer);
      _panePollingTimer = setInterval(function() {
        // Always re-check for new/removed panes (discovers team agents)
        loadPaneTabs();
        // If viewing a non-primary pane, poll its content
        if (_activePaneId && _paneList.length > 1) {
          var primaryPaneId = _paneList.length > 0 ? _paneList[0].id : null;
          if (_activePaneId !== primaryPaneId) {
            pollSelectedPane();
          }
        }
      }, 3000);
    };

    ws.onmessage = function(e) {
      // WS streams the primary pane — only update if viewing primary
      var primaryPaneId = _paneList.length > 0 ? _paneList[0].id : null;
      if (_activePaneId && primaryPaneId && _activePaneId !== primaryPaneId) return;
      var raw = e.data || '';
      var clean = stripAnsi(raw).replace(/\s+$/, '');
      if (!clean) return;
      // Trim to the last ~5000 lines to bound the DOM size.
      var nlCount = 0;
      for (var ci = clean.length - 1; ci >= 0; ci--) {
        if (clean[ci] === '\n') nlCount++;
        if (nlCount > 5000) { clean = clean.slice(ci + 1); break; }
      }
      _renderPane(clean);
    };

    ws.onclose = function(evt) {
      termWs = null;
      if (_panePollingTimer) { clearInterval(_panePollingTimer); _panePollingTimer = null; }
      if (evt && (evt.code === 4401 || evt.code === 1008 || evt.code === 4001)) return;
      termReconnectTimer = setTimeout(function() {
        _termReconnectDelay = Math.min(_termReconnectDelay * 2, 30000);
        connectTerminalWS();
      }, _termReconnectDelay);
    };

    ws.onerror = function() {};
  }

  // ===== CONTEXT MONITORING =====
  function updateCtxPill() {
    if (document.hidden) return;
    var token = _getToken();
    if (!token) return;
    portalFetch('/api/context')
      .then(function(r) { return _safeJson(r); })
      .then(function(d) {
        if (d.error || d.pct == null) return;
        var pct = d.pct;
        var totalK = Math.round((d.total_tokens || 0) / 1000);
        var maxTokens = d.max_tokens || 870000;
        var maxK = Math.round(maxTokens / 1000);
        var maxLabel = maxK >= 1000 ? (maxK / 1000) + 'M' : maxK + 'K';

        // Update fill bar width
        if (ctxFill) ctxFill.style.width = pct + '%';

        // Update fill bar color based on severity
        if (ctxFill) {
          ctxFill.style.background = pct >= 95 ? '#ef4444' : pct >= 80 ? '#ef4444' : pct >= 60 ? '#f59e0b' : '';
          if (pct >= 95) {
            ctxFill.style.animation = 'ctxFlash 0.5s ease-in-out infinite alternate';
          } else {
            ctxFill.style.animation = '';
          }
        }

        // Update pill text: replace the text node after the track div
        if (ctxPill) {
          // The ctx-pill structure is: "CTX " + <div.ctx-track> + " 146k / 1M"
          // We need to update the trailing text
          var children = ctxPill.childNodes;
          for (var i = children.length - 1; i >= 0; i--) {
            if (children[i].nodeType === 3 && children[i].textContent.trim()) {
              children[i].textContent = ' ' + totalK + 'k / ' + maxLabel;
              break;
            }
          }
        }

        // Update tooltip
        if (ctxPill) ctxPill.title = 'Context: ' + pct + '% — input: ' + Math.round((d.input_tokens||0)/1000) + 'k, cache_read: ' + Math.round((d.cache_read||0)/1000) + 'k, cache_new: ' + Math.round((d.cache_creation||0)/1000) + 'k';
      })
      .catch(function() {});
  }

  // ===== SYSTEM STATUS =====
  function updateOnlineStatus() {
    if (document.hidden) return;
    var token = _getToken();
    if (!token) return;
    portalFetch('/api/status')
      .then(function(r) { return _safeJson(r); })
      .then(function(d) {
        claudeAlive = !!d.claude_running;
        var tmuxOk = !!d.tmux_alive;
        if (d.tmux_session) window._tmuxSession = d.tmux_session;
        var isOnline = claudeAlive && tmuxOk;

        // Update online pill
        if (onlineDot) {
          onlineDot.style.background = isOnline ? '' : '#ef4444';
          onlineDot.style.boxShadow = isOnline ? '' : '0 0 6px rgba(239,68,68,0.5)';
        }
        if (onlinePill) {
          // Update text (preserve the dot element)
          var existingDot = onlinePill.querySelector('.online-dot');
          onlinePill.textContent = '';
          if (existingDot) onlinePill.appendChild(existingDot);
          onlinePill.appendChild(document.createTextNode(' ' + (isOnline ? 'Online' : claudeAlive ? 'Online' : 'Offline')));
          onlinePill.style.color = isOnline ? '' : '#ef4444';
        }
      })
      .catch(function() {
        claudeAlive = false;
        if (onlineDot) {
          onlineDot.style.background = '#ef4444';
          onlineDot.style.boxShadow = '0 0 6px rgba(239,68,68,0.5)';
        }
        if (onlinePill) {
          var existingDot = onlinePill.querySelector('.online-dot');
          onlinePill.textContent = '';
          if (existingDot) onlinePill.appendChild(existingDot);
          onlinePill.appendChild(document.createTextNode(' Offline'));
          onlinePill.style.color = '#ef4444';
        }
      });
  }

  // ===== COMPACT DETECTION =====
  function pollCompactStatus() {
    if (document.hidden) return;
    var token = _getToken();
    if (!token) return;
    portalFetch('/api/compact/status')
      .then(function(r) { return _safeJson(r); })
      .then(function(d) {
        var banner = document.getElementById('compact-banner');
        if (!banner) return;
        if (d.compacting) {
          banner.classList.add('active');
          wasCompacting = true;
        } else {
          banner.classList.remove('active');
          if (wasCompacting) {
            wasCompacting = false;
            setTimeout(function() { updateCtxPill(); }, 2000);
          }
        }
      }).catch(function() {});
  }

  // ===== REFRESH TEAMS PANES (wire up the existing button) =====
  window.refreshTeamsPanes = function() {
    // Reload pane tabs and reconnect terminal WS
    loadPaneTabs();
    if (termWs) { try { termWs.close(); } catch(e) {} }
    termWs = null;
    if (termPaneContent) termPaneContent.textContent = 'Reconnecting...';
    connectTerminalWS();
    showToast('Terminal refreshing...');
  };

  // ===== BOOT ALL MONITORING =====
  function bootMonitoring() {
    if (_booted) return;
    _booted = true;

    // Connect terminal WebSocket
    connectTerminalWS();

    // Initial polls
    updateCtxPill();
    updateOnlineStatus();

    // Set up polling intervals
    ctxInterval = setInterval(updateCtxPill, 30000);         // Context every 30s
    statusInterval = setInterval(updateOnlineStatus, 60000); // Status every 60s
    compactInterval = setInterval(pollCompactStatus, 10000); // Compact every 10s

    // Expose stop function for 401 cleanup
    window._stopMonitoring = function() {
      if (ctxInterval) { clearInterval(ctxInterval); ctxInterval = null; }
      if (statusInterval) { clearInterval(statusInterval); statusInterval = null; }
      if (compactInterval) { clearInterval(compactInterval); compactInterval = null; }
      if (termReconnectTimer) { clearTimeout(termReconnectTimer); termReconnectTimer = null; }
      if (_panePollingTimer) { clearInterval(_panePollingTimer); _panePollingTimer = null; }
      if (termWs) { try { termWs.close(); } catch(e) {} termWs = null; }
      _booted = false;
    };

    // Reconnect terminal on visibility change with backoff (e.g. returning from background tab / mobile)
    var _termVisReconnectDelay = 1000;
    document.addEventListener('visibilitychange', function() {
      if (!document.hidden && (!termWs || termWs.readyState !== 1)) {
        if (termReconnectTimer) { clearTimeout(termReconnectTimer); termReconnectTimer = null; }
        termReconnectTimer = setTimeout(function() {
          connectTerminalWS();
          _termVisReconnectDelay = Math.min(_termVisReconnectDelay * 2, 30000);
        }, _termVisReconnectDelay);
      } else if (!document.hidden && termWs && termWs.readyState === 1) {
        _termVisReconnectDelay = 1000; // Reset on successful connection
      }
    });
  }

  // ===== COPY / PASTE =====
  // The terminal pane is a read-only <div>. There are no buttons — copy and
  // paste are keyboard + native-selection only, by design.
  //
  // COPY: handled NATIVELY by the browser. Selecting text and pressing Ctrl+C
  // copies it with zero custom code — the only thing that ever broke this was
  // the live re-render clobbering the user's selection (see _renderPane above,
  // which now preserves it). So there is intentionally no custom copy handler.
  //
  // PASTE: there is no live PTY in the browser, so a native paste has nowhere
  // to go. We keep a custom Ctrl+V that reads the clipboard and injects it into
  // the active tmux pane via /api/inject/pane (the same path the composer uses),
  // e.g. to paste a Claude auth code back into the session.

  // Read the clipboard and inject it into the active pane (no auto-Enter so the
  // user can review before submitting — matches pasting an auth code).
  function termPaste() {
    if (!navigator.clipboard || !navigator.clipboard.readText) {
      if (typeof showToast === 'function') showToast('Clipboard needs HTTPS', 'error');
      return Promise.resolve();
    }
    var token = _getToken();
    var paneId = _activePaneId;
    return navigator.clipboard.readText().then(function (text) {
      if (!text) {
        if (typeof showToast === 'function') showToast('Clipboard is empty');
        return;
      }
      if (!paneId) {
        if (typeof showToast === 'function') showToast('No pane selected', 'error');
        return;
      }
      return fetch('/api/inject/pane', {
        method: 'POST',
        headers: { 'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json' },
        body: JSON.stringify({ pane_id: paneId, message: text })
      }).then(function () {
        if (typeof showToast === 'function') showToast('Pasted to terminal');
      });
    }).catch(function () {
      if (typeof showToast === 'function') showToast('Paste failed', 'error');
    });
  }

  // Keyboard shortcut: Ctrl+V pastes the clipboard into the active pane.
  // Copy is intentionally NOT handled here — native browser copy (Ctrl+C on the
  // user's selection) does the right thing now that re-renders no longer clobber
  // the selection. The div stays focusable so Ctrl+V reaches this handler.
  if (termPaneContent) {
    termPaneContent.setAttribute('tabindex', '0'); // make the div focusable for key events
    termPaneContent.addEventListener('keydown', function (e) {
      var ctrl = e.ctrlKey || e.metaKey;
      if (!ctrl) return;
      var k = (e.key || '').toLowerCase();
      if (k === 'v') {
        e.preventDefault();
        termPaste();
      }
      // Ctrl+C falls through to the browser's native copy (no preventDefault).
    });
  }

  // Expose handlers for unit tests (no UI buttons — keyboard + native only).
  window._portalTerminalClipboard = {
    paste: termPaste,
    getActivePaneId: function () { return _activePaneId; },
    setActivePaneId: function (id) { _activePaneId = id; },
    // Exposed for tests: selection-safe pane renderer + selection probe.
    renderPane: _renderPane,
    hasActiveSelectionInPane: _hasActiveSelectionInPane
  };

  // ===== AUTO-BOOT =====
  // If a token is already set (from the chat IIFE auto-auth), boot immediately
  // Otherwise, listen for the _portalChat to be ready
  function tryBoot() {
    if (_getToken()) {
      bootMonitoring();
    } else {
      // Retry after a short delay (chat IIFE may still be authenticating)
      setTimeout(function() {
        if (_getToken()) bootMonitoring();
      }, 2000);
    }
  }
  tryBoot();

  // ===== RESTART BUTTON =====
  // Handler is inline onclick on the button element (works even if this module fails to load).
  // No duplicate addEventListener needed here.

  // Expose for external use
  window._portalMonitor = {
    refreshTerminal: window.refreshTeamsPanes,
    updateContext: updateCtxPill,
    updateStatus: updateOnlineStatus,
    isClaudeAlive: function() { return claudeAlive; }
  };
})();
