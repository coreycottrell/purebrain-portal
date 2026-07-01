(function(){
  'use strict';

  // Auth helpers provided by shared auth.js (_tok, _auth, _authJson)
  function _esc(s){ return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }
  function _relTime(iso){
    if(!iso) return '--';
    try {
      var d = new Date(iso.endsWith('Z') ? iso : iso+'Z');
      if(isNaN(d.getTime())) return '--';
      var diff = Math.floor((Date.now() - d.getTime()) / 1000);
      if(diff < 0) return 'just now';
      if(diff < 60) return diff+'s ago';
      if(diff < 3600) return Math.floor(diff/60)+'m ago';
      if(diff < 86400) return Math.floor(diff/3600)+'h ago';
      return Math.floor(diff/86400)+'d ago';
    } catch(e){ return '--'; }
  }
  function _fmtDate(iso){
    if(!iso) return '--';
    try {
      var d = new Date(iso.endsWith('Z') ? iso : iso+'Z');
      if(isNaN(d.getTime())) return '--';
      var months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
      var h = d.getHours(); var m = d.getMinutes();
      var ampm = h >= 12 ? 'pm' : 'am';
      h = h % 12 || 12;
      return months[d.getMonth()] + ' ' + d.getDate() + ', ' + h + ':' + (m<10?'0':'') + m + ampm;
    } catch(e){ return '--'; }
  }

  // Build a fire_at ISO string for a one-time task from a date (YYYY-MM-DD)
  // and a time (HH:MM). Pure/testable: validates the date is present and not in
  // the past. Returns { ok:true, iso } or { ok:false, error }.
  function _buildOnceFireAt(dateStr, timeStr, now){
    now = now || new Date();
    if(!dateStr){ return { ok:false, error:'Pick a date for the one-time task' }; }
    var dParts = String(dateStr).split('-');
    if(dParts.length !== 3){ return { ok:false, error:'Invalid date' }; }
    var year = parseInt(dParts[0], 10);
    var month = parseInt(dParts[1], 10) - 1;
    var day = parseInt(dParts[2], 10);
    var tParts = String(timeStr || '09:00').split(':');
    var hours = parseInt(tParts[0], 10);
    var mins = parseInt(tParts[1], 10);
    if(isNaN(hours)) hours = 9;
    if(isNaN(mins)) mins = 0;
    var fireDate = new Date(year, month, day, hours, mins, 0, 0);
    if(isNaN(fireDate.getTime())){ return { ok:false, error:'Invalid date or time' }; }
    if(fireDate < now){ return { ok:false, error:'Pick a date/time in the future' }; }
    return { ok:true, iso: fireDate.toISOString() };
  }

  // Format a fire_at ISO string into the YYYY-MM-DD value a date input expects.
  function _fireToDateInput(iso){
    if(!iso) return '';
    try {
      var d = new Date(iso.endsWith('Z') ? iso : iso+'Z');
      if(isNaN(d.getTime())) return '';
      return d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0');
    } catch(e){ return ''; }
  }

  // ═══════════════════════════════════════════════════════════════════════
  // TASKS PANEL — Scheduled Tasks from /api/scheduled-tasks
  // ═══════════════════════════════════════════════════════════════════════
  var _cachedTasks = [];

  function _loadTasks(){
    if(!_tok()) return;
    fetch('/api/scheduled-tasks', { headers: _auth() })
      .then(function(r){ if(!r.ok) throw new Error('HTTP '+r.status); return r.json(); })
      .then(function(data){
        _cachedTasks = data.tasks || [];
        _renderTasks(_cachedTasks);
        _updateTasksBadge(_cachedTasks);
      })
      .catch(function(e){
        var tb = document.getElementById('tasksTableBody');
        if(tb) tb.innerHTML = '<tr><td colspan="7" style="text-align:center;padding:24px;color:var(--pb-orange);font-size:12px">Failed to load tasks: '+_esc(e.message)+'</td></tr>';
      });
  }

  function _renderTasks(tasks){
    var tb = document.getElementById('tasksTableBody');
    if(!tb) return;
    if(!tasks.length){
      tb.innerHTML = '<tr><td colspan="7" style="text-align:center;padding:24px;color:var(--pb-text-dim);font-size:12px">No scheduled tasks. Click "Schedule Task" to create one.</td></tr>';
      _updateStats(tasks);
      return;
    }
    var now = new Date();
    var html = '';
    tasks.forEach(function(t){
      var st = (t.status || 'pending').toLowerCase();
      if(st === 'in_progress') st = 'active';
      var isCompleted = st === 'completed';
      var fire = t.fire_at ? new Date(t.fire_at) : null;
      var isOverdue = fire && fire < now && !t.recur_type && st !== 'completed';
      if(isOverdue) st = 'failed';
      var recurLabel = t.recur_type ? (t.recur_type.charAt(0).toUpperCase()+t.recur_type.slice(1)) : 'Once';
      if(t.recur_time) recurLabel += ' &middot; ' + _esc(t.recur_time);
      if(t.recur_days && t.recur_days.length) recurLabel += ' (' + _esc(t.recur_days.join(',')) + ')';
      var fireLabel = fire ? _fmtDate(t.fire_at) : '--';
      // Extract priority from message text or use 'medium'
      var pri = 'medium';
      var priMatch = (t.message||'').match(/Priority:\s*(urgent|high|medium|low|normal)/i);
      if(priMatch) pri = priMatch[1].toLowerCase();
      if(pri === 'normal') pri = 'medium';
      if(pri === 'urgent') pri = 'high';
      var priLabel = pri === 'high' ? 'High' : pri === 'low' ? 'Low' : 'Med';
      // Extract first line as title, rest as desc
      var lines = (t.message||'').split('\n');
      var title = lines[0] || 'Untitled';
      var desc = lines.slice(1).join(' ').substring(0,80);
      var opacity = isCompleted ? 'style="opacity:0.55"' : '';
      var clockSvg = '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>';
      html += '<tr data-status="'+_esc(st)+'" data-task-id="'+_esc(t.id)+'" '+opacity+'>';
      html += '<td><div class="tl-task-cell"><div class="tl-task-name">'+_esc(title)+'</div>'+(desc?'<div class="tl-task-desc">'+_esc(desc)+'</div>':'')+'</div></td>';
      html += '<td><div class="tl-agent"><div class="tl-agent-dot ai">A</div>Agent</div></td>';
      html += '<td><div class="tl-schedule">'+clockSvg+recurLabel+'</div></td>';
      html += '<td><span class="tl-status-badge '+_esc(st)+'">'+_esc(st.charAt(0).toUpperCase()+st.slice(1))+'</span></td>';
      html += '<td><span class="tl-priority '+_esc(pri)+'">'+_esc(priLabel)+'</span></td>';
      html += '<td><div class="tl-lastrun">'+_esc(fireLabel)+'</div></td>';
      html += '<td><div class="tl-actions">';
      if(!isCompleted){
        html += '<button class="tl-action-btn" title="Edit" onclick="event.stopPropagation();if(window._portalTasks)window._portalTasks.editTask(\''+_esc(t.id)+'\')"><svg viewBox="0 0 24 24"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg></button>';
        html += '<button class="tl-action-btn" title="Run Now" onclick="event.stopPropagation();if(window._portalTasks)window._portalTasks.patchStatus(\''+_esc(t.id)+'\',\'in_progress\')"><svg viewBox="0 0 24 24"><polygon points="5 3 19 12 5 21 5 3"/></svg></button>';
      }
      html += '<button class="tl-action-btn danger" title="Delete" onclick="event.stopPropagation();if(window._portalTasks)window._portalTasks.deleteTask(\''+_esc(t.id)+'\')"><svg viewBox="0 0 24 24"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg></button>';
      html += '</div></td></tr>';
    });
    tb.innerHTML = html;
    _updateStats(tasks);
  }

  function _updateStats(tasks){
    var now = new Date();
    var active=0, pending=0, completed=0, failed=0;
    tasks.forEach(function(t){
      var st = (t.status||'pending').toLowerCase();
      if(st === 'in_progress') active++;
      else if(st === 'completed') completed++;
      else {
        var fire = t.fire_at ? new Date(t.fire_at) : null;
        if(fire && fire < now && !t.recur_type) failed++;
        else pending++;
      }
    });
    var el;
    el = document.getElementById('tasks-stat-active'); if(el) el.textContent = active;
    el = document.getElementById('tasks-stat-pending'); if(el) el.textContent = pending;
    el = document.getElementById('tasks-stat-completed'); if(el) el.textContent = completed;
    el = document.getElementById('tasks-stat-failed'); if(el) el.textContent = failed;
    el = document.getElementById('tasks-stat-total'); if(el) el.textContent = tasks.length;
    _updateTasksBadge(tasks);
  }

  function _updateTasksBadge(tasks){
    var badge = document.getElementById('tasks-sidebar-badge');
    if(!badge) return;
    var active = (tasks||_cachedTasks).filter(function(t){ return t.status !== 'completed'; }).length;
    if(active > 0){ badge.textContent = active; badge.style.display = ''; }
    else { badge.textContent = ''; badge.style.display = 'none'; }
  }

  function _scheduleTask(){
    if(!_tok()){ showToast('Not connected — enter token in Settings'); return; }
    var prompt = document.getElementById('sched-prompt').value.trim();
    if(!prompt){ showToast('Enter a task description'); return; }
    var type = document.getElementById('sched-type').value;
    var time = document.getElementById('sched-time').value;
    var body = { message: prompt };
    if(type === 'daily' || type === 'weekly'){
      // Recurring: anchor fire_at to the next occurrence at the chosen time.
      var now = new Date();
      var fireDate = new Date(now.getFullYear(), now.getMonth(), now.getDate());
      var parts = time.split(':');
      fireDate.setHours(parseInt(parts[0])||9, parseInt(parts[1])||0, 0, 0);
      if(fireDate <= now) fireDate.setDate(fireDate.getDate()+1);
      body.fire_at = fireDate.toISOString();
      body.recur_type = type;
      body.recur_time = time;
    } else {
      // One-time (and custom): use the selected date + time.
      var dateStr = document.getElementById('sched-date') ? document.getElementById('sched-date').value : '';
      var built = _buildOnceFireAt(dateStr, time);
      if(!built.ok){ showToast(built.error); return; }
      body.fire_at = built.iso;
    }
    fetch('/api/schedule-task', {
      method: 'POST', headers: _authJson(), body: JSON.stringify(body)
    }).then(function(r){ return r.json(); })
      .then(function(d){
        if(d.ok){
          closeScheduleModal();
          document.getElementById('sched-prompt').value = '';
          showToast('Task scheduled');
          _loadTasks();
        } else {
          showToast('Error: '+(d.error||'Unknown'));
        }
      }).catch(function(e){ showToast('Schedule failed: '+e.message); });
  }

  function _deleteTask(taskId){
    if(!_tok()) return;
    if(!confirm('Delete this scheduled task?')) return;
    fetch('/api/scheduled-tasks/'+encodeURIComponent(taskId), {
      method: 'DELETE', headers: _auth()
    }).then(function(r){ return r.json(); })
      .then(function(d){
        if(d.ok){ showToast('Task deleted'); _loadTasks(); }
        else { showToast('Delete failed: '+(d.error||'?')); }
      }).catch(function(e){ showToast('Delete failed: '+e.message); });
  }

  function _patchStatus(taskId, status){
    if(!_tok()) return;
    fetch('/api/scheduled-tasks/'+encodeURIComponent(taskId), {
      method: 'PATCH', headers: _authJson(), body: JSON.stringify({ status: status })
    }).then(function(r){ return r.json(); })
      .then(function(d){
        if(d.ok){ showToast('Status updated'); _loadTasks(); }
      }).catch(function(){ showToast('Update failed'); });
  }

  // ── Edit task (reuse schedule modal in edit mode) ──
  var _editingTaskId = null;

  function _editTask(taskId) {
    if (!_tok()) return;
    var task = null;
    for (var i = 0; i < _cachedTasks.length; i++) {
      if (_cachedTasks[i].id === taskId) { task = _cachedTasks[i]; break; }
    }
    if (!task) { showToast('Task not found'); return; }

    _editingTaskId = taskId;

    // Pre-fill the schedule modal
    var prompt = document.getElementById('sched-prompt');
    var type = document.getElementById('sched-type');
    var time = document.getElementById('sched-time');
    var modalTitle = document.querySelector('#schedule-modal h2');

    if (prompt) prompt.value = task.message || '';
    if (type) type.value = task.recur_type || 'once';
    if (time) time.value = task.recur_time || '09:00';

    // Prefill the date input for one-time tasks from the existing fire_at.
    var dateInput = document.getElementById('sched-date');
    if (dateInput) {
      var isOnce = !task.recur_type;
      dateInput.value = isOnce ? _fireToDateInput(task.fire_at) : '';
      // For a one-time task, mirror fire_at's time into the time input too.
      if (isOnce && time && task.fire_at) {
        var fd = new Date(task.fire_at.endsWith('Z') ? task.fire_at : task.fire_at + 'Z');
        if (!isNaN(fd.getTime())) {
          time.value = String(fd.getHours()).padStart(2,'0') + ':' + String(fd.getMinutes()).padStart(2,'0');
        }
      }
    }

    // Set priority radio
    var priMatch = (task.message || '').match(/Priority:\s*(high|medium|low)/i);
    var pri = priMatch ? priMatch[1].toLowerCase() : 'medium';
    var radios = document.querySelectorAll('input[name="sp"]');
    radios.forEach(function(r) { r.checked = r.value === pri; });

    // Change modal title and button to "Update"
    if (modalTitle) modalTitle.innerHTML = '<svg viewBox="0 0 24 24" style="width:18px;height:18px;stroke:var(--pb-blue);fill:none;stroke-width:2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg> Edit Task';
    var saveBtn = document.querySelector('#schedule-modal .btn-primary');
    if (saveBtn) {
      saveBtn.textContent = 'Update Task';
      saveBtn.setAttribute('onclick', 'updateEditedTask()');
    }

    openScheduleModal();
  }

  function _updateEditedTask() {
    if (!_editingTaskId || !_tok()) return;
    var prompt = document.getElementById('sched-prompt').value.trim();
    if (!prompt) { showToast('Enter a task description'); return; }
    var type = document.getElementById('sched-type').value;
    var time = document.getElementById('sched-time').value;

    var body = { message: prompt };
    if (type === 'daily' || type === 'weekly') {
      var now = new Date();
      var fireDate = new Date(now.getFullYear(), now.getMonth(), now.getDate());
      var parts = time.split(':');
      fireDate.setHours(parseInt(parts[0]) || 9, parseInt(parts[1]) || 0, 0, 0);
      if (fireDate <= now) fireDate.setDate(fireDate.getDate() + 1);
      body.fire_at = fireDate.toISOString();
      body.recur_type = type;
      body.recur_time = time;
    } else {
      var dateStr = document.getElementById('sched-date') ? document.getElementById('sched-date').value : '';
      var built = _buildOnceFireAt(dateStr, time);
      if (!built.ok) { showToast(built.error); return; }
      body.fire_at = built.iso;
    }

    fetch('/api/scheduled-tasks/' + encodeURIComponent(_editingTaskId), {
      method: 'PUT', headers: _authJson(), body: JSON.stringify(body)
    }).then(function(r) { return r.json(); })
      .then(function(d) {
        if (d.ok) {
          showToast('Task updated');
          _resetScheduleModal();
          closeScheduleModal();
          _loadTasks();
        } else {
          showToast('Update failed: ' + (d.error || '?'));
        }
      }).catch(function(e) { showToast('Update failed: ' + e.message); });
  }

  function _resetScheduleModal() {
    _editingTaskId = null;
    var modalTitle = document.querySelector('#schedule-modal h2');
    if (modalTitle) modalTitle.innerHTML = '<svg viewBox="0 0 24 24" style="width:18px;height:18px;stroke:var(--pb-blue);fill:none;stroke-width:2"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg> Schedule Task';
    var saveBtn = document.querySelector('#schedule-modal .btn-primary');
    if (saveBtn) {
      saveBtn.textContent = 'Schedule';
      saveBtn.setAttribute('onclick', 'scheduleTask()');
    }
  }

  // ═══════════════════════════════════════════════════════════════════════
  // TODO PANEL — Hub Tasks from /api/hub/tasks
  // ═══════════════════════════════════════════════════════════════════════
  function _loadHubTasks(){
    if(!_tok()) return;
    fetch('/api/hub/tasks', { headers: _auth() })
      .then(function(r){ if(!r.ok) throw new Error('HTTP '+r.status); return r.json(); })
      .then(function(data){
        var tasks = data.tasks || [];
        _renderTodoPanel(tasks);
      })
      .catch(function(e){
        var wrap = document.getElementById('todo-sections-wrap');
        if(wrap) wrap.innerHTML = '<div style="padding:24px;text-align:center;color:var(--pb-orange);font-size:12px">Failed to load: '+_esc(e.message)+'</div>';
      });
  }

  function _renderTodoPanel(tasks){
    var wrap = document.getElementById('todo-sections-wrap');
    if(!wrap) return;
    if(!tasks.length){
      wrap.innerHTML = '<div style="padding:24px;text-align:center;color:var(--pb-text-dim);font-size:12px">No tasks in the hub. Tasks will appear here when your AI creates them.</div>';
      _updateTodoStats([]);
      return;
    }
    // Group tasks by status
    var approval = [], pending = [], completed = [];
    tasks.forEach(function(t){
      var st = (t.status||'pending').toLowerCase();
      if(st === 'approval' || st === 'needs_approval') approval.push(t);
      else if(st === 'completed' || st === 'done') completed.push(t);
      else pending.push(t);
    });
    var html = '';
    if(approval.length){
      html += '<div class="todo-section"><div class="todo-section-head"><div class="todo-section-dot" style="background:var(--pb-orange-light)"></div><div class="todo-section-title">Needs Your Approval</div><div class="todo-section-count">'+approval.length+' items</div></div>';
      approval.forEach(function(t){ html += _buildTodoCard(t, 'approval'); });
      html += '</div>';
    }
    if(pending.length){
      html += '<div class="todo-section"><div class="todo-section-head"><div class="todo-section-dot" style="background:#f59e0b"></div><div class="todo-section-title">Pending Tasks</div><div class="todo-section-count">'+pending.length+' items</div></div>';
      pending.forEach(function(t){ html += _buildTodoCard(t, 'pending'); });
      html += '</div>';
    }
    if(completed.length){
      html += '<div class="todo-section"><div class="todo-section-head"><div class="todo-section-dot" style="background:var(--pb-green)"></div><div class="todo-section-title">Completed</div><div class="todo-section-count">'+completed.length+' items</div></div>';
      completed.forEach(function(t){ html += _buildTodoCard(t, 'completed'); });
      html += '</div>';
    }
    wrap.innerHTML = html;
    _updateTodoStats(tasks);
    // Wire filter
    var filter = document.getElementById('todoFilter');
    if(filter) filter.onchange = function(){ _filterTodoCards(this.value); };
  }

  function _buildTodoCard(t, status){
    var title = _esc(t.title || t.message || t.name || 'Untitled');
    var desc = _esc(t.description || t.preview || '');
    var agent = _esc(t.agent || t.assigned_to || '');
    var created = t.created_at ? _relTime(t.created_at) : '';
    var urgent = status === 'approval' ? ' urgent' : '';
    var h = '<div class="todo-card'+urgent+'" data-status="'+_esc(status)+'">';
    h += '<div class="todo-card-top"><input type="checkbox" class="todo-check"><div class="todo-card-body">';
    h += '<div class="todo-title">'+title+'</div>';
    if(desc) h += '<div class="todo-preview">'+desc+'</div>';
    h += '</div></div>';
    h += '<div class="todo-card-meta">';
    if(agent) h += '<div class="todo-meta-cell"><div class="todo-meta-label">Agent</div><div class="todo-meta-val">'+agent+'</div></div>';
    if(created) h += '<div class="todo-meta-cell"><div class="todo-meta-label">Created</div><div class="todo-meta-val">'+created+'</div></div>';
    if(t.priority) h += '<div class="todo-meta-cell"><div class="todo-meta-label">Priority</div><div class="todo-meta-val">'+_esc(t.priority)+'</div></div>';
    h += '</div>';
    h += '<div class="todo-card-footer"><button class="todo-btn" onclick="showToast(\'Task details\')">View</button></div>';
    h += '</div>';
    return h;
  }

  function _filterTodoCards(val){
    document.querySelectorAll('.todo-card').forEach(function(card){
      if(val==='all') card.style.display='';
      else card.style.display = card.getAttribute('data-status')===val ? '' : 'none';
    });
  }

  function _updateTodoStats(tasks){
    var approval=0, pending=0, completed=0;
    tasks.forEach(function(t){
      var st = (t.status||'pending').toLowerCase();
      if(st==='approval'||st==='needs_approval') approval++;
      else if(st==='completed'||st==='done') completed++;
      else pending++;
    });
    var el;
    el = document.getElementById('todo-stat-approval'); if(el) el.textContent = approval;
    el = document.getElementById('todo-stat-pending'); if(el) el.textContent = pending;
    el = document.getElementById('todo-stat-completed'); if(el) el.textContent = completed;
    el = document.getElementById('todo-stat-total'); if(el) el.textContent = tasks.length;
    var countEl = document.querySelector('.todo-count');
    if(countEl) countEl.textContent = tasks.length || '';
    // Update sidebar badge
    var todoBadge = document.getElementById('todo-sidebar-badge');
    if(todoBadge){
      var activeCount = approval + pending;
      if(activeCount > 0){ todoBadge.textContent = activeCount; todoBadge.style.display = ''; }
      else { todoBadge.textContent = ''; todoBadge.style.display = 'none'; }
    }
  }

  // Boot: auto-load when token available
  window.addEventListener('portal-auth', function(){
    setTimeout(function(){ _loadTasks(); _loadHubTasks(); }, 600);
  });
  if(_tok()) setTimeout(function(){ _loadTasks(); _loadHubTasks(); }, 1600);

  // Expose API
  window._portalTasks = {
    loadTasks: _loadTasks,
    loadHubTasks: _loadHubTasks,
    scheduleTask: _scheduleTask,
    deleteTask: _deleteTask,
    patchStatus: _patchStatus,
    editTask: _editTask,
    updateBadge: function(){ _updateTasksBadge(_cachedTasks); }
  };

  // Global functions for the modal
  window.updateEditedTask = _updateEditedTask;
  window._resetScheduleModal = _resetScheduleModal;

})();
