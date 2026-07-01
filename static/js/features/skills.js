// skills.js — Skills Shop panel (core built-in)
// Provides browsing, searching, installing, and managing Claude Code skills.
// Depends on: PanelManager (window.PanelManager)

(function() {
  'use strict';

  var _skills = [];
  var _registry = [];
  var _categories = [];
  var _activeCategory = 'all';
  var _activeFilter = 'all'; // 'all' | 'installed' | 'available'
  var _searchQuery = '';
  var _loaded = false;

  // ---------------------------------------------------------------------------
  // Data fetching
  // ---------------------------------------------------------------------------

  function _fetchSkills(cb) {
    var token = localStorage.getItem('portal_token') || '';
    fetch('/api/skills', { headers: { 'Authorization': 'Bearer ' + token } })
      .then(function(r) { return r.json(); })
      .then(function(d) {
        _skills = d.skills || [];
        _categories = d.categories || [];
        if (cb) cb();
      })
      .catch(function(e) {
        console.error('[skills] Failed to fetch skills:', e);
        if (cb) cb();
      });
  }

  function _fetchRegistry(cb) {
    var token = localStorage.getItem('portal_token') || '';
    fetch('/api/skills/registry', { headers: { 'Authorization': 'Bearer ' + token } })
      .then(function(r) { return r.json(); })
      .then(function(d) {
        _registry = d.skills || [];
        if (cb) cb();
      })
      .catch(function(e) {
        console.error('[skills] Failed to fetch registry:', e);
        _registry = [];
        if (cb) cb();
      });
  }

  // ---------------------------------------------------------------------------
  // Rendering
  // ---------------------------------------------------------------------------

  function _escHtml(s) {
    var d = document.createElement('div');
    d.textContent = s || '';
    return d.innerHTML;
  }

  function _renderStats() {
    var el = document.getElementById('skills-stats');
    if (!el) return;
    var installed = _skills.length;
    var external = _registry.filter(function(s) { return !s.installed; }).length;
    var total = installed + external;
    el.innerHTML =
      '<button class="skills-filter-btn' + (_activeFilter === 'all' ? ' active' : '') +
        '" onclick="window._portalSkills.setFilter(\'all\')">' + total + ' All</button>' +
      '<button class="skills-filter-btn' + (_activeFilter === 'installed' ? ' active' : '') +
        '" onclick="window._portalSkills.setFilter(\'installed\')">' + installed + ' Installed</button>' +
      '<button class="skills-filter-btn' + (_activeFilter === 'available' ? ' active' : '') +
        '" onclick="window._portalSkills.setFilter(\'available\')">' + external + ' Available</button>';
  }

  function _renderCategoryPills() {
    var el = document.getElementById('skills-category-pills');
    if (!el) return;

    // Collect all categories from both local and registry
    var allCats = {};
    _skills.forEach(function(s) { allCats[s.category] = (allCats[s.category] || 0) + 1; });
    _registry.forEach(function(s) {
      if (!s.installed) allCats[s.category] = (allCats[s.category] || 0) + 1;
    });

    var html = '<button class="skills-pill' + (_activeCategory === 'all' ? ' active' : '') +
      '" onclick="window._portalSkills.filterCategory(\'all\')">All</button>';
    var catOrder = ['Development', 'Architecture', 'Testing', 'Security', 'Infrastructure',
      'Data & AI', 'DevOps', 'Communication', 'Productivity', 'Social & Content',
      'Documentation', 'Ceremonies', 'Uncategorized'];
    catOrder.forEach(function(cat) {
      if (!allCats[cat]) return;
      html += '<button class="skills-pill' + (_activeCategory === cat ? ' active' : '') +
        '" onclick="window._portalSkills.filterCategory(\'' + _escHtml(cat) + '\')">' +
        _escHtml(cat) + ' <span class="skills-pill-count">' + allCats[cat] + '</span></button>';
    });
    el.innerHTML = html;
  }

  function _getFilteredSkills() {
    // Merge local + uninstalled registry skills
    var merged = _skills.slice();
    var localNames = {};
    _skills.forEach(function(s) { localNames[s.name] = true; });
    _registry.forEach(function(s) {
      if (!localNames[s.name]) {
        merged.push({
          name: s.name,
          display_name: s.display_name || s.name,
          description: s.description || '',
          category: s.category || 'Uncategorized',
          source: s.source || 'external',
          installed: false,
          registry_entry: s
        });
      }
    });

    // Apply installed/available filter
    if (_activeFilter === 'installed') {
      merged = merged.filter(function(s) { return s.installed; });
    } else if (_activeFilter === 'available') {
      merged = merged.filter(function(s) { return !s.installed; });
    }

    // Apply category filter
    if (_activeCategory !== 'all') {
      merged = merged.filter(function(s) { return s.category === _activeCategory; });
    }

    // Apply search
    if (_searchQuery) {
      var q = _searchQuery.toLowerCase();
      merged = merged.filter(function(s) {
        return (s.name || '').toLowerCase().indexOf(q) !== -1 ||
          (s.display_name || '').toLowerCase().indexOf(q) !== -1 ||
          (s.description || '').toLowerCase().indexOf(q) !== -1;
      });
    }

    // Sort: installed first, then alphabetical
    merged.sort(function(a, b) {
      if (a.installed && !b.installed) return -1;
      if (!a.installed && b.installed) return 1;
      return (a.name || '').localeCompare(b.name || '');
    });

    return merged;
  }

  function _renderGrid() {
    var grid = document.getElementById('skills-grid');
    var empty = document.getElementById('skills-empty');
    if (!grid) return;

    var filtered = _getFilteredSkills();

    if (!filtered.length) {
      grid.innerHTML = '';
      if (empty) { empty.style.display = 'flex'; empty.textContent = _searchQuery ? 'No skills match "' + _searchQuery + '"' : 'No skills found.'; }
      return;
    }
    if (empty) empty.style.display = 'none';

    // Group by category
    var byCategory = {};
    filtered.forEach(function(s) {
      var cat = s.category || 'Uncategorized';
      if (!byCategory[cat]) byCategory[cat] = [];
      byCategory[cat].push(s);
    });

    var html = '';
    var catOrder = ['Development', 'Architecture', 'Testing', 'Security', 'Infrastructure',
      'Data & AI', 'DevOps', 'Communication', 'Productivity', 'Social & Content',
      'Documentation', 'Ceremonies', 'Uncategorized'];

    catOrder.forEach(function(cat) {
      var skills = byCategory[cat];
      if (!skills || !skills.length) return;

      var catSlug = cat.toLowerCase().replace(/[^a-z]/g, '-').replace(/-+/g, '-');
      html += '<div class="skills-category-section" data-category="' + catSlug + '">';
      html += '<h3 class="skills-category-heading">' + _escHtml(cat) + ' <span class="skills-category-count">' + skills.length + '</span></h3>';
      html += '<div class="skills-card-row">';

      skills.forEach(function(s) {
        var isInstalled = s.installed;
        var sourceBadge = '';
        if (s.source && s.source !== 'local') {
          sourceBadge = '<span class="skills-source-badge">' + _escHtml(s.source) + '</span>';
        }

        html += '<div class="skills-card' + (isInstalled ? ' installed' : '') + '" data-skill="' + _escHtml(s.name) + '">';
        html += '<div class="skills-card-header">';
        html += '<span class="skills-card-name">' + _escHtml(s.display_name || s.name) + '</span>';
        html += isInstalled
          ? '<span class="skills-badge-installed">Installed</span>'
          : '<span class="skills-badge-available">Available</span>';
        html += '</div>';
        html += '<div class="skills-card-desc">' + _escHtml(s.description || 'No description') + '</div>';
        html += '<div class="skills-card-footer">';
        html += sourceBadge;
        html += '<div class="skills-card-actions">';
        if (isInstalled && s.source !== 'local') {
          html += '<button class="skills-btn skills-btn-remove" onclick="window._portalSkills.uninstall(\'' + _escHtml(s.name) + '\')">Remove</button>';
        }
        if (!isInstalled) {
          html += '<button class="skills-btn skills-btn-install" onclick="window._portalSkills.install(\'' + _escHtml(s.name) + '\')">Install</button>';
        }
        html += '<button class="skills-btn skills-btn-view" onclick="window._portalSkills.viewDetail(\'' + _escHtml(s.name) + '\', ' + (isInstalled ? 'true' : 'false') + ')">View</button>';
        html += '</div></div></div>';
      });

      html += '</div></div>';
    });

    grid.innerHTML = html;
  }

  // ---------------------------------------------------------------------------
  // Detail overlay
  // ---------------------------------------------------------------------------

  function _viewDetail(name, isInstalled) {
    var overlay = document.getElementById('skills-detail-overlay');
    if (!overlay) return;

    var token = localStorage.getItem('portal_token') || '';
    overlay.classList.add('open');
    overlay.innerHTML = '<div class="skills-detail-content"><div class="skills-detail-loading">Loading skill...</div></div>';

    if (isInstalled) {
      // Fetch from local
      fetch('/api/skills/detail/' + encodeURIComponent(name), {
        headers: { 'Authorization': 'Bearer ' + token }
      })
        .then(function(r) { return r.json(); })
        .then(function(d) { _renderDetail(d, true); })
        .catch(function() { _renderDetail({ name: name, content: 'Failed to load skill details.' }, true); });
    } else {
      // Show from registry data
      var entry = _registry.find(function(s) { return s.name === name; });
      _renderDetail({
        name: name,
        display_name: entry ? entry.display_name : name,
        description: entry ? entry.description : '',
        category: entry ? entry.category : 'Uncategorized',
        content: entry ? (entry.content || entry.description || 'No detailed content available. Install the skill to view the full SKILL.md.') : 'Not found in registry.',
        source: entry ? entry.source : 'external',
        url: entry ? entry.url : '',
      }, false);
    }
  }

  function _renderDetail(data, isInstalled) {
    var overlay = document.getElementById('skills-detail-overlay');
    if (!overlay) return;

    var raw = data.content || '';
    // Strip YAML frontmatter
    if (raw.startsWith('---')) {
      var fmEnd = raw.indexOf('---', 3);
      if (fmEnd > 0) raw = raw.substring(fmEnd + 3).trim();
    }
    // Render code blocks BEFORE escaping (preserve them)
    var codeBlocks = [];
    raw = raw.replace(/```(\w*)\n([\s\S]*?)```/g, function(_, lang, code) {
      var idx = codeBlocks.length;
      codeBlocks.push('<pre class="skills-code-block"><code>' + _escHtml(code.trim()) + '</code></pre>');
      return '%%CODEBLOCK_' + idx + '%%';
    });
    // Escape remaining HTML
    var content = _escHtml(raw);
    // Restore code blocks
    codeBlocks.forEach(function(block, i) {
      content = content.replace('%%CODEBLOCK_' + i + '%%', block);
    });
    // Markdown rendering
    content = content.replace(/^### (.+)$/gm, '<h4>$1</h4>');
    content = content.replace(/^## (.+)$/gm, '<h3>$1</h3>');
    content = content.replace(/^# (.+)$/gm, '<h2>$1</h2>');
    content = content.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    content = content.replace(/`([^`]+)`/g, '<code class="skills-inline-code">$1</code>');
    content = content.replace(/^- (.+)$/gm, '<li>$1</li>');
    content = content.replace(/(<li>.*<\/li>)/g, '<ul>$1</ul>');
    content = content.replace(/<\/ul>\s*<ul>/g, '');
    content = content.replace(/\n/g, '<br>');

    var html = '<div class="skills-detail-content">';
    html += '<div class="skills-detail-header">';
    html += '<h2>' + _escHtml(data.display_name || data.name) + '</h2>';
    html += '<button class="skills-detail-close" onclick="window._portalSkills.closeDetail()">&times;</button>';
    html += '</div>';
    html += '<div class="skills-detail-meta">';
    if (data.category) html += '<span class="skills-pill active">' + _escHtml(data.category) + '</span>';
    if (data.source && data.source !== 'local') html += '<span class="skills-source-badge">' + _escHtml(data.source) + '</span>';
    if (isInstalled) html += '<span class="skills-badge-installed">Installed</span>';
    html += '</div>';
    if (data.description) html += '<p class="skills-detail-desc">' + _escHtml(data.description) + '</p>';
    html += '<div class="skills-detail-body">' + content + '</div>';
    html += '<div class="skills-detail-actions">';
    if (!isInstalled) {
      html += '<button class="skills-btn skills-btn-install" onclick="window._portalSkills.install(\'' + _escHtml(data.name) + '\')">Install Skill</button>';
    } else if (data.source !== 'local') {
      html += '<button class="skills-btn skills-btn-remove" onclick="window._portalSkills.uninstall(\'' + _escHtml(data.name) + '\')">Remove Skill</button>';
    }
    html += '</div></div>';
    overlay.innerHTML = html;
  }

  function _closeDetail() {
    var overlay = document.getElementById('skills-detail-overlay');
    if (overlay) overlay.classList.remove('open');
  }

  // ---------------------------------------------------------------------------
  // Install / Uninstall
  // ---------------------------------------------------------------------------

  function _install(name) {
    var entry = _registry.find(function(s) { return s.name === name; });
    if (!entry || !entry.url) {
      if (window.showToast) window.showToast('Skill not found in registry');
      return;
    }

    var token = localStorage.getItem('portal_token') || '';
    if (window.showToast) window.showToast('Installing ' + name + '...');

    fetch('/api/skills/install', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name: entry.name,
        source: entry.source || 'external',
        url: entry.url,
        files: entry.files || {}
      })
    })
      .then(function(r) { return r.json(); })
      .then(function(d) {
        if (d.status === 'installed') {
          if (window.showToast) window.showToast('Installed ' + name + ' successfully');
          _refresh();
        } else {
          if (window.showToast) window.showToast('Install failed: ' + (d.error || 'Unknown error'));
        }
      })
      .catch(function(e) {
        if (window.showToast) window.showToast('Install failed: ' + e.message);
      });
  }

  function _uninstall(name) {
    if (!confirm('Remove skill "' + name + '"? This deletes the skill files.')) return;

    var token = localStorage.getItem('portal_token') || '';
    fetch('/api/skills/uninstall', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: name })
    })
      .then(function(r) { return r.json(); })
      .then(function(d) {
        if (d.status === 'uninstalled') {
          if (window.showToast) window.showToast('Removed ' + name);
          _closeDetail();
          _refresh();
        } else {
          if (window.showToast) window.showToast('Remove failed: ' + (d.error || 'Unknown error'));
        }
      })
      .catch(function(e) {
        if (window.showToast) window.showToast('Remove failed: ' + e.message);
      });
  }

  // ---------------------------------------------------------------------------
  // Search and filter
  // ---------------------------------------------------------------------------

  function _onSearch(e) {
    _searchQuery = (e.target.value || '').trim();
    _renderGrid();
  }

  function _filterCategory(cat) {
    _activeCategory = cat;
    _renderCategoryPills();
    _renderGrid();
  }

  function _setFilter(filter) {
    _activeFilter = filter;
    _renderStats();
    _renderCategoryPills();
    _renderGrid();
  }

  // ---------------------------------------------------------------------------
  // Load / Refresh
  // ---------------------------------------------------------------------------

  function _showLoading() {
    var grid = document.getElementById('skills-grid');
    if (grid) grid.innerHTML = '<div class="skills-loading"><div class="skills-loading-spinner"></div><div class="skills-loading-text">Loading skills...</div></div>';
  }

  function _refresh() {
    _showLoading();
    _fetchSkills(function() {
      _fetchRegistry(function() {
        _renderStats();
        _renderCategoryPills();
        _renderGrid();
      });
    });
  }

  function _load() {
    var searchInput = document.getElementById('skills-search');
    if (searchInput && !_loaded) {
      searchInput.addEventListener('input', _onSearch);
      _loaded = true;
    }
    _refresh();
  }

  // ---------------------------------------------------------------------------
  // Public API
  // ---------------------------------------------------------------------------

  window._portalSkills = {
    load: _load,
    refresh: _refresh,
    filterCategory: _filterCategory,
    setFilter: _setFilter,
    install: _install,
    uninstall: _uninstall,
    viewDetail: _viewDetail,
    closeDetail: _closeDetail,
  };

})();
