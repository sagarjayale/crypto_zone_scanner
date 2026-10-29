// Dashboard: one full GTF result per ready coin for the chosen mode (Monthly by default).
// Coins appear as soon as their own download and scan are done; the rest carry on in the background.
// A buy is a long and a sell is a short: the API keeps the words BUY and SELL, the page says long and short.
const $ = (id) => document.getElementById(id);
const state = { board: null, prices: {}, status: null, open: null, reach: 1, pick: "", ltfMin: 0 };
const ALL_LTF_KEY = "cryptoScanner.otherLtfMin";
try { state.ltfMin = Number(localStorage.getItem(ALL_LTF_KEY)) || 0; } catch (e) { /* storage blocked: off */ }
// The other LTF zones of a row at or above the chosen score (none while the choice is off).
const otherZones = (r) => (state.ltfMin ? (r.other_ltf_zones || []).filter((o) => o.score >= state.ltfMin) : []);
const STATE_TEXT = { TOUCHING_NOW: "Touching now", APPROACHING: "Approaching", JUST_FORMED: "Just formed", ACTIVE: "Active" };
const STATE_CLASS = { TOUCHING_NOW: "in-zone", APPROACHING: "approaching", JUST_FORMED: "fresh", ACTIVE: "fresh" };
const WORK_TEXT = { queued: "waiting to start", downloading: "downloading", scanning: "scanning", failed: "failed", ready: "ready" };
const clock = (s) => (s ? stamp(s).slice(11, 16) : "");
const SIDE_WORD = { BUY: "Long", SELL: "Short" };

function say(text, isError = false) { $("status").textContent = text; $("status").className = "status" + (isError ? " err" : ""); }
const priceOf = (r) => state.prices[r.instrument_key] ?? r.live_price ?? r.cmp;

// The state from the live price: touching when price is at or inside the zone, approaching within the set percentage.
function liveState(r) {
  const z = r.ltf_zone, live = state.prices[r.instrument_key] ?? r.live_price;
  if (!z || live == null) return { key: r.zone_state, note: z ? "from the last closed candle" : "" };
  const away = 100 * (z.side === "demand" ? live - z.proximal : z.proximal - live) / z.proximal;
  const beyond = z.side === "demand" ? live < z.distal : live > z.distal;
  if (beyond) return { key: "ACTIVE", note: "price is beyond the zone" };
  if (away <= 0) return { key: "TOUCHING_NOW", note: "price is in the zone" };
  if (away <= state.reach) return { key: "APPROACHING", note: `${away.toFixed(2)}% away` };
  return { key: r.zone_state === "JUST_FORMED" ? "JUST_FORMED" : "ACTIVE", note: `${away.toFixed(1)}% away` };
}

// Under "location beats trend" the ITF is only asked when the price is in equilibrium.
const curveDecides = (r) => (r.direction_rule || "location") === "location" && r.market_state === "NORMAL" && ["VERY LOW", "LOW", "HIGH", "VERY HIGH"].includes(r.curve);

function zoneCell(z, tf) {
  if (!z) return el("td", { class: "muted" }, "None");
  return el("td", { class: "num" }, `${fmtPrice(z.proximal)} to ${fmtPrice(z.distal)}`,
    el("span", { class: "sub" }, `${TF_LABEL[tf] || ""}, score ${z.score} (${LABEL_TEXT[z.label] || z.label})`));
}

function shown() {
  const conf = Number($("f-conf").value);
  const sig = $("f-signal").value, st = $("f-state").value, scores = readScoreFilters(), q = $("f-search").value.trim().toUpperCase();
  const inGroup = groupSet();
  return (state.board ? state.board.rows : []).map((r) => ({ r, live: liveState(r) })).filter(({ r, live }) => {
    if (inGroup && !inGroup.has(r.instrument_key)) return false;
    const s = r.signal.signal;
    if (sig && (sig === "TRADE" ? s === "NO_TRADE" : s !== sig)) return false;
    if (st && (!r.ltf_zone || live.key !== st)) return false;
    if (!passesScores(r, scores)) return false;
    if (conf && !(r.confirmation && r.confirmation.pct != null && r.confirmation.pct >= conf)) return false;
    return !q || r.symbol.toUpperCase().includes(q);
  });
}

// The coins of the chosen group (top 20, top 50 or the whole list, most traded first).
function groupSet() { const b = state.board; return b && b.ranked ? new Set(coinGroup(b.ranked, $("f-group").value)) : null; }

function renderCounts(rows) {
  const trade = rows.filter(({ r }) => r.signal.signal !== "NO_TRADE");
  const n = { "": rows.length, BUY: trade.filter(({ r }) => r.signal.signal === "BUY").length, SELL: trade.filter(({ r }) => r.signal.signal === "SELL").length,
    APPROACHING: trade.filter(({ live }) => live.key === "APPROACHING").length, TOUCHING_NOW: trade.filter(({ live }) => live.key === "TOUCHING_NOW").length };
  const box = $("counts");
  box.replaceChildren();
  for (const [key, label] of [["", "Coins shown"], ["BUY", "Long"], ["SELL", "Short"], ["APPROACHING", "Approaching"], ["TOUCHING_NOW", "Touching now"]]) {
    box.append(el("button", { "aria-pressed": String(state.pick === key), onclick: () => { state.pick = state.pick === key ? "" : key; render(); } },
      el("span", { class: "n" }, String(n[key])), el("span", { class: "l" }, label)));
  }
}

// The confirmation score: checks the zone rules do not make (mode and BTC agreement, HTF contact, volume, strength
// against BTC, funding and open interest).
function confCell(r) {
  const c = r.confirmation;
  if (!c || c.pct == null) return el("td", { class: "muted" }, c ? "Nothing to check" : "");
  return el("td", {}, el("span", { class: "tag conf-" + c.label }, `${c.points} of ${c.max}`), el("span", { class: "sub" }, c.label));
}
function confLines(c) { return c.checks.map((x) => `${x.label}: ${x.applicable ? (x.max > 1 ? x.points + " of " + x.max : x.passed ? "yes" : "no") : "not checked"} (${x.note})`).join(". ") + "."; }

function planLine(p) {
  if (!p) return "not available";
  if (p.entry == null) return `${p.note}. Stop loss ${fmtPrice(p.stoploss)}; the target is ${p.rr} times the distance from the entry to the stop loss.`;
  return `entry ${fmtPrice(p.entry)}, stop loss ${fmtPrice(p.stoploss)}, target ${fmtPrice(p.target)} (${p.rr} to 1). ${p.note}`;
}
// What the entry of this score is waiting for, in a few words.
function entryNote(s) {
  const name = ENTRY_TEXT[s.primary_entry] || "", fees = feesText(s.fees_r) ? `. ${feesText(s.fees_r)} (stop ${s.stop_pct}% away)` : "";
  if (s.primary_entry === "TYPE_1") return `${name}, ${s.waiting ? "waiting for price to reach the zone" : "price has reached the zone"}, ${s.rr} to 1${fees}`;
  return (s.waiting ? `${name}: no entry price yet, waits for its candles` : `${name}, entered, ${s.rr} to 1`) + fees;
}
function scoreLine(z) {
  return z ? `${fmtPrice(z.proximal)} to ${fmtPrice(z.distal)}: score ${z.score} = freshness ${z.freshness} + strength ${z.strength} (${z.strength_label}) + base ${z.base}${z.score > z.freshness + z.strength + z.base ? " + EMA 1" : ""}. ${z.pattern}, ${z.bases} base ${z.bases === 1 ? "candle" : "candles"}, ${testsText(z)}.` : "none near the price";
}

function detailRow(r) {
  const s = r.signal;
  const facts = [["Mode", modeText(r.mode)], ["Market state", lower(r.market_state)], ["HTF trend", lower(r.htf_trend)], ["ITF trend", `${lower(r.itf_trend)} (${r.itf_reason || "no reading"})`],
    ["Side decided by", curveDecides(r) ? `the curve: ${lower(r.curve)} is on the ${["VERY LOW", "LOW"].includes(r.curve) ? "demand side, so long" : "supply side, so short"}` : r.market_state !== "NORMAL" ? "the expansion rule (only one HTF zone is left)" : "the ITF trend"],
    [`${TF_LABEL[r.htf]} demand`, scoreLine(r.demand_zone)], [`${TF_LABEL[r.htf]} supply`, scoreLine(r.supply_zone)],
    [`${TF_LABEL[r.ltf]} entry zone`, r.ltf_zone ? scoreLine(r.ltf_zone) : (s.reason || "none")]];
  if (r.ltf_zone) facts.push(...ENTRY_KEYS.map(([k, label]) => [label + (s[k] && s[k].entry_type === s.primary_entry ? " (taken at this score)" : ""), planLine(s[k])]),
    ["Contact of the entry zone", contactText(r.contact_ltf)], ["Contact of the HTF zone", contactText(r.contact_htf)]);
  if (r.confirmation) facts.push([`Confirmation ${r.confirmation.points} of ${r.confirmation.max}`, confLines(r.confirmation)]);
  if (s.signal === "NO_TRADE") facts.push(["Why no trade", s.reason || "not given"]);
  for (const k of r.skipped_ltf_zones || []) facts.push([`Not traded: ${TF_LABEL[r.ltf]} zone ${fmtPrice(k.proximal)} to ${fmtPrice(k.distal)}`, `${k.why}. Score ${k.score}.`]);
  const dl = el("dl", { class: "kv" });
  for (const [k, v] of facts) dl.append(el("dt", {}, k), el("dd", {}, String(v)));
  return el("tr", { class: "more" }, el("td", { colspan: "12" }, dl));
}

function render() {
  const all = shown();
  renderCounts(all);
  const g = groupSet(), groupRows = state.board ? state.board.rows.filter((r) => !g || g.has(r.instrument_key)) : [];
  const total = groupRows.length, f = readScoreFilters(), set = Object.values(f).some((v) => v > 0);
  const lost = groupRows.filter((r) => !passesScores(r, f)).length;
  $("hidden-line").textContent = set && total ? `Score filters: ${total - lost} of ${total} ready coins pass. ${lost ? lost + " hidden; choose \"any\" or press Clear score filters to see them." : ""}` : "";
  const rank = { TOUCHING_NOW: 0, APPROACHING: 1, JUST_FORMED: 2, ACTIVE: 3 };
  const rows = all.filter(({ r, live }) => !state.pick || r.signal.signal === state.pick || (r.signal.signal !== "NO_TRADE" && live.key === state.pick))
    .sort((a, b) => Number(a.r.signal.signal === "NO_TRADE") - Number(b.r.signal.signal === "NO_TRADE") || (rank[a.live.key] ?? 9) - (rank[b.live.key] ?? 9) || b.r.signal.score - a.r.signal.score || a.r.symbol.localeCompare(b.r.symbol));
  const body = $("rows");
  body.replaceChildren();
  if (!rows.length) {
    const b = state.board;
    const why = !b ? "Loading" : !b.total ? "Your coin list is empty. On the first start it is filled with the top coins by volume; add coins on the Settings page." : !b.ready ? "No coin is ready yet. Each one appears here as soon as its download and scan finish." : "No coin matches these filters. The score filters start at 7 or more; lower them or press Clear score filters.";
    body.append(el("tr", {}, el("td", { colspan: "12" }, why)));
    return;
  }
  const stale = state.status && state.status.feed.stale;
  for (const { r, live } of rows) {
    const s = r.signal, trade = s.signal !== "NO_TRADE", price = priceOf(r);
    const tr = el("tr", { class: "setup", onclick: () => { state.open = state.open === r.instrument_key ? null : r.instrument_key; render(); } },
      el("td", {}, r.symbol, el("span", { class: "sub" + (stale ? " stale" : "") }, price == null ? "no price" : fmtPrice(price))),
      zoneCell(r.demand_zone, r.htf), zoneCell(r.supply_zone, r.htf),
      el("td", {}, lower(r.curve), el("span", { class: "sub" }, r.market_state && r.market_state !== "NORMAL" ? lower(r.market_state) : `HTF ${lower(r.htf_trend)}`)),
      el("td", {}, lower(r.itf_trend), el("span", { class: "sub" }, TF_LABEL[r.itf] + (curveDecides(r) ? ", not used: the curve decides" : ""))),
      moreNote(zoneCell(r.ltf_zone, r.ltf), r),
      el("td", {}, r.ltf_zone ? el("span", { class: "tag " + (STATE_CLASS[live.key] || "fresh") }, STATE_TEXT[live.key] || "") : "", el("span", { class: "sub" }, r.ltf_zone ? live.note : "")),
      el("td", {}, el("span", { class: "tag " + (s.signal === "BUY" ? "demand" : s.signal === "SELL" ? "supply" : "") }, SIDE_WORD[s.signal] || "No trade"),
        el("span", { class: "sub wrap" }, trade ? `${LABEL_TEXT[s.zone_type] || ""}, score ${s.score}` : (s.reason || ""))),
      confCell(r),
      el("td", { class: "num" }, !trade ? "" : s.entry != null ? `${fmtPrice(s.entry)}, ${fmtPrice(s.stoploss)}, ${fmtPrice(s.target)}` : `zone at ${fmtPrice(s.watch)}, stop ${fmtPrice(s.stoploss)}`,
        el("span", { class: "sub wrap" }, trade ? entryNote(s) : "")),
      timeCell(r),
      el("td", {}, el("div", { class: "rowbtns" },
        el("a", { class: "btn", href: `/chart?symbol=${encodeURIComponent(r.instrument_key)}&mode=${r.mode}`, onclick: (e) => e.stopPropagation() }, "Chart"),
        r.can_paper ? el("button", { class: "btn", onclick: (e) => { e.stopPropagation(); paperTrade(r); } }, "Paper trade") : "")));
    body.append(tr);
    for (const o of otherZones(r)) body.append(otherRow(r, o));
    if (state.open === r.instrument_key) body.append(detailRow(r));
  }
}

// When the signal appeared, which candle the result rests on, and when the LTF zone formed. Shown in the display time zone.
function timeCell(r) {
  const at = (x) => stamp(x).replace(/-/g, "\u2011");     // a date never breaks across two lines
  const trade = r.signal.signal !== "NO_TRADE", lines = [];
  if (r.signal_since) {
    lines.push(el("span", { style: "display:block" }, `${trade ? "Signal" : "No trade"} since ${at(r.signal_since)}`));
    lines.push(el("span", { class: "sub" }, ago(r.signal_since)));
  }
  if (r.candle_ts) lines.push(el("span", { class: "sub" }, `${TF_LABEL[r.ltf] || r.ltf} candle closed ${at(r.candle_ts)}`));
  if (r.ltf_zone && r.ltf_zone.created_ts) lines.push(el("span", { class: "sub" }, `Zone formed ${at(r.ltf_zone.created_ts)}`));
  return el("td", { class: "times", title: r.updated_at ? `Checked ${stamp(r.updated_at)}` : "" }, ...(lines.length ? lines : ["waiting for the next scan"]));
}

// "+2 more LTF zones" under the zone of a row: the other tradable zones not listed under it at the chosen score.
function moreNote(td, r) {
  const n = (r.other_ltf_zones || []).length - otherZones(r).length;
  if (n) td.append(el("span", { class: "sub", style: "color: var(--brass)" }, `+${n} more LTF ${n === 1 ? "zone" : "zones"}`));
  return td;
}
// One more qualifying LTF zone of a coin, listed under its row at or above the chosen score.
function otherRow(r, o) {
  const z = o.zone, trade = o.primary_entry != null;
  const entry = !trade ? "" : o.entry != null ? `${fmtPrice(o.entry)}, ${fmtPrice(o.stoploss)}, ${fmtPrice(o.target)}` : `zone at ${fmtPrice(o.watch)}, stop ${fmtPrice(o.stoploss)}`;
  return el("tr", { class: "other-zone" },
    el("td", { class: "muted" }, "↳ other LTF zone"), el("td", { colspan: "4" }, ""),
    zoneCell(z, r.ltf),
    el("td", {}, el("span", { class: "tag " + (STATE_CLASS[o.zone_state] || "fresh") }, STATE_TEXT[o.zone_state] || "")),
    el("td", {}, el("span", { class: "sub wrap" }, trade ? `${ENTRY_TEXT[o.primary_entry] || ""}, score ${o.score}` : `score ${o.score}: below 5, not traded`)),
    el("td", {}, ""),
    el("td", { class: "num" }, entry, el("span", { class: "sub wrap" }, trade ? [o.waiting ? (o.primary_entry === "TYPE_1" ? "waiting for price to reach the zone" : "waits for its candles") : "entered", feesText(o.fees_r)].filter(Boolean).join(". ") : "")),
    el("td", { class: "times" }, z && z.created_ts ? el("span", { class: "sub" }, `Zone formed ${stamp(z.created_ts).replace(/-/g, "\u2011")}`) : ""),
    el("td", {}, el("div", { class: "rowbtns" },
      el("a", { class: "btn", href: `/chart?symbol=${encodeURIComponent(r.instrument_key)}&mode=${r.mode}&tf=${r.ltf}&zone=${z.zone_id}` }, "Chart"),
      o.can_paper ? el("button", { class: "btn", onclick: () => paperZone(r, z.zone_id) }, "Paper trade") : "")));
}

async function paperTrade(r) { return paperZone(r, r.ltf_zone.zone_id); }
async function paperZone(r, zoneId) {
  try { const o = await post("/api/paper/orders", { zone_id: zoneId, mode: r.mode }); const lev = `${o.leverage}x, estimated liquidation ${fmtPrice(o.liq_price)}`;
    say(o.entry_type && o.entry_type !== "TYPE_1" ? `Paper order placed for ${r.symbol}: ${lower(ENTRY_TEXT[o.entry_type])}, ${qty(o.quantity)} coins for now (${lev}). It fills when ${ENTRY_HOW[o.entry_type]}; the quantity is then worked out again from the real entry. See the Paper page.`
      : `Paper order placed for ${r.symbol}: ${qty(o.quantity)} coins at ${fmtPrice(o.entry)} (${lev}). See the Paper page.`); await loadBoard(); }
  catch (e) { say(`Could not place the paper order. ${e.message}`, true); }
}

function renderProgress() {
  const b = state.board, box = $("progress");
  if (!b || !b.total) { box.textContent = ""; return; }
  const parts = [`${b.ready} of ${b.total} coins in your list ready`];
  if (b.pending.length) parts.push(`still on their way: ${b.pending.slice(0, 8).map((p) => `${p.symbol} (${WORK_TEXT[p.state] || p.state}${p.state === "failed" && p.detail ? ": " + p.detail : ""})`).join(", ")}${b.pending.length > 8 ? ` and ${b.pending.length - 8} more` : ""}`);
  if (b.busy.length) parts.push(`updating now: ${b.busy.slice(0, 6).join(", ")}${b.busy.length > 6 ? ` and ${b.busy.length - 6} more` : ""}`);
  box.textContent = parts.join(". ") + ".";
  box.className = "status" + (b.pending.some((p) => p.state === "failed") ? " err" : "");
}

async function loadBoard() {
  state.board = await api(`/api/results?mode=${$("f-mode").value}`);
  state.reach = Number(state.board.approach_pct) || 1;
  renderProgress(); render();
}

function alertLine(a) {
  const what = { approaching: "approaching", triggered: "touching at", violated: "zone violated at", consumed: "zone used up at", entry2: "entry type 2 at", entry3: "entry type 3 at" }[a.alert] || a.alert;
  const side = a.side === "demand" ? "demand" : "supply";
  return el("li", {}, el("time", {}, stamp(a.time)), `${a.symbol} ${TF_LABEL[a.timeframe]} `, el("span", { class: side + "-t" }, side), ` zone: ${what} ${fmtPrice(a.price)}`);
}

async function loadStatus() {
  const s = await api(`/api/live/status?mode=${$("f-mode").value}`);
  state.status = s;
  Object.assign(state.prices, s.prices || {});
  setDisplayTz(s.display_tz);
  const f = s.feed, b = s.binance || {};
  $("token").textContent = b.banned ? `Binance ban until ${stamp(b.banned_until)}` : `Weight ${b.used_1m ?? 0} of ${b.budget_1m ?? ""}`;
  $("token").className = "badge " + (b.banned ? "bad" : "ok"); $("token").title = `Binance request weight used this minute. The app keeps to ${b.budget_1m} of Binance's ${b.limit_1m}.`;
  $("feed").textContent = !s.scheduler ? "Schedule switched off" : f.connected && !f.stale ? "Live prices (WebSocket)" : f.source === "REST" && !f.stale ? "Prices by REST, WebSocket down" : "Prices stale";
  $("feed").className = "badge " + (f.connected && !f.stale ? "ok" : "bad"); $("feed").title = f.error || "";
  $("bias").textContent = (s.bias || []).map((x) => `${x.symbol} in ${MODE_LABEL[s.bias_mode]} mode: ${SIDE_WORD[x.side] ? lower(SIDE_WORD[x.side]) : "no trade"}${x.curve ? `, curve ${lower(x.curve)}, ITF ${lower(x.itf_trend)}` : ""}`).join(". ") + ((s.bias || []).length ? `. Times in ${DISPLAY_TZ}.` : `Times in ${DISPLAY_TZ}.`);
  const list = $("alerts");
  list.replaceChildren();
  if (!s.alerts.length) list.append(el("li", { class: "empty" }, "No alerts yet. An alert appears when price approaches or touches an entry zone."));
  for (const a of s.alerts) list.append(alertLine(a));
  const parts = [s.last_scan ? `Last scan ${clock(s.last_scan)}` : "No scan since the app started"];
  if (s.next_bar_close) parts.push(`next 5 minute bar closes ${clock(s.next_bar_close)}`);
  if (f.last_poll) parts.push(`prices as of ${clock(f.last_poll)}`);
  if (s.candle_close) parts.push(`last candle close job ${s.candle_close.seconds} s for ${s.candle_close.coins} coins (${s.candle_close.scanned} rescanned)`);
  if (f.error) parts.push(f.error);
  $("foot").textContent = parts.join(". ") + ".";
}

// Prices arrive every second; the table is drawn again at most every 2 seconds for them, so the page stays light.
let renderTimer = null;
function renderSoon() { if (!renderTimer) renderTimer = setTimeout(() => { renderTimer = null; render(); }, 2000); }

function connect() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/live`);
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.type === "prices") { Object.assign(state.prices, m.data); renderSoon(); }
    else if (m.type === "alert") { $("alerts").querySelector(".empty")?.remove(); $("alerts").prepend(alertLine(m)); }
    else if (m.type === "scan" || m.type === "ready") { loadBoard().catch(() => {}); if (m.type === "scan") loadStatus().catch(() => {}); }
  };
  ws.onclose = () => setTimeout(connect, 3000);   // reconnect on its own
}

(async function init() {
  buildGroupSelect($("f-group"));
  $("f-group").onchange = () => { saveGroup($("f-group").value); render(); };
  for (const m of Object.keys(MODES)) $("f-mode").append(el("option", { value: m }, modeText(m)));
  const q = new URLSearchParams(location.search);
  $("f-mode").value = MODES[q.get("mode")] ? q.get("mode") : "MONTHLY";      // Monthly unless a link says otherwise
  $("f-mode").onchange = () => { state.open = null; loadBoard().catch((e) => say(e.message, true)); loadStatus().catch(() => {}); };
  for (const id of ["f-signal", "f-state"]) $(id).onchange = render;
  try { $("f-conf").value = localStorage.getItem("cryptoScanner.confFilter") || ""; } catch (e) { /* storage not available */ }
  $("f-conf").onchange = () => { try { localStorage.setItem("cryptoScanner.confFilter", $("f-conf").value); } catch (e) { /* storage not available */ } render(); };
  buildScoreFilters($("score-filters"), render);
  $("f-search").oninput = render;
  $("f-all-ltf").value = state.ltfMin ? String(state.ltfMin) : "";
  $("f-all-ltf").onchange = () => { state.ltfMin = Number($("f-all-ltf").value) || 0; try { localStorage.setItem(ALL_LTF_KEY, String(state.ltfMin)); } catch (e) { /* not kept */ } render(); };
  try { await loadStatus(); await loadBoard(); } catch (e) { say(`Could not load the dashboard. ${e.message}`, true); }
  connect();
  setInterval(() => { loadStatus().catch(() => {}); if (state.board && state.board.pending.length) loadBoard().catch(() => {}); }, 15000);
})();