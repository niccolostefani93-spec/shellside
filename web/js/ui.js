// Shared UI primitives: toast, sheet (modal), context menu, confirm.

const toastHost = document.getElementById("toasts");
const scrim = document.getElementById("scrim");
const menuEl = document.getElementById("menu");

export function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "html") node.innerHTML = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2).toLowerCase(), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of [].concat(children)) {
    if (child) node.append(child);
  }
  return node;
}

export function toast(message, level = "info", ms = 4200) {
  const node = el("div", { class: "toast", "data-level": level, text: message });
  toastHost.append(node);
  setTimeout(() => {
    node.style.opacity = "0";
    setTimeout(() => node.remove(), 200);
  }, ms);
}

// --------------------------------------------------------------- clipboard

/**
 * Copy text, three ways, in decreasing order of comfort.
 *
 * navigator.clipboard only exists in a secure context: https, or localhost.
 * Reached over plain http on a LAN address — which is exactly how this app is
 * used from a phone — it is undefined, and the old code reported that as a
 * failure and stopped. So:
 *
 *   1. the real clipboard API, when the browser offers it
 *   2. the deprecated execCommand path, which still works on http
 *   3. a sheet with the text already selected, so a long-press and "Copy"
 *      finishes the job by hand
 *
 * Step 3 is not an error state. It is the answer when the browser will not
 * hand the clipboard to an http origin, and it always works.
 */
export async function copyToClipboard(text, { label = "Copied." } = {}) {
  if (!text) {
    toast("Nothing to copy.", "warning");
    return false;
  }
  const clipboard = window.navigator.clipboard;
  if (clipboard && window.isSecureContext) {
    try {
      await clipboard.writeText(text);
      toast(label, "success", 1600);
      return true;
    } catch { /* fall through to the older path */ }
  }
  if (legacyCopy(text)) {
    toast(label, "success", 1600);
    return true;
  }
  copySheet(text);
  return false;
}

function legacyCopy(text) {
  const field = el("textarea", {
    readonly: true,
    style: "position:fixed;top:0;left:0;width:1px;height:1px;opacity:0;",
  });
  field.value = text;
  document.body.append(field);
  let ok = false;
  try {
    field.focus();
    field.select();
    field.setSelectionRange(0, text.length);
    ok = document.execCommand("copy");
  } catch {
    ok = false;
  } finally {
    field.remove();
  }
  return ok;
}

/**
 * Read text off the device clipboard, three ways, mirroring copyToClipboard():
 *
 *   1. the real clipboard API, when the browser offers it and grants the
 *      "clipboard-read" permission
 *   2. — there is no execCommand("paste") equivalent for reading: browsers
 *      block scripted read access on http on purpose, and there is no
 *      legacy escape hatch the way there is for write
 *   3. a sheet with an empty field the user pastes into by hand (Ctrl-V, or
 *      long-press "Paste"), so the browser's own paste gesture — which is
 *      always allowed — does the work instead of the page
 *
 * Step 3 is not an error state, same as in copyToClipboard().
 */
export async function readFromClipboard() {
  const clipboard = window.navigator.clipboard;
  if (clipboard && window.isSecureContext) {
    try {
      const text = await clipboard.readText();
      if (text) return text;
    } catch { /* permission denied, or nothing on the clipboard: fall through */ }
  }
  return pasteSheet();
}

function pasteSheet() {
  return new Promise((resolve) => {
    const field = el("textarea", {
      class: "textarea",
      spellcheck: "false",
      style: "min-height:120px;",
    });
    let done = false;
    const finish = (value) => {
      if (done) return;
      done = true;
      dismissSheet();
      resolve(value);
    };
    sheet({
      title: "Paste here",
      narrow: true,
      body: sheetBody([
        el("div", { class: "field-label", text: "Long-press \u203a Paste, or Ctrl-V" }),
        field,
        el("div", {
          class: "field-note",
          text: "This browser will not hand the clipboard to the page directly, so paste it here and it will be typed into the terminal.",
        }),
      ]),
      footer: sheetFoot([
        el("button", { class: "btn", text: "Cancel", onClick: () => finish(null) }),
        el("button", { class: "btn btn--primary", text: "Insert", onClick: () => finish(field.value) }),
      ]),
      onClose: () => finish(null),
    });
  });
}

/**
 * Is there an image on the device clipboard? Asked before showing a menu, so
 * it must never prompt: "yes" / "no" only when the browser already lets the
 * page read the clipboard silently (Chrome after the permission was granted),
 * "unknown" everywhere else (Firefox, plain http, permission not yet asked).
 */
export async function clipboardImageAvailability() {
  const clipboard = window.navigator.clipboard;
  if (!window.isSecureContext || !clipboard || !clipboard.read) return "unknown";
  try {
    const permission = await window.navigator.permissions.query({ name: "clipboard-read" });
    if (permission.state !== "granted") return "unknown";
    const items = await clipboard.read();
    return items.some((item) => item.types.some((type) => type.startsWith("image/"))) ? "yes" : "no";
  } catch {
    return "unknown";  // Firefox does not know "clipboard-read"; or no focus
  }
}

/**
 * The image on the device clipboard as a Blob, null when there is none, or
 * false when the user cancelled the paste sheet.
 * Same ladder as readFromClipboard(): the clipboard API when the browser
 * allows it, else a sheet the user pastes into — the browser's own paste
 * gesture hands images to the page even on http.
 */
export async function readClipboardImage() {
  const clipboard = window.navigator.clipboard;
  if (clipboard && clipboard.read && window.isSecureContext) {
    try {
      const items = await clipboard.read();
      for (const item of items) {
        const type = item.types.find((candidate) => candidate.startsWith("image/"));
        if (type) return await item.getType(type);
      }
      return null;
    } catch { /* permission denied, or the user dismissed Firefox's prompt */ }
  }
  return pasteImageSheet();
}

function pasteImageSheet() {
  return new Promise((resolve) => {
    const field = el("textarea", {
      class: "textarea",
      spellcheck: "false",
      placeholder: "Ctrl-V here",
      style: "min-height:120px;",
    });
    const note = el("div", {
      class: "field-note",
      text: "This browser will not hand the clipboard to the page directly, so paste the image here and it will be saved in the folder.",
    });
    let done = false;
    const finish = (value) => {
      if (done) return;
      done = true;
      dismissSheet();
      resolve(value);
    };
    field.addEventListener("paste", (event) => {
      event.preventDefault();
      const item = Array.from(event.clipboardData ? event.clipboardData.items : [])
        .find((candidate) => candidate.kind === "file" && candidate.type.startsWith("image/"));
      const file = item ? item.getAsFile() : null;
      if (file) finish(file);
      else note.textContent = "That was not an image. Copy a screenshot or an image and paste again.";
    });
    sheet({
      title: "Paste image",
      narrow: true,
      body: sheetBody([
        el("div", { class: "field-label", text: "Long-press › Paste, or Ctrl-V" }),
        field,
        note,
      ]),
      footer: sheetFoot([
        el("button", { class: "btn", text: "Cancel", onClick: () => finish(false) }),
      ]),
      onClose: () => finish(false),
    });
  });
}

function copySheet(text) {
  const field = el("textarea", {
    class: "textarea",
    readonly: true,
    spellcheck: "false",
    style: "min-height:120px;",
  });
  field.value = text;
  sheet({
    title: "Copy this",
    narrow: true,
    body: sheetBody([
      el("div", { class: "field-label", text: "Already selected \u2014 Ctrl-C, or long-press \u203a Copy" }),
      field,
      el("div", {
        class: "field-note",
        text: "This browser only gives the clipboard to https and localhost, and you are on plain http. Reaching the app through your tunnel makes the Copy actions silent again.",
      }),
    ]),
    footer: sheetFoot([
      el("button", { class: "btn", text: "Select again", onClick: () => { field.focus(); field.select(); } }),
      el("button", { class: "btn btn--primary", text: "Done", onClick: () => dismissSheet() }),
    ]),
  });
  setTimeout(() => { field.focus(); field.select(); }, 0);
}

/**
 * Terminal output in a plain text field, scrolled to the end, for selecting
 * by hand. The way out when a full-screen program (Claude Code, vim, htop,
 * less with mouse on) grabs the mouse and xterm cannot select inside it.
 */
export function selectTextSheet(text) {
  const field = el("textarea", {
    class: "textarea",
    readonly: true,
    spellcheck: "false",
    style: "min-height:50vh;font-family:\"JetBrains Mono\",ui-monospace,monospace;font-size:12px;white-space:pre;",
  });
  field.value = text;
  sheet({
    title: "Select text",
    body: sheetBody([
      el("div", { class: "field-label", text: "Select what you need, then Copy (or Ctrl-C / long-press › Copy)" }),
      field,
    ]),
    footer: sheetFoot([
      el("button", { class: "btn", text: "Done", onClick: () => dismissSheet() }),
      el("button", {
        class: "btn btn--primary",
        text: "Copy",
        onClick: async () => {
          const selected = field.value.substring(field.selectionStart, field.selectionEnd);
          if (await copyToClipboard(selected || field.value,
                                    { label: selected ? "Copied." : "Copied everything." })) {
            dismissSheet();
          }
        },
      }),
    ]),
  });
  setTimeout(() => { field.scrollTop = field.scrollHeight; }, 0);
}

// ------------------------------------------------------------------- sheets

let closeSheet = null;

export function sheet({ title, body, footer, narrow = false, onClose }) {
  dismissSheet();
  const panel = el("div", { class: `sheet${narrow ? " sheet--narrow" : ""}` }, [
    el("div", { class: "sheet-head" }, [
      el("span", { class: "sheet-title", text: title }),
      el("button", { class: "ghost", text: "\u2715", "aria-label": "Close",
                     onClick: () => dismissSheet() }),
    ]),
    body,
    footer || null,
  ]);
  scrim.replaceChildren(panel);
  scrim.hidden = false;
  closeSheet = () => {
    scrim.hidden = true;
    scrim.replaceChildren();
    closeSheet = null;
    if (onClose) onClose();
  };
  const first = panel.querySelector("input, textarea, button.btn--primary");
  if (first) first.focus();
  return { panel, close: () => dismissSheet() };
}

export function dismissSheet() {
  if (closeSheet) closeSheet();
}

scrim.addEventListener("mousedown", (event) => {
  if (event.target === scrim) dismissSheet();
});

export function sheetBody(children) {
  return el("div", { class: "sheet-body" }, children);
}

export function sheetFoot(children) {
  return el("div", { class: "sheet-foot" }, children);
}

export function confirm({ title, message, confirmLabel = "Confirm", danger = true }) {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      dismissSheet();
      resolve(value);
    };
    sheet({
      title,
      narrow: true,
      body: sheetBody([el("p", { text: message, style: "margin:0;color:var(--ink-dim)" })]),
      footer: sheetFoot([
        el("button", { class: "btn", text: "Cancel", onClick: () => finish(false) }),
        el("button", {
          class: `btn ${danger ? "btn--danger" : "btn--primary"}`,
          text: confirmLabel,
          onClick: () => finish(true),
        }),
      ]),
      onClose: () => { if (!settled) { settled = true; resolve(false); } },
    });
  });
}

// -------------------------------------------------------------- context menu

export function contextMenu(event, { heading, items }) {
  event.preventDefault();
  hideMenu();
  const visible = items.filter(Boolean);
  if (!visible.length) return;

  menuEl.replaceChildren();
  if (heading) menuEl.append(el("div", { class: "menu-head", text: heading, title: heading }));

  let lastDefault = null;
  visible.forEach((item, index) => {
    if (item.separator) {
      menuEl.append(el("div", { class: "menu-sep" }));
      return;
    }
    if (lastDefault === true && item.bold !== true) {
      menuEl.append(el("div", { class: "menu-sep" }));
    }
    lastDefault = item.bold === true;
    menuEl.append(el("button", {
      class: `menu-item${item.bold ? " is-default" : ""}`,
      "data-style": item.style && item.style !== "default" ? item.style : null,
      title: item.description || "",
      onClick: () => { hideMenu(); item.run(); },
    }, [
      el("span", { class: "icon", text: item.icon || "" }),
      el("span", { text: item.name }),
      item.hint ? el("span", { class: "hint", text: item.hint }) : null,
    ]));
  });

  menuEl.hidden = false;
  // Position after layout so we can measure and keep it on screen.
  const rect = menuEl.getBoundingClientRect();
  const x = Math.min(event.clientX, window.innerWidth - rect.width - 8);
  const y = Math.min(event.clientY, window.innerHeight - rect.height - 8);
  menuEl.style.left = `${Math.max(8, x)}px`;
  menuEl.style.top = `${Math.max(8, y)}px`;
}

export function hideMenu() {
  menuEl.hidden = true;
}

document.addEventListener("mousedown", (event) => {
  if (!menuEl.hidden && !menuEl.contains(event.target)) hideMenu();
});
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  if (!menuEl.hidden) { hideMenu(); return; }
  dismissSheet();
});
window.addEventListener("blur", hideMenu);
