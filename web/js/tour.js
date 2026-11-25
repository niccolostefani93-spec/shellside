// The guided tour: a few steps over the real interface, one element lit up at
// a time. Shown once per install on the first visit (the flag is tour_seen in
// settings.json, see app.js), and again from the Guide button in the top bar.
// Phones and computers get different steps.
//
// No library: a spotlight box whose huge shadow dims everything else, and a
// card placed next to it. While it is open the page underneath does not take
// clicks or keys, so a stray Enter cannot reach the shell.

import { el } from "./ui.js";

const SEEN_KEY = "shellwalk.tourSeen";

/**
 * Each step: target (CSS selector, or null for a centred card), title, text,
 * and optionally `only: "touch" | "desktop"` and `chrome: true` (on a compact
 * screen, open the commands-and-folders drawer first so the target shows).
 */
const STEPS = [
  {
    target: null,
    title: "Welcome to Shellwalk",
    text: "A real shell on this machine, with a file panel that follows it. "
      + "This short tour shows what you can do. Reopen it any time with Guide in the top bar.",
  },
  {
    target: "#terminal-wrap",
    title: "The terminal",
    text: "A real Linux shell: vim, htop, ssh and background jobs all work. "
      + "It keeps running when you close the browser.",
  },
  {
    target: "#tabbar",
    title: "Tabs",
    text: "Each tab is its own shell. + opens a new one; ↺ lists every shell still running, "
      + "even one no tab shows. Open Shellwalk on another device and the same tabs are there.",
    only: "desktop",
  },
  {
    target: "#tabbar",
    title: "Tabs",
    text: "Each tab is its own shell, and the same tabs show up on every device. "
      + "⛶ goes full screen; ☰ shows saved commands and files.",
    only: "touch",
  },
  {
    target: "#crumbs",
    title: "Where you are",
    text: "The path follows the shell by itself, even after cd -, pushd or a script. "
      + "Tap any part of it to go there.",
  },
  {
    target: "#panel",
    chrome: true,
    title: "Files",
    text: "The folder the shell is in. Open a folder to go into it. Open a file and it does "
      + "the sensible thing: media and PDFs in the browser, text in the editor. "
      + "★ saves the folder to Favourites.",
  },
  {
    target: "#panel",
    chrome: true,
    title: "More actions",
    text: "Right-click a file, a folder, the empty space or the terminal for more: "
      + "rename, move, download, extract… An action types its command at the prompt "
      + "and waits for Enter, so you always see what will run. Remove asks first, "
      + "then runs.",
    only: "desktop",
  },
  {
    target: "#panel",
    chrome: true,
    title: "More actions",
    text: "Long-press a file, a folder or the empty space for more: rename, move, "
      + "download, upload, extract… An action types its command at the prompt and "
      + "waits for Enter, so you always see what will run. Remove asks first, then runs.",
    only: "touch",
  },
  {
    target: "#panel",
    title: "Drag and drop",
    text: "Drop files or folders from your computer onto the terminal or the panel to "
      + "upload them here. Drag one out of the panel onto your desktop to download it.",
    only: "desktop",
  },
  {
    target: "#topbar-commands",
    chrome: true,
    title: "Saved commands",
    text: "Commands one click away. + makes a new one; right-click one to edit it.",
  },
  {
    target: "#keybar",
    title: "Keys a phone lacks",
    text: "Esc, Tab, Ctrl, Alt, the arrows and more. A− and A+ change the text size "
      + "on this device.",
    only: "touch",
  },
  {
    target: "#terminal-wrap",
    title: "Ask in plain words",
    text: "Type what you want instead of a command, or start the line with a comma: "
      + "“, find the five biggest files here”. Suggestions appear in the terminal; "
      + "click one to paste it. Choose the model in Settings › Model.",
  },
  {
    target: "[data-script-id='settings_panel']",
    chrome: true,
    title: "Settings",
    text: "File types and their icons, saved commands, menus, the model, passwords and "
      + "access. Guide, next to it, brings this tour back.",
  },
];

/** Seen in this browser before the flag moved to the server (pre-0.1.1). */
export function seenInThisBrowser() {
  try { return localStorage.getItem(SEEN_KEY) === "1"; } catch { return false; }
}

function visible(node) {
  if (!node) return false;
  const rect = node.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0 && getComputedStyle(node).visibility !== "hidden";
}

/**
 * @param {object} options
 * @param {boolean} options.touch          a touch device (picks the steps)
 * @param {boolean} options.compact        the drawer layout is active
 * @param {(open:boolean)=>void} options.openChrome   open/close the drawer
 * @param {()=>void} [options.onClose]    the tour was finished or skipped
 */
export function startTour({ touch, compact, openChrome, onClose }) {
  if (document.querySelector(".tour")) return;
  const steps = STEPS.filter((step) => !step.only || step.only === (touch ? "touch" : "desktop"));
  const drawerWasOpen = document.body.classList.contains("chrome-open");
  let index = 0;

  const spot = el("div", { class: "tour-spot" });
  const counter = el("span", { class: "tour-count" });
  const title = el("h2", { class: "tour-title" });
  const text = el("p", { class: "tour-text" });
  const back = el("button", { class: "btn", text: "Back", onClick: () => go(index - 1) });
  const next = el("button", { class: "btn btn--primary", onClick: () => go(index + 1) });
  const skip = el("button", { class: "tour-skip", text: "Skip tour", onClick: () => close() });
  const card = el("div", { class: "tour-card", role: "dialog", "aria-modal": "true",
                           "aria-labelledby": "tour-title" }, [
    el("div", { class: "tour-head" }, [counter, skip]),
    title, text,
    el("div", { class: "tour-foot" }, [back, next]),
  ]);
  title.id = "tour-title";
  const overlay = el("div", { class: "tour" }, [spot, card]);
  document.body.append(overlay);

  function place() {
    const step = steps[index];
    const target = step.target ? document.querySelector(step.target) : null;
    const margin = 12;
    const vw = window.innerWidth;
    const vh = window.innerHeight;

    if (!visible(target)) {
      spot.hidden = true;
      overlay.classList.add("is-plain");
      card.style.left = `${Math.max(margin, (vw - card.offsetWidth) / 2)}px`;
      card.style.top = `${Math.max(margin, (vh - card.offsetHeight) / 2)}px`;
      return;
    }
    overlay.classList.remove("is-plain");
    spot.hidden = false;
    const rect = target.getBoundingClientRect();
    const pad = 4;
    const top = Math.max(0, rect.top - pad);
    const left = Math.max(0, rect.left - pad);
    const width = Math.min(vw, rect.right + pad) - left;
    const height = Math.min(vh, rect.bottom + pad) - top;
    Object.assign(spot.style, { top: `${top}px`, left: `${left}px`,
                                width: `${width}px`, height: `${height}px` });

    const cw = card.offsetWidth;
    const ch = card.offsetHeight;
    let cardTop;
    if (top + height + margin + ch <= vh - margin) cardTop = top + height + margin;   // below
    else if (top - margin - ch >= margin) cardTop = top - margin - ch;               // above
    else cardTop = Math.max(margin, top + height - ch - margin);                     // inside, at its foot
    const cardLeft = Math.min(Math.max(margin, left + width / 2 - cw / 2), vw - cw - margin);
    card.style.top = `${cardTop}px`;
    card.style.left = `${cardLeft}px`;
  }

  function go(to) {
    if (to < 0) return;
    if (to >= steps.length) { close(); return; }
    index = to;
    const step = steps[index];
    if (compact && openChrome) openChrome(Boolean(step.chrome));
    counter.textContent = `${index + 1} / ${steps.length}`;
    title.textContent = step.title;
    text.textContent = step.text;
    back.disabled = index === 0;
    next.textContent = index === steps.length - 1 ? "Done" : "Next";
    // The drawer animates open; place once now and again when it settles.
    place();
    setTimeout(place, 260);
    next.focus({ preventScroll: true });
  }

  // Keys go to the tour, not to the terminal underneath.
  function onKey(event) {
    const handled = { Escape: () => close(), ArrowRight: () => go(index + 1),
                      ArrowLeft: () => go(index - 1) }[event.key];
    if (event.key === "Enter" || event.key === " " || event.key === "Tab") {
      if (!card.contains(event.target)) { event.preventDefault(); next.focus(); }
      event.stopPropagation();
      return;
    }
    if (handled) { event.preventDefault(); handled(); }
    event.stopPropagation();
  }

  function close() {
    if (onClose) onClose();
    window.removeEventListener("keydown", onKey, true);
    window.removeEventListener("resize", place);
    overlay.remove();
    if (compact && openChrome) openChrome(drawerWasOpen);
  }

  window.addEventListener("keydown", onKey, true);
  window.addEventListener("resize", place);
  go(0);
}
