// Thin REST client. Every call funnels through here so error handling and the
// access-token header live in exactly one place.

let token = null;

export function setToken(value) {
  token = value;
}

async function request(method, path, body, isForm = false) {
  const headers = {};
  if (token) headers["x-shellwalk-token"] = token;
  if (body && !isForm) headers["Content-Type"] = "application/json";
  const response = await fetch(path, {
    method,
    headers,
    body: body ? (isForm ? body : JSON.stringify(body)) : undefined,
  });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const parsed = await response.json();
      if (parsed.detail) detail = parsed.detail;
    } catch { /* keep the status line */ }
    throw new Error(detail);
  }
  return response.json();
}

const query = (params) => new URLSearchParams(params).toString();

export const api = {
  bootstrap: () => request("GET", "/api/bootstrap"),
  reload: () => request("POST", "/api/reload"),
  dir: (path, showAll = false) =>
    request("GET", `/api/dir?${query({ path, show_all: showAll })}`),
  favorites: () => request("GET", "/api/favorites"),
  addFavorite: (path, label) => request("POST", "/api/favorites", { path, label }),
  removeFavorite: (path) => request("DELETE", `/api/favorites?${query({ path })}`),
  inject: (payload) => request("POST", "/api/inject", payload),
  sessionClose: (session) => request("POST", "/api/session/close", { session }),
  workspace: () => request("GET", "/api/workspace"),
  sessionTitle: (session, title) => request("POST", "/api/session/title", { session, title }),
  sessions: () => request("GET", "/api/sessions"),
  sessionAdopt: (session) => request("POST", "/api/sessions/adopt", { session }),
  invoke: (surface, id, ctx) => request("POST", "/api/invoke", { surface, id, ctx }),
  ask: (prompt, context) => request("POST", "/api/llm/ask", { prompt, context }),
  commands: () => request("GET", "/api/commands"),
  commandCreate: (payload) => request("POST", "/api/commands/create", payload),
  commandPatch: (id, values) => request("POST", "/api/commands/item", { id, values }),
  commandDelete: (id) => request("POST", "/api/commands/delete", { id }),

  scripts: () => request("GET", "/api/scripts"),
  scriptCreate: (fields, file) => {
    const form = new FormData();
    for (const [key, value] of Object.entries(fields)) form.append(key, value);
    form.append("file", file);
    return request("POST", "/api/scripts/create", form, true);
  },
  scriptPatch: (id, values) => request("POST", "/api/scripts/item", { id, values }),
  scriptReplaceCode: (id, file) => {
    const form = new FormData();
    form.append("id", id);
    form.append("file", file);
    return request("POST", "/api/scripts/code", form, true);
  },
  scriptDelete: (id) => request("POST", "/api/scripts/delete", { id }),
  menus: () => request("GET", "/api/context_menu"),
  menuPatch: (surface, id, values) =>
    request("POST", "/api/context_menu/item", { surface, id, values }),
  menuReset: (surface, id) => request("POST", "/api/context_menu/reset", { surface, id }),
  menuCreate: (payload) => request("POST", "/api/context_menu/create", payload),
  menuDelete: (surface, id) => request("POST", "/api/context_menu/delete", { surface, id }),
  settings: () => request("GET", "/api/settings"),
  saveSettings: (patch) => request("POST", "/api/settings", patch),
  services: () => request("GET", "/api/services"),
  updateStatus: () => request("GET", "/api/update/status"),
  updateCancel: () => request("POST", "/api/update/cancel", {}),
  updateApply: (token) => request("POST", "/api/update/apply", { token }),
  updatePreview: (file) => {
    const form = new FormData();
    form.append("file", file);
    return request("POST", "/api/update/preview", form, true);
  },
  backupUrl: () => "/api/update/backup",
  accessStatus: () => request("GET", "/api/access/status"),
  accessSetPassword: (password) => request("POST", "/api/access/password", { password }),
  accessLanOpen: (value) => request("POST", "/api/access/lan-open", { value }),
  accessSetBind: (host) => request("POST", "/api/network/bind", { host }),
  accessLogoutEverywhere: () => request("POST", "/api/access/logout-everywhere", {}),
  accessUnblock: () => request("POST", "/api/access/unblock", {}),
  accessLimits: (patch) => request("POST", "/api/access/limits", patch),
  accessSetInternetPassword: (password, slots, days) =>
    request("POST", "/api/access/internet-password", { password, slots, days }),
  accessRevokeInternetPassword: () =>
    request("POST", "/api/access/internet-password/revoke", {}),
  accessTunnelMode: (value) => request("POST", "/api/access/tunnel-mode", { value }),
  accessDevices: () => request("GET", "/api/access/devices"),
  accessDevicesRevoke: (device_id) => request("POST", "/api/access/devices/revoke", { device_id }),
  accessDevicesRevokeAll: () => request("POST", "/api/access/devices/revoke-all", {}),
  accessDevicesRename: (device_id, label) =>
    request("POST", "/api/access/devices/rename", { device_id, label }),
  logout: () => request("POST", "/auth/logout", {}),
  setService: (category, id) => request("POST", "/api/services/active", { category, id }),
  setServiceConfig: (category, id, values) =>
    request("POST", "/api/services/config", { category, id, values }),
  serviceModels: (category, id, base_url) =>
    request("POST", "/api/services/models", { category, id, base_url }),
  upload: (cwd, file) => {
    const form = new FormData();
    form.append("cwd", cwd);
    form.append("file", file);
    return request("POST", "/api/upload", form, true);
  },
  /**
   * Upload with progress. fetch() cannot report upload progress, so this one
   * uses XMLHttpRequest. onProgress(sentBytes, totalBytes).
   */
  uploadWithProgress: (cwd, file, relpath = "", onProgress = null) => new Promise((resolve, reject) => {
    const form = new FormData();
    form.append("cwd", cwd);
    form.append("file", file);
    if (relpath) form.append("relpath", relpath);
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/upload");
    if (token) xhr.setRequestHeader("x-shellwalk-token", token);
    if (onProgress) xhr.upload.onprogress = (event) => onProgress(event.loaded, event.total);
    xhr.onload = () => {
      let body = null;
      try { body = JSON.parse(xhr.responseText); } catch { /* not JSON */ }
      if (xhr.status >= 200 && xhr.status < 300) resolve(body);
      else reject(new Error((body && body.detail) || `${xhr.status} ${xhr.statusText}`));
    };
    xhr.onerror = () => reject(new Error("Connection lost during the upload."));
    xhr.onabort = () => reject(new Error("Upload cancelled."));
    xhr.send(form);
    return xhr;
  }),
  fileUrl: (path) => `/api/file?${query({ path })}`,
  downloadUrl: (path) => `/api/download?${query({ path })}`,
};
