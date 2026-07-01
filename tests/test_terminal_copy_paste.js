#!/usr/bin/env node
/**
 * Terminal Copy/Paste -- Unit Tests
 *
 * The portal "terminal" (AI Team Monitor pane) is a read-only <div> that
 * renders tmux pane content. Users could not copy text (e.g. the Claude
 * auth URL) out of it, nor paste text (e.g. the auth code) back into the
 * running Claude session. This adds both.
 *
 * Strategy: terminal.js exposes pure, testable handlers on
 * window._portalTerminalClipboard. We load the IIFE in a sandbox with
 * mocked navigator.clipboard, document, and fetch, then assert behavior.
 *
 * These tests FAIL before implementation and PASS after.
 *
 * Usage: node tests/test_terminal_copy_paste.js
 */

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..');
const TERMINAL_JS = path.join(ROOT, 'static', 'js', 'features', 'terminal.js');

let passed = 0;
let failed = 0;
function assert(cond, msg) {
  if (cond) { passed++; console.log(`  PASS: ${msg}`); }
  else { failed++; console.error(`  FAIL: ${msg}`); }
}

// --- Minimal DOM / browser sandbox -----------------------------------------
function makeSandbox(opts) {
  opts = opts || {};
  const clipboardStore = { value: opts.clipboardText || '' };
  const writes = [];
  const fetchCalls = [];
  const toasts = [];
  const docHandlers = {};
  const paneHandlers = {};
  let selectionText = opts.selectionText || '';
  // simulate the active pane id chosen for paste target
  const paneEl = {
    id: 'teamsPaneContent',
    textContent: opts.paneContent || '',
    scrollTop: 0,
    scrollHeight: 100,
    addEventListener: function (type, fn) { paneHandlers[type] = fn; },
    setAttribute: function () {},
    querySelector: function () { return null; },
    // contains(): used by selection-in-pane detection. By default the
    // selection's nodes are considered to live inside the pane.
    contains: function (node) { return node ? node.__inPane !== false : false; },
  };

  // Selection node that reports whether it lives inside the pane.
  function selNode(inPane) { return { __inPane: inPane !== false }; }

  const sandbox = {
    console: console,
    Promise: Promise,
    setTimeout: function (fn) { return 0; },
    clearTimeout: function () {},
    setInterval: function () { return 0; },
    clearInterval: function () {},
    encodeURIComponent: encodeURIComponent,
    location: { protocol: 'https:', host: 'localhost' },
    WebSocket: function () { this.close = function () {}; },
    navigator: {
      clipboard: {
        writeText: function (t) { writes.push(t); clipboardStore.value = t; return Promise.resolve(); },
        readText: function () { return Promise.resolve(clipboardStore.value); },
      },
    },
    document: {
      getElementById: function (id) { return id === 'teamsPaneContent' ? paneEl : null; },
      querySelector: function () { return null; },
      querySelectorAll: function () { return []; },
      addEventListener: function (type, fn) { docHandlers[type] = fn; },
      getSelection: makeSelection,
      hidden: false,
    },
  };

  // A getSelection() mock that mirrors real Selection semantics enough for the
  // selection-in-pane detection: rangeCount, isCollapsed, anchorNode/focusNode.
  // selectionText drives both toString() and whether the selection is "active".
  function makeSelection() {
    var active = !!(selectionText && selectionText.length);
    var anchor = selNode(opts.selectionInPane !== false); // default: inside pane
    return {
      toString: function () { return selectionText; },
      rangeCount: active ? 1 : 0,
      isCollapsed: !active,
      anchorNode: active ? anchor : null,
      focusNode: active ? anchor : null,
    };
  }
  sandbox.window = sandbox;
  sandbox._safeJson = function (r) { return r.json ? r.json() : r; };
  sandbox._tok = function () { return 'TESTTOKEN'; };
  sandbox.portalFetch = function () { return Promise.resolve({ json: function () { return {}; } }); };
  sandbox.escHtml = function (s) { return s; };
  sandbox.showToast = function (m, kind) { toasts.push({ m: m, kind: kind }); };
  sandbox.fetch = function (url, opt) {
    fetchCalls.push({ url: url, opt: opt });
    return Promise.resolve({ ok: true, json: function () { return Promise.resolve({ status: 'sent' }); } });
  };
  sandbox.getSelection = sandbox.document.getSelection;

  sandbox.__writes = writes;
  sandbox.__fetchCalls = fetchCalls;
  sandbox.__toasts = toasts;
  sandbox.__setSelection = function (s) { selectionText = s; };
  sandbox.__clipboard = clipboardStore;
  sandbox.__paneEl = paneEl;
  sandbox.__docHandlers = docHandlers;
  sandbox.__paneHandlers = paneHandlers;
  return sandbox;
}

function loadTerminal(sandbox) {
  const code = fs.readFileSync(TERMINAL_JS, 'utf8');
  vm.createContext(sandbox);
  vm.runInContext(code, sandbox, { filename: 'terminal.js' });
  return sandbox.window._portalTerminalClipboard;
}

async function run() {
  // ===========================================================================
  console.log('\n=== Section 1: API surface (keyboard + native only, no buttons) ===');
  {
    const sb = makeSandbox({});
    const api = loadTerminal(sb);
    assert(api && typeof api === 'object', 'window._portalTerminalClipboard is exposed');
    assert(api && typeof api.paste === 'function', 'paste() handler exists (Ctrl+V -> inject)');
    assert(api && typeof api.getActivePaneId === 'function', 'getActivePaneId() exists (paste target)');
    // Copy is native: there is intentionally NO custom copy() handler and no
    // document-level copy interception. The browser copies the selection itself.
    assert(typeof api.copy === 'undefined', 'no custom copy() handler — native browser copy is used');
    assert(typeof sb.__docHandlers.copy === 'undefined', 'no document-level copy interceptor');
  }

  // ===========================================================================
  console.log('\n=== Section 2: copy relies on native browser behavior (no clipboard writes) ===');
  {
    // With a real selection present, the module must NOT call clipboard.writeText
    // itself — the browser handles Ctrl+C natively now that the selection is
    // preserved. We assert the module performs no clipboard writes on its own.
    const sb = makeSandbox({ selectionText: 'https://claude.ai/auth?code=ABC', paneContent: 'whole pane text' });
    loadTerminal(sb);
    assert(sb.__writes.length === 0, 'module performs no clipboard.writeText (native copy owns it)');
  }

  // ===========================================================================
  console.log('\n=== Section 4: paste() reads clipboard and injects into active pane ===');
  {
    const sb = makeSandbox({ clipboardText: 'MY-AUTH-CODE-123' });
    const api = loadTerminal(sb);
    api.setActivePaneId('%7');           // simulate a selected pane
    await api.paste();
    assert(sb.__fetchCalls.length === 1, 'one fetch (inject) call happened');
    const call = sb.__fetchCalls[0];
    assert(call.url === '/api/inject/pane', 'pasted via /api/inject/pane endpoint');
    assert(call.opt && call.opt.method === 'POST', 'POST method used');
    const body = JSON.parse(call.opt.body);
    assert(body.message === 'MY-AUTH-CODE-123', 'clipboard text sent as message');
    assert(body.pane_id === '%7', 'sent to the active pane id');
    assert(/Bearer TESTTOKEN/.test(call.opt.headers.Authorization), 'auth header included');
  }

  // ===========================================================================
  console.log('\n=== Section 5: paste() with empty clipboard does nothing destructive ===');
  {
    const sb = makeSandbox({ clipboardText: '' });
    const api = loadTerminal(sb);
    api.setActivePaneId('%7');
    await api.paste();
    assert(sb.__fetchCalls.length === 0, 'no inject call for empty clipboard');
  }

  // ===========================================================================
  console.log('\n=== Section 6: keydown leaves Ctrl+C to native copy, intercepts Ctrl+V ===');
  {
    const sb = makeSandbox({ selectionText: 'selected bit', paneContent: 'whole pane' });
    loadTerminal(sb);
    const onKey = sb.__paneHandlers.keydown;
    assert(typeof onKey === 'function', 'a keydown handler is registered on the pane');

    // Ctrl+C must NOT be prevented — the browser performs the native copy.
    let cPrevented = false;
    onKey({ ctrlKey: true, key: 'c', preventDefault: function () { cPrevented = true; } });
    assert(cPrevented === false, 'Ctrl+C is left to native browser copy (not preventDefault-ed)');
    assert(sb.__writes.length === 0, 'no custom clipboard write on Ctrl+C');

    // Ctrl+V must be intercepted to inject into the active pane.
    let vPrevented = false;
    onKey({ ctrlKey: true, key: 'v', preventDefault: function () { vPrevented = true; } });
    assert(vPrevented === true, 'Ctrl+V is intercepted for inject-paste');
  }

  // ===========================================================================
  console.log('\n=== Section 7: re-render does NOT clobber an active selection in the pane ===');
  {
    // The bug: the pane re-renders constantly (WS/poll) by setting .textContent.
    // Setting .textContent destroys the text node -> clears the user selection.
    // _renderPane() must SKIP the update while a selection is active in the pane.
    const sb = makeSandbox({ paneContent: 'original text', selectionText: 'orig' });
    const api = loadTerminal(sb);
    assert(typeof api.renderPane === 'function', 'renderPane() exposed for testing');
    const before = sb.__paneEl.textContent;
    api.renderPane('BRAND NEW STREAMED CONTENT'); // arrives while user is selecting
    assert(sb.__paneEl.textContent === before,
      'pane content unchanged while selection active (textContent NOT rewritten)');
  }

  // ===========================================================================
  console.log('\n=== Section 8: re-render proceeds normally when there is no selection ===');
  {
    const sb = makeSandbox({ paneContent: 'old', selectionText: '' });
    const api = loadTerminal(sb);
    api.renderPane('fresh content');
    assert(sb.__paneEl.textContent === 'fresh content',
      'pane updates normally when no selection is active');
  }

  // ===========================================================================
  console.log('\n=== Section 9: selection OUTSIDE the pane does not block re-render ===');
  {
    // A selection elsewhere on the page (e.g. chat) must not freeze the terminal.
    const sb = makeSandbox({ paneContent: 'old', selectionText: 'somewhere else', selectionInPane: false });
    const api = loadTerminal(sb);
    api.renderPane('fresh content');
    assert(sb.__paneEl.textContent === 'fresh content',
      'pane updates when the active selection is outside the pane');
  }

  // ===========================================================================
  console.log('\n=== Section 10: buffered content is applied once the selection clears ===');
  {
    const sb = makeSandbox({ paneContent: 'v1', selectionText: 'v1' });
    const api = loadTerminal(sb);
    api.renderPane('v2'); // dropped/buffered while selecting
    assert(sb.__paneEl.textContent === 'v1', 'update buffered during selection');
    sb.__setSelection('');           // user releases the selection
    api.renderPane('v3');            // next update arrives with no selection
    assert(sb.__paneEl.textContent === 'v3', 'latest content applied after selection clears');
  }

  // ===========================================================================
  console.log('\n=== Section 11: identical content does not churn the DOM ===');
  {
    // Avoid needless textContent rewrites (which also clear selections).
    const sb = makeSandbox({ paneContent: 'same', selectionText: '' });
    const api = loadTerminal(sb);
    let writeCount = 0;
    let store = 'same';
    Object.defineProperty(sb.__paneEl, 'textContent', {
      get: function () { return store; },
      set: function (v) { writeCount++; store = v; },
      configurable: true,
    });
    api.renderPane('same'); // identical -> should be a no-op
    assert(writeCount === 0, 'no textContent write when content is unchanged');
    api.renderPane('different'); // changed -> should write once
    assert(writeCount === 1, 'one textContent write when content actually changes');
  }

  // ===========================================================================
  console.log('\n=== Section 12: copy is fully native — no module clipboard interception ===');
  {
    // Even with a selection inside the pane, the module must not register any
    // copy interceptor nor write to the clipboard. The browser's native copy
    // of the (now-preserved) selection is the entire copy story.
    const sb = makeSandbox({ paneContent: 'whole pane', selectionText: 'selected bit' });
    loadTerminal(sb);
    assert(typeof sb.__docHandlers.copy === 'undefined', 'no document-level copy listener registered');
    assert(typeof sb.__paneHandlers.copy === 'undefined', 'no pane-level copy listener registered');
    assert(sb.__writes.length === 0, 'no clipboard.writeText performed by the module');
  }

  // ---------------------------------------------------------------------------
  console.log(`\n=== RESULTS: ${passed} passed, ${failed} failed ===\n`);
  process.exit(failed === 0 ? 0 : 1);
}

run().catch(function (e) { console.error(e); process.exit(1); });
