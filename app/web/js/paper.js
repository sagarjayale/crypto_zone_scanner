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

// The "Trailing / locked" column: what the position keeps if price comes back to the stop now, and for the lock method
// the price that locks the next step and the target now. The numbers come from the server (lock in /api/paper/positions).
function lockCell(t) {
  const k = t.lock;
  if (!k) return el("td", {}, "");
  const lines = [];
  if (k.is_locked) lines.push(el("span", { class: "win" }, `Locked ${signed(k.locked_usdt)} (${k.locked_r > 0 ? "+" : ""}${k.locked_r} R)`),
    el("span", { class: "sub" }, `after fees about ${signed(k.locked_after_fees)}`));
  else lines.push(el("span", {}, k.method === "off" ? "Off: fixed stop and target" : "Not locked yet"),
    el("span", { class: "sub" }, `risk ${signed(k.locked_usdt)} at ${fmtPrice(k.stop)}`));
  if (k.method === "lock" && k.next_trigger != null) {
    if (k.steps) lines.push(el("span", { class: "sub" }, `stop ${fmtPrice(k.stop)} (${k.steps} step${k.steps > 1 ? "s" : ""}, ${k.pct_locked}% of target)`));
    lines.push(el("span", { class: "sub" }, `next: at ${fmtPrice(k.next_trigger)} stop → ${fmtPrice(k.next_stop)}`));
    lines.push(el("span", { class: "sub" }, k.target_moves_on
      ? `target ${fmtPrice(k.target)}${k.target_moves ? ` (moved ${k.target_moves}×, first ${fmtPrice(k.first_target)})` : ", moves forward when reached"}`
      : `target ${fmtPrice(k.target)}, closes there`));
  } else if (k.method !== "off") lines.push(el("span", { class: "sub" }, `trailing: ${k.method}`));
  return el("td", {}, ...lines);
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
    open.append(el("tr", {}, el("td", {}, t.symbol), el("td", {}, sideTag(t)), el("td", { class: "num" }, qty(t.quantity)),
      el("td", {}, stamp(t.fill_ts), el("span", { class: "sub" }, [ENTRY_TEXT[t.entry_type] || "", t.signal_ts ? `signal ${stamp(t.signal_ts)}` : ""].filter(Boolean).join(", "))),
      el("td", { class: "num" }, fmtPrice(t.fill_price)),
      el("td", {}, since(t.fill_ts)),
      el("td", { class: "num" }, `${t.leverage ?? ""}x, ${money(t.margin)} USDT`, el("span", { class: "sub" + (liqNear ? " loss" : "") }, `liquidation about ${fmtPrice(t.liq_price)}`)),
      el("td", {}, stop), el("td", {}, target), lockCell(t), el("td", { class: "num" }, p == null ? "no live price" : fmtPrice(p)),
      el("td", { class: "num " + (pnl > 0 ? "win" : pnl < 0 ? "loss" : "") }, pnl == null ? "" : signed(pnl)),
      el("td", {}, el("div", { class: "rowbtns" },
        el("button", { class: "btn", onclick: () => showChart("trade", t.id) }, "Chart"),
        zone ? el("button", { class: "btn", onclick: () => act(`Stop and target saved for ${t.symbol}.`, () => api(`/api/paper/positions/${t.id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ stop: Number(stop.value), target: Number(target.value) }) })) }, "Save") : "",
        el("button", { class: "btn", onclick: () => act(`${t.symbol} closed.`, () => post(`/api/paper/positions/${t.id}/close`)) }, "Close now")))));
  }
  const pend = $("pending"); pend.replaceChildren();
  if (!data.pending.length) pend.append(el("tr", {}, el("td", { colspan: "13" }, "No pending orders. Use Paper trade on a setup in the scanner, or switch on automatic orders above.")));
  for (const o of data.pending) {
    const p = prices[o.instrument_key] ?? o.price;
    pend.append(el("tr", {}, el("td", {}, o.symbol), el("td", {}, TF_LABEL[o.timeframe] || ""), el("td", {}, sideTag(o)), el("td", { class: "num" }, qty(o.quantity)),
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

// ---- chart of one order or trade ----------------------------------------------
const panel = { kind: null, id: null, key: null, status: null };
const STATUS_WORD = { pending: "Pending order", open: "Open position", closed: "Closed trade" };
async function showChart(kind, id) {
  const box = $("tc");
  box.hidden = false;
  $("tc-title").textContent = "Loading";
  try {
    const d = await api(`/api/paper/chart/${kind}/${id}`);
    Object.assign(panel, { kind, id, key: d.instrument_key, status: d.status });
    $("tc-title").textContent = `${d.symbol} · ${d.side === "demand" ? "Long" : "Short"} · ${TF_LABEL[d.timeframe] || d.timeframe} · ${STATUS_WORD[d.status]}`;
    $("tc-full").href = `/chart?symbol=${encodeURIComponent(d.instrument_key)}&tf=${d.timeframe}${d.mode ? `&mode=${d.mode}` : ""}${d.zone && d.zone.zone_id ? `&zone=${d.zone.zone_id}` : ""}`;
    renderDetail(d);
    box.scrollIntoView({ behavior: "smooth", block: "start" });
    const n = await TradeChart.show($("tc-chart"), { ...d, result: d.status === "closed" ? d.result : "open" }, null);
    if (!n) $("tc-title").textContent += " (no candles stored for this timeframe)";
  } catch (e) { $("tc-title").textContent = "Could not load the chart"; say(`Could not load the chart. ${e.message}`, true); }
}
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
$("tc-close").onclick = () => { $("tc").hidden = true; panel.kind = null; };

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
  ws.onmessage = (ev) => { const m = JSON.parse(ev.data); if (m.type === "prices") { Object.assign(prices, m.data); if (summary) render(); if (panel.kind && panel.status !== "closed" && !$("tc").hidden && prices[panel.key] != null) TradeChart.setLive(prices[panel.key]); } else if (m.type === "paper" || m.type === "scan") load().catch(() => {}); };
  ws.onclose = () => setTimeout(connect, 3000);
}
showToken($("token"));
load().catch((e) => say(`Could not load paper trading. ${e.message}`, true));
connect();
