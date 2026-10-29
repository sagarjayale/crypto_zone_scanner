// Chart and replay page. Live mode reads stored candles and zones; replay mode streams
// them bar by bar from a separate copy of the engine on the server.
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const COLOR = { demand: css("--demand"), supply: css("--supply"), brass: css("--brass"), muted: css("--muted"), line: css("--line") };
const FILL = { demand: "rgba(47,165,139,", supply: "rgba(208,89,107,", old: "rgba(143,161,181," };

const $ = (id) => document.getElementById(id);
const state = { key: null, tf: "1d", mode: "MONTHLY", result: null, scan: null, names: {}, zones: [], closed: [], overlay: [], extra: new Set(), contacts: [], contactOff: new Set(), selected: null, replay: null, playing: false,
  markers: [], lines: [], firstTime: null, lastBars: [], targetRR: 2 };
const FILL_PLAN = { risk: "rgba(208,89,107,0.24)", reward: "rgba(47,165,139,0.14)" };

// Nearest loaded candle at or after a time. Zones from another timeframe start between this chart's candles.
function snapTime(t) {
  const bars = state.lastBars;
  if (!bars.length || t <= bars[0].time) return null;
  let lo = 0, hi = bars.length - 1;
  if (t > bars[hi].time) return bars[hi].time;
  while (lo < hi) { const mid = (lo + hi) >> 1; if (bars[mid].time < t) lo = mid + 1; else hi = mid; }
  return bars[lo].time;
}

// ---- chart -------------------------------------------------------------
const chart = LightweightCharts.createChart($("chart"), {
  autoSize: true,
  layout: { background: { color: css("--ink") }, textColor: COLOR.muted, fontFamily: css("--font") },
  grid: { vertLines: { color: "#1c2b3d" }, horzLines: { color: "#1c2b3d" } },
  rightPriceScale: { borderColor: COLOR.line },
  timeScale: { borderColor: COLOR.line, timeVisible: true, secondsVisible: false },
  crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
  localization: { locale: "en-US", priceFormatter: fmtPrice },
});
// Candles are neutral slate so that colour on the chart always means a zone.
const series = chart.addSeries(LightweightCharts.CandlestickSeries, {
  upColor: "#c9d3dd", borderUpColor: "#c9d3dd", wickUpColor: "#c9d3dd",
  downColor: "#4f647b", borderDownColor: "#4f647b", wickDownColor: "#4f647b", priceLineVisible: true, lastValueVisible: true,
});
const markerLayer = LightweightCharts.createSeriesMarkers(series, []);

// Zones are drawn as bands from the candle that created them to the right edge. Zones from another timeframe
// are drawn dashed with their timeframe written on them. The selected zone's plan is highlighted on top:
// a red box from entry to stop (the risk), a green box from entry to target, and thick entry and stop lines.
class ZoneBands {
  constructor() { this.zones = []; this.selected = null; this.firstTime = null; this.plan = null; this.curve = null; }
  setCurve(curve) { this.curve = curve; if (this.requestUpdate) this.requestUpdate(); }
  setScan(time, label) { this.scan = time == null ? null : { time, label }; if (this.requestUpdate) this.requestUpdate(); }
  attached(p) { this.chart = p.chart; this.series = p.series; this.requestUpdate = p.requestUpdate; }
  detached() { this.chart = null; }
  set(zones, selected, firstTime) { this.zones = zones; this.selected = selected; this.firstTime = firstTime; if (this.requestUpdate) this.requestUpdate(); }
  setPlan(plan) { this.plan = plan; if (this.requestUpdate) this.requestUpdate(); }
  paneViews() {
    return [{ zOrder: () => "bottom", renderer: () => ({ draw: (target) => this.draw(target) }) },
            { zOrder: () => "top", renderer: () => ({ draw: (target) => this.drawPlan(target) }) }];
  }
  xOf(time) {
    const scale = this.chart.timeScale();
    let x = scale.timeToCoordinate(time);
    if (x == null) { const near = snapTime(time); x = near == null ? null : scale.timeToCoordinate(near); }
    if (x == null && this.firstTime != null && time < this.firstTime) x = 0;
    return x;
  }
  draw(target) {
    if (!this.chart) return;
    target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
      const cv = this.curve;
      if (cv) {       // the curve: three bands between the HTF demand and supply proximal lines
        const third = (cv.supply - cv.demand) / 3;
        const parts = [["LOW", cv.demand, cv.demand + third, FILL.demand], ["EQUILIBRIUM", cv.demand + third, cv.demand + 2 * third, FILL.old], ["HIGH", cv.demand + 2 * third, cv.supply, FILL.supply]];
        for (const [name, lo, hi, fill] of parts) {
          const y1 = this.series.priceToCoordinate(hi), y2 = this.series.priceToCoordinate(lo);
          if (y1 == null || y2 == null) continue;
          const here = cv.band === name;
          ctx.fillStyle = fill + (here ? "0.13)" : "0.05)");
          ctx.fillRect(0, y1, mediaSize.width, y2 - y1);
          ctx.strokeStyle = COLOR.line; ctx.lineWidth = 1; ctx.setLineDash([2, 6]); ctx.beginPath(); ctx.moveTo(0, y1); ctx.lineTo(mediaSize.width, y1); ctx.stroke(); ctx.setLineDash([]);
          ctx.font = `${here ? "600 " : ""}11px ${css("--font")}`; ctx.fillStyle = here ? COLOR.brass : COLOR.muted; ctx.textBaseline = "middle"; ctx.textAlign = "right";
          const does = cv.rule === "trend" ? "" : name === "LOW" ? ": long" : name === "HIGH" ? ": short" : ": ITF decides";
          ctx.fillText(`Curve ${name.toLowerCase()}${does}${here ? " (price is here)" : ""}`, mediaSize.width - 8, (y1 + y2) / 2);
          ctx.textAlign = "left";
        }
      }
      for (const z of this.zones) {
        const y1 = this.series.priceToCoordinate(z.proximal), y2 = this.series.priceToCoordinate(z.distal);
        if (y1 == null || y2 == null) continue;
        const x1 = this.xOf(z.created);
        if (x1 == null) continue;
        let x2 = mediaSize.width;
        if (z.ended != null) {   // a zone that ended before the loaded candles is not drawn
          if (this.firstTime != null && z.ended < this.firstTime) continue;
          const xe = this.xOf(z.ended);
          if (xe == null) continue;
          x2 = xe;
        }
        if (x2 <= x1) continue;
        const old = isOld(z), picked = z.id === this.selected, other = Boolean(z.tf) && !z.solid;
        ctx.fillStyle = (old ? FILL.old : FILL[z.side]) + (picked ? "0.34)" : old ? "0.10)" : other ? "0.10)" : "0.18)");
        ctx.fillRect(x1, Math.min(y1, y2), x2 - x1, Math.abs(y2 - y1));
        ctx.strokeStyle = old ? COLOR.muted : COLOR[z.side];
        ctx.lineWidth = picked ? 2 : 1;
        ctx.setLineDash(other ? [8, 4] : []); ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y1); ctx.stroke();   // proximal
        ctx.setLineDash(other ? [2, 4] : [4, 4]); ctx.beginPath(); ctx.moveTo(x1, y2); ctx.lineTo(x2, y2); ctx.stroke();  // distal
        ctx.setLineDash([]);
        if (!z.tf && this.scores && z.score != null && Math.abs(y2 - y1) >= 3) {     // "All zones": the score on each band
          ctx.font = `600 11px ${css("--font")}`; ctx.fillStyle = old ? COLOR.muted : COLOR[z.side]; ctx.textBaseline = "bottom";
          ctx.fillText(`Score ${z.score}`, Math.max(x1, 0) + 6, Math.min(y1, y2) - 2);
        }
        if (z.tf) {
          ctx.font = `600 11px ${css("--font")}`; ctx.fillStyle = COLOR[z.side]; ctx.textBaseline = "top";
          ctx.fillText(`${TF_LABEL[z.tf]} ${z.side}${z.note ? ": " + z.note : ""}`, Math.max(x1, 0) + 6, Math.min(y1, y2) + 3);
        }
      }
      const p = this.plan;
      if (p) {      // risk and reward boxes, under the candles
        const x1 = Math.max(this.xOf(p.from) ?? 0, 0), yE = this.series.priceToCoordinate(p.entry), yS = this.series.priceToCoordinate(p.stop), yT = this.series.priceToCoordinate(p.target);
        if (yE != null && yS != null && p.show.entry && p.show.stop) { ctx.fillStyle = FILL_PLAN.risk; ctx.fillRect(x1, Math.min(yE, yS), mediaSize.width - x1, Math.abs(yS - yE)); }
        if (yE != null && yT != null && p.show.entry && p.show.target) { ctx.fillStyle = FILL_PLAN.reward; ctx.fillRect(x1, Math.min(yE, yT), mediaSize.width - x1, Math.abs(yT - yE)); }
      }
    });
  }
  drawPlan(target) {
    const p = this.plan;
    if (!this.chart) return;
    if (this.scan) target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {     // the scan date: a vertical line with its date
      const x = this.xOf(this.scan.time);
      if (x == null) return;
      ctx.strokeStyle = "#5aa2e6"; ctx.lineWidth = 1.5; ctx.setLineDash([6, 4]);
      ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, mediaSize.height); ctx.stroke(); ctx.setLineDash([]);
      ctx.font = `600 11px ${css("--font")}`; ctx.textBaseline = "middle";
      const w = ctx.measureText(this.scan.label).width + 10, left = Math.min(Math.max(x + 4, 4), mediaSize.width - w - 4);
      ctx.fillStyle = "#5aa2e6"; ctx.fillRect(left, 6, w, 18);
      ctx.fillStyle = "#101a26"; ctx.fillText(this.scan.label, left + 5, 16);
    });
    if (!p) return;
    target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
      const x1 = Math.max(this.xOf(p.from) ?? 0, 0);
      const line = (price, color, width, dash, label) => {
        const y = this.series.priceToCoordinate(price);
        if (y == null) return;
        ctx.strokeStyle = color; ctx.lineWidth = width; ctx.setLineDash(dash);
        ctx.beginPath(); ctx.moveTo(x1, y); ctx.lineTo(mediaSize.width, y); ctx.stroke(); ctx.setLineDash([]);
        ctx.font = `600 12px ${css("--font")}`; ctx.textBaseline = "middle";
        const text = `${label} ${fmtPrice(price)}`, w = ctx.measureText(text).width + 12, x = Math.max(x1 - w - 6, 4);   // just left of where the zone starts
        ctx.fillStyle = color; ctx.fillRect(x, y - 10, w, 20);
        ctx.fillStyle = "#101a26"; ctx.fillText(text, x + 6, y + 1);
      };
      if (p.show.target) line(p.target, COLOR.demand, 1.5, [6, 4], "Target");
      if (p.show.stop) line(p.stop, COLOR.supply, 3, [], "Stop loss");
      if (p.show.entry) line(p.entry, COLOR.brass, 3, [], p.entryLabel || "Entry");
    });
  }
}
const bands = new ZoneBands();
series.attachPrimitive(bands);

function notice(text) { $("notice").classList.toggle("show", Boolean(text)); $("notice").firstElementChild.textContent = text || ""; }

// ---- rendering ---------------------------------------------------------
const isOld = (z) => z.status === "violated" || z.status === "consumed" || z.status === "superseded";
function visibleZones() {
  const sides = { demand: $("show-demand").checked, supply: $("show-supply").checked };
  const own = $("show-old").checked ? state.zones.concat(state.closed) : state.zones;
  return own.concat(state.replay ? [] : state.overlay).filter((z) => sides[z.side]);
}
// Zones drawn for the selected setup only: its mode's HTF zone and every higher zone in contact. Not in the list.
function contactZones() { return state.contacts.filter((z) => !state.contactOff.has(z.tf) && ($("show-" + z.side) || {}).checked !== false); }

// The zones the mode's result names: nearest HTF demand and supply, the LTF entry zone, and (if asked) zones in contact.
function resultZones() {
  const r = state.result;
  if (!r || state.replay) return [];
  const out = [], on = (id) => $(id).checked, seen = new Set();
  const add = (z, note, solid) => { if (z) out.push({ id: `r${z.zone_id}`, tf: z.timeframe, side: z.side, proximal: z.proximal, distal: z.distal, status: "fresh",
    created: z.created_ts ? toEpoch(z.created_ts) : 0, ended: null, note, solid }); };
  const sc = (z) => (z && on("l-scores") ? `, score ${z.score} (${LABEL_TEXT[z.label] || z.label})` : "");
  if (on("l-demand")) add(r.demand_zone, `nearest HTF demand${sc(r.demand_zone)}`);
  if (on("l-supply")) add(r.supply_zone, `nearest HTF supply${sc(r.supply_zone)}`);
  if (on("l-ltf")) add(r.ltf_zone, `LTF entry zone${sc(r.ltf_zone)}`, true);
  if (on("l-ltf")) for (const k of r.skipped_ltf_zones || []) add({ ...k, zone_id: `x${k.zone_id}` }, `not traded: ${k.why}`);
  const least = Number($("l-ltf-other").value);       // other LTF zones at or above this score; 0 is off
  if (least) for (const o of r.other_ltf_zones || []) if (o.score >= least) add(o.zone, `other LTF zone, score ${o.score}${o.primary_entry ? `, ${ENTRY_TEXT[o.primary_entry]}` : ""}`);
  if (on("l-contact")) for (const c of [...(r.contact_ltf || []), ...(r.contact_htf || [])]) {
    if (seen.has(c.zone_id)) continue;
    seen.add(c.zone_id);
    out.push({ id: `k${c.zone_id}`, tf: c.tf, side: r.trade_side === "SELL" ? "supply" : "demand", proximal: c.proximal, distal: c.distal, status: "fresh", created: 0, ended: null,
      note: c.relation === "touch" ? `touch at ${fmtPrice(c.at)}` : c.relation });
  }
  return out;
}
// The plan of the mode's signal, drawn when no zone is selected. Entry, stop loss and target each have their own switch.
function applyResultPlan() {
  const r = state.result, s = r && r.signal;
  if (!s || s.signal === "NO_TRADE" || (s.entry == null && s.watch == null) || state.replay) { clearLines(); return; }
  const z = r.ltf_zone, name = ENTRY_TEXT[s.primary_entry] || "Entry";
  // Entry types 2 and 3 have no price until their candles come. Until then the lines are drawn from the proximal line and say so.
  const early = s.entry == null, entry = early ? s.watch : s.entry, sign = z && z.side === "supply" ? -1 : 1;
  const target = early ? entry + sign * (s.rr || state.targetRR) * Math.abs(entry - s.stoploss) : s.target;
  drawPlan({ entry, stop: s.stoploss, target }, { created: z && z.created_ts ? toEpoch(z.created_ts) : state.firstTime },
    { entry: $("l-entry").checked, stop: $("l-stop").checked, target: $("l-target").checked },
    early ? `${name} waiting (proximal line)` : `${name}${s.waiting ? " (waiting)" : ""}`);
}

function render() {
  const showAll = $("l-all").checked || Boolean(state.replay);
  const zones = showAll ? visibleZones() : [];
  const r = state.result;
  bands.scores = $("l-scores").checked;
  bands.set(zones.concat(contactZones(), resultZones()), state.selected, state.firstTime);
  bands.setCurve(r && !state.replay && $("l-curve").checked && r.demand_zone && r.supply_zone && r.supply_zone.proximal > r.demand_zone.proximal
    ? { demand: r.demand_zone.proximal, supply: r.supply_zone.proximal, band: r.curve, rule: r.direction_rule || "location" } : null);
  if (state.selected == null) applyResultPlan();
  bands.setScan(r && r.scan_ts && $("l-scan").checked && !state.replay ? toEpoch(r.scan_ts) : null, r && r.scan_ts ? `Scan date ${r.scan_date}` : "");
  $("all-zones").hidden = !showAll; $("selected-wrap").hidden = !showAll;
  const box = $("zones");
  box.replaceChildren();
  if (!showAll) return;
  const sorted = zones.slice().sort((a, b) => Number(isOld(a)) - Number(isOld(b)) || b.created - a.created);
  for (const z of sorted) {
    box.append(el("button", { class: "zone" + (isOld(z) ? " old" : ""), "aria-pressed": String(z.id === state.selected), onclick: () => selectZone(z.id) },
      el("span", { class: "dot " + z.side }),
      el("span", { class: "lvl" }, `${z.tf ? TF_LABEL[z.tf] + ": " : ""}${fmtPrice(z.proximal)} to ${fmtPrice(z.distal)}`),
      el("span", {}, `Score ${z.score}`),
      el("span", { class: "meta" }, `${z.side === "demand" ? "Demand" : "Supply"}, ${z.status}, ${z.touches} ${z.touches === 1 ? "test" : "tests"}, from ${fmtTime(z.created, state.tf !== "1d" && !/[wmoy]$/.test(state.tf))}`)));
  }
}

// Collect, for the selected zone, each mode's HTF zone and every higher zone in contact with either of them.
function setContacts(z, plans) {
  const seen = new Map();
  const add = (c, note) => { if (!c || c.zone_id === z.id) return; const k = c.zone_id ?? `${c.tf}:${c.proximal}`;
    if (!seen.has(k)) seen.set(k, { id: `c${k}`, tf: c.tf, side: z.side, proximal: c.proximal, distal: c.distal, status: "fresh", created: 0, ended: null, note }); };
  for (const p of plans) {
    const c = p.context || {}, sh = p.shared && !Array.isArray(p.shared) ? p.shared : {};
    const hz = z.side === "demand" ? c.demand : c.supply;
    if (hz) add({ ...hz, tf: c.htf }, `${MODE_LABEL[p.mode] || p.mode} HTF zone`);
    for (const x of sh.ltf || []) add(x, x.relation === "touch" ? `touch at ${fmtPrice(x.at)}` : x.relation);
    for (const x of sh.htf || []) add(x, `${x.relation} with the HTF zone`);
  }
  state.contacts = [...seen.values()];
  renderContactPicker();
}
function renderContactPicker() {
  const box = $("contact-tfs"), tfs = TIMEFRAMES.filter((tf) => state.contacts.some((z) => z.tf === tf));
  box.replaceChildren();
  $("contact-wrap").hidden = !tfs.length;
  for (const tf of tfs) {
    const input = Object.assign(el("input", { type: "checkbox", value: tf }), { checked: !state.contactOff.has(tf) });
    input.onchange = () => { if (input.checked) state.contactOff.delete(tf); else state.contactOff.add(tf); render(); };
    box.append(el("label", { class: "check" }, input, TF_LABEL[tf]));
  }
}

function clearLines() { for (const l of state.lines) series.removePriceLine(l); state.lines = []; bands.setPlan(null); }
// p has entry, stop and target; z is the zone it belongs to. Without a stored plan the zone's own lines are used:
// entry at the proximal line, stop at the distal line, target at the usual multiple of the zone width.
function drawPlan(p, z, show = { entry: true, stop: true, target: true }, entryLabel = "Entry") {
  clearLines();
  if (!p && z) {
    const sign = z.side === "demand" ? 1 : -1;
    p = { entry: z.proximal, stop: z.distal, target: z.proximal + sign * state.targetRR * Math.abs(z.proximal - z.distal) };
  }
  if (!p || p.entry == null) return;
  // On the price axis only the coloured price tags are kept; the lines and their names are drawn by the highlight.
  const add = (price, color) => state.lines.push(series.createPriceLine({ price, title: "", color, lineVisible: false, axisLabelVisible: true }));
  if (show.entry) add(p.entry, COLOR.brass);
  if (show.stop) add(p.stop, COLOR.supply);
  if (show.target) add(p.target, COLOR.demand);
  bands.setPlan({ entry: p.entry, stop: p.stop, target: p.target, from: z ? z.created : state.firstTime, show, entryLabel });
}

// What happened to one entry plan after the scan date.
function outcomeText(o) {
  const when = (t) => String(t).replace("T", " ").slice(0, 16), r = o.result_r == null ? "" : ` (${o.result_r > 0 ? "+" : ""}${o.result_r} R)`;
  if (o.status === "target hit") return `entered ${when(o.entry_ts)} at ${fmtPrice(o.entry)}, target ${fmtPrice(o.target)} hit ${when(o.exit_ts)}${r}`;
  if (o.status === "stopped") return `entered ${when(o.entry_ts)} at ${fmtPrice(o.entry)}, stopped out ${when(o.exit_ts)} at ${fmtPrice(o.exit_price)}${r}`;
  if (o.status === "open") return `entered ${when(o.entry_ts)} at ${fmtPrice(o.entry)}, still open at the last stored candle${r}`;
  return `${o.status}: ${o.note}`;
}
// Entry and exit markers of each plan that was taken after the scan date.
function outcomeMarkers(r) {
  const out = [], buy = r.ltf_zone && r.ltf_zone.side === "demand", short = PLAN_SHORT;
  for (const [name, o] of Object.entries(r.outcomes || {})) {
    if (!o.entry_ts) continue;
    const t1 = snapTime(toEpoch(o.entry_ts)) ?? toEpoch(o.entry_ts), t2 = snapTime(toEpoch(o.exit_ts)) ?? toEpoch(o.exit_ts);
    out.push({ time: t1, position: buy ? "belowBar" : "aboveBar", shape: buy ? "arrowUp" : "arrowDown", color: COLOR.brass, text: `${short[name]} entry` });
    if (o.status !== "open") out.push({ time: t2, position: buy ? "aboveBar" : "belowBar", shape: "circle", color: o.status === "target hit" ? COLOR.demand : COLOR.supply,
      text: `${short[name]} ${o.status === "target hit" ? "target" : "stopped"}` });
  }
  return out;
}

// The panel beside the chart: everything the mode's result says about this coin.
function renderResult() {
  const r = state.result, box = $("result-panel");
  if (!r) { box.replaceChildren(el("p", { class: "empty" }, "No result for this coin yet. It appears once the coin has been downloaded and scanned.")); return; }
  const parts = (z) => `freshness ${z.freshness} + strength ${z.strength}${z.strength_label ? ` (${z.strength_label})` : ""} + base ${z.base}${z.score > z.freshness + z.strength + z.base ? " + EMA 1" : ""}`;
  const s = r.signal, zl = (z) => (z ? `${fmtPrice(z.proximal)} to ${fmtPrice(z.distal)}. Score ${z.score} (${LABEL_TEXT[z.label] || z.label}) = ${parts(z)}. ${testsText(z).replace(/^./, (c) => c.toUpperCase())}${z.distal_from && z.distal_from !== "base" ? `. Distal line from ${DISTAL_TEXT[z.distal_from]}` : ""}` : "none near the price");
  const plan = (p) => (!p ? "not available" : p.entry == null ? p.note : `${fmtPrice(p.entry)}, ${fmtPrice(p.stoploss)}, ${fmtPrice(p.target)}${p.triggered ? " (entered)" : " (waiting)"}`);
  const taken = (k) => (s[k] && s[k].entry_type === s.primary_entry ? " (taken at this score)" : "");
  const head = el("p", { class: "big " + (s.signal === "BUY" ? "demand-t" : s.signal === "SELL" ? "supply-t" : "") }, s.signal === "BUY" ? "Long" : s.signal === "SELL" ? "Short" : "No trade");
  const rows = [["Mode", modeText(r.mode)], ["Price", r.cmp == null ? "none" : fmtPrice(r.cmp)], ["Curve", lower(r.curve)], ["Market state", lower(r.market_state)],
    ["HTF trend", lower(r.htf_trend)], [`ITF trend (${TF_LABEL[r.itf]})`, lower(r.itf_trend)],
    ["Side decided by", (r.direction_rule || "location") === "location" && r.market_state === "NORMAL" && ["VERY LOW", "LOW", "HIGH", "VERY HIGH"].includes(r.curve) ? "the curve (location beats trend)" : r.market_state && r.market_state !== "NORMAL" ? "the expansion rule" : "the ITF trend"], [`${TF_LABEL[r.htf]} demand`, zl(r.demand_zone)], [`${TF_LABEL[r.htf]} supply`, zl(r.supply_zone)]];
  if (r.ltf_zone) rows.push([`${TF_LABEL[r.ltf]} entry zone`, zl(r.ltf_zone)], ["State", lower(r.zone_state)],
    ["Trade score", s.signal === "NO_TRADE" ? `${s.score}` : `${s.score}: takes ${lower(ENTRY_TEXT[s.primary_entry])}`],
    ["Entry, stop loss, target", s.signal === "NO_TRADE" ? "none" : s.entry == null ? `no entry price yet. Zone at ${fmtPrice(s.watch)}, stop loss ${fmtPrice(s.stoploss)}; waiting until ${ENTRY_HOW[s.primary_entry]}`
      : `${fmtPrice(s.entry)}, ${fmtPrice(s.stoploss)}, ${fmtPrice(s.target)} (${lower(ENTRY_TEXT[s.primary_entry])})`],
    ...ENTRY_KEYS.map(([k, label]) => [label + taken(k), plan(s[k])]),
    ["Contact of the entry zone", contactText(r.contact_ltf)], ["Contact of the HTF zone", contactText(r.contact_htf)]);
  if (s.signal === "NO_TRADE") rows.push(["Why no trade", s.reason || "not given"]);
  if (r.scan_ts) {
    rows.unshift(["As of", `${r.scan_date} (last ${TF_LABEL[r.ltf]} candle ${String(r.scan_ts).replace("T", " ").slice(0, 16)})`]);
    if (r.ltf_zone && s.signal !== "NO_TRADE") for (const [name, o] of Object.entries(r.outcomes || {})) rows.push([`After the scan: ${lower(PLAN_NAME[name] || name)}`, outcomeText(o)]);
  }
  box.replaceChildren(head, kv(rows));
}

const targetNote = (p) => (p.target_source === "fixed_3r" ? " (3 times risk, no opposing zone)" : p.target_source === "capped" ? ` (capped at ${p.reward_risk} times risk, opposing zone is further)` : "");
function kv(pairs) { const dl = el("dl", { class: "kv" }); for (const [k, v] of pairs) dl.append(el("dt", {}, k), el("dd", {}, String(v))); return dl; }

function setMarkers(list) { state.markers = list.slice().sort((a, b) => a.time - b.time); markerLayer.setMarkers(state.markers); }
function eventMarker(type, side, time) {
  const demand = side === "demand";
  const base = { time, position: demand ? "belowBar" : "aboveBar", color: COLOR[side] };
  if (type === "created") return { ...base, shape: "circle", text: "New zone" };
  if (type === "touched") return { ...base, shape: demand ? "arrowUp" : "arrowDown", text: "Test" };
  if (type === "violated") return { ...base, shape: "square", color: COLOR.muted, text: "Violated" };
  if (type === "consumed") return { ...base, shape: "square", color: COLOR.muted, text: "Used up" };
  if (type === "entry2" || type === "entry3") return { ...base, shape: demand ? "arrowUp" : "arrowDown", color: COLOR.brass, text: type === "entry2" ? "Entry type 2" : "Entry type 3" };
  return null;
}
const EVENT_TEXT = { created: "New zone", touched: "Test: price reached the zone", touched_again: "Test counted again: another candle reached the zone", strength: "A second exciting candle left the zone (strength 2)", closed_inside: "A candle closed inside the zone",
  entry2: "Entry type 2: the next candle opened inside the zone", entry3: "Entry type 3: the next candle closed back out of the zone",
  consumed: "Used up", violated: "Violated: a candle closed beyond the distal line", plan_changed: "Plan changed", alert_approaching: "Alert: approaching", alert_triggered: "Alert: entry reached" };
function logLine(time, side, text) {
  const li = el("li", {}, el("time", {}, fmtTime(time)), el("span", { class: side + "-t" }, side === "demand" ? "Demand " : "Supply "), text);
  $("log").prepend(li);
}

async function selectZone(id) {
  state.selected = state.selected === id ? null : id;
  state.contacts = []; renderContactPicker();
  render();
  const box = $("detail");
  if (state.selected == null) { state.contacts = []; renderContactPicker(); applyResultPlan(); box.replaceChildren(el("p", { class: "empty" }, "Select a zone to see its score, plan and history.")); if (!state.replay) setMarkers([]); return; }
  if (state.replay) {
    const z = visibleZones().find((x) => x.id === id);
    drawPlan(z, z);
    box.replaceChildren(kv([["Pattern", z.pattern], ["Score", `${z.score} of 8, ${LABEL_TEXT[z.label] || ""}`], ["Status", z.status], ["Tests", z.touches], ["Base candles", z.base_count],
      ["Can be used for", z.execution ? "location and execution" : "location only"],
      ["Entry type 1 (proximal line)", fmtPrice(z.entry)], ["Stop", fmtPrice(z.stop)], ["Target", fmtPrice(z.target)], ["Reward to risk", z.reward_risk]]));
    return;
  }
  try {
    const d = await api(`/api/zone/${id}`);
    const z = d.zone, current = d.plans.filter((x) => x.is_current && x.mode), p = current.find((x) => x.status === "pending") || current[0];
    drawPlan(p, visibleZones().find((x) => x.id === id) || { side: z.side, proximal: z.proximal, distal: z.distal, created: toEpoch(z.created_ts) });
    const role = z.legin_explosive && (z.pattern === "DBR" || z.pattern === "RBD") ? "location and execution" : (z.pattern === "DBR" || z.pattern === "RBD" ? "location only (its leg-in is not an exciting candle)" : "location only (a continuation pattern; entries use reversal patterns)");
    const rows = [["Pattern", z.pattern], ["Score", `${z.score} of 8, ${LABEL_TEXT[z.label] || "not scored"}`], ["Status", z.status],
      ["Tests", `${z.touches}${z.in_visit ? " (price is at the zone now)" : ""}`], ["Base candles", z.base_count],
      ["Leg-out", `${Math.round((z.legout_body || 0) * 100)}% body${z.has_gap ? ", with a gap" : ""}${z.legout_count >= 2 ? ", followed by a second exciting candle" : ""}`],
      ["Distal line from", DISTAL_TEXT[z.distal_from] || "the base candles"], ["Can be used for", role]];
    setContacts(z, current);
    if (!current.length) rows.push(["Plans", "none: no mode allows a trade from this zone now"]);
    for (const x of current) {
      const c = x.context || {};
      rows.push([`${MODE_LABEL[x.mode] || x.mode} mode`, `${x.status}. Curve ${lower(c.curve)}, ${TF_LABEL[c.itf] || "ITF"} trend ${lower(c.itf_trend)}, ${lower(c.market_state)}`]);
    }
    for (const x of current) {
      const sh = x.shared && !Array.isArray(x.shared) ? x.shared : {};
      rows.push([`${MODE_LABEL[x.mode] || x.mode}: contact of this zone`, contactText(sh.ltf)], [`${MODE_LABEL[x.mode] || x.mode}: contact of its HTF zone`, contactText(sh.htf)]);
    }
    if (p) {
      const pl = p.plans || {};
      const early = p.status === "pending" && p.entry_type && p.entry_type !== "TYPE_1";
      rows.push([`Plan: ${lower(ENTRY_TEXT[p.entry_type] || "entry")}`, `${p.status}. Entry ${fmtPrice(p.entry)}, stop ${fmtPrice(p.stop)}, target ${fmtPrice(p.target)}${early ? ". The entry has not happened yet: these are measured from the proximal line and change when it does" : ""}`]);
      for (const [k, label] of ENTRY_KEYS) if (pl[k]) rows.push([label, pl[k].entry == null ? pl[k].note : `entry ${fmtPrice(pl[k].entry)}, stop ${fmtPrice(pl[k].stoploss)}, target ${fmtPrice(pl[k].target)}. ${pl[k].note}`]);
    }
    box.replaceChildren(kv(rows));
    render();                      // now that the contact zones are known, draw them
    $("log").replaceChildren();
    const marks = [];
    for (const e of d.events) {
      const t = toEpoch(e.ts);
      logLine(t, z.side, `${EVENT_TEXT[e.event_type] || e.event_type}${e.price != null ? " at " + fmtPrice(e.price) : ""}`);
      const m = eventMarker(e.event_type, z.side, t);
      if (m && state.firstTime != null && t >= state.firstTime) marks.push(m);
    }
    setMarkers(marks);
  } catch (e) { box.replaceChildren(el("p", { class: "empty" }, `Could not load this zone. ${e.message}`)); }
}

// ---- live mode ---------------------------------------------------------
const enc = encodeURIComponent;
async function loadLive(soft = false) {
  // soft: a quiet refresh after a scan (new candles and zones), keeping the selected zone and the part of the chart in view.
  const keepRange = soft ? chart.timeScale().getVisibleLogicalRange() : null;
  if (!soft) {
    state.selected = null; state.closed = []; state.zones = []; state.overlay = []; state.contacts = []; renderContactPicker(); clearLines(); setMarkers([]); $("log").replaceChildren();
    $("detail").replaceChildren(el("p", { class: "empty" }, "Select a zone to see its score, plan and history."));
    renderOverlayPicker();
  }
  live.bar = null;
  if (!state.key) { series.setData([]); render(); notice("No coins in your list yet. Add them under Settings, then sync from the Data page."); return; }
  try {
    const others = [...state.extra].filter((tf) => tf !== state.tf);
    const BAR = { "1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "2h": 7200, "4h": 14400, "1d": 86400, "1w": 604800, "1mo": 2678400, "3mo": 8035200, "6mo": 16070400, "1y": 31622400 };
    let range = "&limit=1500";
    if (state.scan) {         // candles from well before the scan date onwards
      const back = BAR[state.tf] * (BAR[state.tf] < 86400 ? 1600 : 400);
      range = `&start=${new Date(Date.parse(state.scan + "T00:00:00Z") - back * 1000).toISOString().slice(0, 19)}&limit=7000&first=true`;
    }
    // One request for the candles, the zones and the mode's result.
    const got = await api(`/api/chart/${enc(state.key)}?tf=${state.tf}&mode=${state.mode}${range}&old=${$("show-old").checked}&overlays=${others.join(",")}&result=${!state.scan}`);
    const candles = got.candles, zones = got.zones.concat(got.old);
    state.result = state.scan
      ? await post("/api/scancheck", { symbols: [state.key], mode: state.mode, scan_date: state.scan }).then((x) => x.rows[0]).catch(() => null)
      : got.result;
    renderResult();
    const data = candles.map((c) => ({ time: toEpoch(c.ts), open: c.open, high: c.high, low: c.low, close: c.close }));
    setPrecision(data);
    series.setData(data);
    state.lastBars = data;
    state.firstTime = data.length ? data[0].time : null;
    if (!data.length) { render(); notice(`No ${TF_LABEL[state.tf]} candles stored for this coin. Open the Data page and sync it.`); return; }
    notice("");
    const all = zones.map((z) => ({ id: z.id, side: z.side, pattern: z.pattern, proximal: z.proximal, distal: z.distal, status: z.status, score: z.score,
      touches: z.touches, base_count: z.base_count, label: z.label, created: toEpoch(z.created_ts), ended: isOld(z) && z.updated_ts ? toEpoch(z.updated_ts) : null }));
    state.zones = all.filter((z) => !isOld(z));
    state.closed = all.filter(isOld);
    // Active zones from the other ticked timeframes, drawn dashed with their timeframe written on them.
    state.overlay = others.flatMap((tf) => (got.overlays[tf] || []).map((z) => ({ id: z.id, tf, side: z.side, pattern: z.pattern, proximal: z.proximal,
      distal: z.distal, status: z.status, score: z.score, touches: z.touches, base_count: z.base_count, label: z.label, created: toEpoch(z.created_ts), ended: null })));
    const at = state.scan && state.result && state.result.scan_ts ? data.findIndex((b) => b.time >= toEpoch(state.result.scan_ts)) : -1;
    if (keepRange) chart.timeScale().setVisibleLogicalRange(keepRange);
    else if (at >= 0) chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, at - 90), to: Math.min(data.length + 8, at + 110) });   // centred on the scan date
    else chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, data.length - 160), to: data.length + 8 });
    if (state.scan && state.result) setMarkers(outcomeMarkers(state.result));
    if (!state.play && !state.replay) { $("replay-state").textContent = state.scan ? `As of ${state.scan}` : "Live chart"; $("replay-hint").hidden = Boolean(state.scan); }
    const back = data[Math.max(0, data.length - 120)];
    if (!$("replay-date").value) $("replay-date").value = fmtTime(back.time, false);
    render();
    startForming();
  } catch (e) { if (!soft) { series.setData([]); render(); notice(`Could not load the chart. ${e.message}`); } }
}

// ---- live price on the chart -------------------------------------------
// The candle that is still forming is drawn faintly and follows the live mark price every second; the axis shows the
// live price. It is only drawn: the scanner never uses a forming candle. When a scan has brought new closed candles
// for this coin, the chart refreshes quietly, keeping the zone you selected and the part of the chart you are looking at.
const live = { bar: null, end: 0, reload: null, key: null, tf: null };
const liveOn = () => !state.scan && !state.replay && !state.play;
function drawForming() {
  const b = live.bar;
  if (!b) return;
  const col = b.close >= b.open ? "rgba(201,211,221,0.45)" : "rgba(79,100,123,0.6)";
  try { series.update({ ...b, color: col, borderColor: col, wickColor: col }); } catch (e) { /* older than the last stored candle */ }
}
async function startForming() {
  live.bar = null;
  if (!liveOn() || !state.key) return;
  const key = state.key, tf = state.tf;
  try {
    const f = (await api(`/api/forming/${enc(key)}?tf=${tf}`)).forming;
    if (!f || key !== state.key || tf !== state.tf || !liveOn()) return;
    const last = state.lastBars.length ? state.lastBars[state.lastBars.length - 1].time : 0;
    const t = toEpoch(f.ts);
    if (t <= last) return;
    live.bar = { time: t, open: f.open, high: f.high, low: f.low, close: f.close };
    live.end = toEpoch(f.close_ts); live.key = key; live.tf = tf;
    const p = livePrices[key];
    if (p != null) { live.bar.close = p; live.bar.high = Math.max(live.bar.high, p); live.bar.low = Math.min(live.bar.low, p); }
    drawForming();
  } catch (e) { /* Binance not reachable: the chart simply has no forming candle */ }
}
function onLivePrice(p) {
  if (!live.bar || !liveOn() || live.key !== state.key || live.tf !== state.tf) return;
  if (shiftEpoch(Date.now() / 1000) >= live.end) { live.bar = null; softReload(15000); return; }   // it has closed: the scan brings it in
  live.bar.close = p; live.bar.high = Math.max(live.bar.high, p); live.bar.low = Math.min(live.bar.low, p);
  drawForming();
}
function softReload(ms) {
  if (live.reload) return;
  live.reload = setTimeout(() => { live.reload = null; if (liveOn()) loadLive(true); }, ms);
}
const livePrices = {};
function connectLive() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/live`);
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.type === "prices") { Object.assign(livePrices, m.data); if (state.key && m.data[state.key] != null) onLivePrice(m.data[state.key]); }
    else if (m.type === "scan" && (!m.coins || m.coins.includes(state.key))) softReload(1500);
  };
  ws.onclose = () => setTimeout(connectLive, 3000);
}

// ---- replay mode -------------------------------------------------------
function replayUi(on) {
  $("transport").classList.toggle("live-replay", on);
  for (const id of ["replay-play", "replay-step", "replay-next", "replay-speed", "replay-exit"]) $(id).disabled = !on;
  $("replay-start").disabled = on; $("replay-date").disabled = on;
  if (!on) { $("replay-state").textContent = state.scan ? `As of ${state.scan}` : "Live chart"; $("replay-play").textContent = "Play"; state.playing = false; }
  $("replay-hint").hidden = on || Boolean(state.scan);
}
function replayStatus(remaining, finished) {
  $("replay-state").textContent = finished ? "Replay finished" : `Replay, ${remaining} ${remaining === 1 ? "bar" : "bars"} left`;
}
function send(obj) { if (state.replay && state.replay.readyState === WebSocket.OPEN) state.replay.send(JSON.stringify(obj)); }

// ---- replay from the scan date ------------------------------------------------------------
// The picture stays as it was on the scan date: only the ticked items are drawn and nothing is recalculated.
// Candles after the scan date appear one at a time, and each plan's entry, target and stop loss are marked when reached.
function nearestBar(bars, t) {
  let lo = 0, hi = bars.length - 1;
  if (t >= bars[hi].time) return bars[hi].time;
  while (lo < hi) { const mid = (lo + hi) >> 1; if (bars[mid].time < t) lo = mid + 1; else hi = mid; }
  return bars[lo].time;
}
function replayEvents(r, bars) {
  const out = [], buy = r.ltf_zone && r.ltf_zone.side === "demand", side = buy ? "demand" : "supply";
  const title = PLAN_NAME, short = PLAN_SHORT;
  for (const [name, o] of Object.entries(r.outcomes || {})) {
    if (!o.entry_ts) continue;
    const t1 = nearestBar(bars, toEpoch(o.entry_ts));
    out.push({ time: t1, side, text: `${title[name]} entry at ${fmtPrice(o.entry)}, stop loss ${fmtPrice(o.stop)}, target ${fmtPrice(o.target)}`,
      marker: { time: t1, position: buy ? "belowBar" : "aboveBar", shape: buy ? "arrowUp" : "arrowDown", color: COLOR.brass, text: `${short[name]} entry` } });
    if (o.status === "open") continue;
    const t2 = nearestBar(bars, toEpoch(o.exit_ts)), win = o.status === "target hit";
    out.push({ time: t2, side, text: `${title[name]}: ${win ? "target hit" : "stop loss hit"} at ${fmtPrice(o.exit_price)} (${o.result_r > 0 ? "+" : ""}${o.result_r} R)`,
      marker: { time: t2, position: buy ? "aboveBar" : "belowBar", shape: "circle", color: win ? COLOR.demand : COLOR.supply, text: `${short[name]} ${win ? "target" : "stop loss"}` } });
  }
  return out.sort((x, y) => x.time - y.time);
}
function playStatus() {
  const p = state.play, left = p.bars.length - 1 - p.idx;
  $("replay-state").textContent = left ? `Replay from ${state.scan}, ${left} ${left === 1 ? "bar" : "bars"} left` : "Replay finished";
}
function playStep() {            // one more candle; returns true when an entry or exit was reached on it
  const p = state.play;
  if (!p || p.idx >= p.bars.length - 1) { finishPlay(); return false; }
  const bar = p.bars[++p.idx];
  series.update(bar);
  let hit = false;
  const marks = state.markers.slice();
  while (p.shown < p.events.length && p.events[p.shown].time <= bar.time) {
    const e = p.events[p.shown++];
    logLine(e.time, e.side, e.text); marks.push(e.marker); hit = true;
  }
  if (hit) setMarkers(marks);
  playStatus();
  if (p.idx >= p.bars.length - 1) finishPlay();
  return hit;
}
function pausePlay() { const p = state.play; if (p && p.timer) { clearInterval(p.timer); p.timer = null; } state.playing = false; $("replay-play").textContent = "Play"; }
function finishPlay() {
  const p = state.play;
  if (!p || p.done) return;
  pausePlay(); p.done = true; playStatus();
  const side = state.result.ltf_zone && state.result.ltf_zone.side === "supply" ? "supply" : "demand";
  for (const [name, o] of Object.entries(state.result.outcomes || {}))
    if (!o.entry_ts) logLine(p.bars[p.bars.length - 1].time, side, `${name[0].toUpperCase() + name.slice(1)} plan ${o.status}: ${o.note}`);
    else if (o.status === "open") logLine(p.bars[p.bars.length - 1].time, side, `${name[0].toUpperCase() + name.slice(1)} plan is still open at the last stored candle`);
  for (const id of ["replay-play", "replay-step", "replay-next", "replay-speed"]) $(id).disabled = true;
}
function stopPlay() { if (state.play) { pausePlay(); state.play = null; } }

async function startReplay() {
  if (!state.scan) { $("replay-state").textContent = "Choose a scan date first"; $("scan-date").focus(); return; }
  if ($("l-all").checked) return startEngineReplay();      // every zone recalculated bar by bar, as before
  const r = state.result, bars = state.lastBars;
  if (!r || !r.scan_ts) { $("replay-state").textContent = "No result for this scan date"; return; }
  const at = toEpoch(r.scan_ts);
  let cut = -1;
  for (let i = 0; i < bars.length && bars[i].time <= at; i++) cut = i;
  if (cut < 0 || cut >= bars.length - 1) { $("replay-state").textContent = "No candles stored after the scan date"; return; }
  state.selected = null;
  state.play = { bars, idx: cut, timer: null, shown: 0, events: replayEvents(r, bars), done: false };
  series.setData(bars.slice(0, cut + 1));
  setMarkers([]); $("log").replaceChildren();
  chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, cut - 110), to: cut + 70 });
  replayUi(true); playStatus(); render();
}

// The older kind of replay: the engine recalculates every zone of this one timeframe at each bar.
async function startEngineReplay() {
  const day = state.scan;
  try {
    const r = await post("/api/replay/start", { symbol: state.key, timeframe: state.tf, start: `${day}T00:00:00` });
    const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/replay/${r.session_id}`);
    state.replay = ws;
    ws.onmessage = (msg) => onReplay(shiftReplay(JSON.parse(msg.data)));
    ws.onclose = () => { if (state.replay === ws) { state.replay = null; replayUi(false); loadLive(); } };
    replayUi(true);
  } catch (e) { $("replay-state").textContent = e.message; }
}

// Replay messages carry UTC epochs; the chart shows the chosen time zone.
function shiftReplay(m) {
  const zone = (z) => ({ ...z, created: shiftEpoch(z.created), ended: z.ended == null ? null : shiftEpoch(z.ended) });
  if (m.candles) { m.candles = m.candles.map((c) => ({ ...c, time: shiftEpoch(c.time) })); setPrecision(m.candles); }
  if (m.candle) m.candle = { ...m.candle, time: shiftEpoch(m.candle.time) };
  if (m.events) m.events = m.events.map((e) => ({ ...e, time: shiftEpoch(e.time) }));
  if (m.zones) m.zones = m.zones.map(zone);
  if (m.closed) m.closed = m.closed.map(zone);
  return m;
}

// Enough decimals for the coin: 2 for BTC, 8 or more for a coin priced in fractions of a cent.
function setPrecision(bars) {
  const last = bars.length ? Math.abs(bars[bars.length - 1].close) : 1;
  const precision = Math.min(10, Math.max(2, last > 0 ? Math.ceil(-Math.log10(last)) + 4 : 2));
  series.applyOptions({ priceFormat: { type: "price", precision, minMove: Number((10 ** -precision).toFixed(precision)) } });
}

function onReplay(m) {
  if (m.type === "error") { notice(m.message); return; }
  if (m.type === "init") {
    state.selected = null; clearLines(); state.closed = []; $("log").replaceChildren(); setMarkers([]);
    series.setData(m.candles);
    state.lastBars = m.candles.slice();
    state.firstTime = m.candles.length ? m.candles[0].time : null;
    state.zones = m.zones;
    chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, m.candles.length - 120), to: m.candles.length + 40 });
    replayStatus(m.remaining, false); render();
    return;
  }
  if (m.type === "bar") {
    series.update(m.candle);
    state.lastBars.push(m.candle);
    if (state.firstTime == null) state.firstTime = m.candle.time;
    state.zones = m.zones;
    state.closed.push(...m.closed);
    const marks = state.markers.slice();
    for (const e of m.events) {
      logLine(e.time, e.side, `${EVENT_TEXT[e.type] || e.type} at ${fmtPrice(e.price)}`);
      const mk = eventMarker(e.type, e.side, e.time);
      if (mk) marks.push(mk);
    }
    if (m.events.length) setMarkers(marks);
    if (state.selected != null) { const z = visibleZones().find((x) => x.id === state.selected); if (z) drawPlan(z, z); }
    replayStatus(m.remaining, false); render();
    return;
  }
  if (m.type === "end") {
    state.playing = false; $("replay-play").textContent = "Play"; replayStatus(0, true);
    for (const id of ["replay-play", "replay-step", "replay-next", "replay-speed"]) $(id).disabled = true;
  }
}

$("replay-start").onclick = startReplay;
$("replay-play").onclick = () => {
  if (state.play) {
    if (state.play.timer) return pausePlay();
    state.playing = true; $("replay-play").textContent = "Pause";
    state.play.timer = setInterval(playStep, 1000 / Number($("replay-speed").value));
    return;
  }
  state.playing = !state.playing;
  $("replay-play").textContent = state.playing ? "Pause" : "Play";
  send(state.playing ? { cmd: "play", speed: Number($("replay-speed").value) } : { cmd: "pause" });
};
$("replay-speed").onchange = () => {
  if (state.play) { if (state.play.timer) { clearInterval(state.play.timer); state.play.timer = setInterval(playStep, 1000 / Number($("replay-speed").value)); } return; }
  if (state.playing) send({ cmd: "play", speed: Number($("replay-speed").value) });
};
$("replay-step").onclick = () => { if (state.play) { pausePlay(); playStep(); return; } state.playing = false; $("replay-play").textContent = "Play"; send({ cmd: "step" }); };
$("replay-next").onclick = () => {
  if (state.play) { pausePlay(); while (state.play && !state.play.done && !playStep()) { /* keep stepping to the next entry or exit */ } return; }
  state.playing = false; $("replay-play").textContent = "Play"; send({ cmd: "next_event" });
};
$("replay-exit").onclick = () => { if (state.play) { stopPlay(); replayUi(false); loadLive(); return; } if (state.replay) state.replay.close(); };
$("show-old").onchange = () => { if (state.replay) render(); else loadLive(); };
$("show-demand").onchange = render; $("show-supply").onchange = render;
const OTHER_LTF_KEY = "cryptoScanner.chartOtherLtf";
try { $("l-ltf-other").value = localStorage.getItem(OTHER_LTF_KEY) || ""; } catch (e) { /* storage blocked: off */ }
$("l-ltf-other").addEventListener("change", () => { try { localStorage.setItem(OTHER_LTF_KEY, $("l-ltf-other").value); } catch (e) { /* not kept */ } });
for (const id of ["l-demand", "l-supply", "l-curve", "l-ltf", "l-ltf-other", "l-entry", "l-stop", "l-target", "l-contact", "l-all", "l-scan", "l-scores"]) $(id).onchange = render;
// A scan date shows the result as it stood on that date, and replay then starts from it.
function setScan(day) {
  state.scan = day || null;
  $("scan-date").value = day || ""; $("scan-clear").disabled = !day;
  if (day) $("replay-date").value = day;
  leaveReplayThen(loadLive);
}
$("scan-date").onchange = () => setScan($("scan-date").value);
$("scan-clear").onclick = () => setScan(null);

// Tick boxes for the other timeframes whose zones can be drawn on this chart.
function renderOverlayPicker() {
  const box = $("overlay-tfs");
  box.replaceChildren();
  for (const tf of TIMEFRAMES) {
    if (tf === state.tf) continue;
    const input = Object.assign(el("input", { type: "checkbox", value: tf }), { checked: state.extra.has(tf) });
    input.onchange = () => { if (input.checked) state.extra.add(tf); else state.extra.delete(tf); if (!state.replay) loadLive(); };
    box.append(el("label", { class: "check" }, input, TF_LABEL[tf]));
  }
}

// ---- start -------------------------------------------------------------
function leaveReplayThen(fn) { if (state.play) { stopPlay(); replayUi(false); } if (state.replay) { const ws = state.replay; state.replay = null; ws.onclose = null; ws.close(); replayUi(false); } fn(); }

async function init() {
  showToken($("token"));
  const tfBox = $("timeframes");
  for (const tf of TIMEFRAMES) {
    tfBox.append(el("button", { "aria-pressed": String(tf === state.tf), onclick: (ev) => {
      state.tf = tf; $("replay-date").value = "";
      for (const b of tfBox.children) b.setAttribute("aria-pressed", String(b === ev.currentTarget));
      leaveReplayThen(loadLive);
    } }, TF_LABEL[tf]));
  }
  const press = () => [...tfBox.children].forEach((b, n) => b.setAttribute("aria-pressed", String(TIMEFRAMES[n] === state.tf)));
  try {
    const s = await api("/api/settings");
    state.names = s.symbols || {};
    state.targetRR = Number(s.target_rr) || 2;
    const sel = $("symbol"), ready = new Set(s.ready || []), group = $("group");
    const fill = () => {        // the coins of the chosen group, most traded first, that are ready
      const list = coinGroup(s.ranked || s.universe || [], group.value).filter((k) => ready.has(k));
      sel.replaceChildren(...list.map((key) => el("option", { value: key }, state.names[key] || key)));
      return list;
    };
    buildGroupSelect(group);
    let stocks = fill();
    state.key = stocks[0] || null;
    group.onchange = () => { saveGroup(group.value); stocks = fill(); if (!stocks.includes(state.key)) { state.key = stocks[0] || null; leaveReplayThen(loadLive); } else sel.value = state.key; };
    // A link from the dashboard or the backtest page can name the coin, mode, timeframe and replay date.
    const q = new URLSearchParams(location.search);
    if (q.get("symbol") && ready.has(q.get("symbol")) && !stocks.includes(q.get("symbol"))) { group.value = "all"; stocks = fill(); }   // a link to a coin outside the group
    if (stocks.includes(q.get("symbol"))) { state.key = q.get("symbol"); sel.value = state.key; }
    const modeSel = $("mode");
    for (const m of Object.keys(MODES)) modeSel.append(el("option", { value: m }, modeText(m)));
    state.mode = MODES[q.get("mode")] ? q.get("mode") : "MONTHLY";        // Monthly unless a link says otherwise
    modeSel.value = state.mode;
    state.tf = TIMEFRAMES.includes(q.get("tf")) ? q.get("tf") : MODES[state.mode][2];   // candles of the mode's LTF
    press();
    if (/^\d{4}-\d{2}-\d{2}$/.test(q.get("date") || "")) $("replay-date").value = q.get("date");
    if (/^\d{4}-\d{2}-\d{2}$/.test(q.get("scan") || "")) { state.scan = q.get("scan"); $("scan-date").value = state.scan; $("scan-clear").disabled = false; $("replay-date").value = state.scan; }
    sel.onchange = () => { state.key = sel.value; $("replay-date").value = ""; leaveReplayThen(loadLive); };
    modeSel.onchange = () => { state.mode = modeSel.value; state.tf = MODES[state.mode][2]; press(); $("replay-date").value = ""; leaveReplayThen(loadLive); };
  } catch (e) { notice(`Could not reach the server. ${e.message}`); return; }
  await loadLive();
  connectLive();
  // A link from the Paper page can name a zone: select it, showing used-up zones too if it is one of them.
  const zid = Number(new URLSearchParams(location.search).get("zone"));
  if (zid) {
    const has = () => state.zones.concat(state.closed).some((z) => z.id === zid);
    if (!has() && !$("show-old").checked) { $("show-old").checked = true; await loadLive(); }
    if (has()) selectZone(zid);
  }
}
init();