#!/usr/bin/env node
/**
 * RUNTIME proof: terminal pane selection survives the LIVE renderer.
 *
 * Loads the actually-served portal in headless Chromium, authenticates with
 * the real portal token, lets terminal.js boot (the live renderer), then:
 *   1. asserts computed user-select on #teamsPaneContent is 'text' (CSS fix),
 *   2. creates a REAL selection inside #teamsPaneContent,
 *   3. invokes the REAL live renderer window._portalTerminalClipboard.renderPane()
 *      with brand-new content while the selection is active,
 *   4. asserts window.getSelection().toString() SURVIVES (not cleared),
 *   5. lets the real refresh interval tick and re-asserts the selection survives,
 *   6. confirms document.execCommand('copy') reads the surviving selection.
 *
 * Uses the playwright + chromium already cached under ~/.cache/ms-playwright
 * via the npx module cache. Usage: node tests/runtime_terminal_selection.js
 */

const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const PW = '/home/aiciv/.npm/_npx/e41f203b7505f1fb/node_modules/playwright';
const TOKEN = fs.readFileSync(path.join(ROOT, '.portal-token'), 'utf8').trim();
const URL = 'http://localhost:8097';

let passed = 0, failed = 0;
function assert(c, m) { if (c) { passed++; console.log('  PASS: ' + m); } else { failed++; console.error('  FAIL: ' + m); } }

(async () => {
  const { chromium } = require(PW);
  const browser = await chromium.launch({ headless: true });
  const ctx = await browser.newContext({ permissions: ['clipboard-read', 'clipboard-write'] });
  const page = await ctx.newPage();

  // Seed the token BEFORE the app scripts run so terminal.js boots authenticated.
  await page.addInitScript((tok) => {
    try { localStorage.setItem('portal_token', tok); } catch (e) {}
  }, TOKEN);

  await page.goto(URL, { waitUntil: 'domcontentloaded' });

  // Wait for terminal.js to install the live renderer.
  await page.waitForFunction(
    () => window._portalTerminalClipboard && typeof window._portalTerminalClipboard.renderPane === 'function',
    { timeout: 15000 }
  ).catch(() => {});

  const hasRenderer = await page.evaluate(
    () => !!(window._portalTerminalClipboard && window._portalTerminalClipboard.renderPane)
  );
  assert(hasRenderer, 'terminal.js live renderer (_portalTerminalClipboard.renderPane) is installed at runtime');

  // The AI Team Monitor panel (teamsArea) may default to display:none in a fresh
  // headless layout (no saved panel state). A real user has it open. Force it
  // visible + sized so #teamsPaneContent gets a layout box — without a layout box,
  // headless Chromium will not materialize a visual selection (getSelection
  // returns ''), which is a test artifact, not a product behavior.
  await page.evaluate(() => {
    const area = document.getElementById('teamsArea');
    if (area) { area.style.display = 'flex'; area.style.width = '600px'; area.style.height = '400px'; }
    const pane = document.getElementById('teamsPaneContent');
    if (pane) { pane.style.display = 'block'; pane.style.height = '300px'; pane.style.width = '560px'; }
  });
  const laidOut = await page.evaluate(() => {
    const el = document.getElementById('teamsPaneContent');
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  });
  assert(laidOut, 'terminal pane has a layout box (panel shown, as a real user sees it)');

  // 1. CSS: pane is user-selectable
  const us = await page.evaluate(() => {
    const el = document.getElementById('teamsPaneContent');
    if (!el) return 'NO-EL';
    return getComputedStyle(el).userSelect || getComputedStyle(el).webkitUserSelect;
  });
  assert(us === 'text', 'computed user-select on #teamsPaneContent is "text" (was "none") — got: ' + us);

  // 2+3. Seed deterministic content via the REAL renderer AND create the
  // selection in the SAME evaluate, so a high-cadence WS update can't overwrite
  // the content between seeding and selecting.
  const selText = await page.evaluate(() => {
    const api = window._portalTerminalClipboard;
    api.renderPane('LINE-A keep this\nLINE-B selectable text here\nLINE-C tail');
    const el = document.getElementById('teamsPaneContent');
    const node = el.firstChild; // single text node
    const full = node.textContent;
    const start = full.indexOf('selectable');
    const end = full.indexOf('text here') + 'text here'.length;
    const range = document.createRange();
    range.setStart(node, start);
    range.setEnd(node, end);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
    return sel.toString();
  });
  assert(selText && selText.indexOf('selectable') !== -1, 'a real selection was created inside the pane: "' + selText.slice(0, 30) + '..."');

  // 4. Live renderer fires NEW content while selection active -> must SKIP.
  const afterRender = await page.evaluate(() => {
    window._portalTerminalClipboard.renderPane('TOTALLY NEW STREAMED CONTENT\nmore lines\nand more');
    return { sel: window.getSelection().toString(), paneStartsWith: document.getElementById('teamsPaneContent').textContent.slice(0, 6) };
  });
  assert(afterRender.sel && afterRender.sel.indexOf('selectable') !== -1,
    'selection SURVIVES a live renderPane() call while active (got: "' + afterRender.sel.slice(0, 30) + '")');
  assert(afterRender.paneStartsWith === 'LINE-A',
    'pane content was NOT rewritten while selection active (still starts LINE-A)');

  // 5. Let the real refresh interval (WS/3s poll) tick; selection must persist.
  await page.waitForTimeout(3500);
  const afterTick = await page.evaluate(() => window.getSelection().toString());
  assert(afterTick && afterTick.indexOf('selectable') !== -1,
    'selection SURVIVES a real refresh-interval tick (got: "' + (afterTick || '').slice(0, 30) + '")');

  // 6. Copy reads the surviving selection (native path).
  const copied = await page.evaluate(() => {
    let captured = '';
    const h = (e) => { try { captured = window.getSelection().toString(); } catch (x) {} };
    document.addEventListener('copy', h, { once: true });
    document.execCommand('copy');
    return captured;
  });
  assert(copied && copied.indexOf('selectable') !== -1,
    'native copy reads the surviving selection (got: "' + (copied || '').slice(0, 30) + '")');

  await browser.close();
  console.log('\n=== RUNTIME RESULTS: ' + passed + ' passed, ' + failed + ' failed ===\n');
  process.exit(failed === 0 ? 0 : 1);
})().catch((e) => { console.error('RUNTIME ERROR:', e.message); process.exit(2); });
