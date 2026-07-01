/**
 * Shared auth helpers for portal feature modules.
 * Loaded BEFORE all feature scripts. No dependencies.
 */
(function() {
  'use strict';

  function _tok() {
    return localStorage.getItem('portal_token') || '';
  }

  function _auth() {
    return { 'Authorization': 'Bearer ' + _tok() };
  }

  function _authJson() {
    var h = _auth();
    h['Content-Type'] = 'application/json';
    return h;
  }

  function _safeJson(r) {
    var ct = (r.headers.get('content-type') || '');
    if (!r.ok) throw new Error('Server error ' + r.status);
    if (ct.indexOf('application/json') === -1) {
      return r.text().then(function(t) {
        try { return JSON.parse(t); }
        catch(e) { throw new Error('Non-JSON response'); }
      });
    }
    return r.json();
  }

  // Expose as window globals — modules call these directly,
  // so existing function calls don't need to change.
  window._tok = _tok;
  window._auth = _auth;
  window._authJson = _authJson;
  window._safeJson = _safeJson;
})();
