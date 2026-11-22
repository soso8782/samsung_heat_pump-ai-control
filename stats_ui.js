/* Záložka „Štatistiky“ – samostatný skript (IIFE). Verejné API: window.StatsUI = { show(), hide(), resize() }.
   Používa globály stránky (api), ak existujú; inak vlastné náhrady. Žiadne knižnice, žiadne externé požiadavky.
   Texty idú z prekladov (locales/*.json, kľúče stats.*) cez globálne t(), tn(), fnum(), LOCALE z ui.html. */
(function () {
"use strict";
if (window.StatsUI) return;

/* náhrady, ak by ui.html i18n nedefinoval (stránka sa nesmie zrútiť) */
const t = (k, v) => typeof window.t === "function" ? window.t(k, v) : k;
const tn = (k, n, v) => typeof window.tn === "function" ? window.tn(k, n, v) : k;
const LOC = () => window.LOCALE || "sk-SK";
const fnumL = (x, n) => typeof window.fnum === "function" ? window.fnum(x, n) : x.toFixed(n).replace(".", window.LANG && window.LANG !== "sk" ? "." : ",").replace("-", "−");

const ROOT_ID = "tab-statistiky";
const SKEY = "kurenie-stats-v1";
const VIEWS = ["den", "porovnanie", "obdobie", "poruchy", "zdravie"];
const VIEWS = ["den", "porovnanie", "obdobie", "sezona", "poruchy"];
const GROUP_KEYS = ["vonku", "izby", "voda", "tuv", "kompresor", "energia", "ucinnost", "kvalita"];
const grpName = g => GROUP_KEYS.includes(g) ? t("stats.skupina." + g) : g;
/* názvy z číselníkov: známy kľúč sa prekladá, neznámy (z API) ostáva ako je */
const TL_KEYS = ["beh_kurenie", "beh_tuv", "odmrazovanie", "stoji", "bez_dat"];
const MODE_KEYS = ["kurenie", "tuv", "odmrazovanie", "zmiesane"];
const SRC_KEYS = ["tc_error", "gap", "alert", "manual"];
const HSRC_KEYS = ["tc", "ha", "mix"];
const lk = (prefix, keys, k) => keys.includes(k) ? t(prefix + k) : k;
const tlName = k => lk("stats.stav.", TL_KEYS, k), modeName = k => lk("stats.rezim.", MODE_KEYS, k), srcName = k => lk("stats.zdroj.", SRC_KEYS, k), hsrcName = k => lk("stats.hzdroj.", HSRC_KEYS, k);
/* núdzový zoznam metrík (spec §6), ak by /api/stats/meta zlyhalo: [key, unit, group, decimals, agg]; názov = stats.metrika.<key> */
const DM = [
  ["vonku", "°C", "vonku", 1, "avg"], ["ciel", "°C", "izby", 1, "avg"], ["detska", "°C", "izby", 1, "avg"],
  ["jedalen", "°C", "izby", 1, "avg"], ["kuchyna", "°C", "izby", 1, "avg"], ["obyvacka", "°C", "izby", 1, "avg"],
  ["spalna", "°C", "izby", 1, "avg"], ["pracovna", "°C", "izby", 1, "avg"], ["kupelna", "°C", "izby", 1, "avg"],
  ["chodba", "°C", "izby", 1, "avg"], ["kotolna", "°C", "izby", 1, "avg"],
  ["tw2", "°C", "voda", 1, "avg"], ["tw1", "°C", "voda", 1, "avg"], ["wl", "°C", "voda", 1, "avg"],
  ["dt_beh", "°C", "voda", 1, "avg"], ["tuv", "°C", "tuv", 1, "avg"], ["tuv_ciel", "°C", "tuv", 1, "avg"],
  ["tuv_min", "min", "tuv", 0, "sum"], ["beh_min", "min", "kompresor", 0, "sum"],
  ["kurenie_min", "min", "kompresor", 0, "sum"], ["odmraz_min", "min", "kompresor", 0, "sum"],
  ["starty", "", "kompresor", 0, "sum"], ["hz_avg", "Hz", "kompresor", 0, "avg"], ["hz_max", "Hz", "kompresor", 0, "max"],
  ["kwh", "kWh", "energia", 2, "sum"], ["kwh_cal", "kWh", "energia", 2, "sum"],
  ["kwh_dom", "kWh", "energia", 2, "sum"], ["prikon", "W", "energia", 0, "avg"],
  ["q_kwh", "kWh", "ucinnost", 2, "sum"], ["cop", "", "ucinnost", 2, "ratio"]
];
let dmCache = null;
const defaultMetrics = () => dmCache || (dmCache = DM.map(a => ({key: a[0], label: t("stats.metrika." + a[0]), unit: a[1], group: a[2], decimals: a[3], agg: a[4]})));
/* farby a štýl čiar pre denné grafy (len tokeny) */
const SER = {
  vonku: {c: "--ink-2"}, ciel: {c: "--mut", dash: true}, detska: {c: "--s1"}, jedalen: {c: "--s2"}, kuchyna: {c: "--s3"}, obyvacka: {c: "--s4"},
  spalna: {c: "--s5"}, pracovna: {c: "--s6"}, kupelna: {c: "--s7"}, chodba: {c: "--s8"}, kotolna: {c: "--copper", dash: true},
  tw2: {c: "--copper"}, tw1: {c: "--cool"}, wl: {c: "--s7", dash: true}, tuv: {c: "--s3"},
  beh_min: {c: "--copper", bar: true}, hz_avg: {c: "--s1"}, hz_max: {c: "--s2", dash: true}, kwh: {c: "--s4", bar: true},
  kwh_pump: {c: "--copper", bar: true}, kwh_dom: {c: "--cool"}, cop: {c: "--s3", bar: true}
};
/* izby a ďalšie sledované teploty sa berú z metrík (/api/stats/meta, generuje ich stats.py z profilu); skupina „izby“ bez „ciel“ */
const roomKeys = () => metricList().filter(m => m.group === "izby" && m.key !== "ciel").map(m => m.key);
const roomSer = k => { const i = roomKeys().indexOf(k); return {c: "--s" + (((i < 0 ? 0 : i) % 8) + 1)}; };
const dayCards = () => [
  {id: "t", title: t("stats.karta.teploty"), plots: [
    {id: "vonku", h: 120, label: t("stats.graf.vonku"), keys: ["vonku"]},
    {id: "izby", h: 220, label: t("stats.graf.izby"), keys: ["ciel"].concat(roomKeys())}]},
  {id: "w", title: t("stats.karta.voda"), plots: [{id: "voda", h: 200, label: t("stats.graf.voda"), keys: ["tw2", "tw1", "wl", "tuv"]}]},
  {id: "k", title: t("stats.karta.kompresor"), plots: [
    {id: "beh", h: 120, label: t("stats.graf.beh"), keys: ["beh_min"], bars: true, fixed: [0, 60]},
    {id: "hz", h: 140, label: t("stats.graf.hz"), keys: ["hz_avg", "hz_max"], zero: true}]},
  {id: "e", title: t("stats.karta.spotreba"), plots: [
    {id: "kwh", h: 130, label: t("stats.graf.kwh"), keys: ["kwh_pump"], bars: true, zero: true},
    {id: "dom", h: 150, label: t("stats.graf.dom"), keys: ["kwh_pump", "kwh_dom"], zero: true, need: 2}]},
  {id: "c", title: t("stats.karta.cop"), nomsg: t("stats.graf.cop_nedostupne"), plots: [
    {id: "cop", h: 130, label: t("stats.graf.cop"), keys: ["cop"], bars: true, zero: true}]}
];
const colGroups = () => [
  ["teploty", t("stats.stlpce.teploty"), ["vonku", "ciel"].concat(roomKeys())],
  ["voda", t("stats.stlpce.voda"), ["tw2", "tw1", "wl", "dt_beh"]],
  ["tuv", t("stats.stlpce.tuv"), ["tuv", "tuv_ciel", "tuv_min"]],
  ["kompresor", t("stats.stlpce.kompresor"), ["beh_min", "kurenie_min", "odmraz_min", "starty", "hz_avg", "hz_max"]],
  ["energia", t("stats.stlpce.energia"), ["kwh", "kwh_cal", "kwh_dom", "prikon"]],
  ["ucinnost", t("stats.stlpce.ucinnost"), ["q_kwh", "cop"]],
  ["pokrytie", t("stats.stlpce.pokrytie"), ["n", "src"]]
];
const RNG_COLS = ["vonku", "tw2", "tw1", "beh_min", "starty", "odmraz_min", "tuv_min", "kwh", "kwh_spotreba", "kwh_cal", "kwh_dom", "q_kwh", "cop", "cop_kurenie", "cop_tuv", "prikon", "hdd", "kwh_hdd"];
/* doplnkové metriky: key -> [jednotka, desatinné, skupina]; názov = stats.metrika.<key> */
const EXTRA = {hdd: ["°C·d", 1, "kvalita"], kwh_hdd: ["kWh/°C·d", 2, "kvalita"], beh_pct: ["%", 0, "kompresor"], vonku_min: ["°C", 1, "vonku"],
  vonku_max: ["°C", 1, "vonku"], hodin_s_datami: ["h", 0, "kvalita"], poruch: ["", 0, "kvalita"], tuv_max: ["°C", 1, "tuv"],
  kwh_spotreba: ["kWh", 2, "energia"], kwh_pump: ["kWh", 2, "energia"], cop_kurenie: ["", 2, "ucinnost"], cop_tuv: ["", 2, "ucinnost"]};
const extraOf = k => EXTRA[k] ? {label: t("stats.metrika." + k), unit: EXTRA[k][0], decimals: EXTRA[k][1], group: EXTRA[k][2]} : undefined;
const FAR_PAST = "2020-01-01";

/* ---------- pomocné funkcie ---------- */
const gid = id => document.getElementById(id);
const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const num = v => (v === null || v === undefined || v === "" || typeof v === "boolean") ? null : (Number.isFinite(Number(v)) ? Number(v) : null);
const f = (v, n) => { const x = num(v); if (x === null) return "–"; const d = n === undefined ? 1 : n; return fnumL(/^-0(\.0+)?$/.test(x.toFixed(d)) ? 0 : x, d); };
const sf = (v, n) => { const x = num(v); if (x === null) return "–"; const s = f(x, n); return x > 0 && s !== f(0, n) ? "+" + s : s; };
const pad2 = n => String(n).padStart(2, "0");
const dstr = d => d.getFullYear() + "-" + pad2(d.getMonth() + 1) + "-" + pad2(d.getDate());
const todayS = () => dstr(new Date());
const validD = s => { if (typeof s !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(s)) return false; const d = new Date(s + "T12:00:00"); return !Number.isNaN(d.getTime()) && dstr(d) === s; };
const addDays = (s, n) => { const d = new Date(s + "T12:00:00"); d.setDate(d.getDate() + n); return dstr(d); };
const addYears = (s, n) => { const y = +s.slice(0, 4) + n, m = s.slice(5, 7), dd = s.slice(8, 10); const r = y + "-" + m + "-" + dd; return validD(r) ? r : y + "-" + m + "-28"; };
/* dátum: šablóny stats.datum.dmy / stats.datum.dm (sk „15. 11. 2026“, en „15 Nov 2026“), {mon} = krátky názov mesiaca podľa LOCALE */
const monCache = {};
const monShort = m => { const k = LOC() + m; return monCache[k] || (monCache[k] = new Date(2000, m - 1, 15).toLocaleDateString(LOC(), {month: "short"})); };
const fmtD = s => validD(s) ? t("stats.datum.dmy", {d: +s.slice(8, 10), m: +s.slice(5, 7), mon: monShort(+s.slice(5, 7)), y: s.slice(0, 4)}) : esc(s || "");
const fmtDS = s => validD(s) ? t("stats.datum.dm", {d: +s.slice(8, 10), m: +s.slice(5, 7), mon: monShort(+s.slice(5, 7))}) : String(s || "");
const fmtDW = s => validD(s) ? t("stats.den_kratko." + new Date(s + "T12:00:00").getDay()) + " " + fmtDS(s) : String(s || "");
const hm = ts => { const d = new Date(ts * 1000); return pad2(d.getHours()) + ":" + pad2(d.getMinutes()); };
const dtm = ts => { const x = num(ts); if (x === null) return "–"; const d = new Date(x * 1000); return t("stats.datum.dmy", {d: d.getDate(), m: d.getMonth() + 1, mon: monShort(d.getMonth() + 1), y: d.getFullYear()}) + " " + hm(x); };
const dur = m => { const x = num(m); if (x === null) return "–"; if (x < 1) return "< 1 min"; const r = Math.round(x); return r < 60 ? r + " min" : Math.floor(r / 60) + " h " + pad2(r % 60) + " min"; };
const utcMs = s => Date.UTC(+s.slice(0, 4), +s.slice(5, 7) - 1, +s.slice(8, 10));
const msToS = ms => { const d = new Date(ms); return d.getUTCFullYear() + "-" + pad2(d.getUTCMonth() + 1) + "-" + pad2(d.getUTCDate()); };
const arr = v => Array.isArray(v) ? v : [];
const isObj = v => v && typeof v === "object" && !Array.isArray(v);
function isoWeekStart(y, w) { const j4 = new Date(Date.UTC(y, 0, 4)), dow = (j4.getUTCDay() + 6) % 7; return Date.UTC(y, 0, 4 - dow + (w - 1) * 7); }

/* ---------- stav (JS premenné + sessionStorage) ---------- */
const S = {view: "den", day: todayS(), cmp: [], cmpMetric: "vonku", rngPreset: "30", rngFrom: "", rngTo: "", rngGroup: "day", rngMetric: "kwh", rngYoy: false,
  fltMode: "open", fltFrom: "", fltTo: "", cols: {teploty: true, voda: true, tuv: false, kompresor: true, energia: true, ucinnost: true, pokrytie: false}};
S.cmp = [todayS(), addDays(todayS(), -1)];
const hiddenSer = {"d:chodba": true, "d:kotolna": true};
function saveState() { try { sessionStorage.setItem(SKEY, JSON.stringify(S)); } catch (e) {} }
function loadState() {
  let o = null; try { o = JSON.parse(sessionStorage.getItem(SKEY) || "null"); } catch (e) {}
  if (!isObj(o)) return;
  if (VIEWS.includes(o.view)) S.view = o.view;
  if (validD(o.day) && o.day <= todayS()) S.day = o.day;
  if (Array.isArray(o.cmp)) { const c = o.cmp.filter((d, i, a) => validD(d) && a.indexOf(d) === i).slice(0, 6); if (c.length) S.cmp = c; }
  if (typeof o.cmpMetric === "string" && /^[a-z0-9_]{1,30}$/.test(o.cmpMetric)) S.cmpMetric = o.cmpMetric;
  if (["7", "30", "month", "year", "custom"].includes(o.rngPreset)) S.rngPreset = o.rngPreset;
  if (validD(o.rngFrom)) S.rngFrom = o.rngFrom;
  if (validD(o.rngTo)) S.rngTo = o.rngTo;
  if (["day", "week", "month"].includes(o.rngGroup)) S.rngGroup = o.rngGroup;
  if (typeof o.rngMetric === "string" && /^[a-z0-9_]{1,30}$/.test(o.rngMetric)) S.rngMetric = o.rngMetric;
  S.rngYoy = o.rngYoy === true;
  if (["open", "all", "period"].includes(o.fltMode)) S.fltMode = o.fltMode;
  if (validD(o.fltFrom)) S.fltFrom = o.fltFrom;
  if (validD(o.fltTo)) S.fltTo = o.fltTo;
  if (isObj(o.cols)) for (const k of Object.keys(S.cols)) if (typeof o.cols[k] === "boolean") S.cols[k] = o.cols[k];
}

/* ---------- dáta ---------- */
const D = {meta: null, metrics: null, day: null, cmp: null, quick: null, quickBase: null, rng: null, rngPrev: null, rngPrevErr: false, flt: null, zdr: null, codes: null, at: {}, key: {}};
const seq = {den: 0, porovnanie: 0, obdobie: 0, poruchy: 0, zdravie: 0, quick: 0, meta: 0};
const D = {meta: null, metrics: null, day: null, cmp: null, quick: null, quickBase: null, rng: null, rngPrev: null, rngPrevErr: false, flt: null, codes: null, at: {}, key: {}};
const seq = {den: 0, porovnanie: 0, obdobie: 0, sezona: 0, poruchy: 0, quick: 0, meta: 0};
let built = false, visible = false, root = null, tipEl = null;

async function localApi(path, body) {
  const o = body ? {method: "POST", headers: {"Content-Type": "application/json", "X-Kurenie": "1"}, body: JSON.stringify(body)} : {cache: "no-store"};
  const res = await fetch(path, o); let j = null; try { j = await res.json(); } catch (e) {}
  if (!res.ok) return Object.assign({}, isObj(j) ? j : {}, {error: (j && j.error) || t("stats.spolocne.chyba_servera", {kod: res.status})});
  return j == null ? {} : j;
}
/* každé volanie vráti objekt; chyby (aj sieť, 401) sa premenia na {error} – nikdy nevyhodí výnimku */
async function call(path, body) {
  try {
    const r = typeof api === "function" ? await api(path, body) : await localApi(path, body);
    if (!isObj(r)) return {error: t("stats.spolocne.neplatna_odpoved_servera")};
    return r;
  } catch (e) { return {error: e && e.auth ? t("stats.spolocne.prihlasenie_vyprsalo") : t("stats.spolocne.server_neodpoveda"), auth: !!(e && e.auth)}; }
}
const q = o => Object.keys(o).filter(k => o[k] !== undefined && o[k] !== null && o[k] !== "").map(k => encodeURIComponent(k) + "=" + encodeURIComponent(o[k])).join("&");

function metricList() {
  if (D.metrics && D.metrics.length) return D.metrics;
  const m = D.meta && arr(D.meta.metrics).filter(x => isObj(x) && typeof x.key === "string");
  return m && m.length ? m : defaultMetrics();
}
function M(key) {
  const x = metricList().find(m => m.key === key) || defaultMetrics().find(m => m.key === key) || extraOf(key);
  const r = x ? Object.assign({key}, x) : {key, label: key, unit: "", decimals: 1, group: "kvalita"};
  if (num(r.decimals) === null) r.decimals = 1;
  if (typeof r.label === "string" && r.unit) {   /* jednotku nezobrazovať dvakrát: „Ohrev TÚV (min)“ + „min“ */
    const u = r.unit.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    r.label = r.label.replace(new RegExp("\\s*\\(" + u + "\\)\\s*$"), "").replace(new RegExp("\\s+" + u + "$"), "").replace(/[,\s]+$/, "") || r.label;
  }
  return r;
}
const mUnit = k => M(k).unit || "";
const fm = (k, v) => f(v, M(k).decimals);
const fmu = (k, v) => { const u = mUnit(k); return f(v, M(k).decimals) + (u && num(v) !== null ? " " + u : ""); };
const serOf = k => SER[k] || (M(k).group === "izby" ? roomSer(k) : {});
const valCol = k => serOf(k).c || "--copper";

/* ---------- tooltip ---------- */
function tipShow(html, cx, cy) {
  if (!tipEl) return;
  tipEl.innerHTML = html; tipEl.style.display = "block";
  const w = tipEl.offsetWidth, h = tipEl.offsetHeight;
  tipEl.style.left = Math.max(8, cx + 18 + w > window.innerWidth ? cx - w - 18 : cx + 18) + "px";
  tipEl.style.top = Math.max(8, Math.min(window.innerHeight - h - 8, cy - h / 2)) + "px";
}
function tipHide() { if (tipEl) tipEl.style.display = "none"; }
const trow = (col, label, val, dim) => `<div class="r${dim ? " dim" : ""}"><span>${col ? `<i style="background:var(${col})"></i>` : ""}${label}</span><span>${val}</span></div>`;

/* ---------- graf: jedna os na graf, SVG podľa šírky kontajnera ---------- */
const PAD = {l: 46, r: 10, t: 10, b: 24};
function niceStep(range, target) {
  const raw = Math.max(range, 1e-9) / Math.max(1, target), p = Math.pow(10, Math.floor(Math.log10(raw))), m = raw / p;
  return (m < 1.5 ? 1 : m < 3 ? 2 : m < 7 ? 5 : 10) * p;
}
function scaleFor(vals, o) {
  let lo, hi;
  if (o.fixed) { lo = o.fixed[0]; hi = o.fixed[1]; }
  else {
    if (vals.length) { lo = vals[0]; hi = vals[0]; for (const v of vals) { if (v < lo) lo = v; if (v > hi) hi = v; } } else { lo = 0; hi = 1; }
    if (o.zero) lo = Math.min(lo, 0);
    let span = hi - lo;
    if (span < 1e-9) { if (o.zero) { hi = lo + 1; } else { lo -= .5; hi += .5; } span = hi - lo; }
    if (!o.zero) { lo -= span * .08; hi += span * .08; } else hi += span * .06;
  }
  const step = niceStep(hi - lo, 4);
  lo = Math.floor(lo / step + 1e-9) * step; hi = Math.ceil(hi / step - 1e-9) * step;
  if (hi <= lo) hi = lo + step;
  return {lo, hi, step, dig: Math.min(3, Math.max(0, -Math.floor(Math.log10(step) + 1e-9)))};
}
/* p: {svg, W, H, series:[{id,label,color,dash,w,xs,vals,aligned,hidden,unit,dec}], xmin, xmax, ticks:[{x,label}], fixed, zero, bars, minDx, maxGap, missing:[[a,b]], dots}
   Prázdne hodnoty (null) = prerušená čiara / šedý pás, nikdy nula. */
function drawPlot(p) {
  const svg = p.svg, W = p.W, H = p.H, iw = W - PAD.l - PAD.r, ih = H - PAD.t - PAD.b;
  svg.setAttribute("viewBox", "0 0 " + W + " " + H); svg.setAttribute("height", H);
  const vis = p.series.filter(s => !s.hidden), vals = [];
  vis.forEach(s => s.vals.forEach(v => { if (v !== null) vals.push(v); }));
  const sc = scaleFor(vals, p), span = (p.xmax - p.xmin) || 1;
  const fx = x => PAD.l + iw * (x - p.xmin) / span, fy = v => PAD.t + ih * (1 - (v - sc.lo) / (sc.hi - sc.lo));
  p.fx = fx; p.fy = fy; p.iw = iw;
  let s = "";
  const n = Math.max(1, Math.round((sc.hi - sc.lo) / sc.step));
  for (let k = 0; k <= n; k++) { const v = sc.lo + k * sc.step, y = fy(v).toFixed(1);
    s += `<line x1="${PAD.l}" x2="${W - PAD.r}" y1="${y}" y2="${y}" stroke="var(--grid)"/><text x="${PAD.l - 8}" y="${(+y + 3.5).toFixed(1)}" text-anchor="end">${f(v, sc.dig)}</text>`; }
  for (const m of arr(p.missing)) { const a = fx(m[0]), b = fx(m[1]);
    s += `<rect x="${a.toFixed(1)}" y="${PAD.t}" width="${Math.max(1, b - a).toFixed(1)}" height="${ih}" fill="var(--line)" opacity=".7"><title>${t("stats.spolocne.bez_udajov")}</title></rect>`; }
  for (const tk of arr(p.ticks)) s += `<text x="${fx(tk.x).toFixed(1)}" y="${H - 6}" text-anchor="middle">${esc(tk.label)}</text>`;
  if (p.bars) {
    const bw = Math.max(2, Math.min(26, iw * (p.minDx || 1) / span * .72)), base = fy(Math.min(Math.max(0, sc.lo), sc.hi));
    for (const se of vis) se.vals.forEach((v, i) => { if (v === null || !Number.isFinite(se.xs[i])) return; const y = fy(v), top = Math.min(y, base), hh = Math.abs(base - y);
      if (hh < .5) return; s += `<rect x="${(fx(se.xs[i]) - bw / 2).toFixed(1)}" y="${top.toFixed(1)}" width="${bw.toFixed(1)}" height="${hh.toFixed(1)}" rx="2" fill="var(${se.color})" opacity=".85"/>`; });
    s += `<line x1="${PAD.l}" x2="${W - PAD.r}" y1="${base.toFixed(1)}" y2="${base.toFixed(1)}" stroke="var(--line-2)"/>`;
  } else {
    for (const se of vis) {
      const runs = []; let cur = null, px = null;
      se.vals.forEach((v, i) => { const x = se.xs[i]; if (v === null || !Number.isFinite(x)) { cur = null; return; }
        if (!cur || (p.maxGap && x - px > p.maxGap)) { cur = []; runs.push(cur); }
        cur.push([fx(x), fy(v)]); px = x; });
      const w = se.w || 2, da = se.dash ? ` stroke-dasharray="5 4"` : "";
      for (const r of runs) {
        if (r.length === 1) s += `<circle cx="${r[0][0].toFixed(1)}" cy="${r[0][1].toFixed(1)}" r="3" fill="var(${se.color})"/>`;
        else s += `<path d="${r.map((pt, i) => (i ? "L" : "M") + pt[0].toFixed(1) + " " + pt[1].toFixed(1)).join("")}" fill="none" stroke="var(${se.color})" stroke-width="${w}" stroke-linejoin="round" stroke-linecap="round"${da}/>`;
        if (p.dots && r.length > 1) for (const pt of r) s += `<circle cx="${pt[0].toFixed(1)}" cy="${pt[1].toFixed(1)}" r="2.4" fill="var(${se.color})"/>`;
      }
    }
  }
  s += `<line class="st-cross" x1="0" x2="0" y1="${PAD.t}" y2="${PAD.t + ih}" stroke="var(--ink-2)" stroke-opacity=".4" visibility="hidden"/><g class="st-dots"></g><rect class="st-hit" x="${PAD.l}" y="0" width="${iw}" height="${H}" fill="transparent"/>`;
  svg.innerHTML = s;
}
function nearIdx(xs, x, tol) {
  let bi = -1, bd = Infinity;
  for (let i = 0; i < xs.length; i++) { const d = Math.abs(xs[i] - x); if (d < bd) { bd = d; bi = i; } }
  return bd <= tol ? bi : -1;
}
/* skupina grafov so spoločným kurzorom: g = {plots, xs (referenčné x), tip(i) -> html} */
function attachGroup(g) {
  g.plots.forEach(p => {
    const hit = p.svg.querySelector(".st-hit"); if (!hit) return;
    const at = ev => {
      const r = p.svg.getBoundingClientRect(); if (!r.width || !g.xs.length) return;
      const px = (ev.clientX - r.left) * (p.W / r.width), x = p.xmin + (px - PAD.l) / p.iw * (p.xmax - p.xmin);
      groupHover(g, nearIdx(g.xs, x, Infinity), ev.clientX, ev.clientY);
    };
    hit.addEventListener("pointermove", at); hit.addEventListener("pointerdown", at);
    hit.addEventListener("pointerleave", () => groupHover(g, null)); hit.addEventListener("pointercancel", () => groupHover(g, null));
  });
}
function groupHover(g, i, cx, cy) {
  for (const p of g.plots) {
    const cr = p.svg.querySelector(".st-cross"), dots = p.svg.querySelector(".st-dots"); if (!cr || !dots || !p.fx) continue;
    if (i === null || i < 0) { cr.setAttribute("visibility", "hidden"); dots.innerHTML = ""; continue; }
    const x = p.fx(g.xs[i]); cr.setAttribute("x1", x); cr.setAttribute("x2", x); cr.setAttribute("visibility", "visible");
    let d = "";
    if (!p.bars) for (const se of p.series) { if (se.hidden) continue; let j = i;
      if (!se.aligned) j = nearIdx(se.xs, g.xs[i], p.tol || 0);
      const v = j >= 0 ? se.vals[j] : null; if (v === null || v === undefined) continue;
      d += `<circle cx="${p.fx(se.xs[j]).toFixed(1)}" cy="${p.fy(v).toFixed(1)}" r="4.5" fill="var(${se.color})" stroke="var(--panel)" stroke-width="2"/>`; }
    dots.innerHTML = d;
  }
  if (i === null || i < 0) { tipHide(); return; }
  tipShow(g.tip(i), cx, cy);
}
const mkLegend = (items, act) => items.map(it => it.nodata
  ? `<span class="st-lg nodata" title="${t("stats.spolocne.bez_udajov")}"><i style="background:var(${it.color})"></i>${esc(it.label)} · ${t("stats.spolocne.bez_udajov_m")}</span>`
  : `<button type="button" class="st-lg" data-act="${act}" data-sid="${esc(it.id)}" aria-pressed="${it.hidden ? "false" : "true"}"><i class="${it.dash ? "dash" : it.bar ? "bar" : ""}" style="background:var(${it.color});color:var(${it.color})"></i>${esc(it.label)}</button>`).join("");

const canDraw = host => !!(visible && root && host && host.clientWidth > 80 && root.offsetParent !== null);
const setMsg = (id, txt, cls) => { const el = gid(id); if (!el) return; el.textContent = txt || ""; el.className = "st-msg" + (cls ? " " + cls : ""); };
const stat = (l, v, u, d) => `<div class="stat"><div class="l">${l}</div><div class="v">${v}${u ? `<small>${u}</small>` : ""}</div>${d ? `<div class="d">${d}</div>` : ""}</div>`;
const nodata = x => `<div class="st-nod">${x}</div>`;

/* ---------- poruchy: spoločná karta (Deň aj Poruchy) ---------- */
function faultCard(x, canDel) {
  if (!isObj(x)) return "";
  const open = num(x.ts_end) === null, code = String(x.code || ""), title = x.title || (code ? t("stats.porucha.nazov_kod", {kod: code}) : t("stats.porucha.nazov"));
  let dm = num(x.duration_min); if (dm === null && !open && num(x.ts_start) !== null) dm = (num(x.ts_end) - num(x.ts_start)) / 60;
  const same = x.text && x.description && String(x.text).trim() === String(x.description).trim();
  let h = `<div class="st-fault" data-fid="${esc(x.id)}"><div class="st-fh"><b class="t">${esc(title)}</b>${code ? `<span class="tag">${esc(code)}</span>` : ""}` +
    `<span class="pill ${open ? "p-pozor" : "p-rutina"}">${open ? t("stats.porucha.otvorena") : t("stats.porucha.ukoncena")}</span></div>` +
    `<div class="st-fh"><span>${esc(t("stats.porucha.od", {t: dtm(x.ts_start)}))}</span><span>${esc(t("stats.porucha.do", {t: open ? t("stats.porucha.trva") : dtm(x.ts_end)}))}</span><span>${esc(t("stats.porucha.trvanie", {d: dur(dm)}))}</span><span class="tag">${esc(srcName(x.source) || x.source || "")}</span></div>`;
  if (x.text && !same) h += `<div class="st-fd">${esc(x.text)}</div>`;
  if (x.description) h += `<div class="st-fd">${esc(x.description)}</div>`;
  if (x.fix) h += `<div class="st-fix"><b>${t("stats.porucha.fix")}</b> ${esc(x.fix)}</div>`;
  if (canDel && x.source === "manual" && x.id != null) h += `<div class="st-fh st-fa" data-fid="${esc(x.id)}"><button type="button" class="btn sm danger" data-act="fault-ask" data-id="${esc(x.id)}">${t("stats.porucha.zmazat")}</button></div>`;
  return h + `</div>`;
}
const faultsHtml = (list, canDel) => list.length ? list.map(x => faultCard(x, canDel)).join("") : "";

/* ---------- dostupnosť dát ---------- */
function renderCoverage() {
  const el = gid("st-cov"); if (!el) return;
  const c = D.meta && isObj(D.meta.coverage) ? D.meta.coverage : null;
  if (!c) { el.textContent = D.meta === false ? t("stats.pokrytie.chyba") : t("stats.pokrytie.nacitavam"); return; }
  const fd = isObj(c.first_day) ? c.first_day : {};
  const items = [["vonku", fd.vonku], ["izby", fd.izby], ["voda", fd.voda], ["kompresor", fd.kompresor], ["energia", fd.energia]];
  const parts = [];
  const grp = (names, label) => { const sel = items.filter(i => names.includes(i[0]));
    const byDate = new Map(); sel.forEach(i => { const k = i[1] || ""; if (!byDate.has(k)) byDate.set(k, []); byDate.get(k).push(t("stats.pokrytie.n_" + i[0])); });
    for (const [date, ns] of byDate) { const nm = ns.length === sel.length && label ? label : ns.map((n, ix) => ix ? n : n.charAt(0).toUpperCase() + n.slice(1)).join(", ");
      parts.push(date ? `<span>${t("stats.pokrytie.od_html", {nm: esc(nm), d: fmtD(date)})}</span>` : `<span>${esc(t("stats.pokrytie.bez_dat", {nm}))}</span>`); } };
  grp(["vonku", "izby"], t("stats.pokrytie.vonku_izby")); grp(["voda", "kompresor", "energia"], t("stats.pokrytie.cerpadlo"));
  parts.push(fd.kwh_dom ? `<span>${t("stats.pokrytie.elektromer_html", {d: fmtD(fd.kwh_dom)})}</span>` : `<span>${t("stats.pokrytie.elektromer_bez")}</span>`);
  parts.push(fd.cop ? `<span>${t("stats.pokrytie.cop_html", {d: fmtD(fd.cop)})}</span>` : `<span>${t("stats.pokrytie.cop_bez")}</span>`);
  const extra = [];
  if (c.last_day) extra.push(t("stats.pokrytie.posledny_html", {d: fmtD(c.last_day)}));
  if (num(c.days_with_data) !== null) extra.push(`<b>${esc(f(c.days_with_data, 0))}</b> ${tn("stats.pokrytie.dni_s_datami", num(c.days_with_data))}`);
  if (num(c.hours) !== null) extra.push(`<b>${esc(f(c.hours, 0))}</b> ${tn("stats.pokrytie.hodin", num(c.hours))}`);
  el.innerHTML = `<span class="st-covl">${t("stats.pokrytie.titul")}</span>${parts.join("")}${extra.map(x => `<span>${x}</span>`).join("")}`;
}
async function loadMeta() {
  const my = ++seq.meta, r = await call("api/stats/meta");
  if (my !== seq.meta) return;
  if (r.error || !isObj(r.coverage) && !Array.isArray(r.metrics)) { if (!D.meta) D.meta = false; renderCoverage(); return; }
  D.meta = r; D.at.meta = Date.now();
  if (Array.isArray(r.metrics) && r.metrics.length) D.metrics = null;
  renderCoverage(); fillMetricSelects(); renderCal();
  if (D.day && S.view === "den") renderDay();       /* poznámka o kalibrácii v dlaždici spotreby */
}

/* ---------- export ---------- */
function exportUrl() {
  if (S.view === "den") return "api/stats/export?" + q({kind: "day", d: S.day, lang: window.LANG});
  if (S.view === "porovnanie") return "api/stats/export?" + q({kind: "compare", dates: S.cmp.join(","), lang: window.LANG});
  if (S.view === "obdobie") { const r = rngParams(); return r.ok ? "api/stats/export?" + q({kind: "range", from: r.from, to: r.to, group: S.rngGroup, lang: window.LANG}) : ""; }
  return "";
}
function updateExport() {
  const a = gid("st-export"); if (!a) return;
  const u = exportUrl();
  a.hidden = S.view === "poruchy" || S.view === "zdravie";
  a.hidden = S.view === "poruchy" || S.view === "sezona";
  if (u) { a.href = u; a.removeAttribute("aria-disabled"); a.title = t("stats.export.stiahnut"); } else { a.removeAttribute("href"); a.setAttribute("aria-disabled", "true"); a.title = t("stats.export.najprv_obdobie"); }
}

/* ---------- kalibrácia spotreby ---------- */
const calInfo = () => { const m = D.meta; if (!isObj(m)) return null; if (isObj(m.kalibracia)) return m.kalibracia; return isObj(m.coverage) && isObj(m.coverage.kalibracia) ? m.coverage.kalibracia : null; };
const calK = () => { const d = D.day && num(D.day.kalibracia_k); if (d !== null && d !== false && d !== undefined) return d; const c = calInfo(); return c ? num(c.k) : null; };
function kwhNote(s) {
  const cal = s.kwh_zdroj === "kalibrovany", k = calK();
  return cal ? `<span class="st-badge" style="margin-left:0">${t("stats.kwh.kalibrovane")}</span> ${t("stats.kwh.podla_elektromera")}${k !== null ? " ×" + f(k, 2) : ""}`
    : `<span class="st-badge" style="margin-left:0">${t("stats.kwh.odhad")}</span> ${t("stats.kwh.z_prudu")}`;
}
function renderCal() {
  const box = gid("st-day-cal"); if (!box) return;
  const c = calInfo();
  if (!c) { box.innerHTML = D.meta === false ? nodata(t("stats.kal.chyba")) : nodata(t("stats.kal.nacitavam")); return; }
  const k = num(c.k), nd = num(c.n_dni), nh = num(c.n_hodin), mp = num(c.mape);
  const when = c.datum ? String(c.datum).slice(0, 10) : (num(c.ts) !== null ? dstr(new Date(c.ts * 1000)) : "");
  let h = `<div class="stats st-stats">` +
    stat(t("stats.kal.koeficient"), k !== null ? "×" + f(k, 2) : "–", "", k !== null ? t("stats.kal.vzorec") : t("stats.kal.nekalibrovane")) +
    stat(t("stats.kal.dni"), f(nd === null ? 0 : nd, 0), "", nh !== null ? t("stats.kal.hodin_chodu", {n: f(nh, 0)}) : "") +
    stat(t("stats.kal.odchylka"), mp !== null && k !== null ? "±" + f(mp, 1) : "–", mp !== null && k !== null ? "%" : "", t("stats.kal.dennych_suctov")) +
    stat(t("stats.kal.prepocet"), when ? fmtD(when) : "–", "", t("stats.kal.prepocet_info")) + `</div>`;
  h += `<p class="sub" style="margin:14px 0 0">${t("stats.kal.popis")}</p>`;
  if (c.metoda === "noc") h += `<p class="sub" style="margin:8px 0 0">${t("stats.kal.noc")}</p>`;
  if (c.metoda === "noc_zaklad") h += `<p class="sub" style="margin:8px 0 0">${t("stats.kal.noc_zaklad", {kwh: f(c.noc_zaklad_kwh, 2)})}</p>`;
  const pr = isObj(c.prietok) ? c.prietok : null, pK = pr ? num(pr.kurenie) : null, pT = pr ? num(pr.tuv) : null;
  if (pK !== null || pT !== null) {
    const merany = D.meta && isObj(D.meta.coverage) && D.meta.coverage.cop_zdroj === "pevny_prietok";
    h += `<p class="sub" style="margin:8px 0 0">${t("stats.kal.prietok", {k: pK !== null ? f(pK, 1) + " l/min" : "–", t: pT !== null ? f(pT, 1) + " l/min" : "–", zdroj: merany ? t("stats.kal.merany") : t("stats.kal.tabulky")})}</p>`;
  }
  if (k === null) h += `<div class="callout warn st-warn" role="note" style="margin-top:12px">${esc(c.dovod || t("stats.kal.malo_dat"))}</div>`;
  else if (c.dovod) h += `<p class="sub" style="margin:8px 0 0">${esc(c.dovod)}</p>`;
  if (c.base_zdroj && /regres/i.test(String(c.base_zdroj))) h += `<p class="sub" style="margin:8px 0 0">${t("stats.kal.regresia", {kwh: f(c.base_kwh, 2)})}</p>`;
  box.innerHTML = h;
}

/* ---------- Deň ---------- */
function tilesHtml(s) {
  const hb = D.meta && num(D.meta.hdd_base);
  const flag = v => num(v) > 0 ? `<span class="flag">${f(v, 0)}</span>` : f(v, 0);
  const tiles = [
    stat(t("stats.dlazdica.vonku"), f(s.vonku), "°C", t("stats.dlazdica.vonku_rozsah", {min: f(s.vonku_min), max: f(s.vonku_max)})),
    stat(t("stats.dlazdica.ciel"), f(s.ciel), "°C", t("stats.dlazdica.ciel_info")),
    stat(t("stats.dlazdica.voda"), f(s.tw2), "°C", t("stats.dlazdica.voda_info", {tw1: f(s.tw1), wl: f(s.wl)})),
    stat(t("stats.dlazdica.dt"), f(s.dt_beh), "°C", t("stats.dlazdica.dt_info")),
    stat(t("stats.dlazdica.tuv"), f(s.tuv), "°C", t("stats.dlazdica.tuv_info", {max: f(s.tuv_max), min: f(s.tuv_min, 0)})),
    stat(t("stats.dlazdica.beh"), f(s.beh_pct, 0), "%", t("stats.dlazdica.beh_info", {beh: f(s.beh_min, 0), kur: f(s.kurenie_min, 0)})),
    stat(t("stats.dlazdica.starty"), f(s.starty, 0), "", t("stats.dlazdica.starty_info")),
    stat(t("stats.dlazdica.odmrazovanie"), f(s.odmraz_min, 0), "min", ""),
    stat(t("stats.dlazdica.cop"), f(s.cop, 2), "", num(s.cop) === null ? t("stats.dlazdica.cop_nie") :
      t("stats.dlazdica.cop_info", {q: f(s.q_kwh, 0), kwh: f(s.kwh_spotreba, 0)}) +
      (num(s.cop_kurenie) !== null || num(s.cop_tuv) !== null ? t("stats.dlazdica.cop_rozpis", {kur: f(s.cop_kurenie, 2), tuv: f(s.cop_tuv, 2)}) : "")),
    stat(t("stats.dlazdica.spotreba"), f(num(s.kwh_spotreba) !== null ? s.kwh_spotreba : s.kwh, 1), "kWh", kwhNote(s)),
    stat(t("stats.dlazdica.dom"), f(s.kwh_dom, 1), "kWh", num(s.kwh_dom) === null ? t("stats.dlazdica.dom_nie") :
      (num(s.kwh_spotreba) !== null && num(s.kwh_dom) > 0 ? t("stats.dlazdica.dom_cerpadlo", {p: f(100 * s.kwh_spotreba / s.kwh_dom, 0)}) : t("stats.dlazdica.dom_elektromer"))),
    stat(t("stats.dlazdica.hdd"), f(s.hdd, 1), "°C·d", hb !== null && hb !== false ? t("stats.dlazdica.hdd_zaklad", {hb: f(hb, 0)}) : ""),
    stat(t("stats.dlazdica.kwh_hdd"), f(s.kwh_hdd, 2), "", num(s.kwh_hdd) === null ? t("stats.dlazdica.kwh_hdd_malo") : t("stats.dlazdica.kwh_hdd_info")),
    stat(t("stats.dlazdica.poruchy"), flag(s.poruch), "", t("stats.dlazdica.poruchy_info")),
    stat(t("stats.dlazdica.hodin"), f(s.hodin_s_datami, 0), "h", "")
  ];
  return tiles.join("");
}
function roomsHtml(s) {
  const keys = roomKeys().filter(k => num(s[k]) !== null);
  if (!keys.length) return "";
  return `<div class="st-rooms"><span class="mut">${t("stats.izby_priemer")}</span>${keys.map(k => `<span>${esc(M(k).label)} <b>${f(s[k])} °C</b></span>`).join("")}</div>`;
}
function renderTimeline(d) {
  const hrs = arr(d.hours), tl = arr(d.timeline).filter(x => isObj(x) && num(x.from) !== null && num(x.to) !== null && num(x.to) > num(x.from));
  const box = gid("st-day-tl");
  if (!tl.length) { box.innerHTML = nodata(t("stats.os.nie_je")); return; }
  let t0 = hrs.length ? num(hrs[0].ts) : null, t1 = hrs.length ? num(hrs[hrs.length - 1].ts) : null;
  if (t0 === null) t0 = Math.min(...tl.map(x => +x.from)); t1 = t1 === null ? Math.max(...tl.map(x => +x.to)) : t1 + 3600;
  const span = Math.max(1, t1 - t0), tot = {};
  const segs = tl.map((x, i) => { const a = Math.max(t0, +x.from), b = Math.min(t1, +x.to); tot[x.state] = (tot[x.state] || 0) + Math.max(0, b - a) / 60;
    return `<i class="st-s-${esc(x.state)}" data-i="${i}" style="left:${((a - t0) / span * 100).toFixed(3)}%;width:${(Math.max(0, b - a) / span * 100).toFixed(3)}%"></i>`; }).join("");
  const sumTxt = TL_KEYS.filter(k => tot[k]).map(k => tlName(k) + " " + dur(tot[k])).join(", ");
  let ticks = "";
  hrs.forEach((h, i) => { const hh = num(h.h) !== null ? num(h.h) : i; if (hh % 3) return; const pos = (num(h.ts) - t0) / span * 100; if (!Number.isFinite(pos)) return;
    ticks += `<span class="st-tk${i === 0 ? " first" : ""}${hh % 6 ? " mi" : ""}" style="left:${pos.toFixed(3)}%">${pad2(hh)}:00</span>`; });
  box.innerHTML = `<div class="st-tl" id="st-tl-bar" role="img" aria-label="${esc(t("stats.os.aria", {sum: sumTxt}))}">${segs}</div><div class="st-tkr" aria-hidden="true">${ticks}</div>` +
    `<div class="st-key">${TL_KEYS.map(k => `<span><i class="st-sw st-s-${k}"></i>${tlName(k)}${tot[k] ? " · " + esc(dur(tot[k])) : ""}</span>`).join("")}</div>`;
  box._tl = tl;
}
function renderHours() {
  const d = D.day, hrs = d ? arr(d.hours) : [], box = gid("st-day-hours"); if (!box) return;
  const cg = colGroups();
  const cols = cg.filter(g => S.cols[g[0]]).flatMap(g => g[2]);
  const metricKeys = cg.flatMap(g => g[2]).filter(k => k !== "n" && k !== "src");
  gid("st-hours-info").textContent = hrs.length ? hrs.length + " h" : "";
  if (!cols.length) { box.innerHTML = nodata(t("stats.hodiny.ziadna_skupina")); return; }
  const head = "<tr><th>" + t("stats.hodiny.hodina") + "</th>" + cols.map(k => k === "n" ? `<th>${t("stats.hodiny.vzoriek")}</th>` : k === "src" ? `<th class="t">${t("stats.hodiny.zdroj")}</th>` : `<th>${esc(M(k).label)}<small>${esc(mUnit(k))}</small></th>`).join("") + "</tr>";
  const rows = hrs.map((h, i) => { const hh = num(h.h) !== null ? num(h.h) : i, has = metricKeys.some(k => num(h[k]) !== null);
    return `<tr class="${has ? "" : "nodata"}"><td>${pad2(hh)}:00</td>` + cols.map(k => k === "src" ? `<td class="t">${esc(hsrcName(h.src) || h.src || "–")}</td>` : k === "n" ? `<td>${f(h.n, 0)}</td>` : `<td>${fm(k, h[k])}</td>`).join("") + "</tr>"; }).join("");
  box.innerHTML = `<table class="st-tbl"><thead>${head}</thead><tbody>${rows}</tbody></table>`;
}
function renderCycles() {
  const list = D.day ? arr(D.day.cycles).filter(isObj) : [], box = gid("st-day-cycles"); if (!box) return;
  const tot = list.reduce((a, c) => a + (num(c.minutes) || 0), 0);
  gid("st-cyc-info").textContent = list.length ? t("stats.cykly.suhrn", {n: list.length, d: dur(tot)}) : "";
  if (!list.length) { box.innerHTML = nodata(t("stats.cykly.ziadny")); return; }
  box.innerHTML = `<table class="st-tbl"><thead><tr><th>${t("stats.cykly.start")}</th><th>${t("stats.cykly.koniec")}</th><th>${t("stats.cykly.trvanie")}</th><th class="t">${t("stats.cykly.rezim")}</th><th>${t("stats.cykly.hz_max")}<small>${t("stats.cykly.hz_avg")}</small></th><th>${t("stats.cykly.vonku")}<small>°C</small></th><th>${t("stats.cykly.vystup_max")}<small>°C</small></th><th class="t">${t("stats.cykly.dovod")}</th></tr></thead><tbody>` +
    list.map(c => `<tr><td>${num(c.ts_start) !== null ? hm(c.ts_start) : "–"}</td><td>${num(c.ts_stop) !== null ? hm(c.ts_stop) : t("stats.cykly.bezi")}</td><td>${esc(dur(c.minutes))}</td><td class="t">${esc(modeName(c.mode) || c.mode || "–")}</td>` +
      `<td>${f(c.f_max, 0)}<small class="st-dif">${f(c.f_avg, 0)}</small></td><td>${f(c.vonku)}</td><td>${f(c.tw2_max)}</td><td class="t">${esc(c.reason || "–")}</td></tr>`).join("") + `</tbody></table>`;
}
function drawDayCharts() {
  const host = gid("st-day-charts"); if (!host) return;
  const d = D.day;
  /* spotreba čerpadla = kalibrovaná (kwh_cal), ak je, inak odhad (kwh) */
  const hrs = d ? arr(d.hours).map(h => isObj(h) ? Object.assign({}, h, {kwh_pump: num(h.kwh_cal) !== null ? h.kwh_cal : h.kwh}) : h) : [];
  if (!d || !hrs.length) { host.innerHTML = ""; return; }
  if (!canDraw(host)) return;
  const pumpCal = hrs.some(h => isObj(h) && num(h.kwh_cal) !== null);
  const SLBL = {kwh_pump: pumpCal ? t("stats.graf.cerpadlo_kal") : t("stats.graf.cerpadlo_odhad"), kwh_dom: t("stats.graf.dom_elektromer")};
  const W = host.clientWidth, N = hrs.length, xs = hrs.map((_, i) => i), step = W < 620 ? 6 : 3, ticks = [];
  hrs.forEach((h, i) => { const hh = num(h.h) !== null ? num(h.h) : i; if (hh % step === 0) ticks.push({x: i, label: pad2(hh) + ":00"}); });
  const cards = dayCards().map(card => {
    const pl = card.plots.map(pc => ({pc, series: pc.keys.map(k => { const sd = serOf(k);
      const se = {id: "d:" + k, key: k, label: SLBL[k] || M(k).label, color: valCol(k), dash: !!sd.dash, bar: !!sd.bar, w: 2, xs, vals: hrs.map(h => num(h[k])), aligned: true, hidden: !!hiddenSer["d:" + k]};
      se.has = se.vals.some(v => v !== null); return se; })})).filter(x => x.series.filter(s => s.has).length >= (x.pc.need || 1));
    const items = pl.flatMap(x => x.series).filter((s, i, a) => s.has && a.findIndex(o => o.id === s.id && o.has) === i);
    return {card, pl, items, none: !pl.length};
  });
  host.innerHTML = cards.map(c => `<div class="st-card"><div class="st-card-h"><div class="st-card-t">${esc(c.card.title)}</div><div class="st-leg">${mkLegend(c.items, "lg")}</div></div>` +
    (c.none ? nodata(esc(c.card.nomsg || t("stats.graf.ziadne_udaje"))) : c.pl.map(x => `<div class="st-plot-t">${esc(x.pc.label)}</div><svg class="st-svg" data-p="${x.pc.id}" role="img" aria-label="${esc(c.card.title + ": " + x.pc.label)}"></svg>`).join("")) + `</div>`).join("");
  const plots = [];
  for (const c of cards) for (const x of c.pl) {
    const svg = host.querySelector(`svg[data-p="${x.pc.id}"]`); if (!svg) continue;
    const miss = []; hrs.forEach((_, i) => { if (x.series.every(s => s.vals[i] === null)) miss.push([i - .5, i + .5]); });
    const p = {svg, W, H: x.pc.h, series: x.series.filter(s => s.has), xmin: -.5, xmax: N - .5, ticks, fixed: x.pc.fixed, zero: x.pc.zero, bars: !!x.pc.bars, minDx: 1, maxGap: 1.5, missing: miss, tol: .6};
    drawPlot(p); plots.push(p);
  }
  const tip = i => { const h = hrs[i] || {}, hh = num(h.h) !== null ? num(h.h) : i; let html = `<b>${pad2(hh)}:00 – ${pad2((hh + 1) % 24)}:00</b>`, first = true;
    const seen = new Set();
    for (const c of cards) { if (c.none) continue; const rows = [];
      for (const x of c.pl) for (const s of x.series) { if (!s.has || s.hidden || seen.has(s.id)) continue; seen.add(s.id); const v = s.vals[i]; rows.push(trow(s.color, esc(s.label), v === null ? "–" : esc(fmu(s.key, v)), v === null)); }
      if (!rows.length) continue; html += (first ? "" : "<hr>") + rows.join(""); first = false; }
    if (num(h.n) !== null) html += `<hr><div class="s">${t("stats.graf.vzoriek_v_hodine", {n: f(h.n, 0)})}</div>`;
    return html; };
  attachGroup({plots, xs, tip});
}
function renderDay() {
  const d = D.day; if (!d) return;
  const s = isObj(d.summary) ? d.summary : {}, faults = arr(d.faults).filter(isObj);
  const has = d.has_data === true || (d.has_data === undefined && num(s.hodin_s_datami) > 0);
  gid("st-day").value = S.day; gid("st-day").max = todayS();
  gid("st-day-title").textContent = new Date(S.day + "T12:00:00").toLocaleDateString(LOC(), {weekday: "long", day: "numeric", month: "long", year: "numeric"});
  gid("st-day-badge").innerHTML = S.day === todayS() ? `<span class="pill p-info">${t("stats.den.dnes_prebieha")}</span>` : "";
  gid("st-next").disabled = S.day >= todayS();
  gid("st-day-sum").hidden = false;
  gid("st-day-tiles").hidden = !has; gid("st-day-nodata").hidden = has; gid("st-day-tlwrap").hidden = !has;
  for (const id of ["st-day-p2", "st-day-p3", "st-day-p4"]) gid(id).hidden = !has;
  gid("st-day-p5").hidden = !has && !faults.length;
  if (has) {
    gid("st-day-tiles").innerHTML = tilesHtml(s); gid("st-day-rooms").innerHTML = roomsHtml(s);
    renderTimeline(d); drawDayCharts(); renderHours(); renderCycles();
  } else { gid("st-day-rooms").innerHTML = ""; gid("st-day-charts").innerHTML = ""; }
  gid("st-dfault-info").textContent = faults.length ? String(faults.length) : "";
  gid("st-day-faults").innerHTML = faults.length ? faultsHtml(faults, true) : nodata(t("stats.den.ziadna_porucha"));
  updateExport();
}
async function loadDay() {
  const day = S.day, my = ++seq.den;
  setMsg("st-day-msg", t("stats.spolocne.nacitavam"));
  const r = await call("api/stats/day?" + q({d: day}));
  if (my !== seq.den) return;
  if (r.error || !Array.isArray(r.hours)) {
    D.day = null; D.at.den = 0; setMsg("st-day-msg", t("stats.den.chyba", {e: r.error || t("stats.spolocne.neplatna_odpoved")}), "err");
    gid("st-day-sum").hidden = true; for (const id of ["st-day-p2", "st-day-p3", "st-day-p4", "st-day-p5"]) gid(id).hidden = true;
    gid("st-day-title").textContent = fmtD(day); return;
  }
  D.day = r; D.at.den = Date.now(); D.key.den = day; setMsg("st-day-msg", "");
  renderDay();
}
function gotoDay(d) { if (!validD(d) || d > todayS()) return; S.day = d; saveState(); updateExport(); gid("st-day").value = d; gid("st-next").disabled = d >= todayS(); loadDay(); }
const shiftDay = n => gotoDay(addDays(S.day, n));

/* ---------- výber metriky ---------- */
function metricOptions(sel) {
  const order = [], by = {};
  for (const m of metricList()) { const g = m.group || "kvalita"; if (!by[g]) { by[g] = []; order.push(g); } by[g].push(m); }
  return order.map(g => `<optgroup label="${esc(grpName(g))}">` + by[g].map(m => `<option value="${esc(m.key)}"${m.key === sel ? " selected" : ""}>${esc(M(m.key).label)}${m.unit ? " (" + esc(m.unit) + ")" : ""}</option>`).join("") + `</optgroup>`).join("");
}
function fillMetricSelects() {
  const a = gid("st-cmp-metric"), b = gid("st-rng-metric");
  if (a) { a.innerHTML = metricOptions(S.cmpMetric); if (a.value) S.cmpMetric = a.value; }
  if (b) { b.innerHTML = metricOptions(S.rngMetric); if (b.value) S.rngMetric = b.value; }
}

/* ---------- Porovnanie ---------- */
const cmpItems = () => D.cmp ? arr(D.cmp.dates).filter(isObj) : [];
const dayLabel = it => (it.label && it.label !== it.date ? it.label + " · " : "") + (validD(it.date) ? fmtD(it.date) : String(it.date || ""));
function cmpLabelFor(d) { const it = cmpItems().find(x => x.date === d); return it && it.label && it.label !== d ? it.label + " · " + fmtD(d) : fmtD(d); }
function renderCmpControls() {
  const qb = gid("st-cmp-quick"), sb = gid("st-cmp-sel"); if (!qb || !sb) return;
  gid("st-cmp-base").value = S.cmp[0] || "";
  if (D.quick === false) qb.innerHTML = `<span class="st-msg err">${t("stats.porovnanie.rychle_chyba")}</span>`;
  else if (!D.quick) qb.innerHTML = `<span class="st-msg">${t("stats.spolocne.nacitavam")}</span>`;
  else qb.innerHTML = D.quick.map(it => { const on = S.cmp.includes(it.date), dis = !on && !it.has_data;
    return `<button type="button" class="chip st-ch" data-act="cmp-chip" data-d="${esc(it.date)}" aria-pressed="${on}"${dis ? ` disabled title="${t("stats.porovnanie.bez_dat_den")}"` : ` title="${esc(fmtD(it.date))}"`}>${esc(it.label || it.key || it.date)} <small>${dis ? t("stats.spolocne.bez_dat") : esc(fmtDS(it.date))}</small></button>`; }).join("") || `<span class="st-msg">${t("stats.porovnanie.ziadne_rychle")}</span>`;
  sb.innerHTML = S.cmp.map((d, i) => `<span class="st-sel${i === 0 ? " first" : ""}"><i class="st-dot" style="background:var(--s${i + 1})"></i>${esc(cmpLabelFor(d))}${i === 0 ? " · " + t("stats.porovnanie.zaklad") : ""}` +
    `<button type="button" data-act="cmp-del" data-d="${esc(d)}" aria-label="${esc(t("stats.porovnanie.odobrat", {d: fmtD(d)}))}"${S.cmp.length < 2 ? " disabled" : ""}>✕</button></span>`).join("");
}
async function loadQuick() {
  const base = S.cmp[0] || todayS(), my = ++seq.quick;
  D.quick = null; D.quickBase = base; renderCmpControls();
  const r = await call("api/stats/quick?" + q({base}));
  if (my !== seq.quick) return;
  D.quick = r.error ? false : arr(r.items).filter(x => isObj(x) && validD(x.date));
  renderCmpControls();
}
async function loadCmp() {
  const dates = S.cmp.slice(), my = ++seq.porovnanie;
  setMsg("st-cmp-msg", t("stats.spolocne.nacitavam"));
  const r = await call("api/stats/compare?" + q({dates: dates.join(",")}));
  if (my !== seq.porovnanie) return;
  if (r.error || !Array.isArray(r.dates)) {
    D.cmp = null; D.at.porovnanie = 0; setMsg("st-cmp-msg", t("stats.porovnanie.chyba", {e: r.error || t("stats.spolocne.neplatna_odpoved")}), "err");
    gid("st-cmp-p2").hidden = true; gid("st-cmp-p3").hidden = true; renderCmpControls(); return;
  }
  D.cmp = r; D.at.porovnanie = Date.now(); D.key.porovnanie = dates.join(",");
  const ml = arr(r.metrics).filter(x => isObj(x) && typeof x.key === "string");
  if (ml.length) { D.metrics = ml; fillMetricSelects(); }
  setMsg("st-cmp-msg", ""); renderCmp();
}
function renderCmp() {
  const items = cmpItems();
  gid("st-cmp-p2").hidden = !items.length; gid("st-cmp-p3").hidden = !items.length;
  renderCmpControls();
  const temps = items.filter(it => it.has_data !== false).map(it => num(isObj(it.summary) ? it.summary.vonku : null)).filter(v => v !== null);
  gid("st-cmp-warn").innerHTML = temps.length >= 2 && Math.max(...temps) - Math.min(...temps) > 3
    ? `<div class="callout warn st-warn" role="note">${t("stats.porovnanie.varovanie", {d: f(Math.max(...temps) - Math.min(...temps)), a: f(Math.min(...temps)), b: f(Math.max(...temps))})}</div>` : "";
  drawCmpChart(); renderCmpTable(); updateExport();
}
function drawCmpChart() {
  const host = gid("st-cmp-chart"); if (!host) return;
  const items = cmpItems();
  if (!items.length) { host.innerHTML = ""; return; }
  if (!canDraw(host)) return;
  const key = S.cmpMetric, m = M(key), W = host.clientWidth, xs = Array.from({length: 24}, (_, i) => i);
  const series = items.map((it, i) => { const vals = new Array(24).fill(null);
    if (it.has_data !== false) arr(it.hours).forEach(h => { const hh = isObj(h) ? num(h.h) : null; if (hh !== null && hh >= 0 && hh < 24) vals[hh] = num(h[key]); });
    const id = "c:" + it.date; return {id, key, label: dayLabel(it), color: "--s" + (i + 1), w: i === 0 ? 3 : 1.8, xs, vals, aligned: true, hidden: !!hiddenSer[id], has: vals.some(v => v !== null), nodata: !vals.some(v => v !== null)}; });
  const head = `<div class="st-card-h"><div class="st-card-t">${esc(m.label)} <span class="mut" style="font-weight:400">${esc(m.unit)}</span></div><div class="st-leg">${mkLegend(series, "lg")}</div></div>`;
  if (!series.some(s => s.has)) { host.innerHTML = `<div class="st-card" style="margin-top:0">${head}${nodata(t("stats.porovnanie.ziadne_data_metrika"))}</div>`; return; }
  host.innerHTML = `<div class="st-card" style="margin-top:0">${head}<svg class="st-svg" data-p="cmp" role="img" aria-label="${esc(t("stats.porovnanie.aria_graf", {m: m.label, dni: tn("stats.spolocne.dni", series.length)}))}"></svg></div>`;
  const step = W < 620 ? 6 : 3, ticks = []; for (let h = 0; h < 24; h += step) ticks.push({x: h, label: pad2(h) + ":00"});
  const miss = []; for (let i = 0; i < 24; i++) if (series.every(s => s.vals[i] === null)) miss.push([i - .5, i + .5]);
  const p = {svg: host.querySelector("svg"), W, H: 280, series: series.filter(s => s.has), xmin: -.5, xmax: 23.5, ticks, zero: m.unit !== "°C", bars: false, minDx: 1, maxGap: 1.5, missing: miss, tol: .6, dots: false};
  drawPlot(p);
  const tip = i => { let html = `<b>${pad2(i)}:00 – ${pad2((i + 1) % 24)}:00</b>`;
    for (const s of series) { if (s.hidden || !s.has) continue; const v = s.vals[i]; html += trow(s.color, esc(s.label), v === null ? "–" : esc(fmu(key, v)), v === null); }
    return html; };
  attachGroup({plots: [p], xs, tip});
}
function renderCmpTable() {
  const box = gid("st-cmp-table"); if (!box) return;
  const items = cmpItems();
  if (!items.length) { box.innerHTML = ""; return; }
  const sums = items.map(it => it.has_data !== false && isObj(it.summary) ? it.summary : {});
  const defs = [], seen = new Set();
  for (const m of metricList()) if (sums.some(s => num(s[m.key]) !== null)) { defs.push(M(m.key)); seen.add(m.key); }
  for (const k of ["vonku_min", "vonku_max", "beh_pct", "tuv_max", "kwh_spotreba", "cop_kurenie", "cop_tuv", "hdd", "kwh_hdd", "hodin_s_datami", "poruch"]) if (!seen.has(k) && sums.some(s => num(s[k]) !== null)) defs.push(M(k));
  const gi = g => { const i = GROUP_KEYS.indexOf(g); return i < 0 ? 99 : i; };
  defs.sort((a, b) => gi(a.group) - gi(b.group));
  if (!defs.length) { box.innerHTML = nodata(t("stats.porovnanie.ziadne_data_tabulka")); return; }
  const head = `<tr><th>${t("stats.porovnanie.ukazovatel")}</th>` + items.map((it, i) => `<th class="${i === 0 ? "sel" : ""}"><i class="st-dot" style="background:var(--s${i + 1})"></i>${esc(it.label && it.label !== it.date ? it.label : fmtD(it.date))}<small>${esc(fmtD(it.date))}${it.has_data === false ? " · " + t("stats.spolocne.bez_dat") : i === 0 ? " · " + t("stats.porovnanie.zaklad") : ""}</small></th>`).join("") + `</tr>`;
  let body = "", lastG = null;
  for (const d of defs) {
    if (d.group !== lastG) { lastG = d.group; body += `<tr class="sec"><td colspan="${items.length + 1}">${esc(grpName(d.group))}</td></tr>`; }
    const dec = num(d.decimals) === null ? 1 : d.decimals, base = num(sums[0][d.key]);
    body += `<tr><td>${esc(d.label)}${d.unit ? ` <span class="mut">${esc(d.unit)}</span>` : ""}</td>` + sums.map((s, i) => { const v = num(s[d.key]); let dif = "";
      if (i > 0 && v !== null && base !== null) { const ab = v - base, pc = base !== 0 ? ab / Math.abs(base) * 100 : null; dif = `<span class="st-dif">${esc(sf(ab, dec))}${d.unit ? " " + esc(d.unit) : ""}${pc !== null ? " · " + esc(sf(pc, 1)) + " %" : ""}</span>`; }
      return `<td class="${i === 0 ? "sel" : ""}">${esc(f(v, dec))}${dif}</td>`; }).join("") + `</tr>`;
  }
  box.innerHTML = `<div class="st-scroll"><table class="st-tbl"><thead>${head}</thead><tbody>${body}</tbody></table></div><p class="sub" style="margin:12px 0 0">${t("stats.porovnanie.poznamka")}</p>`;
}
function cmpChanged() {
  S.cmp = S.cmp.slice(0, 6); saveState(); updateExport(); renderCmpControls();
  if (D.quickBase !== S.cmp[0]) loadQuick();
  loadCmp();
}
function cmpAdd(d, fromChip) {
  if (!validD(d)) { setMsg("st-cmp-msg", t("stats.porovnanie.platny_datum"), "err"); return; }
  if (d > todayS()) { setMsg("st-cmp-msg", t("stats.porovnanie.buducnost"), "err"); return; }
  const i = S.cmp.indexOf(d);
  if (i >= 0) { if (fromChip) { if (S.cmp.length < 2) { setMsg("st-cmp-msg", t("stats.porovnanie.aspon_jeden"), "err"); return; } S.cmp.splice(i, 1); cmpChanged(); } else setMsg("st-cmp-msg", t("stats.porovnanie.uz_vybrany"), "err"); return; }
  if (S.cmp.length >= 6) { setMsg("st-cmp-msg", t("stats.porovnanie.najviac_6"), "err"); return; }
  S.cmp.push(d); cmpChanged();
}

/* ---------- Obdobie ---------- */
function rngParams() {
  const td = todayS(); let from, to = td;
  switch (S.rngPreset) {
    case "7": from = addDays(td, -6); break;
    case "30": from = addDays(td, -29); break;
    case "month": from = td.slice(0, 8) + "01"; break;
    case "year": from = td.slice(0, 4) + "-01-01"; break;
    default: from = S.rngFrom; to = S.rngTo;
  }
  if (!validD(from) || !validD(to)) return {ok: false, msg: t("stats.obdobie.zadaj_platne")};
  if (from > to) return {ok: false, msg: t("stats.obdobie.zaciatok_po_konci")};
  const days = Math.round((utcMs(to) - utcMs(from)) / 86400000) + 1;
  if (S.rngGroup === "day" && days > 800) return {ok: false, msg: t("stats.obdobie.max_800")};
  return {ok: true, from, to, days};
}
const rngKey = () => { const p = rngParams(); return p.ok ? [p.from, p.to, S.rngGroup, S.rngYoy].join("|") : "x"; };
function rowStart(r) {
  if (typeof r.from === "string" && validD(r.from.slice(0, 10))) return utcMs(r.from.slice(0, 10));
  const k = String(r.key || ""); let m;
  if ((m = /^(\d{4})-W(\d{1,2})$/.exec(k))) return isoWeekStart(+m[1], +m[2]);
  if ((m = /^(\d{4})-(\d{2})$/.exec(k))) return Date.UTC(+m[1], +m[2] - 1, 1);
  if (validD(k)) return utcMs(k);
  return null;
}
const shiftYearMs = ms => { const d = new Date(ms); return Date.UTC(d.getUTCFullYear() + 1, d.getUTCMonth(), d.getUTCDate()); };
const stepMs = g => g === "week" ? 7 * 86400000 : g === "month" ? 30.44 * 86400000 : 86400000;
function rowLabel(r) {
  const st = rowStart(r), s = st === null ? "" : msToS(st);
  if (S.rngGroup === "month" && s) return t("stats.mesiac." + (+s.slice(5, 7))) + " " + s.slice(0, 4);
  if (S.rngGroup === "week") return String(r.key || "") + (s ? " (" + fmtDS(s) + (validD(String(r.to || "").slice(0, 10)) ? " – " + fmtDS(String(r.to).slice(0, 10)) : "") + ")" : "");
  return s ? fmtDW(s) + " " + s.slice(0, 4) : String(r.key || "");
}
async function loadRng() {
  const p = rngParams(), my = ++seq.obdobie;
  updateExport();
  if (!p.ok) { D.rng = null; D.at.obdobie = 0; setMsg("st-rng-msg", p.msg, "err"); gid("st-rng-p2").hidden = true; gid("st-rng-p3").hidden = true; return; }
  setMsg("st-rng-msg", t("stats.spolocne.nacitavam"));
  const cur = call("api/stats/range?" + q({from: p.from, to: p.to, group: S.rngGroup}));
  const prev = S.rngYoy ? call("api/stats/range?" + q({from: addYears(p.from, -1), to: addYears(p.to, -1), group: S.rngGroup})) : Promise.resolve(null);
  const res = await Promise.all([cur, prev]), r = res[0], pr = res[1];
  if (my !== seq.obdobie) return;
  if (r.error || !Array.isArray(r.rows)) { D.rng = null; D.at.obdobie = 0; setMsg("st-rng-msg", t("stats.obdobie.chyba", {e: r.error || t("stats.spolocne.neplatna_odpoved")}), "err"); gid("st-rng-p2").hidden = true; gid("st-rng-p3").hidden = true; return; }
  D.rng = r; D.rngPrev = pr && !pr.error && Array.isArray(pr.rows) ? pr : null; D.rngPrevErr = !!pr && !D.rngPrev;
  D.at.obdobie = Date.now(); D.key.obdobie = rngKey();
  setMsg("st-rng-msg", D.rngPrevErr ? t("stats.obdobie.rok_chyba") : "", D.rngPrevErr ? "err" : "");
  renderRng();
}
let sezRok = null;
async function loadSezona() {
  const my = ++seq.sezona;
  setMsg("st-sez-msg", t("stats.spolocne.nacitavam"));
  const r = await call("api/sezona" + (sezRok ? "?rok=" + sezRok : ""));
  if (my !== seq.sezona) return;
  if (r.error || !isObj(r.sezona)) { D.sez = null; D.at.sezona = 0; setMsg("st-sez-msg", r.error || t("stats.sezona.chyba"), "err"); gid("st-sez-table").innerHTML = ""; gid("st-sez-ev").hidden = true; return; }
  D.sez = r; sezRok = r.sezona.rok; D.at.sezona = Date.now(); D.key.sezona = String(sezRok);
  setMsg("st-sez-msg", "");
  renderSezona();
}
function renderSezona() {
  const r = D.sez; if (!r) return;
  const s = r.sezona, p = r.predchadzajuca, por = r.porovnanie;
  gid("st-sez-info").textContent = t("stats.sezona.sezona") + " " + s.nazov + " · " + fmtD(s.od) + " – " + fmtD(s.do) + (s.prebieha ? " · " + t("stats.sezona.prebieha") : "");
  const box = gid("st-sez-table"), evb = gid("st-sez-ev");
  if (!s.ma_data) { box.innerHTML = `<p class="sub">${t("stats.sezona.nema_dat")}</p>`; gid("st-sez-por").textContent = ""; evb.hidden = true; return; }
  const row = (k, v, pv, d) => `<tr><th class="t">${t("stats.sezona." + k)}</th><td>${f(v, d)}</td>${p ? `<td>${f(pv, d)}</td>` : ""}</tr>`;
  const pv = x => p ? p[x] : null;
  let body = row("r_kwh", s.kwh, pv("kwh"), 0) + row("r_teplo", s.teplo_kwh, pv("teplo_kwh"), 0) + row("r_cop", s.cop, pv("cop"), 2) + row("r_cena", s.cena_eur, pv("cena_eur"), 0) +
    row("r_hdd", s.hdd, pv("hdd"), 0) + row("r_kwh_hdd", s.kwh_hdd, pv("kwh_hdd"), 2) + row("r_vonku", s.vonku, pv("vonku"), 1);
  for (const x of arr(s.izby)) { const o = p ? arr(p.izby).find(y => y.kluc === x.kluc) : null; body += `<tr><th class="t">${t("stats.sezona.r_izba", {izba: esc(x.nazov)})}</th><td>${f(x.priemer, 1)}</td>${p ? `<td>${f(o ? o.priemer : null, 1)}</td>` : ""}</tr>`; }
  body += row("r_rozptyl", s.rozptyl, pv("rozptyl"), 2) + row("r_starty", s.starty, pv("starty"), 0) + row("r_odmraz", s.odmraz_min, pv("odmraz_min"), 0) + row("r_dni", s.dni_s_udajmi, pv("dni_s_udajmi"), 0);
  box.innerHTML = `<div class="st-scroll"><table class="st-tbl"><thead><tr><th class="t">${t("stats.sezona.th_ukazovatel")}</th><th>${esc(s.nazov)}</th>${p ? `<th>${esc(p.nazov)}</th>` : ""}</tr></thead><tbody>${body}</tbody></table></div>`;
  gid("st-sez-por").textContent = por && por.kwh_hdd_zmena_pct != null ? t("stats.sezona.por_info", {pct: f(por.kwh_hdd_zmena_pct, 1), kwh: f(por.kwh_pri_starej_ucinnosti, 0), u: f(por.uspora_kwh, 0)}) : (p ? "" : t("stats.sezona.bez_predosle"));
  evb.hidden = false;
  const ev = arr(r.udalosti);
  evb.innerHTML = `<div class="st-sub"><h2>${t("stats.sezona.udalosti")}</h2></div>` + (ev.length ? `<ul class="st-ev">` + ev.map(e => `<li><b>${fmtD(e.datum)}</b> · ${esc(t("stats.sezona.druh_" + e.druh))}: ${esc(e.text)}</li>`).join("") + `</ul>` : `<p class="sub">${t("stats.sezona.ziadne_udalosti")}</p>`);
}
function renderRng() {
  if (!D.rng) return;
  const rows = arr(D.rng.rows).filter(isObj);
  gid("st-rng-p2").hidden = false; gid("st-rng-p3").hidden = false;
  const p = rngParams();
  gid("st-rng-info").textContent = p.ok ? fmtD(p.from) + " – " + fmtD(p.to) + " · " + tn(S.rngGroup === "day" ? "stats.spolocne.dni" : S.rngGroup === "week" ? "stats.spolocne.tyzdne" : "stats.spolocne.mesiace", rows.length) : "";
  drawRngChart(); renderRngTable(); updateExport();
}
function drawRngChart() {
  const host = gid("st-rng-chart"); if (!host) return;
  if (!D.rng) { host.innerHTML = ""; return; }
  if (!canDraw(host)) return;
  const key = S.rngMetric, m = M(key), W = host.clientWidth, g = S.rngGroup, step = stepMs(g);
  const pts = arr(D.rng.rows).filter(isObj).map(r => ({r, x: rowStart(r)})).filter(pt => pt.x !== null);
  const valOf = r => num(r.dni_s_datami) === 0 ? null : num(r[key]);
  const cur = {id: "r:cur", key, label: t("stats.obdobie.zvolene"), color: "--copper", w: 2.4, xs: pts.map(pt => pt.x), vals: pts.map(pt => valOf(pt.r)), aligned: true, hidden: !!hiddenSer["r:cur"]};
  cur.has = cur.vals.some(v => v !== null);
  const series = [cur]; let prevPts = [];
  if (S.rngYoy && D.rngPrev) {
    prevPts = arr(D.rngPrev.rows).filter(isObj).map(r => ({r, x: rowStart(r)})).filter(pt => pt.x !== null).map(pt => ({r: pt.r, x: shiftYearMs(pt.x)}));
    const pv = {id: "r:prev", key, label: t("stats.obdobie.pred_rokom_dlho"), color: "--cool", w: 2, xs: prevPts.map(pt => pt.x), vals: prevPts.map(pt => valOf(pt.r)), aligned: false, hidden: !!hiddenSer["r:prev"]};
    pv.has = pv.vals.some(v => v !== null); series.push(pv);
  }
  const head = `<div class="st-card-h"><div class="st-card-t">${esc(m.label)} <span class="mut" style="font-weight:400">${esc(m.unit)}</span></div><div class="st-leg">${mkLegend(series.map(s => Object.assign({}, s, {nodata: !s.has})), "lg")}</div></div>`;
  if (!pts.length || !series.some(s => s.has)) { host.innerHTML = `<div class="st-card" style="margin-top:0">${head}${nodata(t("stats.obdobie.ziadne_data_metrika"))}</div>`; return; }
  host.innerHTML = `<div class="st-card" style="margin-top:0">${head}<svg class="st-svg" data-p="rng" role="img" aria-label="${esc(t("stats.obdobie.aria_graf", {m: m.label}))}"></svg></div>`;
  const allx = series.flatMap(s => s.xs);
  let xmin = Math.min(...allx), xmax = Math.max(...allx);
  if (xmax - xmin < 1) { xmin -= step; xmax += step; } else { xmin -= step / 2; xmax += step / 2; }
  const span = (utcMs(rngParams().to || todayS()) - utcMs(rngParams().from || todayS())) / 86400000, nt = Math.max(2, Math.floor((W - PAD.l - PAD.r) / 95)), ticks = [];
  for (let j = 0; j < nt; j++) { const idx = nt === 1 ? 0 : Math.round(j * (pts.length - 1) / (nt - 1)); if (ticks.length && ticks[ticks.length - 1].i === idx) continue;
    const s = msToS(pts[idx].x); ticks.push({i: idx, x: pts[idx].x, label: g === "month" || span > 330 ? (+s.slice(5, 7)) + "/" + s.slice(2, 4) : fmtDS(s)}); }
  const miss = []; pts.forEach((pt, i) => { if (cur.vals[i] === null) miss.push([pt.x - step / 2, pt.x + step / 2]); });
  const p = {svg: host.querySelector("svg"), W, H: 280, series: series.filter(s => s.has), xmin, xmax, ticks, zero: m.unit !== "°C", bars: false, minDx: step, maxGap: step * 1.6, missing: miss, tol: step * .6, dots: pts.length <= 45};
  drawPlot(p);
  const pvs = series[1];
  const tip = i => { const pt = pts[i]; if (!pt) return ""; let html = `<b>${esc(rowLabel(pt.r))}</b>` + (cur.hidden ? "" : trow("--copper", esc(t("stats.obdobie.zvolene")), cur.vals[i] === null ? "–" : esc(fmu(key, cur.vals[i])), cur.vals[i] === null));
    if (pvs && pvs.has && !pvs.hidden) { const j = nearIdx(pvs.xs, pt.x, step * .6); const v = j >= 0 ? pvs.vals[j] : null; html += trow("--cool", esc(t("stats.obdobie.pred_rokom")), v === null ? "–" : esc(fmu(key, v)), v === null) + (j >= 0 ? `<div class="s">${esc(rowLabel(prevPts[j].r))}</div>` : ""); }
    if (num(pt.r.dni_s_datami) !== null) html += `<hr><div class="s">${t("stats.obdobie.dni_s_datami_tip", {n: f(pt.r.dni_s_datami, 0)})}</div>`;
    return html; };
  attachGroup({plots: [p], xs: cur.xs, tip});
}
function renderRngTable() {
  const box = gid("st-rng-table"); if (!box || !D.rng) return;
  const rows = arr(D.rng.rows).filter(isObj), tot = isObj(D.rng.totals) ? D.rng.totals : {};
  if (!rows.length) { box.innerHTML = nodata(t("stats.obdobie.ziadne_riadky")); return; }
  const cols = [S.rngMetric].concat(RNG_COLS.filter(k => k !== S.rngMetric)).filter(k => rows.some(r => num(r[k]) !== null) || num(tot[k]) !== null);
  const head = `<tr><th>${t("stats.obdobie.stlpec_obdobie")}</th><th>${t("stats.obdobie.dni_s_datami")}</th>` + cols.map(k => `<th class="${k === S.rngMetric ? "sel" : ""}">${esc(M(k).label)}<small>${esc(M(k).unit || "")}</small></th>`).join("") + `</tr>`;
  const body = rows.map(r => `<tr class="${num(r.dni_s_datami) === 0 ? "nodata" : ""}"><td>${esc(rowLabel(r))}</td><td>${f(r.dni_s_datami, 0)}</td>` + cols.map(k => `<td class="${k === S.rngMetric ? "sel" : ""}">${fm(k, r[k])}</td>`).join("") + `</tr>`).join("");
  const foot = Object.keys(tot).length ? `<tr class="tot"><td>${t("stats.obdobie.spolu")}</td><td>${f(tot.dni_s_datami !== undefined ? tot.dni_s_datami : rows.reduce((a, r) => a + (num(r.dni_s_datami) || 0), 0), 0)}</td>` + cols.map(k => `<td class="${k === S.rngMetric ? "sel" : ""}">${fm(k, tot[k])}</td>`).join("") + `</tr>` : "";
  box.innerHTML = `<div class="st-scroll"><table class="st-tbl"><thead>${head}</thead><tbody>${body}${foot}</tbody></table></div><p class="sub" style="margin:12px 0 0">${t("stats.obdobie.poznamka")}</p>`;
}

/* ---------- Poruchy ---------- */
function fltParams() {
  const td = todayS();
  if (S.fltMode === "period") {
    const from = validD(S.fltFrom) ? S.fltFrom : addDays(td, -30), to = validD(S.fltTo) ? S.fltTo : td;
    if (from > to) return {ok: false, msg: t("stats.obdobie.zaciatok_po_konci")};
    return {ok: true, from, to, open: false};
  }
  return {ok: true, from: FAR_PAST, to: td, open: S.fltMode === "open"};
}
const fltKey = () => { const p = fltParams(); return p.ok ? [p.from, p.to, p.open].join("|") : "x"; };
function renderFlt() {
  const list = D.flt || [];
  gid("st-flt-count").textContent = list.length ? String(list.length) : "";
  gid("st-flt-list").innerHTML = list.length ? faultsHtml(list, true) : nodata(S.fltMode === "open" ? t("stats.poruchy.ziadna_otvorena") : t("stats.poruchy.ziadne_v_obdobi"));
}
async function loadFlt() {
  const p = fltParams(), my = ++seq.poruchy;
  if (!p.ok) { D.flt = null; D.at.poruchy = 0; setMsg("st-flt-msg", p.msg, "err"); gid("st-flt-list").innerHTML = ""; return; }
  setMsg("st-flt-msg", t("stats.spolocne.nacitavam"));
  const r = await call("api/stats/faults?" + q({from: p.from, to: p.to, open: p.open ? 1 : undefined}));
  if (my !== seq.poruchy) return;
  if (r.error || !Array.isArray(r.items)) { D.flt = null; D.at.poruchy = 0; setMsg("st-flt-msg", t("stats.poruchy.chyba", {e: r.error || t("stats.spolocne.neplatna_odpoved")}), "err"); gid("st-flt-list").innerHTML = ""; return; }
  D.flt = r.items.filter(isObj).slice().sort((a, b) => (num(b.ts_start) || 0) - (num(a.ts_start) || 0));
  D.at.poruchy = Date.now(); D.key.poruchy = fltKey(); setMsg("st-flt-msg", "");
  renderFlt();
}
/* ---------- zdravie čerpadla (/api/zdravie, len správca) ---------- */
const ZST = {ok: "stav_ok", warn: "stav_warn", crit: "stav_crit", nodata: "stav_nodata"};
function zSpark(tr) {
  const v = arr(tr).map(x => num(x)), W = 120, H = 28, ok = v.filter(x => x !== null);
  if (ok.length < 2) return "";
  const mx = Math.max(10, ...ok.map(Math.abs)), n = v.length;
  const X = i => 2 + i * (W - 4) / Math.max(1, n - 1), Y = x => H / 2 - x / mx * (H / 2 - 3);
  let d = "", pen = false;
  v.forEach((x, i) => { if (x === null) { pen = false; return; } d += (pen ? "L" : "M") + X(i).toFixed(1) + " " + Y(x).toFixed(1); pen = true; });
  const txt = t("stats.zdravie.trend_aria", {v: v.map(x => x === null ? "–" : f(x, 0) + " %").join(", ")});
  return `<svg class="zd-spark" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="${esc(txt)}"><line x1="0" x2="${W}" y1="${H / 2}" y2="${H / 2}" stroke="currentColor" stroke-opacity=".25" stroke-dasharray="3 3"/><path d="${d}" fill="none" stroke="currentColor" stroke-width="1.6"/></svg>`;
}
function zInd(i) {
  const st = ZST[i.stav] ? i.stav : "nodata";
  const kod = i.kod;
  let det = "";
  if (kod === "odmrazovanie" && (num(i.pocet_zmena) !== null || num(i.trvanie_zmena) !== null)) det = t("stats.zdravie.odmraz_detail", {p: f(i.pocet_zmena, 0), d: f(i.trvanie_zmena, 0)});
  else if (kod === "cyklovanie" && num(i.kratke_nedavne) !== null) det = t("stats.zdravie.kratke_detail", {a: f(i.kratke_nedavne, 0), b: f(i.kratke_zaklad, 0)});
  else if (num(i.nedavne) !== null) det = t("stats.zdravie.hodnoty", {a: f(i.nedavne, 2), b: f(i.zaklad, 2)});
  return `<div class="zd-row zd-${st}"><span class="zd-led" aria-hidden="true"></span><div class="zd-main"><div class="zd-h"><b>${esc(i.nazov)}</b> <span class="st-badge">${t("stats.zdravie." + ZST[st])}</span></div>` +
    `<div class="mut">${esc(i.popis)}</div>` +
    `<div class="zd-v">${num(i.zmena_pct) !== null ? t("stats.zdravie.zmena", {v: (i.zmena_pct > 0 ? "+" : "") + f(i.zmena_pct, 0)}) + " · " : ""}${t("stats.zdravie.prah", {v: f(i.prah_pct, 0)})}${det ? " · " + esc(det) : ""}</div></div>` +
    `<div class="zd-tr" title="${esc(t("stats.zdravie.trend_zaklad"))}">${zSpark(i.trend)}</div></div>`;
}
const zDate = ts => { const n = num(ts); return n === null ? "–" : dstr(new Date(n * 1000)); };
function zFind(n) { return `<li><b>${esc(n.nazov)}</b> – ${esc(n.text)} <span class="mut">${zDate(n.od)}${n.do ? " · " + t("stats.zdravie.zrusene", {d: zDate(n.do)}) : ""}</span></li>`; }
function renderZdravie() {
  const z = D.zdr, box = gid("st-zd-body"); if (!box) return;
  if (!isObj(z)) { box.innerHTML = ""; return; }
  if (z.enabled === false) { box.innerHTML = nodata(t("stats.zdravie.vypnute")); return; }
  const o = isObj(z.okno) ? z.okno : {}, ne = arr(o.nedavne), za = arr(o.zaklad);
  let h = `<div class="st-msg">${t("stats.zdravie.okno", {od: ne[0] || "–", do: ne[1] || "–", zod: za[0] || "–", zdo: za[1] || "–", n: o.zaklad_dni || 0})}</div>`;
  if (z.dost_dat === false) h += nodata(t("stats.zdravie.malo_dat", {n: o.zaklad_dni || 0, min: o.zaklad_min_dni || 21}));
  h += `<div class="zd-list">` + arr(z.indikatory).filter(isObj).map(zInd).join("") + `</div>`;
  const ch = isObj(z.chyby) ? arr(z.chyby.kody) : [];
  h += `<p class="st-h3" style="margin-top:20px">${t("stats.zdravie.chyby")}</p>` + (ch.length ? `<div>${ch.map(x => `<span class="tag">${esc(t("stats.zdravie.chyby_pocet", {kod: x.kod, n: x.pocet}))}</span> `).join("")}</div>` : `<div class="mut">${t("stats.zdravie.ziadne_chyby")}</div>`);
  const nz = isObj(z.nalezy) ? z.nalezy : {}, ak = arr(nz.aktivne), hi = arr(nz.historia);
  h += `<p class="st-h3" style="margin-top:20px">${t("stats.zdravie.nalezy")}</p><p class="mut" style="margin:0">${t("stats.zdravie.aktivne")}</p>` + (ak.length ? `<ul class="zd-find">${ak.map(zFind).join("")}</ul>` : `<div class="mut">${t("stats.zdravie.ziadne_nalezy")}</div>`);
  if (hi.length) h += `<p class="mut" style="margin:10px 0 0">${t("stats.zdravie.historia")}</p><ul class="zd-find">${hi.map(zFind).join("")}</ul>`;
  box.innerHTML = h;
}
async function loadZdravie() {
  const my = ++seq.zdravie;
  setMsg("st-zd-msg", t("stats.spolocne.nacitavam"));
  const r = await call("api/zdravie");
  if (my !== seq.zdravie) return;
  if (r.error) { D.zdr = null; D.at.zdravie = 0; setMsg("st-zd-msg", t("stats.zdravie.chyba", {e: r.error}), "err"); gid("st-zd-body").innerHTML = ""; return; }
  D.zdr = r; D.at.zdravie = Date.now(); D.key.zdravie = "z"; setMsg("st-zd-msg", "");
  renderZdravie();
}
function renderCodes() {
  const box = gid("st-codes-list"), dl = gid("st-codelist"); if (!box) return;
  if (D.codes === false) { box.innerHTML = `<div class="st-msg err">${t("stats.kody.chyba")}</div>`; return; }
  if (!D.codes) { box.innerHTML = `<div class="st-msg">${t("stats.spolocne.nacitavam")}</div>`; return; }
  dl.innerHTML = D.codes.map(c => `<option value="${esc(c.code)}">${esc(c.title || "")}</option>`).join("");
  box.innerHTML = `<div class="st-codes">` + D.codes.map(c => { const ro = c.editable === 0 || c.editable === false;
    return `<div class="st-code" data-code="${esc(c.code)}"><div class="st-fh"><span class="tag">${esc(c.code)}</span>${ro ? `<span class="mut">${t("stats.kody.len_citanie")}</span>` : ""}</div>` +
      `<div class="dgrid"><label class="dl">${t("stats.kody.nazov")}<input class="in" data-f="title" maxlength="120" value="${esc(c.title || "")}"${ro ? " disabled" : ""}></label></div>` +
      `<label class="dl">${t("stats.kody.popis")}<textarea data-f="description" maxlength="600"${ro ? " disabled" : ""}>${esc(c.description || "")}</textarea></label>` +
      `<label class="dl">${t("stats.porucha.fix_label")}<textarea data-f="fix" maxlength="600"${ro ? " disabled" : ""}>${esc(c.fix || "")}</textarea></label>` +
      (ro ? "" : `<div class="btnrow" style="margin-top:0"><button type="button" class="btn sm primary" data-act="code-save" data-code="${esc(c.code)}">${t("stats.spolocne.ulozit")}</button><span class="st-msg st-codemsg" role="status"></span></div>`) + `</div>`; }).join("") + `</div>`;
}
async function loadCodes() {
  const r = await call("api/stats/codes");
  D.codes = r.error || !Array.isArray(r.items) ? false : r.items.filter(x => isObj(x) && x.code);
  renderCodes();
}
const delBtn = id => `<button type="button" class="btn sm danger" data-act="fault-ask" data-id="${esc(id)}">${t("stats.porucha.zmazat")}</button>`;
function faultAsk(btn) {
  const box = btn.closest(".st-fa"); if (!box) return;
  box.innerHTML = `<span class="st-ask" role="alertdialog" aria-label="${t("stats.porucha.potvrdenie")}">${t("stats.porucha.zmazat_otazka")} <button type="button" class="btn sm danger" data-act="fault-yes" data-id="${esc(btn.dataset.id)}">${t("stats.porucha.ano_zmazat")}</button><button type="button" class="btn sm" data-act="fault-no" data-id="${esc(btn.dataset.id)}">${t("stats.porucha.nie")}</button></span>`;
  const y = box.querySelector("[data-act=fault-yes]"); if (y) y.focus();
}
function faultNo(btn) { const box = btn.closest(".st-fa"); if (!box) return; box.innerHTML = delBtn(btn.dataset.id); const b = box.querySelector("button"); if (b) b.focus(); }
async function faultYes(btn) {
  const box = btn.closest(".st-fa"), raw = btn.dataset.id, id = /^\d+$/.test(raw) ? Number(raw) : raw;
  btn.disabled = true;
  const r = await call("api/stats/fault_delete", {id});
  if (r.error) { if (box) box.innerHTML = `<span class="st-msg err">${esc(r.error)}</span> ${delBtn(raw)}`; return; }
  D.at.den = 0; D.at.poruchy = 0;
  if (S.view === "den") loadDay(); else loadFlt();
}
function parseDT(s) {
  const m = /^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2})$/.exec(s); if (!m) return null;
  const d = new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5]);
  return d.getFullYear() === +m[1] && d.getMonth() === +m[2] - 1 && d.getDate() === +m[3] && d.getHours() === +m[4] ? d.getTime() : null;
}
async function submitFault() {
  const from = gid("st-f-from").value.trim(), to = gid("st-f-to").value.trim(), code = gid("st-f-code").value.trim().toUpperCase(),
    title = gid("st-f-title").value.trim(), text = gid("st-f-text").value.trim(), a = parseDT(from), b = to ? parseDT(to) : null;
  if (a === null) return setMsg("st-f-msg", t("stats.pridat.cas_od"), "err");
  if (to && b === null) return setMsg("st-f-msg", t("stats.pridat.cas_do"), "err");
  if (b !== null && b < a) return setMsg("st-f-msg", t("stats.pridat.koniec_pred_zaciatkom"), "err");
  if (!/^[A-Z0-9_-]{1,24}$/.test(code)) return setMsg("st-f-msg", t("stats.pridat.kod_poruchy"), "err");
  const body = {ts_start: from, code}; if (to) body.ts_end = to; if (title) body.title = title; if (text) body.text = text;
  const go = gid("st-f-go"); go.disabled = true; setMsg("st-f-msg", t("stats.spolocne.ukladam"));
  const r = await call("api/stats/fault", body);
  go.disabled = false;
  if (r.error) return setMsg("st-f-msg", t("stats.pridat.chyba", {e: r.error}), "err");
  gid("st-f-to").value = ""; gid("st-f-title").value = ""; gid("st-f-text").value = "";
  setMsg("st-f-msg", t("stats.pridat.zapisana"), "ok"); D.at.den = 0; loadFlt();
}
async function saveCode(btn) {
  const row = btn.closest(".st-code"), msg = row && row.querySelector(".st-codemsg"); if (!row) return;
  const val = k => { const el = row.querySelector(`[data-f="${k}"]`); return el ? el.value.trim() : ""; };
  const title = val("title"); if (!title) { if (msg) { msg.textContent = t("stats.kody.nazov_prazdny"); msg.className = "st-msg err st-codemsg"; } return; }
  btn.disabled = true; if (msg) { msg.textContent = t("stats.spolocne.ukladam"); msg.className = "st-msg st-codemsg"; }
  const r = await call("api/stats/fault_code", {code: btn.dataset.code, title, description: val("description"), fix: val("fix")});
  btn.disabled = false;
  if (msg) { msg.textContent = r.error ? t("stats.kody.nepodarilo_ulozit", {e: r.error}) : t("stats.spolocne.ulozene"); msg.className = "st-msg " + (r.error ? "err" : "ok") + " st-codemsg"; }
  if (!r.error) { D.at.den = 0; D.at.poruchy = 0; }
}
async function newCode(btn) {
  const code = gid("st-nc-code").value.trim().toUpperCase(), title = gid("st-nc-title").value.trim();
  if (!/^[A-Z0-9_-]{1,24}$/.test(code)) return setMsg("st-nc-msg", t("stats.kody.zadaj_kod"), "err");
  if (!title) return setMsg("st-nc-msg", t("stats.kody.zadaj_nazov"), "err");
  btn.disabled = true; setMsg("st-nc-msg", t("stats.spolocne.ukladam"));
  const r = await call("api/stats/fault_code", {code, title, description: gid("st-nc-desc").value.trim(), fix: gid("st-nc-fix").value.trim()});
  btn.disabled = false;
  if (r.error) return setMsg("st-nc-msg", t("stats.kody.nepodarilo_ulozit", {e: r.error}), "err");
  for (const id of ["st-nc-code", "st-nc-title", "st-nc-desc", "st-nc-fix"]) gid(id).value = "";
  setMsg("st-nc-msg", t("stats.kody.ulozeny"), "ok"); D.at.den = 0; D.at.poruchy = 0; loadCodes();
}

/* ---------- prepínanie pohľadov, načítanie, kreslenie ---------- */
function redraw() {
  if (S.view === "den") drawDayCharts(); else if (S.view === "porovnanie") drawCmpChart(); else if (S.view === "obdobie") drawRngChart();
}
function syncControls() {
  const td = todayS();
  const day = gid("st-day"); day.value = S.day; day.max = td;
  gid("st-next").disabled = S.day >= td;
  gid("st-cmp-base").max = td; gid("st-cmp-new").max = td; gid("st-cmp-base").value = S.cmp[0] || "";
  fillMetricSelects();
  root.querySelectorAll("#st-views button").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.v === S.view)));
  root.querySelectorAll("#st-rng-presets button").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.p === S.rngPreset)));
  root.querySelectorAll("#st-rng-groups button").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.g === S.rngGroup)));
  root.querySelectorAll("#st-flt-modes button").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.m === S.fltMode)));
  root.querySelectorAll("#st-day-cols button").forEach(b => b.setAttribute("aria-pressed", String(!!S.cols[b.dataset.g])));
  gid("st-rng-yoy").checked = S.rngYoy;
  gid("st-rng-custom").hidden = S.rngPreset !== "custom";
  if (S.rngPreset === "custom") { gid("st-rng-from").value = S.rngFrom; gid("st-rng-to").value = S.rngTo; }
  gid("st-rng-from").max = td; gid("st-rng-to").max = td; gid("st-flt-from").max = td; gid("st-flt-to").max = td;
  gid("st-flt-period").hidden = S.fltMode !== "period";
  gid("st-flt-from").value = validD(S.fltFrom) ? S.fltFrom : addDays(td, -30); gid("st-flt-to").value = validD(S.fltTo) ? S.fltTo : td;
  if (!gid("st-f-from").value) { const n = new Date(); gid("st-f-from").value = dstr(n) + " " + pad2(n.getHours()) + ":" + pad2(n.getMinutes()); }
}
function loadView(v, force) {
  const fresh = k => !force && D.at[v] && D.key[v] === k && Date.now() - D.at[v] < 60000;
  if (v === "den") { if (D.day && fresh(S.day)) renderDay(); else loadDay(); }
  else if (v === "porovnanie") {
    if (D.quickBase !== S.cmp[0] || D.quick === false) loadQuick(); else renderCmpControls();
    if (D.cmp && fresh(S.cmp.join(","))) renderCmp(); else loadCmp();
  } else if (v === "obdobie") { if (D.rng && fresh(rngKey())) renderRng(); else loadRng(); }
  else if (v === "zdravie") { if (D.zdr && fresh("z")) renderZdravie(); else loadZdravie(); }
  else if (v === "sezona") { if (D.sez && fresh(String(sezRok))) renderSezona(); else loadSezona(); }
  else if (v === "poruchy") { if (D.codes === null) loadCodes(); else renderCodes(); if (D.flt && fresh(fltKey())) renderFlt(); else loadFlt(); }
}
function setView(v, noLoad) {
  if (!VIEWS.includes(v)) v = "den";
  S.view = v; saveState();
  root.querySelectorAll("#st-views button").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.v === v)));
  for (const x of VIEWS) gid("st-v-" + x).hidden = x !== v;
  tipHide(); updateExport();
  if (!noLoad) loadView(v, false);
}

/* ---------- obsluha udalostí ---------- */
function toggleSeries(sid) {
  hiddenSer[sid] = !hiddenSer[sid]; redraw();
  const nb = root.querySelector(`[data-act="lg"][data-sid="${sid}"]`); if (nb) nb.focus();
}
function onClick(e) {
  const b = e.target.closest ? e.target.closest("[data-act]") : null; if (!b || !root.contains(b)) return;
  const a = b.dataset.act;
  if (a === "view") setView(b.dataset.v);
  else if (a === "day-prev") shiftDay(-1);
  else if (a === "day-next") shiftDay(1);
  else if (a === "day-today") gotoDay(todayS());
  else if (a === "col") { const g = b.dataset.g; if (g in S.cols) { S.cols[g] = !S.cols[g]; b.setAttribute("aria-pressed", String(S.cols[g])); saveState(); renderHours(); } }
  else if (a === "lg") toggleSeries(b.dataset.sid);
  else if (a === "cmp-chip") cmpAdd(b.dataset.d, true);
  else if (a === "cmp-add") { const v = gid("st-cmp-new").value; if (!v) { setMsg("st-cmp-msg", t("stats.porovnanie.vyber_datum"), "err"); return; } setMsg("st-cmp-msg", ""); cmpAdd(v, false); gid("st-cmp-new").value = ""; }
  else if (a === "cmp-del") { const i = S.cmp.indexOf(b.dataset.d); if (i >= 0 && S.cmp.length > 1) { S.cmp.splice(i, 1); setMsg("st-cmp-msg", ""); cmpChanged(); } }
  else if (a === "rng-preset") { S.rngPreset = b.dataset.p;
    if (S.rngPreset === "custom" && !(validD(S.rngFrom) && validD(S.rngTo))) { S.rngTo = todayS(); S.rngFrom = addDays(S.rngTo, -29); }
    saveState(); syncControls(); loadRng(); }
  else if (a === "rng-grp") { S.rngGroup = b.dataset.g; saveState(); syncControls(); loadRng(); }
  else if (a === "flt-mode") { S.fltMode = b.dataset.m; saveState(); syncControls(); loadFlt(); }
  else if (a === "sez-prev") { sezRok = (D.sez ? D.sez.sezona.rok : new Date().getFullYear()) - 1; loadSezona(); }
  else if (a === "sez-next") { sezRok = (D.sez ? D.sez.sezona.rok : new Date().getFullYear()) + 1; loadSezona(); }
  else if (a === "fault-ask") faultAsk(b);
  else if (a === "fault-no") faultNo(b);
  else if (a === "fault-yes") faultYes(b);
  else if (a === "code-save") saveCode(b);
  else if (a === "code-new") newCode(b);
}
function onChange(e) {
  const el = e.target, id = el.id;
  if (id === "st-day") { if (validD(el.value)) gotoDay(el.value); else el.value = S.day; }
  else if (id === "st-cmp-base") {
    if (!validD(el.value) || el.value > todayS()) { el.value = S.cmp[0] || ""; return; }
    S.cmp = [el.value].concat(S.cmp.slice(1).filter(d => d !== el.value)); setMsg("st-cmp-msg", ""); cmpChanged();
  }
  else if (id === "st-cmp-metric") { S.cmpMetric = el.value; saveState(); drawCmpChart(); }
  else if (id === "st-rng-from" || id === "st-rng-to") { if (id === "st-rng-from") S.rngFrom = validD(el.value) ? el.value : ""; else S.rngTo = validD(el.value) ? el.value : ""; S.rngPreset = "custom"; saveState(); loadRng(); }
  else if (id === "st-rng-metric") { S.rngMetric = el.value; saveState(); renderRng(); }
  else if (id === "st-rng-yoy") { S.rngYoy = el.checked; saveState(); loadRng(); }
  else if (id === "st-flt-from" || id === "st-flt-to") { if (id === "st-flt-from") S.fltFrom = validD(el.value) ? el.value : ""; else S.fltTo = validD(el.value) ? el.value : ""; saveState(); loadFlt(); }
}
function onKey(e) {
  if (!visible || S.view !== "den" || e.altKey || e.ctrlKey || e.metaKey || e.shiftKey) return;
  const tg = e.target && e.target.tagName; if (/^(INPUT|TEXTAREA|SELECT)$/.test(tg || "")) return;
  if (e.key === "ArrowLeft") { e.preventDefault(); shiftDay(-1); } else if (e.key === "ArrowRight") { e.preventDefault(); shiftDay(1); }
}

/* ---------- kostra ---------- */
function build() {
  if (built) return true;
  const host = gid(ROOT_ID); if (!host) return false;
  root = host;
  const seg = (id, label, act, key, items) => `<div class="st-seg" id="${id}" role="group" aria-label="${label}">` + items.map(i => `<button type="button" data-act="${act}" data-${key}="${i[0]}" aria-pressed="false">${i[1]}</button>`).join("") + `</div>`;
  host.innerHTML =
`<section class="panel pad st-head">
  <div class="st-bar"><h2>${t("stats.titul")}</h2>${seg("st-views", t("stats.pohlad.aria"), "view", "v", VIEWS.map(k => [k, t("stats.pohlad." + k)]))}<span class="st-grow"></span><a class="btn" id="st-export" href="#" rel="noopener">${t("stats.export.csv")}</a></div>
  <div class="st-cov" id="st-cov" role="status">${t("stats.pokrytie.nacitavam")}</div>
</section>
<div class="st-view" id="st-v-den" hidden>
  <section class="panel pad">
    <div class="st-bar"><div class="st-title" id="st-day-title"></div><span id="st-day-badge"></span><span class="st-grow"></span>
      <div class="st-nav"><button type="button" class="btn icon" id="st-prev" data-act="day-prev" aria-label="${t("stats.den.predosly")}" title="${t("stats.den.predosly_tip")}">‹</button><input type="date" id="st-day" aria-label="${t("stats.den.datum")}"><button type="button" class="btn icon" id="st-next" data-act="day-next" aria-label="${t("stats.den.nasledujuci")}" title="${t("stats.den.nasledujuci_tip")}">›</button><button type="button" class="btn" id="st-today" data-act="day-today">${t("stats.den.dnes")}</button></div></div>
    <div class="st-msg" id="st-day-msg" role="status" style="margin-top:8px"></div>
    <div id="st-day-sum">
      <div class="st-nod" id="st-day-nodata" hidden>${t("stats.den.ziadne_data")}</div>
      <div class="stats st-stats" id="st-day-tiles"></div><div id="st-day-rooms"></div>
      <div id="st-day-tlwrap"><p class="st-h3" style="margin-top:24px">${t("stats.den.casova_os")}</p><div id="st-day-tl"></div></div>
    </div>
  </section>
  <section class="panel pad" id="st-day-pcal"><div class="st-sub"><h2>${t("stats.kal.titul")}</h2><span class="sub">${t("stats.kal.podtitul")}</span></div><div id="st-day-cal"></div></section>
  <section class="panel pad" id="st-day-p2"><div class="st-sub"><h2>${t("stats.den.grafy")}</h2><span class="sub">${t("stats.den.grafy_info")}</span></div><div id="st-day-charts"></div></section>
  <section class="panel pad" id="st-day-p3"><div class="st-sub"><h2>${t("stats.den.hodina_po_hodine")}</h2><span class="sub" id="st-hours-info"></span></div>
    <div class="st-cols" id="st-day-cols" role="group" aria-label="${t("stats.den.skupiny_stlpcov")}">${colGroups().map(g => `<button type="button" class="chip st-ch" data-act="col" data-g="${g[0]}" aria-pressed="${!!S.cols[g[0]]}">${g[1]}</button>`).join("")}</div>
    <div class="st-scroll" id="st-day-hours"></div></section>
  <section class="panel pad" id="st-day-p4"><div class="st-sub"><h2>${t("stats.cykly.titul")}</h2><span class="sub" id="st-cyc-info"></span></div><div class="st-scroll" id="st-day-cycles"></div></section>
  <section class="panel pad" id="st-day-p5"><div class="st-sub"><h2>${t("stats.den.poruchy_dna")}</h2><span class="sub" id="st-dfault-info"></span></div><div id="st-day-faults"></div></section>
</div>
<div class="st-view" id="st-v-porovnanie" hidden>
  <section class="panel pad">
    <div class="st-sub"><h2>${t("stats.porovnanie.titul")}</h2><span class="sub">${t("stats.porovnanie.podtitul")}</span></div>
    <div class="st-ctl">
      <label class="st-fld">${t("stats.porovnanie.zakladny_den")}<input type="date" class="in" id="st-cmp-base"></label>
      <div class="st-fld"><label for="st-cmp-new">${t("stats.porovnanie.pridat_iny")}</label><div class="st-nav"><input type="date" class="in" id="st-cmp-new"><button type="button" class="btn sm" id="st-cmp-add" data-act="cmp-add">${t("stats.porovnanie.pridat")}</button></div></div>
      <label class="st-fld">${t("stats.spolocne.metrika")}<select class="sel" id="st-cmp-metric"></select></label>
    </div>
    <p class="st-h3" style="margin-top:20px">${t("stats.porovnanie.rychle")}</p><div class="st-chips" id="st-cmp-quick"></div>
    <p class="st-h3" style="margin-top:20px">${t("stats.porovnanie.vybrane")}</p><div class="st-selected" id="st-cmp-sel"></div>
    <div class="st-msg" id="st-cmp-msg" role="status" style="margin-top:10px"></div>
  </section>
  <section class="panel pad" id="st-cmp-p2"><div id="st-cmp-warn"></div><div id="st-cmp-chart" style="margin-top:12px"></div></section>
  <section class="panel pad" id="st-cmp-p3"><div class="st-sub"><h2>${t("stats.porovnanie.suhrny")}</h2></div><div id="st-cmp-table"></div></section>
</div>
<div class="st-view" id="st-v-obdobie" hidden>
  <section class="panel pad">
    <div class="st-sub"><h2>${t("stats.pohlad.obdobie")}</h2><span class="sub" id="st-rng-info"></span></div>
    <div class="st-ctl">
      <div class="st-fld"><span>${t("stats.obdobie.rozsah")}</span>${seg("st-rng-presets", t("stats.obdobie.rozsah"), "rng-preset", "p", [["7", t("stats.obdobie.p7")], ["30", t("stats.obdobie.p30")], ["month", t("stats.obdobie.mesiac_tento")], ["year", t("stats.obdobie.rok_tento")], ["custom", t("stats.obdobie.vlastny")]])}</div>
      <div class="st-fld"><span>${t("stats.obdobie.granularita")}</span>${seg("st-rng-groups", t("stats.obdobie.granularita"), "rng-grp", "g", [["day", t("stats.obdobie.g_den")], ["week", t("stats.obdobie.g_tyzden")], ["month", t("stats.obdobie.g_mesiac")]])}</div>
    </div>
    <div class="st-ctl" id="st-rng-custom" style="margin-top:12px" hidden>
      <label class="st-fld">${t("stats.spolocne.od")}<input type="date" class="in" id="st-rng-from"></label><label class="st-fld">${t("stats.spolocne.do")}<input type="date" class="in" id="st-rng-to"></label>
    </div>
    <div class="st-ctl" style="margin-top:12px">
      <label class="st-fld">${t("stats.spolocne.metrika")}<select class="sel" id="st-rng-metric"></select></label>
      <label class="st-chk"><span class="sw"><input type="checkbox" id="st-rng-yoy"><i></i></span>${t("stats.obdobie.porovnat_rok")}</label>
    </div>
    <div class="st-msg" id="st-rng-msg" role="status" style="margin-top:10px"></div>
  </section>
  <section class="panel pad" id="st-rng-p2"><div id="st-rng-chart"></div></section>
  <section class="panel pad" id="st-rng-p3"><div class="st-sub"><h2>${t("stats.obdobie.tabulka")}</h2></div><div id="st-rng-table"></div></section>
</div>
<div class="st-view" id="st-v-zdravie" hidden>
  <section class="panel pad">
    <div class="st-sub"><h2>${t("stats.zdravie.titul")}</h2><span class="sub">${t("stats.zdravie.podtitul")}</span></div>
    <div class="st-msg" id="st-zd-msg" role="status"></div>
    <div id="st-zd-body"></div>
  </section>
<div class="st-view" id="st-v-sezona" hidden>
  <section class="panel pad">
    <div class="st-sub"><h2>${t("stats.sezona.titul")}</h2><span class="sub" id="st-sez-info"></span></div>
    <div class="st-ctl"><button type="button" class="btn" data-act="sez-prev" aria-label="${t("stats.sezona.predosla_sezona")}">◀</button><button type="button" class="btn" data-act="sez-next" aria-label="${t("stats.sezona.nasledujuca_sezona")}">▶</button></div>
    <div class="st-msg" id="st-sez-msg" role="status" style="margin-top:10px"></div>
    <div id="st-sez-table" style="margin-top:8px"></div>
    <p class="sub" id="st-sez-por" style="margin:12px 0 0"></p>
  </section>
  <section class="panel pad" id="st-sez-ev"></section>
</div>
<div class="st-view" id="st-v-poruchy" hidden>
  <section class="panel pad">
    <div class="st-sub"><h2>${t("stats.pohlad.poruchy")}</h2><span class="sub" id="st-flt-count"></span></div>
    <div class="st-ctl">${seg("st-flt-modes", t("stats.poruchy.filter"), "flt-mode", "m", [["open", t("stats.poruchy.len_otvorene")], ["all", t("stats.poruchy.vsetky")], ["period", t("stats.pohlad.obdobie")]])}
      <div class="st-ctl" id="st-flt-period" hidden><label class="st-fld">${t("stats.spolocne.od")}<input type="date" class="in" id="st-flt-from"></label><label class="st-fld">${t("stats.spolocne.do")}<input type="date" class="in" id="st-flt-to"></label></div></div>
    <div class="st-msg" id="st-flt-msg" role="status" style="margin-top:10px"></div>
    <div id="st-flt-list" style="margin-top:8px"></div>
  </section>
  <section class="panel pad">
    <div class="st-sub"><h2>${t("stats.pridat.titul")}</h2><span class="sub">${t("stats.pridat.podtitul")}</span></div>
    <form id="st-fform" autocomplete="off" novalidate>
      <div class="dgrid">
        <label class="dl">${t("stats.pridat.od")}<input class="in" id="st-f-from" placeholder="2026-11-12 14:30" maxlength="16"></label>
        <label class="dl">${t("stats.pridat.do")}<input class="in" id="st-f-to" placeholder="2026-11-12 16:00" maxlength="16"></label>
        <label class="dl">${t("stats.pridat.kod")}<input class="in" id="st-f-code" list="st-codelist" maxlength="24" placeholder="${t("stats.pridat.kod_priklad")}"></label>
        <label class="dl">${t("stats.pridat.nazov")}<input class="in" id="st-f-title" maxlength="120"></label>
      </div>
      <label class="dl" style="margin-top:12px">${t("stats.pridat.poznamka")}<textarea class="st-ta" id="st-f-text" maxlength="600" rows="2"></textarea></label>
      <div class="btnrow"><button type="submit" class="btn primary" id="st-f-go">${t("stats.pridat.tlacidlo")}</button><span class="st-msg" id="st-f-msg" role="status"></span></div>
    </form>
    <datalist id="st-codelist"></datalist>
  </section>
  <section class="panel pad">
    <details id="st-codes"><summary>${t("stats.kody.titul")}</summary>
      <div id="st-codes-list" style="margin-top:12px"></div>
      <div class="st-code" style="margin-top:6px"><div class="st-fh"><b class="t">${t("stats.kody.novy")}</b></div>
        <div class="dgrid"><label class="dl">${t("stats.pridat.kod")}<input class="in" id="st-nc-code" maxlength="24"></label><label class="dl">${t("stats.kody.nazov")}<input class="in" id="st-nc-title" maxlength="120"></label></div>
        <label class="dl">${t("stats.kody.popis")}<textarea class="st-ta" id="st-nc-desc" maxlength="600"></textarea></label>
        <label class="dl">${t("stats.porucha.fix_label")}<textarea class="st-ta" id="st-nc-fix" maxlength="600"></textarea></label>
        <div class="btnrow" style="margin-top:0"><button type="button" class="btn sm primary" data-act="code-new">${t("stats.kody.pridat")}</button><span class="st-msg" id="st-nc-msg" role="status"></span></div></div>
    </details>
  </section>
</div>`;
  tipEl = document.createElement("div"); tipEl.className = "st-tip"; tipEl.setAttribute("aria-hidden", "true"); document.body.appendChild(tipEl);
  host.addEventListener("click", onClick); host.addEventListener("change", onChange);
  host.addEventListener("submit", e => { if (e.target && e.target.id === "st-fform") { e.preventDefault(); submitFault(); } });
  document.addEventListener("keydown", onKey);
  const tl = gid("st-day-tl");
  tl.addEventListener("pointermove", ev => { const el = ev.target.closest ? ev.target.closest("i[data-i]") : null, x = el && tl._tl ? tl._tl[+el.dataset.i] : null;
    if (!x) { tipHide(); return; }
    tipShow(`<b>${hm(x.from)} – ${hm(x.to)}</b>` + trow(null, esc(tlName(x.state) || x.state), esc(dur((x.to - x.from) / 60))), ev.clientX, ev.clientY); });
  tl.addEventListener("pointerleave", tipHide);
  loadState();
  built = true; D.codes = null;
  syncControls(); renderCal();
  return true;
}

/* ---------- verejné API ---------- */
function show() {
  if (!build()) return;
  visible = true;
  if (!D.meta || Date.now() - (D.at.meta || 0) > 300000) loadMeta();
  setView(S.view, true);
  loadView(S.view, false);
}
function hide() { visible = false; tipHide(); }
function resize() { if (visible && built) redraw(); }
window.StatsUI = {show, hide, resize};
})();
