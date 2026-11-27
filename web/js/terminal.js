// xterm.js surface bound to the PTY websocket.
//
// The socket carries raw bytes in both directions. Reconnect is automatic and
// keeps the same session id, so a phone that drops its connection walking
// between cells comes back to the same shell with its scrollback intact.

// xterm ships as UMD bundles loaded from /static/vendor, so it arrives on
// window rather than as a module. Wait for the deferred scripts to land.
import { copyToClipboard } from "./ui.js";

function vendorReady() {
  if (window.Terminal) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const deadline = Date.now() + 8000;
    const tick = () => {
      if (window.Terminal) resolve();
      else if (Date.now() > deadline) reject(new Error("xterm failed to load from /static/vendor"));
      else setTimeout(tick, 30);
    };
    tick();
  });
}

const THEME = {
  background: "#10141a",
  foreground: "#c6d1de",
  cursor: "#e0a247",
  cursorAccent: "#10141a",
  selectionBackground: "#26303d",
  black: "#10141a", brightBlack: "#46525f",
  red: "#d4635f", brightRed: "#e08480",
  green: "#7fae6c", brightGreen: "#9cc78b",
  yellow: "#e0a247", brightYellow: "#eebd75",
  blue: "#5f97ca", brightBlue: "#84b2dc",
  magenta: "#b088c4", brightMagenta: "#c9a6d9",
  cyan: "#6aacc6", brightCyan: "#8ec7dd",
  white: "#c6d1de", brightWhite: "#e6edf5",
};

export class TerminalSurface {
  constructor(host, { fontSize = 14, sessionId = null, onSession, onStatus, onPasteLink, onClosed } = {}) {
    this.host = host;
    this.fontSize = fontSize;
    this.onSession = onSession;
    this.onClosed = onClosed;
    this.onStatus = onStatus;
    // Called with a command when a `shellwalk-paste:` link printed by the
    // command assistant (server/shell/assist_client.py) is clicked.
    this.onPasteLink = onPasteLink;
    // Which PTY to attach to is decided by whoever creates this surface —
    // now that there can be several, "the one session id in sessionStorage"
    // is not a thing. app.js owns the tab list and persistence; this class
    // just remembers whatever id the server confirms.
    this.sessionId = sessionId;
    this.socket = null;
    this.retry = 0;
    this.closed = false;
  }

  async start() {
    await vendorReady();
    const { Terminal } = window;
    const { FitAddon } = window.FitAddon;
    const { WebLinksAddon } = window.WebLinksAddon;
    this.term = new Terminal({
      fontFamily: '"JetBrains Mono", ui-monospace, monospace',
      fontSize: this.fontSize,
      theme: THEME,
      cursorBlink: true,
      scrollback: 10000,
      allowProposedApi: true,
      // The app owns right-click: it opens our context menu, not the browser's.
      rightClickSelectsWord: false,
      // OSC 8 hyperlinks printed into the terminal. The assistant writes its
      // suggestions as shellwalk-paste:<base64url command>; a click pastes
      // the command (never runs it). Ordinary web links open in a new tab.
      linkHandler: {
        allowNonHttpProtocols: true,
        activate: (event, uri) => this.activateLink(uri),
      },
    });
    this.fit = new FitAddon();
    this.term.loadAddon(this.fit);
    this.term.loadAddon(new WebLinksAddon());
    this.term.open(this.host);
    // Ctrl-Shift-C copies the selection, as in a desktop terminal. Without
    // this the browser takes the key (Chrome opens DevTools) and Ctrl-C goes
    // to the program as an interrupt. Tip: when a program grabs the mouse
    // (Claude Code, vim, htop), hold Shift while dragging to select anyway.
    this.term.attachCustomKeyEventHandler((event) => {
      if (event.ctrlKey && event.shiftKey && !event.altKey && event.code === "KeyC") {
        event.preventDefault();
        if (event.type === "keydown") {
          const text = this.term.getSelection();
          if (text) copyToClipboard(text);
        }
        return false;
      }
      return true;
    });
    // A newly created tab that is not the active one opens into a hidden
    // (display:none) host, where FitAddon has nothing to measure. The
    // ResizeObserver below picks it up correctly once it is shown.
    try { this.fit.fit(); } catch { /* hidden host: sized later, on activation */ }

    // inputFilter lets the phone key bar's sticky Ctrl turn the next typed
    // character into its control code.
    this.term.onData((data) => {
      const out = this.inputFilter ? this.inputFilter(data) : data;
      if (out) this.send({ type: "input", data: out });
    });
    this.term.onResize(({ rows, cols }) => this.send({ type: "resize", rows, cols }));

    const observer = new ResizeObserver(() => {
      try { this.fit.fit(); } catch { /* element not laid out yet */ }
    });
    observer.observe(this.host);

    this.connect();
  }

  connect() {
    const protocol = location.protocol === "https:" ? "wss" : "ws";
    const suffix = this.sessionId ? `?session=${encodeURIComponent(this.sessionId)}` : "";
    this.socket = new WebSocket(`${protocol}://${location.host}/ws/terminal${suffix}`);
    this.socket.binaryType = "arraybuffer";

    this.socket.onopen = () => {
      this.retry = 0;
      const { rows, cols } = this.term;
      this.send({ type: "resize", rows, cols });
    };

    this.socket.onmessage = (event) => {
      if (typeof event.data === "string") {
        let message;
        try { message = JSON.parse(event.data); } catch { return; }
        if (message.type === "session") {
          // A reconnect: the server sends the history again, and redraws
          // the screen, so start from a clean terminal instead of stacking
          // a second copy under the first.
          if (this.connectedOnce && message.resumed) this.term.reset();
          this.connectedOnce = true;
          this.sessionId = message.id;
          if (this.onSession) this.onSession(message);
        } else if (message.type === "closed") {
          // The shell was ended (its tab closed on another device, or it is
          // gone): stop reconnecting, the app drops the tab.
          this.closed = true;
          if (this.onClosed) this.onClosed(message);
        }
        return;
      }
      this.term.write(new Uint8Array(event.data));
    };

    this.socket.onclose = (event) => {
      if (this.onStatus) this.onStatus({ connected: false, code: event.code, reason: event.reason });
      if (this.closed) return;   // destroy() closed this on purpose — never reconnect
      if (event.code === 4403) {
        this.term.write(`\r\n\x1b[31m[shellwalk] access denied: ${event.reason}\x1b[0m\r\n`);
        return;
      }
      this.retry += 1;
      const delay = Math.min(8000, 400 * 2 ** Math.min(this.retry, 5));
      setTimeout(() => this.connect(), delay);
    };
  }

  activateLink(uri) {
    const prefix = "shellwalk-paste:";
    if (uri.startsWith(prefix)) {
      let command = "";
      try {
        const encoded = uri.slice(prefix.length).replace(/-/g, "+").replace(/_/g, "/");
        const bytes = Uint8Array.from(atob(encoded), (c) => c.charCodeAt(0));
        command = new TextDecoder().decode(bytes);
      } catch { return; }
      if (command && this.onPasteLink) this.onPasteLink(command);
      return;
    }
    if (/^https?:\/\//i.test(uri)) window.open(uri, "_blank", "noopener");
  }

  /** Tear down for good — closing a tab, not a dropped connection. */
  destroy() {
    this.closed = true;
    if (this.socket) { try { this.socket.close(1000); } catch { /* already gone */ } }
    if (this.term) { try { this.term.dispose(); } catch { /* already gone */ } }
  }

  send(message) {
    if (this.socket && this.socket.readyState === WebSocket.OPEN) {
      this.socket.send(JSON.stringify(message));
    }
  }

  focus() {
    if (this.term) this.term.focus();
  }

  clear() {
    if (this.term) this.term.clear();
  }

  selection() {
    return this.term ? this.term.getSelection() : "";
  }

  /**
   * Paste text straight into the PTY, like Ctrl-Shift-V in a desktop
   * terminal. Unlike the inject API it does not need the shell at its
   * prompt: xterm wraps it in bracketed-paste markers when the running
   * program asked for them, so vim, Claude Code, REPLs and the shell all
   * receive it as a paste, not as typed keys.
   */
  paste(text) {
    if (!this.term || !text) return;
    this.term.paste(text);
    this.term.focus();
  }

  /** The last `lines` rows of output, scrollback included, as plain text. */
  recentText(lines = 500) {
    if (!this.term) return "";
    const buffer = this.term.buffer.active;
    const end = buffer.length - 1;
    const start = Math.max(0, end - lines);
    const out = [];
    for (let index = start; index <= end; index += 1) {
      const line = buffer.getLine(index);
      if (!line) continue;
      // A wrapped row continues the previous one: join, so copied text keeps
      // its original long lines instead of breaking at the terminal width.
      const text = line.translateToString(true);
      if (line.isWrapped && out.length) out[out.length - 1] += text;
      else out.push(text);
    }
    return out.join("\n").replace(/\s+$/, "");
  }

  visibleText(lines = 60) {
    if (!this.term) return "";
    const buffer = this.term.buffer.active;
    const end = buffer.baseY + buffer.cursorY;
    const start = Math.max(0, end - lines);
    const out = [];
    for (let index = start; index <= end; index += 1) {
      const line = buffer.getLine(index);
      if (line) out.push(line.translateToString(true));
    }
    return out.join("\n").replace(/\n{3,}/g, "\n\n").trim();
  }

  /** Send bytes as if typed, e.g. from the phone key bar. */
  sendInput(data) {
    this.send({ type: "input", data });
  }

  /** Arrow key bytes, honouring the application cursor mode vim and less set. */
  arrow(letter) {
    const app = this.term && this.term.modes && this.term.modes.applicationCursorKeysMode;
    return `${app ? "\x1bO" : "\x1b["}${letter}`;
  }

  setFontSize(size) {
    if (!this.term) return;
    this.term.options.fontSize = size;
    try { this.fit.fit(); } catch { /* ignore */ }
  }
}
