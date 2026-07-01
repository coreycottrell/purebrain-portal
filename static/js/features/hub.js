(function(){
  'use strict';

  var CIRC = 2 * Math.PI * 22; // r=22 for all ring SVGs

  function _esc(s){ return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }
  function _relTime(iso){
    if(!iso) return '--';
    try {
      var d = new Date(iso.endsWith('Z') ? iso : iso+'Z');
      var diff = Math.floor((Date.now() - d.getTime()) / 1000);
      if(diff < 0) return 'just now';
      if(diff < 60) return diff+'s ago';
      if(diff < 3600) return Math.floor(diff/60)+'m ago';
      if(diff < 86400) return Math.floor(diff/3600)+'h ago';
      return Math.floor(diff/86400)+'d ago';
    } catch(e){ return '--'; }
  }
  function _fmtUptime(sec){
    if(!sec || sec < 0) return '--';
    var d = Math.floor(sec / 86400);
    var h = Math.floor((sec % 86400) / 3600);
    var m = Math.floor((sec % 3600) / 60);
    var parts = [];
    if(d) parts.push(d + 'd');
    if(h || d) parts.push(h + 'h');
    parts.push(m + 'm');
    return parts.join(' ');
  }
  function _setRing(id, pct){
    var el = document.getElementById(id);
    if(el) el.setAttribute('stroke-dashoffset', (CIRC * (1 - Math.min(pct,100)/100)).toFixed(1));
  }
  function _setText(id, txt){
    var el = document.getElementById(id);
    if(el) el.textContent = txt;
  }

  function _refreshHub(){
    if(!_tok()) return;
    _loadHubStatus();
    _loadHubLiveAgents();
    _loadRecentActivity();
    _loadBoopStatus();
    _loadBoopConfig();
    _loadContextForHub();
    _loadSystemStats();
    _loadIntegrations();
  }

  // Hero + Uptime from /api/status
  function _loadHubStatus(){
    fetch('/api/status', { headers: _auth() })
      .then(function(r){ return r.json(); })
      .then(function(d){
        // CIV name
        var name = d.civ || 'Portal';
        var display = name.charAt(0).toUpperCase() + name.slice(1);
        _setText('hub-civ-name', display);
        _setText('hub-avatar-letter', display.charAt(0).toUpperCase());
        // Uptime
        _setText('hub-uptime-val', _fmtUptime(d.uptime));
        // Claude service LED
        var led = document.getElementById('hub-svc-claude-led');
        var val = document.getElementById('hub-svc-claude-val');
        if(led) led.className = 'hub-svc-led ok';
        if(val) val.textContent = d.claude_running ? 'running' : 'idle';
      }).catch(function(){
        var led = document.getElementById('hub-svc-claude-led');
        var val = document.getElementById('hub-svc-claude-val');
        if(led) led.className = 'hub-svc-led warn';
        if(val) val.textContent = 'unreachable';
      });
  }

  // System stats — Memory, CPU, Disk from /api/system/stats
  function _loadSystemStats(){
    fetch('/api/system/stats', { headers: _auth() })
      .then(function(r){ return r.json(); })
      .then(function(d){
        // Memory gauge
        var memPct = d.memory_total_gb > 0 ? Math.round(d.memory_used_gb / d.memory_total_gb * 100) : 0;
        _setRing('hub-ring-memory', memPct);
        _setText('hub-pct-memory', memPct);
        _setText('hub-sub-memory', d.memory_used_gb + ' / ' + d.memory_total_gb + ' GB');
        // CPU gauge — show load as percentage of 100% (1.0 = 100% of one core, cap display at 100)
        var cpuPct = Math.min(Math.round(d.cpu_load * 100), 100);
        _setRing('hub-ring-cpu', cpuPct);
        _setText('hub-pct-cpu', cpuPct);
        _setText('hub-sub-cpu', 'load ' + d.cpu_load);
        // Disk gauge
        var diskPct = d.disk_total_gb > 0 ? Math.round(d.disk_used_gb / d.disk_total_gb * 100) : 0;
        _setRing('hub-ring-disk', diskPct);
        _setText('hub-pct-disk', diskPct);
        _setText('hub-sub-disk', d.disk_used_gb + ' / ' + d.disk_total_gb + ' GB');
      }).catch(function(){});
  }

  function _loadHubLiveAgents(){
    fetch('/api/cc/proxy/api/chat/channels', { headers: _auth() })
      .then(function(r){ return r.json(); })
      .then(function(channels){
        if(!Array.isArray(channels)) return;
        var agentMap = {};
        channels.forEach(function(ch){
          var lm = ch.last_message;
          if(!lm || !lm.sender_name || lm.sender_type !== 'ai') return;
          var msgTime = new Date(lm.created_at).getTime();
          if(Date.now() - msgTime > 60 * 60 * 1000) return;
          if(!agentMap[lm.sender_name] || msgTime > agentMap[lm.sender_name].time){
            agentMap[lm.sender_name] = { name: lm.sender_name, time: msgTime, channel: ch.name };
          }
        });
        var agents = Object.values(agentMap).sort(function(a,b){ return b.time - a.time; });
        _setText('hub-live-count', agents.length);
        var list = document.getElementById('hub-live-agents-list');
        if(!list) return;
        if(!agents.length){
          list.innerHTML = '<div style="padding:8px 0;color:var(--pb-text-dim);font-size:11px">No active agents in last 60 min</div>';
          return;
        }
        var html = '';
        agents.forEach(function(a){
          var ago = _relTime(new Date(a.time).toISOString());
          html += '<div class="hub-agent-row"><div class="hub-agent-dot live"></div><span class="hub-agent-name">'+_esc(a.name)+'</span><span class="hub-agent-time">'+_esc(ago)+'</span></div>';
        });
        list.innerHTML = html;
      }).catch(function(){
        fetch('/api/hub/live-agents', { headers: _auth() })
          .then(function(r){ return r.json(); })
          .then(function(data){
            var agents = data.agents || [];
            _setText('hub-live-count', agents.length);
            var list = document.getElementById('hub-live-agents-list');
            if(!list) return;
            if(!agents.length){
              list.innerHTML = '<div style="padding:8px 0;color:var(--pb-text-dim);font-size:11px">No active sub-agents</div>';
              return;
            }
            var html = '';
            agents.forEach(function(a){
              html += '<div class="hub-agent-row"><div class="hub-agent-dot live"></div><span class="hub-agent-name">'+_esc(a.description||a.id||'Agent')+'</span><span class="hub-agent-time">live</span></div>';
            });
            list.innerHTML = html;
          }).catch(function(){});
      });
  }

  var _CATEGORY_COLORS = {agent:'blue',task:'green',update:'amber',chat:'blue',file:'green',system:'gray'};

  function _loadRecentActivity(){
    fetch('/api/activity?limit=8', { headers: _auth() })
      .then(function(r){ return r.json(); })
      .then(function(data){
        var items = data.activity;
        if(!Array.isArray(items) || !items.length) return;
        var el = document.getElementById('hub-recent-activity');
        if(!el) return;
        var html = '';
        items.forEach(function(item){
          var time = '';
          if(item.ts){
            try {
              var d = new Date(item.ts);
              time = d.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
            } catch(e){}
          }
          var color = _CATEGORY_COLORS[item.category] || 'gray';
          var text = _esc(item.action||'');
          if(item.detail) text += ' <span style="opacity:0.6">'+_esc(item.detail)+'</span>';
          html += '<div class="hub-activity-item '+color+'"><span class="hub-act-time">'+_esc(time)+'</span><span class="hub-act-text">'+text+'</span></div>';
        });
        el.innerHTML = html;
      }).catch(function(){});
  }

  function _loadBoopStatus(){
    fetch('/api/boop/status', { headers: _auth() })
      .then(function(r){ return r.json(); })
      .then(function(d){
        var led = document.getElementById('hub-svc-boop-led');
        var val = document.getElementById('hub-svc-boop-val');
        if(led) led.className = 'hub-svc-led ' + (d.active ? 'ok' : 'warn');
        if(val) val.textContent = d.active ? 'running (PID '+d.pid+')' : 'stopped';
      }).catch(function(){});
  }

  function _loadBoopConfig(){
    fetch('/api/boop/config', { headers: _auth() })
      .then(function(r){ return r.json(); })
      .then(function(d){
        _setText('hub-stat-boop', (d.cadence_minutes||30)+'m');
      }).catch(function(){});
  }

  function _loadContextForHub(){
    fetch('/api/context', { headers: _auth() })
      .then(function(r){ return r.json(); })
      .then(function(d){
        var pct = d.percent || d.pct || 0;
        // Context gauge
        _setRing('hub-ring-context', pct);
        _setText('hub-pct-context', Math.round(pct));
        if(d.used_tokens) _setText('hub-sub-context', Math.round(d.used_tokens/1000)+'k / '+(d.max_tokens ? Math.round(d.max_tokens/1000)+'k' : '1M'));
        // Token stat
        if(d.used_tokens) _setText('hub-stat-tokens', Math.round(d.used_tokens/1000)+'k');
        // Message count
        if(d.message_count !== undefined) _setText('hub-stat-messages', d.message_count.toLocaleString());
      }).catch(function(){});
  }

  // Connected APIs from /api/integrations/status
  function _loadIntegrations(){
    fetch('/api/integrations/status', { headers: _auth() })
      .then(function(r){ return r.json(); })
      .then(function(d){
        var items = d.integrations || [];
        _setText('hub-apis-count', '(' + d.active + '/' + d.total + ' active)');
        var listEl = document.getElementById('hub-apis-list');
        if(!listEl) return;
        if(!items.length){
          listEl.innerHTML = '<div style="padding:12px 0;color:var(--pb-text-dim);font-size:11px">No integrations found</div>';
          return;
        }
        var html = '';
        items.forEach(function(it){
          var ok = it.status === 'active';
          var cls = ok ? 'ok' : 'warn';
          html += '<div class="hub-api-card">';
          html += '<div class="hub-api-top"><span class="hub-api-dot '+cls+'"></span><span class="hub-api-name">'+_esc(it.name)+'</span><span class="hub-api-badge '+cls+'">'+_esc(it.status)+'</span></div>';
          html += '<div class="hub-api-desc">'+_esc(it.desc)+'</div>';
          html += '<div class="hub-api-meta"><span class="hub-api-detail">'+_esc(it.detail)+'</span></div>';
          html += '</div>';
        });
        listEl.innerHTML = html;
      }).catch(function(){});
  }

  // Quick action buttons
  function _wireHubActions(){
    var btnRestart = document.getElementById('hub-btn-restart');
    if(btnRestart) btnRestart.onclick = function(){
      if(!confirm('Restart the AI agent? This will start a fresh session.')) return;
      fetch('/api/restart', { method:'POST', headers:_auth() })
        .then(function(r){ return r.json(); })
        .then(function(d){ showToast(d.ok ? 'Agent restarting...' : 'Failed: '+(d.error||'?')); })
        .catch(function(e){ showToast('Restart failed: '+e.message); });
    };
    var btnContinue = document.getElementById('hub-btn-continue');
    if(btnContinue) btnContinue.onclick = function(){
      fetch('/api/continue', { method:'POST', headers:_auth() })
        .then(function(r){ return r.json(); })
        .then(function(d){ showToast(d.ok ? 'Session continuing...' : 'Failed: '+(d.error||'?')); })
        .catch(function(e){ showToast('Continue failed: '+e.message); });
    };
    var btnResume = document.getElementById('hub-btn-resume');
    if(btnResume) btnResume.onclick = function(){
      fetch('/api/resume', { method:'POST', headers:_auth() })
        .then(function(r){ return r.json(); })
        .then(function(d){ showToast(d.ok ? 'Session resumed...' : 'Failed: '+(d.error||'?')); })
        .catch(function(e){ showToast('Resume failed: '+e.message); });
    };
    var btnCompact = document.getElementById('hub-btn-compact');
    if(btnCompact) btnCompact.onclick = function(){
      showToast('Sending /compact command...');
      fetch('/api/chat/send', { method:'POST', headers:_authJson(), body: JSON.stringify({ message: '/compact' }) })
        .then(function(){ showToast('Compact requested'); })
        .catch(function(e){ showToast('Failed: '+e.message); });
    };
    var btnDiag = document.getElementById('hub-btn-diagnostics');
    if(btnDiag) btnDiag.onclick = function(){
      showToast('Running diagnostics...');
      fetch('/api/debug/report', { method:'POST', headers:_auth() })
        .then(function(r){ return r.json(); })
        .then(function(d){ showToast('Diagnostics complete'); })
        .catch(function(e){ showToast('Failed: '+e.message); });
    };
  }

  // Boot: auto-load when token available
  window.addEventListener('portal-auth', function(){
    setTimeout(function(){ _refreshHub(); _wireHubActions(); }, 600);
  });
  if(_tok()) setTimeout(function(){ _refreshHub(); _wireHubActions(); }, 1600);

  // Expose API
  window._portalHub = { refresh: _refreshHub };

})();
