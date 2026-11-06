// Paper trading page. Positions come from the database; prices and fills arrive over /ws/live.
const $ = (id) => document.getElementById(id);
let data = { pending: [], open: [], closed: [] }, summary = null, prices = {};
const sideTag = (t) => el("span", { class: "tag " + t.side }, t.side === "demand" ? "Long" : "Short");
function say(text, isError = false) { $("status").textContent = text; $("status").className = "status" + (isError ? " err" : ""); }
// Open profit or loss at the live price, after the entry fee and the funding so far (the exit fee is not known yet).
const livePnl = (t) => { const p = prices[t.instrument_key] ?? t.price; return p == null ? null : (p - t.fill_price) * (t.side === "demand" ? 1 : -1) * t.quantity - (t.fees || 0) + (t.funding || 0); };

function renderFigures() {
  const s = summary, fig = $("figures");
  fig.replaceChildren();
  const open = data.open.reduce((sum, t) => sum + (livePnl(t) ?? 0), 0);
  const rows = [["Balance (USDT)", money(s.balance)], ["Closed profit or loss", signed(s.realised)], ["Open profit or loss", signed(open)], ["Margin in use", money(s.margin_used)],
    ["Fees paid", money(s.fees_paid)], ["Funding", signed(s.funding)],
    ["Closed trades", s.closed_trades], ["Win rate", s.win_rate == null ? "none yet" : `${s.win_rate}%`], ["Expectancy per trade", s.expectancy_r == null ? "none yet" : `${s.expectancy_r} R`],
    ["Risk per trade", `${s.risk_pct}% of the balance`]];
  for (const [k, v] of rows) fig.append(el("div", {}, el("dt", {}, k), el("dd", {}, String(v))));
}

// The "Trailing / locked" column: a step ladder from the entry to the target. Locked steps are filled, a dot marks the
// live price, and the next step says which price locks it and how far that is. After the first target the ladder keeps
// growing when the target moves forward ("move"). The step numbers come from the server (lock in /api/paper/positions).
const pctAway = (from, to) => (from ? Math.abs(to - from) / from * 100 : null);
function ladderBar(k, sign, p) {
  const levels = k.levels || [];
  const top = Math.max(...[...levels, k.target ?? k.first_target].map((x) => (x - k.entry) * sign));
  const pos = (x) => Math.max(0, Math.min(1, ((x - k.entry) * sign) / (top || 1))) * 100;
  const bar = el("div", { class: "ladder", role: "img", "aria-label": `${k.steps} of ${k.steps_to_target} steps locked` });
  bar.append(el("div", { class: "ladder-fill", style: `width:${k.steps ? pos(k.stop) : 0}%` }));
  levels.forEach((x, i) => {
    const n = i + 1, cls = n <= k.steps ? "done" : n === k.steps + 1 ? "next" : "";
    bar.append(el("span", { class: `ladder-tick ${cls}${n === k.steps_to_target ? " goal" : ""}`, style: `left:${pos(x)}%`, title: `step ${n}: stop ${fmtPrice(x)}` }));
  });
  if (p != null) bar.append(el("span", { class: "ladder-dot" + ((p - k.entry) * sign < 0 ? " under" : ""), style: `left:${pos(p)}%`, title: `price ${fmtPrice(p)}` }));
  const ends = el("div", { class: "ladder-ends" }, el("span", {}, fmtPrice(k.entry)), el("span", {}, `🎯 ${fmtPrice(k.target ?? k.first_target)}`));
  return [bar, ends];
}
// A warning when the coin's stored candles are behind: stops, fills and the trailing stop on candles wait for them.
const LAG_WARN_MIN = 5;
function lagWarning(lag) {
  if (!lag || lag.minutes == null || lag.minutes <= LAG_WARN_MIN) return null;
  const m = lag.minutes, txt = m >= 120 ? `${Math.floor(m / 60)} h ${m % 60} min` : `${m} min`;
  return el("span", { class: "lag-warn", title: `The newest stored ${TF_LABEL[lag.tf] || lag.tf} candle closed ${txt} ago. The live price still checks the stop and target every second; the checks on candles (wicks the live price can miss) and the trailing stop catch up when the candles do.` },
    `⚠ candles ${txt} behind`);
}
function lockCell(t, p) {
  const k = t.lock;
  if (!k) return el("td", {}, "");
  const warn = lagWarning(t.lag);
  const cell = lockCellBody(t, p, k);
  if (warn) cell.prepend(warn);
  return cell;
}
function lockCellBody(t, p, k) {
  const sign = t.side === "demand" ? 1 : -1;
  const lines = [];
  if (k.method !== "lock" || k.next_trigger == null) {
    if (k.is_locked) lines.push(el("span", { class: "lock-head win" }, `🔒 Locked ${signed(k.locked_usdt)} (${k.locked_r > 0 ? "+" : ""}${k.locked_r} R)`));
    else lines.push(el("span", { class: "lock-head" }, k.method === "off" ? "Off: fixed stop and target" : "🔓 Not locked yet"));
    if (k.method !== "off") lines.push(el("span", { class: "sub" }, `trailing: ${k.method}, stop ${fmtPrice(k.stop)}`));
    return el("td", { class: "lockcell" }, ...lines);
  }
  if (k.target_reached && k.target_moves_on) lines.push(el("span", { class: "lock-goal" }, `🎯 First target ${fmtPrice(k.first_target)} reached · running beyond target`));
  if (k.is_locked) {
    const n = k.beyond_target ? `Step ${k.steps} (${k.steps - k.steps_to_target} beyond target)` : `Step ${k.steps} of ${k.steps_to_target}`;
    lines.push(el("span", { class: "lock-head win" }, `🔒 ${n} locked · stop ${fmtPrice(k.stop)}`),
      el("span", { class: "sub" }, `keeps ${k.locked_r > 0 ? "+" : ""}${k.locked_r} R, ${signed(k.locked_usdt)} USDT (after fees about ${signed(k.locked_after_fees)})`));
  } else {
    lines.push(el("span", { class: "lock-head" }, `🔓 Not locked yet · Step 0 of ${k.steps_to_target}`),
      el("span", { class: "sub" }, `stop ${fmtPrice(k.stop)}: risk ${signed(k.locked_usdt)} USDT`));
  }
  lines.push(...ladderBar(k, sign, p));
  const away = p == null ? null : (k.next_trigger - p) * sign;
  lines.push(el("span", { class: "lock-next" }, `Next: step ${k.steps + 1} → stop ${fmtPrice(k.next_stop)} when price reaches ${fmtPrice(k.next_trigger)}` +
    (away == null ? "" : away <= 0 ? " (reached: locks at the next candle close)" : ` (${pctAway(p, k.next_trigger).toFixed(2)}% away)`)));
  lines.push(el("span", { class: "sub" }, k.target_moves_on
    ? `Target ${fmtPrice(k.target)}: does not close the trade, moves forward ${fmtPrice(k.step)} each time it is reached${k.target_moves ? ` (moved ${k.target_moves}×)` : ""}. Only the locked stop ends the trade.`
    : `Target ${fmtPrice(k.target)} closes the trade.`));
  return el("td", { class: "lockcell" }, ...lines);
}

// ---- pending orders: how near the entry is, and whether price is coming closer --------------------------------
// Distance in R (entry to stop), so a calm coin and a fast one are judged alike.
const NEAR = [["now", 0.25, "Entering now"], ["close", 1, "Close"], ["between", 3, "Between"], ["far", Infinity, "Far"]];
const NEAR_KEY = "cryptoScanner.paperNear";
let nearFilter = (() => { try { return localStorage.getItem(NEAR_KEY) || "all"; } catch (e) { return "all"; } })();
const priceHist = {};                    // coin -> [[seconds, price], ...] for the last 5 minutes (in the page only)
function remember(update) {
  const now = Date.now() / 1000;
  for (const [k, v] of Object.entries(update)) {
    const h = priceHist[k] || (priceHist[k] = []);
    h.push([now, Number(v)]);
    while (h.length && now - h[0][0] > 300) h.shift();
  }
}
function nearness(o) {
  const p = prices[o.instrument_key] ?? o.price;
  const risk = Math.abs(o.entry - o.stop);
  if (p == null || !risk) return { group: "far", r: null, pct: null, p };
  const sign = o.side === "demand" ? 1 : -1;            // a buy limit waits below the price, a sell limit above it
  const dist = (p - o.entry) * sign;
  const r = Math.max(0, dist / risk);
  const group = (NEAR.find(([, lim]) => r <= lim) || NEAR[3])[0];
  // direction and speed over the last 5 minutes (at least one minute of prices is needed)
  const h = priceHist[o.instrument_key] || [];
  let trend = "measuring", eta = null;
  if (h.length > 1 && h[h.length - 1][0] - h[0][0] >= 60) {
    const [t0, p0] = h[0], [t1] = h[h.length - 1];
    const closer = ((p0 - o.entry) * sign - dist) / risk;           // R gained toward the entry since then
    if (Math.abs(closer) < 0.05) trend = "flat";
    else if (closer > 0) { trend = "closer"; const perS = closer / (t1 - t0); eta = perS > 0 ? r / perS : null; }
    else trend = "away";
  }
  return { group, r, pct: Math.abs(p - o.entry) / p * 100, p, trend, eta, through: dist <= 0 };
}
// arrows show which way the price moves: a buy limit waits below the price, so "closer" is down; a sell limit, up
const TREND_TEXT = { closer: "coming closer", away: "moving away", flat: "➖ flat", measuring: "measuring…" };
function trendText(o, trend) {
  if (trend === "flat" || trend === "measuring") return TREND_TEXT[trend];
  const down = (trend === "closer") === (o.side === "demand");
  return `${down ? "⬇" : "⬆"} ${TREND_TEXT[trend]}`;
}
const etaText = (s) => (s == null || !isFinite(s) ? "" : s < 90 ? ", under 2 min at this speed" : `, ~${duration(s)} at this speed`);
const WAIT_TEXT = { waiting: "Waits for a candle to close inside the zone", testing: "Price is in the zone: waits for a candle to close inside it",
  armed: "Armed: a candle closed inside the zone, the next candle can enter", unknown: "Zone no longer stored" };
function nearCell(o, n) {
  const arrowTxt = trendText(o, n.trend);
  const label = (NEAR.find(([g]) => g === n.group) || NEAR[3])[2];
  const kids = [el("span", { class: `near-badge ${n.group}`, style: n.group === "now" ? `animation-delay:-${(Date.now() % 1600) / 1000}s` : "" }, label.toUpperCase())];
  if (n.r != null) kids.push(el("span", { class: "near-dist" }, n.through ? "price is at the entry" : `${n.r.toFixed(2)} R · ${n.pct.toFixed(2)}% away`));
  if (n.r != null) kids.push(el("div", { class: "near-bar" }, el("span", { style: `width:${Math.max(4, 100 - Math.min(n.r, 4) / 4 * 100)}%` })));
  kids.push(el("span", { class: `near-trend ${n.trend}` }, arrowTxt + (n.trend === "closer" ? etaText(n.eta) : "")));
  if (o.wait && o.wait !== "limit") kids.push(el("span", { class: "sub wrap" + (o.wait === "armed" ? " win" : "") }, WAIT_TEXT[o.wait] || ""));
  const warn = lagWarning(o.lag);
  if (warn) kids.push(warn);
  return el("td", { class: "nearcell" }, ...kids);
}
function nearChips(list) {
  const box = $("near-chips");
  const count = Object.fromEntries(NEAR.map(([g]) => [g, 0]));
  for (const [, n] of list) count[n.group]++;
  const chip = (g, text) => el("button", { class: `near-chip ${g}`, "aria-pressed": String(nearFilter === g),
    onclick: () => { nearFilter = g; try { localStorage.setItem(NEAR_KEY, g); } catch (e) { /* storage not available */ } render(); } }, text);
  box.replaceChildren(...NEAR.map(([g, , label]) => chip(g, `${{ now: "🔴", close: "🟢", between: "🟡", far: "⚪" }[g]} ${label} ${count[g]}`)), chip("all", `All ${list.length}`));
}

function render() {
  renderFigures();
  const open = $("open"); open.replaceChildren();
  if (!data.open.length) open.append(el("tr", {}, el("td", { colspan: "13" }, "No open positions. An entry type 1 order becomes a position when the live price reaches its entry; an entry type 2 or 3 order when its candles have closed.")));
  for (const t of data.open) {
    const p = prices[t.instrument_key] ?? t.price, pnl = livePnl(t), zone = true;
    const stop = el("input", { class: "edit", type: "number", step: "any", value: t.stop, "aria-label": "Stop" });
    const target = el("input", { class: "edit", type: "number", step: "any", value: t.target, "aria-label": "Target" });
    const liqNear = p != null && t.liq_price != null && Math.abs(p - t.liq_price) / p < 0.05;
    const k = t.lock || {}, sgn = t.side === "demand" ? 1 : -1, risk0 = Math.abs(t.fill_price - (k.first_stop ?? t.stop));
    const nearStop = p != null && risk0 > 0 && (p - t.stop) * sgn <= 0.3 * risk0;
    open.append(el("tr", { class: nearStop ? "near-stop" : k.is_locked ? "is-locked" : "" }, el("td", {}, t.symbol, nearStop ? el("span", { class: "sub loss" }, "near the stop") : ""), el("td", {}, sideTag(t)), el("td", { class: "num" }, qty(t.quantity)),
      el("td", {}, stamp(t.fill_ts), el("span", { class: "sub" }, [ENTRY_TEXT[t.entry_type] || "", t.signal_ts ? `signal ${stamp(t.signal_ts)}` : ""].filter(Boolean).join(", "))),
      el("td", { class: "num" }, fmtPrice(t.fill_price)),
      el("td", {}, since(t.fill_ts)),
      el("td", { class: "num" }, `${t.leverage ?? ""}x, ${money(t.margin)} USDT`, el("span", { class: "sub" + (liqNear ? " loss" : "") }, `liquidation about ${fmtPrice(t.liq_price)}`)),
      el("td", {}, stop), el("td", {}, target), lockCell(t, p), el("td", { class: "num" }, p == null ? "no live price" : fmtPrice(p)),
      el("td", { class: "num " + (pnl > 0 ? "win" : pnl < 0 ? "loss" : "") }, pnl == null ? "" : signed(pnl)),
      el("td", {}, el("div", { class: "rowbtns" },
        el("button", { class: "btn", onclick: () => showChart("trade", t.id) }, "Chart"),
        zone ? el("button", { class: "btn", onclick: () => act(`Stop and target saved for ${t.symbol}.`, () => api(`/api/paper/positions/${t.id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ stop: Number(stop.value), target: Number(target.value) }) })) }, "Save") : "",
        el("button", { class: "btn", onclick: () => act(`${t.symbol} closed.`, () => post(`/api/paper/positions/${t.id}/close`)) }, "Close now")))));
  }
  const pend = $("pending"); pend.replaceChildren();
  const ranked = data.pending.map((o) => [o, nearness(o)]).sort((a, b) => (a[1].r ?? 1e9) - (b[1].r ?? 1e9));
  nearChips(ranked);
  const shownPending = ranked.filter(([, n]) => nearFilter === "all" || n.group === nearFilter);
  if (!data.pending.length) pend.append(el("tr", {}, el("td", { colspan: "14" }, "No pending orders. Use Paper trade on a setup in the scanner, or switch on automatic orders above.")));
  else if (!shownPending.length) pend.append(el("tr", {}, el("td", { colspan: "14" }, "No order in this group right now.")));
  for (const [o, n] of shownPending) {
    const p = n.p;
    pend.append(el("tr", { class: `near-row ${n.group}` }, el("td", {}, o.symbol), nearCell(o, n), el("td", {}, TF_LABEL[o.timeframe] || ""), el("td", {}, sideTag(o)), el("td", { class: "num" }, qty(o.quantity)),
      el("td", {}, ENTRY_TEXT[o.entry_type] || "Entry type 1", el("span", { class: "sub wrap" }, o.entry_type === "TYPE_2" || o.entry_type === "TYPE_3" ? `waits until ${ENTRY_HOW[o.entry_type]}` : "limit at the proximal line")),
      el("td", { class: "num" }, fmtPrice(o.entry), el("span", { class: "sub" }, o.entry_type === "TYPE_2" || o.entry_type === "TYPE_3" ? "proximal line, until the entry" : "")),
      el("td", { class: "num" }, fmtPrice(o.stop)), el("td", { class: "num" }, fmtPrice(o.target)),
      el("td", { class: "num" }, `${o.leverage ?? ""}x`, el("span", { class: "sub" }, `liquidation about ${fmtPrice(o.liq_price)}`)), el("td", { class: "num" }, p == null ? "" : fmtPrice(p)),
      el("td", {}, stamp(o.created_at), el("span", { class: "sub" }, ago(o.created_at))),
      el("td", {}, o.signal_ts ? stamp(o.signal_ts) : "", el("span", { class: "sub" }, o.signal_ts ? ago(o.signal_ts) : "")),
      el("td", {}, el("div", { class: "rowbtns" },
        el("button", { class: "btn", onclick: () => showChart("order", o.id) }, "Chart"),
        el("button", { class: "btn primary", onclick: () => enterNow(o) }, "Enter now"),
        el("button", { class: "btn", onclick: () => act(`Order for ${o.symbol} cancelled.`, () => api(`/api/paper/orders/${o.id}`, { method: "DELETE" })) }, "Cancel order")))));
  }
  const closed = $("closed"); closed.replaceChildren();
  if (!data.closed.length) closed.append(el("tr", {}, el("td", { colspan: "14" }, "No closed trades yet.")));
  const why = { stop: "Stop", target: "Target", manual: "Closed by you", trail: "Trailing stop" };
  for (const t of data.closed) {
    closed.append(el("tr", {}, el("td", {}, t.symbol), el("td", {}, sideTag(t)), el("td", { class: "num" }, qty(t.quantity)), el("td", {}, stamp(t.fill_ts), el("span", { class: "sub" }, ENTRY_TEXT[t.entry_type] || "")), el("td", {}, stamp(t.exit_ts)),
      el("td", {}, duration(t.duration_s)),
      el("td", { class: "num" }, fmtPrice(t.fill_price)), el("td", { class: "num" }, fmtPrice(t.exit_price)), el("td", {}, why[t.exit_reason] || ""),
      el("td", { class: "num" }, money(t.fees)), el("td", { class: "num" }, signed(t.funding)),
      el("td", { class: "num " + (t.result === "win" ? "win" : "loss") }, signed(t.pnl)), el("td", { class: "num" }, t.result_r == null ? "" : `${t.result_r > 0 ? "+" : ""}${t.result_r} R`),
      el("td", {}, el("button", { class: "btn", onclick: () => showChart("trade", t.id) }, "Chart"))));
  }
}

// Enter a pending order now at the live price: first the numbers (nothing is entered), then your OK, then the entry.
async function enterNow(o) {
  try {
    const p = await post(`/api/paper/orders/${o.id}/enter`, { dry_run: true });
    const side = p.side === "demand" ? "long" : "short";
    const text = `Enter ${o.symbol} ${side} now at about ${fmtPrice(p.price)}?\n\nStop ${fmtPrice(p.stop)} · Target ${fmtPrice(p.target)} · ${p.rr} to 1\n` +
      `${qty(p.quantity)} coins · risk ${money(p.risk_usdt)} USDT · ${p.leverage}x, liquidation about ${fmtPrice(p.liq_price)}\n` +
      `Market entry: taker fee${p.fees_r && p.fees_r.loss != null ? ` (${feesText(p.fees_r)})` : ""}. Paper only; nothing is sent to Binance.` +
      (p.low_rr ? `\n\nWarning: only ${p.rr} to 1 is left between the stop and the target. Enter anyway?` : "");
    if (!confirm(text)) return;
    await act(`${o.symbol} entered at market. See Open positions.`, () => post(`/api/paper/orders/${o.id}/enter`, { allow_low_rr: p.low_rr }));
  } catch (e) { say(`Could not enter ${o.symbol}. ${e.message}`, true); }
}

// ---- chart of one order or trade: a popup with timeframes, a live candle and stop / target you can drag ----------
const panel = { kind: null, id: null, key: null, status: null, tf: null, own: null, saved: null, d: null };
const STATUS_WORD = { pending: "Pending order", open: "Open position", closed: "Closed trade" };
const POPUP_TFS = ["1m", "5m", "15m", "30m", "1h", "4h", "1d"];
const SHOW_KEY = "cryptoScanner.paperChartShow";
let chartShow = (() => { try { return JSON.parse(localStorage.getItem(SHOW_KEY) || "{}"); } catch (e) { return {}; } })();
let formingTimer = null;
function closeChart() {
  $("tc").hidden = true; panel.kind = null; clearInterval(formingTimer);
  document.body.classList.remove("modal-open");
}
async function showChart(kind, id, tf) {
  const modal = $("tc"), same = panel.kind === kind && panel.id === id;
  modal.hidden = false; document.body.classList.add("modal-open");
  $("tc-title").textContent = "Loading"; $("tc-status").textContent = "";
  try {
    const d = await api(`/api/paper/chart/${kind}/${id}${tf ? `?tf=${tf}` : ""}`);
    const own = same && panel.own ? panel.own : (d.own_timeframe || d.timeframe);
    Object.assign(panel, { kind, id, key: d.instrument_key, status: d.status, tf: d.timeframe, own, d });
    $("tc-title").textContent = `${d.symbol} · ${d.side === "demand" ? "Long" : "Short"} · ${STATUS_WORD[d.status]} · order on ${TF_LABEL[own] || own}`;
    $("tc-full").href = `/chart?symbol=${encodeURIComponent(d.instrument_key)}&tf=${d.timeframe}${d.mode ? `&mode=${d.mode}` : ""}${d.zone && d.zone.zone_id ? `&zone=${d.zone.zone_id}` : ""}`;
    renderTfs(); renderDetail(d);
    const editable = d.status !== "closed";
    panel.saved = { stop: d.stop, target: d.target };
    $("tc-edit").hidden = !editable;
    $("tc-stop").value = d.stop ?? ""; $("tc-target").value = d.target ?? "";
    $("tc-edit-note").textContent = d.status === "open"
      ? "Drag the stop or target line on the chart, or type a price, then Save. A trailing stop keeps tightening from what it has locked."
      : "Drag the stop or target line on the chart, or type a price, then Save. The size is worked out again for the same risk.";
    const live = d.status !== "closed" && BAR_SECONDS[d.timeframe] <= 86400;
    let forming = null;
    if (live) { try { forming = (await api(`/api/forming/${encodeURIComponent(d.instrument_key)}?tf=${d.timeframe}`)).forming; } catch (e) { /* drawn without it */ } }
    const row = data.open.find((t) => t.id === id && kind === "trade");
    const k = row && row.lock;
    const steps = k && k.levels ? k.levels.map((price, i) => ({ n: i + 1, price, done: i + 1 <= k.steps })) : [];
    const n = await TradeChart.show($("tc-chart"), { ...d, result: d.status === "closed" ? d.result : "open" }, null,
      { editable, visible: chartShow, steps, forming, liveCandle: live, onDrag: (field, price, done) => {
        $(field === "stop" ? "tc-stop" : "tc-target").value = Number(price.toPrecision(8));
        if (done) $("tc-status").textContent = `${field === "stop" ? "Stop" : "Target"} moved to ${fmtPrice(price)}: press Save to keep it.`;
      } });
    if (!n) $("tc-status").textContent = `No ${TF_LABEL[d.timeframe] || d.timeframe} candles are stored for this coin.`;
    clearInterval(formingTimer);
    if (live) formingTimer = setInterval(async () => {       // the forming candle from Binance now and then, to stay exact
      if (panel.kind !== kind || panel.id !== id || $("tc").hidden) return;
      try { const f = (await api(`/api/forming/${encodeURIComponent(d.instrument_key)}?tf=${panel.tf}`)).forming; if (f) TradeChart.seed(f); } catch (e) { /* next time */ }
    }, 15000);
  } catch (e) { $("tc-title").textContent = "Could not load the chart"; $("tc-status").textContent = e.message; }
}
const BAR_SECONDS = { "1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "2h": 7200, "4h": 14400, "1d": 86400, "1w": 604800, "1mo": 2678400 };
function renderTfs() {
  const box = $("tc-tfs");
  box.replaceChildren(...POPUP_TFS.map((tf) => el("button", { "aria-pressed": String(tf === panel.tf),
    title: tf === panel.own ? "the order's own timeframe" : "", onclick: () => { if (tf !== panel.tf) showChart(panel.kind, panel.id, tf); } },
    TF_LABEL[tf] + (tf === panel.own ? " •" : ""))));
}
$("tc-show").addEventListener("change", (e) => {
  const k = e.target.dataset.k;
  if (!k) return;
  chartShow[k] = e.target.checked;
  try { localStorage.setItem(SHOW_KEY, JSON.stringify(chartShow)); } catch (err) { /* storage not available */ }
  if (panel.kind) showChart(panel.kind, panel.id, panel.tf);
});
for (const box of $("tc-show").querySelectorAll("input")) box.checked = chartShow[box.dataset.k] !== false;
$("tc-stop").oninput = () => { const v = Number($("tc-stop").value); if (v > 0) TradeChart.setLevels(v, Number($("tc-target").value) || panel.saved.target); };
$("tc-target").oninput = () => { const v = Number($("tc-target").value); if (v > 0) TradeChart.setLevels(Number($("tc-stop").value) || panel.saved.stop, v); };
$("tc-reset").onclick = () => {
  $("tc-stop").value = panel.saved.stop; $("tc-target").value = panel.saved.target;
  TradeChart.setLevels(panel.saved.stop, panel.saved.target); $("tc-status").textContent = "Back to the saved stop and target.";
};
$("tc-save").onclick = async () => {
  const body = { stop: Number($("tc-stop").value), target: Number($("tc-target").value) };
  const path = panel.kind === "order" ? `/api/paper/orders/${panel.id}` : `/api/paper/positions/${panel.id}`;
  try {
    await api(path, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    panel.saved = body;
    if (panel.d) { Object.assign(panel.d, body); renderDetail(panel.d); }
    $("tc-status").textContent = `Saved: stop ${fmtPrice(body.stop)}, target ${fmtPrice(body.target)}.`;
    await load();
  } catch (e) { $("tc-status").textContent = `Not saved: ${e.message}`; }
};
$("tc-close").onclick = closeChart;
$("tc").addEventListener("click", (e) => { if (e.target === $("tc")) closeChart(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("tc").hidden) closeChart(); });
function renderDetail(d) {
  const z = d.zone, rows = [];
  const add = (k, v) => { if (v !== "" && v != null) rows.push(el("dt", {}, k), el("dd", {}, String(v))); };
  add("Entry type", ENTRY_TEXT[d.entry_type] || "Entry type 1");
  add("Signal", d.signal_ts ? `${stamp(d.signal_ts)} (${ago(d.signal_ts)})` : "");
  add("Placed", d.placed_at ? stamp(d.placed_at) : "");
  add("Entry time", d.entry_ts ? stamp(d.entry_ts) : "not entered yet");
  if (d.exit_ts) add("Exit time", `${stamp(d.exit_ts)} (${{ stop: "stop", target: "target", manual: "closed by you", trail: "trailing stop" }[d.exit_reason] || d.exit_reason || ""})`);
  if (d.duration_s != null) add(d.exit_ts ? "Duration" : "Open for", duration(d.duration_s));
  add("Entry", fmtPrice(d.entry));
  if (d.exit_price != null) add("Exit", fmtPrice(d.exit_price));
  add("Stop", fmtPrice(d.stop)); add("Target", fmtPrice(d.target));
  add("Reward to risk", d.rr == null ? "" : `${d.rr} to 1`);
  add("Quantity", qty(d.quantity));
  add("Leverage", d.leverage ? `${d.leverage}x, liquidation about ${fmtPrice(d.liq_price)}` : "");
  if (d.status === "closed") {
    add("Fees", `${money(d.fees)} USDT`); add("Funding", `${signed(d.funding)} USDT`);
    add("Profit or loss", `${signed(d.pnl)} USDT${d.result_r == null ? "" : ` (${d.result_r > 0 ? "+" : ""}${d.result_r} R)`}`);
  } else if (d.status === "open") {
    add("Price now", d.price == null ? "no live price" : fmtPrice(d.price));
    add("Open profit or loss", d.open_pnl == null ? "" : `${signed(d.open_pnl)} USDT`);
  } else add("Price now", d.price == null ? "no live price" : fmtPrice(d.price));
  if (z) {
    add("Zone", `${fmtPrice(z.proximal)} to ${fmtPrice(z.distal)}, ${z.pattern}, ${d.zone_status}`);
    add("Zone formed", stamp(d.zone_created_ts));
    add("Zone score", `${z.score} (freshness ${z.freshness}, strength ${z.strength}: ${z.strength_label}, base ${z.base}: ${z.bases} candles)`);
    add("Tests", z.wick_tests);
  } else add("Zone", "no longer stored");
  if (d.trail && d.trail.trail_method === "lock") add("Trailing stop", `lock ${d.trail.trail_lock_pct}% of the target distance (gap ${d.trail.trail_lock_gap_pct}% of a step) on ${TF_LABEL[d.trail.tf] || d.trail.tf} candles, first stop ${fmtPrice(d.trail.first_stop)}, first target ${fmtPrice(d.trail.first_target)}${d.trail.trail_lock_target === "move" ? `, target moves forward (moved ${d.trail.target_moves || 0} times)` : ", closes at the target"}`);
  else if (d.trail) add("Trailing stop", `${d.trail.trail_method}, from +${d.trail.trail_start_r} R on ${TF_LABEL[d.trail.tf] || d.trail.tf} candles, first stop ${fmtPrice(d.trail.first_stop)}${d.trail.no_target ? ", target removed" : ""}`);
  for (const h of d.history || []) add(`${h.field === "stop" ? "Stop" : "Target"} changed`, `${stamp(h.time)}: ${fmtPrice(h.old)} to ${h.new == null ? "none" : fmtPrice(h.new)}${h.note ? ` (${h.note})` : ""}`);
  $("tc-detail").replaceChildren(...rows);
}

async function load() {
  [data, summary] = await Promise.all([api("/api/paper/positions"), api("/api/paper/summary")]);
  $("auto").checked = summary.auto; $("auto-score").value = String(summary.auto_min_score); $("lev").value = summary.leverage;
  render();
}
async function act(done, fn) { try { await fn(); say(done); await load(); } catch (e) { say(e.message, true); } }

async function saveAuto() {
  await act($("auto").checked ? `Automatic orders on for score ${$("auto-score").value} and above.` : "Automatic orders off.",
    () => api("/api/settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ paper_auto: $("auto").checked, paper_auto_min_score: Number($("auto-score").value) }) }));
}
$("auto").onchange = saveAuto; $("auto-score").onchange = saveAuto;
$("lev").onchange = () => act(`Leverage ${$("lev").value}x for new orders. Orders whose estimated liquidation comes before their stop loss are refused.`,
  () => api("/api/settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ paper_leverage: Number($("lev").value) }) }));
$("reset").onclick = () => { if (confirm("Archive every paper order and trade and start again from the starting capital?")) act("Paper account reset.", () => post("/api/paper/reset")); };

function connect() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/live`);
  ws.onmessage = (ev) => { const m = JSON.parse(ev.data); if (m.type === "prices") { Object.assign(prices, m.data); remember(m.data); if (summary) render(); if (panel.kind && panel.status !== "closed" && !$("tc").hidden && prices[panel.key] != null) TradeChart.setLive(prices[panel.key]); } else if (m.type === "paper" || m.type === "scan") load().catch(() => {}); };
  ws.onclose = () => setTimeout(connect, 3000);
}
showToken($("token"));
load().catch((e) => say(`Could not load paper trading. ${e.message}`, true));
connect();
