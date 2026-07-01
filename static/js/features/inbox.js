(function(){
'use strict';

// Multi-account state
var emailAccounts = [];       // [{provider, address, label}, ...]
var activeAccountIdx = 0;
var emailConfigured = false;
var emailAddress = '';

// Per-account thread cache
var _inboxThreads = [];
var _inboxEmail = '';
var _currentThreadId = null;
var _threadCache = {};  // {accountIdx: {threads: [], timestamp: Date.now()}}
var _CACHE_TTL = 60000; // 1 minute default — overridden by _getCacheTTL()

function _getCacheTTL() {
  var val = window._portalPrefs && window._portalPrefs.inbox_sync_interval;
  if (val === 'manual') return Infinity;
  return parseInt(val, 10) || 60000;
}
var _syncInProgress = false; // Guard against duplicate fetches
var _inboxInitDone = false;  // Track if _initInbox already ran
var _lastSyncTime = 0;       // Timestamp of last successful sync
var _threadDetailCache = {};  // {threadId: {data: {...}, timestamp: Date.now()}}
var _THREAD_DETAIL_CACHE_TTL = 300000; // 5 minutes

// ── Email Wizard ─────────────────────────────────────────────────────────
function openEmailWizard(){
  document.getElementById('emailWizardOverlay').classList.add('open');
  ewGoTo(1);
}

function closeEmailWizard(){
  document.getElementById('emailWizardOverlay').classList.remove('open');
}

function openConfirmModal(){
  document.getElementById('confirmModalOverlay').classList.add('open');
}

function closeConfirmModal(){
  document.getElementById('confirmModalOverlay').classList.remove('open');
}

function ewGoTo(step){
  for(var i=1;i<=4;i++){
    var el=document.getElementById('ewStep'+i);
    if(el) el.classList.toggle('active',i===step);
  }
}

function ewNext(step){
  if(step===2){
    var prov=document.getElementById('ewProvider').value;
    if(!prov&&document.getElementById('ewStep1').classList.contains('active')){showToast('Please select a provider');return;}
    var sub=document.getElementById('ewCredSub');
    if(prov==='gmail') sub.textContent='Enter your Gmail address and an App Password.';
    else if(prov==='outlook') sub.textContent='Enter your Outlook email and app password.';
    else if(prov==='agentmail') sub.textContent='Enter your AgentMail address and API key.';
    else sub.textContent='Enter your SMTP email and password.';
    // Update field labels based on provider
    var step2Labels=document.querySelectorAll('#ewStep2 .ew-label');
    var emailLabel=step2Labels[0];
    var pwLabel=step2Labels[1];
    var pwInput=document.getElementById('ewPassword');
    if(prov==='gmail'){
      if(emailLabel)emailLabel.textContent='Gmail Address';
      if(pwLabel)pwLabel.textContent='App Password';
      if(pwInput)pwInput.placeholder='16-character app password';
    }else if(prov==='agentmail'){
      if(emailLabel)emailLabel.textContent='Email Address';
      if(pwLabel)pwLabel.textContent='API Key';
      if(pwInput)pwInput.placeholder='Enter API key';
    }else{
      if(emailLabel)emailLabel.textContent='Email Address';
      if(pwLabel)pwLabel.textContent='App Password / API Key';
      if(pwInput)pwInput.placeholder='Enter app password or API key';
    }
  }
  if(step===3){
    var em=document.getElementById('ewEmail').value.trim();
    var pw=document.getElementById('ewPassword').value.trim();
    if(!em||!pw){showToast('Please fill in all fields');return;}
    // Strip spaces from Gmail app passwords
    var prov=document.getElementById('ewProvider').value || 'agentmail';
    if(prov==='gmail') pw=pw.replace(/\s/g,'');
    ewGoTo(3);
    document.getElementById('ewTestStatus').textContent='Testing connection...';
    document.getElementById('ewTestIcon').textContent='\u23F3';
    document.getElementById('ewTestNext').disabled=true;
    // Save as new account via accounts endpoint
    fetch('/api/inbox/accounts', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer ' + localStorage.getItem('portal_token'), 'Content-Type': 'application/json' },
      body: JSON.stringify({ provider: prov, api_key: pw, address: em, label: '' })
    })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.error) throw new Error(data.error);
      emailAccounts = data.accounts || [];
      // Test connection using the new account (last in array)
      var newIdx = emailAccounts.length - 1;
      return fetch('/api/inbox/threads?limit=1&account=' + newIdx, { headers: { 'Authorization': 'Bearer ' + localStorage.getItem('portal_token') } });
    })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.error) throw new Error(data.error);
      document.getElementById('ewTestStatus').textContent='Connection successful!';
      document.getElementById('ewTestIcon').textContent='\u2705';
      document.getElementById('ewTestNext').disabled=false;
    })
    .catch(function(err) {
      document.getElementById('ewTestStatus').textContent='Connection failed: ' + err.message;
      document.getElementById('ewTestIcon').textContent='\u274C';
      document.getElementById('ewTestNext').disabled=true;
      // Remove the bad account
      if (emailAccounts.length > 0) {
        fetch('/api/inbox/accounts', {
          method: 'DELETE',
          headers: { 'Authorization': 'Bearer ' + localStorage.getItem('portal_token'), 'Content-Type': 'application/json' },
          body: JSON.stringify({ index: emailAccounts.length - 1 })
        }).catch(function(){});
      }
    });
    return;
  }
  if(step===4){
    var em=document.getElementById('ewEmail').value.trim();
    emailConfigured=true;
    emailAddress=em;
    document.getElementById('ewDoneEmail').textContent=em;
    var banner=document.querySelector('.todo-email-banner');
    if(banner){
      banner.innerHTML='<span style="color:var(--pb-green);font-weight:700">\u2713 Email connected</span><span style="color:var(--pb-text-dim);margin-left:8px">'+escHtml(em)+'</span>';
      banner.style.borderColor='rgba(34,197,94,0.2)';
      banner.style.background='rgba(34,197,94,0.04)';
    }
    // Switch to newly added account and refresh
    activeAccountIdx = emailAccounts.length - 1;
    _renderAccountTabs();
    _initInbox();
  }
  ewGoTo(step);
}

// ── Account Tabs ─────────────────────────────────────────────────────────
function _renderAccountTabs() {
  var container = document.getElementById('inboxAccountTabs');
  if (!container) return;
  if (emailAccounts.length <= 0) {
    container.style.display = 'none';
    return;
  }
  container.style.display = 'flex';
  var html = '';
  emailAccounts.forEach(function(acct, idx) {
    var label = acct.label || acct.address.split('@')[0] || 'Account ' + (idx + 1);
    var isActive = idx === activeAccountIdx;
    html += '<div class="inbox-acct-tab' + (isActive ? ' active' : '') + '" data-acct-idx="' + idx + '" onclick="switchAccount(' + idx + ')">'
      + '<span class="inbox-acct-label">' + escHtml(label) + '</span>'
      + '<button class="inbox-acct-remove" onclick="event.stopPropagation();removeAccount(' + idx + ')" title="Remove account">&times;</button>'
      + '</div>';
  });
  html += '<div class="inbox-acct-tab inbox-acct-add" onclick="openEmailWizard()" title="Add email account">+</div>';
  container.innerHTML = html;
}

function switchAccount(idx) {
  if (idx < 0 || idx >= emailAccounts.length) return;
  activeAccountIdx = idx;
  _renderAccountTabs();
  _currentThreadId = null;
  closeEmailDetail();
  // Show cached threads instantly if available
  var cached = _threadCache[idx];
  if (cached && cached.threads) {
    _inboxThreads = cached.threads;
    _renderThreadList(_inboxThreads);
    // Background refresh if cache is stale (don't show loading — cached data is visible)
    if (Date.now() - cached.timestamp > _getCacheTTL()) {
      syncInbox(true); // silent=true: no loading indicator since cached data shown
    }
  } else {
    _showInboxLoading();
    syncInbox();
  }
}

function removeAccount(idx) {
  if (idx < 0 || idx >= emailAccounts.length) return;
  var acct = emailAccounts[idx];
  if (!confirm('Remove email account "' + (acct.label || acct.address) + '"?')) return;
  fetch('/api/inbox/accounts', {
    method: 'DELETE',
    headers: { 'Authorization': 'Bearer ' + localStorage.getItem('portal_token'), 'Content-Type': 'application/json' },
    body: JSON.stringify({ index: idx })
  })
  .then(function(r) { return r.json(); })
  .then(function(data) {
    if (data.error) { showToast('Error: ' + data.error); return; }
    emailAccounts = data.accounts || [];
    if (activeAccountIdx >= emailAccounts.length) {
      activeAccountIdx = Math.max(0, emailAccounts.length - 1);
    }
    _renderAccountTabs();
    // Clear cache for removed account
    delete _threadCache[idx];
    if (emailAccounts.length === 0) {
      emailConfigured = false;
      _inboxInitDone = false;
      _inboxThreads = [];
      _threadCache = {};
      _renderThreadList([]);
      document.querySelector('.inbox-status-text').textContent = 'Not connected';
      document.querySelector('.inbox-status-dot').style.background = 'var(--pb-text-dim)';
      var setupBtn = document.getElementById('inboxSetupBtn');
      if (setupBtn) setupBtn.style.display = '';
    } else {
      syncInbox();
    }
  })
  .catch(function(err) { showToast('Failed to remove account'); });
}

// Chat/Inbox/CC view switching
function switchChatView(view){
  // Auto-close chat search when switching views
  if(window._closeChatSearch) window._closeChatSearch();
  document.querySelectorAll('.chat-subtab').forEach(function(b){b.classList.remove('active')});
  var viewNames = { chat: 'Chat', inbox: 'Inbox', cc: 'CC' };
  var target = viewNames[view] || 'Chat';
  document.querySelectorAll('.chat-subtab').forEach(function(b){
    if (b.textContent.trim().indexOf(target) === 0) b.classList.add('active');
  });
  var msgs=document.querySelector('.chat-messages');
  var wrapper=document.querySelector('.chat-messages-wrapper');
  var banner=document.querySelector('.brain-banner');
  var composer=document.querySelector('.composer');
  var inbox=document.getElementById('inboxView');
  var ccView=document.getElementById('ccView');
  var actions=document.getElementById('chatHeaderActions');

  // Dock button HTML — included in every view so they never disappear
  var _dockState=window.DockManager?window.DockManager.getState():'UNDOCKED';
  var _isDocked=_dockState!=='UNDOCKED';
  var _dockBtns='<button class="dock-btn" id="dockChatBtn" onclick="toggleDockChat()" title="'+(_isDocked?'Undock chat':'Dock chat to bottom')+' (Ctrl+Shift+D)">'+(_isDocked?'<svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg> Undock':'<svg viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="2"/><line x1="3" y1="15" x2="21" y2="15"/></svg> Dock')+'</button>'+'<button class="dock-btn" id="dockResetBtn" onclick="resetDockHeight()" title="Reset dock size to default" style="'+(_isDocked?'':'display:none')+'"><svg viewBox="0 0 24 24"><polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"/></svg></button>';
  // Clock widget HTML — included in chat view innerHTML so the IIFE interval re-discovers it
  var _clockHtml='<div class="live-clock-widget" id="liveClockWidget" onclick="toggleTimezoneMenu()" title="Click to change timezone"><svg viewBox="0 0 24 24" style="width:12px;height:12px;stroke:currentColor;fill:none;stroke-width:2"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg><span id="liveClock">--:--:--</span><span id="liveClockTZ" style="font-size:9px;opacity:0.6">UTC</span><div class="tz-menu" id="tzMenu"><div class="tz-option" onclick="setPortalTZ(\'Asia/Karachi\')">PKT (Karachi)</div><div class="tz-option" onclick="setPortalTZ(\'America/New_York\')">ET (New York)</div><div class="tz-option" onclick="setPortalTZ(\'America/Chicago\')">CT (Chicago)</div><div class="tz-option" onclick="setPortalTZ(\'America/Los_Angeles\')">PT (Los Angeles)</div><div class="tz-option" onclick="setPortalTZ(\'Europe/London\')">GMT (London)</div><div class="tz-option" onclick="setPortalTZ(\'UTC\')">UTC</div><div class="tz-option" onclick="setPortalTZ(\'Asia/Dubai\')">GST (Dubai)</div><div class="tz-option" onclick="setPortalTZ(\'Asia/Kolkata\')">IST (India)</div><div class="tz-option" onclick="setPortalTZ(\'Asia/Tokyo\')">JST (Tokyo)</div><div class="tz-option" onclick="setPortalTZ(\'Australia/Sydney\')">AEST (Sydney)</div></div></div>';

  // --- DOCKED behavior: inbox/cc render as main panels above the dock ---
  if (_isDocked && (view === 'inbox' || view === 'cc')) {
    // Restore any previously promoted panel first (handles inbox->cc switch)
    _restorePromotedPanels();
    // Ensure chat content stays visible in the dock
    msgs.style.display='';
    if(wrapper)wrapper.style.display='';
    banner.style.display='';
    composer.style.display='';
    // Hide inbox/cc inside chatArea (they'll be moved out)
    inbox.style.display='none';
    if(ccView)ccView.classList.remove('visible');

    // Collapse dock from expanded to saved height
    var app=document.querySelector('.app');
    if(app.classList.contains('dock-expanded')){
      window.DockManager.collapse();
    }

    // Move the target view out of chatArea into main-col as a panel
    var mainCol=document.querySelector('.main-col');
    var targetEl=(view==='inbox')?inbox:ccView;
    if(targetEl && targetEl.parentNode!==mainCol){
      mainCol.appendChild(targetEl);
    }
    // Hide other panels above the dock (except chat)
    if(window.PanelManager && window.PanelManager._hideAllExceptChat){
      window.PanelManager._hideAllExceptChat();
    }
    // Show the promoted element as a main panel
    targetEl.classList.add('docked-promoted-panel');
    if(view==='inbox'){
      targetEl.style.display='flex';
    } else {
      targetEl.classList.add('visible');
    }
    // Apply dock padding so it doesn't hide behind the dock
    var savedH=parseInt(localStorage.getItem('portal_dock_height'))||280;
    targetEl.style.paddingBottom=(savedH+8)+'px';

    // Sync PanelManager state so sidebar and active panel stay consistent
    if(window.PanelManager){
      window.PanelManager._highlightSidebar('chat');
      window.PanelManager.setActivePanel('chat');
    }

    // Update header actions
    if(view==='inbox'){
      actions.innerHTML=_dockBtns+'<button class="ch-search" onclick="searchInbox()"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg> Search</button>';
      if (!_inboxInitDone && localStorage.getItem('portal_token')) _initInbox();
    } else {
      actions.innerHTML=_dockBtns;
      if(window['_ccStartPolling'])window['_ccStartPolling']();
      var ccM=document.getElementById('ccMessages');
      if(ccM)requestAnimationFrame(function(){ccM.scrollTop=ccM.scrollHeight;});
    }
    return;
  }

  if (_isDocked && view === 'chat') {
    // Moving back to chat while docked — restore inbox/cc to chatArea, expand dock
    _restorePromotedPanels();
    // Expand dock to fullscreen for chat view
    window.DockManager.expand();
    // Show chat content
    msgs.style.display='';
    if(wrapper)wrapper.style.display='';
    banner.style.display='';
    composer.style.display='';
    inbox.style.display='none';
    if(ccView)ccView.classList.remove('visible');
    actions.innerHTML=_clockHtml+_dockBtns+'<button class="ch-search" onclick="toggleChatSearch()"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg> Search</button><button class="poke-btn"><svg viewBox="0 0 24 24"><path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/></svg> Poke '+(window._portalCivName||'AI')+'</button>';
    if(window._ccStopPolling)window._ccStopPolling();
    return;
  }

  // --- UNDOCKED behavior (original logic) ---
  msgs.style.display='none';
  if(wrapper)wrapper.style.display='none';
  banner.style.display='none';
  composer.style.display='none';
  inbox.style.display='none';
  if(ccView)ccView.classList.remove('visible');

  if(view==='inbox'){
    inbox.style.display='flex';
    actions.innerHTML=_dockBtns+'<button class="ch-search" onclick="searchInbox()"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg> Search</button>';
    // Load accounts + threads if not already loaded
    if (!_inboxInitDone && localStorage.getItem('portal_token')) _initInbox();
  }else if(view==='cc'){
    if(ccView)ccView.classList.add('visible');
    actions.innerHTML=_dockBtns;
    if(window['_ccStartPolling'])window['_ccStartPolling']();
    var ccM=document.getElementById('ccMessages');
    if(ccM)requestAnimationFrame(function(){ccM.scrollTop=ccM.scrollHeight;});
  }else{
    msgs.style.display='';
    if(wrapper)wrapper.style.display='';
    banner.style.display='';
    composer.style.display='';
    actions.innerHTML=_clockHtml+_dockBtns+'<button class="ch-search" onclick="toggleChatSearch()"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg> Search</button><button class="poke-btn"><svg viewBox="0 0 24 24"><path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/></svg> Poke '+(window._portalCivName||'AI')+'</button>';
    if(window._ccStopPolling)window._ccStopPolling();
  }
}

// Helper: move promoted inbox/cc panels back into chatArea
function _restorePromotedPanels(){
  var chatArea=document.getElementById('chatArea');
  var banner=chatArea.querySelector('.brain-banner');
  var inbox=document.getElementById('inboxView');
  var ccView=document.getElementById('ccView');
  if(inbox && inbox.classList.contains('docked-promoted-panel')){
    inbox.classList.remove('docked-promoted-panel');
    inbox.style.display='none';
    inbox.style.paddingBottom='';
    chatArea.insertBefore(inbox,banner);
  }
  if(ccView && ccView.classList.contains('docked-promoted-panel')){
    ccView.classList.remove('docked-promoted-panel');
    ccView.classList.remove('visible');
    ccView.style.paddingBottom='';
    chatArea.insertBefore(ccView,banner);
  }
}

// ── HTML email helper ────────────────────────────────────────────────────
function escAttr(s) {
  return (s || '').replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

// ── HTML email sanitization ──────────────────────────────────────────────
function _sandboxHtml(html) {
  // Replace external images with placeholders
  var safeHtml = html.replace(/<img\s[^>]*src\s*=\s*["']https?:\/\/[^"']+["'][^>]*>/gi, function(match) {
    var altMatch = match.match(/alt\s*=\s*["']([^"']*)["']/i);
    var alt = altMatch ? altMatch[1] : 'Image';
    return '<span style="display:inline-block;padding:4px 8px;background:#f0f0f0;color:#666;font-size:11px;border-radius:3px;margin:2px 0">[' + alt + ']</span>';
  });
  // Block tracking pixels (1x1 or 0-size images)
  safeHtml = safeHtml.replace(/<img\s[^>]*(width\s*=\s*["']?[01]|height\s*=\s*["']?[01])[^>]*>/gi, '');
  // Inject base styles for readability in dark themes
  var baseStyle = '<style>body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;font-size:13px;line-height:1.6;color:#333;background:#fff;margin:8px;word-wrap:break-word}a{color:#2563eb}img{max-width:100%;height:auto}</style>';
  return baseStyle + safeHtml;
}

function _showEmailImages(btn) {
  btn.style.display = 'none';
  var container = btn.nextElementSibling;
  var frame = container ? container.querySelector('.email-html-frame') : null;
  if (frame && frame.dataset.originalHtml) {
    var baseStyle = '<style>body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;font-size:13px;line-height:1.6;color:#333;background:#fff;margin:8px;word-wrap:break-word}a{color:#2563eb}img{max-width:100%;height:auto}</style>';
    frame.srcdoc = baseStyle + frame.dataset.originalHtml;
    frame.onload = function() {
      try { frame.style.height = frame.contentDocument.body.scrollHeight + 20 + 'px'; } catch(e) {}
    };
  }
}

// ── AgentMail Inbox (real API-backed) ─────────────────────────────────────

function _inboxTimeAgo(iso) {
  if (!iso) return '';
  var d = new Date(iso), now = new Date(), diff = Math.floor((now - d) / 1000);
  if (diff < 60) return 'just now';
  if (diff < 3600) return Math.floor(diff / 60) + 'm ago';
  if (diff < 86400) return Math.floor(diff / 3600) + 'h ago';
  if (diff < 604800) return Math.floor(diff / 86400) + 'd ago';
  return d.toLocaleDateString();
}

function _renderThreadList(threads) {
  var list = document.querySelector('.inbox-list');
  if (!threads || !threads.length) {
    list.innerHTML = '<div class="inbox-empty" style="display:flex;flex-direction:column;align-items:center;justify-content:center;padding:60px 20px;color:var(--pb-text-dim);text-align:center"><svg viewBox="0 0 24 24" style="width:48px;height:48px;fill:none;stroke:var(--pb-text-dim);stroke-width:1.2;margin-bottom:16px;opacity:0.4"><rect x="2" y="4" width="20" height="16" rx="2"/><polyline points="22,4 12,13 2,4"/></svg><div style="font-size:14px;font-weight:600;margin-bottom:6px">No messages yet</div><div style="font-size:12px;opacity:0.6">Send an email or wait for incoming mail</div></div>';
    return;
  }
  var html = '';
  threads.forEach(function(t) {
    var sender = (t.senders && t.senders[0]) || 'Unknown';
    var senderName = sender.replace(/<[^>]+>/g, '').trim() || sender;
    var unreadClass = t.unread ? ' unread' : '';
    var countBadge = t.message_count > 1 ? ' <span style="font-size:10px;color:var(--pb-text-dim);font-weight:400">(' + t.message_count + ')</span>' : '';
    html += '<div class="inbox-item' + unreadClass + '" data-thread-id="' + escHtml(t.thread_id) + '" onclick="openThread(\'' + escHtml(t.thread_id) + '\')">'
      + '<div class="inbox-item-body">'
      + '<div class="inbox-item-header"><span class="inbox-item-from">' + escHtml(senderName) + countBadge + '</span><span class="inbox-item-time">' + _inboxTimeAgo(t.timestamp) + '</span></div>'
      + '<div class="inbox-item-subject">' + escHtml(t.subject) + '</div>'
      + '<div class="inbox-item-preview">' + escHtml(t.preview) + '</div>'
      + '</div></div>';
  });
  list.innerHTML = html;
}

function _showInboxLoading() {
  var list = document.querySelector('.inbox-list');
  if (list) {
    list.innerHTML = '<div style="display:flex;flex-direction:column;align-items:center;justify-content:center;padding:60px 20px;color:var(--pb-text-dim);text-align:center">'
      + '<svg viewBox="0 0 24 24" style="width:32px;height:32px;fill:none;stroke:var(--pb-text-dim);stroke-width:1.5;animation:spin 1s linear infinite;margin-bottom:12px"><path d="M23 4v6h-6"/><path d="M1 20v-6h6"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>'
      + '<div style="font-size:13px">Loading messages...</div></div>';
  }
}

function syncInbox(silent) {
  // Guard against duplicate concurrent fetches
  if (_syncInProgress) return;
  _syncInProgress = true;
  var fetchAccountIdx = activeAccountIdx; // capture for closure
  var btn = document.getElementById('inboxSyncBtn');
  var statusText = document.querySelector('.inbox-status-text');
  if (!silent) {
    btn.innerHTML = '<svg viewBox="0 0 24 24" style="width:12px;height:12px;animation:spin 1s linear infinite"><path d="M23 4v6h-6"/><path d="M1 20v-6h6"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg> Syncing...';
  }
  fetch('/api/inbox/threads?account=' + fetchAccountIdx, { headers: { 'Authorization': 'Bearer ' + localStorage.getItem('portal_token') } })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      _syncInProgress = false;
      if (data.error) {
        btn.innerHTML = '<svg viewBox="0 0 24 24" style="width:12px;height:12px"><path d="M23 4v6h-6"/><path d="M1 20v-6h6"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg> Sync Now';
        statusText.textContent = 'Error: ' + data.error;
        statusText.style.color = 'var(--pb-red, #ef4444)';
        return;
      }
      // Show green "Synced!" state for 2 seconds
      if (!silent) {
        btn.innerHTML = '<svg viewBox="0 0 24 24" style="width:12px;height:12px;fill:none;stroke:var(--pb-green);stroke-width:3"><polyline points="20 6 9 17 4 12"/></svg> Synced!';
        btn.style.color = 'var(--pb-green)';
        setTimeout(function() {
          btn.innerHTML = '<svg viewBox="0 0 24 24" style="width:12px;height:12px"><path d="M23 4v6h-6"/><path d="M1 20v-6h6"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg> Sync Now';
          btn.style.color = '';
        }, 2000);
      } else {
        btn.innerHTML = '<svg viewBox="0 0 24 24" style="width:12px;height:12px"><path d="M23 4v6h-6"/><path d="M1 20v-6h6"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg> Sync Now';
      }
      // Only apply results if user hasn't switched accounts during fetch
      if (fetchAccountIdx !== activeAccountIdx) return;
      var prevUnread = _inboxThreads.filter(function(t) { return t.unread; }).length;
      var prevCount = _inboxThreads.length;
      _inboxThreads = data.threads || [];
      _inboxEmail = data.email || '';
      // Detect new mail: compare unread counts (catches replies to existing threads)
      // Also detect new threads as a fallback
      // Skip on initial load (prevCount === 0) -- that is just loading, not new mail
      var newUnread = _inboxThreads.filter(function(t) { return t.unread; }).length;
      var hasNewMail = (prevCount > 0) && (newUnread > prevUnread || _inboxThreads.length > prevCount);
      if (hasNewMail) {
        var diff = Math.max(newUnread - prevUnread, _inboxThreads.length - prevCount, 1);
        showToast(diff + ' new message' + (diff > 1 ? 's' : ''));
        if (window._pushNotification) {
          // Build a useful notification body from the new unread threads
          var unreadThreads = _inboxThreads.filter(function(t) { return t.unread; });
          var body = '';
          if (unreadThreads.length > 0) {
            // Show up to 3 thread subjects with sender
            var preview = unreadThreads.slice(0, 3);
            var lines = preview.map(function(t) {
              var from = 'Unknown';
              if (t.senders && t.senders.length > 0) {
                // Parse "Name <email>" format -- take the name part
                var s = t.senders[t.senders.length - 1];
                var nameMatch = s.match(/^([^<]+)</);
                from = nameMatch ? nameMatch[1].trim() : s.split('@')[0];
              } else if (t.from_name) {
                from = t.from_name;
              } else if (t.from_email) {
                from = t.from_email.split('@')[0];
              }
              var subj = t.subject || '(no subject)';
              if (subj.length > 50) subj = subj.substring(0, 47) + '...';
              return from + ': ' + subj;
            });
            body = lines.join('\n');
            if (unreadThreads.length > 3) body += '\n+' + (unreadThreads.length - 3) + ' more';
          } else {
            body = diff + ' new message' + (diff > 1 ? 's' : '') + ' in inbox';
          }
          window._pushNotification(
            diff + ' new email' + (diff > 1 ? 's' : ''),
            body,
            'email',
            'chat:inbox'
          );
        }
      }
      // Cache threads for this account
      _threadCache[fetchAccountIdx] = { threads: _inboxThreads, timestamp: Date.now() };
      _lastSyncTime = Date.now();
      _renderThreadList(_inboxThreads);
      _updateUnreadBadge();
      statusText.textContent = _inboxEmail + ' \u00B7 synced just now';
      statusText.style.color = '';
      document.querySelector('.inbox-status-dot').style.background = 'var(--pb-green)';
    })
    .catch(function(err) {
      _syncInProgress = false;
      btn.innerHTML = '<svg viewBox="0 0 24 24" style="width:12px;height:12px"><path d="M23 4v6h-6"/><path d="M1 20v-6h6"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg> Sync Now';
      statusText.textContent = 'Sync failed';
      statusText.style.color = 'var(--pb-red, #ef4444)';
    });
}

function openThread(threadId) {
  _currentThreadId = threadId;
  var detail = document.getElementById('emailDetail');
  var toolbar = document.querySelector('.inbox-toolbar');
  var list = document.querySelector('.inbox-list');
  var accountTabs = document.getElementById('inboxAccountTabs');
  toolbar.style.display = 'none';
  list.style.display = 'none';
  if (accountTabs) accountTabs.style.display = 'none';
  detail.style.display = 'flex';
  document.getElementById('emailDetailSubject').textContent = '';
  document.getElementById('emailDetailFrom').textContent = '';
  document.getElementById('emailDetailTime').textContent = '';
  var item = document.querySelector('.inbox-item[data-thread-id="' + threadId + '"]');
  if (item) item.classList.remove('unread');

  // Check cache first
  var cached = _threadDetailCache[threadId];
  if (cached && (Date.now() - cached.timestamp < _THREAD_DETAIL_CACHE_TTL)) {
    _renderThreadDetail(cached.data);
    // Background refresh if cache is older than 60s
    if (Date.now() - cached.timestamp > 60000) {
      _fetchThreadDetail(threadId, true); // silent=true, update cache only
    }
    return;
  }

  // Show loading and fetch
  document.getElementById('emailDetailBody').innerHTML = '<div style="text-align:center;padding:40px;color:var(--pb-text-dim)">Loading...</div>';
  _fetchThreadDetail(threadId, false);
}

function _fetchThreadDetail(threadId, silent) {
  fetch('/api/inbox/threads/' + encodeURIComponent(threadId) + '?account=' + activeAccountIdx, { headers: { 'Authorization': 'Bearer ' + localStorage.getItem('portal_token') } })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.error) {
        if (!silent) {
          document.getElementById('emailDetailBody').innerHTML = '<div style="padding:20px;color:var(--pb-red,#ef4444)">' + escHtml(data.error) + '</div>';
        }
        return;
      }
      // Update cache
      _threadDetailCache[threadId] = { data: data, timestamp: Date.now() };
      // Only render if user hasn't switched threads (or not silent)
      if (!silent || _currentThreadId === threadId) {
        if (_currentThreadId === threadId) {
          _renderThreadDetail(data);
        }
      }
      // Mark as read
      fetch('/api/inbox/threads/' + encodeURIComponent(threadId) + '/read?account=' + activeAccountIdx, {
        method: 'POST',
        headers: { 'Authorization': 'Bearer ' + localStorage.getItem('portal_token') }
      }).catch(function() {});
      var thr = _inboxThreads.find(function(t) { return t.thread_id === threadId; });
      if (thr) thr.unread = false;
      _updateUnreadBadge();
    })
    .catch(function(err) {
      if (!silent && _currentThreadId === threadId) {
        document.getElementById('emailDetailBody').innerHTML = '<div style="padding:20px;color:var(--pb-red,#ef4444)">Failed to load thread</div>';
      }
    });
}

function _renderThreadDetail(data) {
  document.getElementById('emailDetailSubject').textContent = data.subject || '(no subject)';
  var msgs = data.messages || [];
  if (msgs.length > 0) {
    document.getElementById('emailDetailFrom').textContent = msgs[0]['from'] || '';
    document.getElementById('emailDetailTime').textContent = msgs[0].timestamp ? new Date(msgs[0].timestamp).toLocaleString() : '';
  }
  var bodyHtml = '';
  msgs.forEach(function(m, idx) {
    var fromStr = escHtml(m['from'] || 'Unknown');
    var timeStr = m.timestamp ? new Date(m.timestamp).toLocaleString() : '';
    var content;
    if (m.html) {
      // Progressive rendering: show plain text first, then HTML iframe
      var plainPreview = m.text ? escHtml(m.text).replace(/\n/g, '<br>') : '';
      var hadImages = /<img\s[^>]*src\s*=\s*["']https?:\/\//i.test(m.html);
      content = '<div style="position:relative">'
        + '<div class="email-text-preview" style="font-size:13px;line-height:1.7;color:var(--pb-text-muted)">' + plainPreview + '</div>'
        + '<iframe class="email-html-frame" srcdoc="' + escAttr(_sandboxHtml(m.html)) + '" '
        + 'data-original-html="' + escAttr(m.html) + '" '
        + 'style="width:100%;min-height:200px;border:none;background:#fff;border-radius:4px;opacity:0;transition:opacity 0.3s;position:absolute;top:0;left:0" '
        + 'sandbox="allow-same-origin" onload="this.style.opacity=1;this.style.position=\'\';'
        + 'var p=this.previousElementSibling;if(p)p.style.display=\'none\';'
        + 'try{this.style.height=this.contentDocument.body.scrollHeight+20+\'px\'}catch(e){}"></iframe>'
        + '</div>';
      if (hadImages) {
        content = '<button class="show-images-btn" onclick="_showEmailImages(this)" '
          + 'style="display:inline-block;margin:0 0 8px;padding:4px 12px;background:none;border:1px solid var(--pb-border);border-radius:4px;color:var(--pb-text-dim);font-size:11px;cursor:pointer;font-family:inherit">'
          + 'Show images</button>' + content;
      }
    } else if (m.text) {
      content = escHtml(m.text).replace(/\n/g, '<br>');
    } else {
      content = '<em style="color:var(--pb-text-dim)">(no text content)</em>';
    }
    bodyHtml += '<div style="' + (idx > 0 ? 'border-top:1px solid var(--pb-border);padding-top:16px;margin-top:16px' : '') + '">'
      + '<div style="display:flex;justify-content:space-between;margin-bottom:8px;font-size:11px;color:var(--pb-text-dim)"><strong style="color:var(--pb-text-muted)">' + fromStr + '</strong><span>' + escHtml(timeStr) + '</span></div>'
      + '<div style="font-size:13px;line-height:1.7;color:var(--pb-text-muted)">' + content + '</div>'
      + '</div>';
  });
  document.getElementById('emailDetailBody').innerHTML = bodyHtml;
}

function closeEmailDetail() {
  document.getElementById('emailDetail').style.display = 'none';
  document.querySelector('.inbox-toolbar').style.display = '';
  document.querySelector('.inbox-list').style.display = '';
  var accountTabs = document.getElementById('inboxAccountTabs');
  if (accountTabs && emailAccounts.length > 0) accountTabs.style.display = 'flex';
  _currentThreadId = null;
}

function toggleEmailActions() {
  document.getElementById('emailActionMenu').classList.toggle('open');
}
document.addEventListener('click', function(e) {
  var menu = document.getElementById('emailActionMenu');
  if (menu && menu.classList.contains('open') && !e.target.closest('.email-action-dropdown')) { menu.classList.remove('open'); }
});

function mailoDraft(action) {
  if (action === 'reply' && _currentThreadId) {
    _openReplyComposer();
    toggleEmailActions();
    return;
  }
  toggleEmailActions();
}

function _openReplyComposer() {
  var actions = document.querySelector('.email-detail-actions');
  if (document.getElementById('inlineReplyBox')) return;
  var box = document.createElement('div');
  box.id = 'inlineReplyBox';
  box.style.cssText = 'padding:12px 20px;border-top:1px solid var(--pb-border);background:var(--pb-surface)';
  box.innerHTML = '<textarea id="replyTextarea" placeholder="Type your reply..." style="width:100%;min-height:80px;background:var(--pb-bg);color:var(--pb-text);border:1px solid var(--pb-border);border-radius:6px;padding:10px;font-size:13px;font-family:inherit;resize:vertical;box-sizing:border-box"></textarea>'
    + '<div style="display:flex;gap:8px;margin-top:8px;justify-content:flex-end">'
    + '<button onclick="document.getElementById(\'inlineReplyBox\').remove()" style="padding:6px 14px;border-radius:6px;border:1px solid var(--pb-border);background:none;color:var(--pb-text-muted);font-size:12px;font-family:inherit;cursor:pointer">Cancel</button>'
    + '<button onclick="_sendReply()" style="padding:6px 14px;border-radius:6px;border:none;background:var(--pb-blue);color:#fff;font-size:12px;font-family:inherit;cursor:pointer;font-weight:600">Send Reply</button>'
    + '</div>';
  actions.parentNode.insertBefore(box, actions.nextSibling);
  document.getElementById('replyTextarea').focus();
}

function _sendReply() {
  var text = document.getElementById('replyTextarea').value.trim();
  if (!text) { showToast('Reply cannot be empty'); return; }
  var sendBtn = document.querySelector('#inlineReplyBox button:last-child');
  sendBtn.textContent = 'Sending...';
  sendBtn.disabled = true;
  fetch('/api/inbox/threads/' + encodeURIComponent(_currentThreadId) + '?account=' + activeAccountIdx, { headers: { 'Authorization': 'Bearer ' + localStorage.getItem('portal_token') } })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      var msgs = data.messages || [];
      var lastMsg = msgs[msgs.length - 1];
      if (!lastMsg) throw new Error('No messages in thread');
      return fetch('/api/inbox/reply/' + encodeURIComponent(lastMsg.message_id) + '?account=' + activeAccountIdx, {
        method: 'POST',
        headers: { 'Authorization': 'Bearer ' + localStorage.getItem('portal_token'), 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: text, thread_id: _currentThreadId })
      });
    })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.error) throw new Error(data.error);
      showToast('Reply sent');
      var box = document.getElementById('inlineReplyBox');
      if (box) box.remove();
      // Invalidate thread detail cache so openThread fetches fresh data with the new reply
      delete _threadDetailCache[_currentThreadId];
      openThread(_currentThreadId);
    })
    .catch(function(err) {
      showToast('Send failed: ' + err.message);
      sendBtn.textContent = 'Send Reply';
      sendBtn.disabled = false;
    });
}

function _updateUnreadBadge() {
  var unread = _inboxThreads.filter(function(t) { return t.unread; }).length;
  var badge = document.querySelector('.inbox-badge');
  if (badge) { badge.textContent = unread || ''; badge.style.display = unread ? '' : 'none'; }
  var markBtn = document.getElementById('inboxMarkAllReadBtn');
  if (markBtn) markBtn.style.display = unread > 0 ? '' : 'none';
}

function markAllRead() {
  var btn = document.getElementById('inboxMarkAllReadBtn');
  if (!btn) return;
  var origHtml = btn.innerHTML;
  btn.innerHTML = '<svg viewBox="0 0 24 24" style="width:12px;height:12px;animation:spin 1s linear infinite"><path d="M23 4v6h-6"/><path d="M1 20v-6h6"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg> Marking...';
  btn.disabled = true;
  // Optimistically mark all as read locally
  _inboxThreads.forEach(function(t) { t.unread = false; });
  _renderThreadList(_inboxThreads);
  _updateUnreadBadge();
  fetch('/api/inbox/mark-all-read?account=' + activeAccountIdx, {
    method: 'POST',
    headers: { 'Authorization': 'Bearer ' + localStorage.getItem('portal_token') }
  })
  .then(function(r) { return r.json(); })
  .then(function(data) {
    btn.disabled = false;
    if (data.error) {
      showToast('Error: ' + data.error);
      btn.innerHTML = origHtml;
      syncInbox(); // Re-fetch to get correct state
      return;
    }
    showToast('All messages marked as read');
    btn.style.display = 'none';
    btn.innerHTML = origHtml;
    // Invalidate thread cache so next sync fetches fresh state
    delete _threadCache[activeAccountIdx];
  })
  .catch(function(err) {
    btn.disabled = false;
    btn.innerHTML = origHtml;
    showToast('Failed to mark all read');
    syncInbox();
  });
}

function filterInbox(val) {
  if (val === 'sent') {
    // Fetch sent threads from backend
    _showInboxLoading();
    fetch('/api/inbox/threads?account=' + activeAccountIdx + '&folder=sent', {
      headers: { 'Authorization': 'Bearer ' + localStorage.getItem('portal_token') }
    })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.error) { showToast('Error: ' + data.error); _renderThreadList(_inboxThreads); return; }
      _renderThreadList(data.threads || []);
    })
    .catch(function() { showToast('Failed to load sent messages'); _renderThreadList(_inboxThreads); });
    return;
  }
  if (val === 'all') {
    _renderThreadList(_inboxThreads); // Show cached inbox threads
    return;
  }
  // Existing unread filter
  document.querySelectorAll('.inbox-item').forEach(function(item) {
    if (val === 'unread') item.style.display = item.classList.contains('unread') ? '' : 'none';
  });
}
function searchInbox() {
  var toolbar = document.querySelector('.inbox-toolbar');
  var existing = document.getElementById('inboxSearchBar');
  if (existing) {
    existing.remove();
    _renderThreadList(_inboxThreads);
    return;
  }
  var bar = document.createElement('div');
  bar.id = 'inboxSearchBar';
  bar.style.cssText = 'display:flex;gap:8px;padding:8px 16px;border-bottom:1px solid var(--pb-border);align-items:center';
  bar.innerHTML = '<input id="inboxSearchInput" type="text" placeholder="Search messages..." '
    + 'style="flex:1;padding:6px 12px;background:var(--pb-bg);color:var(--pb-text);'
    + 'border:1px solid var(--pb-border);border-radius:6px;font-size:12px;font-family:inherit;box-sizing:border-box">'
    + '<button onclick="searchInbox()" style="background:none;border:none;color:var(--pb-text-dim);cursor:pointer;font-size:16px;padding:2px 6px">&times;</button>';
  toolbar.parentNode.insertBefore(bar, toolbar.nextSibling);
  var input = document.getElementById('inboxSearchInput');
  input.focus();
  input.addEventListener('input', function() {
    var q = this.value.toLowerCase();
    if (!q) { _renderThreadList(_inboxThreads); return; }
    document.querySelectorAll('.inbox-item').forEach(function(item) {
      item.style.display = item.textContent.toLowerCase().indexOf(q) >= 0 ? '' : 'none';
    });
  });
  input.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') searchInbox(); // close
  });
}

// ── Compose Email Modal ──────────────────────────────────────────────────
function handleComposeEmail() {
  fetch('/api/inbox/status', { headers: { 'Authorization': 'Bearer ' + localStorage.getItem('portal_token') } })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (!data.configured) {
        openConfirmModal();
        return;
      }
      _openComposeModal();
    })
    .catch(function() {
      if (confirm('Email is not configured yet. Set up AgentMail now?')) openEmailWizard();
    });
}

function _openComposeModal() {
  if (document.getElementById('composeOverlay')) return;
  // Build account selector if multiple accounts
  var fromSelect = '';
  if (emailAccounts.length > 1) {
    var opts = '';
    emailAccounts.forEach(function(a, i) {
      var sel = i === activeAccountIdx ? ' selected' : '';
      opts += '<option value="' + i + '"' + sel + '>' + escHtml(a.label || a.address) + ' (' + escHtml(a.address) + ')</option>';
    });
    fromSelect = '<div style="display:flex;align-items:center;gap:8px"><label style="font-size:12px;color:var(--pb-text-dim);font-weight:600;white-space:nowrap">From:</label><select id="composeFrom" style="flex:1;padding:8px 12px;background:var(--pb-bg);color:var(--pb-text);border:1px solid var(--pb-border);border-radius:6px;font-size:13px;font-family:inherit;box-sizing:border-box">' + opts + '</select></div>';
  }
  var overlay = document.createElement('div');
  overlay.id = 'composeOverlay';
  overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.6);z-index:9999;display:flex;align-items:center;justify-content:center;backdrop-filter:blur(4px)';
  overlay.onclick = function(e) { if (e.target === overlay) overlay.remove(); };
  overlay.innerHTML = '<div style="background:var(--pb-surface);border:1px solid var(--pb-border);border-radius:12px;width:520px;max-width:90vw;max-height:85vh;display:flex;flex-direction:column;overflow:hidden">'
    + '<div style="padding:16px 20px;border-bottom:1px solid var(--pb-border);display:flex;justify-content:space-between;align-items:center"><span style="font-size:14px;font-weight:700;color:var(--pb-text)">Compose Email</span><button onclick="document.getElementById(\'composeOverlay\').remove()" style="background:none;border:none;color:var(--pb-text-dim);font-size:18px;cursor:pointer;padding:0;line-height:1">&times;</button></div>'
    + '<div style="padding:16px 20px;flex:1;overflow-y:auto;display:flex;flex-direction:column;gap:10px">'
    + fromSelect
    + '<input id="composeTo" type="email" placeholder="To (email address)" style="width:100%;padding:8px 12px;background:var(--pb-bg);color:var(--pb-text);border:1px solid var(--pb-border);border-radius:6px;font-size:13px;font-family:inherit;box-sizing:border-box">'
    + '<input id="composeSubject" type="text" placeholder="Subject" style="width:100%;padding:8px 12px;background:var(--pb-bg);color:var(--pb-text);border:1px solid var(--pb-border);border-radius:6px;font-size:13px;font-family:inherit;box-sizing:border-box">'
    + '<textarea id="composeBody" placeholder="Write your message..." style="width:100%;min-height:160px;background:var(--pb-bg);color:var(--pb-text);border:1px solid var(--pb-border);border-radius:6px;padding:10px 12px;font-size:13px;font-family:inherit;resize:vertical;box-sizing:border-box"></textarea>'
    + '</div>'
    + '<div style="padding:12px 20px;border-top:1px solid var(--pb-border);display:flex;justify-content:flex-end;gap:8px">'
    + '<button onclick="document.getElementById(\'composeOverlay\').remove()" style="padding:8px 16px;border-radius:6px;border:1px solid var(--pb-border);background:none;color:var(--pb-text-muted);font-size:12px;font-family:inherit;cursor:pointer">Cancel</button>'
    + '<button id="composeSendBtn" onclick="_sendComposedEmail()" style="padding:8px 20px;border-radius:6px;border:none;background:var(--pb-blue);color:#fff;font-size:12px;font-family:inherit;cursor:pointer;font-weight:600">Send</button>'
    + '</div></div>';
  document.body.appendChild(overlay);
  document.getElementById('composeTo').focus();
}

function _sendComposedEmail() {
  var to = document.getElementById('composeTo').value.trim();
  var subject = document.getElementById('composeSubject').value.trim();
  var text = document.getElementById('composeBody').value.trim();
  if (!to || !text) { showToast('To and message body are required'); return; }
  var sendAccountIdx = activeAccountIdx;
  var fromEl = document.getElementById('composeFrom');
  if (fromEl) sendAccountIdx = parseInt(fromEl.value, 10);
  var btn = document.getElementById('composeSendBtn');
  btn.textContent = 'Sending...';
  btn.disabled = true;
  fetch('/api/inbox/send?account=' + sendAccountIdx, {
    method: 'POST',
    headers: { 'Authorization': 'Bearer ' + localStorage.getItem('portal_token'), 'Content-Type': 'application/json' },
    body: JSON.stringify({ to: to, subject: subject, text: text })
  })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.error) throw new Error(data.error);
      showToast('Email sent to ' + to);
      document.getElementById('composeOverlay').remove();
      syncInbox();
    })
    .catch(function(err) {
      showToast('Send failed: ' + err.message);
      btn.textContent = 'Send';
      btn.disabled = false;
    });
}

// ── Inbox init on page load ──────────────────────────────────────────────
function _initInbox() {
  _inboxInitDone = true;
  fetch('/api/inbox/status', { headers: { 'Authorization': 'Bearer ' + localStorage.getItem('portal_token') } })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      emailAccounts = data.accounts || [];
      _renderAccountTabs();
      if (data.configured) {
        emailConfigured = true;
        emailAddress = data.email;
        document.querySelector('.inbox-status-dot').style.background = 'var(--pb-green)';
        var banner = document.querySelector('.todo-email-banner');
        if (banner) {
          banner.innerHTML = '<span style="color:var(--pb-green);font-weight:700">\u2713 Email connected</span><span style="color:var(--pb-text-dim);margin-left:8px">' + escHtml(data.email) + '</span>';
          banner.style.borderColor = 'rgba(34,197,94,0.2)';
          banner.style.background = 'rgba(34,197,94,0.04)';
        }
        // Use cache if available, otherwise show loading and fetch
        var cached = _threadCache[activeAccountIdx];
        if (cached && cached.threads && (Date.now() - cached.timestamp < _getCacheTTL())) {
          _inboxThreads = cached.threads;
          _renderThreadList(_inboxThreads);
          var age = Math.floor((Date.now() - cached.timestamp) / 1000);
          var agoText = age < 60 ? 'just now' : Math.floor(age / 60) + 'm ago';
          document.querySelector('.inbox-status-text').textContent = emailAddress + ' \u00B7 synced ' + agoText;
        } else {
          document.querySelector('.inbox-status-text').textContent = data.email + ' \u00B7 loading...';
          _showInboxLoading();
          syncInbox();
        }
        var setupBtn = document.getElementById('inboxSetupBtn');
        if (setupBtn) setupBtn.style.display = 'none';
      } else {
        document.querySelector('.inbox-status-text').textContent = 'Not connected';
        var setupBtn = document.getElementById('inboxSetupBtn');
        if (setupBtn) setupBtn.style.display = '';
      }
    })
    .catch(function(err) {
      _inboxInitDone = false; // Allow retry on next tab switch
      document.querySelector('.inbox-status-text').textContent = 'Connection error \u2014 tap Sync to retry';
      document.querySelector('.inbox-status-dot').style.background = 'var(--pb-red, #ef4444)';
    });
}

// ── Sync timestamp refresh (every 30s) ──────────────────────────────────
setInterval(function() {
  if (!_lastSyncTime || !emailConfigured) return;
  var age = Math.floor((Date.now() - _lastSyncTime) / 1000);
  var statusText = document.querySelector('.inbox-status-text');
  if (!statusText) return;
  var agoText = age < 60 ? 'just now' : age < 3600 ? Math.floor(age / 60) + 'm ago' : Math.floor(age / 3600) + 'h ago';
  statusText.textContent = _inboxEmail + ' \u00B7 synced ' + agoText;
}, 30000);

// === Background inbox polling for notifications ===
// Checks for new emails periodically even when not on the inbox tab
(function _startInboxBackgroundPoll() {
  function _bgPoll() {
    if (!localStorage.getItem('portal_token')) return;
    if (_getCacheTTL() === Infinity) return;
    // Initialize inbox silently if not done yet (so prevCount gets set)
    if (!_inboxInitDone) {
      _inboxInitDone = true;
      syncInbox(true);
      return;
    }
    syncInbox(true);
  }
  // Start polling after 15s (gives page time to load), then every 2 minutes
  setTimeout(function() {
    _bgPoll();
    setInterval(_bgPoll, 120000);
  }, 15000);
})();

// === Expose globals for onclick handlers in HTML ===
window.openEmailWizard = openEmailWizard;
window.closeEmailWizard = closeEmailWizard;
window.openConfirmModal = openConfirmModal;
window.closeConfirmModal = closeConfirmModal;
window.ewGoTo = ewGoTo;
window.ewNext = ewNext;
window.switchChatView = switchChatView;
window._restorePromotedPanels = _restorePromotedPanels;
window.syncInbox = syncInbox;
window.openThread = openThread;
window.closeEmailDetail = closeEmailDetail;
window.toggleEmailActions = toggleEmailActions;
window.mailoDraft = mailoDraft;
window.filterInbox = filterInbox;
window.searchInbox = searchInbox;
window.handleComposeEmail = handleComposeEmail;
window._initInbox = _initInbox;
window.markAllRead = markAllRead;
window._sendReply = _sendReply;
window._openReplyComposer = _openReplyComposer;
window.switchAccount = switchAccount;
window.removeAccount = removeAccount;
window._showEmailImages = _showEmailImages;
})();
