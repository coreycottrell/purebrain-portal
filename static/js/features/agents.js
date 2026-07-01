(function(){
  'use strict';

  // ── Helpers ──────────────────────────────────────────────────────
  // _escH: alias for global escHtml
  var _escH = escHtml;

  // _authHeaders: delegates to shared _auth() from auth.js
  function _authHeaders(){ return _auth(); }

  function _fmtRelTime(isoStr){
    if(!isoStr) return '';
    try {
      var d = new Date(isoStr + (isoStr.endsWith('Z') ? '' : 'Z'));
      var diff = Math.floor((Date.now() - d.getTime()) / 1000);
      if(diff < 0) return 'just now';
      if(diff < 60)  return diff + 's ago';
      if(diff < 3600) return Math.floor(diff/60) + 'm ago';
      if(diff < 86400) return Math.floor(diff/3600) + 'h ago';
      return Math.floor(diff/86400) + 'd ago';
    } catch(e){ return ''; }
  }

  function _sortKey(a){
    var s = a.status || 'idle';
    if(s === 'active')  return 0;
    if(s === 'working') return 1;
    if(s === 'offline') return 3;
    return 2;
  }

  function _initials(name){
    if(!name) return '??';
    return name.split(/[-_\s]+/).map(function(w){ return w[0] ? w[0].toUpperCase() : ''; }).join('').slice(0,2);
  }

  // Type -> color mappings (matches Vortex design tokens)
  var _TYPE_COLORS = {
    'orchestration':'#a855f7','core':'#22c55e','specialist':'#2a93c1',
    'pipeline':'#fbbf24','governance':'#ef4444','lead':'#f97316',
    'conductor':'#a855f7','primary':'#a855f7'
  };
  var _TYPE_GLOWS = {
    'orchestration':'glow-purple','core':'glow-green','specialist':'glow-blue',
    'pipeline':'glow-yellow','governance':'glow-red','lead':'glow-orange',
    'conductor':'glow-purple','primary':'glow-purple'
  };

  // ── State ────────────────────────────────────────────────────────
  var _allAgents = [];
  var _liveAgentIds = new Set();
  var _agentsLoaded = false;
  var _refreshTimer = null;
  var _liveTimer = null;

  // ── Card Builder ─────────────────────────────────────────────────
  // Builds a flip-card from an API agent object.
  // API shape: { id, name, description, type, status, capabilities, department,
  //              is_lead, last_active, created_at, current_task, last_completed }
  function _buildAgentCard(a, opts){
    opts = opts || {};
    var id = a.id || '';
    var name = a.name || id;
    var type = a.type || 'specialist';
    var status = a.status || 'idle';
    var desc = a.description || '';
    var caps = a.capabilities || [];
    var dept = a.department || '';
    var task = a.current_task || '';
    var lastActive = a.last_active || '';
    var created = a.created_at || '';
    var lastCompleted = a.last_completed || '';
    var isLead = a.is_lead;

    var isLive = (status === 'active' || status === 'working');
    var color = _TYPE_COLORS[type] || '#2a93c1';
    var glow = _TYPE_GLOWS[type] || 'glow-blue';
    var init = _initials(name);
    var statusLabel = status.charAt(0).toUpperCase() + status.slice(1);

    // Build skills HTML
    var skillsHtml = '';
    if(caps && caps.length){
      for(var c=0; c<caps.length; c++){
        skillsHtml += '<span class="ar-back-skill">' + _escH(caps[c]) + '</span>';
      }
    }

    // Action buttons
    var assignBtn, chatBtn;
    if(isLive){
      assignBtn = '<button class="ar-action-btn primary" onclick="event.stopPropagation();openAssignTask(\'' + _escH(id) + '\',\'' + _escH(type) + '\',true)">Assign Task</button>';
    } else {
      assignBtn = '<button class="ar-action-btn primary" onclick="event.stopPropagation();wakeAgent(\'' + _escH(id) + '\',this.closest(\'.ar-flip\'))">Wake</button>';
    }
    chatBtn = '<button class="ar-action-btn secondary" onclick="event.stopPropagation();chatWithAgent(\'' + _escH(id) + '\')">Chat</button>';

    var html = '<div class="ar-flip" data-agent="' + _escH(id) + '" data-status="' + _escH(status) + '" data-type="' + _escH(type) + '">';
    if(opts.selectable){
      html += '<div class="ar-card-select" data-agent="' + _escH(id) + '" onclick="event.stopPropagation();this.classList.toggle(\'checked\');updateBatchBar()"></div>';
    }
    html += '<div class="ar-flip-inner">';

    // ── FRONT ──
    html += '<div class="ar-flip-front"><div class="ar-card ' + glow + '">';
    html += '<div class="ar-flip-btn" onclick="event.stopPropagation();this.closest(\'.ar-flip\').classList.toggle(\'flipped\')" title="More info">i</div>';
    html += '<div class="ar-card-top">';
    html += '<div class="ar-hex-av" style="background:' + color + '">' + _escH(init) + '</div>';
    html += '<div class="ar-card-info"><div class="ar-card-name">' + _escH(name) + '</div>';
    html += '<div class="ar-card-meta"><span class="ar-dot st-' + _escH(status) + '"></span><span>' + _escH(statusLabel) + '</span>';
    html += '<span class="ar-type-badge t-' + _escH(type) + '">' + _escH(type) + '</span></div></div></div>';

    // Description
    if(desc){
      html += '<div class="ar-back-section" style="margin-bottom:6px"><div class="ar-back-label">Description</div><div class="ar-card-desc" style="margin-bottom:0">' + _escH(desc) + '</div></div>';
    }
    // Current task (only if active/working)
    if(isLive && task){
      html += '<div class="ar-back-section" style="margin-bottom:6px"><div class="ar-back-label">Current Task</div><div class="ar-task-line" style="margin-bottom:0">' + _escH(task) + '</div></div>';
    }
    // Last completed (only if idle/offline)
    if(!isLive && lastCompleted){
      html += '<div class="ar-back-section" style="margin-bottom:6px"><div class="ar-back-label">Last Completed</div><div class="ar-front-meta" style="margin-bottom:0"><span class="meta-item">' + _escH(_fmtRelTime(lastCompleted)) + '</span></div></div>';
    }
    // Last active
    if(lastActive){
      html += '<div class="ar-back-section" style="margin-bottom:6px"><div class="ar-back-label">Last Active</div><div class="ar-front-meta" style="margin-bottom:0"><span class="meta-item">' + _escH(_fmtRelTime(lastActive)) + '</span></div></div>';
    }
    // Skills
    if(caps && caps.length){
      html += '<div class="ar-back-section" style="margin-bottom:8px"><div class="ar-back-label">Skills (' + caps.length + ')</div><div class="ar-back-skills">' + skillsHtml + '</div></div>';
    }
    // Actions
    html += '<div class="ar-card-actions">' + assignBtn + chatBtn + '</div>';
    html += '</div></div>';

    // ── BACK ──
    html += '<div class="ar-flip-back"><div class="ar-back">';
    html += '<div class="ar-flip-btn" onclick="event.stopPropagation();this.closest(\'.ar-flip\').classList.toggle(\'flipped\')" title="Back">&larr;</div>';
    html += '<div class="ar-back-head"><div class="ar-hex-av" style="background:' + color + ';width:32px;height:32px;font-size:10px">' + _escH(init) + '</div>';
    html += '<div><div class="ar-back-name">' + _escH(name) + '</div><div class="ar-back-id">ID: ' + _escH(id) + '</div></div></div>';

    // Stats on back
    html += '<div class="ar-back-stats-6">';
    html += '<div class="ar-back-stat"><div class="ar-back-stat-val">' + _escH(statusLabel) + '</div><div class="ar-back-stat-label">Status</div></div>';
    html += '<div class="ar-back-stat"><div class="ar-back-stat-val">' + _escH(type) + '</div><div class="ar-back-stat-label">Role</div></div>';
    html += '<div class="ar-back-stat"><div class="ar-back-stat-val">' + (isLead ? 'Yes' : 'No') + '</div><div class="ar-back-stat-label">Lead</div></div>';
    if(created){
      html += '<div class="ar-back-stat"><div class="ar-back-stat-val">' + _escH(created.slice(0,10)) + '</div><div class="ar-back-stat-label">Created</div></div>';
    }
    html += '</div>';

    // Department
    if(dept){
      html += '<div class="ar-back-section"><div class="ar-back-label">Department</div><div class="ar-back-tags"><span class="ar-back-tag">' + _escH(dept) + '</span></div></div>';
    }
    // Full skills list on back
    if(caps && caps.length){
      html += '<div class="ar-back-section"><div class="ar-back-label">All Skills (' + caps.length + ')</div><div class="ar-back-skills">' + skillsHtml + '</div></div>';
    }

    html += '</div></div>'; // close ar-back + ar-flip-back
    html += '</div></div>'; // close ar-flip-inner + ar-flip

    return html;
  }

  // ── Renderers ────────────────────────────────────────────────────
  function _renderActiveAgentsGrid(agents){
    var grid = document.getElementById('active-agents-grid');
    if(!grid) return;

    // Filter to only active/working agents
    var active = agents.filter(function(a){ return a.status === 'active' || a.status === 'working'; });

    if(!active.length){
      grid.innerHTML = '<div style="padding:24px;color:var(--pb-text-dim);text-align:center;font-size:13px;width:100%">No active agents right now. All agents are idle.</div>';
      return;
    }

    var html = '';
    active.sort(function(a,b){ return _sortKey(a) - _sortKey(b); });
    for(var i=0; i<active.length; i++){
      html += _buildAgentCard(active[i], { selectable: false });
    }
    grid.innerHTML = html;
  }

  function _renderRosterGrid(agents, gridId){
    var grid = document.getElementById(gridId);
    if(!grid) return;

    if(!agents || !agents.length){
      grid.innerHTML = '<div style="padding:24px;color:var(--pb-text-dim);text-align:center;font-size:13px;width:100%">No agents found.</div>';
      return;
    }

    var sorted = agents.slice().sort(function(a,b){ return _sortKey(a) - _sortKey(b); });
    var html = '';
    for(var i=0; i<sorted.length; i++){
      html += _buildAgentCard(sorted[i], { selectable: true });
    }
    grid.innerHTML = html;
  }

  function _updateStats(agents){
    var counts = { active:0, working:0, idle:0, offline:0, total: agents.length };
    for(var i=0; i<agents.length; i++){
      var s = agents[i].status || 'idle';
      if(counts[s] !== undefined) counts[s]++;
    }

    // Active agents summary
    var el;
    el = document.getElementById('aa-stat-active');  if(el) el.textContent = counts.active + counts.working;
    el = document.getElementById('aa-stat-working'); if(el) el.textContent = counts.working;
    el = document.getElementById('aa-stat-idle');    if(el) el.textContent = counts.idle;
    el = document.getElementById('aa-stat-total');   if(el) el.textContent = counts.total;

    // Roster stats (both copies)
    el = document.getElementById('ar1-total');  if(el) el.textContent = counts.total;
    el = document.getElementById('ar1-active'); if(el) el.textContent = counts.active + counts.working;
    el = document.getElementById('ar1-idle');   if(el) el.textContent = counts.idle;
    el = document.getElementById('ar2-total');  if(el) el.textContent = counts.total;
    el = document.getElementById('ar2-active'); if(el) el.textContent = counts.active + counts.working;
    el = document.getElementById('ar2-idle');   if(el) el.textContent = counts.idle;
  }

  // ── API Calls ────────────────────────────────────────────────────
  function _loadAgents(opts){
    opts = opts || {};
    var params = new URLSearchParams();
    if(opts.type)   params.set('type', opts.type);
    if(opts.status) params.set('status', opts.status);
    if(opts.search) params.set('search', opts.search);
    var url = '/api/agents' + (params.toString() ? '?' + params.toString() : '');

    return fetch(url, { headers: _authHeaders() })
      .then(function(r){
        if(!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .then(function(d){
        _allAgents = d.agents || [];
        _agentsLoaded = true;

        // Render into all grids
        _renderActiveAgentsGrid(_allAgents);
        _renderRosterGrid(_allAgents, 'ar-grid');
        _renderRosterGrid(_allAgents, 'ar-grid-2');
        _updateStats(_allAgents);

        return _allAgents;
      })
      .catch(function(e){
        var grids = ['active-agents-grid','ar-grid','ar-grid-2'];
        for(var g=0; g<grids.length; g++){
          var el = document.getElementById(grids[g]);
          if(el) el.innerHTML = '<div style="padding:24px;color:#ef4444;text-align:center;font-size:13px;width:100%">Failed to load agents: ' + _escH(e.message) + '</div>';
        }
      });
  }

  function _loadLiveAgents(){
    fetch('/api/hub/live-agents', { headers: _authHeaders() })
      .then(function(r){
        if(!r.ok) return { agents: [] };
        return r.json();
      })
      .then(function(d){
        var live = d.agents || [];
        _liveAgentIds = new Set();
        for(var i=0; i<live.length; i++){
          if(live[i].id) _liveAgentIds.add(live[i].id);
        }
        // Update status dots for live agents across all grids
        document.querySelectorAll('.ar-flip[data-agent]').forEach(function(flip){
          var agId = flip.getAttribute('data-agent');
          if(_liveAgentIds.has(agId)){
            flip.setAttribute('data-status', 'active');
            var dot = flip.querySelector('.ar-dot');
            if(dot) dot.className = 'ar-dot st-active';
            var meta = flip.querySelector('.ar-card-meta span:nth-child(2)');
            if(meta) meta.textContent = 'Active';
          }
        });
      })
      .catch(function(){ /* silent */ });
  }

  // ── Filter Wiring ───────────────────────────────────────────────
  // Roster grid filter (works on DOM elements already rendered)
  function _wireFilter(searchId, typeId, statusId, gridId){
    var searchEl = document.getElementById(searchId);
    var typeEl   = document.getElementById(typeId);
    var statusEl = document.getElementById(statusId);

    function doFilter(){
      var q = (searchEl ? searchEl.value : '').toLowerCase();
      var t = typeEl ? typeEl.value : '';
      var s = statusEl ? statusEl.value : '';
      var grid = document.getElementById(gridId);
      if(!grid) return;
      grid.querySelectorAll('.ar-flip').forEach(function(flip){
        var name   = (flip.querySelector('.ar-card-name') || {}).textContent || '';
        var desc   = (flip.querySelector('.ar-card-desc') || {}).textContent || '';
        var aType  = flip.getAttribute('data-type') || '';
        var aStatus = flip.getAttribute('data-status') || '';
        var matchQ = !q || name.toLowerCase().indexOf(q) !== -1 || desc.toLowerCase().indexOf(q) !== -1;
        var matchT = !t || aType === t;
        var matchS = !s || aStatus === s;
        flip.style.display = (matchQ && matchT && matchS) ? '' : 'none';
      });
    }

    if(searchEl) searchEl.addEventListener('input', doFilter);
    if(typeEl)   typeEl.addEventListener('change', doFilter);
    if(statusEl) statusEl.addEventListener('change', doFilter);
  }

  // ── View Toggle Wiring ──────────────────────────────────────────
  function _wireViewToggle(){
    document.querySelectorAll('.ar-view-btn').forEach(function(btn){
      btn.addEventListener('click', function(){
        var parent = btn.closest('.ar-header') || btn.closest('.agents-area');
        if(!parent) return;
        parent.querySelectorAll('.ar-view-btn').forEach(function(b){ b.classList.remove('active'); });
        btn.classList.add('active');
        // Find the nearest ar-grid sibling
        var area = btn.closest('.agents-subtab-content') || btn.closest('[style*="flex-direction:column"]');
        if(!area) return;
        var grid = area.querySelector('.ar-grid');
        if(!grid) return;
        if(btn.getAttribute('data-arview') === 'list') grid.classList.add('list-view');
        else grid.classList.remove('list-view');
      });
    });
  }

  // ── Boot ─────────────────────────────────────────────────────────
  function _boot(){
    var token = localStorage.getItem('portal_token');
    if(!token) return;

    // Initial load
    _loadAgents();
    _loadLiveAgents();

    // Wire filters for both roster grids
    _wireFilter('ar-search',   'ar-filter-type',   'ar-filter-status',   'ar-grid');
    _wireFilter('ar-search-2', 'ar-filter-type-2', 'ar-filter-status-2', 'ar-grid-2');
    _wireViewToggle();

    // Auto-refresh every 30s
    _refreshTimer = setInterval(function(){
      // Only refresh if agents or agent-roster tab is visible
      var agPanel = document.getElementById('agentsArea');
      var arPanel = document.getElementById('agent-rosterArea');
      var agVis = agPanel && (agPanel.classList.contains('visible') || agPanel.style.display === 'flex');
      var arVis = arPanel && (arPanel.classList.contains('visible') || arPanel.style.display === 'flex');
      if(agVis || arVis) _loadAgents();
    }, 30000);

    // Live agents polling every 10s
    _liveTimer = setInterval(_loadLiveAgents, 20000);
  }

  // Listen for token changes and boot
  if(localStorage.getItem('portal_token')){
    // Token already present -- boot on DOMContentLoaded or immediately
    if(document.readyState === 'loading'){
      document.addEventListener('DOMContentLoaded', _boot);
    } else {
      // Small delay to ensure DOM is ready after other IIFEs
      setTimeout(_boot, 200);
    }
  }
  // Listen for auth event from the chat IIFE (login flow)
  window.addEventListener('portal-auth', function(e) {
    if (e.detail && e.detail.token && !_agentsLoaded) {
      setTimeout(_boot, 300);
    }
  });

  // Expose for panel switching hook
  window._loadAgentsPanel = function(){ if(!_agentsLoaded) _boot(); else _loadAgents(); };

})();
