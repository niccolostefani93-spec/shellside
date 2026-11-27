// Settings > Update / Backup.
//
// Three independent things from one panel: import a code update, review exactly
// what it will do, and download the whole project as a zip.
//
// The panel never merges anything itself. Preview stages the content and shows
// the plan; Apply arms it and the app restarts into run.py — the merge
// happens before the app's code is loaded again. See server/update_plan.py.

import { api } from "./api.js";
import { confirm, el, toast } from "./ui.js";

const ACTION_STYLE = {
  add: { mark: "+", colour: "var(--ok)", label: "new" },
  overwrite: { mark: "~", colour: "var(--warn)", label: "replaced" },
  delete: { mark: "\u2212", colour: "var(--danger)", label: "removed" },
};

function treeNode(node, depth = 0) {
  const style = ACTION_STYLE[node.action];
  const row = el("div", {
    style: `display:flex;gap:8px;align-items:baseline;padding:1px 0 1px ${depth * 14}px;`,
  }, [
    el("span", {
      text: style ? style.mark : (node.dir ? "\u25b8" : " "),
      style: `width:10px;flex:0 0 auto;color:${style ? style.colour : "var(--ink-faint)"};`,
    }),
    el("span", {
      text: node.name,
      style: `color:${style ? style.colour : (node.dir ? "#9fc3d2" : "var(--ink-dim)")};`,
    }),
    style ? el("span", { text: style.label,
                         style: "color:var(--ink-faint);font-size:11px;" }) : null,
  ]);
  return el("div", {}, [row, ...node.children.map((c) => treeNode(c, depth + 1))]);
}

function planView(plan, onApply, onCancel) {
  const counts = plan.counts || {};
  const busy = (plan.sessions || []).filter((s) => !s.idle);

  const summary = el("div", { style: "display:flex;gap:14px;margin-bottom:12px;" }, [
    el("span", { text: `${counts.add || 0} new`, style: "color:var(--ok);" }),
    el("span", { text: `${counts.overwrite || 0} replaced`, style: "color:var(--warn);" }),
    el("span", { text: `${counts.delete || 0} removed`, style: "color:var(--danger);" }),
    el("span", { text: plan.source || "", style: "color:var(--ink-faint);margin-left:auto;" }),
  ]);

  const warnings = (plan.warnings || []).length
    ? el("div", { style: "margin:12px 0;padding:9px 11px;border-left:2px solid var(--warn);background:var(--ground);" },
        [el("div", { class: "field-label", text: "Worth reading" }),
         ...plan.warnings.map((w) => el("div", { text: w,
           style: "color:var(--ink-dim);font-size:11px;margin-top:3px;" }))])
    : null;

  // Specific to this app: a restart drops live shells. Losing a prompt is
  // nothing; losing a half-finished job is not.
  const sessionNote = busy.length
    ? el("div", { style: "margin:12px 0;padding:9px 11px;border-left:2px solid var(--danger);background:var(--ground);" },
        [el("div", { class: "field-label", text: "Running right now" }),
         ...busy.map((s) => el("div", {
           text: `${s.running || "a program"} in ${s.cwd || "?"} \u2014 the restart will kill it.`,
           style: "color:var(--danger);font-size:11px;margin-top:3px;" }))])
    : null;

  return el("div", {}, [
    summary,
    warnings,
    sessionNote,
    el("div", { class: "field-label", text: "What will change" }),
    el("div", { style: "background:var(--ground);padding:10px;border:1px solid var(--rule);max-height:34vh;overflow:auto;" },
       (plan.trees || []).map((t) => treeNode(t))),
    el("div", { style: "display:flex;gap:8px;margin-top:14px;" }, [
      el("button", { class: "btn", text: "Discard", onClick: onCancel }),
      el("button", { class: "btn btn--primary",
                     text: "Apply and restart", onClick: onApply }),
    ]),
    el("div", { class: "field-note", style: "margin-top:8px;",
      text: "Nothing is written until you press Apply. The app restarts and the merge is performed before its code is loaded again \u2014 the page will reconnect on its own." }),
  ]);
}

export function tabUpdate(ctx) {
  const body = el("div", {});

  const render = async () => {
    let status;
    try {
      status = await api.updateStatus();
    } catch (error) {
      body.replaceChildren(el("div", { class: "empty" },
        [el("b", { text: error.message })]));
      return;
    }

    const picker = el("input", { type: "file", accept: ".zip", style: "display:none" });
    const chooseButton = el("button", {
      class: "btn btn--primary", text: "Choose an update zip\u2026",
      onClick: () => picker.click(),
    });

    picker.addEventListener("change", async () => {
      const file = picker.files[0];
      if (!file) return;
      chooseButton.disabled = true;
      chooseButton.textContent = "Reading\u2026";
      try {
        const plan = await api.updatePreview(file);
        body.replaceChildren(planView(plan, () => applyPlan(plan), async () => {
          await api.updateCancel();
          render();
        }));
      } catch (error) {
        toast(error.message, "error", 9000);
        body.replaceChildren(
          el("div", { style: "padding:9px 11px;border-left:2px solid var(--danger);background:var(--ground);margin-bottom:14px;" },
             [el("div", { text: error.message,
                          style: "color:var(--danger);white-space:pre-wrap;font-size:11px;" })]),
        );
        setTimeout(render, 30);
      }
    });

    const applyPlan = async (plan) => {
      const ok = await confirm({
        title: "Apply this update?",
        message: `${plan.counts.add} new, ${plan.counts.overwrite} replaced, ` +
                 `${plan.counts.delete} removed. The app will restart and every ` +
                 `open shell will be closed.`,
        confirmLabel: "Apply and restart",
        danger: true,
      });
      if (!ok) return;
      try {
        await api.updateApply(plan.token);
        toast("Applying and restarting\u2026 the page will reconnect.", "success", 12000);
      } catch (error) {
        toast(error.message, "error", 9000);
      }
    };

    const backupButton = el("button", {
      class: "btn", text: "Download your settings as a zip",
      onClick: () => {
        toast("Building the archive\u2026 this can take a few seconds.", "info", 4000);
        window.location.href = api.backupUrl();
      },
    });

    body.replaceChildren(...[
      el("div", { class: "field" }, [
        el("span", { class: "field-label", text: "Import an update (deprecated)" }),
        el("div", { class: "field-note", style: "color:var(--warn);margin-bottom:8px;",
          text: "Deprecated: importing a zip of code will be removed in a future version. Update with git pull and restart the service instead." }),
        picker, chooseButton,
        el("div", { class: "field-note",
          text: `The zip must contain ${status.roots.map((r) => r + "/").join(", ")} at the top level, with the project's own structure inside. It may also carry ${status.files.join(", ")}, and a delete.json listing paths to remove. Anything else is ignored and listed in the preview.` }),
      ]),
      status.staged
        ? el("div", { class: "field-note", style: "color:var(--warn);" }, [
            el("span", { text: "An unfinished import is still staged. " }),
            el("button", { class: "ghost", text: "Discard it",
              onClick: async () => { await api.updateCancel(); render(); } }),
          ])
        : null,
      el("div", { class: "field", style: "margin-top:20px;padding-top:16px;border-top:1px solid var(--rule);" }, [
        el("span", { class: "field-label", text: "Backup" }),
        backupButton,
        el("div", { class: "field-note",
          text: `Everything that makes this install yours, from ${status.data_home}: settings, saved commands, favorites, service choices, your own menu actions and scripts. Passwords and keys are left out. To restore, unzip it into that folder.` }),
      ]),
    ].filter(Boolean));
  };

  render();
  return body;
}
