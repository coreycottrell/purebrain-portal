(function(){
  'use strict';
  // Route CC API calls through portal proxy to avoid CORS issues
  var CC_BASE = '/api/cc/proxy';
  var CC_KEY = '';
  var CIV_NAME = '';
  // Auto-detect CIV_NAME from server (identity file) instead of hardcoding
  fetch('/health').then(function(r){return r.json();}).then(function(d){
    CIV_NAME = d.civ || '';
  }).catch(function(){});
  var POLL_INTERVAL = 10000; // default — overridden by _getCCPollInterval()
  function _getCCPollInterval() {
    return parseInt((window._portalPrefs && window._portalPrefs.cc_poll_interval) || '10000', 10);
  }
  var pollTimer = null;
  var sinceId = 0;
  var channels = {};
  var ccMsgContainer = document.getElementById('ccMessages');
  var ccFilter = document.getElementById('ccChannelFilter');
  var ccStatus = document.getElementById('ccStatus');
  var ccBadge = document.getElementById('ccBadge');
  var unreadCount = 0;
  var activeFilter = 'all';
  var isActive = false;
  var _ccSettingsLoaded = false;

  // Only auto-scroll if user is near the bottom (not reading history)
  function _isNearBottom() {
    if (!ccMsgContainer) return true;
    var threshold = 80;
    return ccMsgContainer.scrollHeight - ccMsgContainer.scrollTop - ccMsgContainer.clientHeight < threshold;
  }
  function _scrollToBottomIfNear() {
    if (_isNearBottom()) ccMsgContainer.scrollTop = ccMsgContainer.scrollHeight;
  }

  // Load CC config from server-side settings (works across devices)
  function _loadCCSettings(callback) {
    var tok = localStorage.getItem('portal_token') || '';
    if (!tok) { if (callback) callback(); return; }
    fetch('/api/settings', { headers: { 'Authorization': 'Bearer ' + tok } })
      .then(function(r) { return r.ok ? r.json() : {}; })
      .then(function(s) {
        CC_KEY = s.cc_civ_key || localStorage.getItem('cc_civ_key') || '';
        sinceId = parseInt(s.cc_since_id || localStorage.getItem('cc_since_id') || '0', 10);
        _ccSettingsLoaded = true;
        // Migrate localStorage key to server if needed
        if (CC_KEY && !s.cc_civ_key) { _saveCCSettings(); }
        // Restore cached messages (skip entries without timestamps)
        if (s.cc_cached_messages && ccMsgContainer) {
          try {
            var msgs = typeof s.cc_cached_messages === 'string' ? JSON.parse(s.cc_cached_messages) : s.cc_cached_messages;
            if (Array.isArray(msgs) && msgs.length > 0) {
              var now = Date.now();
              var CACHE_TTL_MS = 24 * 60 * 60 * 1000; // 24 hours
              var validMsgs = msgs.filter(function(msg) {
                if (!msg.created_at) return false;
                var msgTime = new Date(msg.created_at).getTime();
                return !isNaN(msgTime) && (now - msgTime) <= CACHE_TTL_MS;
              });
              if (validMsgs.length > 0) {
                ccMsgContainer.innerHTML = '';
                validMsgs.forEach(function(msg) {
                  var wrapper = document.createElement('div');
                  wrapper.innerHTML = renderCCMessage(msg);
                  ccMsgContainer.appendChild(wrapper.firstChild);
                });
                ccMsgContainer.scrollTop = ccMsgContainer.scrollHeight;
              }
            }
          } catch(e) { /* ignore corrupt cache */ }
        }
        // Show CC tab if key exists
        if (CC_KEY) {
          var ccTab = document.getElementById('ccSubtab');
          if (ccTab) ccTab.style.display = '';
        }
        if (callback) callback();
      })
      .catch(function() { if (callback) callback(); });
  }

  // Save CC state to server-side settings
  function _saveCCSettings(extra) {
    var tok = localStorage.getItem('portal_token') || '';
    if (!tok) return;
    var payload = { cc_civ_key: CC_KEY, cc_since_id: String(sinceId) };
    if (extra) { for (var k in extra) payload[k] = extra[k]; }
    fetch('/api/settings', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer ' + tok, 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    }).catch(function() {});
  }

  // ccEsc: alias for global escHtml
  var ccEsc = escHtml;

  function ccHeaders() {
    // Use portal auth for proxy requests (proxy adds CIV key server-side)
    var tok = localStorage.getItem('portal_token') || '';
    return { 'Authorization': 'Bearer ' + tok, 'Content-Type': 'application/json' };
  }

  function formatCCTime(iso) {
    if (!iso) return '';
    var d = new Date(iso);
    if (isNaN(d.getTime())) return '';
    var now = new Date();
    var isToday = d.toDateString() === now.toDateString();
    var time = d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }).toLowerCase();
    return isToday ? time : d.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ' ' + time;
  }

  function renderCCBody(text) {
    // Use shared markdown renderer from chat.js, then highlight @mentions
    var md = window._portalChat && window._portalChat.renderMarkdown
      ? window._portalChat.renderMarkdown(text)
      : ccEsc(text).replace(/\n/g, '<br>');
    // Highlight @mentions (case-insensitive, any CIV name)
    md = md.replace(/@(\w+)/g, '<span class="cc-mention">@$1</span>');
    return md;
  }

  function getChannelName(channelId, channelName) {
    if (channelName && channelName !== 'unknown') return '#' + channelName;
    return channels[channelId] ? '#' + channels[channelId] : '#ch-' + channelId;
  }

  function renderCCMessage(msg) {
    var chName = getChannelName(msg.channel_id, msg.channel_name);
    var avatarClass = msg.sender_type === 'human' ? 'human' : 'ai';
    var initial = (msg.sender_name || '?').charAt(0).toUpperCase();
    var html = '<div class="cc-msg" data-cc-id="' + msg.id + '" data-channel="' + msg.channel_id + '" data-created="' + ccEsc(msg.created_at || '') + '">' +
      '<div class="cc-msg-avatar ' + avatarClass + '">' + ccEsc(initial) + '</div>' +
      '<div class="cc-msg-body">' +
        '<div class="cc-msg-header">' +
          '<span class="cc-channel-pill">' + ccEsc(chName) + '</span>' +
          '<span class="cc-msg-sender">' + ccEsc(msg.sender_name) + '</span>' +
          '<span class="cc-msg-time">' + formatCCTime(msg.created_at) + '</span>' +
        '</div>' +
        '<div class="cc-msg-text">' + renderCCBody(msg.body) + '</div>' +
        '<button class="cc-reply-btn" onclick="window._ccReply(' + msg.channel_id + ',' + msg.id + ',this)">Reply</button>' +
        '<div class="cc-reply-area" id="cc-reply-' + msg.id + '"></div>' +
      '</div>' +
    '</div>';
    return html;
  }

  function loadChannels() {
    fetch(CC_BASE + '/api/chat/channels', { headers: ccHeaders() })
      .then(function(r) { if (!r.ok) throw new Error('CC ' + r.status); return r.json(); })
      .then(function(data) {
        var arr = Array.isArray(data) ? data : (data.channels || []);
        arr.forEach(function(ch) { channels[ch.id] = ch.name; });
        // Populate filter dropdown
        if (ccFilter) {
          var opts = '<option value="all">All Channels</option>';
          arr.forEach(function(ch) {
            opts += '<option value="' + ch.id + '">' + ccEsc('#' + ch.name) + '</option>';
          });
          ccFilter.innerHTML = opts;
        }
      })
      .catch(function(e) { console.warn('[CC] channels error:', e.message); });
  }

  function pollMessages() {
    CC_KEY = localStorage.getItem('cc_civ_key') || '';
    if (!CC_KEY) return;  // Don't poll without a key
    fetch(CC_BASE + '/api/chat/messages/since?since_id=' + sinceId + '&limit=50', { headers: ccHeaders() })
      .then(function(r) { if (!r.ok) throw new Error('CC ' + r.status); return r.json(); })
      .then(function(data) {
        var msgs = Array.isArray(data) ? data : (data.messages || []);
        if (msgs.length === 0) return;
        // Remove empty placeholder
        var empty = ccMsgContainer.querySelector('.cc-empty');
        if (empty) empty.remove();
        var newRendered = 0;
        var mentionCount = 0;
        // Capture scroll position BEFORE appending new messages
        var wasNearBottom = _isNearBottom();
        // Render new messages
        msgs.forEach(function(msg) {
          // Always advance cursor past every message
          if (msg.id > sinceId) sinceId = msg.id;
          // Skip if already rendered
          if (ccMsgContainer.querySelector('[data-cc-id="' + msg.id + '"]')) return;
          var wrapper = document.createElement('div');
          wrapper.innerHTML = renderCCMessage(msg);
          var el = wrapper.firstChild;
          // Apply channel filter
          if (activeFilter !== 'all' && String(msg.channel_id) !== activeFilter) {
            el.style.display = 'none';
          }
          ccMsgContainer.appendChild(el);
          newRendered++;
          // Detect @mentions and DMs for notification
          if (CIV_NAME && msg.body && msg.body.toLowerCase().indexOf('@' + CIV_NAME.toLowerCase()) !== -1) {
            mentionCount++;
          }
          // Detect DM channels (named dm_xxx_yyy) where we are a participant
          var chName = getChannelName(msg.channel_id, msg.channel_name);
          if (chName && chName.toLowerCase().indexOf('dm_') === 0 && (!msg.agent_name || msg.agent_name.toLowerCase() !== (CIV_NAME||'').toLowerCase())) {
            mentionCount++; // DMs count as mentions for notification purposes
          }
        });
        // Push CC mention notification (fixed: was referencing undefined 'newMsgs')
        if (mentionCount > 0 && window._pushNotification) {
          // Build preview from the actual mention messages
          var mentionMsgs = msgs.filter(function(m) {
            return CIV_NAME && m.body && m.body.toLowerCase().indexOf('@' + CIV_NAME.toLowerCase()) !== -1;
          });
          var body = '';
          if (mentionMsgs.length > 0) {
            var preview = mentionMsgs.slice(0, 2);
            var lines = preview.map(function(m) {
              var from = m.agent_name || m.from || 'Someone';
              var text = (m.body || '').substring(0, 60);
              if ((m.body || '').length > 60) text += '...';
              return from + ': ' + text;
            });
            body = lines.join('\n');
            if (mentionMsgs.length > 2) body += '\n+' + (mentionMsgs.length - 2) + ' more';
          } else {
            body = mentionCount + ' new @' + CIV_NAME + ' mention' + (mentionCount > 1 ? 's' : '');
          }
          window._pushNotification(
            'CC Mention' + (mentionCount > 1 ? 's' : ''),
            body,
            'cc',
            'chat:cc'
          );
        }
        // CC notifications are @mentions ONLY — general chatter should not notify
        // DMs are handled by the CC bridge (tmux injection), not here
        if (false && !isActive && newRendered > 0 && window._pushNotification && mentionCount === 0) {
          var otherMsgs = msgs.filter(function(m) {
            return !CIV_NAME || !m.agent_name || m.agent_name.toLowerCase() !== CIV_NAME.toLowerCase();
          });
          if (otherMsgs.length > 0) {
            var lastMsg = otherMsgs[otherMsgs.length - 1];
            var ccFrom = lastMsg.agent_name || lastMsg.from || 'Someone';
            var ccText = (lastMsg.body || '').substring(0, 80);
            if ((lastMsg.body || '').length > 80) ccText += '...';
            var ccBody = ccFrom + ': ' + ccText;
            if (otherMsgs.length > 1) ccBody += '\n+' + (otherMsgs.length - 1) + ' more';
            window._pushNotification(
              newRendered + ' new CC message' + (newRendered > 1 ? 's' : ''),
              ccBody,
              'cc',
              'chat:cc'
            );
          }
        }
        // Cap DOM at 200 messages
        var domMsgs = ccMsgContainer.querySelectorAll('.cc-msg');
        if (domMsgs.length > 200) {
          var excess = domMsgs.length - 200;
          for (var i = 0; i < excess; i++) domMsgs[i].remove();
        }
        // Update unread badge only for truly new rendered messages
        if (!isActive && newRendered > 0) {
          unreadCount += newRendered;
          if (ccBadge) ccBadge.textContent = unreadCount > 99 ? '99+' : String(unreadCount);
        }
        // Persist sinceId + cache recent messages (last 200)
        try {
          _saveCCSettings({ cc_cached_messages: _collectCacheMessages() });
        } catch(e) { /* ignore */ }
        // Only auto-scroll if user was near the bottom before new messages arrived
        if (wasNearBottom) ccMsgContainer.scrollTop = ccMsgContainer.scrollHeight;
        if (ccStatus) ccStatus.textContent = 'Connected';
      })
      .catch(function(e) {
        if (ccStatus) ccStatus.textContent = 'Error: ' + e.message;
        console.warn('[CC] poll error:', e.message);
      });
  }

  function _collectCacheMessages() {
    var allMsgs = [];
    ccMsgContainer.querySelectorAll('.cc-msg').forEach(function(el) {
      var createdAt = el.getAttribute('data-created') || '';
      if (!createdAt) return;
      allMsgs.push({
        id: parseInt(el.getAttribute('data-cc-id')),
        channel_id: parseInt(el.getAttribute('data-channel')),
        sender_name: el.querySelector('.cc-msg-sender') ? el.querySelector('.cc-msg-sender').textContent : '',
        sender_type: el.querySelector('.cc-msg-avatar') && el.querySelector('.cc-msg-avatar').classList.contains('human') ? 'human' : 'ai',
        body: el.querySelector('.cc-msg-text') ? el.querySelector('.cc-msg-text').textContent : '',
        created_at: createdAt
      });
    });
    if (allMsgs.length > 200) allMsgs = allMsgs.slice(-200);
    return allMsgs;
  }

  function loadHistory() {
    if (!CC_KEY) return;
    fetch(CC_BASE + '/api/chat/messages/since?since_id=0&limit=200', { headers: ccHeaders() })
      .then(function(r) { if (!r.ok) throw new Error('CC ' + r.status); return r.json(); })
      .then(function(data) {
        var msgs = Array.isArray(data) ? data : (data.messages || []);
        if (msgs.length === 0) return;
        var empty = ccMsgContainer.querySelector('.cc-empty');
        if (empty) empty.remove();
        msgs.forEach(function(msg) {
          if (msg.id > sinceId) sinceId = msg.id;
          if (ccMsgContainer.querySelector('[data-cc-id="' + msg.id + '"]')) return;
          var wrapper = document.createElement('div');
          wrapper.innerHTML = renderCCMessage(msg);
          var el = wrapper.firstChild;
          if (activeFilter !== 'all' && String(msg.channel_id) !== activeFilter) {
            el.style.display = 'none';
          }
          ccMsgContainer.appendChild(el);
        });
        try { _saveCCSettings({ cc_cached_messages: _collectCacheMessages() }); } catch(e) {}
        ccMsgContainer.scrollTop = ccMsgContainer.scrollHeight;
        if (ccStatus) ccStatus.textContent = 'Connected';
      })
      .catch(function(e) {
        if (ccStatus) ccStatus.textContent = 'Error: ' + e.message;
        console.warn('[CC] history error:', e.message);
      });
  }

  function startPolling() {
    isActive = true;
    // Clear unread
    unreadCount = 0;
    if (ccBadge) ccBadge.textContent = '';
    // Re-read key from localStorage in case it was just saved
    CC_KEY = localStorage.getItem('cc_civ_key') || '';
    if (!CC_KEY) {
      if (ccMsgContainer) ccMsgContainer.innerHTML = '<div class="cc-empty" style="padding:32px;text-align:center;color:var(--pb-text-dim);font-size:13px;line-height:1.8;">CC key not configured.<br>Go to <strong>Settings → Preferences → CC Integration</strong> to enter your CIV key.</div>';
      if (ccStatus) ccStatus.textContent = 'Not configured';
      return;
    }
    if (!pollTimer) {
      loadChannels();
      // Load full history if container is empty, otherwise poll for new
      if (!ccMsgContainer || !ccMsgContainer.querySelector('.cc-msg')) {
        loadHistory();
      } else {
        pollMessages();
      }
      pollTimer = setInterval(pollMessages, _getCCPollInterval());
    }
  }

  function stopPolling() {
    isActive = false;
    // Keep polling in background at slower rate for badge updates
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    // Background poll every 60s
    if (!window._ccBgTimer) {
      window._ccBgTimer = setInterval(pollMessages, 60000);
    }
  }

  // Full stop: clear both active and background timers (called on 401)
  window._ccFullStop = function() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    if (window._ccBgTimer) { clearInterval(window._ccBgTimer); window._ccBgTimer = null; }
    isActive = false;
  };

  // Reply handler
  window._ccReply = function(channelId, msgId, btn) {
    var replyArea = document.getElementById('cc-reply-' + msgId);
    if (!replyArea) return;
    // Toggle — if already open, close it
    if (replyArea.innerHTML) { replyArea.innerHTML = ''; return; }
    replyArea.innerHTML = '<div class="cc-reply-composer">' +
      '<input type="text" placeholder="Type your reply..." id="cc-reply-input-' + msgId + '" onkeydown="if(event.key===\'Enter\')window._ccSendReply(' + channelId + ',' + msgId + ')">' +
      '<button onclick="window._ccSendReply(' + channelId + ',' + msgId + ')">Send</button>' +
      '</div>';
    var input = document.getElementById('cc-reply-input-' + msgId);
    if (input) input.focus();
  };

  window._ccSendReply = function(channelId, msgId) {
    var input = document.getElementById('cc-reply-input-' + msgId);
    if (!input) return;
    var text = input.value.trim();
    if (!text) return;
    input.disabled = true;
    fetch(CC_BASE + '/api/chat/channels/' + channelId + '/messages', {
      method: 'POST',
      headers: ccHeaders(),
      body: JSON.stringify({ body: text })
    })
    .then(function(r) { if (!r.ok) throw new Error('CC send ' + r.status); return r.json(); })
    .then(function() {
      var replyArea = document.getElementById('cc-reply-' + msgId);
      if (replyArea) replyArea.innerHTML = '';
      // Immediately poll for the new message
      pollMessages();
    })
    .catch(function(e) {
      input.disabled = false;
      if (typeof showToast === 'function') showToast('CC send error: ' + e.message);
    });
  };

  window._ccFilterChannel = function(val) {
    activeFilter = val;
    var msgs = ccMsgContainer.querySelectorAll('.cc-msg');
    msgs.forEach(function(m) {
      if (val === 'all') { m.style.display = ''; }
      else { m.style.display = m.getAttribute('data-channel') === val ? '' : 'none'; }
    });
  };

  window._ccStartPolling = startPolling;
  window._ccStopPolling = stopPolling;

  // Toggle online/offline
  var ccPaused = false;
  var ccToggleBtn = document.getElementById('ccToggleBtn');

  function _updateToggleUI() {
    if (!ccToggleBtn) return;
    if (ccPaused) {
      ccToggleBtn.textContent = 'Offline';
      ccToggleBtn.style.background = 'var(--pb-text-dim)';
      if (ccStatus) ccStatus.textContent = 'Paused';
    } else {
      ccToggleBtn.textContent = 'Online';
      ccToggleBtn.style.background = 'var(--pb-green)';
    }
  }

  window._ccToggle = function() {
    ccPaused = !ccPaused;
    _updateToggleUI();
    // Save pause state to server
    _saveCCSettings({ cc_paused: ccPaused });
    if (ccPaused) {
      // Stop all polling
      if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
      if (window._ccBgTimer) { clearInterval(window._ccBgTimer); window._ccBgTimer = null; }
      // Send offline heartbeat to CC so we show as offline immediately
      if (CC_KEY) {
        fetch(CC_BASE + '/api/chat/presence/civ-heartbeat', {
          method: 'POST', headers: ccHeaders(),
          body: JSON.stringify({ user_type: 'ai', status: 'offline' })
        }).catch(function(){});
      }
      if (typeof showToast === 'function') showToast('CC bridge paused — you are now offline');
    } else {
      // Resume — send online heartbeat immediately
      if (CC_KEY) {
        fetch(CC_BASE + '/api/chat/presence/civ-heartbeat', {
          method: 'POST', headers: ccHeaders(),
          body: JSON.stringify({ user_type: 'ai', status: 'online' })
        }).catch(function(){});
      }
      loadChannels();
      pollMessages();
      if (window._ccBgTimer) { clearInterval(window._ccBgTimer); window._ccBgTimer = null; }
      window._ccBgTimer = setInterval(pollMessages, 60000);
      if (typeof showToast === 'function') showToast('CC bridge resumed — you are online');
    }
  };

  // Check CC bridge availability before showing the tab
  function _checkCCBridge(callback) {
    var tok = localStorage.getItem('portal_token') || '';
    if (!tok) { if (callback) callback(false); return; }
    fetch('/api/cc/status', { headers: { 'Authorization': 'Bearer ' + tok } })
      .then(function(r) { return r.ok ? r.json() : { available: false }; })
      .then(function(d) { if (callback) callback(!!d.available); })
      .catch(function() { if (callback) callback(false); });
  }

  // Load CC settings from server, then start if key exists and not paused
  _loadCCSettings(function() {
    if (CC_KEY) {
      _checkCCBridge(function(bridgeUp) {
        if (!bridgeUp) {
          // Bridge unreachable — hide tab, show message if user navigates here
          var ccTab = document.getElementById('ccSubtab');
          if (ccTab) ccTab.style.display = 'none';
          if (ccMsgContainer) ccMsgContainer.innerHTML = '<div class="cc-empty" style="padding:32px;text-align:center;color:var(--pb-text-dim);font-size:13px;line-height:1.8;">CC Bridge not connected.<br>The Command Center is not reachable right now.</div>';
          return;
        }
        // Bridge available — proceed normally
        var tok = localStorage.getItem('portal_token') || '';
        fetch('/api/settings', { headers: { 'Authorization': 'Bearer ' + tok } })
          .then(function(r) { return r.ok ? r.json() : {}; })
          .then(function(s) {
            ccPaused = s.cc_paused === true;
            _updateToggleUI();
            if (!ccPaused) {
              loadChannels();
              setTimeout(function() {
                var hasMessages = ccMsgContainer && ccMsgContainer.querySelector('.cc-msg');
                if (!hasMessages) { loadHistory(); } else { pollMessages(); }
                if (window._ccBgTimer) { clearInterval(window._ccBgTimer); window._ccBgTimer = null; }
                window._ccBgTimer = setInterval(pollMessages, 60000);
              }, 3000);
            }
          })
          .catch(function() {
            loadChannels();
            setTimeout(function() {
              var hasMessages = ccMsgContainer && ccMsgContainer.querySelector('.cc-msg');
              if (!hasMessages) { loadHistory(); } else { pollMessages(); }
              if (window._ccBgTimer) { clearInterval(window._ccBgTimer); window._ccBgTimer = null; }
              window._ccBgTimer = setInterval(pollMessages, 60000);
            }, 3000);
          });
      });
    }
  });
  // Re-init after login (token may not exist when script first loads)
  window.addEventListener('portal-auth', function() {
    setTimeout(function() {
      _loadCCSettings(function() {
        if (CC_KEY) {
          _checkCCBridge(function(bridgeUp) {
            var ccTab = document.getElementById('ccSubtab');
            if (!bridgeUp) {
              if (ccTab) ccTab.style.display = 'none';
              return;
            }
            if (ccTab) ccTab.style.display = '';
            loadChannels();
            var hasMessages = ccMsgContainer && ccMsgContainer.querySelector('.cc-msg');
            if (!hasMessages) { loadHistory(); } else { pollMessages(); }
            if (window._ccBgTimer) { clearInterval(window._ccBgTimer); window._ccBgTimer = null; }
            window._ccBgTimer = setInterval(pollMessages, 60000);
          });
        }
      });
    }, 500);
  });
  // Listen for key being saved in Settings → save to server + start polling
  window.addEventListener('cc-key-saved', function() {
    CC_KEY = localStorage.getItem('cc_civ_key') || '';
    if (CC_KEY) {
      _saveCCSettings();  // persist to server
      var ccTab = document.getElementById('ccSubtab');
      if (ccTab) ccTab.style.display = '';
      loadChannels();
      loadHistory();
      if (window._ccBgTimer) { clearInterval(window._ccBgTimer); window._ccBgTimer = null; }
      window._ccBgTimer = setInterval(pollMessages, 60000);
    }
  });
})();
