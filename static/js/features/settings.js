(function(){
  'use strict';

  // Auth helpers provided by shared auth.js (_tok, _auth, _authJson)

  var _settingsLoaded = false;

  // Shared prefs object — read by chat.js, inbox.js, cc-chat.js, clock widget
  window._portalPrefs = {};

  // -----------------------------------------------------------------------
  // CC Presence — single source of truth for "is CC available?"
  // -----------------------------------------------------------------------
  window._ccAvailable = false;

  function _checkCCAvailability() {
    if (!_tok()) return;
    fetch('/api/cc/status', { headers: _auth() })
      .then(function(r) { return r.ok ? r.json() : { available: false, bridge_loaded: false }; })
      .then(function(d) {
        window._ccAvailable = !!(d.available && d.bridge_loaded);
        _applyCCVisibility();
      })
      .catch(function() {
        window._ccAvailable = false;
        _applyCCVisibility();
      });
  }

  function _applyCCVisibility() {
    var els = document.querySelectorAll('.cc-dependent');
    var show = window._ccAvailable;
    for (var i = 0; i < els.length; i++) {
      els[i].style.display = show ? '' : 'none';
    }
  }

  // -----------------------------------------------------------------------
  // Notification System
  // -----------------------------------------------------------------------
  var _notifCache = [];
  var _notifUnread = 0;

  function _loadNotifications() {
    if (!_tok()) return;
    fetch('/api/notifications', { headers: _auth() })
      .then(function(r) { return r.json(); })
      .then(function(data) {
        _notifCache = data.notifications || [];
        _notifUnread = data.unread || 0;
        _renderNotifBadge();
        _renderNotifList();
      }).catch(function() {});
  }

  function _renderNotifBadge() {
    var badge = document.querySelector('.notif-badge');
    if (badge) {
      badge.textContent = _notifUnread || '';
      badge.style.display = _notifUnread > 0 ? '' : 'none';
    }
  }

  function _renderNotifList() {
    var list = document.querySelector('.notif-list');
    if (!list) return;
    if (!_notifCache.length) {
      list.innerHTML = '<div style="display:flex;flex-direction:column;align-items:center;justify-content:center;padding:40px 20px;color:var(--pb-text-dim);text-align:center">'
        + '<svg viewBox="0 0 24 24" style="width:40px;height:40px;fill:none;stroke:var(--pb-text-dim);stroke-width:1.2;margin-bottom:12px;opacity:0.4"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>'
        + '<div style="font-size:13px;font-weight:600;margin-bottom:4px">No notifications</div>'
        + '<div style="font-size:11px;opacity:0.6">Activity alerts will appear here</div></div>';
      return;
    }
    var html = '';
    for (var i = 0; i < _notifCache.length; i++) {
      var n = _notifCache[i];
      var unreadClass = n.read ? '' : ' notif-unread';
      var timeAgo = _notifTimeAgo(n.timestamp);
      var icon = _notifIcon(n.category);
      var linkAttr = n.link ? ' data-link="' + escHtml(n.link) + '"' : '';
      html += '<div class="notif-item' + unreadClass + '" data-id="' + escHtml(n.id) + '"' + linkAttr + ' onclick="_handleNotifClick(this)">'
        + '<div class="notif-item-icon">' + icon + '</div>'
        + '<div class="notif-item-content">'
        + '<div class="notif-item-title">' + escHtml(n.title) + '</div>'
        + '<div class="notif-item-body">' + escHtml(n.body) + '</div>'
        + '<div class="notif-item-time">' + timeAgo + '</div>'
        + '</div></div>';
    }
    list.innerHTML = html;
  }

  function _notifIcon(category) {
    if (category === 'email') return '<svg viewBox="0 0 24 24" style="width:16px;height:16px;fill:none;stroke:currentColor;stroke-width:1.5"><rect x="2" y="4" width="20" height="16" rx="2"/><polyline points="22,4 12,13 2,4"/></svg>';
    if (category === 'cc') return '<svg viewBox="0 0 24 24" style="width:16px;height:16px;fill:none;stroke:currentColor;stroke-width:1.5"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>';
    if (category === 'task') return '<svg viewBox="0 0 24 24" style="width:16px;height:16px;fill:none;stroke:currentColor;stroke-width:1.5"><path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg>';
    return '<svg viewBox="0 0 24 24" style="width:16px;height:16px;fill:none;stroke:currentColor;stroke-width:1.5"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>';
  }

  function _notifTimeAgo(iso) {
    if (!iso) return '';
    var d = new Date(iso), now = new Date(), diff = Math.floor((now - d) / 1000);
    if (diff < 60) return 'just now';
    if (diff < 3600) return Math.floor(diff / 60) + 'm ago';
    if (diff < 86400) return Math.floor(diff / 3600) + 'h ago';
    return Math.floor(diff / 86400) + 'd ago';
  }

  function _handleNotifClick(el) {
    var id = el.getAttribute('data-id');
    var link = el.getAttribute('data-link');
    _markNotifRead(id);
    // Navigate to the linked tab/subtab
    if (link) {
      // Support "panel:subtab" format (e.g. "chat:inbox", "chat:cc")
      var parts = link.split(':');
      var panel = parts[0];
      var subtab = parts[1] || null;

      if (window.PanelManager && window.PanelManager.switchTo) {
        window.PanelManager.switchTo(panel);
      } else {
        var item = document.querySelector('.sidebar-item[data-tab="'+panel+'"]');
        if (item) item.click();
      }
      // Switch subtab if specified (e.g. inbox, cc within chat)
      if (subtab && typeof window.switchChatView === 'function') {
        setTimeout(function(){ window.switchChatView(subtab); }, 50);
      }
      document.getElementById('notifPanel').classList.remove('open');
    }
  }

  function _markNotifRead(id) {
    fetch('/api/notifications', {
      method: 'POST',
      headers: _authJson(),
      body: JSON.stringify({ id: id })
    }).then(function(r) { return r.json(); })
      .then(function(data) {
        _notifUnread = data.unread || 0;
        _renderNotifBadge();
        for (var i = 0; i < _notifCache.length; i++) {
          if (_notifCache[i].id === id) { _notifCache[i].read = true; break; }
        }
        _renderNotifList();
      }).catch(function() {});
  }

  function _clearAllNotifs() {
    fetch('/api/notifications', {
      method: 'POST',
      headers: _authJson(),
      body: JSON.stringify({ action: 'clear' })
    }).then(function(r) { return r.json(); })
      .then(function(data) {
        _notifCache = [];
        _notifUnread = 0;
        _renderNotifBadge();
        _renderNotifList();
        if (typeof showToast === 'function') showToast('Notifications cleared');
      }).catch(function() {});
  }

  function _markAllNotifRead() {
    fetch('/api/notifications', {
      method: 'POST',
      headers: _authJson(),
      body: JSON.stringify({ id: 'all' })
    }).then(function(r) { return r.json(); })
      .then(function(data) {
        _notifUnread = 0;
        _renderNotifBadge();
        for (var i = 0; i < _notifCache.length; i++) { _notifCache[i].read = true; }
        _renderNotifList();
        if (typeof showToast === 'function') showToast('All notifications marked as read');
      }).catch(function() {});
  }

  // Dedup: track recent notifications to prevent identical repeats
  var _recentNotifKeys = {};

  function _pushNotification(title, body, category, link) {
    var prefs = window._portalPrefs || {};
    // Respect notification settings
    var notifSettings = {};
    try {
      var sRaw = localStorage.getItem('_notifPrefsCache');
      if (sRaw) notifSettings = JSON.parse(sRaw);
    } catch(e) {}
    if (category === 'email' && notifSettings.email_alerts === false) return;
    if (category === 'cc' && notifSettings.push_notifications === false) return;

    // Dedup: skip if same category+title was pushed in the last 5 minutes
    var dedupKey = (category || '') + ':' + title;
    var now = Date.now();
    if (_recentNotifKeys[dedupKey] && (now - _recentNotifKeys[dedupKey]) < 300000) return;
    _recentNotifKeys[dedupKey] = now;

    fetch('/api/notifications', {
      method: 'POST',
      headers: _authJson(),
      body: JSON.stringify({ action: 'create', title: title, body: body || '', category: category || 'system', link: link || '' })
    }).then(function(r) { return r.json(); })
      .then(function() { _loadNotifications(); })
      .catch(function() {});

    // Browser notification if permission granted
    if (typeof Notification !== 'undefined' && Notification.permission === 'granted') {
      try { new Notification(title, { body: body || '', icon: '/favicon.ico' }); } catch(e) {}
    }

    // Sound if enabled
    if (notifSettings.sound) {
      try { new Audio('/static/notification.mp3').play(); } catch(e) {}
    }
  }

  // Poll for new notifications every 60s
  var _notifPollTimer = null;
  function _startNotifPolling() {
    if (_notifPollTimer) clearInterval(_notifPollTimer);
    _notifPollTimer = setInterval(function() { if (_tok()) _loadNotifications(); }, 60000);
  }

  // -----------------------------------------------------------------------
  // Settings loader
  // -----------------------------------------------------------------------
  function _loadSettings(){
    if(!_tok()) return;
    fetch('/api/settings', { headers: _auth() })
      .then(function(r){ return r.json(); })
      .then(function(s){
        _settingsLoaded = true;

        // Hydrate shared prefs object
        window._portalPrefs = {
          chat_font_size: s.chat_font_size || '13',
          chat_history_count: s.chat_history_count || '200',
          send_on_enter: s.send_on_enter !== false,
          inbox_sync_interval: s.inbox_sync_interval || '60000',
          cc_poll_interval: s.cc_poll_interval || '10000',
          timezone: s.timezone || '',
          clock_format: s.clock_format || '24',
          notifications: s.notifications || {}
        };
        _applyPrefs();
        _hydrateSettingsForms(s);

        // Cache notification prefs for _pushNotification (avoids extra fetch)
        // Default email_alerts and push_notifications to true if not explicitly set
        var notifPrefs = s.notifications || {};
        if (notifPrefs.email_alerts === undefined) notifPrefs.email_alerts = true;
        if (notifPrefs.push_notifications === undefined) notifPrefs.push_notifications = true;
        try { localStorage.setItem('_notifPrefsCache', JSON.stringify(notifPrefs)); } catch(e) {}

        // Apply theme
        if(s.theme && typeof setTheme === 'function'){
          setTheme(s.theme);
          document.querySelectorAll('.settings-theme-btn').forEach(function(b){
            b.classList.toggle('active', b.getAttribute('data-stheme') === s.theme);
          });
        }
        // Apply notification toggles (only the 3 notification cards + digest)
        var notifSection = document.querySelector('#settingsPrefs .profile-grid-4');
        var toggleCards = notifSection ? notifSection.querySelectorAll('.settings-toggle-card') : [];
        if(s.notifications && toggleCards.length){
          var keys = ['email_alerts','push_notifications','sound'];
          toggleCards.forEach(function(card,i){
            if(i < keys.length && s.notifications[keys[i]] !== undefined){
              var on = !!s.notifications[keys[i]];
              card.setAttribute('data-on', on?'true':'false');
              var val = card.querySelector('.settings-toggle-val');
              if(val){ val.textContent = on?'Enabled':'Off'; val.style.color = on?'':'var(--pb-text-dim)'; }
            }
          });
        }
        if(s.digest_frequency){
          var digestCard = toggleCards[3];
          if(digestCard){
            var val = digestCard.querySelector('.settings-toggle-val');
            if(val){ val.textContent = s.digest_frequency; val.style.color = s.digest_frequency==='Off'?'var(--pb-text-dim)':''; }
          }
        }
      }).catch(function(){});
  }

  // Apply prefs to the DOM (CSS custom properties, etc.)
  function _applyPrefs() {
    var p = window._portalPrefs;
    document.documentElement.style.setProperty('--chat-font-size', p.chat_font_size + 'px');
  }

  // Set form controls to match loaded settings
  function _hydrateSettingsForms(s) {
    var el;
    el = document.getElementById('settingFontSize');
    if (el) el.value = s.chat_font_size || '13';
    el = document.getElementById('settingHistoryCount');
    if (el) el.value = s.chat_history_count || '200';
    el = document.getElementById('settingSyncInterval');
    if (el) el.value = s.inbox_sync_interval || '60000';
    el = document.getElementById('settingCCPoll');
    if (el) el.value = s.cc_poll_interval || '10000';
    // Send behavior toggle
    var sendCard = document.getElementById('settingSendBehavior');
    if (sendCard) {
      var on = s.send_on_enter !== false;
      sendCard.setAttribute('data-on', on ? 'true' : 'false');
      var val = sendCard.querySelector('.settings-toggle-val');
      if (val) { val.textContent = on ? 'Enabled' : 'Ctrl+Enter'; val.style.color = on ? '' : 'var(--pb-text-dim)'; }
    }
    // Timezone — hydrate clock widget if setting exists
    if (s.timezone && typeof window.setPortalTZ === 'function') {
      window.setPortalTZ(s.timezone);
    }
    // Clock format toggle card
    var clockCard = document.getElementById('settingClockFormat');
    if (clockCard) {
      var fmt = s.clock_format || '24';
      var is12 = fmt === '12';
      clockCard.setAttribute('data-on', is12 ? 'true' : 'false');
      var val = clockCard.querySelector('.settings-toggle-val');
      if (val) { val.textContent = is12 ? '12h' : '24h'; val.style.color = is12 ? '' : 'var(--pb-text-dim)'; }
    }
  }

  // Toggle Clock Format card (12h / 24h)
  function _toggleClockFormat(card) {
    var is12 = card.getAttribute('data-on') === 'true';
    var newFmt = is12 ? '24' : '12';
    card.setAttribute('data-on', is12 ? 'false' : 'true');
    var val = card.querySelector('.settings-toggle-val');
    if (val) { val.textContent = newFmt === '12' ? '12h' : '24h'; val.style.color = newFmt === '12' ? '' : 'var(--pb-text-dim)'; }
    window._portalPrefs.clock_format = newFmt;
    _saveSettingPartial({ clock_format: newFmt });
  }

  // Toggle "Enter to Send" card
  function _toggleSendBehavior(card) {
    var on = card.getAttribute('data-on') === 'true';
    var newVal = !on;
    card.setAttribute('data-on', newVal ? 'true' : 'false');
    var val = card.querySelector('.settings-toggle-val');
    if (val) { val.textContent = newVal ? 'Enabled' : 'Ctrl+Enter'; val.style.color = newVal ? '' : 'var(--pb-text-dim)'; }
    window._portalPrefs.send_on_enter = newVal;
    _saveSettingPartial({ send_on_enter: newVal });
  }

  function _saveSettingPartial(partial){
    if(!_tok()) return;
    fetch('/api/settings', {
      method: 'POST', headers: _authJson(), body: JSON.stringify(partial)
    }).catch(function(){});
  }

  // -----------------------------------------------------------------------
  // Profile data pipeline — loads from /api/profile, inline editing
  // -----------------------------------------------------------------------
  var _profileLoaded = false;

  function _setField(id, val) {
    var el = document.getElementById(id);
    if (el) el.textContent = val || '-';
  }

  function _loadProfile() {
    if (!_tok()) return;
    // Show loading state on all profile fields if not yet loaded
    if (!_profileLoaded) {
      var fields = document.querySelectorAll('[id^="prof"]');
      fields.forEach(function(el) {
        if (el.textContent === '-' || el.textContent === 'Not configured') {
          el.innerHTML = '<span style="display:inline-block;width:60px;height:12px;background:var(--pb-border);border-radius:4px;opacity:0.5;animation:pulse 1.5s ease-in-out infinite"></span>';
        }
      });
    }
    fetch('/api/profile', { headers: _auth() })
      .then(function(r) { return r.json(); })
      .then(function(p) {
        _profileLoaded = true;

        // Hero
        _setField('profHeroName', p.civ_name);
        var heroTagline = document.getElementById('profileHeroTagline');
        if (heroTagline) heroTagline.textContent = p.role || 'AI Agent';

        // AI Identity
        _setField('profCivName', p.civ_name);
        _setField('profRole', p.role);
        _setField('profModel', p.model);
        _setField('profBorn', p.born ? new Date(p.born).toLocaleDateString() : '');
        _setField('profParent', p.parent_civ);
        _setField('profAgents', p.agent_count);
        _setField('profArch', p.architecture);
        var statusEl = document.getElementById('profStatus');
        if (statusEl) {
          statusEl.textContent = (p.status || 'active').charAt(0).toUpperCase() + (p.status || 'active').slice(1);
          statusEl.style.color = p.status === 'active' ? '#22c55e' : 'var(--pb-text-dim)';
        }

        // Human Partner
        _setField('profHumanName', p.human_name);
        _setField('profHumanTitle', p.human_title);
        _setField('profHumanOrg', p.human_org);
        _setField('profHumanBg', p.human_background);
        _setField('profHumanStyle', p.human_style);
        _setField('profHumanValues', p.human_values);
        _setField('profHumanRole', p.human_role);
        _setField('profHumanRel', p.human_relationship);

        // Contact Card
        _setField('profCivEmail', p.civ_email || 'Not configured');
        _setField('profHumanEmail', p.human_email || 'Not configured');
        _setField('profContactOrg', p.human_org);
        _setField('profWebsite', p.website);
      }).catch(function() {});
  }

  function _editProfileField(container, field) {
    // Find the value element (profile-value or profile-contact-value)
    var valueEl = container.querySelector('.profile-value') || container.querySelector('.profile-contact-value');
    if (!valueEl) return;
    if (container.querySelector('input')) return; // already editing
    var current = valueEl.textContent === '-' || valueEl.textContent === 'Not configured' ? '' : valueEl.textContent;
    var input = document.createElement('input');
    input.type = 'text';
    input.value = current;
    input.style.cssText = 'width:100%;padding:4px 8px;background:var(--pb-bg);color:var(--pb-text);border:1px solid var(--pb-blue);border-radius:4px;font-size:12px;font-family:inherit;outline:none';
    input.onblur = function() { _saveProfileField(field, input.value, valueEl, container); };
    input.onkeydown = function(e) {
      if (e.key === 'Enter') input.blur();
      if (e.key === 'Escape') { valueEl.style.display = ''; input.remove(); }
    };
    valueEl.style.display = 'none';
    container.appendChild(input);
    input.focus();
  }

  function _saveProfileField(field, value, valueEl, container) {
    var input = container.querySelector('input');
    if (input) input.remove();
    valueEl.style.display = '';
    valueEl.textContent = value || '-';
    var body = {};
    body[field] = value;
    fetch('/api/profile', {
      method: 'POST',
      headers: _authJson(),
      body: JSON.stringify(body)
    }).catch(function() {});
  }

  // Intercept theme changes to save server-side
  var _origSetTheme = window.setThemeFromSettings;
  window.setThemeFromSettings = function(theme){
    if(typeof setTheme === 'function') setTheme(theme);
    _saveSettingPartial({ theme: theme });
    document.querySelectorAll('.settings-theme-btn').forEach(function(b){
      b.classList.toggle('active', b.getAttribute('data-stheme') === theme);
    });
  };

  // Intercept toggle clicks to save server-side
  var _origToggle = window.toggleSettingCard;
  window.toggleSettingCard = function(card){
    var on = card.getAttribute('data-on') === 'true';
    var val = card.querySelector('.settings-toggle-val');
    if(on){ card.setAttribute('data-on','false'); if(val){val.textContent='Off';val.style.color='var(--pb-text-dim)';} }
    else{ card.setAttribute('data-on','true'); if(val){val.textContent='Enabled';val.style.color='';} }
    // Determine which setting this is
    var label = card.querySelector('.profile-label');
    var key = label ? label.textContent.toLowerCase().replace(/\s+/g,'_') : '';
    var notif = {};
    notif[key] = !on;
    _saveSettingPartial({ notifications: notif });
    // Update local notification prefs cache
    try {
      var cached = JSON.parse(localStorage.getItem('_notifPrefsCache') || '{}');
      cached[key] = !on;
      localStorage.setItem('_notifPrefsCache', JSON.stringify(cached));
    } catch(e) {}
  };

  var _origCycleDigest = window.cycleDigestFrequency;
  window.cycleDigestFrequency = function(card){
    var opts = ['Daily','Weekly','Monthly','Off'];
    var val = card.querySelector('.settings-toggle-val');
    var idx = opts.indexOf(val.textContent);
    var next = opts[(idx+1)%opts.length];
    val.textContent = next;
    val.style.color = next==='Off' ? 'var(--pb-text-dim)' : '';
    _saveSettingPartial({ digest_frequency: next });
  };

  // Boot: auto-load when token available
  function _boot() {
    _loadSettings();
    _loadProfile();
    _loadNotifications();
    _checkCCAvailability();
    _startNotifPolling();
  }
  window.addEventListener('portal-auth', function(){
    setTimeout(_boot, 600);
  });
  if(_tok()) setTimeout(_boot, 1600);

  // Expose API
  window._portalSettings = { load: _loadSettings };
  window._applyPrefs = _applyPrefs;
  window._toggleSendBehavior = _toggleSendBehavior;
  window._toggleClockFormat = _toggleClockFormat;
  window._saveSettingPartial = _saveSettingPartial;
  window._loadProfile = _loadProfile;
  window._editProfileField = _editProfileField;
  window._loadNotifications = _loadNotifications;
  window._pushNotification = _pushNotification;
  window._handleNotifClick = _handleNotifClick;
  window._markNotifRead = _markNotifRead;
  window._markAllNotifRead = _markAllNotifRead;
  window._clearAllNotifs = _clearAllNotifs;
  window._applyCCVisibility = _applyCCVisibility;

})();
