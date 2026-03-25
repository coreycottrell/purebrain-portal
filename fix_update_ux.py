#!/usr/bin/env python3
"""
fix_update_ux.py -- Apply UX fixes to the portal update flow.

Changes:
1. HTML: Add remote version display, dedicated success message element
2. JS: Version-aware confirm dialog, human-readable step labels,
       success message with version info, stored version state

Run: python3 fix_update_ux.py
"""

import re
import sys
from pathlib import Path

HTML_FILE = Path(__file__).parent / "portal-pb-styled.html"

def main():
    if not HTML_FILE.exists():
        print(f"ERROR: {HTML_FILE} not found")
        sys.exit(1)

    content = HTML_FILE.read_text(encoding="utf-8")
    original = content

    # =========================================================================
    # PATCH 1: HTML -- Replace update-available-info section to add remote version
    # =========================================================================
    old_html_available = (
        '      <div id="update-available-info" style="display:none;flex-direction:column;gap:8px;">\n'
        '        <div style="font-size:0.85rem;color:#e8ecf4;">\n'
        '          <span id="update-commits-behind"></span> new commit(s) available\n'
        '        </div>\n'
        '        <div id="update-changelog" style="max-height:160px;overflow-y:auto;background:var(--surface2);border:1px solid var(--border2);border-radius:6px;padding:10px;font-size:0.82rem;font-family:var(--font-mono);color:#e8ecf4;line-height:1.5;"></div>\n'
        '      </div>'
    )
    new_html_available = (
        '      <div id="update-available-info" style="display:none;flex-direction:column;gap:8px;">\n'
        '        <div style="display:flex;align-items:center;gap:8px;font-size:0.88rem;color:#e8ecf4;flex-wrap:wrap;">\n'
        '          <span>Update available:</span>\n'
        '          <code id="update-remote-version" style="color:#22c55e;font-weight:700;">v--</code>\n'
        '          <span style="color:#b0b8c8;font-size:0.82rem;">(<span id="update-commits-behind"></span> commit(s))</span>\n'
        '        </div>\n'
        '        <div id="update-changelog" style="max-height:160px;overflow-y:auto;background:var(--surface2);border:1px solid var(--border2);border-radius:6px;padding:10px;font-size:0.82rem;font-family:var(--font-mono);color:#e8ecf4;line-height:1.5;"></div>\n'
        '      </div>'
    )

    if old_html_available not in content:
        print("WARNING: Could not find old HTML available section -- may have already been patched")
    else:
        content = content.replace(old_html_available, new_html_available, 1)
        print("PATCH 1: Replaced update-available-info HTML (added remote version display)")

    # =========================================================================
    # PATCH 2: HTML -- Add dedicated success message element after uptodate-msg
    # =========================================================================
    old_uptodate_line = (
        '      <div id="update-uptodate-msg" style="display:none;font-size:0.85rem;color:#22c55e;">&#x2714; Portal is up to date.</div>\n'
        '      <div id="update-error-msg" style="display:none;font-size:0.85rem;color:#ef4444;"></div>'
    )
    new_uptodate_line = (
        '      <div id="update-uptodate-msg" style="display:none;font-size:0.85rem;color:#22c55e;">&#x2714; Portal is up to date.</div>\n'
        '      <div id="update-success-msg" style="display:none;flex-direction:column;gap:6px;background:rgba(34,197,94,0.08);border:1px solid rgba(34,197,94,0.25);border-radius:8px;padding:12px;font-size:0.85rem;color:#22c55e;"></div>\n'
        '      <div id="update-error-msg" style="display:none;font-size:0.85rem;color:#ef4444;"></div>'
    )

    if old_uptodate_line not in content:
        print("WARNING: Could not find uptodate/error msg block -- may have already been patched")
    else:
        content = content.replace(old_uptodate_line, new_uptodate_line, 1)
        print("PATCH 2: Added update-success-msg element")

    # =========================================================================
    # PATCH 3: JS -- Replace entire update functions block
    # =========================================================================
    old_js_start = "  // --- Portal Update Functions ---\n\n  var _updateCheckInterval = null;"
    old_js_end = "  window.checkForUpdates = checkForUpdates;\n  window.applyUpdate = applyUpdate;\n  window.pollUpdateStatus = pollUpdateStatus;"

    start_idx = content.find(old_js_start)
    end_idx = content.find(old_js_end)

    if start_idx < 0 or end_idx < 0:
        print("ERROR: Could not find JS update functions block boundaries")
        sys.exit(1)

    end_idx += len(old_js_end)

    new_js_block = r'''  // --- Portal Update Functions ---

  var _updateCheckInterval = null;
  var _updateCurrentVersion = '';
  var _updateTargetVersion = '';

  function _escapeHtmlUpdate(str) {
    var d = document.createElement('div');
    d.appendChild(document.createTextNode(str));
    return d.innerHTML;
  }

  function showSettingsUpdateDot(show) {
    var gear = document.getElementById('settings-btn');
    if (!gear) return;
    var dot = document.getElementById('settings-update-dot');
    if (show && !dot) {
      dot = document.createElement('span');
      dot.id = 'settings-update-dot';
      dot.style.cssText = 'position:absolute;top:2px;right:2px;width:8px;height:8px;background:#22c55e;border-radius:50%;';
      gear.style.position = 'relative';
      gear.appendChild(dot);
    } else if (!show && dot) {
      dot.remove();
    }
  }

  function checkForUpdates() {
    var btn = document.getElementById('update-check-btn');
    if (!btn) return;
    btn.textContent = 'Checking...';
    btn.disabled = true;

    // Hide previous messages
    var successMsg = document.getElementById('update-success-msg');
    if (successMsg) successMsg.style.display = 'none';

    fetch('/api/update/check', {
      headers: { 'Authorization': 'Bearer ' + token }
    })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      btn.textContent = 'Check for Updates';
      btn.disabled = false;

      document.getElementById('update-error-msg').style.display = 'none';

      if (data.status === 'available') {
        _updateCurrentVersion = data.current_version || '';
        // Derive target version from changelog or remote info
        _updateTargetVersion = '';
        if (data.changelog && data.changelog.length > 0) {
          // Try to extract version from latest commit message (e.g., "v1.2.1" or "Release 1.2.1")
          var latestMsg = data.changelog[0].message || '';
          var verMatch = latestMsg.match(/v?(\d+\.\d+\.\d+)/);
          if (verMatch) _updateTargetVersion = verMatch[1];
        }

        document.getElementById('update-current-version').textContent = 'v' + data.current_version;
        document.getElementById('update-current-sha').textContent = data.current_sha.substring(0, 7);
        document.getElementById('update-commits-behind').textContent = data.commits_behind;

        // Set remote version display
        var remoteVerEl = document.getElementById('update-remote-version');
        if (remoteVerEl) {
          remoteVerEl.textContent = _updateTargetVersion ? 'v' + _updateTargetVersion : data.remote_sha.substring(0, 7);
        }

        var cl = document.getElementById('update-changelog');
        cl.innerHTML = '';
        (data.changelog || []).forEach(function(c) {
          cl.innerHTML += '<div>' + c.sha.substring(0,7) + ' ' + _escapeHtmlUpdate(c.message) + '</div>';
        });

        document.getElementById('update-available-info').style.display = 'flex';
        document.getElementById('update-uptodate-msg').style.display = 'none';
        document.getElementById('update-apply-btn').style.display = 'inline-block';
        document.getElementById('update-badge').style.display = 'inline-block';
        showSettingsUpdateDot(true);

      } else if (data.status === 'up_to_date') {
        document.getElementById('update-current-version').textContent = 'v' + data.current_version;
        document.getElementById('update-current-sha').textContent = data.current_sha.substring(0, 7);
        document.getElementById('update-available-info').style.display = 'none';
        document.getElementById('update-uptodate-msg').style.display = 'block';
        document.getElementById('update-apply-btn').style.display = 'none';
        document.getElementById('update-badge').style.display = 'none';
        showSettingsUpdateDot(false);
        // Also clear any release-notes badge -- portal is current
        var newBadge = document.getElementById('settings-new-badge');
        if (newBadge) newBadge.style.display = 'none';

      } else if (data.status === 'error') {
        document.getElementById('update-error-msg').textContent = data.error;
        document.getElementById('update-error-msg').style.display = 'block';
      }
    })
    .catch(function(err) {
      btn.textContent = 'Check for Updates';
      btn.disabled = false;
      document.getElementById('update-error-msg').textContent = 'Network error: ' + err.message;
      document.getElementById('update-error-msg').style.display = 'block';
    });
  }

  function applyUpdate() {
    var currentVersion = _updateCurrentVersion || '?';
    var _updateTargetVersion_display = _updateTargetVersion || 'latest';
    var confirmMsg = 'This will update the portal from v' + currentVersion
      + ' to ' + (_updateTargetVersion ? 'v' + _updateTargetVersion : _updateTargetVersion_display)
      + ' and restart.\n\nContinue?';
    if (!confirm(confirmMsg)) return;

    var btn = document.getElementById('update-apply-btn');
    btn.style.display = 'none';
    document.getElementById('update-check-btn').style.display = 'none';
    document.getElementById('update-progress').style.display = 'flex';
    document.getElementById('update-progress-text').textContent = 'Starting update...';
    document.getElementById('update-steps').innerHTML = '';

    fetch('/api/update/apply', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json' },
      body: '{}'
    })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.status === 'started') {
        pollUpdateStatus(data.job_id);
      } else {
        document.getElementById('update-progress').style.display = 'none';
        document.getElementById('update-check-btn').style.display = 'inline-block';
        document.getElementById('update-error-msg').textContent = data.error || 'Unexpected response';
        document.getElementById('update-error-msg').style.display = 'block';
        btn.style.display = 'inline-block';
      }
    })
    .catch(function(err) {
      document.getElementById('update-progress').style.display = 'none';
      document.getElementById('update-check-btn').style.display = 'inline-block';
      document.getElementById('update-error-msg').textContent = 'Failed to start update: ' + err.message;
      document.getElementById('update-error-msg').style.display = 'block';
      btn.style.display = 'inline-block';
    });
  }

  var _updateStepLabels = {
    'starting':         'Initializing update...',
    'fetch':            'Fetching latest changes...',
    'compare':          'Comparing versions...',
    'check_tree':       'Checking file tree...',
    'record_rollback':  'Recording rollback point...',
    'verify_custom':    'Verifying custom files...',
    'verify_preserved': 'Verifying preserved files...',
    'pull':             'Pulling new code...',
    'running_tests':    'Running tests...',
    'read_version':     'Reading new version...',
    'restart':          'Restarting portal...'
  };

  function pollUpdateStatus(jobId) {
    var stepsEl = document.getElementById('update-steps');
    var completedSet = {};

    var poll = setInterval(function() {
      fetch('/api/update/status', {
        headers: { 'Authorization': 'Bearer ' + token }
      })
      .then(function(r) { return r.json(); })
      .then(function(data) {
        if (data.status === 'in_progress') {
          var stepName = data.step || 'starting';
          var label = _updateStepLabels[stepName] || stepName.replace(/_/g, ' ');
          document.getElementById('update-progress-text').textContent = label;

          // Show completed steps as a list
          (data.steps_completed || []).forEach(function(s) {
            if (!completedSet[s]) {
              completedSet[s] = true;
              var stepLabel = _updateStepLabels[s] || s.replace(/_/g, ' ');
              stepsEl.innerHTML += '<div style="color:#22c55e;">&#x2714; ' + _escapeHtmlUpdate(stepLabel) + '</div>';
            }
          });

        } else if (data.status === 'success') {
          clearInterval(poll);
          document.getElementById('update-progress').style.display = 'none';
          document.getElementById('update-available-info').style.display = 'none';
          document.getElementById('update-badge').style.display = 'none';
          document.getElementById('update-check-btn').style.display = 'inline-block';
          showSettingsUpdateDot(false);

          // Show dedicated success message with version info
          var successEl = document.getElementById('update-success-msg');
          if (successEl) {
            var newVer = data.new_version || _updateTargetVersion || 'latest';
            successEl.innerHTML =
              '<div style="font-weight:700;">&#x2714; Updated to v' + _escapeHtmlUpdate(newVer) + ' successfully!</div>' +
              '<div style="font-size:0.82rem;color:#b0b8c8;">Portal will restart in 5 seconds. The page will reload automatically.</div>';
            successEl.style.display = 'flex';
          }

          setTimeout(function() { location.reload(); }, 5000);

        } else if (data.status === 'failed') {
          clearInterval(poll);
          document.getElementById('update-progress').style.display = 'none';
          document.getElementById('update-check-btn').style.display = 'inline-block';

          var errMsg = 'Update failed: ' + (data.error || 'Unknown error');
          if (data.rolled_back_to) {
            errMsg += ' (rolled back to ' + data.rolled_back_to.substring(0,7) + ')';
          }
          document.getElementById('update-error-msg').textContent = errMsg;
          document.getElementById('update-error-msg').style.display = 'block';
          document.getElementById('update-apply-btn').style.display = 'inline-block';
        }
      })
      .catch(function() {
        document.getElementById('update-progress-text').textContent = 'Portal restarting...';
      });
    }, 2000);

    // Safety timeout: stop polling after 3 minutes
    setTimeout(function() { clearInterval(poll); }, 180000);
  }

  // Auto-check for updates 5s after page load and every 24 hours
  setTimeout(function() {
    if (token) checkForUpdates();
  }, 5000);
  _updateCheckInterval = setInterval(function() {
    if (token) checkForUpdates();
  }, 24 * 60 * 60 * 1000);

  window.checkForUpdates = checkForUpdates;
  window.applyUpdate = applyUpdate;
  window.pollUpdateStatus = pollUpdateStatus;'''

    content = content[:start_idx] + new_js_block + content[end_idx:]
    print("PATCH 3: Replaced JS update functions block")

    # =========================================================================
    # Verify patches applied
    # =========================================================================
    checks = [
        ('update-remote-version', 'Remote version element in HTML'),
        ('update-success-msg', 'Success message element in HTML'),
        ('_updateCurrentVersion', 'Version state variables in JS'),
        ('_updateTargetVersion', 'Target version tracking in JS'),
        ('_updateStepLabels', 'Human-readable step labels in JS'),
        ('data.new_version', 'New version in success handler'),
        ('This will update the portal from', 'Version-aware confirm dialog'),
    ]

    all_ok = True
    for needle, desc in checks:
        if needle not in content:
            print(f"FAIL: {desc} -- '{needle}' not found in output")
            all_ok = False
        else:
            print(f"  OK: {desc}")

    if not all_ok:
        print("\nERROR: Some patches failed verification. File NOT written.")
        sys.exit(1)

    # Write back
    HTML_FILE.write_text(content, encoding="utf-8")
    print(f"\nSUCCESS: All patches applied to {HTML_FILE}")
    print(f"File size: {len(content)} characters")

    # Quick diff stats
    added = len(content) - len(original)
    print(f"Net change: +{added} characters")


if __name__ == "__main__":
    main()
