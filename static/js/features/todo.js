(function(){
  'use strict';

  // ── helpers ────────────────────────────────────────────────────────────────
  function _esc(s){ return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }
  function _relTime(iso){
    if(!iso) return '--';
    try {
      // Handle +00:00 timezone offset (don't append Z if offset already present)
      var s = iso;
      if(!/[Z+-]\d{2}:?\d{2}$/.test(s) && !s.endsWith('Z')) s = s + 'Z';
      var d = new Date(s);
      if(isNaN(d.getTime())) return '--';
      var diff = Math.floor((Date.now() - d.getTime()) / 1000);
      if(diff < 0) return 'just now';
      if(diff < 60) return diff+'s ago';
      if(diff < 3600) return Math.floor(diff/60)+'m ago';
      if(diff < 86400) return Math.floor(diff/3600)+'h ago';
      return Math.floor(diff/86400)+'d ago';
    } catch(e){ return '--'; }
  }

  // ── state ──────────────────────────────────────────────────────────────────
  var _tasks = [];
  var _filter = 'all'; // all | email | manual
  var _refreshTimer = null;
  var _detailTask = null;

  // ── columns ────────────────────────────────────────────────────────────────
  var COLS = [
    { key:'needs-approval', label:'Needs Approval', color:'#f59e0b' },
    { key:'pending',        label:'Pending',        color:'#fb923c' },
    { key:'in-progress',   label:'In Progress',    color:'var(--pb-blue-light)' },
    { key:'completed',      label:'Completed',      color:'var(--pb-green)' },
  ];

  // ── auth ───────────────────────────────────────────────────────────────────
  function _tok(){ return localStorage.getItem('portal_token') || ''; }
  function _auth(){ return { 'Authorization': 'Bearer ' + _tok(), 'Content-Type': 'application/json' }; }

  // ── API calls ──────────────────────────────────────────────────────────────
  function _loadTasks(cb){
    if(!_tok()) return;
    fetch('/api/todo/tasks', { headers: _auth() })
      .then(function(r){ if(!r.ok) throw new Error('HTTP '+r.status); return r.json(); })
      .then(function(data){
        _tasks = data.tasks || [];
        if(cb) cb();
        _render();
        _updateBadge();
      })
      .catch(function(e){
        _showKanbanError('Failed to load tasks: ' + e.message);
      });
  }

  function _scanEmails(){
    var btn = document.getElementById('kbScanBtn');
    if(btn){ btn.disabled = true; btn.textContent = 'Scanning...'; }
    fetch('/api/todo/scan-emails', { method:'POST', headers: _auth() })
      .then(function(r){ if(!r.ok) throw new Error('HTTP '+r.status); return r.json(); })
      .then(function(data){
        if(btn){ btn.disabled = false; btn.textContent = 'Scan Emails'; }
        var msg = data.created ? ('Found ' + data.created + ' task' + (data.created!==1?'s':'')) : 'No new tasks found';
        if(typeof showToast === 'function') showToast(msg);
        _loadTasks();
      })
      .catch(function(e){
        if(btn){ btn.disabled = false; btn.textContent = 'Scan Emails'; }
        if(typeof showToast === 'function') showToast('Scan failed: ' + e.message);
      });
  }

  function _updateTaskStatus(taskId, newStatus){
    fetch('/api/todo/tasks/' + taskId, {
      method: 'PUT',
      headers: _auth(),
      body: JSON.stringify({ status: newStatus })
    })
    .then(function(r){ if(!r.ok) throw new Error('HTTP '+r.status); return r.json(); })
    .then(function(){ _loadTasks(); })
    .catch(function(e){
      if(typeof showToast === 'function') showToast('Update failed: ' + e.message);
    });
  }

  function _deleteTask(taskId){
    fetch('/api/todo/tasks/' + taskId, {
      method: 'DELETE',
      headers: _auth()
    })
    .then(function(r){ if(!r.ok) throw new Error('HTTP '+r.status); return r.json(); })
    .then(function(){
      _closeDetail();
      _loadTasks();
    })
    .catch(function(e){
      if(typeof showToast === 'function') showToast('Delete failed: ' + e.message);
    });
  }

  function _addManualTask(title, description){
    fetch('/api/todo/tasks', {
      method: 'POST',
      headers: _auth(),
      body: JSON.stringify({ title: title, description: description, source: 'manual' })
    })
    .then(function(r){ if(!r.ok) throw new Error('HTTP '+r.status); return r.json(); })
    .then(function(){ _loadTasks(); })
    .catch(function(e){
      if(typeof showToast === 'function') showToast('Add failed: ' + e.message);
    });
  }

  // ── render ─────────────────────────────────────────────────────────────────
  function _filteredTasks(){
    if(_filter === 'all') return _tasks;
    return _tasks.filter(function(t){ return t.source === _filter; });
  }

  function _render(){
    var wrap = document.getElementById('todoArea');
    if(!wrap || !wrap.classList.contains('visible')) return;

    var filtered = _filteredTasks();

    // Stats
    var stats = { 'needs-approval':0, pending:0, 'in-progress':0, completed:0 };
    _tasks.forEach(function(t){ if(stats[t.status]!==undefined) stats[t.status]++; });
    var sa = document.getElementById('todo-stat-approval');
    var sp = document.getElementById('todo-stat-pending');
    var sc = document.getElementById('todo-stat-completed');
    var st = document.getElementById('todo-stat-total');
    if(sa) sa.textContent = stats['needs-approval'];
    if(sp) sp.textContent = stats['pending'];
    if(sc) sc.textContent = stats['completed'];
    if(st) st.textContent = _tasks.length;

    var board = document.getElementById('kb-board');
    if(!board){
      // Board not created yet — create it now
      var sectWrap = document.getElementById('todo-sections-wrap');
      if(sectWrap){
        sectWrap.innerHTML = _boardHTML();
        board = document.getElementById('kb-board');
      }
      if(!board) return;
    }

    var cols = {};
    COLS.forEach(function(c){ cols[c.key] = []; });
    filtered.forEach(function(t){
      var s = t.status || 'needs-approval';
      if(!cols[s]) s = 'needs-approval';
      cols[s].push(t);
    });

    COLS.forEach(function(col){
      var colEl = board.querySelector('[data-col="'+col.key+'"]');
      if(!colEl) return;
      var cnt = colEl.querySelector('.kb-col-count');
      if(cnt) cnt.textContent = cols[col.key].length;
      var cards = colEl.querySelector('.kb-cards');
      if(!cards) return;
      if(!cols[col.key].length){
        cards.innerHTML = '<div class="kb-empty">No tasks</div>';
        return;
      }
      cards.innerHTML = cols[col.key].map(function(t){ return _cardHTML(t); }).join('');
      // Bind card click
      cards.querySelectorAll('.kb-card').forEach(function(card){
        card.addEventListener('click', function(){
          var id = card.getAttribute('data-id');
          var task = _tasks.find(function(x){ return x.id === id; });
          if(task) _openDetail(task);
        });
      });
    });
  }

  function _cardHTML(t){
    var srcBadge = t.source === 'email'
      ? '<span class="kb-badge kb-badge-email"><svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"></path><polyline points="22,6 12,12 2,6"></polyline></svg> Email</span>'
      : '<span class="kb-badge kb-badge-manual"><svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"></line><line x1="5" y1="12" x2="19" y2="12"></line></svg> Manual</span>';
    var fromLine = t.from ? '<div class="kb-card-from">From: '+_esc(t.from)+'</div>' : '';
    return '<div class="kb-card" data-id="'+_esc(t.id)+'">'
      + '<div class="kb-card-top">'
      + srcBadge
      + '<span class="kb-card-time">'+_relTime(t.created_at)+'</span>'
      + '</div>'
      + '<div class="kb-card-title">'+_esc(t.title)+'</div>'
      + fromLine
      + '</div>';
  }

  function _updateBadge(){
    var badge = document.getElementById('todo-sidebar-badge');
    if(!badge) return;
    var active = _tasks.filter(function(t){ return t.status !== 'completed'; }).length;
    if(active > 0){
      badge.textContent = active;
      badge.style.display = '';
    } else {
      badge.style.display = 'none';
    }
    // Also update count in header
    var cnt = document.querySelector('.todo-count');
    if(cnt) cnt.textContent = _tasks.length || '';
  }

  function _showKanbanError(msg){
    var board = document.getElementById('kb-board');
    if(board) board.innerHTML = '<div style="padding:40px;text-align:center;color:var(--pb-orange);font-size:12px">'+_esc(msg)+'</div>';
  }

  // ── detail drawer ──────────────────────────────────────────────────────────
  function _openDetail(task){
    _detailTask = task;
    var drawer = document.getElementById('kb-detail-drawer');
    if(!drawer) return;

    document.getElementById('kb-detail-title').textContent = task.title || '';
    document.getElementById('kb-detail-from').textContent = task.from || '--';
    document.getElementById('kb-detail-subject').textContent = task.subject || '--';
    document.getElementById('kb-detail-source').textContent = task.source === 'email' ? 'Email' : 'Manual';
    document.getElementById('kb-detail-time').textContent = _relTime(task.created_at);
    document.getElementById('kb-detail-body').textContent = task.description || '(no description)';

    var sel = document.getElementById('kb-detail-status');
    if(sel) sel.value = task.status || 'needs-approval';

    drawer.classList.add('open');
  }

  function _closeDetail(){
    _detailTask = null;
    var drawer = document.getElementById('kb-detail-drawer');
    if(drawer) drawer.classList.remove('open');
  }

  // ── add task modal ─────────────────────────────────────────────────────────
  function _openAddModal(){
    var overlay = document.getElementById('kb-add-overlay');
    if(overlay){
      overlay.classList.add('open');
      var inp = document.getElementById('kb-add-title');
      if(inp) inp.focus();
    }
  }

  function _closeAddModal(){
    var overlay = document.getElementById('kb-add-overlay');
    if(overlay) overlay.classList.remove('open');
    var inp = document.getElementById('kb-add-title');
    if(inp) inp.value = '';
    var desc = document.getElementById('kb-add-desc');
    if(desc) desc.value = '';
  }

  // ── init panel ─────────────────────────────────────────────────────────────
  function _initPanel(){
    var todoArea = document.getElementById('todoArea');
    if(!todoArea || todoArea.getAttribute('data-kb-init')) return;
    todoArea.setAttribute('data-kb-init', '1');

    // Replace header actions
    var headerActions = todoArea.querySelector('.todo-header-actions');
    if(headerActions){
      headerActions.innerHTML = ''
        + '<select class="todo-filter kb-source-filter" onchange="window._todoFilterSource(this.value)">'
        + '<option value="all">All</option>'
        + '<option value="email">Email</option>'
        + '<option value="manual">Manual</option>'
        + '</select>'
        + '<button class="todo-header-btn" id="kbScanBtn" onclick="window._todoScanEmails()">Scan Emails</button>'
        + '<button class="todo-header-btn" style="background:rgba(34,197,94,0.1);border-color:rgba(34,197,94,0.3);color:var(--pb-green)" onclick="window._todoOpenAdd()">+ Add Task</button>';
    }

    // Replace todo-wrap content (keep stats, replace sections wrap with board)
    var sectWrap = todoArea.querySelector('#todo-sections-wrap');
    if(sectWrap){
      sectWrap.innerHTML = _boardHTML();
    }

    // Inject detail drawer if not present
    if(!document.getElementById('kb-detail-drawer')){
      var drawer = document.createElement('div');
      drawer.innerHTML = _detailDrawerHTML();
      document.body.appendChild(drawer.firstElementChild);
    }

    // Inject add modal if not present
    if(!document.getElementById('kb-add-overlay')){
      var modal = document.createElement('div');
      modal.innerHTML = _addModalHTML();
      document.body.appendChild(modal.firstElementChild);
    }

    _bindDrawerEvents();
    _bindAddModalEvents();
    _loadTasks();

    // Auto-refresh every 60s
    if(_refreshTimer) clearInterval(_refreshTimer);
    _refreshTimer = setInterval(function(){
      var area = document.getElementById('todoArea');
      if(area && area.classList.contains('visible')) _loadTasks();
    }, 60000);
  }

  function _boardHTML(){
    var html = '<div class="kb-board" id="kb-board">';
    COLS.forEach(function(col){
      html += '<div class="kb-col" data-col="'+col.key+'">'
        + '<div class="kb-col-header" style="border-top-color:'+col.color+'">'
        + '<span class="kb-col-title">'+_esc(col.label)+'</span>'
        + '<span class="kb-col-count">0</span>'
        + '</div>'
        + '<div class="kb-cards"></div>'
        + '</div>';
    });
    html += '</div>';
    return html;
  }

  function _detailDrawerHTML(){
    return '<div class="kb-drawer-overlay" id="kb-detail-drawer" onclick="if(event.target===this)window._todoCloseDetail()">'
      + '<div class="kb-drawer">'
      + '<div class="kb-drawer-header">'
      + '<span class="kb-drawer-title-label">Task Detail</span>'
      + '<button class="kb-drawer-close" onclick="window._todoCloseDetail()">&#x2715;</button>'
      + '</div>'
      + '<div class="kb-drawer-body">'
      + '<div class="kb-detail-title" id="kb-detail-title"></div>'
      + '<div class="kb-drawer-meta">'
      + '<div class="kb-drawer-meta-row"><span class="kb-drawer-meta-label">Source</span><span id="kb-detail-source"></span></div>'
      + '<div class="kb-drawer-meta-row"><span class="kb-drawer-meta-label">From</span><span id="kb-detail-from"></span></div>'
      + '<div class="kb-drawer-meta-row"><span class="kb-drawer-meta-label">Subject</span><span id="kb-detail-subject"></span></div>'
      + '<div class="kb-drawer-meta-row"><span class="kb-drawer-meta-label">Created</span><span id="kb-detail-time"></span></div>'
      + '</div>'
      + '<div class="kb-drawer-section-label">Description</div>'
      + '<div class="kb-detail-body" id="kb-detail-body"></div>'
      + '<div class="kb-drawer-section-label" style="margin-top:16px">Move to Column</div>'
      + '<select class="kb-detail-status-sel" id="kb-detail-status" onchange="window._todoChangeStatus(this.value)">'
      + '<option value="needs-approval">Needs Approval</option>'
      + '<option value="pending">Pending</option>'
      + '<option value="in-progress">In Progress</option>'
      + '<option value="completed">Completed</option>'
      + '</select>'
      + '</div>'
      + '<div class="kb-drawer-footer">'
      + '<button class="kb-drawer-delete-btn" onclick="window._todoDeleteTask()">Delete Task</button>'
      + '</div>'
      + '</div>'
      + '</div>';
  }

  function _addModalHTML(){
    return '<div class="kb-add-overlay" id="kb-add-overlay" onclick="if(event.target===this)window._todoCloseAdd()">'
      + '<div class="kb-add-modal">'
      + '<div class="kb-add-header">'
      + '<span style="font-family:Oswald,sans-serif;font-size:14px;font-weight:700;color:var(--pb-text)">New Task</span>'
      + '<button class="kb-drawer-close" onclick="window._todoCloseAdd()">&#x2715;</button>'
      + '</div>'
      + '<div style="padding:16px">'
      + '<label class="kb-add-label">Title</label>'
      + '<input class="kb-add-input" id="kb-add-title" type="text" placeholder="Task title..." maxlength="200">'
      + '<label class="kb-add-label">Description (optional)</label>'
      + '<textarea class="kb-add-input" id="kb-add-desc" rows="4" placeholder="Additional details..."></textarea>'
      + '<div style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px">'
      + '<button class="todo-btn" onclick="window._todoCloseAdd()">Cancel</button>'
      + '<button class="todo-btn approve" onclick="window._todoSubmitAdd()">Add Task</button>'
      + '</div>'
      + '</div>'
      + '</div>'
      + '</div>';
  }

  function _bindDrawerEvents(){
    document.addEventListener('keydown', function(e){
      if(e.key === 'Escape') _closeDetail();
    });
  }

  function _bindAddModalEvents(){
    document.addEventListener('keydown', function(e){
      if(e.key === 'Escape') _closeAddModal();
    });
  }

  // ── global API (called from HTML) ──────────────────────────────────────────
  window._todoScanEmails = _scanEmails;
  window._todoOpenAdd = _openAddModal;
  window._todoCloseAdd = _closeAddModal;
  window._todoCloseDetail = _closeDetail;
  window._todoFilterSource = function(val){
    _filter = val;
    _render();
  };
  window._todoChangeStatus = function(newStatus){
    if(!_detailTask) return;
    _detailTask.status = newStatus;
    _updateTaskStatus(_detailTask.id, newStatus);
  };
  window._todoDeleteTask = function(){
    if(!_detailTask) return;
    if(typeof showToast === 'function') showToast('Deleting task...');
    _deleteTask(_detailTask.id);
  };
  window._todoSubmitAdd = function(){
    var title = (document.getElementById('kb-add-title')||{}).value || '';
    title = title.trim();
    if(!title){ if(typeof showToast==='function') showToast('Please enter a title'); return; }
    var desc = (document.getElementById('kb-add-desc')||{}).value || '';
    _closeAddModal();
    _addManualTask(title, desc.trim());
  };

  // ── hook into tab switching ────────────────────────────────────────────────
  // Watch for todo tab becoming visible, then init
  var _observer = new MutationObserver(function(mutations){
    mutations.forEach(function(m){
      if(m.target && m.target.id === 'todoArea' && m.target.classList.contains('visible')){
        if(!m.target.getAttribute('data-kb-init')){
          _initPanel();
        } else {
          // Already initialized — re-render and refresh data
          _render();
          _loadTasks();
        }
      }
    });
  });
  var _todoArea = document.getElementById('todoArea');
  if(_todoArea){
    _observer.observe(_todoArea, { attributes: true, attributeFilter: ['class'] });
    // If already visible on load
    if(_todoArea.classList.contains('visible')) _initPanel();
  }

  // Also expose for external trigger
  window._todoInit = _initPanel;


})();
