// Backtest page: the scan-date check. One date, the rules as of that date, and what each entry type did afterwards.
// A trade score of 7 or more follows entry type 1; a score of 5 to 6.5 follows entry types 2 and 3.
// Every check is saved and can be reopened from the list of past checks.
const $ = (id) => document.getElementById(id);
let names = {}, shown = null;
const NEW_PLANS = ["type1", "type2", "type3"], OLD_PLANS = ["aggressive", "confirmation", "conservative"];
// A check saved before the entry types were added carries the earlier three entries; it is shown under their own names.
const isOldCheck = (obj) => Boolean(obj) && !("type1" in obj) && "aggressive" in obj;
let PLANS = NEW_PLANS.map((k) => [k, PLAN_NAME[k]]);
const signedR = (v) => `${v > 0 ? "+" : ""}${v} R`;
function say(text, isError = false) { $("s-status").textContent = text; $("s-status").className = "status" + (isError ? " err" : ""); }

// ---- coins: all ticked to begin with, one box to tick or clear them all
const boxes = () => [...$("s-symbols").querySelectorAll("input")];
function syncAll() {
  const on = boxes().filter((b) => b.checked).length, all = boxes().length;
  $("s-all").checked = all > 0 && on === all;
  $("s-all").indeterminate = on > 0 && on < all;      // some ticked: a partial mark
}
$("s-all").onchange = () => { for (const b of boxes()) b.checked = $("s-all").checked; syncAll(); };

function outcomeCell(o) {
  if (!o.entry_ts) return el("td", { class: "outcome muted" }, o.status === "never entered" ? "Never entered" : "Not taken", el("span", { class: "sub wrap" }, o.note || ""));
  const r = o.result_r == null ? "" : `, ${signedR(o.result_r)}`;
  const cost = o.fees_r == null ? "" : `; fees ${o.fees_r} R, funding ${o.funding_r} R`;
  const head = o.status === "target hit" ? `Target hit${r}` : o.status === "stopped" ? `Stopped${r}` : `Still open${r}`;
  return el("td", { class: "outcome" }, el("span", { class: o.status === "target hit" ? "win" : o.status === "stopped" ? "loss" : "" }, head),
    el("span", { class: "sub wrap" }, `in ${stamp(o.entry_ts)} at ${fmtPrice(o.entry)}, stop loss ${fmtPrice(o.stop)}, target ${fmtPrice(o.target)}, out ${stamp(o.exit_ts)}${cost}`));
}
const planCell = (x, earlier) => el("td", {}, x ? `${x.entered} entered, ${x.target_hit} target, ${x.stopped} stopped` : "",
  el("span", { class: "sub" }, [earlier ? `earlier rules: ${earlier.toLowerCase()}` : "", x && x.entered ? `${x.win_rate == null ? "no closed trade" : x.win_rate + "% win"}, ${signedR(x.total_r)}` : ""].filter(Boolean).join(", ")));

// Totals per plan for the rows on screen, worked out the same way the server does for the whole check.
function totals(rows) {
  const out = {};
  for (const [name] of PLANS) {
    const got = rows.map((r) => r.outcomes[name]);
    const entered = got.filter((o) => ["target hit", "stopped", "open"].includes(o.status)), closed = entered.filter((o) => o.status !== "open");
    const wins = closed.filter((o) => o.status === "target hit");
    out[name] = { entered: entered.length, target_hit: wins.length, stopped: closed.length - wins.length, open: entered.length - closed.length,
      never_entered: got.filter((o) => o.status === "never entered").length, not_taken: got.filter((o) => o.status === "not taken").length,
      win_rate: closed.length ? Math.round(1000 * wins.length / closed.length) / 10 : null,
      total_r: Math.round(100 * closed.reduce((sum, o) => sum + o.result_r, 0)) / 100,
      fees_r: Math.round(100 * closed.reduce((sum, o) => sum + (o.fees_r || 0), 0)) / 100,
      funding_r: Math.round(100 * closed.reduce((sum, o) => sum + (o.funding_r || 0), 0)) / 100 };
  }
  return out;
}
function zoneCell(z, tf) {
  if (!z) return el("td", { class: "muted" }, "None");
  return el("td", { class: "num" }, `${fmtPrice(z.proximal)} to ${fmtPrice(z.distal)}`, el("span", { class: "sub" }, `${TF_LABEL[tf] || ""}, score ${z.score} (${LABEL_TEXT[z.label] || z.label})`));
}

let report = null;
function showReport(out) {
  shown = out.id; report = out;
  const old = out.rows.some((r) => isOldCheck(r.outcomes));
  PLANS = (old ? OLD_PLANS : NEW_PLANS).map((k) => [k, PLAN_NAME[k]]);
  PLANS.forEach(([, label], n) => { $("s-p" + (n + 1)).textContent = label; });
  $("s-rule-line").textContent = old ? "This check was saved under the earlier entry rules (aggressive, confirmation, conservative). Run the date again to see it with entry types 1, 2 and 3."
    : "A trade score of 7 or more follows entry type 1. A score of 5 to 6.5 follows entry types 2 and 3, and only on the zone's next test. \"Not taken\" means the score does not use that entry.";
  $("s-result").hidden = false;
  if (!$("sf-hd")) buildScoreFilters($("score-filters"), drawReport);      // score filters: 7 or more to begin with
  drawReport();
}
// Draw the report for the rows that pass the score filters. The totals follow the filters; the saved check keeps every row.
function drawReport() {
  const out = report, f = readScoreFilters(), rows = out.rows.filter((r) => passesScores(r, f)), set = Object.values(f).some((v) => v > 0);
  $("s-title").textContent = `${out.id ? "Check #" + out.id + ": " : ""}as of ${out.scan_date}, ${MODE_LABEL[out.mode] || out.mode} mode, ${out.rows.length} ${out.rows.length === 1 ? "coin" : "coins"}${costText(out.costs)}`;
  $("s-filter-line").textContent = set ? `Score filters: ${rows.length} of ${out.rows.length} coins pass. The totals below are for those ${rows.length}.` : "";
  const sum = $("s-summary"), all = totals(rows); sum.replaceChildren();
  for (const [name, label] of PLANS) {
    const x = all[name];
    sum.append(el("tr", {}, el("td", {}, label), ...[x.entered, x.target_hit, x.stopped, x.open, x.never_entered, x.not_taken].map((v) => el("td", { class: "num" }, String(v))),
      el("td", { class: "num" }, x.win_rate == null ? "" : `${x.win_rate}%`), el("td", { class: "num" }, signedR(x.total_r)),
      el("td", { class: "num" }, signedR(x.fees_r)), el("td", { class: "num" }, signedR(x.funding_r))));
  }
  const body = $("s-rows"); body.replaceChildren();
  if (!rows.length) body.append(el("tr", {}, el("td", { colspan: "11" }, "No coin passes the score filters. Lower them or press Clear score filters.")));
  for (const r of rows) {
    const s = r.signal, trade = s.signal !== "NO_TRADE", z = r.ltf_zone;
    const link = `/chart?symbol=${encodeURIComponent(r.instrument_key)}&mode=${r.mode}&scan=${out.scan_date}`;
    body.append(el("tr", {}, el("td", {}, r.symbol, el("span", { class: "sub" }, r.cmp == null ? "" : fmtPrice(r.cmp))),
      el("td", {}, el("span", { class: "tag " + (s.signal === "BUY" ? "demand" : s.signal === "SELL" ? "supply" : "") }, s.signal === "BUY" ? "Long" : s.signal === "SELL" ? "Short" : "No trade"),
        el("span", { class: "sub wrap" }, trade ? [`${LABEL_TEXT[s.zone_type] || ""}, score ${s.score}`, s.stop_pct != null ? `stop ${s.stop_pct}% away` : "", feesText(s.fees_r)].filter(Boolean).join(". ") : (s.reason || ""))),
      zoneCell(r.demand_zone, r.htf), zoneCell(r.supply_zone, r.htf),
      el("td", {}, lower(r.curve)), el("td", {}, lower(r.itf_trend)),
      z ? el("td", { class: "num" }, `${fmtPrice(z.proximal)} to ${fmtPrice(z.distal)}`, el("span", { class: "sub" }, `${TF_LABEL[r.ltf]} ${z.side}, score ${z.score}, ${lower(r.zone_state)}`)) : el("td", { class: "muted" }, "None"),
      ...(trade ? PLANS.map(([n]) => outcomeCell(r.outcomes[n])) : [el("td", { class: "muted", colspan: "3" }, "No trade on this date")]),
      el("td", {}, el("a", { class: "btn", href: link }, "Chart and replay"))));
  }
}

async function loadPast() {
  const list = await api("/api/scanchecks"), body = $("s-past");
  body.replaceChildren();
  if (!list.length) { body.append(el("tr", {}, el("td", { colspan: "9" }, "No checks yet. Pick a scan date above and press Check this date."))); return; }
  for (const c of list) {
    const s = c.summary || {};
    body.append(el("tr", { class: "pick", "aria-selected": String(c.id === shown), onclick: () => openPast(c.id) },
      el("td", {}, `#${c.id}`), el("td", {}, stamp(c.created_at)), el("td", {}, c.scan_date), el("td", {}, MODE_LABEL[c.mode] || c.mode), el("td", { class: "num" }, String(c.coins ?? c.stocks)),
      ...(isOldCheck(s) ? OLD_PLANS.map((k) => planCell(s[k], PLAN_NAME[k])) : NEW_PLANS.map((k) => planCell(s[k]))),
      el("td", {}, el("button", { class: "btn", onclick: (e) => { e.stopPropagation(); removePast(c.id); } }, "Delete"))));
  }
}
async function openPast(id) {
  try { showReport(await api(`/api/scancheck/${id}`)); say(`Showing saved check #${id}.`); await loadPast(); $("s-result").scrollIntoView({ behavior: "smooth", block: "start" }); }
  catch (e) { say(`Could not open that check. ${e.message}`, true); }
}
async function removePast(id) {
  if (!confirm(`Delete saved check #${id}?`)) return;
  try { await api(`/api/scancheck/${id}`, { method: "DELETE" }); if (shown === id) { shown = null; $("s-result").hidden = true; } await loadPast(); }
  catch (e) { say(`Could not delete it. ${e.message}`, true); }
}

function costText(c) {
  if (!c) return "";
  return `, fees ${c.fee_maker_pct}% maker and ${c.fee_taker_pct}% taker${c.fee_bnb ? " less 10% (BNB)" : ""}, funding ${c.include_funding ? "included" : "left out"}`;
}

$("s-run").onclick = async () => {
  const symbols = boxes().filter((b) => b.checked).map((b) => b.value);
  if (!symbols.length) return say("Tick at least one coin.", true);
  if (!$("s-date").value) return say("Choose the scan date.", true);
  try {
    say(`Checking ${symbols.length} ${symbols.length === 1 ? "coin" : "coins"}`); $("s-run").disabled = true;
    const out = await post("/api/scancheck", { symbols, mode: $("s-mode").value, scan_date: $("s-date").value, save: true,
      fee_maker_pct: Number($("s-maker").value), fee_taker_pct: Number($("s-taker").value), fee_bnb: $("s-bnb").checked, include_funding: $("s-funding").checked });
    showReport(out);
    say(`${out.rows.length} ${out.rows.length === 1 ? "coin" : "coins"} checked as of ${out.scan_date}. Saved as check #${out.id}.`);
    await loadPast();
  } catch (e) { say(`Could not run the check. ${e.message}`, true); }
  finally { $("s-run").disabled = false; }
};

(async function init() {
  showToken($("token"));
  try {
    const s = await api("/api/settings");
    names = s.symbols || {};
    const ready = new Set(s.ready || []), box = $("s-symbols");
    const fillCoins = () => {
      const coins = coinGroup(s.ranked || s.universe || [], $("s-group").value).filter((k) => ready.has(k));
      box.replaceChildren(...coins.map((k) => el("label", { class: "check" }, Object.assign(el("input", { type: "checkbox", value: k }), { checked: true }), names[k] || k)));
      if (!coins.length) box.append(el("p", { class: "empty" }, "No coin is ready yet. Download and scan some on the Data page."));
      syncAll();
    };
    buildGroupSelect($("s-group"));
    $("s-group").onchange = () => { saveGroup($("s-group").value); fillCoins(); };
    fillCoins();
    $("s-maker").value = s.fee_maker_pct; $("s-taker").value = s.fee_taker_pct; $("s-bnb").checked = Boolean(s.fee_bnb); $("s-funding").checked = s.include_funding !== false;
    box.onchange = syncAll; syncAll();
    for (const m of Object.keys(MODES)) $("s-mode").append(el("option", { value: m }, modeText(m)));
    $("s-mode").value = "MONTHLY";
    $("s-date").value = new Date(Date.now() - 60 * 864e5).toISOString().slice(0, 10);
    await loadPast();
  } catch (e) { say(`Could not load the page. ${e.message}`, true); }
})();