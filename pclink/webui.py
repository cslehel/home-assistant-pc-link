"""The small local setup / status page served by the API (``api.enabled``)."""
from __future__ import annotations

SETUP_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PC Link</title>
<style>
  :root { --bg:#f5f6f8; --card:#fff; --fg:#1d232a; --muted:#6b7480; --line:#e2e5ea; --ok:#2ea043; --warn:#e6a217; --bad:#c0392b; --acc:#2563eb; }
  @media (prefers-color-scheme: dark) { :root { --bg:#14181d; --card:#1c2129; --fg:#e6e9ee; --muted:#98a2b3; --line:#2b323c; } }
  * { box-sizing: border-box; }
  body { margin:0; font:15px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; background:var(--bg); color:var(--fg); }
  header { display:flex; align-items:center; gap:12px; padding:14px 18px; border-bottom:1px solid var(--line); background:var(--card); position:sticky; top:0; }
  header h1 { font-size:18px; margin:0; flex:1; }
  header .dot { width:10px; height:10px; border-radius:50%; background:var(--muted); display:inline-block; }
  header .dot.on { background:var(--ok); }
  main { max-width:1100px; margin:0 auto; padding:18px; display:grid; gap:18px; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); }
  .card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:16px; }
  .card h2 { margin:0 0 10px; font-size:15px; letter-spacing:.02em; text-transform:uppercase; color:var(--muted); }
  .wide { grid-column: 1 / -1; }
  table { width:100%; border-collapse:collapse; font-size:14px; }
  th, td { text-align:left; padding:6px 8px; border-bottom:1px solid var(--line); vertical-align:top; }
  th { color:var(--muted); font-weight:600; }
  .pill { display:inline-block; padding:1px 8px; border-radius:999px; font-size:12px; background:var(--line); }
  .pill.active,.pill.passive,.pill.ok { background:#dff5e3; color:#116329; }
  .pill.idle,.pill.warn { background:#fff3d6; color:#7a4b00; }
  .pill.away,.pill.blocked,.pill.used_up,.pill.outside_hours,.pill.bad { background:#fde2e1; color:#8a1c14; }
  .muted { color:var(--muted); }
  input, select, button, textarea { font:inherit; color:inherit; background:var(--bg); border:1px solid var(--line); border-radius:6px; padding:6px 8px; }
  button { cursor:pointer; background:var(--acc); color:#fff; border-color:transparent; }
  button.secondary { background:var(--line); color:var(--fg); }
  button:disabled { opacity:.5; cursor:default; }
  .row { display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin:6px 0; }
  textarea { width:100%; min-height:380px; font:13px/1.4 ui-monospace, SFMono-Regular, Consolas, monospace; white-space:pre; }
  #msg { min-height:1.4em; }
  .bar { height:6px; background:var(--line); border-radius:3px; overflow:hidden; }
  .bar > i { display:block; height:100%; background:var(--ok); }
  .bar.warn > i { background:var(--warn); } .bar.bad > i { background:var(--bad); }
  details summary { cursor:pointer; color:var(--muted); }
  code { font-size:13px; }
</style>
</head>
<body>
<header>
  <span class="dot" id="dot"></span>
  <h1 id="title">PC Link</h1>
  <span class="muted" id="sub"></span>
  <input id="token" type="password" placeholder="API token" size="22">
  <button class="secondary" id="saveTok">Use token</button>
  <button class="secondary" id="forgetTok" title="Remove the token from this page">Forget token</button>
</header>
<main>
  <section class="card wide">
    <h2>Users</h2>
    <table><thead><tr><th>User</th><th>Status</th><th>Presence</th><th>Activity</th><th>Today</th><th>Remaining</th><th>Rules / mode</th><th>Actions</th></tr></thead>
    <tbody id="users"></tbody></table>
  </section>
  <section class="card">
    <h2>This PC</h2>
    <table id="pc"></table>
  </section>
  <section class="card">
    <h2>Recent events</h2>
    <table><tbody id="events"></tbody></table>
  </section>
  <section class="card wide">
    <h2>Configuration <span class="muted" id="cfgpath"></span></h2>
    <div class="row">
      <button class="secondary" id="loadCfg">Load config.yaml</button>
      <button id="saveCfg" disabled>Validate, save &amp; reload</button>
      <button class="secondary" id="reload">Reload from disk</button>
      <span id="msg"></span>
    </div>
    <textarea id="cfg" spellcheck="false" placeholder="Enter the API token above, then load the configuration."></textarea>
    <details><summary>Notes</summary>
      <p class="muted">Saving validates the YAML first and refuses invalid files. Changes to <code>storage</code>, <code>logging</code>, the agent's IPC port and the API section need a service restart; everything else applies immediately. The token is <code>api.token</code> in the config. Loading and saving the configuration here needs <code>api.admin_token</code> when one is set, otherwise <code>api.token</code> with <code>api.allow_config_edit: true</code>.</p>
    </details>
  </section>
</main>
<script>
(() => {
  const $ = (id) => document.getElementById(id);
  // The token lives in this tab only (sessionStorage: gone when the tab closes), never in
  // localStorage, where it stayed on the PC for every later user of this browser profile.
  const store = (() => { try { return window.sessionStorage; } catch { return null; } })();
  try { localStorage.removeItem("pclink_token"); } catch {}   // what an earlier version left behind
  let token = "";
  try { token = (store && store.getItem("pclink_token")) || ""; } catch {}
  $("token").value = token;
  $("saveTok").onclick = () => { token = $("token").value.trim(); try { store && store.setItem("pclink_token", token); } catch {} refresh(); };
  $("forgetTok").onclick = () => { token = ""; $("token").value = ""; try { store && store.removeItem("pclink_token"); } catch {}
    $("cfg").value = ""; $("saveCfg").disabled = true; $("msg").textContent = "Token forgotten."; refresh(); };
  const headers = () => token ? { "Authorization": "Bearer " + token, "Content-Type": "application/json" } : { "Content-Type": "application/json" };
  const fmtMin = (m) => m == null ? "–" : (m >= 60 ? Math.floor(m / 60) + " h " + String(Math.round(m % 60)).padStart(2, "0") + " min" : Math.round(m) + " min");
  const pill = (t) => `<span class="pill ${t || ""}">${t || "–"}</span>`;
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  async function api(path, opts) {
    const r = await fetch(path, Object.assign({ headers: headers() }, opts || {}));
    const text = await r.text();
    let data; try { data = JSON.parse(text); } catch { data = text; }
    if (!r.ok) throw new Error((data && data.error) || r.statusText);
    return data;
  }
  async function command(payload) {
    try { const r = await api("/api/command", { method: "POST", body: JSON.stringify(payload) });
      $("msg").textContent = r.ok ? "OK" : ("Refused: " + (r.error || "")); }
    catch (e) { $("msg").textContent = "Error: " + e.message; }
    setTimeout(refresh, 400);
  }
  // The buttons carry their command as data attributes and one listener runs them: nothing a
  // user name could contain ever becomes script (the page's CSP allows no inline handlers anyway).
  const button = (label, action, user, extra, secondary) =>
    `<button${secondary ? ' class="secondary"' : ""} data-action="${esc(action)}" data-user="${esc(user)}"${extra ? ` data-minutes="${Number(extra) || 0}"` : ""}>${esc(label)}</button>`;
  $("users").addEventListener("click", (ev) => {
    const b = ev.target.closest("button[data-action]");
    if (!b) return;
    const payload = { action: b.dataset.action, user: b.dataset.user };
    if (b.dataset.minutes) payload.minutes = Number(b.dataset.minutes);
    command(payload);
  });
  function userRow(u) {
    const rem = u.remaining_minutes;
    const lim = u.limit_minutes;
    let bar = "";
    if (u.managed && lim) {
      const pct = Math.max(0, Math.min(100, 100 * (u.used_minutes || 0) / (lim + (u.bonus_minutes || 0) + (u.rollover_minutes || 0))));
      bar = `<div class="bar ${rem <= 0 ? "bad" : rem <= 15 ? "warn" : ""}"><i style="width:${pct}%"></i></div>`;
    }
    const rules = Object.entries(u.rules || {}).map(([k, r]) => `${esc(r.name)}: ${pill(r.state)}`).join("<br>");
    const mode = [u.focus_active ? pill("focus " + fmtMin(u.focus_remaining_minutes)) : "", u.blocked ? pill("paused") : "", u.past_bedtime ? pill("bedtime") : "", u.login_blocked ? pill("login blocked") : ""].filter(Boolean).join(" ");
    const act = [u.activity, u.app, u.domain, u.now_playing ? "♪ " + u.now_playing : ""].filter(Boolean).map(esc).join(" · ");
    const n = esc(u.user);
    const actions = u.managed ? [
      button("+15", "add_minutes", u.user, 15), button("+30", "add_minutes", u.user, 30),
      button(u.blocked ? "Resume" : "Pause", u.blocked ? "unblock" : "block", u.user, 0, true),
      button(u.focus_active ? "End focus" : "Focus", u.focus_active ? "focus_off" : "focus", u.user, 0, true),
      button("Log off", "logoff", u.user, 0, true)].join(" ") :
      button("Log off", "logoff", u.user, 0, true);
    return `<tr><td><b>${esc(u.name || u.user)}</b><br><span class="muted">${n}${u.managed ? "" : " · unlimited"}</span></td>
      <td>${pill(u.status)}</td><td>${pill(u.presence)}<br><span class="muted">${esc((u.presence_details || {}).reason || "")}</span></td>
      <td>${act || "–"}</td><td>${fmtMin(u.used_minutes)}${bar}</td>
      <td>${u.managed ? fmtMin(rem) + (u.weekly_limit_minutes ? "<br><span class='muted'>week " + fmtMin(u.weekly_remaining_minutes) + "</span>" : "") : "–"}</td>
      <td>${rules}<br>${mode}</td><td class="row">${actions}</td></tr>`;
  }
  async function refresh() {
    try {
      const [pc, users, events] = await Promise.all([api("/api/status"), api("/api/users"), api("/api/events?n=12")]);
      $("dot").classList.add("on");
      $("title").textContent = "PC Link · " + (pc.device_name || "");
      const info = pc.service_info || {};
      $("sub").textContent = `v${info.version || ""} · ${pc.logged_in_users || "nobody logged in"}`;
      $("cfgpath").textContent = info.config || "";
      $("users").innerHTML = Object.values(users).sort((a, b) => (a.user > b.user ? 1 : -1)).map(userRow).join("") || "<tr><td colspan=8 class=muted>No users yet.</td></tr>";
      const rows = [["CPU", (pc.cpu_percent ?? "–") + " %"], ["Memory", (pc.memory_percent ?? "–") + " %"], ["Active sessions", pc.active_sessions],
        ["Time limits", pc.time_limits_enabled ? "on" : "off"], ["Parent at home", pc.parent_present ? "yes" : "no"],
        ["Parent PIN", pc.parent_pin_enabled ? (pc.parent_unlocked ? "unlocked" : "locked") : "not set"],
        ["Pending updates", pc.pending_updates ?? "–"], ["Reboot pending", pc.reboot_pending == null ? "–" : (pc.reboot_pending ? "yes" : "no")],
        ["USB devices", pc.usb_devices ?? "–"], ["Printer", pc.printer_status ?? "–"], ["Top process", pc.top_process ?? "–"],
        ["Uptime", pc.uptime_hours != null ? pc.uptime_hours.toFixed(1) + " h" : "–"]];
      $("pc").innerHTML = rows.map(([k, v]) => `<tr><th>${k}</th><td>${esc(v)}</td></tr>`).join("");
      $("events").innerHTML = events.map((e) => `<tr><td class="muted">${new Date(e.ts * 1000).toLocaleTimeString()}</td><td>${esc(e.user || "")}</td><td>${esc(e.kind)}</td><td class="muted">${esc(e.detail || "")}</td></tr>`).join("");
    } catch (e) { $("dot").classList.remove("on"); $("sub").textContent = e.message === "unauthorized" ? "enter the API token (or the read token) to see this PC" : "not connected: " + e.message; }
  }
  $("loadCfg").onclick = async () => { try { $("cfg").value = await api("/api/config"); $("saveCfg").disabled = false; $("msg").textContent = "Loaded."; } catch (e) { $("msg").textContent = "Error: " + e.message; } };
  $("saveCfg").onclick = async () => {
    $("msg").textContent = "Validating…";
    try { const r = await api("/api/config", { method: "PUT", body: $("cfg").value, headers: Object.assign(headers(), { "Content-Type": "text/yaml" }) });
      $("msg").textContent = r.ok ? ("Saved and reloaded." + (r.restart_needed ? " Restart the service for: " + r.restart_needed.join(", ") : "")) : ("Rejected: " + r.error); }
    catch (e) { $("msg").textContent = "Error: " + e.message; }
    refresh();
  };
  $("reload").onclick = () => api("/api/reload", { method: "POST" }).then((r) => { $("msg").textContent = r.ok ? "Reloaded." : ("Failed: " + r.error); refresh(); }).catch((e) => { $("msg").textContent = "Error: " + e.message; });
  refresh();
  setInterval(refresh, 5000);
})();
</script>
</body>
</html>
"""
