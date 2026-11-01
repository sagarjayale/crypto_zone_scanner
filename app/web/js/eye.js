// GTF EYE page: coins whose price is approaching, in or reacting from an HTF demand or supply zone of the chosen mode.
// The server sends every coin once per mode; the buttons filter on the page, so nothing waits on a click.
(function () {
  const $ = (id) => document.getElementById(id);
  const KEY = "cryptoScanner.eye";
  const DEFAULTS = { criteria: "approaching", side: "demand", kind: "standard", mode: "DAILY", score: "0", search: "" };
  let prefs = loadPrefs(KEY, DEFAULTS);
  if (!EYE_MODES.includes(prefs.mode)) prefs.mode = "DAILY";
  let data = null;

  // the three yellow toggles
  function toggle(id, field) {
    const box = $(id);
    const press = () => box.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.v === prefs[field])));
    box.addEventListener("click", (e) => {
      const b = e.target.closest("button");
      if (!b) return;
      prefs[field] = b.dataset.v; press(); savePrefs(KEY, prefs); render();
    });
    return press;
  }
  const presses = [toggle("t-criteria", "criteria"), toggle("t-side", "side"), toggle("t-kind", "kind")];

  // Daily ... Yearly
  const tfBox = $("t-mode");
  for (const m of EYE_MODES) tfBox.append(el("button", { "data-v": m }, EYE_LABEL[m]));
  const pressMode = () => tfBox.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.v === prefs.mode)));
  tfBox.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b || b.dataset.v === prefs.mode) return;
    prefs.mode = b.dataset.v; pressMode(); savePrefs(KEY, prefs);
    data = null; render(); loader.load(prefs.mode);
  });

  buildGroupSelect($("f-group"));
  $("f-group").onchange = () => { saveGroup($("f-group").value); render(); };
  $("f-score").value = prefs.score;
  $("f-score").onchange = () => { prefs.score = $("f-score").value; savePrefs(KEY, prefs); render(); };
  $("f-search").value = prefs.search;
  $("f-search").oninput = () => { prefs.search = $("f-search").value; savePrefs(KEY, prefs); render(); };
  $("f-reset").onclick = () => {
    const modeChanged = prefs.mode !== DEFAULTS.mode;
    prefs = { ...DEFAULTS }; savePrefs(KEY, prefs);
    $("f-score").value = prefs.score; $("f-search").value = ""; presses.forEach((p) => p()); pressMode();
    if (modeChanged) { data = null; loader.load(prefs.mode); }
    render();
  };

  function wanted(r) {
    const z = r[prefs.side];
    if (!z || !z.state) return null;
    if (prefs.criteria === "approaching" ? z.state !== "approaching" : !["in_zone", "reacting"].includes(z.state)) return null;
    if (z.score < Number(prefs.score)) return null;
    if (prefs.kind === "gtf" && r.signal !== (prefs.side === "demand" ? "BUY" : "SELL")) return null;
    const s = prefs.search.trim().toUpperCase();
    if (s && !r.symbol.includes(s)) return null;
    return z;
  }

  function extraLine(r, z) {
    if (z.state === "in_zone") return "in zone";
    if (z.state === "reacting") return `reacting, ${z.distance_pct}% away (${z.reaction_r}× zone)`;
    return `${z.distance_pct}% ${z.side === "demand" ? "above" : "below"} the zone`;
  }

  function render() {
    const cards = $("cards");
    const label = EYE_LABEL[prefs.mode];
    if (!data) { $("s-line").textContent = `Loading ${label} zones`; cards.replaceChildren(); return; }
    const rows = inGroup(data, $("f-group").value);
    const shown = [];
    for (const r of rows) { const z = wanted(r); if (z) shown.push([r, z]); }
    if (prefs.criteria === "approaching") shown.sort((a, b) => a[1].distance_pct - b[1].distance_pct);
    cards.replaceChildren(...shown.map(([r, z]) => coinCard(r, prefs.mode, z, extraLine(r, z))));
    const crit = prefs.criteria === "approaching" ? "approaching" : "in or reacting from";
    $("s-line").textContent = `${shown.length} of ${rows.length} coins ${crit} a ${label.toLowerCase()} ${prefs.side} zone` +
      (prefs.kind === "gtf" ? `, with a GTF ${prefs.side === "demand" ? "long" : "short"} signal` : "") + ". Click a coin to open its chart.";
    if (!shown.length) cards.append(el("p", { class: "lede" }, "No coin matches these filters right now."));
    const tf = TF_LABEL[data.frames[0]].toLowerCase();
    $("help").textContent = `Approaching: the price is within ${data.approach_pct}% of the proximal line of the nearest ${tf} zone (Settings, approach %). ` +
      `In zone: the price is between the proximal and distal lines. Reacting: one of the last ${data.react_candles} closed ${tf} candles touched the zone and the price is back outside it, not yet more than ${data.react_max_r} zone heights away. ` +
      `Only zones tested at most ${data.max_tests}× before (Settings, wick tests), as for trading. ` +
      `Standard mode: the ${tf} zone only. GTF mode: only coins whose full GTF chain (curve, ITF trend, LTF zone) gives the same side, as on the Dashboard.`;
  }

  const loader = eyeLoader((d) => { if (d.mode === prefs.mode) { data = d; render(); } },
    (e) => { $("s-line").textContent = `Could not load the zones: ${e.message}`; });
  presses.forEach((p) => p()); pressMode(); render();
  loader.load(prefs.mode);
  loader.every(() => prefs.mode);
  showToken($("token"));
  setInterval(() => showToken($("token")), 30000);
})();
