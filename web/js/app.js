// Shellwalk — application wiring.

import { api } from "./api.js";
import { Navigator } from "./navigator.js";
import { TerminalSurface } from "./terminal.js";
import { startTour, seenInThisBrowser } from "./tour.js";
import { askModel, commandForm, openSettings, pickFolder, promptText,
         savedCommands, scriptCodeUpload, scriptForm, tagButton } from "./modals.js";
import { clipboardImageAvailability, confirm, contextMenu, copyToClipboard, dismissSheet, el,
         readClipboardImage, readFromClipboard, selectTextSheet, sheet, sheetBody, sheetFoot, toast } from "./ui.js";

const dom = {
  topbar: document.getElementById("topbar"),
  topbarCommands: document.getElementById("topbar-commands"),
  topbarScripts: document.getElementById("topbar-scripts"),
  tabbar: document.getElementById("tabbar"),
  keybar: document.getElementById("keybar"),
  composer: document.getElementById("composer"),
  composerInput: document.getElementById("composer-input"),
  crumbs: document.getElementById("crumbs"),
  entries: document.getElementById("entries"),
  panel: document.getElementById("panel"),
  procName: document.getElementById("proc-name"),
  proc: document.getElementById("proc"),
  hiddenCount: document.getElementById("hidden-count"),
  toggleHidden: document.getElementById("toggle-hidden"),
  saveFavorite: document.getElementById("save-favorite"),
  collapsePanel: document.getElementById("collapse-panel"),
  terminalWrap: document.getElementById("terminal-wrap"),
  fileInput: document.getElementById("file-input"),
  segs: [...document.querySelectorAll(".seg")],
};

const state = {
  settings: {},
  scripts: [],
  commands: [],
  contextMenus: {},
  favorites: [],
  services: {},
  cwd: null,
  listing: null,
  view: "folders",
  showAll: false,
  idle: true,
};

let navigator = null;

// ----------------------------------------------------------------------- tabs
//
// Each tab is its own PTY, its own /ws/control connection and its own xterm
// surface. `terminal` and `control` below always point at the *active* tab's
// instances — activateTab() reassigns them — so every existing call site
// that reads `terminal.foo()` or writes through `control` keeps working
// unchanged; it always reaches whichever shell is on screen.
//
// A tab that isn't active keeps its control socket open in the background.
// That is what makes "run a command in tab 2, look at the file panel in tab 1,
// switch to tab 2 and see it already caught up" work without polling: the
// backend's own dirty-listing push (see server/main.py) is per connection,
// so a backgrounded tab's listing is already current by the time you look.

// The list of tabs lives on the server (/api/workspace), not in the
// browser: closing the browser leaves every shell running, and the next
// browser to open Shellwalk — phone or computer — gets the same tabs back.
// Closing a *tab* ends its shell for good. Only which tab was on screen is
// remembered per browser, so a reload lands where you were.
const ACTIVE_KEY = "shellwalk_active_session";

let tabs = [];
let activeTabId = null;
let terminal = null;
let control = null;

function activeTab() {
  return tabs.find((tab) => tab.id === activeTabId) || null;
}

function newTabId() {
  return window.crypto && window.crypto.randomUUID
    ? window.crypto.randomUUID()
    : `t_${Date.now()}_${Math.random().toString(36).slice(2)}`;
}

function persistTabs() {
  const tab = activeTab();
  try {
    if (tab && tab.sessionId) sessionStorage.setItem(ACTIVE_KEY, tab.sessionId);
  } catch { /* private mode */ }
}

async function loadWorkspaceTabs() {
  try {
    const data = await api.workspace();
    if (Array.isArray(data.tabs) && data.tabs.length) {
      return data.tabs.map((entry) => ({ sessionId: entry.session, title: entry.title }));
    }
  } catch (error) {
    toast(`Could not load the open shells: ${error.message}`, "error");
  }
  return [{ sessionId: null, title: null }];   // none open: start with one
}

/** The lowest number no open tab uses, so closing and opening never repeats one. */
function nextTabTitle() {
  const used = new Set(tabs.map((tab) => tab.title));
  let n = 1;
  while (used.has(String(n))) n += 1;
  return String(n);
}

function createTab({ id = null, sessionId = null, title = null, activate = false } = {}) {
  const tab = {
    id: id || newTabId(),
    sessionId,
    title: title || nextTabTitle(),
    host: el("div", { class: "term-host" }),
    terminal: null,
    control: null,
    closed: false,
    cwd: null,
    listing: null,
    idle: true,
    running: null,
    alive: true,
  };
  dom.terminalWrap.append(tab.host);
  tabs.push(tab);

  tab.terminal = new TerminalSurface(tab.host, {
    fontSize: deviceFontSize() || state.settings.font_size || 14,
    sessionId: tab.sessionId,
    onSession: (message) => {
      tab.sessionId = message.id;
      // The server's name wins (renamed on another device); a new shell
      // takes this tab's number so every device shows the same one.
      if (message.title) {
        if (message.title !== tab.title) { tab.title = message.title; renderTabBar(); }
      } else {
        api.sessionTitle(tab.sessionId, tab.title).catch(() => {});
      }
      persistTabs();
      openTabControl(tab);
      if (message.resumed) toast(`Reattached “${tab.title}” to its running shell.`, "success", 2200);
    },
    // The link was printed in this tab, so it pastes into this tab even if
    // another one is active by the time it is clicked.
    onClosed: () => dropTab(tab, "elsewhere"),
    onPasteLink: (command) => {
      if (tab.id !== activeTabId) activateTab(tab.id);
      inject(command, false);
    },
  });

  // start() mounts xterm and opens the PTY websocket — it does that once,
  // here, for every tab. A tab created later, from the + button, needs this
  // exactly as much as the ones restored at boot; skipping it is how a tab
  // ends up as a pill and an empty pane with no shell behind it.
  tab.terminal.inputFilter = applyStickyCtrl;
  tab.terminal.focusElsewhere = focusComposer;
  enableTouchScroll(tab.host);
  tab.ready = tab.terminal.start().catch((error) => {
    toast(`Could not start “${tab.title}”: ${error.message}`, "error");
    throw error;
  });

  renderTabBar();
  if (activate) tab.ready.then(() => activateTab(tab.id)).catch(() => {});
  return tab;
}

function openTabControl(tab) {
  if (tab.closed) return;
  const protocol = location.protocol === "https:" ? "wss" : "ws";
  const sessionParam = tab.sessionId ? `?session=${encodeURIComponent(tab.sessionId)}` : "";
  const socket = new WebSocket(`${protocol}://${location.host}/ws/control${sessionParam}`);
  tab.control = socket;
  if (tab.id === activeTabId) control = socket;

  socket.onopen = () => {
    if (tab.sessionId) socket.send(JSON.stringify({ type: "attach", session: tab.sessionId }));
    socket.send(JSON.stringify({ type: "show_all", value: state.showAll }));
  };

  socket.onmessage = (event) => {
    const message = JSON.parse(event.data);
    if (message.type === "listing") {
      tab.cwd = message.path;
      tab.listing = message;
      if (tab.id !== activeTabId) return;   // remembered, not rendered — see above
      state.cwd = message.path;
      state.listing = message;
      dom.hiddenCount.textContent = message.hidden_count ? `(${message.hidden_count})` : "";
      if (state.view === "folders") navigator.render(message);
      else navigator.renderCrumbs(message);
    } else if (message.type === "status") {
      tab.idle = message.idle;
      tab.running = message.running;
      tab.alive = message.alive;
      updateTabDot(tab);
      if (tab.id === activeTabId) setStatus(message);
    }
  };

  socket.onclose = () => {
    if (tab.closed || tab.control !== socket) return;   // deliberate, or superseded
    setTimeout(() => openTabControl(tab), 900);
  };
}

function activateTab(id) {
  const tab = tabs.find((entry) => entry.id === id);
  if (!tab || tab.id === activeTabId) return;
  activeTabId = tab.id;
  terminal = tab.terminal;
  setCtrl(false);
  control = tab.control;

  tabs.forEach((entry) => entry.host.classList.toggle("is-active", entry.id === id));
  requestAnimationFrame(() => {
    // Normally already resolved by the time a click can land here — see
    // createTab()'s tab.ready — but a tab clicked the instant it appears is
    // still starting, and fit()/focus() have nothing to act on yet. That
    // resolves on its own the moment start() finishes and activateTab runs
    // again as part of the ready chain.
    try { tab.terminal.fit.fit(); tab.terminal.focus(); } catch { /* still starting up */ }
  });

  if (tab.listing) {
    state.cwd = tab.cwd;
    state.listing = tab.listing;
    dom.hiddenCount.textContent = tab.listing.hidden_count ? `(${tab.listing.hidden_count})` : "";
    if (state.view === "folders") navigator.render(tab.listing);
    else navigator.renderCrumbs(tab.listing);
  }
  setStatus({ idle: tab.idle, running: tab.running, alive: tab.alive !== false });

  renderTabBar();
  persistTabs();
  // The cached listing above is instant; asking this tab's own connection to
  // confirm it catches anything that changed in this folder while a
  // different tab had the wheel.
  askControl({ type: "refresh" });
}

async function closeTab(id) {
  const tab = tabs.find((entry) => entry.id === id);
  if (!tab) return;
  if (tabs.length <= 1) {
    toast("Keep at least one shell open.", "warning");
    return;
  }
  if (tab.idle === false) {
    const ok = await confirm({
      title: "Close tab",
      message: `“${tab.title}” is running ${tab.running || "something"}. Closing ends it. Continue?`,
      confirmLabel: "Close it",
      danger: true,
    });
    if (!ok) return;
  }

  dropTab(tab, "here");
}

/**
 * Take a tab off the screen. Closed "here" also ends its shell on the
 * server; "elsewhere" means the shell already ended (its tab was closed on
 * another device), so only this browser's copy goes. Never leaves zero tabs.
 */
function dropTab(tab, where) {
  const index = tabs.indexOf(tab);
  if (index < 0) return;
  tabs.splice(index, 1);
  tab.closed = true;
  tab.terminal.destroy();
  try { tab.control && tab.control.close(1000); } catch { /* already gone */ }
  tab.host.remove();
  if (where === "here" && tab.sessionId) api.sessionClose(tab.sessionId).catch(() => {});
  if (where === "elsewhere") toast(`“${tab.title}” was closed.`, "info", 2200);

  if (!tabs.length) {
    activeTabId = null;
    createTab({ activate: true });
    return;
  }
  if (activeTabId === tab.id) {
    activeTabId = null;
    const next = tabs[Math.min(index, tabs.length - 1)];
    activateTab(next.id);
  } else {
    renderTabBar();
  }
}

function updateTabDot(tab) {
  if (!tab.dotEl) return;
  tab.dotEl.classList.toggle("is-idle", tab.alive !== false && tab.idle);
  tab.dotEl.classList.toggle("is-busy", tab.alive !== false && !tab.idle);
  tab.dotEl.classList.toggle("is-dead", tab.alive === false);
}

function renameTab(tab, name) {
  const trimmed = (name || "").trim();
  if (!trimmed) return;
  tab.title = trimmed.slice(0, 40);
  if (tab.sessionId) api.sessionTitle(tab.sessionId, tab.title).catch(() => {});
  renderTabBar();
}

function startRename(tab, titleEl) {
  const input = el("input", { class: "shelltab-title-input", value: tab.title });
  titleEl.replaceWith(input);
  input.focus();
  input.select();
  // Enter commits and re-renders the tab bar, which removes `input` from the
  // DOM — and removing a focused element fires its own blur. Without this
  // guard that blur calls commit() a second time, reentrantly, into a
  // replaceChildren() that the first call already has in flight.
  let settled = false;
  const commit = () => {
    if (settled) return;
    settled = true;
    renameTab(tab, input.value);
  };
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); commit(); }
    else if (event.key === "Escape") {
      event.preventDefault();
      settled = true;
      renderTabBar();
    }
  });
  input.addEventListener("blur", commit);
}

function renderTabBar() {
  const pills = tabs.map((tab) => {
    const dot = el("i", { class: "shelltab-dot" });
    tab.dotEl = dot;
    updateTabDot(tab);

    const titleEl = el("span", { class: "shelltab-title", text: tab.title });

    const pill = el("div", {
      class: `shelltab${tab.id === activeTabId ? " is-active" : ""}`,
      role: "tab",
      "aria-selected": String(tab.id === activeTabId),
      title: tab.title,
      onClick: (event) => {
        if (event.target.closest(".tab-close")) return;
        if (tab.id === activeTabId) startRename(tab, titleEl);
        else activateTab(tab.id);
      },
    }, [
      dot,
      titleEl,
      el("button", { class: "shelltab-close", title: "Close tab", text: "\u2715",
                     onClick: (event) => { event.stopPropagation(); closeTab(tab.id); } }),
    ]);

    pill.addEventListener("contextmenu", (event) => contextMenu(event, {
      heading: tab.title,
      items: [
        { name: "Switch to it", icon: "\u25b8", bold: true, run: () => activateTab(tab.id) },
        { name: "Rename\u2026", icon: "\u270e", run: () => startRename(tab, titleEl) },
        { name: "Close", icon: "\u2715", style: "danger", run: () => closeTab(tab.id) },
      ],
    }));

    return pill;
  });

  dom.tabbar.replaceChildren(
    ...pills,
    el("button", { class: "shelltab-add", title: "New shell",
                   onClick: () => createTab({ activate: true }) }, ["+"]),
    el("button", { class: "shelltab-add", title: "Running shells \u2014 reopen one no tab shows",
                   onClick: () => openSessionList() }, ["\u21ba"]),
    el("button", { class: "shelltab-add chrome-toggle fullscreen-toggle", title: "Full screen",
                   style: "margin-left:auto;",
                   onClick: () => toggleFullscreen() }, [document.fullscreenElement ? "\u2922" : "\u26f6"]),
    el("button", { class: "shelltab-add chrome-toggle", title: "Commands and folders",
                   onClick: () => toggleChrome() }, ["\u2630"]),
  );
}

// ------------------------------------------------------------ drag and drop
//
// Drop files or whole folders anywhere on the terminal or the folder panel to
// upload them into the shell's current folder; drop them on a folder in the
// panel to upload into that one instead. Folders keep their structure.

/** Every file under a dropped item, with its path relative to the drop. */
async function collectDropped(dataTransfer) {
  const found = [];
  const readEntries = (reader) => new Promise((resolve, reject) => reader.readEntries(resolve, reject));
  const fileOf = (entry) => new Promise((resolve, reject) => entry.file(resolve, reject));

  const walk = async (entry, prefix) => {
    if (entry.isFile) {
      found.push({ file: await fileOf(entry), relpath: prefix ? `${prefix}${entry.name}` : "" });
    } else if (entry.isDirectory) {
      const reader = entry.createReader();
      // readEntries() returns at most 100 entries per call: keep asking.
      for (let batch = await readEntries(reader); batch.length; batch = await readEntries(reader)) {
        for (const child of batch) await walk(child, `${prefix}${entry.name}/`);
      }
    }
  };

  const items = Array.from(dataTransfer.items || []).filter((item) => item.kind === "file");
  // Entries must be taken synchronously, before the first await.
  const entries = items.map((item) => (item.webkitGetAsEntry ? item.webkitGetAsEntry() : null));
  if (entries.length && entries.every(Boolean)) {
    for (const entry of entries) await walk(entry, "");
  } else {
    Array.from(dataTransfer.files || []).forEach((file) => found.push({ file, relpath: "" }));
  }
  return found;
}

function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(0)} KB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
}

let uploading = false;

async function uploadDropped(items, targetDir) {
  if (!items.length) return;
  if (uploading) { toast("An upload is already running.", "warning"); return; }
  uploading = true;
  const total = items.reduce((sum, item) => sum + item.file.size, 0);
  const name = el("div", { class: "upload-name" });
  const bar = el("div", { class: "upload-bar-fill" });
  const count = el("div", { class: "upload-count" });
  const box = el("div", { class: "upload-progress", role: "status" }, [
    el("div", { class: "upload-title", text: `Uploading to ${targetDir}` }),
    name, el("div", { class: "upload-bar" }, [bar]), count,
  ]);
  document.body.append(box);

  let done = 0;
  let failed = 0;
  let lastError = "";
  for (const [index, item] of items.entries()) {
    name.textContent = item.relpath || item.file.name;
    try {
      await api.uploadWithProgress(targetDir, item.file, item.relpath, (sent) => {
        const fraction = total ? (done + sent) / total : 1;
        bar.style.width = `${Math.min(100, fraction * 100).toFixed(1)}%`;
        count.textContent = `${index + 1} of ${items.length} \u00b7 ${formatBytes(done + sent)} of ${formatBytes(total)}`;
      });
    } catch (error) {
      failed += 1;
      lastError = error.message;
    }
    done += item.file.size;
  }
  box.remove();
  uploading = false;
  askControl({ type: "touched" });

  const saved = items.length - failed;
  if (!failed) toast(items.length === 1 ? `Uploaded ${items[0].file.name}.` : `Uploaded ${saved} files.`, "success");
  else toast(`${saved} uploaded, ${failed} failed: ${lastError}`, "error", 6000);
}

// ----------------------------------------------------------------- paste image
//
// "Paste image" in the folder and command-line menus: the image on the device
// clipboard (a screenshot, typically) is saved into the folder, through the
// same upload path as a drop. A name that already exists gets " (1)".

const IMAGE_EXTENSIONS = { "image/jpeg": "jpg", "image/svg+xml": "svg" };

function screenshotName(type) {
  const now = new Date();
  const two = (value) => String(value).padStart(2, "0");
  const stamp = `${now.getFullYear()}-${two(now.getMonth() + 1)}-${two(now.getDate())}`
              + `-${two(now.getHours())}${two(now.getMinutes())}${two(now.getSeconds())}`;
  const ext = IMAGE_EXTENSIONS[type] || (type.split("/")[1] || "png").replace(/[^a-z0-9]/gi, "") || "png";
  return `screenshot-${stamp}.${ext}`;
}

async function pasteImage(targetDir) {
  if (!targetDir) return;
  const blob = await readClipboardImage();
  if (blob === false) return;  // paste sheet cancelled
  if (!blob) { toast("There is no image on the clipboard.", "info"); return; }
  const file = new File([blob], screenshotName(blob.type || "image/png"), { type: blob.type || "image/png" });
  await uploadDropped([{ file, relpath: "" }], targetDir);
}

/**
 * contextMenu(), minus "Paste image" when the clipboard is known to hold no
 * image. Knowing takes an async look at the clipboard, so the browser's own
 * menu is suppressed first and ours opens a moment later.
 */
async function menuWithClipboard(event, options) {
  event.preventDefault();
  if (options.items.some((item) => item && item.builtin === "paste_image")
      && await clipboardImageAvailability() === "no") {
    options = { ...options, items: options.items.filter((item) => !item || item.builtin !== "paste_image") };
  }
  contextMenu(event, options);
}

function wireDragAndDrop() {
  const overlay = el("div", { class: "drop-overlay" }, [el("div", { class: "drop-label" })]);
  document.body.append(overlay);
  const label = overlay.firstChild;
  let depth = 0;
  let marked = null;

  const isFileDrag = (event) => Array.from(event.dataTransfer?.types || []).includes("Files");
  const PATH_TYPE = "application/x-shellwalk-path";
  const isPathDrag = (event) => Array.from(event.dataTransfer?.types || []).includes(PATH_TYPE);
  const overTerminal = (event) => event.target instanceof Node && dom.terminalWrap.contains(event.target);

  // An entry from the folder panel dropped on the terminal types its path at
  // the prompt (quoted, not run), as desktop terminals do. Anywhere else on
  // the page it does nothing; outside the browser it downloads (navigator.js).
  document.addEventListener("dragover", (event) => {
    if (!isPathDrag(event)) return;
    if (overTerminal(event)) {
      event.preventDefault();
      event.dataTransfer.dropEffect = "copy";
    } else {
      event.dataTransfer.dropEffect = "none";
    }
  });
  document.addEventListener("drop", (event) => {
    if (!isPathDrag(event)) return;
    event.preventDefault();
    if (!overTerminal(event) || !terminal) return;
    const path = event.dataTransfer.getData(PATH_TYPE);
    if (path) terminal.sendInput(`${shellQuote(path)} `);
    terminal.focus();
  });
  const folderUnder = (event) => {
    const node = event.target instanceof Element ? event.target.closest("[data-drop-dir]") : null;
    return node && dom.panel.contains(node) ? node : null;
  };
  const mark = (node) => {
    if (marked === node) return;
    if (marked) marked.classList.remove("is-drop-target");
    marked = node;
    if (marked) marked.classList.add("is-drop-target");
  };
  const hide = () => {
    depth = 0;
    overlay.classList.remove("is-visible");
    mark(null);
  };

  document.addEventListener("dragenter", (event) => {
    if (!isFileDrag(event)) return;
    depth += 1;
    overlay.classList.add("is-visible");
  });
  document.addEventListener("dragleave", (event) => {
    if (!isFileDrag(event)) return;
    depth -= 1;
    if (depth <= 0) hide();
  });
  document.addEventListener("dragover", (event) => {
    if (!isFileDrag(event)) return;
    event.preventDefault();               // without this the browser opens the file
    event.dataTransfer.dropEffect = "copy";
    const folder = folderUnder(event);
    mark(folder);
    label.textContent = `Drop to upload to ${folder ? folder.dataset.dropDir : (state.cwd || "the current folder")}`;
  });
  document.addEventListener("drop", async (event) => {
    if (!isFileDrag(event)) return;
    event.preventDefault();
    const folder = folderUnder(event);
    const target = folder ? folder.dataset.dropDir : state.cwd;
    hide();
    if (!target) { toast("The current folder is not known yet.", "warning"); return; }
    let items;
    try {
      items = await collectDropped(event.dataTransfer);
    } catch (error) {
      toast(`Could not read the dropped files: ${error.message}`, "error");
      return;
    }
    if (!items.length) { toast("Nothing to upload (empty folder?).", "info"); return; }
    await uploadDropped(items, target);
    if (terminal) terminal.focus();
  });
}

// ------------------------------------------------------------------ phones
//
// On a narrow screen only the tab bar and the folder path sit above the
// terminal (see the phone section of app.css). The commands bar and the folder
// panel open from the tab bar's \u2630 button and close again as soon as the
// keyboard comes up, because then every line of terminal counts.

const FONT_KEY = "shellwalk.fontSize";
const isTouch = window.matchMedia("(pointer: coarse)").matches;

function deviceFontSize() {
  try {
    const size = parseInt(localStorage.getItem(FONT_KEY), 10);
    return size >= 8 && size <= 32 ? size : null;
  } catch { return null; }
}

function changeFontSize(delta) {
  const current = deviceFontSize() || state.settings.font_size || 14;
  const size = Math.max(8, Math.min(32, current + delta));
  // Per device: a phone and a desktop want different sizes for the same shell.
  try { localStorage.setItem(FONT_KEY, String(size)); } catch { /* private mode */ }
  tabs.forEach((tab) => tab.terminal.setFontSize(size));
  toast(`Text size ${size}`, "info", 900);
}

function openTour() {
  startTour({
    touch: isTouch,
    compact: document.body.classList.contains("compact"),
    openChrome: (open) => toggleChrome(open),
    onClose: markTourSeen,
  });
}

/** Once per install: every browser and address after this one skips the tour. */
function markTourSeen() {
  if (state.settings.tour_seen) return;
  state.settings.tour_seen = true;
  api.saveSettings({ tour_seen: true }).catch(() => { /* shows again next visit */ });
}

function toggleChrome(open = !document.body.classList.contains("chrome-open")) {
  document.body.classList.toggle("chrome-open", open);
}

/** The browser's own full screen: hides its address bar and the system bars. */
async function toggleFullscreen() {
  try {
    if (document.fullscreenElement) await document.exitFullscreen();
    else await document.documentElement.requestFullscreen({ navigationUI: "hide" });
  } catch (error) {
    toast("This browser does not allow full screen here.", "warning");
  }
}

/**
 * Drag to scroll on touch screens.
 *
 * xterm.js scrolls with the mouse wheel but not reliably with a finger on
 * Android. A vertical drag on the terminal is turned into wheel events on
 * xterm's own element, so it does exactly what the wheel does: scroll back
 * through the output, or, inside vim, less or Claude Code's full-screen
 * views, whatever those programs do with the wheel. A flick keeps going and
 * slows down. A tap still focuses the terminal and opens the keyboard.
 */
function enableTouchScroll(host) {
  if (!isTouch) return;
  let lastY = null;
  let moved = 0;
  let velocity = 0;
  let lastTime = 0;
  let glide = null;

  // When a program grabs the mouse (Claude Code's full-screen view, vim,
  // htop), xterm.js turns each wheel event into at most ONE wheel report to
  // the program, however far the finger went. The scroll speed then
  // followed how often the phone fired touchmove and felt like dragging
  // through mud. In that mode the finger's distance is counted instead: one
  // report per TOUCH_ROWS_PER_NOTCH rows travelled, sent as line-mode wheel
  // events so xterm forwards each one.
  const TOUCH_ROWS_PER_NOTCH = 1;
  const MAX_NOTCHES_PER_MOVE = 12;
  let pending = 0;

  const rowHeight = () => {
    const row = host.querySelector(".xterm-rows > div");
    return (row && row.offsetHeight) || 17;
  };

  const fire = (target, init) => target.dispatchEvent(new WheelEvent("wheel", {
    ...init, bubbles: true, cancelable: true,
  }));

  const wheel = (deltaY) => {
    const target = host.querySelector(".xterm-screen") || host.querySelector(".xterm");
    if (!target || !deltaY) return;
    if (!host.querySelector(".xterm.enable-mouse-events")) {
      pending = 0;
      fire(target, { deltaY, deltaMode: WheelEvent.DOM_DELTA_PIXEL });
      return;
    }
    const step = rowHeight() * TOUCH_ROWS_PER_NOTCH;
    pending += deltaY;
    let notches = Math.trunc(pending / step);
    pending -= notches * step;
    notches = Math.max(-MAX_NOTCHES_PER_MOVE, Math.min(MAX_NOTCHES_PER_MOVE, notches));
    for (let i = 0; i < Math.abs(notches); i += 1) {
      fire(target, { deltaY: Math.sign(notches), deltaMode: WheelEvent.DOM_DELTA_LINE });
    }
  };

  host.addEventListener("touchstart", (event) => {
    if (event.touches.length !== 1) { lastY = null; return; }
    cancelAnimationFrame(glide);
    pending = 0;
    lastY = event.touches[0].clientY;
    lastTime = event.timeStamp;
    moved = 0;
    velocity = 0;
  }, { passive: true });

  host.addEventListener("touchmove", (event) => {
    if (lastY === null || event.touches.length !== 1) return;
    const y = event.touches[0].clientY;
    const delta = lastY - y;
    moved += Math.abs(delta);
    if (moved < 8) return;                 // still a tap
    event.preventDefault();
    const dt = Math.max(1, event.timeStamp - lastTime);
    velocity = delta / dt;                 // px per ms
    lastY = y;
    lastTime = event.timeStamp;
    wheel(delta);
  }, { passive: false });

  host.addEventListener("touchend", (event) => {
    if (lastY === null || moved < 8) { lastY = null; return; }
    lastY = null;
    // The finger stopped before it lifted: no flick, nothing to glide.
    if (event.timeStamp - lastTime > 120) return;
    let speed = velocity * 16;             // px per frame
    const step = () => {
      if (Math.abs(speed) < 0.6) return;
      wheel(speed);
      speed *= 0.94;
      glide = requestAnimationFrame(step);
    };
    glide = requestAnimationFrame(step);
  });
}

/** Track the visual viewport, so the keyboard shrinks the page instead of covering it. */
function followViewport() {
  const viewport = window.visualViewport;
  if (!viewport) return;
  let tallest = viewport.height;
  const update = () => {
    tallest = Math.max(tallest, viewport.height);
    document.documentElement.style.setProperty("--app-height", `${viewport.height}px`);
    const keyboard = viewport.height < tallest - 120;
    const was = document.body.classList.contains("kb-open");
    document.body.classList.toggle("kb-open", keyboard);
    if (keyboard && !was) toggleChrome(false);
    // Keyboard put away (Android's back key leaves the focus where it was):
    // let go of the compose box so it folds away with it.
    if (!keyboard && was && dom.composerInput && document.activeElement === dom.composerInput) {
      dom.composerInput.blur();
    }
    window.scrollTo(0, 0);
  };
  viewport.addEventListener("resize", update);
  window.addEventListener("orientationchange", () => { tallest = 0; setTimeout(update, 300); });
  update();
}

// Sticky modifiers: tap Ctrl or Alt on the key bar, then a key on the phone
// keyboard. Tapping both gives Ctrl+Alt. They release after one key.
const armed = { ctrl: false, alt: false };

function setModifier(name, on) {
  armed[name] = on;
  const button = dom.keybar && dom.keybar.querySelector(`[data-key=${name}]`);
  if (button) button.classList.toggle("is-armed", on);
}

function setCtrl(on) {
  setModifier("ctrl", on);
  setModifier("alt", on && armed.alt);
}

function applyStickyCtrl(data) {
  if ((!armed.ctrl && !armed.alt) || data.length !== 1) return data;
  let out = data;
  if (armed.ctrl) {
    const code = data.toUpperCase().charCodeAt(0);
    if (code >= 64 && code <= 95) out = String.fromCharCode(code - 64);   // @ A-Z [ \ ] ^ _
    else if (data === " ") out = "\x00";
    else if (data === "?") out = "\x7f";
  }
  if (armed.alt) out = `\x1b${out}`;     // Alt is Esc followed by the key
  setModifier("ctrl", false);
  setModifier("alt", false);
  return out;
}

/** Bytes from a key bar button, with any armed modifier applied. */
function withModifiers(data) {
  if (!armed.ctrl && !armed.alt) return data;
  if (data.length === 1) return applyStickyCtrl(data);
  // Arrows and the like: xterm's modifier encoding, e.g. Ctrl+Left = ESC[1;5D.
  const match = /^\x1b[\[O]([A-D])$/.exec(data);
  const mod = 1 + (armed.alt ? 2 : 0) + (armed.ctrl ? 4 : 0);
  setModifier("ctrl", false);
  setModifier("alt", false);
  return match ? `\x1b[1;${mod}${match[1]}` : data;
}

const KEYS = [
  { label: "Esc", data: "\x1b" },
  { label: "Tab", data: "\t" },
  { label: "\u21e7Tab", data: "\x1b[Z", title: "Shift+Tab" },
  { label: "Ctrl", modifier: "ctrl", title: "Tap, then a key: Ctrl+that key" },
  { label: "Alt", modifier: "alt", title: "Tap, then a key: Alt+that key" },
  { label: "^C", data: "\x03", plain: true, title: "Ctrl+C: stop the running command" },
  { gap: true },
  { label: "\u2191", arrow: "A", repeat: true },
  { label: "\u2193", arrow: "B", repeat: true },
  { label: "\u2190", arrow: "D", repeat: true },
  { label: "\u2192", arrow: "C", repeat: true },
  { gap: true },
  { label: "|", data: "|" },
  { label: "~", data: "~" },
  { label: "/", data: "/" },
  { label: "-", data: "-" },
  { label: "^D", data: "\x04", plain: true, title: "Ctrl+D: end of input" },
  { label: "^R", data: "\x12", plain: true, title: "Ctrl+R: search history" },
  { gap: true },
  { label: "A\u2212", font: -1, dim: true, title: "Smaller text on this device" },
  { label: "A+", font: 1, dim: true, title: "Bigger text on this device" },
];

function buildKeybar() {
  // The compact layout follows narrow screens and every touch device, so a
  // phone keeps it even with the browser's "Desktop site" switched on.
  const narrow = window.matchMedia("(max-width: 720px)");
  const applyCompact = () => document.body.classList.toggle("compact", isTouch || narrow.matches);
  narrow.addEventListener("change", applyCompact);
  applyCompact();

  if (!isTouch || !dom.keybar) return;
  document.body.classList.add("is-touch");

  const press = (key) => {
    if (key.font) { changeFontSize(key.font); return; }
    if (key.modifier) { setModifier(key.modifier, !armed[key.modifier]); return; }
    if (!terminal) return;
    const data = key.arrow ? terminal.arrow(key.arrow) : key.data;
    terminal.sendInput(key.plain ? data : withModifiers(data));
  };

  dom.keybar.replaceChildren(...KEYS.map((key) => {
    if (key.gap) return el("span", { class: "key-gap" });
    const button = el("button", {
      class: `key${key.dim ? " key--dim" : ""}`,
      text: key.label,
      title: key.title || key.label,
      "data-key": key.modifier || null,
    });
    let timer = null;
    let delay = null;
    const stop = () => {
      clearTimeout(delay); clearInterval(timer); delay = timer = null;
      button.classList.remove("is-down");
    };
    // pointerdown + preventDefault: the terminal keeps focus, so the phone
    // keyboard stays open while you tap these.
    button.addEventListener("pointerdown", (event) => {
      event.preventDefault();
      button.classList.add("is-down");
      press(key);
      if (key.repeat) delay = setTimeout(() => { timer = setInterval(() => press(key), 70); }, 380);
    });
    ["pointerup", "pointercancel", "pointerleave"].forEach((type) => button.addEventListener(type, stop));
    button.addEventListener("click", (event) => event.preventDefault());
    return button;
  }));
  applyComposeBox();
}

// ------------------------------------------------------------ compose box
//
// Phone keyboards rewrite the word they are composing (autocorrect, the
// automatic capital, dictation). xterm reads them through a hidden textarea
// and sometimes gets the rewrite wrong: whole stretches sent twice, or a
// letter lost. With the compose box the keyboard writes into a real text
// box instead, and only the finished line reaches the terminal, on Enter.
// The key bar still sends Esc, ^C, arrows and the rest at once. Touch
// screens only; Settings › General turns it off for vim or a password
// prompt, which want every key as it is typed.

let composerValue = "";

/** The Settings switch (on unless turned off). */
function composeBoxEnabled() {
  return state.settings.compose_box !== false;
}

function composeBoxActive() {
  return isTouch && composeBoxEnabled() && !!dom.composer;
}

/** Give the compose box the keyboard instead of xterm. True when it did. */
function focusComposer() {
  if (!composeBoxActive()) return false;
  dom.composerInput.focus({ preventScroll: true });
  return true;
}

function applyComposeBox() {
  document.body.classList.toggle("line-mode", composeBoxActive());
}

function fitComposer() {
  const box = dom.composerInput;
  box.style.height = "auto";
  // Grows with the text, up to about half of what the keyboard leaves, so a
  // long message stays readable while it is written.
  const room = (window.visualViewport ? window.visualViewport.height : window.innerHeight) * 0.45;
  box.style.height = `${Math.min(box.scrollHeight + 2, Math.max(96, room))}px`;
}

function submitComposer() {
  if (!terminal) return;
  const text = dom.composerInput.value;
  if (!terminal.submitLine(text)) {
    toast("Not connected. Your line stays in the box: send it again once the shell is back.", "warning", 2600);
    return;
  }
  dom.composerInput.value = composerValue = "";
  fitComposer();
}

function wireComposer() {
  if (!isTouch || !dom.composer) return;
  const box = dom.composerInput;

  dom.composer.addEventListener("submit", (event) => {
    event.preventDefault();
    submitComposer();
  });

  box.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      submitComposer();
    }
  });
  // Some phone keyboards send no usable keydown for Enter while composing;
  // the line break still shows up here.
  box.addEventListener("beforeinput", (event) => {
    if (event.inputType === "insertLineBreak" || event.inputType === "insertParagraph") {
      event.preventDefault();
      submitComposer();
    }
  });

  // Folded away until it has the keyboard: tapping the terminal hands it
  // the focus (focusComposer), and it opens between the key bar and the
  // keyboard. At rest the screen looks exactly as without it.
  box.addEventListener("focus", () => document.body.classList.add("composing"));
  box.addEventListener("blur", () => document.body.classList.remove("composing"));

  box.addEventListener("input", () => {
    // Ctrl or Alt armed on the key bar: the one character just typed is a
    // key for the terminal (Ctrl+R, Alt+B...), not text for the box.
    const value = box.value;
    if ((armed.ctrl || armed.alt) && terminal && value.length === composerValue.length + 1) {
      let at = 0;
      while (at < composerValue.length && value[at] === composerValue[at]) at += 1;
      box.value = composerValue;
      terminal.sendInput(applyStickyCtrl(value[at]));
    } else {
      composerValue = value;
    }
    fitComposer();
  });
}

// ----------------------------------------------------------- running shells

function describeAge(seconds) {
  if (seconds < 90) return "just now";
  if (seconds < 5400) return `${Math.round(seconds / 60)} min ago`;
  if (seconds < 172800) return `${Math.round(seconds / 3600)} h ago`;
  return `${Math.round(seconds / 86400)} days ago`;
}

/**
 * Every shell still alive on the server, so one whose tab was lost (browser
 * killed, tab closed by accident, Shellwalk restarted with tmux on) can be
 * opened again.
 */
async function openSessionList() {
  let data;
  try {
    data = await api.sessions();
  } catch (error) {
    toast(error.message, "error");
    return;
  }
  const shown = new Set(tabs.map((tab) => tab.sessionId));
  const now = Date.now() / 1000;

  const open = async (shell) => {
    const existing = tabs.find((tab) => tab.sessionId === shell.id);
    if (existing) { dismissSheet(); activateTab(existing.id); return; }
    try {
      const result = await api.sessionAdopt(shell.id);
      dismissSheet();
      createTab({ sessionId: result.session, activate: true });
    } catch (error) {
      toast(error.message, "error");
    }
  };

  const rows = data.sessions.map((shell) => {
    const here = shown.has(shell.id);
    const where = shell.cwd || "unknown folder";
    const what = shell.running ? `running ${shell.running}` : "at the prompt";
    const tags = [shell.tmux ? "tmux" : null, here ? "in a tab" : (shell.attached ? "open elsewhere" : null)]
      .filter(Boolean).join(" \u00b7 ");
    return el("div", { class: "row" }, [
      el("div", { class: "row-main" }, [
        el("div", { class: "row-name", text: where }),
        el("div", { class: "row-desc", text: `${what} \u00b7 started ${describeAge(now - shell.created)}` }),
      ]),
      el("div", { class: "row-tools" }, [
        tagButton(here ? "Show" : "Open", { onClick: () => open(shell) }),
        here ? null : tagButton("End", {
          style: "danger",
          onClick: async () => {
            const ok = await confirm({
              title: "End shell",
              message: `End the shell in ${where}${shell.running ? `, and ${shell.running} with it` : ""}?`,
              confirmLabel: "End it",
              danger: true,
            });
            if (!ok) return;
            try {
              await api.sessionAdopt(shell.id).catch(() => null);
              await api.sessionClose(shell.id);
              toast("Shell ended.", "success");
              openSessionList();
            } catch (error) { toast(error.message, "error"); }
          },
        }),
      ]),
      tags ? el("span", { class: "row-tag", text: tags }) : null,
    ]);
  });

  sheet({
    title: "Running shells",
    body: sheetBody([
      el("div", { class: "rows" }, rows.length ? rows
        : [el("div", { class: "empty" }, [el("b", { text: "No shells are running." })])]),
      el("div", { class: "field-note", text: data.tmux_available
        ? "Closing a tab ends its shell. Closing the browser keeps them all: the next browser, on any device, opens the same tabs. To keep them through a Shellwalk restart or update too, turn on Settings \u203a General \u203a Keep shells in tmux."
        : "Closing a tab ends its shell. Closing the browser keeps them all: the next browser, on any device, opens the same tabs. They do not survive a Shellwalk restart unless you install tmux (sudo apt install tmux) and turn on Settings \u203a General \u203a Keep shells in tmux." }),
    ]),
    footer: sheetFoot([
      el("button", { class: "btn btn--primary", text: "Close", onClick: () => dismissSheet() }),
    ]),
  });
}

// ------------------------------------------------------------------ injection

/** The single funnel for every click that ends up as typed text. */
async function inject(text, execute = false, options = {}) {
  if (!terminal || !terminal.sessionId) {
    toast("No shell session yet. Give it a moment.", "warning");
    return false;
  }
  try {
    const result = await api.inject({
      session: terminal.sessionId,
      text,
      execute,
      ...options,
    });
    if (!result.ok) {
      toast(result.reason, "warning");
      return false;
    }
    terminal.focus();
    return true;
  } catch (error) {
    toast(error.message, "error");
    return false;
  }
}

/** Navigation always runs: the folder panel has to follow the shell. */
const goTo = (path) => inject(`cd ${shellQuote(path)}`, true);

function shellQuote(value) {
  return /^[A-Za-z0-9_@%+=:,./-]+$/.test(value)
    ? value
    : `'${value.replace(/'/g, `'\\''`)}'`;
}

// -------------------------------------------------------------- control socket
//
// openTabControl(), above in the tabs section, is the per-tab version of what
// used to be the single openControl(). setStatus() and askControl() stay
// here: both operate on "whichever tab is active" through the module-level
// `control` binding, which activateTab() keeps pointed at the right socket.

function setStatus({ idle, running, alive }) {
  state.idle = idle;
  dom.proc.classList.toggle("is-idle", alive && idle);
  dom.proc.classList.toggle("is-busy", alive && !idle);
  dom.proc.classList.toggle("is-dead", !alive);
  dom.procName.textContent = !alive ? "shell exited" : (idle ? "ready" : (running || "running"));
}

const askControl = (message) => {
  if (control && control.readyState === WebSocket.OPEN) control.send(JSON.stringify(message));
};

// -------------------------------------------------------------------- top bar

function renderTopbar() {
  dom.topbarCommands.replaceChildren(
    ...state.commands
      .filter((command) => command.topbar !== false && command.enabled !== false)
      .map((command) => {
        const run = async () => {
          if (command.confirm) {
            const ok = await confirm({
              title: command.name,
              message: fillTemplate(command.confirm_message || `Run \u201c${command.name}\u201d?`,
                                     { cwd: state.cwd }),
              confirmLabel: command.name,
              danger: command.style === "danger",
            });
            if (!ok) return;
          }
          inject(command.command, command.execute);
        };

        const chip = el("button", {
          class: "chip",
          "data-style": command.style && command.style !== "default" ? command.style : null,
          title: `${command.description}\n\n${command.command}`,
          onClick: run,
        }, [
          el("span", { class: "icon", text: "\u2731" }),
          el("span", { text: command.name }),
        ]);
        // A chip is the thing you actually look at, so it is also where you
        // reach for when you want to change it. Every command works the same
        // way here \u2014 the ones that shipped with the project are not a
        // different kind of thing from one you just made.
        chip.addEventListener("contextmenu", (event) => contextMenu(event, {
          heading: command.name,
          items: [
            { name: command.execute ? "Run it" : "Type it out", icon: "\u25b8",
              bold: true, description: command.command, run },
            { name: "Edit\u2026", icon: "\u270e",
              description: "Name, command, where it appears.",
              run: () => commandForm(appContext, command) },
            { name: "Manage saved commands\u2026", icon: "\u2731",
              description: "Settings \u203a Commands.",
              run: () => savedCommands(appContext) },
            { name: "Keep it out of the top bar", icon: "\u2193",
              description: "It stays in the saved commands list.",
              run: async () => {
                try {
                  await api.commandPatch(command.id, { topbar: false });
                  await appContext.reload();
                  toast(`\u201c${command.name}\u201d is in the list only now.`, "success");
                } catch (error) { toast(error.message, "error"); }
              } },
            { name: "Delete", icon: "\u2715", style: "danger",
              description: `Remove commands/${command.id}.json from your data folder.`,
              run: async () => {
                const ok = await confirm({
                  title: "Delete command",
                  message: `Delete \u201c${command.name}\u201d? The file commands/${command.id}.json in your data folder is removed.`,
                  confirmLabel: "Delete",
                  danger: true,
                });
                if (!ok) return;
                try {
                  await api.commandDelete(command.id);
                  await appContext.reload();
                  toast("Deleted.", "success");
                } catch (error) { toast(error.message, "error"); }
              } },
          ],
        }));
        return chip;
      }),
    // Shortcuts are made where they show up: right after the last one.
    el("button", { class: "chip chip--add", title: "New command shortcut",
                   "aria-label": "New command",
                   onClick: () => commandForm(appContext, null) }, [
      el("span", { class: "icon", text: "+" }),
    ]),
  );

  dom.topbarScripts.replaceChildren(
    ...state.scripts
      .filter((script) => script.enabled !== false)
      .map((script) => {
        const chip = el("button", {
          class: "chip",
          "data-style": script.style && script.style !== "default" ? script.style : null,
          "data-script-id": script.id,
          title: script.description,
          onClick: () => runComponent("scripts", script, {}),
        }, [
          el("span", { class: "icon", text: script.icon || "\u25a2" }),
          el("span", { text: script.name }),
        ]);
        // Same treatment as the commands on the left: a script is a script,
        // whether it is one of the ones that came with the project or one
        // you just uploaded. There is no second, protected category here.
        chip.addEventListener("contextmenu", (event) => contextMenu(event, {
          heading: script.name,
          items: [
            { name: "Run it", icon: "\u25b8", bold: true, description: script.description,
              run: () => runComponent("scripts", script, {}) },
            { name: "Edit\u2026", icon: "\u270e",
              description: "Name, icon, colour, whether it asks first.",
              run: () => scriptForm(appContext, script) },
            { name: "Replace its code\u2026", icon: "\u21bb",
              description: "Upload a new routes.py or zip for this script.",
              run: () => scriptCodeUpload(appContext, script) },
            { name: "Delete", icon: "\u2715", style: "danger",
              description: "Remove this script from the top bar.",
              run: async () => {
                const ok = await confirm({
                  title: "Delete script",
                  message: `Delete \u201c${script.name}\u201d? ${script.description || ""}`.trim(),
                  confirmLabel: "Delete",
                  danger: true,
                });
                if (!ok) return;
                try {
                  await api.scriptDelete(script.id);
                  await appContext.reload();
                  toast("Deleted.", "success");
                } catch (error) { toast(error.message, "error"); }
              } },
          ],
        }));
        return chip;
      }),
    el("button", { class: "chip", id: "guide-chip", title: "A short tour of what Shellwalk can do",
                   onClick: () => openTour() }, [
      el("span", { class: "icon", text: "?" }),
      el("span", { text: "Guide" }),
    ]),
    el("button", { class: "chip", title: "Add a script (a routes.py, or a zip of one)",
                   onClick: () => scriptForm(appContext, null) }, [
      el("span", { class: "icon", text: "+" }),
      el("span", { text: "Script" }),
    ]),
  );
}

// ---------------------------------------------------------- component running

function fillTemplate(text, ctx) {
  return (text || "").replace(/\{(\w+)\}/g, (match, key) =>
    (ctx[key] !== undefined && ctx[key] !== null ? String(ctx[key]) : match));
}

/** Execute a discovered component, applying whatever directives come back. */
async function runComponent(surface, component, ctx) {
  const context = { cwd: state.cwd, ...ctx };

  if (component.builtin) {
    return runBuiltin(component.builtin, context);
  }

  if (component.confirm) {
    const ok = await confirm({
      title: component.name,
      message: fillTemplate(component.confirm_message || `Run ${component.name}?`, context),
      confirmLabel: component.name,
      danger: component.style === "danger",
    });
    if (!ok) return;
  }

  // Actions that need a name ask for it here. The manifest says which
  // placeholder it fills, so a "New folder" entry is still just a folder with
  // a manifest.json and a command.txt — no code registered anywhere.
  if (component.prompt && component.prompt.field
      && !context[component.prompt.field]) {
    const answer = await promptText({
      title: component.prompt.title || component.name,
      label: component.prompt.label || "Name",
      value: fillTemplate(component.prompt.default || "", context),
      note: component.prompt.note || "",
      confirmLabel: component.prompt.confirm_label || component.name,
      selectStem: Boolean(component.prompt.select_stem),
    });
    if (answer === null) return;
    context[component.prompt.field] = answer;
  }

  // Actions that need a destination collect it before the backend is asked.
  if (component.needs_picker && !context[component.needs_picker]) {
    const chosen = await pickFolder({
      start: context.cwd || "/",
      title: component.picker_title || component.name,
      confirmLabel: component.name,
    });
    if (!chosen) return;
    context[component.needs_picker] = chosen;
  }

  let result;
  try {
    result = await api.invoke(surface, component.id, context);
  } catch (error) {
    toast(error.message, "error");
    return;
  }

  if (result.needs && result.needs.length) {
    toast(`${component.name} still needs: ${result.needs.join(", ")}.`, "warning");
    return;
  }
  if (result.builtin) return runBuiltin(result.builtin, context);
  if (result.open) window.open(result.open, "_blank", "noopener");
  if (result.toast) toast(result.toast, result.level || "info");
  if (result.inject) await inject(result.inject, Boolean(result.execute));
}

async function runBuiltin(name, ctx) {
  switch (name) {
    case "clear":
      terminal.clear();
      break;
    case "download":
      if (ctx.file_path) window.open(api.downloadUrl(ctx.file_path), "_blank", "noopener");
      break;
    case "edit_as_text":
      if (ctx.file_path) {
        window.open(`/static/edit.html?path=${encodeURIComponent(ctx.file_path)}`, "_blank", "noopener");
      }
      break;
    case "upload":
      dom.fileInput.value = "";
      dom.fileInput.click();
      break;
    case "paste": {
      // Straight into the PTY, not through inject(): a paste must work
      // inside any running program (Claude Code, vim, a REPL), not only at
      // the shell prompt.
      const text = await readFromClipboard();
      if (text) terminal.paste(text);
      break;
    }
    case "select_text":
      selectTextSheet(terminal.recentText());
      break;
    case "paste_image":
      await pasteImage(ctx.cwd || state.cwd);
      break;
    case "copy_path":
      await copyText(ctx.file_path || ctx.cwd || "");
      break;
    case "copy_selection":
      await copyText(terminal.selection());
      break;
    case "save_favorite":
      await saveFavorite(ctx.cwd || state.cwd);
      break;
    case "saved_commands":
      savedCommands(appContext);
      break;
    case "ask_llm":
      askModel(appContext, { logText: ctx.log || "" });
      break;
    case "settings":
      openSettings(appContext);
      break;
    case "logout": {
      let passwordSet = true;
      try {
        passwordSet = (await api.accessStatus()).password_set;
      } catch { /* if this check itself fails, warn for the stricter case */ }

      const ok = await confirm({
        title: "Sign out",
        message: passwordSet
          ? "Signs you out. The next visit — from this device or the LAN — " +
            "will need the password."
          : "This machine currently needs no password for its own local " +
            "access. Signing out means the next visit, even from this " +
            "device, will need either a LAN password (set one first, in " +
            "Settings \u203a Access) or the recovery link the server prints " +
            "to its own console on every start.",
        confirmLabel: "Sign out",
        danger: !passwordSet,
      });
      if (!ok) break;

      await api.logout();
      location.href = "/auth/login";
      break;
    }
    default:
      toast(`No handler for builtin “${name}”.`, "warning");
  }
}

// Clipboard access is handled in ui.js, which falls back to a selectable
// sheet when the browser refuses an http origin. See copyToClipboard().
const copyText = (text) => copyToClipboard(text);

// ----------------------------------------------------------------- file clicks

// Every file click goes through a handler (Settings › File types). The left
// click runs the first handler with "click" on; the right-click menu lists all
// of them, so whatever a left click does is always also a menu item.

function handlersFor(entry) {
  if (!entry || entry.dir) return [];
  return (state.settings.handlers || []).filter((h) => (h.extensions || []).includes(entry.ext));
}

function defaultHandler(entry) {
  return handlersFor(entry).find((h) => h.click !== false) || null;
}

function handlerValues(entry) {
  return {
    file_path: shellQuote(entry.path),
    file_name: shellQuote(entry.name),
    file_dir: shellQuote(entry.path.replace(/\/[^/]*$/, "") || "/"),
    cwd: shellQuote(state.cwd || "."),
  };
}

async function runHandler(handler, entry) {
  switch (handler.kind) {
    case "browser":
      window.open(api.fileUrl(entry.path), "_blank", "noopener");
      return;
    case "editor":
      window.open(`/static/edit.html?path=${encodeURIComponent(entry.path)}`, "_blank", "noopener");
      return;
    case "command":
      await inject(fillTemplate(handler.command, handlerValues(entry)), Boolean(handler.execute));
      return;
    default:
      toast(`Unknown handler kind \u201c${handler.kind}\u201d.`, "warning");
  }
}

function openInEditor(path) {
  window.open(`/static/edit.html?path=${encodeURIComponent(path)}`, "_blank", "noopener");
}

async function openEntry(entry) {
  if (!entry) return;
  if (entry.dir) { goTo(entry.path); return; }
  const handler = defaultHandler(entry);
  if (!handler) {
    // No handler takes the left click for this extension: the text editor.
    // It opens binary and very large files read-only or not at all (edit.html).
    openInEditor(entry.path);
    return;
  }
  await runHandler(handler, entry);
}

/** The top of an entry's right-click menu: the left-click action first, in bold. */
function entryOpenItems(entry) {
  if (entry.dir) {
    return [{ name: "Open", icon: "\u25b8", bold: true,
              description: "Go into this folder (same as a left click).",
              run: () => goTo(entry.path) }];
  }
  const primary = defaultHandler(entry);
  const all = handlersFor(entry);
  const ordered = primary ? [primary, ...all.filter((h) => h !== primary)] : all;
  return ordered.map((handler) => ({
    name: handler.name,
    icon: handler.icon,
    bold: handler === primary,
    description: handler === primary ? "Same as a left click." : "",
    hint: handler.kind === "command" ? (handler.execute ? "runs" : "cmd") : null,
    run: () => runHandler(handler, entry),
  }));
}

// -------------------------------------------------------------- context menus

function menuItems(surface, ctx, entry = null, scope = null) {
  const components = state.contextMenus[surface] || [];
  return components
    // Hidden in Settings › Context menus. The item still exists on disk and in
    // state; it just does not appear in the menu.
    .filter((component) => component.enabled !== false)
    .filter((component) => matches(component, entry, scope))
    .map((component) => ({
      name: component.name,
      icon: component.icon,
      description: component.description,
      style: component.style,
      bold: component.bold,
      hint: component.command && !component.bold ? "cmd" : null,
      builtin: component.builtin || null,
      run: () => runComponent(surface, component, ctx),
    }));
}

function matches(component, entry, scope) {
  // The folder panel is one menu with two halves: what applies to the entry
  // you hit, and what applies to the folder you are in. A component says which
  // half it belongs to; everything else keeps working untouched, because the
  // default is "entry".
  if (scope) {
    const declared = component.scope || "entry";
    if (declared !== "any" && declared !== scope) return false;
  }
  const rule = component.match || {};
  if (rule.kinds && rule.kinds.length) {
    const kind = entry ? (entry.dir ? "dir" : "file") : null;
    if (!kind || !rule.kinds.includes(kind)) return false;
  }
  if (rule.extensions && rule.extensions.length) {
    if (!entry || entry.dir) return false;
    if (!rule.extensions.includes(entry.ext)) return false;
  }
  return true;
}

/** Right-click anywhere in the panel that is not an entry. */
function folderMenu(event) {
  menuWithClipboard(event, {
    heading: state.cwd || "",
    items: menuItems("file_entry", { cwd: state.cwd }, null, "folder"),
  });
}

function wireContextMenus() {
  // Folder panel: one surface, two halves. Hit an entry and you get the
  // actions for that entry; hit anything else and you get the actions for the
  // folder. The old empty_area surface is gone — it was only reachable when
  // the grid happened to have a gap in it, which in a full folder it never does.
  navigator.onContext = (event, entry) => {
    if (!entry) { folderMenu(event); return; }
    const openItems = entryOpenItems(entry);
    // A handler that edits as text makes the generic "Edit as text" item a
    // duplicate of a line already above it.
    const hasEditor = handlersFor(entry).some((h) => h.kind === "editor");
    const generic = menuItems("file_entry", {
        file_name: entry.name,
        file_path: entry.path,
        // Rename writes back into the same folder, so it needs the parent as
        // its own value: the shell's cwd is not necessarily the file's home.
        file_dir: entry.path.replace(/\/[^/]*$/, "") || "/",
        cwd: state.cwd,
        entry,
      }, entry, "entry").filter((item) => !(hasEditor && item.builtin === "edit_as_text"));
    contextMenu(event, {
      heading: entry.name,
      items: [...openItems, openItems.length ? { separator: true } : null, ...generic],
    });
  };

  // The panel bar too. When every pixel of the grid is an entry — the usual
  // case in a real folder — this is the reliable way to reach the folder half.
  const panelBar = dom.panel.querySelector(".panel-bar");
  if (panelBar) panelBar.addEventListener("contextmenu", folderMenu);

  // Top bar: right-click its empty space (not a chip, which has its own menu)
  // for the ways to add or manage what sits in it.
  dom.topbar.addEventListener("contextmenu", (event) => {
    if (event.target.closest(".chip, .brand")) return;
    contextMenu(event, {
      heading: "Top bar",
      items: [
        { name: "New command\u2026", icon: "+", bold: true,
          description: "A shortcut chip on the left that types or runs a command.",
          run: () => commandForm(appContext, null) },
        { name: "Manage saved commands\u2026", icon: "\u2731",
          description: "Settings \u203a Commands.",
          run: () => savedCommands(appContext) },
        { name: "New script\u2026", icon: "\u25a2",
          description: "A chip on the right backed by a routes.py.",
          run: () => scriptForm(appContext, null) },
      ],
    });
  });

  // Terminal: log menu when text is selected, command-line menu otherwise.
  dom.terminalWrap.addEventListener("contextmenu", (event) => {
    const selection = terminal.selection();
    if (selection && selection.trim()) {
      contextMenu(event, {
        heading: "Selected output",
        items: menuItems("log", { cwd: state.cwd, log: selection, selection }),
      });
    } else {
      menuWithClipboard(event, {
        heading: "Command line",
        items: menuItems("command_line", { cwd: state.cwd, log: terminal.visibleText() }),
      });
    }
  });
}

// ------------------------------------------------------------------ favourites

async function saveFavorite(path) {
  if (!path) return;
  try {
    const result = await api.addFavorite(path);
    state.favorites = result.favorites;
    toast(result.added ? `Saved ${path}.` : "Already in favourites.",
          result.added ? "success" : "info");
    if (state.view === "favorites") renderFavorites();
  } catch (error) {
    toast(error.message, "error");
  }
}

async function refreshFavorites() {
  try {
    const result = await api.favorites();
    state.favorites = result.favorites;
    if (result.removed && result.removed.length) {
      toast(`Removed ${result.removed.length} favourite${result.removed.length > 1 ? "s" : ""} whose folder is gone.`,
            "warning");
    }
  } catch (error) {
    toast(error.message, "error");
  }
}

function renderFavorites() {
  if (!state.favorites.length) {
    dom.entries.replaceChildren(el("div", { class: "empty" }, [
      el("b", { text: "No favourites yet." }),
      el("span", { text: " Right-click the folder panel, or use the star, to save where you are." }),
    ]));
    return;
  }
  dom.entries.replaceChildren(...state.favorites.map((favorite) => {
    const node = el("button", { class: "entry is-dir", title: favorite.path,
                                onClick: () => goTo(favorite.path) }, [
      el("span", { class: "mark", text: "\u2605" }),
      el("span", { class: "label", text: favorite.label }),
      el("span", { class: "meta", text: favorite.path.replace(/^.*\/(?=[^/]*\/[^/]*$)/, "\u2026/") }),
    ]);
    node.addEventListener("contextmenu", (event) => {
      contextMenu(event, {
        heading: favorite.label,
        items: [
          { name: "Go here", icon: "\u203a", bold: true, run: () => goTo(favorite.path) },
          { name: "Forget", icon: "\u2715", style: "danger", run: async () => {
              const result = await api.removeFavorite(favorite.path);
              state.favorites = result.favorites;
              renderFavorites();
            } },
        ],
      });
    });
    return node;
  }));
}

// ---------------------------------------------------------------------- chrome

function wireChrome() {
  dom.segs.forEach((seg) => seg.addEventListener("click", async () => {
    dom.segs.forEach((other) => other.classList.toggle("is-on", other === seg));
    state.view = seg.dataset.view;
    if (state.view === "favorites") { await refreshFavorites(); renderFavorites(); }
    else if (state.listing) navigator.render(state.listing);
  }));

  dom.toggleHidden.addEventListener("click", () => {
    state.showAll = !state.showAll;
    dom.toggleHidden.setAttribute("aria-pressed", String(state.showAll));
    askControl({ type: "show_all", value: state.showAll });
  });

  dom.saveFavorite.addEventListener("click", () => saveFavorite(state.cwd));
  dom.collapsePanel.addEventListener("click", () => {
    dom.panel.classList.toggle("is-collapsed");
    setTimeout(() => terminal.focus(), 0);
  });

  dom.fileInput.addEventListener("change", async () => {
    const file = dom.fileInput.files[0];
    if (!file || !state.cwd) return;
    toast(`Uploading ${file.name}\u2026`, "info", 2000);
    try {
      const result = await api.upload(state.cwd, file);
      toast(`Saved as ${result.name}.`, "success");
      askControl({ type: "refresh" });
    } catch (error) {
      toast(error.message, "error");
    }
  });

  // "Show all types" resets when you leave the folder (doc section 3.2).
  let lastPath = null;
  setInterval(() => {
    if (state.cwd !== lastPath) {
      lastPath = state.cwd;
      if (state.showAll) {
        state.showAll = false;
        dom.toggleHidden.setAttribute("aria-pressed", "false");
        askControl({ type: "show_all", value: false });
      }
    }
  }, 400);
}

// ------------------------------------------------------------------- bootstrap

const appContext = {
  state,
  inject,
  get terminal() { return terminal; },
  setComposeBox: (on) => {
    state.settings.compose_box = on;
    applyComposeBox();
  },
  setFontSize: (size) => {
    // Settings sets the size for every device; drop this device's own choice.
    try { localStorage.removeItem(FONT_KEY); } catch { /* private mode */ }
    tabs.forEach((tab) => tab.terminal.setFontSize(size));
  },
  // Re-draw the folder panel from the listing it already has (new icons).
  refreshPanel: () => {
    if (state.view === "folders" && state.listing) navigator.render(state.listing);
  },
  reload: async () => {
    const fresh = await api.reload();
    state.scripts = fresh.scripts;
    state.commands = fresh.commands;
    state.contextMenus = fresh.context_menus;
    state.settings = await api.settings();
    renderTopbar();
  },
};

async function main() {
  let boot;
  try {
    boot = await api.bootstrap();
  } catch (error) {
    document.body.replaceChildren(el("div", { class: "empty",
      style: "padding:40px;" }, [
      el("b", { text: "Cannot reach the server." }),
      el("span", { text: ` ${error.message}` }),
    ]));
    return;
  }

  if (boot.version) {
    // Visible proof of which build is answering, without spending screen space.
    document.getElementById("proc").title =
      `Shellwalk ${boot.version} \u2014 what currently owns the terminal`;
    document.getElementById("brand").title = `Shellwalk ${boot.version} on GitHub`;
  }

  Object.assign(state, {
    settings: boot.settings,
    scripts: boot.scripts,
    commands: boot.commands,
    contextMenus: boot.context_menus,
    favorites: boot.favorites,
    services: boot.services,
    defaultHandlers: boot.default_handlers || [],
    defaultFileIcons: boot.default_file_icons || null,
  });

  navigator = new Navigator({
    crumbs: dom.crumbs,
    grid: dom.entries,
    emptyMessage: "This folder is empty.",
    onNavigate: goTo,
    onOpenFile: openEntry,
    icons: () => state.settings.file_icons,
    downloadUrl: (entry) => new URL(api.downloadUrl(entry.path), window.location.href).href,
  });

  renderTopbar();
  wireChrome();
  wireContextMenus();
  buildKeybar();
  wireComposer();
  followViewport();
  wireDragAndDrop();
  if (!document.fullscreenEnabled) document.body.classList.add("no-fullscreen");
  document.addEventListener("fullscreenchange", () => renderTabBar());

  const stored = await loadWorkspaceTabs();
  const restored = stored.map((entry) =>
    createTab({ sessionId: entry.sessionId, title: entry.title }));
  // Every tab's start() (xterm mount + PTY connect) is already running in
  // parallel — createTab() kicks it off. Wait for all of them so the first
  // activateTab() below has a real terminal to fit and focus.
  await Promise.allSettled(restored.map((tab) => tab.ready));
  let lastActive = null;
  try { lastActive = sessionStorage.getItem(ACTIVE_KEY); } catch { /* private mode */ }
  const first = tabs.find((tab) => tab.sessionId && tab.sessionId === lastActive) || tabs[0];
  if (first) activateTab(first.id);

  // First visit to this install: the tour, once the layout has settled.
  if (!state.settings.tour_seen) {
    if (seenInThisBrowser()) markTourSeen();
    else setTimeout(openTour, 700);
  }
}

main();
