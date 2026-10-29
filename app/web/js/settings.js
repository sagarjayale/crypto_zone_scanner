// Settings page: coin list, zone rules, costs, paper trading, time zone, Telegram, jobs and the rejection log.
const $ = (id) => document.getElementById(id);
let S = {};
function say(text, isError = false) { $("status").textContent = text; $("status").className = "status" + (isError ? " err" : ""); window.scrollTo({ top: 0 }); }
const put = (body) => api("/api/settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
const RULES = [["base_body_pct", "Base candle: body below, % of range", 1], ["leg_body_pct", "Explosive candle: body above, % of range", 1],
  ["max_base", "Most base candles", 1], ["gap_exciting_pct", "Gap candle counts as exciting (PDF marking only): opens beyond the previous high or low by at least, % of price (0 = off)", 0.05],
  ["max_wick_tests", "Tests allowed on an execution zone (1: a second test makes it unusable)", 1], ["min_score", "Smallest trade score shown (7 is entry type 1 only)", 0.5],
  ["min_htf_score", "Smallest score for an HTF location zone", 0.5], ["ema_period", "EMA for the extra point", 1], ["ema_period_2", "Second EMA for the extra point (PDF marking only)", 1],
  ["approach_pct", "Approaching: within, % of the proximal line", 0.1], ["ltf_max_gap_pct", "Ignore execution zones further than, % of price", 1],
  ["htf_buffer_pct", "Allow execution zones beyond the HTF distal by, % of its width", 1],
  ["contact_touch_pct", "HTF contact: touching within, % of price", 0.05], ["contact_near_pct", "HTF contact: near within, % of price", 0.1],
  ["target_rr", "Target, times the distance from entry to stop loss", 0.5]];
const HD = ["1m", "5m", "15m", "30m", "1h"];
const MISC_NUM = ["fee_maker_pct", "fee_taker_pct", "paper_capital", "paper_risk_pct", "paper_leverage", "paper_mmr_pct", "paper_slippage_pct",
  "confirm_volume_mult", "confirm_funding_pct"], MISC_BOOL = ["fee_bnb", "include_funding", "all_ltf_zones"];

function renderUniverse() {
  const box = $("universe");
  box.replaceChildren();
  if (!(S.universe || []).length) box.append(el("span", { class: "empty" }, "The list is empty. Find a coin below, or show the top coins by volume, and add them."));
  for (const key of S.universe || []) {
    box.append(el("span", { class: "chip" }, S.symbols[key] || key, el("button", { "aria-label": `Remove ${S.symbols[key] || key}`, onclick: () => saveUniverse(S.universe.filter((k) => k !== key), `${S.symbols[key] || key} removed.`) }, "\u00d7")));
  }
}
async function saveUniverse(list, done) {
  try { S = await put({ universe: list }); renderUniverse(); say(S.sync_job_id ? `${done} It is being downloaded and scanned now; see Recent jobs.` : done); loadJobs(); }
  catch (e) { say(e.message, true); }
}
async function find() {
  const box = $("found"), text = $("find").value.trim();
  box.replaceChildren();
  if (!text.length) return box.append(el("span", { class: "empty" }, "Type part of a symbol, such as SOL."));
  try {
    const rows = await api(`/api/instruments/search?q=${encodeURIComponent(text)}`);
    if (!rows.length) return box.append(el("span", { class: "empty" }, "Nothing found. If the coin list was never downloaded, use Refresh coins on the Data page."));
    for (const r of rows) {
      if ((S.universe || []).includes(r.instrument_key)) continue;
      box.append(el("button", { class: "chip add", title: volText(r), onclick: () => { saveUniverse([...(S.universe || []), r.instrument_key], `${r.symbol} added.`); box.replaceChildren(); } }, `Add ${r.symbol}`));
    }
  } catch (e) { say(e.message, true); }
}
const volText = (r) => (r.quote_volume == null ? "" : `24 h volume ${(r.quote_volume / 1e6).toLocaleString("en-US", { maximumFractionDigits: 0 })} million USDT`);

// ---- add many at once: the top coins by volume, or pasted symbols
function showPicker(res, title) {
  const list = $("picker-list"), missing = res.rows.filter((r) => r.found === false), already = res.rows.filter((r) => r.in_list).length;
  $("picker").hidden = false;
  list.replaceChildren();
  for (const r of res.rows) {
    const found = r.found !== false;
    const box = el("input", { type: "checkbox", value: r.instrument_key || "" });
    box.checked = found && !r.in_list; box.disabled = !found;
    list.append(el("label", { class: "check" }, box, r.symbol || r.query, el("span", { class: "hint" }, r.in_list ? "already in your list" : found ? volText(r) : "not a trading USDT perpetual on Binance")));
  }
  const parts = [`${title}: ${res.count} ${res.count === 1 ? "coin" : "coins"}`];
  if (already) parts.push(`${already} already in your list`);
  if (missing.length) parts.push(`${missing.length} not found (${missing.map((r) => r.query).join(", ")})`);
  $("picker-note").textContent = parts.join(", ") + ". A new coin is downloaded from its listing date; the app spreads the requests to stay inside Binance's limit, so a long list takes a while the first time.";
  countPicked();
}
function picked() { return [...$("picker-list").querySelectorAll("input:checked:not(:disabled)")].map((i) => i.value).filter(Boolean); }
function countPicked() { const n = picked().length; $("pick-add").textContent = n ? `Add selected (${n})` : "Add selected"; $("pick-add").disabled = !n; $("pick-replace").disabled = !n; }
function setAll(on) { for (const i of $("picker-list").querySelectorAll("input:not(:disabled)")) i.checked = on; countPicked(); }
async function loadTop() {
  try { const n = Number($("top_n").value) || 30; showPicker(await api(`/api/instruments/top?n=${n}`), `Top ${n} by 24 hour volume`); $("status").textContent = ""; }
  catch (e) { $("picker").hidden = true; say(e.message, true); }
}
async function matchPasted() {
  try { showPicker(await post("/api/instruments/match", { text: $("paste").value }), "Pasted symbols"); $("status").textContent = ""; }
  catch (e) { $("picker").hidden = true; say(e.message, true); }
}
async function addPicked(replace = false) {
  const keys = picked();
  if (!keys.length) return;
  if (replace && !confirm(`Make your coin list exactly these ${keys.length} coins? Coins taken out keep their stored candles.`)) return;
  $("picker").hidden = true;
  const list = replace ? keys : [...(S.universe || []), ...keys.filter((k) => !(S.universe || []).includes(k))];
  await saveUniverse(list, `${keys.length} ${keys.length === 1 ? "coin" : "coins"} ${replace ? "now make up the list" : "added"}.`);
}

function renderRules() {
  const scope = $("th-scope").value, over = scope ? (S[`thresholds:${scope}`] || {}) : null, form = $("th-form");
  form.replaceChildren();
  for (const [key, label, step] of RULES) {
    const input = el("input", { type: "number", id: "th-" + key, step: String(step), min: "0" });
    if (over) { if (over[key] != null) input.value = over[key]; input.placeholder = `${S[key]} (all timeframes)`; } else input.value = S[key];
    form.append(el("div", {}, el("label", { class: "lbl", for: "th-" + key }, label), input));
  }
  if (over) form.append(el("p", { class: "hint" }, "Leave a field empty to use the value for all timeframes."));
}
async function saveRules() {
  const scope = $("th-scope").value, out = {};
  for (const [key] of RULES) { const v = $("th-" + key).value; if (v !== "") out[key] = Number(v); }
  try { S = await put(scope ? { [`thresholds:${scope}`]: out } : out); renderRules(); say("Zone rules saved. They apply to zones found from now on; use Rebuild all zones to apply them to history."); }
  catch (e) { say(e.message, true); }
}

async function loadJobs() {
  const body = $("jobs"); body.replaceChildren();
  const jobs = await api("/api/jobs");
  if (!jobs.length) body.append(el("tr", {}, el("td", { colspan: "5" }, "No jobs have run yet.")));
  for (const j of jobs) body.append(el("tr", {}, el("td", {}, j.type), el("td", {}, stamp(j.started_at)), el("td", {}, stamp(j.ended_at)),
    el("td", { class: j.status === "failed" ? "loss" : "" }, j.status), el("td", { style: "white-space:normal" }, j.error || j.progress || "")));
}
async function loadRejections() {
  const body = $("rejections"); body.replaceChildren();
  const rows = await api("/api/rejections?limit=50");
  if (!rows.length) body.append(el("tr", {}, el("td", { colspan: "4" }, "No rejections logged in the last 90 days.")));
  for (const r of rows) body.append(el("tr", {}, el("td", {}, (S.symbols || {})[r.instrument_key] || r.instrument_key), el("td", {}, TF_LABEL[r.timeframe]), el("td", {}, stamp(r.ts)), el("td", {}, r.reason)));
}
async function follow(jobId, label) {
  for (;;) { const j = await api(`/api/jobs/${jobId}`); if (j.status === "running") { say(`${label}: ${j.progress || "starting"}`); await new Promise((r) => setTimeout(r, 1500)); continue; }
    say(j.status === "failed" ? `${label} failed. ${j.error}` : `${label} finished. ${j.progress || ""}`, j.status === "failed"); loadJobs(); return; }
}

function renderModes() {
  const box = $("modes"), on = S.modes || Object.keys(MODES);
  box.replaceChildren();
  for (const m of Object.keys(MODES)) box.append(el("label", { class: "check" }, Object.assign(el("input", { type: "checkbox", value: m }), { checked: on.includes(m) }), modeText(m)));
}

function fill() {
  renderUniverse(); renderRules(); renderModes();
  for (const k of MISC_NUM) $(k).value = S[k];
  for (const k of MISC_BOOL) $(k).checked = Boolean(S[k]);
  $("direction_rule").value = S.direction_rule || "location";
  $("itf_rule").value = S.itf_rule === "pct" ? "pct" : "atr"; $("itf_atr_mult").value = S.itf_atr_mult; $("itf_slope_pct").value = S.itf_slope_pct;
  $("top_n").value = S.top_n || 50; $("universe_auto").checked = Boolean(S.universe_auto);
  for (const tf of HD) $("hd-" + tf).value = (S.history_days || {})[tf] ?? ""; $("display_tz").value = S.display_tz || "Asia/Riyadh"; setDisplayTz(S.display_tz);
  $("entry_23").value = S.entry_23 === "TYPE_2" ? "TYPE_2" : "TYPE_3";
  $("min_stop_pct").value = S.min_stop_pct ?? 0; $("min_bases").value = S.min_bases ?? 0;
  $("max_bases").value = S.max_bases ?? 0; $("skip_reaction_zones").checked = Boolean(S.skip_reaction_zones); $("check_next_htf").checked = Boolean(S.check_next_htf);
  $("trail_method").value = S.trail_method || "off"; $("trail_start_r").value = S.trail_start_r ?? 1; $("trail_tf").value = S.trail_tf || "ltf";
  $("trail_atr_mult").value = S.trail_atr_mult ?? 2; $("trail_swing").value = S.trail_swing ?? 2; $("trail_keep_target").checked = S.trail_keep_target !== false;
  $("trail_lock_pct").value = S.trail_lock_pct ?? 10; $("trail_lock_gap_pct").value = S.trail_lock_gap_pct ?? 20;
  $("trail_lock_target").value = S.trail_lock_target === "close" ? "close" : "move"; lockExample();
  $("marking_rule").value = S.marking_rule === "pdf" ? "pdf" : "scanner";
  $("zone_history").value = S.zone_history === "all" ? "all" : "scanner";
  $("htf_zone_pick").value = S.htf_zone_pick === "any" ? "any" : "fresh";
  $("telegram_chat_id").value = S.telegram_chat_id || ""; $("telegram_alerts").checked = Boolean(S.telegram_alerts);
  $("tg-state").textContent = S.telegram_configured ? "A bot token and chat id are stored. Alerts and paper fills are sent to that chat." : "Not set up yet. Create a bot with BotFather in Telegram, then enter its token and your chat id.";
}

$("top-load").onclick = loadTop;
$("auto-save").onclick = async () => {
  try { S = await put({ universe_auto: $("universe_auto").checked, top_n: Number($("top_n").value) || 50 }); fill(); loadJobs();
    say(S.universe_auto ? `Your list is now the top ${S.top_n} by volume, checked every day. New coins are downloaded in the background.` : "Your list stays as it is; it no longer follows the top coins."); }
  catch (e) { say(e.message, true); }
}; $("paste-go").onclick = matchPasted; $("pick-replace").onclick = () => addPicked(true); $("paste").onkeydown = (e) => { if (e.key === "Enter") matchPasted(); };
$("pick-all").onclick = () => setAll(true); $("pick-none").onclick = () => setAll(false); $("pick-add").onclick = () => addPicked(false);
$("picker-list").onchange = countPicked;
$("find-go").onclick = find; $("find").onkeydown = (e) => { if (e.key === "Enter") find(); };
$("th-scope").onchange = renderRules; $("th-save").onclick = saveRules;
$("rebuild").onclick = async () => { if (confirm("Clear every zone and plan and scan all stored candles again with the current rules? Candles and paper trades are kept.")) { try { follow((await post("/api/rebuild/zones")).job_id, "Rebuild"); } catch (e) { say(e.message, true); } } };
$("modes-save").onclick = async () => {
  const on = [...$("modes").querySelectorAll("input:checked")].map((i) => i.value);
  if (!on.length) return say("Keep at least one mode switched on.", true);
  try { S = await put({ modes: on }); fill(); say("Modes saved. They apply from the next scan."); } catch (e) { say(e.message, true); }
};
$("rule-save").onclick = async () => {
  try {
    S = await put({ direction_rule: $("direction_rule").value, itf_rule: $("itf_rule").value, itf_atr_mult: Number($("itf_atr_mult").value), itf_slope_pct: Number($("itf_slope_pct").value) }); fill();
    await post("/api/scan/run", { symbols: "all" });       // plans and results are rebuilt under the rule just saved
    say("Rules saved. Every coin is being rescanned now; see Recent jobs."); loadJobs();
  } catch (e) { say(e.message, true); }
};
$("marking-save").onclick = async () => {
  if (!confirm("Save the zone marking and history, and find every zone again with them? Candles and paper trades are kept; pending paper orders are cancelled if their zone is gone.")) return;
  try {
    S = await put({ marking_rule: $("marking_rule").value, zone_history: $("zone_history").value, htf_zone_pick: $("htf_zone_pick").value }); fill();
    follow((await post("/api/rebuild/zones")).job_id, "Rebuild");
  } catch (e) { say(e.message, true); }
};
$("entry-save").onclick = async () => {
  try {
    S = await put({ entry_23: $("entry_23").value, min_stop_pct: Number($("min_stop_pct").value) || 0, min_bases: Math.round(Number($("min_bases").value) || 0),
      max_bases: Math.round(Number($("max_bases").value) || 0), skip_reaction_zones: $("skip_reaction_zones").checked, check_next_htf: $("check_next_htf").checked }); fill();
    await post("/api/scan/run", { symbols: "all" });       // plans and results are rebuilt with the entry just chosen
    say("Entry saved. Every coin is being rescanned now; see Recent jobs."); loadJobs();
  } catch (e) { say(e.message, true); }
};
$("misc-save").onclick = async () => { const out = {}; for (const k of MISC_NUM) out[k] = Number($(k).value); for (const k of MISC_BOOL) out[k] = $(k).checked;
  out.display_tz = $("display_tz").value.trim() || "UTC"; out.top_n = Number($("top_n").value) || 50;
  out.history_days = Object.fromEntries(HD.map((tf) => [tf, Number($("hd-" + tf).value)]).filter(([, v]) => v > 0));
  try { S = await put(out); fill(); say("Settings saved. Times are now shown in " + S.display_tz + "."); } catch (e) { say(e.message, true); } };
$("tg-save").onclick = async () => { const out = { telegram_chat_id: $("telegram_chat_id").value.trim(), telegram_alerts: $("telegram_alerts").checked }; if ($("telegram_token").value) out.telegram_token = $("telegram_token").value.trim();
  try { S = await put(out); $("telegram_token").value = ""; fill(); say("Telegram settings saved."); } catch (e) { say(e.message, true); } };
$("tg-test").onclick = async () => { try { await post("/api/alerts/test"); say("Test alert sent. Check your Telegram chat."); } catch (e) { say(e.message, true); } };
$("reconnect").onclick = async () => { try { await post("/api/feed/reconnect"); say("Live prices restarted."); } catch (e) { say(e.message, true); } };

(async function init() {
  showToken($("token"));
  for (const tf of TIMEFRAMES) $("th-scope").append(el("option", { value: tf }, `${TF_LABEL[tf]} only`));
  try {
    S = await api("/api/settings"); fill();
    const d = await api("/api/data/status"), w = d.weight || {};
    $("calls").textContent = `Binance request weight used this minute: ${w.used_1m} of the ${w.budget_1m} the app keeps to (Binance allows ${w.limit_1m} per minute)${w.banned ? `. Banned until ${stamp(w.banned_until)}` : ""}.`;
    await loadJobs(); await loadRejections();
  } catch (e) { say(`Could not load settings. ${e.message}`, true); }
})();

// The lock settings worked through on entry 100, stop 90, target 200, so the numbers can be checked before saving.
function lockExample() {
  const box = $("trail-example");
  if ($("trail_method").value !== "lock") { box.textContent = ""; return; }
  const pct = Number($("trail_lock_pct").value) || 10, gap = (Number($("trail_lock_gap_pct").value) || 0) / 100;
  const step = 100 * pct / 100, r = (n) => Math.round(n * 100) / 100;
  const moves = $("trail_lock_target").value === "move";
  box.textContent = `Example: long at 100, stop 90, target 200. One step is ${r(step)}. ` +
    `At ${r(100 + step * (1 + gap))} the stop moves to ${r(100 + step)}, at ${r(100 + step * (2 + gap))} to ${r(100 + 2 * step)}, and so on. ` +
    (moves ? `At 200 the target moves to ${r(200 + step)}; the trade ends only when price comes back to the locked stop.` : "At 200 the trade closes at the target.");
}
for (const id of ["trail_method", "trail_lock_pct", "trail_lock_gap_pct", "trail_lock_target"]) $(id).addEventListener("input", lockExample);

$("trail-save").onclick = async () => {
  try {
    S = await put({ trail_method: $("trail_method").value, trail_start_r: Number($("trail_start_r").value) || 1, trail_tf: $("trail_tf").value,
      trail_atr_mult: Number($("trail_atr_mult").value) || 2, trail_swing: Math.round(Number($("trail_swing").value) || 2), trail_keep_target: $("trail_keep_target").checked,
      trail_lock_pct: Number($("trail_lock_pct").value) || 10, trail_lock_gap_pct: Number($("trail_lock_gap_pct").value) || 0,
      trail_lock_target: $("trail_lock_target").value });
    fill();
    say(S.trail_method === "off" ? "Trailing stop off: trades keep their first stop and target." : `Trailing stop saved (${S.trail_method}). It applies to paper positions filled from now on.`);
  } catch (e) { say(e.message, true); }
};
