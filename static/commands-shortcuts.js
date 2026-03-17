
(function() {
  'use strict';

  function escHtml(s) {
    if (!s) return '';
    return String(s).replace(/&/g,'&amp;').replace(/\x3c/g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }
  function authGet() {
    var tok = localStorage.getItem('pb_token') || localStorage.getItem('portal_token') || '';
    return { 'Authorization': 'Bearer ' + tok };
  }

  // ============================================================
  // COMMANDS PANEL
  // ============================================================
  var commandsLoaded = false;

  function cmdBlock(code) {
    return '<div class="cmd-block" onclick="window._copyCmd(this)"><code>' + escHtml(code) + '</code><span class="cmd-copy-btn">&#x2398; Copy</span></div>';
  }

  window._copyCmd = function(el) {
    var code = el.querySelector('code');
    if (!code) return;
    var text = code.textContent.trim();
    if (navigator.clipboard) {
      navigator.clipboard.writeText(text).then(function() {
        var btn = el.querySelector('.cmd-copy-btn');
        if (btn) { btn.textContent = 'Copied!'; setTimeout(function() { btn.textContent = '\u2398 Copy'; }, 1500); }
      });
    } else {
      var ta = document.createElement('textarea');
      ta.value = text; document.body.appendChild(ta); ta.select(); document.execCommand('copy'); document.body.removeChild(ta);
      var btn = el.querySelector('.cmd-copy-btn');
      if (btn) { btn.textContent = 'Copied!'; setTimeout(function() { btn.textContent = '\u2398 Copy'; }, 1500); }
    }
  };

  window.loadCommands = function() {
    console.log('[CMD] loadCommands called. loaded=' + commandsLoaded);
    if (commandsLoaded) return;
    var container = document.getElementById('commands-content');
    if (!container) { console.error('[CMD] commands-content not found!'); return; }
    container.innerHTML = '<div style="padding:20px;color:var(--teal);font-size:0.82rem;">Fetching commands...</div>';

    fetch('/api/commands', { headers: authGet() })
      .then(function(r) {
        console.log('[CMD] fetch status=' + r.status);
        return r.json();
      })
      .then(function(d) {
        console.log('[CMD] data keys=' + Object.keys(d).join(','));
        commandsLoaded = true;
        renderCommands(d, container);
      })
      .catch(function(e) {
        container.innerHTML = '<div style="padding:20px;color:#ef4444;font-size:0.82rem;">Failed to load: ' + escHtml(e.message) + '</div>';
      });
  };

  function renderCommands(d, container) {
    var s = d.server || {};
    var p = d.paths || {};
    var t = d.tmux || {};
    var civ = d.civ || {};

    var ip     = escHtml(s.server_ip || 'your-server');
    var port   = escHtml(s.ssh_port  || '22');
    var user   = escHtml(s.ssh_user  || 'user');
    var purl   = escHtml(s.portal_url || '');
    var sess   = escHtml(t.primary_session || civ.name + '-primary');
    var civN   = escHtml(civ.name || 'aether');
    var home   = escHtml(p.home || '/home/' + (s.ssh_user || 'user'));
    var croot  = escHtml(p.civ_root || home + '/projects/AI-CIV/aether');
    var pdir   = escHtml(p.portal_dir || home + '/purebrain_portal');
    var tdir   = escHtml(p.tools_dir || croot + '/tools');
    var ldir   = escHtml(p.logs_dir  || croot + '/logs');

    var html = '';

    // Server Info
    html += '<div class="cmd-section">';
    html += '<div class="cmd-section-title">&#x1F4E1; Server Info</div>';
    html += '<div class="cmd-cards-grid single"><div class="cmd-card">';
    html += '<div class="cmd-info-grid">';
    html += '<div class="cmd-info-item"><div class="cmd-info-label">Server IP</div><div class="cmd-info-value">' + ip + '</div><div class="cmd-info-sub">SSH Port: ' + port + '</div></div>';
    html += '<div class="cmd-info-item"><div class="cmd-info-label">SSH User</div><div class="cmd-info-value">' + user + '</div><div class="cmd-info-sub">Non-root access</div></div>';
    html += '<div class="cmd-info-item"><div class="cmd-info-label">Portal URL</div><div class="cmd-info-value">' + purl + '</div><div class="cmd-info-sub">HTTPS frontend</div></div>';
    html += '<div class="cmd-info-item"><div class="cmd-info-label">Primary Session</div><div class="cmd-info-value">' + sess + '</div><div class="cmd-info-sub">tmux session name</div></div>';
    html += '</div></div></div></div>';

    // SSH Access
    html += '<div class="cmd-section">';
    html += '<div class="cmd-section-title">&#x1F511; SSH Access</div>';
    html += '<div class="cmd-cards-grid">';
    html += '<div class="cmd-card"><div class="cmd-label">Connect to server</div>';
    html += cmdBlock('ssh -p ' + (s.ssh_port||'22') + ' ' + (s.ssh_user||'user') + '@' + (s.server_ip||'your-server'));
    html += '</div>';
    html += '<div class="cmd-card"><div class="cmd-label">Attach to primary tmux session</div>';
    html += cmdBlock('tmux attach -t ' + (t.primary_session||civN+'-primary'));
    html += '</div>';
    html += '<div class="cmd-card"><div class="cmd-label">Create one-word alias (add to ~/.zshrc)</div>';
    html += cmdBlock('alias ' + civN + '="ssh -p ' + (s.ssh_port||'22') + ' -t ' + (s.ssh_user||'user') + '@' + (s.server_ip||'your-server') + ' tmux attach -t ' + (t.primary_session||civN+'-primary') + '"');
    html += '</div>';
    html += '<div class="cmd-card"><div class="cmd-label">SSH copy public key (one-time setup)</div>';
    html += cmdBlock('ssh-copy-id -p ' + (s.ssh_port||'22') + ' ' + (s.ssh_user||'user') + '@' + (s.server_ip||'your-server'));
    html += '</div>';
    html += '</div></div>';

    // Service Status
    html += '<div class="cmd-section">';
    html += '<div class="cmd-section-title">&#x1F4CA; Status Checks</div>';
    html += '<div class="cmd-cards-grid">';
    html += '<div class="cmd-card"><div class="cmd-label">Check Claude session alive</div>' + cmdBlock('tmux has-session -t ' + (t.primary_session||sess) + ' && echo ALIVE || echo DEAD') + '</div>';
    html += '<div class="cmd-card"><div class="cmd-label">Check Telegram bridge</div>' + cmdBlock('ps aux | grep telegram_bridge | grep -v grep') + '</div>';
    html += '<div class="cmd-card"><div class="cmd-label">Check portal server</div>' + cmdBlock('ps aux | grep portal_server | grep -v grep') + '</div>';
    html += '<div class="cmd-card"><div class="cmd-label">Context window usage</div>' + cmdBlock('cat /tmp/claude_context_pct.txt 2>/dev/null || echo "not tracked"') + '</div>';
    html += '</div></div>';

    // Log Tailing
    html += '<div class="cmd-section">';
    html += '<div class="cmd-section-title">&#x1F4C4; Log Files</div>';
    html += '<div class="cmd-cards-grid">';
    html += '<div class="cmd-card"><div class="cmd-label">Portal log</div>' + cmdBlock('tail -50 ' + pdir + '/portal.log') + '</div>';
    html += '<div class="cmd-card"><div class="cmd-label">Telegram bridge log</div>' + cmdBlock('tail -50 ' + ldir + '/telegram_bridge.log') + '</div>';
    html += '<div class="cmd-card"><div class="cmd-label">PureBrain web conversations</div>' + cmdBlock('tail -20 ' + ldir + '/purebrain_web_conversations.jsonl') + '</div>';
    html += '<div class="cmd-card"><div class="cmd-label">All log files</div>' + cmdBlock('ls -lht ' + ldir + '/ | head -20') + '</div>';
    html += '</div></div>';

    // Restarts
    html += '<div class="cmd-section">';
    html += '<div class="cmd-section-title">&#x1F504; Restarts</div>';
    html += '<div class="cmd-cards-grid">';
    html += '<div class="cmd-card"><div class="cmd-label">Restart Telegram bridge</div>' + cmdBlock('pkill -f telegram_bridge.py; rm -f ' + home + '/.telegram_bridge.pid; nohup python3 ' + tdir + '/telegram_bridge.py >> ' + ldir + '/telegram_bridge.log 2>&1 &') + '</div>';
    html += '<div class="cmd-card"><div class="cmd-label">Restart portal server</div>' + cmdBlock('cd ' + pdir + ' && kill $(pgrep -f portal_server.py) 2>/dev/null; sleep 1; nohup python3 portal_server.py > portal.log 2>&1 &') + '<div class="cmd-warn">&#x26A0; Warn Jared before restarting — brief 502 during restart</div></div>';
    html += '<div class="cmd-card"><div class="cmd-label">Compact context (in Claude)</div>' + cmdBlock('tmux send-keys -t ' + (t.primary_session||sess) + ':0.0 "/compact" Enter') + '</div>';
    html += '<div class="cmd-card"><div class="cmd-label">Kill a stuck process</div>' + cmdBlock('kill PID_NUMBER') + '<div class="cmd-note">Get PID from: ps aux | grep PROCESS_NAME</div></div>';
    html += '</div></div>';

    // tmux Reference
    html += '<div class="cmd-section">';
    html += '<div class="cmd-section-title">&#x1F5A5; tmux Quick Reference</div>';
    html += '<div class="cmd-cards-grid">';
    html += '<div class="cmd-card"><div class="cmd-label">List all sessions</div>' + cmdBlock('tmux list-sessions') + '</div>';
    html += '<div class="cmd-card"><div class="cmd-label">Create new named session</div>' + cmdBlock('tmux new-session -d -s SESSION_NAME "COMMAND"') + '</div>';
    html += '<div class="cmd-card"><div class="cmd-label">Kill a session</div>' + cmdBlock('tmux kill-session -t SESSION_NAME') + '</div>';
    html += '<div class="cmd-card"><div class="cmd-label">Scroll up (view history)</div>' + cmdBlock('Ctrl+B then [ then arrows/PgUp. Press q to exit.') + '</div>';
    html += '</div></div>';

    // Quick Reference Table
    html += '<div class="cmd-section">';
    html += '<div class="cmd-section-title">&#x1F4CB; Quick Reference</div>';
    html += '<div class="cmd-cards-grid single"><div class="cmd-card">';
    html += '<table class="cmd-ref-table"><thead><tr><th>Item</th><th>Value</th></tr></thead><tbody>';
    html += '<tr><td class="ref-key">Server IP</td><td class="ref-val">' + ip + '</td></tr>';
    html += '<tr><td class="ref-key">SSH Port</td><td class="ref-val">' + port + '</td></tr>';
    html += '<tr><td class="ref-key">SSH User</td><td class="ref-val">' + user + '</td></tr>';
    html += '<tr><td class="ref-key">Primary Session</td><td class="ref-val">' + sess + '</td></tr>';
    html += '<tr><td class="ref-key">Portal URL</td><td class="ref-val">' + purl + '</td></tr>';
    html += '<tr><td class="ref-key">CIV Root</td><td class="ref-val">' + croot + '</td></tr>';
    html += '<tr><td class="ref-key">Portal Dir</td><td class="ref-val">' + pdir + '</td></tr>';
    html += '<tr><td class="ref-key">Tools Dir</td><td class="ref-val">' + tdir + '</td></tr>';
    html += '<tr><td class="ref-key">Logs Dir</td><td class="ref-val">' + ldir + '</td></tr>';
    html += '<tr><td class="ref-key">tmux Detach</td><td class="ref-val">Ctrl+B then D</td></tr>';
    html += '</tbody></table>';
    html += '</div></div></div>';

    // Troubleshooting
    html += '<div class="cmd-section">';
    html += '<div class="cmd-section-title danger">&#x1F6A8; Troubleshooting</div>';
    html += '<div class="cmd-cards-grid">';
    html += '<div class="cmd-card danger-card"><div class="cmd-label danger">AI not responding on Telegram</div>';
    html += '<div class="cmd-step">1. Check Claude session:</div>' + cmdBlock('tmux has-session -t ' + (t.primary_session||sess) + ' && echo ALIVE || echo DEAD');
    html += '<div class="cmd-step">2. Check Telegram bridge:</div>' + cmdBlock('ps aux | grep telegram_bridge | grep -v grep');
    html += '<div class="cmd-step">3. Restart bridge if down:</div>' + cmdBlock('pkill -f telegram_bridge.py; nohup python3 ' + tdir + '/telegram_bridge.py >> ' + ldir + '/telegram_bridge.log 2>&1 &');
    html += '</div>';
    html += '<div class="cmd-card danger-card"><div class="cmd-label danger">Portal not loading (502/down)</div>';
    html += '<div class="cmd-step">1. Check portal process:</div>' + cmdBlock('ps aux | grep portal_server | grep -v grep');
    html += '<div class="cmd-step">2. Restart portal:</div>' + cmdBlock('cd ' + pdir + ' && nohup python3 portal_server.py > portal.log 2>&1 &');
    html += '<div class="cmd-warn">&#x26A0; Warn Jared first — he gets 502 errors during restarts</div>';
    html += '</div>';
    html += '<div class="cmd-card danger-card"><div class="cmd-label danger">Context above 80% (risk of crash)</div>';
    html += cmdBlock('tmux send-keys -t ' + (t.primary_session||sess) + ':0.0 "/compact" Enter');
    html += '<div class="cmd-note">Inject /compact to free context window</div></div>';
    html += '<div class="cmd-card danger-card"><div class="cmd-label danger">Duplicate Telegram messages</div>';
    html += cmdBlock('pkill -f telegram_bridge.py; sleep 2; nohup python3 ' + tdir + '/telegram_bridge.py >> ' + ldir + '/telegram_bridge.log 2>&1 &');
    html += '</div>';
    html += '</div></div>';

    container.innerHTML = html;
  }

  // ============================================================
  // SHORTCUTS PANEL
  // ============================================================
  var shortcutsLoaded = false;

  window.loadShortcuts = function() {
    console.log('[SC] loadShortcuts called. loaded=' + shortcutsLoaded);
    if (shortcutsLoaded) return;
    var container = document.getElementById('shortcuts-content');
    if (!container) { console.error('[SC] shortcuts-content not found!'); return; }
    container.innerHTML = '<div style="padding:20px;color:var(--teal);font-size:0.82rem;">Fetching shortcuts...</div>';

    fetch('/api/shortcuts', { headers: authGet() })
      .then(function(r) {
        console.log('[SC] fetch status=' + r.status);
        return r.json();
      })
      .then(function(d) {
        console.log('[SC] data keys=' + Object.keys(d).join(','));
        shortcutsLoaded = true;
        renderShortcuts(d, container);
      })
      .catch(function(e) {
        console.error('[SC] Error:', e);
        container.innerHTML = '<div style="padding:20px;color:#ef4444;font-size:0.82rem;">Failed to load: ' + escHtml(e.message) + '</div>';
      });
  };

  function renderShortcuts(d, container) {
    var html = '';

    // Slash Commands
    var cmds = d.slash_commands || [];
    html += '<div class="sc-section"><div class="sc-section-title">&#x2215; Slash Commands</div>';
    html += '<table class="sc-table"><thead><tr><th>Command</th><th>Description</th><th>Type</th></tr></thead><tbody>';
    for (var i = 0; i < cmds.length; i++) {
      var c = cmds[i];
      var badge = c.type === 'built-in' ? '<span class="sc-badge-builtin">built-in</span>' : '<span class="sc-badge-custom">custom</span>';
      html += '<tr><td><span class="sc-cmd">' + escHtml(c.cmd) + '</span></td><td style="color:var(--text-dim)">' + escHtml(c.desc) + '</td><td>' + badge + '</td></tr>';
    }
    html += '</tbody></table></div>';

    // Keyboard Shortcuts
    var keys = d.keyboard_shortcuts || [];
    html += '<div class="sc-section"><div class="sc-section-title">&#x2328; Keyboard Shortcuts</div>';
    html += '<table class="sc-table"><thead><tr><th>Keys</th><th>Action</th><th>Where</th></tr></thead><tbody>';
    for (var i = 0; i < keys.length; i++) {
      var k = keys[i];
      var ks = (k.keys || []).map(function(key, idx) {
        var out = '<span class="sc-key">' + escHtml(key) + '</span>';
        return out;
      }).join('<span class="sc-plus">+</span>');
      html += '<tr><td>' + ks + '</td><td style="color:var(--text-dim)">' + escHtml(k.desc) + '</td><td style="color:var(--text-dim);font-size:0.7rem;">' + escHtml(k.context) + '</td></tr>';
    }
    html += '</tbody></table></div>';

    // Chat Features
    var feats = d.chat_features || [];
    html += '<div class="sc-section"><div class="sc-section-title">&#x25C8; Chat Features</div>';
    html += '<table class="sc-table"><thead><tr><th>Feature</th><th>How It Works</th></tr></thead><tbody>';
    for (var i = 0; i < feats.length; i++) {
      html += '<tr><td style="font-weight:600;color:var(--text)">' + escHtml(feats[i].feature) + '</td><td style="color:var(--text-dim)">' + escHtml(feats[i].desc) + '</td></tr>';
    }
    html += '</tbody></table></div>';

    // BOOP Automation
    var boops = d.boop_automation || [];
    html += '<div class="sc-section"><div class="sc-section-title">&#x26A1; BOOP Automation</div>';
    html += '<table class="sc-table"><thead><tr><th>Name</th><th>Trigger</th><th>What It Does</th></tr></thead><tbody>';
    for (var i = 0; i < boops.length; i++) {
      var b = boops[i];
      html += '<tr><td style="font-weight:600;color:var(--text)">' + escHtml(b.name) + '</td><td style="color:var(--teal);font-size:0.72rem;">' + escHtml(b.trigger) + '</td><td style="color:var(--text-dim)">' + escHtml(b.desc) + '</td></tr>';
    }
    html += '</tbody></table></div>';

    // Sidebar Tabs
    var tabs = d.sidebar_tabs || [];
    html += '<div class="sc-section"><div class="sc-section-title">&#x25A6; Sidebar Panels</div>';
    html += '<div class="sc-tabs-grid">';
    for (var i = 0; i < tabs.length; i++) {
      var tab = tabs[i];
      html += '<div class="sc-tab-card">';
      html += '<div class="sc-tab-icon">' + escHtml(tab.icon) + '</div>';
      html += '<div><div class="sc-tab-name">' + escHtml(tab.name) + '</div><div class="sc-tab-desc">' + escHtml(tab.desc) + '</div></div>';
      html += '</div>';
    }
    html += '</div></div>';

    container.innerHTML = html;
  }

  console.log('[PureBrain Portal] Commands / Shortcuts panels loaded');
})();
