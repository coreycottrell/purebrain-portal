(function(){
  'use strict';

  var aioZoom = 1;
  var aioCollapsed = {};
  var AIO_AGENTS = [];

  function _escH(s){ return typeof escHtml === 'function' ? escHtml(s) : s; }
  function _authHeaders(){ return typeof _auth === 'function' ? _auth() : {}; }

  function _initials(name){
    if(!name) return '??';
    return name.split(/[-_\s]+/).map(function(w){ return w[0] ? w[0].toUpperCase() : ''; }).join('').slice(0,2);
  }

  var _TYPE_COLORS = {
    'orchestration':'#7C3AED','core':'#22C55E','specialist':'#2A93C1',
    'pipeline':'#FBBF24','governance':'#EF4444','lead':'#F97316',
    'conductor':'#7C3AED','primary':'#7C3AED'
  };

  function _loadAgentsForOrganogram(){
    fetch('/api/agents', { headers: _authHeaders() })
      .then(function(r){ return r.json(); })
      .then(function(d){
        var agents = d.agents || [];

        // Only show active/working agents + the conductor (root)
        var activeAgents = agents.filter(function(a){
          var s = a.status || 'idle';
          var n = (a.name || '').toLowerCase();
          return s === 'active' || s === 'working' || n === 'the conductor' || n === 'the-conductor' || a.id === 'the-conductor';
        });

        // Find conductor ID
        var conductorId = null;
        activeAgents.forEach(function(a){
          var n = (a.name || '').toLowerCase();
          if(n === 'the conductor' || n === 'the-conductor' || a.id === 'the-conductor'){
            conductorId = a.id || a.name;
          }
        });

        // If no conductor found, use first orchestration/governance agent as root
        if(!conductorId && activeAgents.length > 0){
          var root = activeAgents.find(function(a){ return a.type === 'orchestration' || a.type === 'governance'; });
          if(root) conductorId = root.id || root.name;
          else conductorId = activeAgents[0].id || activeAgents[0].name;
        }

        // Build set of active IDs for hierarchy path inclusion
        var activeIds = {};
        activeAgents.forEach(function(a){ activeIds[a.id || a.name] = true; });

        // Also include any agents in the hierarchy path between active agents and root
        // (e.g., if market-researcher is active and reports to pm-lead, include pm-lead even if idle)
        var allAgentsMap = {};
        agents.forEach(function(a){ allAgentsMap[a.id || a.name] = a; });

        function addAncestors(agentId){
          var a = allAgentsMap[agentId];
          if(!a) return;
          var parentId = a.reports_to || null;
          if(parentId && !activeIds[parentId] && allAgentsMap[parentId]){
            activeIds[parentId] = true;
            activeAgents.push(allAgentsMap[parentId]);
            addAncestors(parentId);
          }
        }
        // Copy array to avoid mutation during iteration
        activeAgents.slice().forEach(function(a){ addAncestors(a.id || a.name); });

        AIO_AGENTS = activeAgents.map(function(a){
          var id = a.id || a.name;
          var reportsTo = a.reports_to || a.manager_id || null;

          // Non-root agents without reports_to default to conductor
          if(!reportsTo && id !== conductorId){
            reportsTo = conductorId;
          }

          return {
            id: id,
            name: a.name || a.id,
            role: a.description || a.type || '',
            status: a.status || 'idle',
            skills: a.capabilities || [],
            color: _TYPE_COLORS[a.type] || '#2A93C1',
            manager_id: reportsTo,
            initials: _initials(a.name)
          };
        });

        aioRender();
      })
      .catch(function(e){
        var tree = document.getElementById('aio-tree');
        if(tree) tree.innerHTML = '<div style="padding:24px;color:#ef4444;text-align:center;font-size:13px">Failed to load agents: ' + e.message + '</div>';
      });
  }

  function aioMakeBeep(){
    var d = document.createElement('div');
    d.className = 'aio-beep';
    return d;
  }

  function aioMakeCard(agent){
    var card = document.createElement('div');
    card.className = 'aio-card';
    card.setAttribute('data-aid', agent.id);

    var statusColor = agent.status === 'active' ? '#22C55E' : '#6B7280';
    var statusBg = agent.status === 'active' ? 'rgba(34,197,94,0.12)' : 'rgba(107,114,128,0.12)';

    var html = '<div class="aio-card-body">';
    html += '<div class="aio-initials" style="background:' + agent.color + '">' + _escH(agent.initials) + '</div>';
    html += '<div style="flex:1;min-width:0">';
    html += '<div style="font-size:13px;font-weight:600;color:var(--pb-text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">' + _escH(agent.name) + '</div>';
    html += '<div style="font-size:11px;color:var(--pb-text-dim);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">' + _escH(agent.role) + '</div>';
    html += '<div style="margin-top:4px;display:flex;gap:4px;align-items:center;flex-wrap:wrap">';
    html += '<span class="aio-status-pill" style="background:' + statusBg + ';color:' + statusColor + '">' + _escH(agent.status) + '</span>';
    agent.skills.slice(0,2).forEach(function(s){
      html += '<span class="aio-skill-tag">' + _escH(s) + '</span>';
    });
    if(agent.skills.length > 2) html += '<span class="aio-skill-tag">+' + (agent.skills.length - 2) + '</span>';
    html += '</div></div></div>';

    card.innerHTML = html;
    card.onclick = function(e){ if(e.target.closest('button,a')) return; aioShowPanel(agent); };
    card.addEventListener('dblclick', function(e){ e.preventDefault(); aioCollapsed[agent.id] = !aioCollapsed[agent.id]; aioRender(); });
    return card;
  }

  function aioRender(){
    var tree = document.getElementById('aio-tree');
    if(!tree) return;
    tree.innerHTML = '';

    var agents = AIO_AGENTS;
    if(!agents.length){
      tree.innerHTML = '<div style="padding:24px;color:var(--pb-text-dim);text-align:center;font-size:13px">No agents loaded.</div>';
      return;
    }

    var allIds = agents.map(function(a){ return a.id; });
    var roots = agents.filter(function(a){ return !a.manager_id || allIds.indexOf(a.manager_id) === -1; });
    if(roots.length === 0 && agents.length > 0) roots = agents.slice(0,1);

    var rendered = {};

    function buildBranch(parent){
      if(rendered[parent.id]) return null;
      rendered[parent.id] = true;

      var group = document.createElement('div');
      group.className = 'aio-group';
      group.appendChild(aioMakeCard(parent));

      var children = agents.filter(function(a){ return a.manager_id === parent.id && a.id !== parent.id && !rendered[a.id]; });

      if(children.length > 0){
        var vline = document.createElement('div');
        vline.className = 'aio-vline';
        vline.appendChild(aioMakeBeep());
        group.appendChild(vline);

        var toggleBtn = document.createElement('button');
        toggleBtn.className = 'aio-collapse-btn';
        toggleBtn.innerHTML = aioCollapsed[parent.id] ? '&#9654;' : '&#9660;';
        toggleBtn.title = children.length + ' agent' + (children.length > 1 ? 's' : '');
        toggleBtn.onclick = function(e){ e.stopPropagation(); aioCollapsed[parent.id] = !aioCollapsed[parent.id]; aioRender(); };
        group.appendChild(toggleBtn);

        if(!aioCollapsed[parent.id]){
          var vline2 = document.createElement('div');
          vline2.className = 'aio-vline';
          vline2.appendChild(aioMakeBeep());
          group.appendChild(vline2);

          var childRow = document.createElement('div');
          childRow.className = 'aio-children';
          var wrapList = [];
          children.forEach(function(c){
            var wrap = document.createElement('div');
            wrap.className = 'aio-child-wrap';
            var drop = document.createElement('div');
            drop.className = 'aio-drop';
            drop.appendChild(aioMakeBeep());
            wrap.appendChild(drop);
            var branch = buildBranch(c);
            if(branch){ wrap.appendChild(branch); childRow.appendChild(wrap); wrapList.push(wrap); }
          });
          group.appendChild(childRow);

          requestAnimationFrame(function(){
            if(wrapList.length > 1){
              var rowRect = childRow.getBoundingClientRect();
              var firstRect = wrapList[0].getBoundingClientRect();
              var lastRect = wrapList[wrapList.length-1].getBoundingClientRect();
              var leftEdge = firstRect.left + firstRect.width/2 - rowRect.left;
              var rightEdge = lastRect.left + lastRect.width/2 - rowRect.left;
              var barWidth = rightEdge - leftEdge;
              var hbar = document.createElement('div');
              hbar.className = 'aio-hline';
              hbar.style.width = barWidth + 'px';
              hbar.style.left = leftEdge + 'px';
              hbar.style.transform = 'none';
              var dotL = document.createElement('div'); dotL.className = 'aio-beep aio-beep-left';
              var dotR = document.createElement('div'); dotR.className = 'aio-beep aio-beep-right';
              hbar.appendChild(dotL); hbar.appendChild(dotR);
              childRow.appendChild(hbar);
            }
          });
        }
      }
      return group;
    }

    roots.forEach(function(r){
      var branch = buildBranch(r);
      if(branch) tree.appendChild(branch);
    });

    // Show zoom controls
    var zoomCtrl = document.getElementById('aio-zoom-ctrl');
    if(zoomCtrl) zoomCtrl.style.display = 'flex';
  }

  // Panel
  function aioShowPanel(agent){
    document.getElementById('aio-panel-title').textContent = agent.name;
    var html = '<div style="display:flex;align-items:center;gap:12px;margin-bottom:16px">';
    html += '<div class="aio-initials" style="width:52px;height:52px;font-size:16px;background:' + agent.color + '">' + _escH(agent.initials) + '</div>';
    html += '<div><div style="font-size:16px;font-weight:600;color:var(--pb-text)">' + _escH(agent.name) + '</div>';
    html += '<div style="font-size:12px;color:var(--pb-text-dim)">' + _escH(agent.role) + '</div>';
    var sc = agent.status === 'active' ? '#22C55E' : '#6B7280';
    html += '<div style="margin-top:4px"><span class="aio-status-pill" style="background:rgba(' + (agent.status === 'active' ? '34,197,94' : '107,114,128') + ',0.12);color:' + sc + '">' + _escH(agent.status) + '</span></div>';
    html += '</div></div>';

    html += '<div class="aio-panel-field"><label class="aio-panel-label">Skills</label>';
    html += '<div style="display:flex;gap:4px;flex-wrap:wrap">';
    agent.skills.forEach(function(s){ html += '<span class="aio-skill-tag" style="font-size:11px;padding:3px 8px">' + _escH(s) + '</span>'; });
    html += '</div></div>';

    html += '<div class="aio-panel-field"><label class="aio-panel-label">Reports To</label>';
    var mgr = AIO_AGENTS.find(function(a){ return a.id === agent.manager_id; });
    html += '<div style="font-size:12px;color:var(--pb-text)">' + (mgr ? _escH(mgr.name) : '-- None (root) --') + '</div></div>';

    var reports = AIO_AGENTS.filter(function(a){ return a.manager_id === agent.id; });
    if(reports.length > 0){
      html += '<div class="aio-panel-field"><label class="aio-panel-label">Direct Reports (' + reports.length + ')</label>';
      reports.forEach(function(r){
        html += '<div style="font-size:12px;color:var(--pb-text);padding:2px 0">&bull; ' + _escH(r.name) + '</div>';
      });
      html += '</div>';
    }

    document.getElementById('aio-panel-content').innerHTML = html;
    document.getElementById('aio-panel').style.display = 'block';
  }

  // Expose globals for inline onclick handlers
  window.aioClosePanel = function(){ document.getElementById('aio-panel').style.display = 'none'; };

  // Close panel on Escape key
  document.addEventListener('keydown', function(e){
    if(e.key === 'Escape'){
      var panel = document.getElementById('aio-panel');
      if(panel && panel.style.display === 'block') window.aioClosePanel();
    }
  });

  // Close panel on click outside
  document.addEventListener('click', function(e){
    var panel = document.getElementById('aio-panel');
    if(!panel || panel.style.display !== 'block') return;
    // If click is inside the panel or on an aio-card, don't close
    if(e.target.closest('#aio-panel') || e.target.closest('.aio-card')) return;
    window.aioClosePanel();
  });
  window.aioUpdateZoomLabel = function(){ var el = document.getElementById('aio-zoom-label'); if(el) el.textContent = Math.round(aioZoom*100) + '%'; };
  window.aioZoomIn = function(){ aioZoom = Math.min(aioZoom + 0.1, 2); document.getElementById('aio-tree').style.transform = 'scale(' + aioZoom + ')'; window.aioUpdateZoomLabel(); };
  window.aioZoomOut = function(){ aioZoom = Math.max(aioZoom - 0.1, 0.4); document.getElementById('aio-tree').style.transform = 'scale(' + aioZoom + ')'; window.aioUpdateZoomLabel(); };
  window.aioFitToScreen = function(){
    var canvas = document.getElementById('aio-canvas');
    var tree = document.getElementById('aio-tree');
    if(!canvas || !tree) return;
    tree.style.transform = 'scale(1)';
    var tW = tree.scrollWidth, tH = tree.scrollHeight;
    var cW = canvas.clientWidth - 64, cH = canvas.clientHeight - 64;
    if(tW === 0 || tH === 0) return;
    aioZoom = Math.min(cW/tW, cH/tH, 1.5);
    aioZoom = Math.max(aioZoom, 0.3);
    tree.style.transform = 'scale(' + aioZoom + ')';
    window.aioUpdateZoomLabel();
  };

  window.aioSearch = function(query){
    var clearBtn = document.getElementById('aio-search-clear');
    document.querySelectorAll('#aio-canvas .aio-card').forEach(function(n){ n.classList.remove('search-match','search-dim'); });
    if(!query.trim()){ if(clearBtn) clearBtn.style.display = 'none'; return; }
    if(clearBtn) clearBtn.style.display = 'block';
    var q = query.toLowerCase();
    var first = null;
    document.querySelectorAll('#aio-canvas .aio-card').forEach(function(n){
      if(n.textContent.toLowerCase().indexOf(q) >= 0){ n.classList.add('search-match'); if(!first) first = n; }
      else{ n.classList.add('search-dim'); }
    });
    if(first) first.scrollIntoView({ behavior:'smooth', block:'center', inline:'center' });
  };
  window.aioClearSearch = function(){ document.getElementById('aio-search').value = ''; window.aioSearch(''); };

  window.aioExportPNG = function(){
    if(typeof html2canvas === 'undefined'){
      var s = document.createElement('script');
      s.src = 'https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js';
      s.onload = function(){ _doExport(); };
      document.head.appendChild(s);
    } else { _doExport(); }
  };

  function _doExport(){
    var el = document.getElementById('aio-canvas');
    var origOverflow = el.style.overflow, origWidth = el.style.width, origHeight = el.style.height, origFlex = el.style.flex;
    el.style.overflow = 'visible'; el.style.width = el.scrollWidth + 'px'; el.style.height = el.scrollHeight + 'px'; el.style.flex = 'none';
    html2canvas(el, { scale:2, useCORS:true, backgroundColor:'#0a0e1a' }).then(function(canvas){
      el.style.overflow = origOverflow; el.style.width = origWidth; el.style.height = origHeight; el.style.flex = origFlex;
      var link = document.createElement('a');
      link.download = 'ai-organogram-' + new Date().toISOString().slice(0,10) + '.png';
      link.href = canvas.toDataURL(); link.click();
    }).catch(function(){ el.style.overflow = origOverflow; el.style.width = origWidth; el.style.height = origHeight; el.style.flex = origFlex; });
  }

  // Wire subtab switching — render organogram when tab is clicked
  function _wireSubtab(){
    var tabs = document.querySelectorAll('#agentsSubtabs .agents-subtab');
    tabs.forEach(function(tab){
      tab.addEventListener('click', function(){
        if(tab.getAttribute('data-subtab') === 'ai-organogram'){
          if(!AIO_AGENTS.length){
            _loadAgentsForOrganogram();
          } else {
            setTimeout(function(){ aioRender(); }, 50);
          }
          // Show zoom controls
          var zc = document.getElementById('aio-zoom-ctrl');
          if(zc) zc.style.display = 'flex';
        } else {
          // Hide zoom controls when not on organogram
          var zc = document.getElementById('aio-zoom-ctrl');
          if(zc) zc.style.display = 'none';
        }
      });
    });
  }

  // Boot
  if(document.readyState === 'loading'){
    document.addEventListener('DOMContentLoaded', _wireSubtab);
  } else {
    setTimeout(_wireSubtab, 300);
  }

})();
