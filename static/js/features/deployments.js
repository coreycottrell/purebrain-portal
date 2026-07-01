(function(){
  'use strict';

  // ── Helpers ──────────────────────────────────────────────────────
  var _escH = escHtml;

  function _authHeaders(){ return _auth(); }

  // ── Status Badge Class Map ─────────────────────────────────────
  var _STATUS_CLASSES = {
    'live':        'dep-status-live',
    'staging':     'dep-status-staging',
    'maintenance': 'dep-status-maintenance',
    'offline':     'dep-status-offline'
  };

  // ── State ──────────────────────────────────────────────────────
  var _deployments = [];
  var _deploymentsLoaded = false;
  var _editingId = null;       // null = adding, string = editing
  var _modalInjected = false;

  // ── SVG Icons ──────────────────────────────────────────────────
  var _visitSvg = '<svg viewBox="0 0 24 24" style="width:14px;height:14px;stroke:currentColor;fill:none;stroke-width:2;vertical-align:middle;margin-right:4px"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>';

  var _rocketSvg = '<svg viewBox="0 0 24 24" style="width:18px;height:18px;stroke:var(--pb-blue);fill:none;stroke-width:2"><path d="M4.5 16.5c-1.5 1.26-2 5-2 5s3.74-.5 5-2c.71-.84.7-2.13-.09-2.91a2.18 2.18 0 0 0-2.91-.09z"/><path d="M12 15l-3-3a22 22 0 0 1 2-3.95A12.88 12.88 0 0 1 22 2c0 2.72-.78 7.5-6 11a22.35 22.35 0 0 1-4 2z"/></svg>';

  var _addSvg = '<svg viewBox="0 0 24 24" style="width:16px;height:16px;stroke:currentColor;fill:none;stroke-width:2;vertical-align:middle;margin-right:4px"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>';

  // ── Modal Injection ────────────────────────────────────────────
  function _ensureModal(){
    if(_modalInjected) return;

    var html = '<div class="modal-overlay" id="deploy-modal" style="display:none">';
    html += '<div class="modal" style="max-width:520px">';
    html += '<h2 id="dep-modal-title" style="display:flex;align-items:center;gap:8px">';
    html += _rocketSvg + ' Add Deployment</h2>';

    // Name
    html += '<label style="display:block;margin-bottom:4px;font-size:13px;color:var(--pb-text-dim)">Name *</label>';
    html += '<input id="dep-f-name" type="text" placeholder="My Deployment" style="width:100%;margin-bottom:12px;padding:8px 10px;border:1px solid var(--pb-border);border-radius:6px;background:var(--pb-bg-card);color:var(--pb-text);font-size:13px;box-sizing:border-box"/>';

    // Description
    html += '<label style="display:block;margin-bottom:4px;font-size:13px;color:var(--pb-text-dim)">Description</label>';
    html += '<textarea id="dep-f-desc" rows="2" placeholder="What does this deployment do?" style="width:100%;margin-bottom:12px;padding:8px 10px;border:1px solid var(--pb-border);border-radius:6px;background:var(--pb-bg-card);color:var(--pb-text);font-size:13px;resize:vertical;box-sizing:border-box"></textarea>';

    // URL
    html += '<label style="display:block;margin-bottom:4px;font-size:13px;color:var(--pb-text-dim)">URL</label>';
    html += '<input id="dep-f-url" type="url" placeholder="https://example.com" style="width:100%;margin-bottom:12px;padding:8px 10px;border:1px solid var(--pb-border);border-radius:6px;background:var(--pb-bg-card);color:var(--pb-text);font-size:13px;box-sizing:border-box"/>';

    // Platform (dropdown)
    html += '<label style="display:block;margin-bottom:4px;font-size:13px;color:var(--pb-text-dim)">Platform</label>';
    html += '<select id="dep-f-platform" style="width:100%;margin-bottom:12px;padding:8px 10px;border:1px solid var(--pb-border);border-radius:6px;background:var(--pb-bg-card);color:var(--pb-text);font-size:13px;box-sizing:border-box">';
    html += '<option value="">Select platform...</option>';
    html += '<option value="VPS">VPS</option>';
    html += '<option value="Netlify">Netlify</option>';
    html += '<option value="Cloudflare Pages">Cloudflare Pages</option>';
    html += '<option value="WordPress">WordPress</option>';
    html += '<option value="Vercel">Vercel</option>';
    html += '<option value="Other">Other</option>';
    html += '</select>';

    // Stack
    html += '<label style="display:block;margin-bottom:4px;font-size:13px;color:var(--pb-text-dim)">Stack</label>';
    html += '<input id="dep-f-stack" type="text" placeholder="e.g. Python + Flask, Node + React" style="width:100%;margin-bottom:12px;padding:8px 10px;border:1px solid var(--pb-border);border-radius:6px;background:var(--pb-bg-card);color:var(--pb-text);font-size:13px;box-sizing:border-box"/>';

    // Status (dropdown)
    html += '<label style="display:block;margin-bottom:4px;font-size:13px;color:var(--pb-text-dim)">Status</label>';
    html += '<select id="dep-f-status" style="width:100%;margin-bottom:12px;padding:8px 10px;border:1px solid var(--pb-border);border-radius:6px;background:var(--pb-bg-card);color:var(--pb-text);font-size:13px;box-sizing:border-box">';
    html += '<option value="live">Live</option>';
    html += '<option value="staging">Staging</option>';
    html += '<option value="maintenance">Maintenance</option>';
    html += '<option value="offline">Offline</option>';
    html += '</select>';

    // Repo
    html += '<label style="display:block;margin-bottom:4px;font-size:13px;color:var(--pb-text-dim)">Repository</label>';
    html += '<input id="dep-f-repo" type="text" placeholder="https://github.com/org/repo" style="width:100%;margin-bottom:12px;padding:8px 10px;border:1px solid var(--pb-border);border-radius:6px;background:var(--pb-bg-card);color:var(--pb-text);font-size:13px;box-sizing:border-box"/>';

    // Server
    html += '<label style="display:block;margin-bottom:4px;font-size:13px;color:var(--pb-text-dim)">Server</label>';
    html += '<input id="dep-f-server" type="text" placeholder="e.g. vps-1, us-east-1" style="width:100%;margin-bottom:12px;padding:8px 10px;border:1px solid var(--pb-border);border-radius:6px;background:var(--pb-bg-card);color:var(--pb-text);font-size:13px;box-sizing:border-box"/>';

    // Actions
    html += '<div class="modal-actions" style="display:flex;gap:8px;justify-content:flex-end;margin-top:8px">';
    html += '<button class="btn-primary" onclick="window._depSave()">Save</button>';
    html += '<button class="btn-secondary" onclick="window._depCloseModal()">Cancel</button>';
    html += '</div>';

    html += '</div></div>';

    document.body.insertAdjacentHTML('beforeend', html);

    // Close on overlay click
    var overlay = document.getElementById('deploy-modal');
    if(overlay){
      overlay.addEventListener('click', function(e){
        if(e.target === overlay) window._depCloseModal();
      });
    }

    _modalInjected = true;
  }

  // ── Modal Open / Close ─────────────────────────────────────────
  function _openModal(dep){
    _ensureModal();
    var modal = document.getElementById('deploy-modal');
    if(!modal) return;

    var titleEl = document.getElementById('dep-modal-title');

    if(dep){
      // Edit mode
      _editingId = dep.id;
      if(titleEl) titleEl.innerHTML = _rocketSvg + ' Edit Deployment';
      _setField('dep-f-name', dep.name || '');
      _setField('dep-f-desc', dep.description || '');
      _setField('dep-f-url', dep.url || '');
      _setField('dep-f-platform', dep.platform || '');
      _setField('dep-f-stack', dep.stack || '');
      _setField('dep-f-status', dep.status || 'live');
      _setField('dep-f-repo', dep.repo || '');
      _setField('dep-f-server', dep.server || '');
    } else {
      // Add mode
      _editingId = null;
      if(titleEl) titleEl.innerHTML = _rocketSvg + ' Add Deployment';
      _setField('dep-f-name', '');
      _setField('dep-f-desc', '');
      _setField('dep-f-url', '');
      _setField('dep-f-platform', '');
      _setField('dep-f-stack', '');
      _setField('dep-f-status', 'live');
      _setField('dep-f-repo', '');
      _setField('dep-f-server', '');
    }

    modal.style.display = '';
  }

  function _closeModal(){
    var modal = document.getElementById('deploy-modal');
    if(modal) modal.style.display = 'none';
    _editingId = null;
  }

  function _setField(id, val){
    var el = document.getElementById(id);
    if(!el) return;
    el.value = val;
  }

  function _getField(id){
    var el = document.getElementById(id);
    return el ? el.value.trim() : '';
  }

  // ── Card Builder ───────────────────────────────────────────────
  function _buildDeployCard(dep){
    var id = dep.id || '';
    var name = dep.name || 'Untitled';
    var desc = dep.description || '';
    var status = dep.status || 'live';
    var platform = dep.platform || '';
    var stack = dep.stack || '';
    var url = dep.url || '';

    var statusClass = _STATUS_CLASSES[status] || 'dep-status-live';
    var statusLabel = status.charAt(0).toUpperCase() + status.slice(1);

    var html = '<div class="rule-card" data-dep-id="' + _escH(id) + '">';

    // Header
    html += '<div class="rule-card-header">';
    html += '<div class="rule-card-title">' + _escH(name) + '</div>';
    html += '<span class="dep-status-badge ' + _escH(statusClass) + '">' + _escH(statusLabel) + '</span>';
    html += '</div>';

    // Body
    if(desc){
      html += '<div class="rule-card-body">' + _escH(desc) + '</div>';
    }

    // Meta
    html += '<div class="rule-card-meta">';
    if(platform) html += '<span>Platform: ' + _escH(platform) + '</span>';
    if(stack) html += '<span>Stack: ' + _escH(stack) + '</span>';
    if(url) html += '<span class="dep-health-indicator" data-url="' + _escH(url) + '"></span>';
    html += '</div>';

    // Actions
    html += '<div class="deploy-card-actions">';
    if(url){
      html += '<a href="' + _escH(url) + '" target="_blank" class="deploy-visit-btn" rel="noopener">';
      html += _visitSvg + ' Visit Site</a>';
    }
    html += '<button class="deploy-visit-btn dep-edit-btn" onclick="window._depEdit(\'' + _escH(id) + '\')">Edit</button>';
    html += '<button class="deploy-visit-btn dep-delete-btn" onclick="window._depDelete(\'' + _escH(id) + '\')">Delete</button>';
    html += '</div>';

    html += '</div>';
    return html;
  }

  // ── Render Deployment List ─────────────────────────────────────
  function _renderDeployments(){
    var container = document.getElementById('depCards');
    if(!container) return;

    // Update count
    var countEl = document.getElementById('depCount');
    if(countEl) countEl.textContent = _deployments.length + ' deployment' + (_deployments.length !== 1 ? 's' : '');

    var html = '';
    if(!_deployments.length){
      html += '<div style="grid-column:1/-1;padding:24px;color:var(--pb-text-dim);text-align:center;font-size:13px">No deployments found. Add your first deployment above.</div>';
    } else {
      for(var i = 0; i < _deployments.length; i++){
        html += _buildDeployCard(_deployments[i]);
      }
    }

    container.innerHTML = html;

    // Run health checks after rendering
    _runHealthChecks();
  }

  // ── API: Load Deployments ──────────────────────────────────────
  function _loadDeployments(){
    return fetch('/api/deployments', { headers: _authHeaders() })
      .then(function(r){
        if(!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .then(function(d){
        _deployments = d.deployments || d || [];
        if(!Array.isArray(_deployments)) _deployments = [];
        _deploymentsLoaded = true;
        _renderDeployments();
        return _deployments;
      })
      .catch(function(e){
        var container = document.getElementById('deployList');
        if(container){
          container.innerHTML = '<div style="padding:24px;color:#ef4444;text-align:center;font-size:13px">Failed to load deployments: ' + _escH(e.message) + '</div>';
        }
      });
  }

  // ── API: Save Deployment ───────────────────────────────────────
  function _save(){
    var name = _getField('dep-f-name');
    if(!name){
      showToast('Name is required');
      return;
    }

    var payload = {
      name:        name,
      description: _getField('dep-f-desc'),
      url:         _getField('dep-f-url'),
      platform:    _getField('dep-f-platform'),
      stack:       _getField('dep-f-stack'),
      status:      _getField('dep-f-status') || 'live',
      repo:        _getField('dep-f-repo'),
      server:      _getField('dep-f-server')
    };

    var method, endpoint;
    if(_editingId){
      method = 'PUT';
      endpoint = '/api/deployments/' + encodeURIComponent(_editingId);
    } else {
      method = 'POST';
      endpoint = '/api/deployments';
    }

    var headers = _authHeaders();
    headers['Content-Type'] = 'application/json';

    fetch(endpoint, {
      method: method,
      headers: headers,
      body: JSON.stringify(payload)
    })
    .then(function(r){
      if(!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    })
    .then(function(){
      showToast(_editingId ? 'Deployment updated' : 'Deployment added');
      _closeModal();
      _loadDeployments();
    })
    .catch(function(e){
      showToast('Save failed: ' + e.message);
    });
  }

  // ── API: Delete Deployment ─────────────────────────────────────
  function _delete(id){
    if(!confirm('Delete this deployment? This cannot be undone.')) return;

    fetch('/api/deployments/' + encodeURIComponent(id), {
      method: 'DELETE',
      headers: _authHeaders()
    })
    .then(function(r){
      if(!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    })
    .then(function(){
      showToast('Deployment deleted');
      _loadDeployments();
    })
    .catch(function(e){
      showToast('Delete failed: ' + e.message);
    });
  }

  // ── Edit: Find deployment by ID and open modal ─────────────────
  function _edit(id){
    var dep = null;
    for(var i = 0; i < _deployments.length; i++){
      if(_deployments[i].id === id || String(_deployments[i].id) === String(id)){
        dep = _deployments[i];
        break;
      }
    }
    if(!dep){
      showToast('Deployment not found');
      return;
    }
    _openModal(dep);
  }

  // ── Health Checks ──────────────────────────────────────────────
  function _runHealthChecks(){
    var indicators = document.querySelectorAll('.dep-health-indicator[data-url]');
    indicators.forEach(function(el){
      var url = el.getAttribute('data-url');
      if(!url) return;

      el.innerHTML = '<span style="color:var(--pb-text-dim);font-size:11px">Checking...</span>';

      var ctrl = new AbortController();
      var timer = setTimeout(function(){ ctrl.abort(); }, 5000);

      fetch(url, { mode: 'no-cors', signal: ctrl.signal })
        .then(function(){
          clearTimeout(timer);
          el.innerHTML = '<span style="display:inline-flex;align-items:center;gap:4px;font-size:11px">'
            + '<span style="width:8px;height:8px;border-radius:50%;background:#22c55e;display:inline-block"></span>'
            + '<span style="color:#22c55e">OK</span></span>';
        })
        .catch(function(){
          clearTimeout(timer);
          el.innerHTML = '<span style="display:inline-flex;align-items:center;gap:4px;font-size:11px">'
            + '<span style="width:8px;height:8px;border-radius:50%;background:#ef4444;display:inline-block"></span>'
            + '<span style="color:#ef4444">Down</span></span>';
        });
    });
  }

  // ── Mods Tab Handlers ──────────────────────────────────────────
  window._modClearCache = function(){
    // Safe list: only clear known cache keys, never auth/config
    var safeToClear = [
      'cc_cached_messages',
      'portal_dock_height',
      'portal_chat_docked'
    ];
    var cleared = 0;
    safeToClear.forEach(function(k){
      if(localStorage.getItem(k) !== null){
        localStorage.removeItem(k);
        cleared++;
      }
    });
    // Clear server-side caches via settings
    var tok = localStorage.getItem('portal_token') || '';
    if(tok){
      fetch('/api/settings', {
        method: 'POST',
        headers: { 'Authorization': 'Bearer ' + tok, 'Content-Type': 'application/json' },
        body: JSON.stringify({ cc_cached_messages: null })
      }).catch(function(){});
    }
    showToast('Cleared ' + cleared + ' cached items + server CC cache');
  };

  // ── Module Health / Backup / Restore ──────────────────────────
  var _STATUS_COLOR = { ok: '#22c55e', no_backup: '#f59e0b', missing: '#ef4444', corrupted: '#ef4444' };
  var _STATUS_LABEL = { ok: 'OK', no_backup: 'No Backup', missing: 'Missing', corrupted: 'Corrupted' };
  var _modHealthOpen = false;

  function _renderModGroup(title, groupMods){
    if(!groupMods.length) return '';
    var h = '<div style="font-size:9px;font-weight:700;color:var(--pb-text-dim);text-transform:uppercase;letter-spacing:0.1em;padding:6px 10px 3px">' + title + '</div>';
    groupMods.forEach(function(m){
      var col = _STATUS_COLOR[m.status] || '#888';
      var label = _STATUS_LABEL[m.status] || m.status;
      var needsRepair = (m.status === 'corrupted' || m.status === 'missing');
      var sz = m.size ? (m.size > 1024 ? Math.round(m.size / 1024) + 'KB' : m.size + 'B') : '';
      h += '<div style="display:flex;align-items:center;gap:6px;padding:3px 10px">';
      h += '<span style="width:6px;height:6px;border-radius:50%;background:' + col + ';flex-shrink:0;display:inline-block"></span>';
      h += '<code style="font-size:10px;font-weight:600;color:var(--pb-text);flex:1">' + _escH(m.name) + '</code>';
      if(sz) h += '<span style="font-size:9px;color:var(--pb-text-dim)">' + sz + '</span>';
      if(needsRepair){
        h += '<button onclick="window._modRepair(' + JSON.stringify(m.name) + ')" style="font-size:9px;font-weight:600;color:var(--pb-blue);background:none;border:1px solid var(--pb-blue);cursor:pointer;padding:1px 6px;font-family:inherit;border-radius:3px">Repair</button>';
      } else {
        h += '<span style="font-size:9px;font-weight:600;color:' + col + ';text-transform:uppercase;letter-spacing:0.04em">' + label + '</span>';
      }
      h += '</div>';
    });
    return h;
  }

  function _renderModHealth(data){
    var el = document.getElementById('mods-health-list');
    var badge = document.getElementById('mods-health-badge');
    if(!el) return;
    var s = data.summary || {};
    var mods = data.modules || [];
    var total = s.total || mods.length;
    var issues = (s.missing || 0) + (s.corrupted || 0);
    if(badge){
      badge.textContent = issues === 0 ? (total + '/' + total + ' OK') : (issues + ' issue' + (issues !== 1 ? 's' : ''));
      badge.style.color = issues === 0 ? '#22c55e' : '#ef4444';
    }
    var jsM  = mods.filter(function(m){ return m.name.slice(-3) === '.js'; });
    var cssM = mods.filter(function(m){ return m.name.slice(-4) === '.css'; });
    var otherM = mods.filter(function(m){ return m.name.slice(-3) !== '.js' && m.name.slice(-4) !== '.css'; });
    el.innerHTML = _renderModGroup('JavaScript', jsM) + _renderModGroup('CSS', cssM) + _renderModGroup('Other', otherM);
  }

  function _fetchAndRenderHealth(){
    var el = document.getElementById('mods-health-list');
    if(!el) return;
    el.innerHTML = '<div style="color:var(--pb-text-dim);font-size:11px;padding:8px 10px">Checking...</div>';
    fetch('/api/mods/health', { headers: _authHeaders() })
      .then(function(r){ return r.json(); })
      .then(function(d){ _renderModHealth(d); })
      .catch(function(){
        el.innerHTML = '<div style="color:#ef4444;font-size:11px;padding:8px 10px">Health check failed</div>';
        showToast('Module health check failed', 'error');
      });
  }

  window._modCheckHealth = function(){
    var el = document.getElementById('mods-health-list');
    var chevron = document.getElementById('mods-health-chevron');
    var badge = document.getElementById('mods-health-badge');
    if(!el) return;
    if(_modHealthOpen){
      _modHealthOpen = false;
      el.style.display = 'none';
      if(chevron) chevron.style.transform = '';
      return;
    }
    _modHealthOpen = true;
    el.style.display = 'block';
    if(chevron) chevron.style.transform = 'rotate(180deg)';
    if(badge) badge.textContent = '';
    _fetchAndRenderHealth();
  };

  // Refresh health data without toggling (used after backup/repair actions)
  function _refreshHealthIfOpen(){
    if(_modHealthOpen) _fetchAndRenderHealth();
  }

  window._modBackupAll = function(){
    showToast('Backing up modules...');
    fetch('/api/mods/backup', { method: 'POST', headers: _authHeaders() })
      .then(function(r){ return r.json(); })
      .then(function(d){
        showToast('Backed up ' + (d.backed_up || 0) + ' modules');
        _refreshHealthIfOpen();
      })
      .catch(function(){ showToast('Backup failed', 'error'); });
  };

  window._modRestoreAll = function(){
    if(!confirm('Rebuild all modules from backup? This overwrites current files.')) return;
    showToast('Rebuilding all modules...');
    fetch('/api/mods/restore-all', { method: 'POST', headers: _authHeaders() })
      .then(function(r){ return r.json(); })
      .then(function(d){
        showToast('Restored ' + (d.restored || 0) + ' · Failed ' + (d.failed || 0));
        _refreshHealthIfOpen();
      })
      .catch(function(){ showToast('Rebuild failed', 'error'); });
  };

  window._modRepair = function(name){
    showToast('Repairing ' + name + '...');
    fetch('/api/mods/restore/' + encodeURIComponent(name), { method: 'POST', headers: _authHeaders() })
      .then(function(r){ return r.json(); })
      .then(function(d){
        if(d.status === 'restored') showToast(name + ' restored');
        else showToast('Repair failed: ' + (d.error || 'unknown'), 'error');
        _refreshHealthIfOpen();
      })
      .catch(function(){ showToast('Repair request failed', 'error'); });
  };

  // ── Init / Auto-Init ──────────────────────────────────────────
  function _init(){
    if(!localStorage.getItem('portal_token')) return;
    _loadDeployments();
  }

  // ── Expose Globally ────────────────────────────────────────────
  window._depEdit = function(id){ _edit(id); };
  window._depDelete = function(id){ _delete(id); };
  window._depSave = function(){ _save(); };
  window._depCloseModal = function(){ _closeModal(); };
  window._depOpenModal = function(){ _openModal(null); };
  window.initDeployments = function(){
    if(!_deploymentsLoaded) _init();
    else _loadDeployments();
  };

  // ── Boot on Auth ───────────────────────────────────────────────
  // Auto-load when deploymentsArea becomes visible via panel switch
  // Hook into the global switchDeployTab if available, or use
  // a simple visibility observer.
  if(typeof MutationObserver !== 'undefined'){
    var _observed = false;
    var _observer = new MutationObserver(function(mutations){
      for(var m = 0; m < mutations.length; m++){
        var target = mutations[m].target;
        if(target && target.id === 'deploymentsArea' && target.classList.contains('visible')){
          if(!_deploymentsLoaded) _init();
          break;
        }
      }
    });

    // Observe the deploymentsArea if it exists, or wait for DOM
    function _attachObserver(){
      var area = document.getElementById('deploymentsArea');
      if(area && !_observed){
        _observer.observe(area, { attributes: true, attributeFilter: ['class'] });
        _observed = true;
        // If already visible, init now
        if(area.classList.contains('visible') && !_deploymentsLoaded) _init();
      }
    }

    if(document.readyState === 'loading'){
      document.addEventListener('DOMContentLoaded', _attachObserver);
    } else {
      setTimeout(_attachObserver, 200);
    }
  }

  // Also listen for portal-auth event (login flow)
  window.addEventListener('portal-auth', function(e){
    if(e.detail && e.detail.token && !_deploymentsLoaded){
      setTimeout(function(){
        var area = document.getElementById('deploymentsArea');
        if(area && area.classList.contains('visible')) _init();
      }, 300);
    }
  });

})();
