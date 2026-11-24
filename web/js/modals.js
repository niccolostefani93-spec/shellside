// Modals: folder picker, saved commands, Ask-the-model, settings.

import { api } from "./api.js";
import { Navigator } from "./navigator.js";
import { tabUpdate } from "./update_panel.js";
import { tabAccess } from "./access_panel.js";
import { confirm, el, sheet, sheetBody, sheetFoot, toast, dismissSheet } from "./ui.js";

// ------------------------------------------------------------ folder picker

/**
 * Browse to a folder and return its path, or null if cancelled.
 * Reuses Navigator, so it looks and behaves exactly like the main folder view.
 */
export function pickFolder({ start, title = "Choose a folder", confirmLabel = "Move here" }) {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      dismissSheet();
      resolve(value);
    };

    const crumbs = el("ol", { class: "crumbs", style: "padding:0 0 8px;margin:0;list-style:none;display:flex;" });
    const grid = el("div", {
      class: "grid",
      style: "padding:0;max-height:44vh;overflow-y:auto;",
    });
    const chosen = el("div", { class: "field-note", text: start });

    const navigator = new Navigator({
      crumbs,
      grid,
      emptyMessage: "No subfolders here.",
      onNavigate: (path) => load(path),
    });

    async function load(path) {
      try {
        const listing = await api.dir(path, false);
        // A destination picker only cares about folders.
        listing.entries = listing.entries.filter((entry) => entry.dir);
        navigator.render(listing);
        chosen.textContent = listing.path;
      } catch (error) {
        toast(error.message, "error");
      }
    }

    const confirmButton = el("button", {
      class: "btn btn--primary",
      text: confirmLabel,
      onClick: () => finish(navigator.path),
    });

    sheet({
      title,
      body: sheetBody([
        el("div", { class: "field-label", text: "Destination" }),
        crumbs,
        grid,
        chosen,
      ]),
      footer: sheetFoot([
        el("button", { class: "btn", text: "Cancel", onClick: () => finish(null) }),
        confirmButton,
      ]),
      onClose: () => { if (!settled) { settled = true; resolve(null); } },
    });

    load(start);
  });
}

// ------------------------------------------------------------- text prompt

/**
 * Ask for a single value and return it, or null if cancelled.
 *
 * Used by any menu entry whose manifest carries a "prompt" block: New file,
 * New folder, Rename. The value goes into the command template, and the
 * command is then typed into the terminal — the modal collects the name, the
 * terminal still does the deciding.
 *
 * selectStem preselects "report" in "report.txt", so renaming keeps the
 * extension without anyone having to steer a caret on a phone.
 */
export function promptText({
  title,
  label = "Name",
  value = "",
  note = "",
  confirmLabel = "OK",
  selectStem = false,
}) {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (result) => {
      if (settled) return;
      settled = true;
      dismissSheet();
      resolve(result);
    };

    const input = el("input", { class: "input", spellcheck: "false",
                                autocapitalize: "off", autocomplete: "off" });
    input.value = value;

    const accept = () => {
      const trimmed = input.value.trim();
      if (!trimmed) {
        toast("Give it a name first.", "warning");
        input.focus();
        return;
      }
      finish(trimmed);
    };

    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") { event.preventDefault(); accept(); }
    });

    sheet({
      title: title || label,
      narrow: true,
      body: sheetBody([
        el("label", { class: "field" }, [
          el("span", { class: "field-label", text: label }),
          input,
          note ? el("div", { class: "field-note", text: note }) : null,
        ]),
      ]),
      footer: sheetFoot([
        el("button", { class: "btn", text: "Cancel", onClick: () => finish(null) }),
        el("button", { class: "btn btn--primary", text: confirmLabel, onClick: accept }),
      ]),
      onClose: () => { if (!settled) { settled = true; resolve(null); } },
    });

    // sheet() focuses the first input; take the selection over from it.
    setTimeout(() => {
      input.focus();
      const dot = input.value.lastIndexOf(".");
      const end = selectStem && dot > 0 ? dot : input.value.length;
      input.setSelectionRange(0, end);
    }, 0);
  });
}

// ----------------------------------------------------------- saved commands

const COMMAND_TABS = [
  { id: "topbar", label: "In the top bar",
    empty: "Nothing in the top bar yet.",
    match: (command) => command.topbar !== false },
  { id: "list", label: "In this list only",
    empty: "Nothing here yet. Commands you keep out of the top bar live here.",
    match: (command) => command.topbar === false },
];

// Which half of the Commands tab is showing. Kept across re-renders, because
// every edit redraws the whole Settings sheet.
let commandsView = "topbar";

/**
 * Settings › Commands: the one place saved commands are managed.
 *
 * Clicking a row runs it — including the ones that also sit in the top bar,
 * and it is the only way to run the ones kept out of it. The pencil edits,
 * the cross removes — for real, for every command here. There is no
 * shipped-vs-yours distinction: each one is a JSON file in DATA_HOME/commands/.
 */
function tabCommands(ctx) {
  const commands = ctx.state.commands || [];
  const back = () => openSettings(ctx, "commands");

  const redraw = async () => {
    try { await ctx.reload(); } catch (error) { toast(error.message, "error"); }
    back();
  };

  const runCommand = async (command) => {
    if (command.confirm) {
      const ok = await confirm({
        title: command.name,
        message: command.confirm_message || `Run “${command.name}”?`,
        confirmLabel: command.name,
        danger: command.style === "danger",
      });
      if (!ok) { back(); return; }
    }
    dismissSheet();
    ctx.inject(command.command, command.execute);
  };

  const remove = async (command) => {
    const ok = await confirm({
      title: "Delete command",
      message: `Delete “${command.name}”? The file commands/${command.id}.json in your data folder is removed.`,
      confirmLabel: "Delete",
      danger: true,
    });
    if (!ok) { back(); return; }
    try {
      await api.commandDelete(command.id);
      toast("Deleted.", "success");
    } catch (error) {
      toast(error.message, "error");
    }
    redraw();
  };

  function row(command) {
    const hidden = command.enabled === false;
    const tools = el("div", { class: "row-tools" }, [
      el("span", { class: "row-tag", text: command.execute ? "runs" : "pastes" }),
      el("button", { class: `tagbtn${hidden ? "" : " is-on"}`, text: hidden ? "Hidden" : "Shown",
                     title: hidden ? "Not in the top bar or this list. Click to show it."
                                    : "Click to hide it from the top bar and this list.",
                     onClick: async () => {
                       try {
                         await api.commandPatch(command.id, { enabled: hidden });
                       } catch (error) { toast(error.message, "error"); }
                       redraw();
                     } }),
      el("button", { class: "tagbtn", text: "\u270e", title: "Edit this command.",
                     onClick: () => commandForm(ctx, command, back) }),
      el("button", {
        class: "tagbtn", "data-style": "danger", text: "\u2715",
        title: "Delete this command.",
        onClick: () => remove(command),
      }),
    ]);

    return el("div", { class: `row${hidden ? " is-off" : ""}`,
                       style: "align-items:flex-start;" }, [
      el("button", {
        class: "row-main row-click",
        style: "text-align:left;",
        title: hidden ? "Hidden — show it first." : "Send it to the terminal",
        onClick: () => {
          if (hidden) { toast("That one is hidden. Show it first.", "warning"); return; }
          runCommand(command);
        },
      }, [
        el("div", { class: "row-name", text: command.name }),
        command.description ? el("div", { class: "row-desc", text: command.description }) : null,
        el("div", { class: "row-code", text: command.command }),
      ]),
      tools,
    ]);
  }

  const definition = COMMAND_TABS.find((entry) => entry.id === commandsView) || COMMAND_TABS[0];
  const items = commands.filter(definition.match);

  return el("div", {}, [
    el("div", { class: "segmented", style: "margin-bottom:10px;" }, COMMAND_TABS.map((entry) =>
      el("button", {
        class: `seg${entry.id === definition.id ? " is-on" : ""}`,
        text: entry.label,
        onClick: () => { commandsView = entry.id; back(); },
      })
    )),
    items.length
      ? el("div", { class: "rows" }, items.map(row))
      : el("div", { class: "empty" }, [
          el("b", { text: definition.empty }),
          el("span", { text: " Use “New command” below." }),
        ]),
    el("div", { class: "sheet-foot", style: "padding-left:0;padding-right:0;border-top:none;justify-content:flex-start;" }, [
      el("button", { class: "btn btn--primary", text: "New command",
                     onClick: () => commandForm(ctx, null, back) }),
    ]),
    el("div", { class: "field-note",
                text: "Commands in the top bar show up as chips on its left; the + after them adds a new one. Each command is a JSON file in the commands/ folder of your data folder (~/.config/shellside by default)." }),
  ]);
}

/** Open Settings on the Commands tab. Kept for the "saved_commands" builtin. */
export function savedCommands(ctx) {
  openSettings(ctx, "commands");
}

/**
 * Create or edit one saved command. `onDone` decides where you land after
 * Save or Cancel: the Settings tab reopens itself, the top bar just closes.
 */
export function commandForm(ctx, command = null, onDone = dismissSheet) {
  const editing = Boolean(command);
  const current = command || {};

  const name = el("input", { class: "input", value: current.name || "" });
  const description = el("input", { class: "input", value: current.description || "" });
  const body = el("textarea", { class: "textarea", spellcheck: "false",
                                style: "min-height:70px;" });
  body.value = current.command || "";

  const where = el("select", { class: "select" }, [
    el("option", { value: "topbar", text: "Top bar, and this list",
                   selected: current.topbar !== false }),
    el("option", { value: "list", text: "This list only",
                   selected: current.topbar === false }),
  ]);
  const mode = el("select", { class: "select" }, [
    el("option", { value: "paste", text: "Type it into the terminal — I press Enter",
                   selected: !current.execute }),
    el("option", { value: "run", text: "Run it immediately",
                   selected: Boolean(current.execute) }),
  ]);
  const style = el("select", { class: "select" }, ["default", "info", "warning", "danger"].map((value) =>
    el("option", { value, text: value, selected: (current.style || "default") === value })
  ));
  const confirmFirst = el("select", { class: "select" }, [
    el("option", { value: "no", text: "No", selected: !current.confirm }),
    el("option", { value: "yes", text: "Yes — ask before it runs",
                   selected: Boolean(current.confirm) }),
  ]);
  const confirmMessage = el("input", { class: "input",
    value: current.confirm_message || "", placeholder: `Run “${current.name || "this"}”?` });
  const order = el("input", { class: "input", value: String(current.order ?? 200),
                              style: "max-width:120px;" });

  const save = async () => {
    const values = {
      name: name.value.trim(),
      description: description.value.trim(),
      command: body.value.trim(),
      topbar: where.value === "topbar",
      execute: mode.value === "run",
      style: style.value,
      confirm: confirmFirst.value === "yes",
      confirm_message: confirmMessage.value.trim(),
      order: parseInt(order.value, 10) || 200,
    };
    if (!values.name || !values.command) {
      toast("A name and a command are both needed.", "warning");
      return;
    }
    try {
      if (editing) await api.commandPatch(current.id, values);
      else await api.commandCreate(values);
      toast(editing ? "Saved." : `“${values.name}” added.`, "success");
      await ctx.reload();
      commandsView = values.topbar ? "topbar" : "list";
      onDone();
    } catch (error) {
      toast(error.message, "error");
    }
  };

  sheet({
    title: editing ? `Edit “${current.name}”` : "New command",
    narrow: true,
    body: sheetBody([
      el("label", { class: "field" }, [
        el("span", { class: "field-label", text: "Name" }),
        name,
      ]),
      el("label", { class: "field" }, [
        el("span", { class: "field-label", text: "Description — shown under the name" }),
        description,
      ]),
      el("label", { class: "field" }, [
        el("span", { class: "field-label", text: "Command" }),
        body,
      ]),
      el("label", { class: "field" }, [
        el("span", { class: "field-label", text: "Where it appears" }),
        where,
      ]),
      el("label", { class: "field" }, [
        el("span", { class: "field-label", text: "When clicked" }),
        mode,
      ]),
      el("div", { style: "display:flex;gap:10px;" }, [
        el("label", { class: "field", style: "flex:1 1 0;" }, [
          el("span", { class: "field-label", text: "Colour" }),
          style,
        ]),
        el("label", { class: "field", style: "flex:0 0 120px;" }, [
          el("span", { class: "field-label", text: "Order" }),
          order,
        ]),
      ]),
      el("label", { class: "field" }, [
        el("span", { class: "field-label", text: "Confirm first" }),
        confirmFirst,
      ]),
      el("label", { class: "field" }, [
        el("span", { class: "field-label", text: "Confirmation message — optional" }),
        confirmMessage,
      ]),
    ]),
    footer: sheetFoot([
      el("button", { class: "btn", text: "Cancel",
                     onClick: () => onDone() }),
      el("button", { class: "btn btn--primary", text: editing ? "Save" : "Add",
                     onClick: save }),
    ]),
  });
}

// -------------------------------------------------------------------- scripts

/** Create or edit a script's display metadata (the right side of the top bar). */
export function scriptForm(ctx, script = null) {
  const editing = Boolean(script);
  const current = script || {};

  const name = el("input", { class: "input", value: current.name || "" });
  const icon = el("input", { class: "input", value: current.icon || "\u2731",
                             maxlength: "3", style: "max-width:80px;" });
  const description = el("input", { class: "input", value: current.description || "" });
  const style = el("select", { class: "select" }, ["default", "info", "warning", "danger"].map((value) =>
    el("option", { value, text: value, selected: (current.style || "default") === value })
  ));
  const order = el("input", { class: "input", value: String(current.order ?? 200),
                              style: "max-width:120px;" });
  const confirmFirst = el("select", { class: "select" }, [
    el("option", { value: "no", text: "No", selected: !current.confirm }),
    el("option", { value: "yes", text: "Yes — ask before it runs",
                   selected: Boolean(current.confirm) }),
  ]);
  const confirmMessage = el("input", { class: "input",
    value: current.confirm_message || "", placeholder: `Run “${current.name || "this"}”?` });

  const fileField = editing ? null : el("input", { type: "file", class: "input",
    accept: ".py,.zip" });

  const save = async () => {
    const fields = {
      name: name.value.trim(),
      icon: icon.value.trim(),
      description: description.value.trim(),
      style: style.value,
      order: parseInt(order.value, 10) || 200,
      confirm: confirmFirst.value === "yes",
      confirm_message: confirmMessage.value.trim(),
    };
    if (!fields.name) {
      toast("Give it a name first.", "warning");
      return;
    }
    try {
      if (editing) {
        await api.scriptPatch(current.id, fields);
        toast("Saved.", "success");
      } else {
        const file = fileField.files[0];
        if (!file) { toast("Attach a routes.py or a zip containing one.", "warning"); return; }
        await api.scriptCreate(fields, file);
        toast(`“${fields.name}” added.`, "success");
      }
      await ctx.reload();
      dismissSheet();
    } catch (error) {
      toast(error.message, "error");
    }
  };

  sheet({
    title: editing ? `Edit “${current.name}”` : "New script",
    narrow: true,
    body: sheetBody([
      el("div", { style: "display:flex;gap:10px;align-items:flex-end;" }, [
        el("label", { class: "field", style: "flex:1 1 auto;" }, [
          el("span", { class: "field-label", text: "Name" }),
          name,
        ]),
        el("label", { class: "field", style: "flex:0 0 auto;" }, [
          el("span", { class: "field-label", text: "Icon" }),
          icon,
        ]),
      ]),
      el("label", { class: "field" }, [
        el("span", { class: "field-label", text: "Description" }),
        description,
      ]),
      el("div", { style: "display:flex;gap:10px;" }, [
        el("label", { class: "field", style: "flex:1 1 0;" }, [
          el("span", { class: "field-label", text: "Colour" }),
          style,
        ]),
        el("label", { class: "field", style: "flex:0 0 120px;" }, [
          el("span", { class: "field-label", text: "Order" }),
          order,
        ]),
      ]),
      el("label", { class: "field" }, [
        el("span", { class: "field-label", text: "Confirm first" }),
        confirmFirst,
      ]),
      el("label", { class: "field" }, [
        el("span", { class: "field-label", text: "Confirmation message — optional" }),
        confirmMessage,
      ]),
      !editing ? el("label", { class: "field" }, [
        el("span", { class: "field-label", text: "Code — a routes.py, or a zip containing one" }),
        fileField,
        el("div", { class: "field-note",
                    text: "The zip needs routes.py at its top level. It runs server-side with an invoke(ctx) function, the same shape as any other script — see an existing one under app/scripts/ for the pattern." }),
      ]) : el("div", { class: "field-note",
                       text: "To change the code itself, use “Replace its code…” from the script's right-click menu." }),
    ]),
    footer: sheetFoot([
      el("button", { class: "btn", text: "Cancel", onClick: () => dismissSheet() }),
      el("button", { class: "btn btn--primary", text: editing ? "Save" : "Add",
                     onClick: save }),
    ]),
  });
}

/** Re-upload just the code for an existing script. */
export function scriptCodeUpload(ctx, script) {
  const fileField = el("input", { type: "file", class: "input", accept: ".py,.zip" });

  const save = async () => {
    const file = fileField.files[0];
    if (!file) { toast("Attach a routes.py or a zip containing one.", "warning"); return; }
    try {
      await api.scriptReplaceCode(script.id, file);
      toast("Code replaced.", "success");
      await ctx.reload();
      dismissSheet();
    } catch (error) {
      toast(error.message, "error");
    }
  };

  sheet({
    title: `Replace code — “${script.name}”`,
    narrow: true,
    body: sheetBody([
      el("label", { class: "field" }, [
        el("span", { class: "field-label", text: "New routes.py, or a zip containing one" }),
        fileField,
        el("div", { class: "field-note",
                    text: "This replaces the routes.py of \u201c" + script.name + "\u201d. The name, icon and other display settings are unchanged." }),
      ]),
    ]),
    footer: sheetFoot([
      el("button", { class: "btn", text: "Cancel", onClick: () => dismissSheet() }),
      el("button", { class: "btn btn--primary", text: "Replace", onClick: save }),
    ]),
  });
}

// -------------------------------------------------------- Ask-the-model chips

/**
 * Split a model answer into text and command chips.
 *
 * The handoff doc specifies |command| delimiters. Pipes are also shell syntax,
 * so `|ps aux | grep ssh|` is genuinely ambiguous. The rule used here:
 *
 *   - a line that starts and ends with a pipe is one command, inner pipes and
 *     all (first-to-last matching)
 *   - otherwise pipe pairs are matched non-greedily for inline mentions
 *
 * That matches the instruction given to the model in the service system prompt
 * ("put nothing else on a line that contains a command") while still degrading
 * sensibly when a model ignores it.
 */
export function parseAnswer(text) {
  const parts = [];
  for (const [index, line] of text.split("\n").entries()) {
    if (index > 0) parts.push({ type: "text", value: "\n" });
    const trimmed = line.trim();
    const first = line.indexOf("|");
    const last = line.lastIndexOf("|");

    if (trimmed.startsWith("|") && trimmed.endsWith("|") && trimmed.length > 2) {
      const command = line.slice(first + 1, last).trim();
      if (command) {
        if (line.slice(0, first)) parts.push({ type: "text", value: line.slice(0, first) });
        parts.push({ type: "command", value: command });
        if (line.slice(last + 1)) parts.push({ type: "text", value: line.slice(last + 1) });
        continue;
      }
    }

    if (first !== -1 && last > first) {
      let rest = line;
      const pattern = /\|([^|\n]+)\|/g;
      let cursor = 0;
      let match;
      let matched = false;
      while ((match = pattern.exec(rest)) !== null) {
        matched = true;
        if (match.index > cursor) {
          parts.push({ type: "text", value: rest.slice(cursor, match.index) });
        }
        parts.push({ type: "command", value: match[1].trim() });
        cursor = match.index + match[0].length;
      }
      if (matched) {
        if (cursor < rest.length) parts.push({ type: "text", value: rest.slice(cursor) });
        continue;
      }
    }
    parts.push({ type: "text", value: line });
  }
  return parts;
}

function renderAnswer(text, onPaste) {
  return el("div", { class: "answer" }, parseAnswer(text).map((part) =>
    part.type === "command"
      ? el("button", {
          class: "cmd-chip",
          text: part.value,
          title: "Paste into the terminal",
          onClick: () => onPaste(part.value),
        })
      : document.createTextNode(part.value)
  ));
}

export function askModel(ctx, { logText = "" } = {}) {
  const template = logText
    ? `I wanted to achieve:\n\n\nInstead I got:\n${logText}`
    : "I wanted to achieve:\n";

  const prompt = el("textarea", { class: "textarea", spellcheck: "false" });
  prompt.value = template;

  const output = el("div", { style: "margin-top:14px;" });
  const send = el("button", { class: "btn btn--primary", text: "Ask" });

  async function run() {
    const question = prompt.value.trim();
    if (!question) { toast("Write something to ask first.", "warning"); return; }
    send.disabled = true;
    output.replaceChildren(el("div", { class: "answer" }, [
      el("span", { class: "spinner", text: "\u2588" }),
      el("span", { text: " waiting for the model" }),
    ]));
    try {
      const result = await api.ask(question, "");
      if (!result.ok) {
        output.replaceChildren(el("div", { class: "answer", text: result.error }));
      } else {
        output.replaceChildren(
          el("div", { class: "field-label", text: "Answer \u2014 click a command to paste it" }),
          renderAnswer(result.answer, (command) => {
            ctx.inject(command, false);
            dismissSheet();
          })
        );
      }
    } catch (error) {
      output.replaceChildren(el("div", { class: "answer", text: error.message }));
    } finally {
      send.disabled = false;
    }
  }

  send.addEventListener("click", run);

  sheet({
    title: "Ask the model",
    body: sheetBody([
      el("label", { class: "field" }, [
        el("span", { class: "field-label", text: "Prompt \u2014 edit freely" }),
        prompt,
      ]),
      el("div", { class: "field-note",
                  text: "Commands the model wraps in pipes appear as chips. Clicking one pastes it into the terminal; you still press Enter." }),
      output,
    ]),
    footer: sheetFoot([
      el("button", { class: "btn", text: "Close", onClick: () => dismissSheet() }),
      send,
    ]),
  });

  // Put the cursor on the blank line under "I wanted to achieve:".
  const caret = template.indexOf("\n") + 1;
  prompt.focus();
  prompt.setSelectionRange(caret, caret);
}

// ------------------------------------------------------------------ settings

function fieldText(label, value, note, onChange) {
  const input = el("input", { class: "input", value: value ?? "" });
  input.addEventListener("change", () => onChange(input.value));
  return el("label", { class: "field" }, [
    el("span", { class: "field-label", text: label }),
    input,
    note ? el("div", { class: "field-note", text: note }) : null,
  ]);
}

function fieldToggle(label, value, note, onChange) {
  const button = el("button", {
    class: "btn",
    text: value ? "On" : "Off",
    "aria-pressed": value ? "true" : "false",
  });
  button.addEventListener("click", () => {
    value = !value;
    button.textContent = value ? "On" : "Off";
    button.setAttribute("aria-pressed", value ? "true" : "false");
    onChange(value);
  });
  return el("div", { class: "field" }, [
    el("span", { class: "field-label", text: label }),
    button,
    note ? el("div", { class: "field-note", text: note }) : null,
  ]);
}

function tabGeneral(ctx, patch) {
  const settings = ctx.state.settings;
  return el("div", {}, [
    fieldText("Shell", settings.shell,
      "Takes effect for new sessions. Close the tab and reopen it to restart.",
      (value) => patch({ shell: value })),
    fieldText("Starting folder", settings.start_dir, null,
      (value) => patch({ start_dir: value })),
    fieldText("Terminal font size", settings.font_size,
      "For every device. On a phone, A\u2212 and A+ on the key bar set a size for that device only.", (value) => {
      const size = parseInt(value, 10);
      if (size >= 8 && size <= 32) { patch({ font_size: size }); ctx.setFontSize(size); }
    }),
    fieldToggle("Clear the line before pasting", settings.clear_line_before_inject,
      "Sends Ctrl-U first so a half-typed line does not get mixed into the pasted command.",
      (value) => patch({ clear_line_before_inject: value })),
    fieldToggle("Refuse to paste while a program is running", settings.block_inject_when_busy,
      "When vim or a REPL owns the terminal, clicks would otherwise type into it.",
      (value) => patch({ block_inject_when_busy: value })),
    fieldToggle("Keep shells in tmux", settings.tmux_sessions,
      "New shells run inside Shellside's own tmux server, so they survive Shellside restarting or updating, and the tab reattaches to the same shell. Needs tmux on the host (sudo apt install tmux). Your own tmux sessions and ~/.tmux.conf are not touched. Takes effect for new tabs.",
      (value) => patch({ tmux_sessions: value })),
  ]);
}

// ------------------------------------------------------------ file types

const HANDLER_KINDS = [
  { kind: "browser", label: "Open in the browser", tag: "browser" },
  { kind: "editor",  label: "Open in the text editor", tag: "editor" },
  { kind: "command", label: "Run a command in the terminal", tag: "terminal" },
];

const HANDLER_PLACEHOLDERS =
  "{file_path} {file_name} {file_dir} {cwd} \u2014 substituted and shell-quoted.";

const kindInfo = (kind) => HANDLER_KINDS.find((item) => item.kind === kind) || { label: kind, tag: kind };
const takesCommand = (kind) => kind === "command";

function parseExtensions(text) {
  return Array.from(new Set(text.split(/[\s,]+/).map((item) => item.trim().toLowerCase())
    .filter((item) => item && item !== ".")
    .map((item) => (item.startsWith(".") ? item : `.${item}`)))).sort();
}

/**
 * Settings › File types.
 *
 * One list of handlers decides everything about clicking a file: which files
 * show, what a left click does, and what the right-click menu offers.
 */
function tabFileTypes(ctx, patch) {
  const settings = ctx.state.settings;
  const handlers = (settings.handlers || []).map((h) => ({ ...h }));

  const save = async (next) => {
    await patch({ handlers: next });
    openSettings(ctx, "file_types");
  };

  // Extensions an earlier left-click handler already claims: listing them
  // again only adds a right-click entry, which is worth saying out loud.
  const claimed = new Map();
  const shadowed = handlers.map((handler) => {
    const lost = handler.click === false ? []
      : handler.extensions.filter((ext) => claimed.has(ext));
    if (handler.click !== false) {
      handler.extensions.forEach((ext) => { if (!claimed.has(ext)) claimed.set(ext, handler.name); });
    }
    return lost;
  });

  const move = (index, delta) => {
    const next = handlers.slice();
    const [item] = next.splice(index, 1);
    next.splice(index + delta, 0, item);
    save(next);
  };
  const change = (index, values) => save(handlers.map((h, i) => (i === index ? { ...h, ...values } : h)));

  const rows = handlers.map((handler, index) => {
    const tools = el("div", { class: "row-tools" });
    tools.append(tagButton(handler.click === false ? "Menu only" : "Left click", {
      on: handler.click !== false,
      title: handler.click === false
        ? "Only in the right-click menu. Click to let it handle the left click too."
        : "Handles the left click for its extensions (unless a handler above already does). Click to make it menu-only.",
      onClick: () => change(index, { click: handler.click === false }),
    }));
    if (takesCommand(handler.kind)) {
      tools.append(tagButton(handler.execute ? "Runs" : "Pastes", {
        on: Boolean(handler.execute),
        style: handler.execute ? "warning" : null,
        title: handler.execute
          ? "Runs the moment you click. Click to make it wait for Enter instead."
          : "Types the command into the terminal and waits for Enter. Click to make it run immediately.",
        onClick: () => change(index, { execute: !handler.execute }),
      }));
    }
    if (index > 0) tools.append(tagButton("\u2191", { title: "Move up \u2014 higher wins the left click.", onClick: () => move(index, -1) }));
    if (index < handlers.length - 1) tools.append(tagButton("\u2193", { title: "Move down.", onClick: () => move(index, 1) }));
    tools.append(tagButton("Edit", { onClick: () => handlerForm(ctx, handlers, index) }));
    tools.append(tagButton("Delete", {
      style: "danger",
      onClick: async () => {
        const ok = await confirm({
          title: "Delete handler",
          message: `Delete \u201c${handler.name}\u201d? Its extensions stop opening on click, and stop showing in the folder panel unless another handler lists them.`,
          confirmLabel: "Delete",
        });
        if (ok) save(handlers.filter((_, i) => i !== index));
      },
    }));

    return el("div", { class: `row${handler.click === false ? " is-off" : ""}`, style: "align-items:flex-start;" }, [
      el("div", { class: "row-main" }, [
        el("div", { class: "row-name", text: `${handler.icon || ""} ${handler.name}`.trim() }),
        el("div", { class: "row-desc", text: handler.extensions.join(" ") }),
        takesCommand(handler.kind) ? el("div", { class: "row-code", text: handler.command }) : null,
        shadowed[index].length
          ? el("div", { class: "field-note", text: `Left click already taken above for: ${shadowed[index].join(" ")} \u2014 menu only for those.` })
          : null,
      ]),
      tools,
      el("span", { class: "row-tag", text: kindInfo(handler.kind).tag }),
    ]);
  });

  return el("div", {}, [
    el("div", { class: "sheet-foot", style: "padding:0 0 14px;border-top:none;justify-content:flex-start;" }, [
      el("button", { class: "btn btn--primary", text: "Add a handler",
                     onClick: () => handlerForm(ctx, handlers, null) }),
      el("button", {
        class: "btn",
        text: "Restore defaults",
        onClick: async () => {
          const ok = await confirm({
            title: "Restore default handlers",
            message: "Replace every handler with the ones the project ships with? Your own handlers are removed.",
            confirmLabel: "Restore",
            danger: true,
          });
          if (ok) save(ctx.state.defaultHandlers || []);
        },
      }),
    ]),
    el("div", { class: "rows" }, rows.length ? rows
      : [el("div", { class: "empty" }, [el("b", { text: "No handlers: no file opens on click." })])]),
    el("div", { class: "field-note", text:
      "Left click: the first handler from the top that lists the extension and is set to Left click; "
      + "a file no handler takes opens in the text editor (read-only if it is binary). "
      + "Right click: every handler that lists it, the left-click one first in bold. "
      + "A file shows in the folder panel when any handler lists its extension; folders and executables always show." }),
    fieldToggle("Show dotfiles", settings.show_dotfiles, null,
      (value) => patch({ show_dotfiles: value })),
    iconsSection(ctx),
  ]);
}

// ------------------------------------------------------------ file icons

/** Settings › File types › Icons: the emoji in front of each name. */
function iconsSection(ctx) {
  const icons = ctx.state.settings.file_icons || ctx.state.defaultFileIcons
    || { folder: "", default: "", executable: "", groups: [] };
  const groups = (icons.groups || []).map((g) => ({ ...g, extensions: [...g.extensions] }));

  const save = async (next) => {
    try {
      ctx.state.settings = await api.saveSettings({ file_icons: next });
      if (ctx.refreshPanel) ctx.refreshPanel();
      toast("Saved.", "success", 1600);
    } catch (error) { toast(error.message, "error"); }
    openSettings(ctx, "file_types");
  };
  const saveGroups = (next) => save({ ...icons, groups: next });

  const special = (key, label, note) => {
    const input = el("input", { class: "input icon-input", value: icons[key] || "",
                                maxlength: "8", "aria-label": `${label} icon` });
    input.addEventListener("change", () => save({ ...icons, [key]: input.value.trim() }));
    return el("div", { class: "row" }, [
      input,
      el("div", { class: "row-main" }, [
        el("div", { class: "row-name", text: label }),
        el("div", { class: "row-desc", text: note }),
      ]),
    ]);
  };

  const move = (index, delta) => {
    const next = groups.slice();
    const [item] = next.splice(index, 1);
    next.splice(index + delta, 0, item);
    saveGroups(next);
  };

  const rows = groups.map((group, index) => {
    const tools = el("div", { class: "row-tools" });
    if (index > 0) tools.append(tagButton("\u2191", { title: "Move up \u2014 higher wins when two groups list the same extension.", onClick: () => move(index, -1) }));
    if (index < groups.length - 1) tools.append(tagButton("\u2193", { title: "Move down.", onClick: () => move(index, 1) }));
    tools.append(tagButton("Edit", { onClick: () => iconGroupForm(ctx, icons, groups, index, save) }));
    tools.append(tagButton("Delete", {
      style: "danger",
      onClick: async () => {
        const ok = await confirm({
          title: "Delete icon group",
          message: `Delete \u201c${group.name}\u201d? Its files get the default icon.`,
          confirmLabel: "Delete",
        });
        if (ok) saveGroups(groups.filter((_, i) => i !== index));
      },
    }));
    return el("div", { class: "row", style: "align-items:flex-start;" }, [
      el("span", { class: "icon-preview", text: group.icon }),
      el("div", { class: "row-main" }, [
        el("div", { class: "row-name", text: group.name }),
        el("div", { class: "row-desc", text: group.extensions.join(" ") }),
      ]),
      tools,
    ]);
  });

  return el("div", { class: "icons-section" }, [
    el("span", { class: "field-label", style: "margin-top:22px;", text: "Icons" }),
    el("div", { class: "field-note", style: "margin:0 0 10px;", text:
      "The emoji in front of each name in the folder panel. A file takes the icon of the first group that lists its extension; otherwise the executable or default one." }),
    el("div", { class: "rows" }, [
      special("folder", "Folders", "Every folder."),
      special("executable", "Executables", "Files with the execute bit and no group of their own."),
      special("default", "Everything else", "Files no group lists."),
      ...rows,
    ]),
    el("div", { class: "sheet-foot", style: "padding:12px 0 0;border-top:none;justify-content:flex-start;" }, [
      el("button", { class: "btn btn--primary", text: "Add an icon group",
                     onClick: () => iconGroupForm(ctx, icons, groups, null, save) }),
      el("button", {
        class: "btn",
        text: "Restore default icons",
        onClick: async () => {
          const ok = await confirm({
            title: "Restore default icons",
            message: "Replace every icon and group with the ones the project ships with?",
            confirmLabel: "Restore",
            danger: true,
          });
          if (ok && ctx.state.defaultFileIcons) save(ctx.state.defaultFileIcons);
        },
      }),
    ]),
  ]);
}

function iconGroupForm(ctx, icons, groups, index, save) {
  const editing = index !== null;
  const current = editing ? groups[index] : { name: "", icon: "", extensions: [] };
  const name = el("input", { class: "input", value: current.name });
  const icon = el("input", { class: "input icon-input", value: current.icon, maxlength: "8" });
  const extensions = el("textarea", { class: "textarea", spellcheck: "false", style: "min-height:60px;" });
  extensions.value = current.extensions.join(" ");
  const back = () => openSettings(ctx, "file_types");

  const submit = () => {
    const values = {
      id: current.id || `g_${Date.now().toString(36)}`,
      name: name.value.trim(),
      icon: icon.value.trim(),
      extensions: parseExtensions(extensions.value),
    };
    if (!values.name) { toast("Give the group a name.", "warning"); return; }
    if (!values.icon) { toast("Pick an emoji for the group.", "warning"); return; }
    if (!values.extensions.length) { toast("List at least one extension.", "warning"); return; }
    const next = editing ? groups.map((g, i) => (i === index ? values : g)) : [values, ...groups];
    save({ ...icons, groups: next });
  };

  sheet({
    title: editing ? `Edit \u201c${current.name}\u201d` : "New icon group",
    body: sheetBody([
      el("div", { style: "display:flex;gap:10px;align-items:flex-end;" }, [
        el("label", { class: "field", style: "flex:0 0 auto;" }, [
          el("span", { class: "field-label", text: "Emoji" }),
          icon,
        ]),
        el("label", { class: "field", style: "flex:1 1 auto;" }, [
          el("span", { class: "field-label", text: "Name" }),
          name,
        ]),
      ]),
      el("label", { class: "field" }, [
        el("span", { class: "field-label", text: "Extensions" }),
        extensions,
        el("div", { class: "field-note", text: "Space separated, e.g. .jpg .png .webp. On a phone, the emoji keyboard fills the Emoji field; on a computer, paste one (Win + . or Ctrl + Cmd + Space opens a picker)." }),
      ]),
    ]),
    footer: sheetFoot([
      el("button", { class: "btn", text: "Cancel", onClick: back }),
      el("button", { class: "btn btn--primary", text: editing ? "Save" : "Add", onClick: submit }),
    ]),
  });
}

/** Create (index === null) or edit one handler, then save the whole list. */
function handlerForm(ctx, handlers, index) {
  const editing = index !== null;
  const current = editing ? handlers[index] : { kind: "command", click: true, execute: false, icon: "\u25b8" };

  const name = el("input", { class: "input", value: current.name || "" });
  const icon = el("input", { class: "input", value: current.icon || "", maxlength: "3", style: "max-width:80px;" });
  const extensions = el("textarea", { class: "textarea", spellcheck: "false", style: "min-height:60px;" });
  extensions.value = (current.extensions || []).join(" ");
  const kind = el("select", { class: "select" }, HANDLER_KINDS.map((item) =>
    el("option", { value: item.kind, text: item.label, selected: item.kind === current.kind })));
  const command = el("textarea", { class: "textarea", spellcheck: "false", style: "min-height:60px;" });
  command.value = current.command || "";
  const mode = el("select", { class: "select" }, [
    el("option", { value: "paste", text: "Type it into the terminal \u2014 I press Enter", selected: !current.execute }),
    el("option", { value: "run", text: "Run it immediately", selected: Boolean(current.execute) }),
  ]);
  const click = el("select", { class: "select" }, [
    el("option", { value: "yes", text: "Yes \u2014 a left click does this", selected: current.click !== false }),
    el("option", { value: "no", text: "No \u2014 right-click menu only", selected: current.click === false }),
  ]);

  const commandFields = el("div", {}, [
    el("label", { class: "field" }, [
      el("span", { class: "field-label", text: "Command" }),
      command,
      el("div", { class: "field-note", text: HANDLER_PLACEHOLDERS }),
    ]),
    el("label", { class: "field" }, [
      el("span", { class: "field-label", text: "When clicked" }),
      mode,
    ]),
  ]);
  const sync = () => {
    commandFields.style.display = takesCommand(kind.value) ? "" : "none";
  };
  kind.addEventListener("change", sync);
  sync();

  const back = () => openSettings(ctx, "file_types");

  const submit = async () => {
    const values = {
      ...current,
      id: current.id || `h_${Date.now().toString(36)}`,
      name: name.value.trim(),
      icon: icon.value.trim(),
      kind: kind.value,
      extensions: parseExtensions(extensions.value),
      command: takesCommand(kind.value) ? command.value.trim() : "",
      execute: mode.value === "run",
      click: click.value === "yes",
    };
    if (!values.name) { toast("Give the handler a name.", "warning"); return; }
    if (!values.extensions.length) { toast("List at least one extension.", "warning"); return; }
    if (takesCommand(values.kind) && !values.command) { toast("This kind of handler needs a command.", "warning"); return; }
    // New handlers go on top: adding one usually means "make a click do this".
    const next = editing ? handlers.map((h, i) => (i === index ? values : h)) : [values, ...handlers];
    try {
      ctx.state.settings = await api.saveSettings({ handlers: next });
      toast(editing ? "Handler updated." : `\u201c${values.name}\u201d added.`, "success");
      back();
    } catch (error) {
      toast(error.message, "error");
    }
  };

  sheet({
    title: editing ? `Edit \u201c${current.name}\u201d` : "New handler",
    body: sheetBody([
      el("div", { style: "display:flex;gap:10px;align-items:flex-end;" }, [
        el("label", { class: "field", style: "flex:1 1 auto;" }, [
          el("span", { class: "field-label", text: "Name \u2014 shown in the right-click menu" }),
          name,
        ]),
        el("label", { class: "field", style: "flex:0 0 auto;" }, [
          el("span", { class: "field-label", text: "Icon" }),
          icon,
        ]),
      ]),
      el("label", { class: "field" }, [
        el("span", { class: "field-label", text: "Extensions" }),
        extensions,
        el("div", { class: "field-note", text: "Space separated, e.g. .jpg .png .webp. These also become visible in the folder panel." }),
      ]),
      el("label", { class: "field" }, [
        el("span", { class: "field-label", text: "What it does" }),
        kind,
      ]),
      commandFields,
      el("label", { class: "field" }, [
        el("span", { class: "field-label", text: "Left click" }),
        click,
        el("div", { class: "field-note", text: "If a handler higher in the list already takes the left click for an extension, that one wins." }),
      ]),
    ]),
    footer: sheetFoot([
      el("button", { class: "btn", text: "Cancel", onClick: back }),
      el("button", { class: "btn btn--primary", text: editing ? "Save" : "Add", onClick: submit }),
    ]),
  });
}

// -------------------------------------------------- context menu editor

const SURFACE_LABELS = [
  { surface: "file_entry", scope: "entry",  label: "Folder panel \u2014 on a file or folder" },
  { surface: "file_entry", scope: "folder", label: "Folder panel \u2014 on the folder itself" },
  { surface: "command_line", scope: null,   label: "Terminal \u2014 right-click, nothing selected" },
  { surface: "log", scope: null,            label: "Terminal \u2014 right-click, text selected" },
];

const PLACEHOLDER_NOTE =
  "{file_path} {file_name} {file_dir} {cwd} {home} \u2014 substituted and shell-quoted before the command is used.";

function inSection(item, scope) {
  if (!scope) return true;
  return (item.scope || "entry") === scope || (item.scope || "entry") === "any";
}

export function tagButton(label, { on = false, style = null, title = "", onClick }) {
  return el("button", {
    class: `tagbtn${on ? " is-on" : ""}`,
    "data-style": style,
    title,
    text: label,
    onClick,
  });
}

/**
 * The context menu editor.
 *
 * Every row here is still a folder under app/context_menu/. This tab does not
 * replace that model, it just stops "run on click or type it out" from being a
 * thing you can only discover by reading a manifest.
 */
function tabContextMenus(ctx) {
  const host = el("div", {});
  const note = el("div", { class: "field-note" });

  const refresh = async (data) => {
    try {
      const fresh = data || await api.menus();
      ctx.state.contextMenus = fresh.context_menus;
      draw(fresh);
    } catch (error) {
      host.replaceChildren(el("div", { class: "empty" }, [el("b", { text: error.message })]));
    }
  };

  const act = async (promise) => {
    try {
      const fresh = await promise;
      toast("Saved.", "success", 1400);
      await refresh(fresh);
    } catch (error) {
      toast(error.message, "error");
    }
  };

  function row(surface, item) {
    const disabled = item.enabled === false;
    const isCommand = item.action === "command";
    const tools = el("div", { class: "row-tools" });

    if (isCommand) {
      tools.append(tagButton(item.execute ? "Runs" : "Pastes", {
        on: Boolean(item.execute),
        style: item.execute ? "warning" : null,
        title: item.execute
          ? "Runs the moment you click it. Click to make it wait for Enter instead."
          : "Types the command into the terminal and waits for you to press Enter. Click to make it run immediately.",
        onClick: () => act(api.menuPatch(surface, item.id, { execute: !item.execute })),
      }));
      tools.append(tagButton("Ask first", {
        on: Boolean(item.confirm),
        title: "Show a confirmation dialog before this action does anything.",
        onClick: () => act(api.menuPatch(surface, item.id, { confirm: !item.confirm })),
      }));
    }

    tools.append(tagButton(disabled ? "Hidden" : "Shown", {
      on: !disabled,
      title: disabled ? "Not in the menu. Click to put it back."
                      : "In the menu. Click to hide it.",
      onClick: () => act(api.menuPatch(surface, item.id, { enabled: disabled })),
    }));

    if (item.overridden && item.overridden.length && !item.user) {
      tools.append(tagButton("Reset", {
        title: `Changed here: ${item.overridden.join(", ")}. Put it back the way it shipped.`,
        onClick: () => act(api.menuReset(surface, item.id)),
      }));
    }

    if (item.user) {
      tools.append(tagButton("Edit", {
        title: "Change this action.",
        onClick: () => actionForm(ctx, { surface, item }),
      }));
      tools.append(tagButton("Delete", {
        style: "danger",
        title: "Remove this action for good.",
        onClick: async () => {
          const ok = await confirm({
            title: "Delete action",
            message: `Delete “${item.name}” from this menu? The folder app/context_menu/${surface}/${item.id}/ is removed.`,
            confirmLabel: "Delete",
          });
          if (ok) act(api.menuDelete(surface, item.id));
        },
      }));
    }

    return el("div", { class: `row${disabled ? " is-off" : ""}`,
                       style: "align-items:flex-start;" }, [
      el("div", { class: "row-main" }, [
        el("div", { class: "row-name", text: `${item.icon || ""} ${item.name}`.trim() }),
        item.description ? el("div", { class: "row-desc", text: item.description }) : null,
        item.command ? el("div", { class: "row-code", text: item.command }) : null,
      ]),
      tools,
      el("span", { class: "row-tag", text: item.user ? "yours" : item.action }),
    ]);
  }

  function draw(data) {
    const menus = data.context_menus || {};
    const sections = SURFACE_LABELS.map(({ surface, scope, label }) => {
      const items = (menus[surface] || []).filter((item) => inSection(item, scope));
      return el("div", { style: "margin-bottom:20px;" }, [
        el("div", { class: "field-label", text: label }),
        el("div", { class: "rows" }, items.length
          ? items.map((item) => row(surface, item))
          : [el("div", { class: "empty" }, [el("b", { text: "Nothing in this menu yet." })])]),
      ]);
    });

    note.textContent =
      "Every row is a folder under app/context_menu/. Actions you add here get their own folder; "
      + "changes to the ones that came with the project go to app/context_menu/overrides.json, so an update cannot quietly undo them.";

    host.replaceChildren(
      el("div", { class: "sheet-foot",
                  style: "padding:0 0 14px;border-top:none;justify-content:flex-start;" }, [
        el("button", { class: "btn btn--primary", text: "Add an action",
                       onClick: () => actionForm(ctx, {}) }),
      ]),
      ...sections,
      note,
    );
  }

  refresh();
  return host;
}

/** Create or edit one action. Command actions only — see menu_editor.py. */
function actionForm(ctx, { surface = null, item = null }) {
  const editing = Boolean(item);
  const current = item || {};
  const startScope = current.scope || "entry";

  const target = el("select", { class: "select" }, SURFACE_LABELS.map(({ surface: s, scope, label }) =>
    el("option", { value: `${s}|${scope || ""}`, text: label,
                   selected: surface === s && (scope || "") === (s === "file_entry" ? startScope : "") })
  ));
  if (editing) target.disabled = true;

  const name = el("input", { class: "input", value: current.name || "" });
  const icon = el("input", { class: "input", value: current.icon || "\u2731",
                             maxlength: "3", style: "max-width:80px;" });
  const description = el("input", { class: "input", value: current.description || "" });
  const command = el("textarea", { class: "textarea", spellcheck: "false",
                                   style: "min-height:70px;" });
  command.value = current.command || "";

  const mode = el("select", { class: "select" }, [
    el("option", { value: "paste", text: "Type it into the terminal \u2014 I press Enter",
                   selected: !current.execute }),
    el("option", { value: "run", text: "Run it immediately", selected: Boolean(current.execute) }),
  ]);
  const style = el("select", { class: "select" }, ["default", "info", "warning", "danger"].map((value) =>
    el("option", { value, text: value, selected: (current.style || "default") === value })
  ));
  const confirmFirst = el("select", { class: "select" }, [
    el("option", { value: "no", text: "No", selected: !current.confirm }),
    el("option", { value: "yes", text: "Yes \u2014 ask before it does anything",
                   selected: Boolean(current.confirm) }),
  ]);
  const extensions = el("input", { class: "input",
    value: ((current.match || {}).extensions || []).join(" ") });
  const kinds = el("select", { class: "select" }, [
    el("option", { value: "", text: "Files and folders" }),
    el("option", { value: "file", text: "Files only" }),
    el("option", { value: "dir", text: "Folders only" }),
  ]);
  const startKinds = ((current.match || {}).kinds || [])[0] || "";
  kinds.value = startKinds;

  const entryOnly = el("div", {}, [
    el("label", { class: "field" }, [
      el("span", { class: "field-label", text: "Only for these extensions \u2014 optional" }),
      extensions,
      el("div", { class: "field-note", text: "Space separated, e.g. .zip .tar.gz. Leave empty for every file." }),
    ]),
    el("label", { class: "field" }, [
      el("span", { class: "field-label", text: "Show it for" }),
      kinds,
    ]),
  ]);

  const syncScope = () => {
    const [chosen] = target.value.split("|");
    entryOnly.style.display = chosen === "file_entry" ? "" : "none";
  };
  target.addEventListener("change", syncScope);
  syncScope();

  const save = async () => {
    const [chosenSurface, chosenScope] = target.value.split("|");
    const values = {
      name: name.value.trim(),
      icon: icon.value.trim(),
      description: description.value.trim(),
      command: command.value.trim(),
      execute: mode.value === "run",
      confirm: confirmFirst.value === "yes",
      style: style.value,
      scope: chosenSurface === "file_entry" ? (chosenScope || "entry") : "entry",
      match: chosenSurface === "file_entry"
        ? {
            extensions: extensions.value.split(/[\s,]+/).filter(Boolean),
            kinds: kinds.value ? [kinds.value] : [],
          }
        : {},
    };
    if (!values.name || !values.command) {
      toast("A name and a command are both needed.", "warning");
      return;
    }
    try {
      if (editing) await api.menuPatch(surface, item.id, values);
      else await api.menuCreate({ surface: chosenSurface, ...values });
      toast(editing ? "Action updated." : `“${values.name}” added.`, "success");
      await ctx.reload();
      openSettings(ctx, "context_menu_editor");
    } catch (error) {
      toast(error.message, "error");
    }
  };

  sheet({
    title: editing ? `Edit “${current.name}”` : "New action",
    body: sheetBody([
      el("label", { class: "field" }, [
        el("span", { class: "field-label", text: "Which menu" }),
        target,
        editing ? el("div", { class: "field-note",
                              text: "An action cannot move between menus. Delete it and add it again." }) : null,
      ]),
      el("div", { style: "display:flex;gap:10px;align-items:flex-end;" }, [
        el("label", { class: "field", style: "flex:1 1 auto;" }, [
          el("span", { class: "field-label", text: "Name" }),
          name,
        ]),
        el("label", { class: "field", style: "flex:0 0 auto;" }, [
          el("span", { class: "field-label", text: "Icon" }),
          icon,
        ]),
      ]),
      el("label", { class: "field" }, [
        el("span", { class: "field-label", text: "Description \u2014 shown under the name" }),
        description,
      ]),
      el("label", { class: "field" }, [
        el("span", { class: "field-label", text: "Command" }),
        command,
        el("div", { class: "field-note", text: PLACEHOLDER_NOTE }),
      ]),
      el("label", { class: "field" }, [
        el("span", { class: "field-label", text: "When clicked" }),
        mode,
        el("div", { class: "field-note",
                    text: "Typing it out is the safer default: you see the real command, with the real paths, before anything happens." }),
      ]),
      el("div", { style: "display:flex;gap:10px;" }, [
        el("label", { class: "field", style: "flex:1 1 0;" }, [
          el("span", { class: "field-label", text: "Confirm first" }),
          confirmFirst,
        ]),
        el("label", { class: "field", style: "flex:1 1 0;" }, [
          el("span", { class: "field-label", text: "Colour" }),
          style,
        ]),
      ]),
      entryOnly,
    ]),
    footer: sheetFoot([
      el("button", { class: "btn", text: "Cancel",
                     onClick: () => openSettings(ctx, "context_menu_editor") }),
      el("button", { class: "btn btn--primary", text: editing ? "Save" : "Add", onClick: save }),
    ]),
  });
}

/**
 * The services of one category as an accordion: the active one is open and
 * carries its own settings (from `detailFor`) inside its row, so they can
 * never be mistaken for the settings of the service listed below it.
 */
function tabService(ctx, category, note, detailFor = null) {
  const info = (ctx.state.services || {})[category] || { available: [], active: null };
  return el("div", {}, [
    el("div", { class: "rows" }, info.available.length ? info.available.map((service) => {
      const active = service.id === info.active;
      const head = el(active ? "div" : "button", {
        class: "row",
        title: active ? null : `Make ${service.name} the active service.`,
        onClick: active ? null : async () => {
          try {
            ctx.state.services = await api.setService(category, service.id);
            toast(`${service.name} is now active.`, "success");
            openSettings(ctx, "llm_link");
          } catch (error) { toast(error.message, "error"); }
        },
      }, [
        el("span", { class: "service-mark", text: active ? "●" : "○" }),
        el("div", { class: "row-main" }, [
          el("div", { class: "row-name", text: service.name }),
          el("div", { class: "row-desc", text: service.description }),
        ]),
        el("span", { class: `row-tag${active ? " is-on" : ""}`, text: active ? "active" : "use" }),
      ]);
      const detail = active && detailFor ? detailFor(service) : null;
      return el("div", { class: `service${active ? " is-active" : ""}` }, [
        head,
        detail ? el("div", { class: "service-detail" }, [detail]) : null,
      ]);
    }) : [el("div", { class: "empty" }, [el("b", { text: "No services found." })])]),
    el("div", { class: "field-note service-note", text: note }),
  ]);
}

function tabModel(ctx, patch) {
  const settings = ctx.state.settings;
  return el("div", {}, [
    fieldToggle("Suggest a fix when a command is not found", settings.assist_on_not_found !== false,
      "When bash cannot find a command \u2014 a typo like \u201cmakedire cane\u201d, or a request in plain words \u2014 the line is sent to the model below and its suggestions appear in the terminal. Tap one to paste it; you still press Enter. Bash only. To ask on purpose, start the line with a comma: \u201c, find the biggest files here\u201d.",
      (value) => patch({ assist_on_not_found: value })),
    fieldToggle("Show the model what you see", settings.assist_share_context !== false,
      "Along with the line, the model gets the current folder's listing (as in the file panel: names, sizes, dates) and the last terminal lines, so \u201cdelete the 7 screenshots\u201d or \u201cfix the error above\u201d work. Turn off if your model service is not on this machine and you would rather not send that.",
      (value) => patch({ assist_share_context: value })),
    el("div", { class: "field" }, [
      el("span", { class: "field-label", text: "Model service" }),
      tabService(ctx, "llm",
        "Add a backend by creating services/llm/<name>/ with a manifest.json and a service.py exposing ask(prompt, context). \u201cEcho\u201d only repeats the question, so suggestions stay off while it is active.",
        (service) => serviceConnection(ctx, "llm", service)),
    ]),
  ]);
}

/**
 * Address and model of the active service, saved per install in
 * DATA_HOME/data/service_config.json (over the defaults in its manifest.json).
 * The model list comes from the service itself (Ollama: installed models),
 * so nothing here assumes a particular model exists.
 */
function serviceConnection(ctx, category, service) {
  if (!service || !(service.editable || []).length) return null;
  const config = service.config || {};
  const editable = service.editable;

  const address = el("input", { class: "input", spellcheck: "false",
                                autocapitalize: "off", autocomplete: "off" });
  address.value = config.base_url || "";
  const modelText = el("input", { class: "input", spellcheck: "false",
                                  autocapitalize: "off", autocomplete: "off",
                                  placeholder: "model name" });
  modelText.value = config.model || "";
  const modelSelect = el("select", { class: "select", style: "display:none;" });
  const status = el("div", { class: "field-note", text: "" });
  let usingSelect = false;

  const loadModels = async () => {
    status.textContent = "Looking for models\u2026";
    try {
      const result = await api.serviceModels(category, service.id, address.value.trim());
      if (!result.supported) { status.textContent = ""; return; }
      if (!result.ok) {
        usingSelect = false;
        modelSelect.style.display = "none";
        modelText.style.display = "";
        status.textContent = `Could not list models: ${result.error}`;
        return;
      }
      const current = usingSelect ? modelSelect.value : modelText.value.trim();
      modelSelect.replaceChildren(
        el("option", { value: "", text: "Automatic \u2014 first available model" }),
        ...result.models.map((name) => el("option", { value: name, text: name })),
      );
      if (current && !result.models.includes(current)) {
        modelSelect.append(el("option", { value: current, text: `${current} (not found)` }));
      }
      modelSelect.value = current;
      usingSelect = true;
      modelSelect.style.display = "";
      modelText.style.display = "none";
      status.textContent = result.models.length
        ? `${result.models.length} model${result.models.length === 1 ? "" : "s"} found.`
        : "No models found at this address.";
    } catch (error) { status.textContent = error.message; }
  };

  const save = async () => {
    const values = {};
    if (editable.includes("base_url")) values.base_url = address.value.trim();
    if (editable.includes("model")) values.model = usingSelect ? modelSelect.value : modelText.value.trim();
    try {
      ctx.state.services = await api.setServiceConfig(category, service.id, values);
      toast(`${service.name} settings saved.`, "success");
      loadModels();
    } catch (error) { toast(error.message, "error"); }
  };

  if (editable.includes("model")) loadModels();

  // Rendered inside the service's own row (see tabService), so it needs no
  // heading of its own: the row above it already says which service this is.
  return el("div", {}, [
    editable.includes("base_url") ? el("label", { class: "field" }, [
      el("span", { class: "field-label", text: "Address" }),
      address,
    ]) : null,
    editable.includes("model") ? el("label", { class: "field" }, [
      el("span", { class: "field-label", text: "Model" }),
      modelSelect,
      modelText,
    ]) : null,
    status,
    el("div", { class: "sheet-foot", style: "padding-left:0;padding-right:0;border-top:none;justify-content:flex-start;" }, [
      el("button", { class: "btn", text: "Refresh list", onClick: loadModels }),
      el("button", { class: "btn btn--primary", text: "Save", onClick: save }),
    ]),
  ]);
}

const RENDERERS = {
  settings_general: tabGeneral,
  settings_file_types: tabFileTypes,
  settings_context_menu_editor: (ctx) => tabContextMenus(ctx),
  settings_commands: tabCommands,
  settings_llm_link: tabModel,
  settings_update: tabUpdate,
  settings_security: tabAccess,
};

export function openSettings(ctx, activeId = null) {
  const panel = (ctx.state.scripts || []).find((script) => script.builtin === "settings");
  const tabs = (panel && panel.children) || [];
  const current = activeId || (tabs[0] && tabs[0].id);

  const body = sheetBody([]);
  const tabBar = el("div", { class: "tabs" }, tabs.map((tab) =>
    el("button", {
      class: `tab${tab.id === current ? " is-on" : ""}`,
      text: tab.name,
      title: tab.description,
      onClick: () => openSettings(ctx, tab.id),
    })
  ));

  const patch = async (values) => {
    try {
      ctx.state.settings = await api.saveSettings(values);
      toast("Saved.", "success", 1600);
    } catch (error) { toast(error.message, "error"); }
  };

  const tab = tabs.find((item) => item.id === current);
  const renderer = tab && RENDERERS[tab.builtin];
  body.replaceChildren(
    renderer
      ? renderer(ctx, patch)
      : el("div", { class: "empty" }, [
          el("b", { text: `The tab folder “${current}” was discovered` }),
          el("span", { text: " but no renderer is registered for it. Add one in web/js/modals.js, or give the folder a routes.py." }),
        ])
  );

  sheet({
    title: "Settings",
    body: el("div", { style: "display:flex;flex-direction:column;min-height:0;overflow:hidden;" },
             [tabBar, body]),
    footer: sheetFoot([
      el("button", {
        class: "btn",
        text: "Reload config from disk",
        onClick: async () => {
          try { await ctx.reload(); toast("Configuration reloaded.", "success"); }
          catch (error) { toast(error.message, "error"); }
        },
      }),
      el("button", { class: "btn btn--primary", text: "Done", onClick: () => dismissSheet() }),
    ]),
  });
}
