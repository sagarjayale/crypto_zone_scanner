const statusEl = document.getElementById("status");
const $1 = (id) => document.getElementById(id);
let busy = false;
const rowsEl = document.getElementById("rows");
let names = {};

function say(text, isError = false) { statusEl.textContent = text; statusEl.className = "status" + (isError ? " err" : ""); }

async function loadTable() {
  const [status, settings] = await Promise.all([api("/api/data/status"), api("/api/settings")]);
  names = settings.symbols || {};
  const w = status.weight || {}, wbox = $1("weight");
  wbox.replaceChildren();
  for (const [k, v] of [["Weight used this minute", `${w.used_1m ?? 0} of ${w.budget_1m ?? ""}`], ["Binance limit per minute", w.limit_1m ?? ""],
    ["Share the app keeps to", w.share == null ? "" : `${Math.round(w.share * 100)}%`], ["Last 429 (too many requests)", w.last_429 ? stamp(w.last_429) : "none"],
    ["IP ban", w.banned ? `until ${stamp(w.banned_until)}` : "none"], ["Times shown in", DISPLAY_TZ]])
    wbox.append(el("div", {}, el("dt", {}, k), el("dd", { class: k === "IP ban" && w.banned ? "loss" : "" }, String(v))));
  const work = status.status || {}, keys = Object.keys(work);
  const WORK = { queued: "Waiting to start", downloading: "Downloading", scanning: "Scanning", ready: "Ready", failed: "Failed" };
  const nReady = keys.filter((k) => work[k].ready).length, moving = keys.filter((k) => ["queued", "downloading", "scanning"].includes(work[k].state) && !work[k].ready);
  $1("ready-line").textContent = keys.length ? `${nReady} of ${keys.length} coins ready${moving.length ? `. In progress: ${moving.slice(0, 10).map((k) => `${names[k] || k} (${WORK[work[k].state].toLowerCase()})`).join(", ")}` : ""}.` : "";
  busy = keys.some((k) => ["downloading", "scanning"].includes(work[k].state) || (work[k].state === "queued" && !work[k].ready));
  rowsEl.replaceChildren();
  const order = (r) => TIMEFRAMES.indexOf(r.timeframe);
  const rows = status.rows.sort((a, b) => a.instrument_key.localeCompare(b.instrument_key) || order(a) - order(b));
  const stored = new Set(rows.map((r) => r.instrument_key));
  for (const k of keys.filter((k) => !stored.has(k)))      // in the list, nothing stored yet
    rowsEl.append(el("tr", {}, el("td", {}, names[k] || k, " ", el("span", { class: "sub" }, WORK[work[k].state] || work[k].state)), el("td", { colspan: "8", class: "muted" }, work[k].detail || "No candles stored yet")));
  if (!rows.length && !keys.length) {
    rowsEl.append(el("tr", {}, el("td", { colspan: "9" }, "No candles stored yet. On the first start the app downloads the coin list and the top coins by volume by itself; otherwise choose Download and scan all.")));
    return;
  }
  let last = null;
  for (const r of rows) {
    const first = r.instrument_key !== last;
    last = r.instrument_key;
        rowsEl.append(el("tr", {},
      el("td", {}, first ? (names[r.instrument_key] || r.instrument_key) : "", first ? " " : "", first && work[r.instrument_key] ? el("span", { class: "sub" + (work[r.instrument_key].state === "failed" ? " loss" : "") },
        WORK[work[r.instrument_key].state] + (work[r.instrument_key].state === "failed" && work[r.instrument_key].detail ? ": " + work[r.instrument_key].detail : "")) : ""),
      el("td", {}, TF_LABEL[r.timeframe] || r.timeframe),
      el("td", { class: "num" }, Number(r.candles).toLocaleString("en-US")),
      el("td", {}, stamp(r.first_candle_ts)),
      el("td", {}, stamp(r.last_candle_ts)),
      el("td", {}, stamp(r.last_scanned_ts)),
      el("td", {}, stamp(r.last_sync_at)),
      el("td", { class: "num" + (r.gaps ? " warn" : "") }, r.gaps ? String(r.gaps) : ""),
      el("td", {}, first ? el("button", { class: "btn", onclick: () => syncOne(r.instrument_key) }, "Sync") : "")));
  }
}

async function follow(jobId, label) {
  for (;;) {
    const job = await api(`/api/jobs/${jobId}`);
    if (job.status === "running") { say(`${label}: ${job.progress || "starting"}`); await new Promise((r) => setTimeout(r, 1500)); continue; }
    if (job.status === "failed") say(`${label} failed. ${job.error}`, true); else say(`${label} finished. ${job.progress || ""}`);
    await loadTable();
    return;
  }
}

async function run(label, path, body) {
  try { say(`${label}: starting`); const r = await post(path, body); await follow(r.job_id, label); }
  catch (e) { say(`${label} failed. ${e.message}`, true); }
}

async function syncOne(key) {
  try { say(`Sync ${names[key] || key}: running`); await post(`/api/data/sync/${encodeURIComponent(key)}`); say(`Sync ${names[key] || key} finished.`); await loadTable(); }
  catch (e) { say(`Sync failed. ${e.message}`, true); }
}

document.getElementById("sync-all").onclick = () => run("Download and scan", "/api/data/sync", { symbols: "all" });
document.getElementById("repair").onclick = () => run("Repair gaps", "/api/data/sync", { symbols: "all", repair: true });
document.getElementById("scan-all").onclick = () => run("Scan all", "/api/scan/run");
document.getElementById("instruments").onclick = () => run("Refresh coins", "/api/instruments/refresh");
document.getElementById("backfill").onclick = () => {
  if (confirm("Download the full history again for every coin? A coin listed years ago needs a few hundred requests; the app spreads them over several minutes to stay inside Binance's limit.")) run("Full download", "/api/data/backfill", { symbols: "all" });
};

showToken(document.getElementById("token"));
loadTable().catch((e) => say(`Could not load the table. ${e.message}`, true));
setInterval(() => { if (busy) loadTable().catch(() => {}); else showToken(document.getElementById("token")); }, 4000);      // keep the page moving while coins are in progress
