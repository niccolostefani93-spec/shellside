// The navigation widget: clickable breadcrumb plus a grid of entries.
//
// One implementation serves both the main view (where clicking a folder cd's
// the real shell) and the Move destination picker (where clicking only browses).
// The difference is entirely in the callbacks the owner passes in, so the two
// can never drift apart in behaviour or appearance.

import { el } from "./ui.js";

const UNITS = ["B", "K", "M", "G", "T"];

export function formatSize(bytes) {
  if (bytes === null || bytes === undefined) return "";
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < UNITS.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value < 10 && unit > 0 ? value.toFixed(1) : Math.round(value)}${UNITS[unit]}`;
}

// Used when the owner passes no icon table (and as its fallback values).
const FALLBACK_ICONS = { folder: "\u{1F4C1}", default: "\u{1F4C4}", executable: "\u2699\uFE0F", groups: [] };

// extension -> emoji, rebuilt only when the settings object changes.
let iconCache = { source: null, byExt: new Map() };

/** The emoji in front of an entry's name (Settings › File types › Icons). */
export function iconFor(entry, icons) {
  const table = icons || FALLBACK_ICONS;
  if (entry.dir) return table.folder || FALLBACK_ICONS.folder;
  if (iconCache.source !== table) {
    const byExt = new Map();
    for (const group of table.groups || []) {
      for (const ext of group.extensions || []) if (!byExt.has(ext)) byExt.set(ext, group.icon);
    }
    iconCache = { source: table, byExt };
  }
  const fromGroup = entry.ext ? iconCache.byExt.get(entry.ext) : null;
  if (fromGroup) return fromGroup;
  if (entry.executable) return table.executable || FALLBACK_ICONS.executable;
  return table.default || FALLBACK_ICONS.default;
}

export class Navigator {
  /**
   * @param {object} options
   * @param {HTMLElement} options.crumbs   container for breadcrumb segments
   * @param {HTMLElement} options.grid     container for entries
   * @param {(path:string)=>void} options.onNavigate      folder or crumb clicked
   * @param {(entry:object)=>void} [options.onOpenFile]   file clicked
   * @param {(event, entry|null)=>void} [options.onContext] right-click
   * @param {string} [options.emptyMessage]
   * @param {()=>object} [options.icons]   the file_icons settings
   */
  constructor(options) {
    this.crumbs = options.crumbs;
    this.grid = options.grid;
    this.onNavigate = options.onNavigate;
    this.onOpenFile = options.onOpenFile;
    this.onContext = options.onContext;
    // (entry) => absolute download URL; makes entries draggable out of the page.
    this.downloadUrl = options.downloadUrl || null;
    this.emptyMessage = options.emptyMessage || "Nothing here.";
    // () => the file_icons settings object; read at render time so a change
    // in Settings shows on the next render without rebuilding the widget.
    this.icons = options.icons || (() => null);
    this.listing = null;
    this.showAll = false;

    // Wired unconditionally and checked at event time: app.js assigns
    // onContext after construction, so a constructor-time check would leave
    // the empty grid area with the browser's own menu.
    this.grid.addEventListener("contextmenu", (event) => {
      if (!this.onContext) return;
      if (event.target.closest(".entry")) return; // entries handle their own
      this.onContext(event, null);
    });
    if (this.crumbs) {
      this.crumbs.addEventListener("contextmenu", (event) => {
        if (this.onContext) this.onContext(event, null);
      });
    }
  }

  get path() {
    return this.listing ? this.listing.path : null;
  }

  render(listing) {
    const previous = this.listing;
    this.listing = listing;
    this.renderCrumbs(listing);
    this.renderGrid(listing, previous ? previous.path : null);
  }

  renderCrumbs(listing) {
    if (!this.crumbs) return;
    const segments = listing.segments || [];
    this.crumbs.replaceChildren(...segments.map((segment) =>
      el("li", { class: "crumb" }, [
        el("button", {
          text: segment.label,
          title: segment.path,
          onClick: () => this.onNavigate(segment.path),
        }),
      ])
    ));
    // Keep the deepest segment in view when the path is long.
    this.crumbs.scrollLeft = this.crumbs.scrollWidth;
  }

  renderGrid(listing, previousPath = null) {
    // The panel now re-renders whenever the folder changes underneath it, not
    // only when you navigate. Scrolled halfway down a long folder, a deleted
    // file would otherwise throw you back to the top.
    const scroll = previousPath === listing.path ? this.grid.scrollTop : 0;
    this.renderGridInner(listing);
    if (scroll) this.grid.scrollTop = scroll;
  }

  renderGridInner(listing) {
    if (listing.error) {
      this.grid.replaceChildren(el("div", { class: "empty" }, [
        el("b", { text: listing.error }),
      ]));
      return;
    }
    if (!listing.entries.length) {
      const hidden = listing.hidden_count
        ? ` ${listing.hidden_count} entries are hidden by your file-type filter.`
        : "";
      this.grid.replaceChildren(el("div", { class: "empty" }, [
        el("b", { text: this.emptyMessage }),
        el("span", { text: hidden }),
      ]));
      return;
    }

    const icons = this.icons();
    this.grid.replaceChildren(...listing.entries.map((entry) => {
      const classes = ["entry"];
      if (entry.dir) classes.push("is-dir");
      if (entry.native) classes.push("is-native");
      if (entry.executable && !entry.dir) classes.push("is-exec");
      if (!entry.default_visible) classes.push("is-hidden-type");

      const node = el("button", {
        class: classes.join(" "),
        title: entry.path,
        // Read by the drag-and-drop upload: dropping on a folder uploads into it.
        "data-drop-dir": entry.dir ? entry.path : null,
        onClick: () => {
          if (entry.dir) this.onNavigate(entry.path);
          else if (this.onOpenFile) this.onOpenFile(entry);
        },
      }, [
        el("span", { class: "mark", text: iconFor(entry, icons), "aria-hidden": "true" }),
        el("span", { class: "label", text: entry.name }),
        el("span", { class: "meta", text: entry.dir ? "" : formatSize(entry.size) }),
      ]);

      if (this.downloadUrl) {
        // Drag an entry out of the browser onto the desktop or a folder to
        // download it; a folder arrives as a zip. "DownloadURL" is understood
        // by Chromium browsers (Chrome, Edge, Brave); elsewhere the drag
        // simply carries the link.
        node.draggable = true;
        node.addEventListener("dragstart", (event) => {
          const url = this.downloadUrl(entry);
          const name = entry.dir ? `${entry.name}.zip` : entry.name;
          const type = entry.dir ? "application/zip" : "application/octet-stream";
          event.dataTransfer.effectAllowed = "copy";
          event.dataTransfer.setData("DownloadURL", `${type}:${name}:${url}`);
          event.dataTransfer.setData("text/uri-list", url);
          event.dataTransfer.setData("text/plain", url);
          // Read back by app.js when the entry is dropped on the terminal.
          event.dataTransfer.setData("application/x-shellwalk-path", entry.path);
          node.classList.add("is-dragging");
        });
        node.addEventListener("dragend", () => node.classList.remove("is-dragging"));
      }

      if (this.onContext) {
        node.addEventListener("contextmenu", (event) => {
          event.stopPropagation();
          this.onContext(event, entry);
        });
      }
      return node;
    }));
  }
}
