// Settings > Access.
//
// Replaces the old swappable "security service" tab. There is nothing to pick
// here anymore — the gate in server/gate.py always runs — this panel only
// configures it: the LAN password, the explicit "no password" opt-in, and
// recovery if you lock yourself out.

import { api } from "./api.js";
import { confirm, el, toast } from "./ui.js";
import { promptText } from "./modals.js";

export function tabAccess(ctx) {
  const body = el("div", {});

  const render = async () => {
    let status;
    try {
      status = await api.accessStatus();
    } catch (error) {
      body.replaceChildren(el("div", { class: "empty" },
        [el("b", { text: "This tab only works from the machine running the app." })]));
      return;
    }

    const passwordInput = el("input", { class: "input", type: "password",
      placeholder: status.password_set ? "Change it\u2026" : "Set a password\u2026" });

    const saveButton = el("button", { class: "btn btn--primary", text: "Save" });
    saveButton.addEventListener("click", async () => {
      const value = passwordInput.value;
      if (!value) {
        const ok = await confirm({
          title: "Remove the LAN password?",
          message: "This returns Shellwalk to its unconfigured state: other " +
                    "devices are signed out, \u201cLAN without a password\u201d is " +
                    "switched off, and from the next restart it listens on this " +
                    "machine only. Run ./shellwalk setup to open it to the LAN again.",
          confirmLabel: "Remove it", danger: true,
        });
        if (!ok) return;
      }
      try {
        await api.accessSetPassword(value);
        toast(value ? "Saved. Other signed-in devices were signed out."
                    : "Removed. Back to this machine only.", "success");
        passwordInput.value = "";
        render();
      } catch (error) { toast(error.message, "error"); }
    });

    const lanOpenToggle = el("button", {
      class: "btn", text: status.lan_open ? "On" : "Off",
      "aria-pressed": String(status.lan_open),
    });
    lanOpenToggle.addEventListener("click", async () => {
      const next = !status.lan_open;
      if (next) {
        const ok = await confirm({
          title: "Allow the LAN without a password?",
          message: "Anyone on your network gets a shell on this machine, no " +
                    "password asked. The password stays saved and takes over " +
                    "again the moment you switch this off.",
          confirmLabel: "Allow it", danger: true,
        });
        if (!ok) return;
      }
      try {
        const result = await api.accessLanOpen(next);
        toast(result.lan_open ? "LAN is open." : "LAN password re-enabled.", "success");
        render();
      } catch (error) { toast(error.message, "error"); }
    });

    const bindNote = status.bind_loopback_only
      ? el("span", { text: "This machine's own browser needs no password " +
          "\u2014 the app is bound to loopback, so only your tunnel or this " +
          "device can reach it.", style: "color:var(--ink-faint);" })
      : el("span", { text: "Bound to 0.0.0.0: this password is what stands " +
          "between your LAN and a shell on this machine.",
          style: "color:var(--warn);" });

    const bindPending = status.bind_host_live !== status.bind_host_saved;
    const bindLabel = (host) => host === "0.0.0.0" ? "This network (LAN)" : "This device only";

    const setBind = async (host) => {
      if (host === status.bind_host_saved) return;
      let sessionNote = "";
      try {
        const upd = await api.updateStatus();
        if (upd.sessions && upd.sessions.length) {
          sessionNote = ` ${upd.sessions.length} shell${upd.sessions.length === 1 ? "" : "s"} ` +
            "open right now will be closed.";
        }
      } catch { /* not fatal if this check itself fails */ }

      const goingWide = host === "0.0.0.0";
      const ok = await confirm({
        title: goingWide ? "Allow this network to reach it?" : "Restrict to this device only?",
        message: (goingWide
          ? "Anyone on your network will be able to reach this app at its address on this network, " +
            "not just from this device. The LAN password below is what protects it once you do this " +
            "\u2014 set one first if you haven't."
          : "Only this device (or your tunnel) will be able to reach it after this. " +
            "Anything currently connecting over the network will lose access.")
          + " The app restarts to apply this." + sessionNote,
        confirmLabel: goingWide ? "Allow it" : "Restrict it",
        danger: goingWide,
      });
      if (!ok) return;

      try {
        const result = await api.accessSetBind(host);
        if (result.restarting) {
          toast("Restarting to apply it\u2026", "success");
          setTimeout(render, 2500);
        } else {
          render();
        }
      } catch (error) { toast(error.message, "error"); }
    };

    const bindToggle = el("div", { class: "segmented", role: "tablist" }, ["127.0.0.1", "0.0.0.0"].map((host) =>
      el("button", {
        role: "tab",
        class: `seg${status.bind_host_saved === host ? " is-on" : ""}`,
        text: bindLabel(host),
        onClick: () => setBind(host),
      })
    ));

    const bindSection = el("div", { class: "field" }, [
      el("span", { class: "field-label", text: "Reachable from" }),
      bindToggle,
      el("div", { class: "field-note" }, [
        bindPending
          ? el("span", { text: `Set to “${bindLabel(status.bind_host_saved)}”, applying now\u2026`,
                         style: "color:var(--warn);" })
          : el("span", { text: status.bind_host_saved === "0.0.0.0"
              ? "Reachable from any device on this network, at this machine's address."
              : "Reachable only from this device, or through a tunnel you've set up." }),
      ]),
    ]);

    const recoveryRow = el("div", { class: "field" }, [
      el("span", { class: "field-label", text: "Recovery link (this machine only)" }),
      el("div", { class: "row-code", style: "word-break:break-all;", text: status.recovery_url }),
      el("div", { class: "field-note",
        text: "Signs you in with no password, from loopback only. Rotates every " +
              "restart \u2014 also printed in the terminal that started the app." }),
    ]);

    const logoutEverywhereButton = el("button", { class: "btn btn--danger",
      text: "Sign out every device" });
    logoutEverywhereButton.addEventListener("click", async () => {
      const ok = await confirm({
        title: "Sign out everywhere?",
        message: "Every browser signed in with the LAN password is signed out " +
                  "immediately, including this one if you are using the password.",
        confirmLabel: "Sign out everywhere",
      });
      if (!ok) return;
      await api.accessLogoutEverywhere();
      toast("Done.", "success");
    });

    // ─── Internet password (device enrollment) ──────────────────────────

    const ip = status.internet_password;
    const daysLeft = ip.expires_at ? Math.max(0, (ip.expires_at - Date.now() / 1000) / 86400) : 0;

    const internetPasswordInput = el("input", { class: "input", type: "password",
      placeholder: "Set an internet password\u2026" });
    const slotsInput = el("input", { class: "input", type: "number", min: "1", value: "1",
      style: "width:70px;display:inline-block;" });
    const daysInput = el("input", { class: "input", type: "number", min: "1", value: "7",
      style: "width:70px;display:inline-block;" });

    const internetSaveButton = el("button", { class: "btn btn--primary", text: "Set" });
    internetSaveButton.addEventListener("click", async () => {
      const value = internetPasswordInput.value;
      if (!value) {
        const ok = await confirm({
          title: "Remove the internet password?",
          message: "New devices will no longer be able to enroll from outside. " +
                    "Devices already enrolled keep working on their own token.",
          confirmLabel: "Remove it", danger: true,
        });
        if (!ok) return;
        try { await api.accessSetInternetPassword("", 1, 1); toast("Removed.", "success"); render(); }
        catch (error) { toast(error.message, "error"); }
        return;
      }
      try {
        const result = await api.accessSetInternetPassword(
          value, parseInt(slotsInput.value, 10) || 1, parseFloat(daysInput.value) || 1);
        if (result.tunnel_mode && !status.tunnel_mode) {
          toast("Set. Tunnel mode turned on automatically \u2014 loopback no longer signs " +
                "you in without a password.", "success", 4000);
        } else {
          toast("Set.", "success");
        }
        internetPasswordInput.value = "";
        render();
      } catch (error) { toast(error.message, "error"); }
    });

    const internetStatusNote = !ip.set
      ? el("span", { text: "Not set \u2014 devices can only enroll from this machine.",
                     style: "color:var(--ink-faint);" })
      : ip.active
      ? el("span", { text: `Active: ${ip.slots_total - ip.slots_used} of ${ip.slots_total} ` +
              `enrollment${ip.slots_total === 1 ? "" : "s"} left, ` +
              `${daysLeft < 1 ? "under a day" : Math.ceil(daysLeft) + " days"} remaining.`,
              style: "color:var(--ok, var(--cyan));" })
      : el("span", { text: `Expired \u2014 used ${ip.slots_used} of ${ip.slots_total} slots. ` +
              "Set a new one to enroll more devices.", style: "color:var(--warn);" });

    const internetRevokeButton = el("button", { class: "btn btn--danger", text: "Revoke now",
      disabled: !ip.set || !ip.active });
    internetRevokeButton.addEventListener("click", async () => {
      const ok = await confirm({
        title: "Revoke the internet password now?",
        message: "It stops working immediately, whatever slots or days it had " +
                  "left. Devices already enrolled keep working on their own token " +
                  "\u2014 this only closes the door for new enrollments.",
        confirmLabel: "Revoke it", danger: true,
      });
      if (!ok) return;
      try { await api.accessRevokeInternetPassword(); toast("Revoked.", "success"); render(); }
      catch (error) { toast(error.message, "error"); }
    });

    const internetSection = el("div", { class: "field" }, [
      el("span", { class: "field-label", text: "Internet password (device enrollment)" }),
      internetPasswordInput,
      el("div", { style: "display:flex;gap:8px;align-items:center;margin-top:6px;" }, [
        el("span", { text: "Slots", style: "color:var(--ink-dim);font-size:12px;" }), slotsInput,
        el("span", { text: "Days", style: "color:var(--ink-dim);font-size:12px;" }), daysInput,
        internetSaveButton, internetRevokeButton,
      ]),
      el("div", { class: "field-note" }, [internetStatusNote]),
    ]);

    // ─── Tunnel mode ──────────────────────────────────────────────────────

    const tunnelToggle = el("button", {
      class: "btn", text: status.tunnel_mode ? "On" : "Off",
      "aria-pressed": String(status.tunnel_mode),
    });
    tunnelToggle.addEventListener("click", async () => {
      const next = !status.tunnel_mode;
      if (!next) {
        const ok = await confirm({
          title: "Turn tunnel mode off?",
          message: "If this app is bound to loopback and reachable through a tunnel " +
                    "(e.g. cloudflared), turning this off means that traffic is trusted " +
                    "again with no password \u2014 the tunnel forwards as loopback.",
          confirmLabel: "Turn it off", danger: true,
        });
        if (!ok) return;
      }
      try {
        const result = await api.accessTunnelMode(next);
        toast(result.tunnel_mode ? "Tunnel mode on." : "Tunnel mode off.", "success");
        render();
      } catch (error) { toast(error.message, "error"); }
    });

    const tunnelSection = el("div", { class: "field" }, [
      el("span", { class: "field-label", text: "Tunnel mode" }),
      tunnelToggle,
      el("div", { class: "field-note", text: "When on, a loopback-presenting request \u2014 " +
        "including your own tunnel \u2014 is no longer trusted automatically; it needs a " +
        "real session or device credential like everything else." }),
    ]);

    // ─── Devices ──────────────────────────────────────────────────────────

    let devicesList;
    try { devicesList = (await api.accessDevices()).devices; }
    catch { devicesList = []; }

    const revokeAllButton = el("button", { class: "btn btn--danger", text: "Revoke all devices" });
    revokeAllButton.addEventListener("click", async () => {
      const ok = await confirm({
        title: "Revoke every enrolled device?",
        message: "Every device that signs in with its own token \u2014 not the LAN " +
                  "password \u2014 will need to enroll again with a new internet password.",
        confirmLabel: "Revoke all", danger: true,
      });
      if (!ok) return;
      await api.accessDevicesRevokeAll();
      toast("Done.", "success");
      render();
    });

    const deviceRows = devicesList.length
      ? devicesList.map((d) => {
          const when = d.last_seen ? new Date(d.last_seen * 1000).toLocaleString() : "never";
          const meta = d.meta || {};
          const detailBits = [
            meta.os && meta.browser ? `${meta.os} \u00b7 ${meta.browser}` : null,
            meta.ip || null,
            meta.timezone || null,
          ].filter(Boolean);

          const revokeBtn = el("button", { class: "btn btn--danger", text: "Revoke",
            disabled: d.revoked });
          revokeBtn.addEventListener("click", async () => {
            await api.accessDevicesRevoke(d.id);
            toast("Revoked.", "success");
            render();
          });

          const renameBtn = el("button", { class: "btn", text: "Rename" });
          renameBtn.addEventListener("click", async () => {
            const next = await promptText({
              title: "Rename device", label: "Label", value: d.label,
              confirmLabel: "Save",
            });
            if (next === null) return;
            await api.accessDevicesRename(d.id, next);
            toast("Renamed.", "success");
            render();
          });

          return el("div", { style: "display:flex;justify-content:space-between;" +
              "align-items:center;padding:6px 0;border-bottom:1px solid var(--rule);" }, [
            el("div", {}, [
              el("div", { text: d.label + (d.is_this_device ? " (this device)" : ""),
                style: d.revoked ? "color:var(--ink-faint);text-decoration:line-through;" : "" }),
              el("div", { text: `last seen ${when}`, style: "color:var(--ink-faint);font-size:11px;" }),
              detailBits.length
                ? el("div", { text: detailBits.join(" \u2014 "),
                    style: "color:var(--ink-faint);font-size:11px;" })
                : null,
            ].filter(Boolean)),
            el("div", { style: "display:flex;gap:6px;" },
              d.revoked
                ? [el("span", { text: "revoked", style: "color:var(--ink-faint);font-size:12px;" })]
                : [renameBtn, revokeBtn]),
          ]);
        })
      : [el("div", { class: "field-note", text: "No devices enrolled yet." })];

    const devicesSection = el("div", { style: "margin-top:10px;" }, [
      el("span", { class: "field-label", text: "Enrolled devices" }),
      ...deviceRows,
      devicesList.length ? el("div", { style: "margin-top:10px;" }, [revokeAllButton]) : null,
    ].filter(Boolean));

    const lockoutSection = status.block_authentications
      ? el("div", { style: "padding:9px 11px;border-left:2px solid var(--danger);background:var(--ground);margin-top:14px;" }, [
          el("div", { text: `Sign-in is frozen after ${status.block_auth_after} ` +
                            "failed attempts.", style: "color:var(--danger);" }),
          el("button", { class: "btn", text: "Clear it", style: "margin-top:8px;",
            onClick: async () => { await api.accessUnblock(); toast("Cleared.", "success"); render(); } }),
        ])
      : null;

    const limits = el("div", {}, [
      el("div", { class: "field-label", text: "Attempt limits" }),
      ...["throttle_seconds", "block_ip_after", "block_auth_after"].map((key) => {
        const labels = {
          throttle_seconds: "Seconds between attempts",
          block_ip_after: "Failures before an address is blocked",
          block_auth_after: "Failures before sign-in freezes entirely",
        };
        const input = el("input", { class: "input", type: "number", min: "1",
          value: String(status[key]), style: "width:90px;display:inline-block;" });
        input.addEventListener("change", async () => {
          try { await api.accessLimits({ [key]: parseInt(input.value, 10) }); toast("Saved.", "success", 1600); }
          catch (error) { toast(error.message, "error"); }
        });
        return el("div", { style: "display:flex;justify-content:space-between;align-items:center;padding:5px 0;" },
          [el("span", { text: labels[key], style: "color:var(--ink-dim);font-size:12px;" }), input]);
      }),
    ]);

    body.replaceChildren(...[
      bindSection,
      el("div", { class: "field" }, [
        el("span", { class: "field-label", text: "LAN password" }),
        passwordInput, saveButton,
        el("div", { class: "field-note" }, [bindNote]),
      ]),
      el("div", { class: "field" }, [
        el("span", { class: "field-label", text: "Allow the LAN with no password" }),
        lanOpenToggle,
      ]),
      recoveryRow,
      el("div", { class: "field", style: "margin-top:6px;" }, [logoutEverywhereButton]),
      lockoutSection,
      el("div", { style: "margin-top:18px;padding-top:14px;border-top:1px solid var(--rule);" }, [
        tunnelSection, internetSection, devicesSection,
      ]),
      el("div", { style: "margin-top:18px;padding-top:14px;border-top:1px solid var(--rule);" }, [limits]),
    ].filter(Boolean));
  };

  render();
  return body;
}
