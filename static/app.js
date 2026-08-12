"use strict";
// Single-page controller. No framework, no build step.

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const api = async (path, opts = {}) => {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  const text = await res.text();
  let data;
  try { data = text ? JSON.parse(text) : {}; } catch { data = { raw: text }; }
  if (!res.ok) throw new Error(data.detail || data.error || res.statusText);
  return data;
};

// -------------------------------- tabs -------------------------------- //
function activateTab(name) {
  $$(".tab").forEach((t) => t.classList.toggle("tab-active", t.dataset.tab === name));
  $$(".panel").forEach((p) => p.classList.toggle("hidden", p.dataset.panel !== name));
  if (name === "history") loadHistory();
  if (name === "mapping") loadProfileList();
}
$$(".tab").forEach((t) => t.addEventListener("click", () => activateTab(t.dataset.tab)));

// State shared across tabs.
const S = { token: null, headers: [], psFields: [], matches: [], importType: "products" };

// -------------------------- import mode ------------------------------- //
function setMode(mode) {
  S.importType = mode === "combinations" ? "combinations" : "products";
  const badge = $("#modeBadge");
  badge.textContent = "Mode: " + (S.importType === "combinations" ? "Combinations" : "Products");
  badge.classList.remove("hidden");
  // Sensible default scope per mode. Combinations includes descriptions so a
  // single sheet can set a per-combination description in the same pass.
  const on = S.importType === "combinations"
    ? ["combinations", "stock", "images", "descriptions"]
    : ["products"];
  $$(".scope").forEach((c) => { c.checked = on.includes(c.value); });
  // Tax conversion applies to product prices AND combination price impacts.
  $("#priceTaxRow").classList.remove("hidden");
  $("#modeOverlay").classList.add("hidden");
}
$$("[data-mode]").forEach((b) => b.addEventListener("click", () => setMode(b.dataset.mode)));
$("#btnChangeMode").addEventListener("click", () => $("#modeOverlay").classList.remove("hidden"));

// ------------------------------ settings ------------------------------ //
async function loadSettings() {
  try {
    const s = await api("/api/settings");
    $("#shopUrl").value = s.url || "";
    $("#langId").value = s.default_lang_id || 1;
    if (s.has_api_key) $("#apiKey").placeholder = "•••• saved (" + s.api_key_masked + ")";
  } catch (e) { /* first run */ }
}

function connPayload() {
  return {
    url: $("#shopUrl").value.trim() || null,
    api_key: $("#apiKey").value.trim() || null,
    default_lang_id: parseInt($("#langId").value, 10) || 1,
  };
}

$("#btnSave").addEventListener("click", async () => {
  await api("/api/settings", { method: "POST", body: JSON.stringify(connPayload()) });
  $("#testResult").innerHTML = `<span class="text-green-600">Saved.</span>`;
});

$("#btnTest").addEventListener("click", async () => {
  const el = $("#testResult");
  el.innerHTML = `<span class="text-slate-500">Testing…</span>`;
  try {
    const r = await api("/api/settings/test", {
      method: "POST", body: JSON.stringify(connPayload()),
    });
    if (!r.ok) {
      el.innerHTML = `<div class="text-red-600 font-medium">✗ ${r.error}</div>`;
      return;
    }
    const chips = r.resources.map((x) =>
      `<span class="inline-block bg-slate-100 border border-slate-200 rounded px-2 py-0.5 text-xs mr-1 mb-1">${x}</span>`
    ).join("");
    el.innerHTML =
      `<div class="text-green-600 font-medium mb-2">✓ Connected — ${r.resource_count} resources available</div>
       <div>${chips}</div>`;
  } catch (e) {
    el.innerHTML = `<div class="text-red-600 font-medium">✗ ${e.message}</div>`;
  }
});

// ------------------------------- upload ------------------------------- //
$("#fileInput").addEventListener("change", async (ev) => {
  const file = ev.target.files[0];
  if (!file) return;
  const fd = new FormData();
  fd.append("file", file);
  const res = await fetch("/api/upload", { method: "POST", body: fd });
  const data = await res.json();
  if (!res.ok) { $("#uploadInfo").textContent = data.detail || "Upload failed"; return; }
  S.token = data.token;
  $("#uploadInfo").textContent = `Loaded ${data.filename}`;
  $("#sheetSelect").innerHTML = data.sheets.map((s) => `<option>${s}</option>`).join("");
  loadPreview();
});

$("#sheetSelect").addEventListener("change", loadPreview);

async function loadPreview() {
  if (!S.token) return;
  $("#previewWrap").innerHTML = `<p class="text-sm text-slate-500">Loading preview…</p>`;
  let data;
  try {
    const sheet = encodeURIComponent($("#sheetSelect").value);
    data = await api(`/api/upload/${S.token}/preview?sheet=${sheet}`);
  } catch (e) {
    $("#previewWrap").innerHTML =
      `<p class="text-sm text-red-600">Could not preview this file: ${escapeHtml(e.message)}</p>`;
    return;
  }
  if (!data.rows || !data.rows.length) {
    $("#previewWrap").innerHTML =
      `<p class="text-sm text-amber-600">The file was read but appears to have no rows.</p>`;
    return;
  }
  const rows = data.rows.map((r, i) =>
    `<tr class="${i === 0 ? "bg-amber-50 font-medium" : ""}">
       <td class="px-2 py-1 text-slate-400">${i + 1}</td>
       ${r.map((c) => `<td class="px-2 py-1 border border-slate-100">${escapeHtml(c)}</td>`).join("")}
     </tr>`).join("");
  $("#previewWrap").innerHTML =
    `<p class="text-xs text-slate-500 mb-1">${data.n_rows_total} rows total. Highlighted row = current header guess.</p>
     <table class="text-xs border-collapse">${rows}</table>`;
}

$("#btnParse").addEventListener("click", async () => {
  if (!S.token) return;
  const payload = {
    sheet: $("#sheetSelect").value,
    header_row: (parseInt($("#headerRow").value, 10) || 1) - 1,
  };
  const data = await api(`/api/upload/${S.token}/parse`, {
    method: "POST", body: JSON.stringify(payload),
  });
  S.headers = data.headers;
  $("#uploadInfo").textContent = `Parsed ${data.row_count} rows, ${data.headers.length} columns.`;
  activateTab("mapping");
});

// ------------------------------ mapping ------------------------------- //
$("#btnAutomatch").addEventListener("click", async () => {
  if (!S.token || !S.headers.length) { $("#mapWrap").textContent = "Upload & parse a file first."; return; }
  $("#mapWrap").textContent = "Matching…";
  try {
    const data = await api(`/api/mapping/${S.token}/automatch?import_type=${S.importType}`, { method: "POST", body: "{}" });
    S.psFields = data.ps_fields;
    S.matches = data.matches;
    renderMatches();
  } catch (e) { $("#mapWrap").innerHTML = `<span class="text-red-600">${e.message}</span>`; }
});

function badgeColor(b) {
  return b === "green" ? "bg-green-100 text-green-700"
    : b === "amber" ? "bg-amber-100 text-amber-700"
    : "bg-red-100 text-red-700";
}

function renderMatches() {
  const opts = (sel) => S.psFields.map((f) =>
    `<option value="${f}" ${f === sel ? "selected" : ""}>${f}</option>`).join("");
  const rows = S.matches.map((m, i) => `
    <tr class="border-b border-slate-100">
      <td class="px-2 py-1 font-medium">${escapeHtml(m.header)}</td>
      <td class="px-2 py-1">
        <select data-idx="${i}" class="mapSel rounded border border-slate-300 px-2 py-1 text-xs">
          <option value="">— skip —</option>${opts(m.field)}
        </select>
      </td>
      <td class="px-2 py-1">
        <span class="px-2 py-0.5 rounded text-xs ${badgeColor(m.badge)}">
          ${m.field ? Math.round(m.confidence * 100) + "% " + m.method : "unmapped"}
        </span>
      </td>
    </tr>`).join("");
  $("#mapWrap").innerHTML = `
    <table class="w-full text-sm">
      <thead><tr class="text-left text-xs text-slate-500">
        <th class="px-2 py-1">Spreadsheet column</th>
        <th class="px-2 py-1">PrestaShop field</th>
        <th class="px-2 py-1">Confidence</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
    <button id="btnConfirmMap" class="mt-3 bg-slate-900 text-white text-sm px-4 py-2 rounded">Confirm mapping</button>`;
  $$(".mapSel").forEach((sel) => sel.addEventListener("change", (e) => {
    S.matches[+e.target.dataset.idx].field = e.target.value || null;
  }));
  $("#btnConfirmMap").addEventListener("click", confirmMapping);
}

async function confirmMapping() {
  const column_map = {};
  S.matches.forEach((m) => { if (m.field) column_map[m.field] = m.header; });
  await api(`/api/mapping/${S.token}/confirm`, {
    method: "POST", body: JSON.stringify({ column_map, constants: {} }),
  });
  activateTab("import");
}

// profiles
async function loadProfileList() {
  try {
    const data = await api("/api/mapping/profiles");
    $("#profileSelect").innerHTML =
      `<option value="">— profiles —</option>` +
      data.profiles.map((p) => `<option>${p.name}</option>`).join("");
  } catch {}
}
$("#btnSaveProfile").addEventListener("click", async () => {
  const name = $("#profileName").value.trim();
  if (!name) return;
  const column_map = {};
  S.matches.forEach((m) => { if (m.field) column_map[m.field] = m.header; });
  await api("/api/mapping/profiles", {
    method: "POST",
    body: JSON.stringify({ name, column_map, constants: {} }),
  });
  loadProfileList();
});
$("#btnLoadProfile").addEventListener("click", async () => {
  const name = $("#profileSelect").value;
  if (!name) return;
  const p = await api(`/api/mapping/profiles/${encodeURIComponent(name)}`);
  S.matches.forEach((m) => {
    const field = Object.keys(p.column_map || {}).find((f) => p.column_map[f] === m.header);
    m.field = field || null;
  });
  renderMatches();
});

// ------------------------------- import ------------------------------- //
$("#btnValidate").addEventListener("click", async () => {
  if (!S.token) return;
  const data = await api(`/api/import/${S.token}/validate`, { method: "POST", body: "{}" });
  const rows = data.issues.map((i) => `
    <tr class="${i.severity === "error" ? "text-red-600" : "text-amber-600"}">
      <td class="px-2 py-1">${i.row === null ? "—" : i.row + 1}</td>
      <td class="px-2 py-1">${i.field || ""}</td>
      <td class="px-2 py-1">${escapeHtml(i.message)}</td>
    </tr>`).join("");
  $("#validateWrap").innerHTML = `
    <p class="my-2 ${data.blocking ? "text-red-600" : "text-green-600"}">
      ${data.error_count} errors, ${data.warning_count} warnings.
      ${data.blocking ? "Import blocked." : "OK to import."}</p>
    ${rows ? `<table class="w-full text-xs"><thead><tr class="text-left text-slate-500">
      <th class="px-2">Row</th><th class="px-2">Field</th><th class="px-2">Message</th></tr></thead>
      <tbody>${rows}</tbody></table>` : ""}`;
});

$("#btnRun").addEventListener("click", () => {
  if (!S.token) return;
  const scope = $$(".scope:checked").map((c) => c.value);
  const payload = {
    mode: $("#mode").value,
    dry_run: $("#dryRun").checked,
    concurrency: parseInt($("#concurrency").value, 10) || 2,
    scope,
    create_missing: $("#createMissing").checked,
    price_includes_tax: $("#priceInclTax").checked,
    tax_rate: parseFloat($("#taxRate").value) || 0,
    import_type: S.importType,
  };
  runImport(payload);
});

async function runImport(payload) {
  $("#progressWrap").classList.remove("hidden");
  $("#resultWrap").innerHTML = "";
  const results = [];
  let total = 0, done = 0;

  const res = await fetch(`/api/import/${S.token}/run`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    $("#resultWrap").innerHTML = `<span class="text-red-600">${err.detail || "Run failed"}</span>`;
    return;
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done: fin, value } = await reader.read();
    if (fin) break;
    buffer += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buffer.indexOf("\n\n")) >= 0) {
      const chunk = buffer.slice(0, idx).trim();
      buffer = buffer.slice(idx + 2);
      if (!chunk.startsWith("data:")) continue;
      const ev = JSON.parse(chunk.slice(5).trim());
      if (ev.type === "start") { total = ev.total; }
      else if (ev.type === "row") {
        done++;
        results.push(ev);
        $("#bar").style.width = total ? `${Math.round((done / total) * 100)}%` : "100%";
        $("#progressText").textContent = `${done}/${total} — row ${ev.row + 1}: ${ev.action}`;
      } else if (ev.type === "done") {
        $("#progressText").textContent = `Done — ${ev.succeeded} ok, ${ev.failed} failed (run #${ev.run_id})`;
        renderResults(results, ev.run_id);
      }
    }
  }
}

function renderResults(results, runId) {
  const rows = results.sort((a, b) => a.row - b.row).map((r) => `
    <tr class="${r.success ? "" : "text-red-600"}">
      <td class="px-2 py-1">${r.row + 1}</td>
      <td class="px-2 py-1">${escapeHtml(r.reference || "")}</td>
      <td class="px-2 py-1">${r.action}</td>
      <td class="px-2 py-1">${escapeHtml(r.message || "")}</td>
      ${r.payload ? `<td class="px-2 py-1"><details><summary class="cursor-pointer text-slate-400">xml</summary><pre class="text-[10px] whitespace-pre-wrap max-w-md">${escapeHtml(r.payload)}</pre></details></td>` : "<td></td>"}
    </tr>`).join("");
  $("#resultWrap").innerHTML = `
    <div class="flex justify-between items-center my-2">
      <span class="text-slate-500 text-xs">${results.length} rows</span>
      <a href="/api/import/history/${runId}/csv" class="text-xs text-blue-600 underline">Download CSV</a>
    </div>
    <table class="w-full text-xs"><thead><tr class="text-left text-slate-500">
      <th class="px-2">Row</th><th class="px-2">Reference</th><th class="px-2">Action</th>
      <th class="px-2">Message</th><th class="px-2">Payload</th></tr></thead>
      <tbody>${rows}</tbody></table>`;
}

// ------------------------------ history ------------------------------- //
async function loadHistory() {
  const data = await api("/api/import/history");
  if (!data.runs.length) { $("#historyWrap").textContent = "No runs yet."; return; }
  const rows = data.runs.map((r) => `
    <tr class="border-b border-slate-100">
      <td class="px-2 py-1">${r.id}</td>
      <td class="px-2 py-1">${r.started_at?.slice(0, 19).replace("T", " ")}</td>
      <td class="px-2 py-1">${r.mode}</td>
      <td class="px-2 py-1">${r.dry_run ? "dry" : "live"}</td>
      <td class="px-2 py-1">${r.total}</td>
      <td class="px-2 py-1 text-green-600">${r.succeeded}</td>
      <td class="px-2 py-1 text-red-600">${r.failed}</td>
      <td class="px-2 py-1"><a class="text-blue-600 underline" href="/api/import/history/${r.id}/csv">csv</a></td>
    </tr>`).join("");
  $("#historyWrap").innerHTML = `
    <table class="w-full text-sm"><thead><tr class="text-left text-xs text-slate-500">
      <th class="px-2">#</th><th class="px-2">Started</th><th class="px-2">Mode</th>
      <th class="px-2">Type</th><th class="px-2">Total</th><th class="px-2">OK</th>
      <th class="px-2">Fail</th><th class="px-2">CSV</th></tr></thead>
      <tbody>${rows}</tbody></table>`;
}

// ------------------------------- utils -------------------------------- //
function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

activateTab("settings");
loadSettings();
