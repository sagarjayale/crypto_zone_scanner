// Trade chart on the backtest and paper pages: the candles around one trade, with its zone, entry, stop loss and target,
// and vertical lines where it was entered, where it ended, and where the backtest range ends. A paper order not yet
// entered has no entry line in time; an open position runs to the last candle and can follow the live price.
const TradeChart = (() => {
  const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const C = { demand: cssVar("--demand"), supply: cssVar("--supply"), brass: cssVar("--brass"), muted: cssVar("--muted"), line: cssVar("--line"), text: cssVar("--text") };
  const BAR = { "1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "2h": 7200, "4h": 14400, "1d": 86400, "1w": 604800, "1mo": 2678400, "3mo": 8035200, "6mo": 16070400, "1y": 31622400 };
  let chart = null, series = null, markerLayer = null, overlay = null, lines = [], bars = [], nowLine = null, boxEl = null;
  let opts = {}, drag = null;          // opts: { editable, onDrag(field, price), visible: {...}, steps: [...] } (paper popup)
  const shown = (k) => !(opts.visible && opts.visible[k] === false);

  function snap(t) {      // nearest loaded candle at or after a time
    if (!bars.length) return null;
    if (t <= bars[0].time) return bars[0].time;
    if (t >= bars[bars.length - 1].time) return bars[bars.length - 1].time;
    let lo = 0, hi = bars.length - 1;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (bars[mid].time < t) lo = mid + 1; else hi = mid; }
    return bars[lo].time;
  }

  class Overlay {
    constructor() { this.t = null; }
    attached(p) { this.chart = p.chart; this.series = p.series; this.requestUpdate = p.requestUpdate; }
    detached() { this.chart = null; }
    set(t) { this.t = t; if (this.requestUpdate) this.requestUpdate(); }
    paneViews() {
      return [{ zOrder: () => "bottom", renderer: () => ({ draw: (target) => this.boxes(target) }) },
              { zOrder: () => "top", renderer: () => ({ draw: (target) => this.marks(target) }) }];
    }
    x(time) { if (time == null) return null; const s = snap(time); return s == null ? null : this.chart.timeScale().timeToCoordinate(s); }
    boxes(target) {
      const t = this.t;
      if (!this.chart || !t) return;
      target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
        const y = (p) => this.series.priceToCoordinate(p);
        const xz = this.x(t.formed) ?? 0, xe = this.x(t.entered), xx = this.x(t.exited) ?? mediaSize.width;
        const yp = y(t.proximal), yd = y(t.distal);
        if (yp != null && yd != null && shown("zone")) {      // the zone, from the candle that formed it to the exit
          ctx.fillStyle = (t.side === "demand" ? "rgba(47,165,139," : "rgba(208,89,107,") + "0.16)";
          ctx.fillRect(xz, Math.min(yp, yd), Math.max(xx - xz, 0), Math.abs(yd - yp));
        }
        const yE = y(t.entry), yS = y(t.stop), yT = y(t.target);
        if (xe != null && yE != null && yS != null && shown("stop")) { ctx.fillStyle = "rgba(208,89,107,0.26)"; ctx.fillRect(xe, Math.min(yE, yS), Math.max(xx - xe, 2), Math.abs(yS - yE)); }
        if (xe != null && yE != null && yT != null && shown("target")) { ctx.fillStyle = "rgba(47,165,139,0.15)"; ctx.fillRect(xe, Math.min(yE, yT), Math.max(xx - xe, 2), Math.abs(yT - yE)); }
      });
    }
    marks(target) {
      const t = this.t;
      if (!this.chart || !t) return;
      target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
        const font = cssVar("--font");
        const vertical = (time, color, label, row, dash) => {
          if (time == null) return;
          const x = this.x(time);
          if (x == null) return;
          ctx.strokeStyle = color; ctx.lineWidth = 1.5; ctx.setLineDash(dash);
          ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, mediaSize.height); ctx.stroke(); ctx.setLineDash([]);
          ctx.font = `600 11px ${font}`; ctx.textBaseline = "middle";
          const w = ctx.measureText(label).width + 10, left = Math.min(Math.max(x + 4, 4), mediaSize.width - w - 4), top = 6 + row * 22;
          ctx.fillStyle = color; ctx.fillRect(left, top, w, 18);
          ctx.fillStyle = "#101a26"; ctx.fillText(label, left + 5, top + 10);
        };
        const level = (price, color, width, dash, label) => {
          const y = this.series.priceToCoordinate(price);
          if (y == null) return;
          const x1 = this.x(t.entered) ?? 0, x2 = this.x(t.exited) ?? mediaSize.width;
          ctx.strokeStyle = color; ctx.lineWidth = width; ctx.setLineDash(dash);
          ctx.beginPath(); ctx.moveTo(Math.max(x1 - 40, 0), y); ctx.lineTo(Math.min(x2 + 40, mediaSize.width), y); ctx.stroke(); ctx.setLineDash([]);
          ctx.font = `600 12px ${font}`; ctx.textBaseline = "middle";
          const text = `${label} ${fmtPrice(price)}`, w = ctx.measureText(text).width + 12, left = Math.max(x1 - 46 - w, 4);
          ctx.fillStyle = color; ctx.fillRect(left, y - 10, w, 20);
          ctx.fillStyle = "#101a26"; ctx.fillText(text, left + 6, y + 1);
        };
        if (shown("steps")) for (const st of t.steps || []) {          // lock steps of a trailing stop: thin lines
          const y = this.series.priceToCoordinate(st.price);
          if (y == null) continue;
          ctx.strokeStyle = st.done ? C.demand : C.muted; ctx.lineWidth = 1; ctx.setLineDash(st.done ? [] : [2, 4]);
          ctx.beginPath(); ctx.moveTo(this.x(t.entered) ?? 0, y); ctx.lineTo(mediaSize.width, y); ctx.stroke(); ctx.setLineDash([]);
          ctx.font = `500 10px ${font}`; ctx.textBaseline = "middle"; ctx.fillStyle = st.done ? C.demand : C.muted;
          ctx.fillText(`${st.done ? "✓ " : ""}step ${st.n}`, mediaSize.width - 60, y - 7);
        }
        const editable = opts.editable && t.result !== "win" && t.result !== "loss";
        if (shown("target")) level(t.target, C.demand, drag && drag.field === "target" ? 3 : 1.5, [6, 4], editable ? "Target ⇕" : "Target");
        if (shown("stop")) level(t.stop, C.supply, 3, drag && drag.field === "stop" ? [4, 3] : [], editable ? "Stop loss ⇕" : "Stop loss");
        if (shown("entry")) level(t.entry, C.brass, 3, [], t.entered == null ? "Entry (waiting)" : "Entry");
        if (t.liq) level(t.liq, C.muted, 1, [2, 3], "Liquidation");
        vertical(t.signal, C.muted, `Signal ${fmtTime(t.signal)}`, 2, [3, 3]);
        vertical(t.entered, C.brass, `Entry ${fmtTime(t.entered)}`, 0, []);
        vertical(t.exited, t.result === "win" ? C.demand : t.result === "loss" ? C.supply : C.muted, `${t.result === "open" ? "Still open at" : "Exit"} ${fmtTime(t.exited)}`, 1, []);
        if (t.rangeEnd != null && bars.length && t.rangeEnd >= bars[0].time && t.rangeEnd <= bars[bars.length - 1].time + BAR[t.tf])
          vertical(t.rangeEnd, C.text, `Backtest end ${fmtTime(t.rangeEnd, false)}`, 2, [5, 4]);
      });
    }
  }

  function init(box) {
    chart = LightweightCharts.createChart(box, {
      autoSize: true, layout: { background: { color: cssVar("--ink") }, textColor: C.muted, fontFamily: cssVar("--font") },
      grid: { vertLines: { color: "#1c2b3d" }, horzLines: { color: "#1c2b3d" } }, rightPriceScale: { borderColor: C.line },
      timeScale: { borderColor: C.line, timeVisible: true, secondsVisible: false }, localization: { locale: "en-US", priceFormatter: fmtPrice },
    });
    series = chart.addSeries(LightweightCharts.CandlestickSeries, { upColor: "#c9d3dd", borderUpColor: "#c9d3dd", wickUpColor: "#c9d3dd",
      downColor: "#4f647b", borderDownColor: "#4f647b", wickDownColor: "#4f647b", priceLineVisible: false });
    markerLayer = LightweightCharts.createSeriesMarkers(series, []);
    overlay = new Overlay();
    series.attachPrimitive(overlay);
    boxEl = box;
    // Dragging the stop or target line (paper popup only). Captured before the chart so it does not scroll meanwhile.
    const yOf = (e) => e.clientY - box.getBoundingClientRect().top;
    const near = (e) => {
      const t = overlay.t;
      if (!opts.editable || !t || t.result === "win" || t.result === "loss") return null;
      for (const field of ["stop", "target"]) {
        if (!shown(field)) continue;
        const y = series.priceToCoordinate(t[field]);
        if (y != null && Math.abs(y - yOf(e)) <= 7) return field;
      }
      return null;
    };
    box.addEventListener("pointerdown", (e) => {
      const field = near(e);
      if (!field) return;
      drag = { field, id: e.pointerId };
      chart.applyOptions({ handleScroll: false, handleScale: false });
      box.setPointerCapture(e.pointerId);
      e.preventDefault(); e.stopPropagation();
    }, true);
    box.addEventListener("pointermove", (e) => {
      if (!drag) { box.style.cursor = near(e) ? "ns-resize" : ""; return; }
      const price = series.coordinateToPrice(yOf(e));
      if (price == null || !(price > 0)) return;
      overlay.t[drag.field] = price;
      const line = lines[drag.field === "stop" ? 1 : 2];
      if (line) line.applyOptions({ price });
      overlay.set(overlay.t);
      if (opts.onDrag) opts.onDrag(drag.field, price, false);
    }, true);
    const end = (e) => {
      if (!drag) return;
      const field = drag.field;
      drag = null;
      chart.applyOptions({ handleScroll: true, handleScale: true });
      try { box.releasePointerCapture(e.pointerId); } catch (err) { /* already released */ }
      overlay.set(overlay.t);
      if (opts.onDrag) opts.onDrag(field, overlay.t[field], true);
    };
    box.addEventListener("pointerup", end, true);
    box.addEventListener("pointercancel", end, true);
  }

  // trade: a row from the backtest, or a paper order or trade from /api/paper/chart (which brings its own candles);
  // rangeEnd: the backtest's "To" date as an epoch, or null.
  async function show(box, trade, rangeEnd, options) {
    opts = options || {};
    if (!chart || boxEl !== box) { if (chart) chart.remove(); chart = null; lines = []; nowLine = null; init(box); }
    const tf = trade.timeframe, bar = BAR[tf] || 86400, intraday = bar < 86400;
    const live = trade.status === "pending" || trade.status === "open";
    const formed = toEpoch(trade.zone_created_ts || trade.entry_ts || trade.placed_at);
    const entered = trade.entry_ts ? toEpoch(trade.entry_ts) : null;
    const exited = trade.exit_ts ? toEpoch(trade.exit_ts) : live ? null : entered;
    const pad = bar * (intraday ? 240 : 45);        // intraday bars only fill a quarter of the day, so the padding is wider
    const iso = (t) => new Date((t - tzOffset(t)) * 1000).toISOString().slice(0, 19);     // back to UTC for the API
    const rows = trade.candles || await api(`/api/candles/${encodeURIComponent(trade.instrument_key)}?tf=${tf}&start=${iso(formed - pad)}&end=${iso((exited ?? entered ?? formed) + pad)}&limit=4000`);
    bars = rows.map((c) => ({ time: toEpoch(c.ts), open: c.open, high: c.high, low: c.low, close: c.close }));
    series.setData(bars);
    const last = bars.length ? Math.abs(bars[bars.length - 1].close) : 1, precision = Math.min(10, Math.max(2, last > 0 ? Math.ceil(-Math.log10(last)) + 4 : 2));
    series.applyOptions({ priceFormat: { type: "price", precision, minMove: Number((10 ** -precision).toFixed(precision)) } });
    for (const l of lines) series.removePriceLine(l);
    const add = (price, color) => series.createPriceLine({ price, title: "", color, lineVisible: false, axisLabelVisible: true });
    lines = [add(trade.entry, C.brass), add(trade.stop, C.supply), add(trade.target, C.demand)];
    if (nowLine) { series.removePriceLine(nowLine); nowLine = null; }
    const buy = trade.side === "demand";
    const marks = entered == null ? [] : [{ time: snap(entered), position: buy ? "belowBar" : "aboveBar", shape: buy ? "arrowUp" : "arrowDown", color: C.brass, text: buy ? "Long" : "Short" }];
    if (trade.exit_ts) marks.push({ time: snap(exited), position: buy ? "aboveBar" : "belowBar", shape: "circle", color: trade.result === "win" ? C.demand : trade.result === "loss" ? C.supply : C.muted, text: trade.result === "win" ? "Target" : trade.result === "loss" ? "Stopped" : "Open" });
    markerLayer.setMarkers(marks.filter((m) => m.time != null).sort((a, b) => a.time - b.time));
    overlay.set({ side: trade.side, tf, proximal: trade.zone_proximal ?? trade.entry, distal: trade.zone_distal ?? trade.stop, entry: trade.entry,
      stop: trade.stop, target: trade.target, formed, entered, exited, result: trade.result, rangeEnd,
      liq: trade.status === "open" || trade.status === "pending" ? (shown("price") ? trade.liq_price : null) : null,
      signal: trade.signal_ts ? toEpoch(trade.signal_ts) : null, steps: opts.steps || [] });
    if (!shown("entry")) lines[0].applyOptions({ axisLabelVisible: false });
    if (!shown("stop")) lines[1].applyOptions({ axisLabelVisible: false });
    if (!shown("target")) lines[2].applyOptions({ axisLabelVisible: false });
    if (live && opts.forming) seedForming(opts.forming);
    if (live && trade.price != null && shown("price")) setLive(trade.price);
    const i0 = bars.findIndex((b) => b.time >= formed), i1 = exited == null ? -1 : bars.findIndex((b) => b.time >= exited);
    const span = Math.max((i1 < 0 ? bars.length : i1) - Math.max(i0, 0), 10);
    chart.timeScale().setVisibleLogicalRange({ from: Math.max(i0, 0) - Math.max(span * 0.6, 20), to: (i1 < 0 ? bars.length : i1) + Math.max(span * 0.6, 20) });
    return bars.length;
  }
  // The candle still forming (from /api/forming): drawn after the stored ones, and then moved by every live price.
  function seedForming(f) {
    if (!series || !f || !bars.length) return;
    const bar = { time: toEpoch(f.ts), open: f.open, high: f.high, low: f.low, close: f.close };
    const last = bars[bars.length - 1], size = BAR[overlay && overlay.t ? overlay.t.tf : ""] || 0;
    if (bar.time < last.time || (size && bar.time - last.time > size)) return;    // stored candles behind: no gap is drawn
    if (bar.time === last.time) bars[bars.length - 1] = bar; else bars.push(bar);
    series.update(bar);
  }
  // The live price on an open position or a pending order: a line on the price axis that follows the feed, and the
  // candle in progress grows with it (a new one starts when the timeframe's next candle begins). Day and below only.
  function setLive(price, withCandle = true) {
    if (!series || price == null) return;
    if (opts.visible && opts.visible.price === false) { if (nowLine) { series.removePriceLine(nowLine); nowLine = null; } }
    else if (nowLine) nowLine.applyOptions({ price });
    else nowLine = series.createPriceLine({ price, title: "Now", color: C.text, lineWidth: 1, lineStyle: 2, lineVisible: true, axisLabelVisible: true });
    const t = overlay && overlay.t, bar = t && BAR[t.tf];
    if (!withCandle || !bars.length || !bar || bar > 86400 || !opts.liveCandle) return;
    const last = bars[bars.length - 1], now = shiftEpoch(Math.floor(Date.now() / 1000));
    const start = last.time + Math.floor((now - last.time) / bar) * bar;
    if (start - last.time > bar) return;        // the stored candles are behind: no live candle until they catch up
    if (start === last.time) {
      last.high = Math.max(last.high, price); last.low = Math.min(last.low, price); last.close = price;
      series.update({ ...last });
    } else if (start > last.time) {
      const nb = { time: start, open: last.close, high: Math.max(last.close, price), low: Math.min(last.close, price), close: price };
      bars.push(nb); series.update(nb);
    }
  }
  // Put the stop or target back (Reset), or show what was saved.
  function setLevels(stop, target) {
    if (!overlay || !overlay.t) return;
    overlay.t.stop = stop; overlay.t.target = target;
    if (lines[1]) lines[1].applyOptions({ price: stop });
    if (lines[2]) lines[2].applyOptions({ price: target });
    overlay.set(overlay.t);
  }
  return { show, setLive, setLevels, seed: seedForming };
})();
