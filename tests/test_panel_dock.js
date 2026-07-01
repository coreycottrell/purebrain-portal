#!/usr/bin/env node
// Test suite for panel-manager.js + dock.js extraction
// Validates both bug fixes and that the extraction doesn't break existing behavior
// Uses minimal DOM simulation (no jsdom dependency needed)

const fs = require('fs');
const path = require('path');

let passed = 0;
let failed = 0;

function assert(condition, msg) {
  if (condition) { passed++; console.log(`  PASS: ${msg}`); }
  else { failed++; console.error(`  FAIL: ${msg}`); }
}

// --- Minimal DOM simulation ---
function createDOM() {
  const elements = {};
  const listeners = {};
  const classLists = {};

  function getClassList(id) {
    if (!classLists[id]) {
      const classes = new Set();
      classLists[id] = {
        add(c) { classes.add(c); },
        remove(c) { classes.delete(c); },
        toggle(c, force) {
          if (force === undefined) { classes.has(c) ? classes.delete(c) : classes.add(c); }
          else if (force) { classes.add(c); }
          else { classes.delete(c); }
        },
        contains(c) { return classes.has(c); },
        _classes: classes,
      };
    }
    return classLists[id];
  }

  function makeElement(id, attrs = {}) {
    const el = {
      id: id,
      style: {},
      classList: getClassList(id),
      getAttribute(name) { return attrs[name] || null; },
      setAttribute(name, val) { attrs[name] = val; },
      innerHTML: '',
      textContent: '',
      offsetHeight: 280,
      closest() { return null; },
      click() {
        const handlers = listeners[id] || [];
        handlers.filter(h => h.type === 'click').forEach(h => h.fn({ preventDefault() {} }));
      },
      addEventListener(type, fn) {
        if (!listeners[id]) listeners[id] = [];
        listeners[id].push({ type, fn });
      },
      querySelectorAll() { return []; },
    };
    elements[id] = el;
    return el;
  }

  // Pre-create needed elements
  const app = makeElement('app');
  const chatArea = makeElement('chatArea');
  const filesArea = makeElement('filesArea');
  const tasksArea = makeElement('tasksArea');
  const teamsArea = makeElement('teamsArea');
  const settingsArea = makeElement('settingsArea');
  const dockChatBtn = makeElement('dockChatBtn');
  const topnavDockBtn = makeElement('topnavDockBtn');
  const dockResetBtn = makeElement('dockResetBtn');
  const dockResize = makeElement('dockResize');
  const customPanelsGroup = makeElement('custom-panels-group');
  const panelKanban = makeElement('panel-kanban');
  const modsCustomPanels = makeElement('mods-custom-panels');

  // Sidebar items
  const sidebarChat = makeElement('sidebar-chat', { 'data-tab': 'chat' });
  sidebarChat.classList.add('sidebar-item');
  const sidebarFiles = makeElement('sidebar-files', { 'data-tab': 'files' });
  sidebarFiles.classList.add('sidebar-item');
  const sidebarTasks = makeElement('sidebar-tasks', { 'data-tab': 'tasks' });
  sidebarTasks.classList.add('sidebar-item');
  const sidebarTeams = makeElement('sidebar-teams', { 'data-tab': 'teams' });
  sidebarTeams.classList.add('sidebar-item');

  // Custom nav item
  const navKanban = makeElement('nav-kanban', { 'data-panel': 'kanban' });
  navKanban.classList.add('nav-item');
  navKanban.textContent = 'Kanban';

  // Mock localStorage
  const storage = {};
  global.localStorage = {
    getItem(k) { return storage[k] || null; },
    setItem(k, v) { storage[k] = v; },
    removeItem(k) { delete storage[k]; },
  };

  // Build allPanelIds for the built-in panels
  const builtinPanelIds = [
    'letstalkArea','chatArea','tasksArea','filesArea','teamsArea','todoArea',
    'agentsArea','agent-rosterArea','referArea','aboutArea','hubArea',
    'constitutionArea','deploymentsArea','paymentsArea','settingsArea','clientsArea'
  ];

  // Mock document
  global.document = {
    querySelector(sel) {
      if (sel === '.app') return app;
      if (sel.includes('data-tab="chat"')) return sidebarChat;
      if (sel.includes('data-tab="files"')) return sidebarFiles;
      if (sel.includes('data-tab="tasks"')) return sidebarTasks;
      if (sel.includes('data-tab="teams"')) return sidebarTeams;
      if (sel.includes('data-panel="kanban"')) return navKanban;
      return null;
    },
    querySelectorAll(sel) {
      if (sel === '.sidebar-item') return [sidebarChat, sidebarFiles, sidebarTasks, sidebarTeams];
      if (sel === '.sidebar-item[data-tab]') return [sidebarChat, sidebarFiles, sidebarTasks, sidebarTeams];
      if (sel === '.nav-item[data-panel]') return [navKanban];
      if (sel === '[id^="panel-"]') return [panelKanban];
      const match = sel.match(/^#(.+)/);
      if (match && elements[match[1]]) return [elements[match[1]]];
      // Handle class-based selectors for dock padding
      if (sel.includes('.chat-area') || sel.includes('.files-area')) {
        return builtinPanelIds.map(id => elements[id]).filter(Boolean);
      }
      return [];
    },
    getElementById(id) { return elements[id] || null; },
    addEventListener(type, fn) {
      if (!listeners['__document__']) listeners['__document__'] = [];
      listeners['__document__'].push({ type, fn });
    },
    body: { style: {} },
  };

  global.window = {
    innerWidth: 1200,
    addEventListener() {},
    _customPanelHandlers: {},
  };

  return { elements, app, chatArea, filesArea, tasksArea, teamsArea, settingsArea, panelKanban, sidebarChat, sidebarFiles, sidebarTasks, sidebarTeams, navKanban };
}

// --- Load modules ---
function loadModules() {
  // Clear module state
  delete global.window.PanelManager;
  delete global.window.DockManager;
  delete global.window.allPanels;
  delete global.window.toggleDockChat;
  delete global.window.resetDockHeight;

  // Load panel-manager.js
  const pmCode = fs.readFileSync(path.join(__dirname, '..', 'static', 'js', 'core', 'panel-manager.js'), 'utf8');
  eval(pmCode);

  // Load dock.js
  const dockCode = fs.readFileSync(path.join(__dirname, '..', 'static', 'js', 'features', 'dock.js'), 'utf8');
  eval(dockCode);
}

// ==================== TESTS ====================

console.log('\n=== Test: Module Loading ===');
{
  const dom = createDOM();
  loadModules();
  assert(typeof window.PanelManager === 'object', 'PanelManager exists on window');
  assert(typeof window.DockManager === 'object', 'DockManager exists on window');
  assert(typeof window.toggleDockChat === 'function', 'toggleDockChat global exists');
  assert(typeof window.resetDockHeight === 'function', 'resetDockHeight global exists');
  assert(Array.isArray(window.allPanels), 'allPanels legacy array exists');
  assert(window.allPanels.includes('chatArea'), 'allPanels includes chatArea');
  assert(window.allPanels.includes('filesArea'), 'allPanels includes filesArea');
}

console.log('\n=== Test: Panel Switching (UNDOCKED) ===');
{
  const dom = createDOM();
  loadModules();
  const PM = window.PanelManager;

  // Switch to files
  PM.switchTo('files');
  assert(PM.getActivePanel() === 'files', 'Active panel is files');
  assert(dom.filesArea.classList.contains('visible'), 'Files panel visible');
  assert(dom.chatArea.style.display === 'none', 'Chat hidden when showing files');

  // Switch to chat
  PM.switchTo('chat');
  assert(PM.getActivePanel() === 'chat', 'Active panel is chat');
  assert(dom.chatArea.style.display === 'flex', 'Chat visible');
  assert(!dom.filesArea.classList.contains('visible'), 'Files hidden when showing chat');
}

console.log('\n=== Test: Custom Panel Registration ===');
{
  const dom = createDOM();
  loadModules();
  const PM = window.PanelManager;

  PM.registerPanel('kanban', { elementId: 'panel-kanban' });
  assert(PM._customPanels['kanban'], 'Kanban registered');
  assert(PM._customPanels['kanban'].elementId === 'panel-kanban', 'Kanban element ID correct');
}

console.log('\n=== Test: Custom Panel Switching (UNDOCKED) ===');
{
  const dom = createDOM();
  loadModules();
  const PM = window.PanelManager;
  PM.registerPanel('kanban', { elementId: 'panel-kanban' });

  // Switch to kanban (custom panel)
  PM.switchTo('kanban');
  assert(PM.getActivePanel() === 'kanban', 'Active panel is kanban');
  assert(dom.panelKanban.classList.contains('visible'), 'Kanban panel visible');
  assert(dom.chatArea.style.display === 'none', 'Chat hidden when showing kanban');
  assert(!dom.filesArea.classList.contains('visible'), 'Files hidden when showing kanban');
}

console.log('\n=== Test: Dock State Machine ===');
{
  const dom = createDOM();
  loadModules();
  const DM = window.DockManager;

  assert(DM.getState() === 'UNDOCKED', 'Initial state is UNDOCKED');

  // Can't test full dock without more DOM simulation, but verify state query works
  dom.app.classList.add('chat-docked');
  assert(DM.getState() === 'DOCKED', 'DOCKED state detected');

  dom.app.classList.add('dock-expanded');
  assert(DM.getState() === 'DOCKED_EXPANDED', 'DOCKED_EXPANDED state detected');

  dom.app.classList.remove('chat-docked');
  dom.app.classList.remove('dock-expanded');
  assert(DM.getState() === 'UNDOCKED', 'Back to UNDOCKED');
}

console.log('\n=== Test: BUG FIX #1 — Undock stays on current panel ===');
{
  const dom = createDOM();
  loadModules();
  const PM = window.PanelManager;
  const DM = window.DockManager;

  // Simulate docked state viewing files above the dock
  PM.switchTo('files');
  assert(PM.getActivePanel() === 'files', 'Active panel is files before docking');

  // Manually enter docked state (simulating what _enterDocked does)
  dom.app.classList.add('chat-docked');
  dom.chatArea.style.display = 'flex';
  dom.chatArea.style.height = '280px';
  PM.setActivePanel('files');
  PM.setLastNonChatTab('files');

  // Verify we're in DOCKED state
  assert(DM.getState() === 'DOCKED', 'In DOCKED state');

  // Now undock via toggle
  DM.toggle();

  // BUG FIX: Should stay on files, NOT go to chat
  assert(PM.getActivePanel() === 'files', 'After undock, active panel stays on files (BUG #1 FIX)');
  assert(dom.filesArea.classList.contains('visible'), 'Files panel visible after undock');
  assert(DM.getState() === 'UNDOCKED', 'State is UNDOCKED after toggle');
}

console.log('\n=== Test: BUG FIX #1 — Undock from expanded goes to chat ===');
{
  const dom = createDOM();
  loadModules();
  const PM = window.PanelManager;
  const DM = window.DockManager;

  // Simulate DOCKED_EXPANDED state
  dom.app.classList.add('chat-docked');
  dom.app.classList.add('dock-expanded');
  dom.chatArea.style.display = 'flex';
  PM.setActivePanel('chat');

  assert(DM.getState() === 'DOCKED_EXPANDED', 'In DOCKED_EXPANDED state');

  // Undock from expanded
  DM.toggle();

  // From expanded, chat was fullscreen so undock should show chat
  assert(PM.getActivePanel() === 'chat', 'After undock from expanded, shows chat');
  assert(DM.getState() === 'UNDOCKED', 'State is UNDOCKED');
}

console.log('\n=== Test: BUG FIX #2 — Custom panels in docked mode ===');
{
  const dom = createDOM();
  loadModules();
  const PM = window.PanelManager;
  const DM = window.DockManager;

  PM.registerPanel('kanban', { elementId: 'panel-kanban' });

  // Switch to kanban while undocked
  PM.switchTo('kanban');
  assert(PM.getActivePanel() === 'kanban', 'Viewing kanban');
  assert(PM.getLastNonChatTab() === 'kanban', 'Last non-chat tab is kanban');

  // Now simulate entering docked state
  dom.app.classList.add('chat-docked');
  dom.chatArea.style.display = 'flex';
  dom.chatArea.style.height = '280px';
  PM.setActivePanel('kanban');

  // Undock — should stay on kanban, not break
  DM.toggle();

  assert(PM.getActivePanel() === 'kanban', 'After undock, still on kanban (BUG #2 FIX)');
  assert(dom.panelKanban.classList.contains('visible'), 'Kanban panel visible after undock');
  assert(!dom.filesArea.classList.contains('visible'), 'Files not visible');
  assert(DM.getState() === 'UNDOCKED', 'State is UNDOCKED');
}

console.log('\n=== Test: switchTo in DOCKED mode (non-chat) ===');
{
  const dom = createDOM();
  loadModules();
  const PM = window.PanelManager;
  const DM = window.DockManager;

  // Enter docked state
  dom.app.classList.add('chat-docked');
  dom.chatArea.style.display = 'flex';
  dom.chatArea.style.height = '280px';
  PM.setActivePanel('tasks');

  // Switch to files while docked
  PM.switchTo('files');
  assert(PM.getActivePanel() === 'files', 'Switched to files in docked mode');
  assert(dom.filesArea.classList.contains('visible'), 'Files visible above dock');
  assert(!dom.tasksArea.classList.contains('visible'), 'Tasks hidden');
}

console.log('\n=== Test: switchTo in DOCKED mode (chat = expand) ===');
{
  const dom = createDOM();
  loadModules();
  const PM = window.PanelManager;
  const DM = window.DockManager;

  // Enter docked state
  dom.app.classList.add('chat-docked');
  dom.chatArea.style.display = 'flex';
  dom.chatArea.style.height = '280px';
  PM.setActivePanel('files');

  // Click chat while docked => should expand
  PM.switchTo('chat');
  assert(dom.app.classList.contains('dock-expanded'), 'Clicking chat in DOCKED expands');
  assert(PM.getActivePanel() === 'chat', 'Active panel is chat after expand');
}

console.log('\n=== Test: switchTo in DOCKED_EXPANDED (non-chat = collapse) ===');
{
  const dom = createDOM();
  loadModules();
  const PM = window.PanelManager;
  const DM = window.DockManager;

  // Enter docked-expanded state
  dom.app.classList.add('chat-docked');
  dom.app.classList.add('dock-expanded');
  dom.chatArea.style.display = 'flex';
  PM.setActivePanel('chat');

  // Click files while expanded => should collapse to DOCKED showing files
  PM.switchTo('files');
  assert(!dom.app.classList.contains('dock-expanded'), 'dock-expanded removed');
  assert(dom.app.classList.contains('chat-docked'), 'Still docked');
  assert(PM.getActivePanel() === 'files', 'Active panel is files after collapse');
  assert(dom.filesArea.classList.contains('visible'), 'Files visible');
}

console.log('\n=== Test: Custom panel switchTo in docked mode ===');
{
  const dom = createDOM();
  loadModules();
  const PM = window.PanelManager;
  const DM = window.DockManager;

  PM.registerPanel('kanban', { elementId: 'panel-kanban' });

  // Enter docked state
  dom.app.classList.add('chat-docked');
  dom.chatArea.style.display = 'flex';
  dom.chatArea.style.height = '280px';
  PM.setActivePanel('files');

  // Switch to kanban while docked
  PM.switchTo('kanban');
  assert(PM.getActivePanel() === 'kanban', 'Kanban active in docked mode');
  assert(dom.panelKanban.classList.contains('visible'), 'Kanban panel visible above dock');
  assert(!dom.filesArea.classList.contains('visible'), 'Files hidden');
}

console.log('\n=== Test: localStorage dock persistence ===');
{
  const dom = createDOM();
  loadModules();
  const DM = window.DockManager;
  const PM = window.PanelManager;

  // Enter docked
  PM.setLastNonChatTab('teams');
  DM.toggle(); // UNDOCKED -> DOCKED
  assert(localStorage.getItem('portal_chat_docked') === '1', 'Dock state saved');

  // Exit docked
  DM.toggle(); // DOCKED -> UNDOCKED
  assert(localStorage.getItem('portal_chat_docked') === null, 'Dock state cleared');
}

// --- Summary ---
console.log(`\n${'='.repeat(50)}`);
console.log(`Results: ${passed} passed, ${failed} failed`);
if (failed > 0) { process.exit(1); }
else { console.log('All tests passed!'); }
