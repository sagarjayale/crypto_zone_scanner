// Shared by the GTF EYE and Premium pages: the zone modes, page preferences, loading /api/eye and the coin cards.
const EYE_MODES = ["15M", "30M", "HOURLY", "2HR", "4HR", "DAILY", "WEEKLY", "MONTHLY", "QUARTERLY", "HALFYEARLY", "YEARLY"];
// button names: the timeframe of the zones each mode uses
const EYE_LABEL = { "15M": "15 min", "30M": "30 min", HOURLY: "1 hr", "2HR": "2 hr", "4HR": "4 hr", DAILY: "Daily", WEEKLY: "Weekly",
  MONTHLY: "Monthly", QUARTERLY: "Quarterly", HALFYEARLY: "Half yearly", YEARLY: "Yearly" };
const EYE_REFRESH_MS = 10000;          // prices move: the cards are worked out again every 10 seconds
const STATE_TEXT = { in_zone: "In zone", reacting: "Reacting", approaching: "Approaching" };

function loadPrefs(key, defaults) {
  try { return { ...defaults, ...(JSON.parse(localStorage.getItem(key) || "{}")) }; } catch (e) { return { ...defaults }; }
}
function savePrefs(key, prefs) { try { localStorage.setItem(key, JSON.stringify(prefs)); } catch (e) { /* storage not available */ } }

// Loads /api/eye for a mode. A load still running is never doubled; a later mode wins over an earlier one.
function eyeLoader(onData, onError) {
  let busy = false, wanted = null, timer = null;
  async function load(mode) {
    wanted = mode;
    if (busy) return;
    busy = true;
    try {
      while (wanted) {
        const m = wanted; wanted = null;
        const data = await api(`/api/eye?mode=${encodeURIComponent(m)}`);
        if (!wanted) onData(data);
      }
    } catch (e) { onError(e); } finally { busy = false; }
  }
  return {
    load,
    every(modeFn) { clearInterval(timer); timer = setInterval(() => { if (!document.hidden) load(modeFn()); }, EYE_REFRESH_MS); },
  };
}

function chartLink(r, mode, zoneId) {
  const q = new URLSearchParams({ symbol: r.instrument_key, mode, tf: MODES[mode][0] });
  if (zoneId) q.set("zone", zoneId);
  return "/chart?" + q.toString();
}

// One coin card. z: the zone view of one side (or null), extra: a line under the price.
function coinCard(r, mode, z, extra) {
  const side = z ? z.side : "";
  const lines = [el("span", { class: "eye-name" }, r.symbol.replace(/USDT$/, "")), el("span", { class: "eye-price" }, fmtPrice(r.price))];
  if (extra) lines.push(el("span", { class: "eye-extra" }, extra));
  if (z) {
    lines.push(el("span", { class: "eye-zone" }, `${fmtPrice(z.proximal)} – ${fmtPrice(z.distal)}`));
    lines.push(el("span", { class: "eye-score" }, `score ${z.score}` + (z.touches ? `, tested ${z.touches}×` : ", fresh")));
  }
  const title = z ? `${r.symbol}: ${side} zone ${fmtPrice(z.proximal)} to ${fmtPrice(z.distal)}, ${STATE_TEXT[z.state] || "away from it"}. Open the chart.`
                  : `${r.symbol}: open the chart.`;
  return el("a", { class: `eye-card ${side}`, href: chartLink(r, mode, z && z.zone_id), title }, ...lines);
}

// The coins of the chosen group, most traded first, as row objects.
function inGroup(data, group) {
  const keep = new Set(coinGroup(data.ranked || [], group));
  const order = new Map((data.ranked || []).map((k, i) => [k, i]));
  return data.rows.filter((r) => keep.has(r.instrument_key)).sort((a, b) => (order.get(a.instrument_key) ?? 1e9) - (order.get(b.instrument_key) ?? 1e9));
}
